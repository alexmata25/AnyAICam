"""Rich alert emails (2026-09-27).

Alert emails used to carry only a title and a one-line message ("Person
detected / Camera 1 detected a person"). Customers could not tell which
camera, when, or what happened without logging in. An alert email now
carries:

- the camera's name and the event's local time (stored timestamps are UTC;
  shown in the same display timezone the Notifications page uses);
- the event thumbnail, embedded inline (no expiring links, and it works in
  mail clients that block remote images) whenever the event has one;
- a "View event video" button: the canonical Playback deep link for the
  event's own clip (main._customer_event_playback_href), the Voice Call
  screen for a visitor call, or the alerts list otherwise.

Detection media (thumbnail and clip) reach the cloud 60-150 s after the
event itself, so detection alerts are held briefly (``pending_media``) and
sent by notification_retry_worker as soon as the thumbnail exists, or after
MEDIA_WAIT_SECONDS with a link only. Visitor calls and camera/appliance/
storage problems are never held -- they are time-critical or have no media.

Everything here only reads; delivery bookkeeping stays in
notification_engine / notification_retry_worker.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

logger = logging.getLogger("anyaicam.notification_email")

# Event types whose alert is about something a camera captured, so the
# cloud receives a thumbnail/clip for it shortly after the event.
MEDIA_EVENT_TYPES = frozenset({"person", "vehicle", "smart_motion", "motion", "ppe", "lpr", "facial_recognition", "people_counting"})
MEDIA_WAIT_SECONDS = max(0, int(os.environ.get("ANYAICAM_ALERT_EMAIL_MEDIA_WAIT_SECONDS", "180")))
THUMBNAIL_CID = "event-thumbnail"
DEFAULT_DISPLAY_TIMEZONE = "America/Chicago"  # main.APPLIANCE_TIMEZONE


def waits_for_media(event_type: str, event_id: str | None) -> bool:
    """Should this alert's email be held until its media arrives?"""
    return bool(event_id) and event_type in MEDIA_EVENT_TYPES and MEDIA_WAIT_SECONDS > 0


def public_base_url() -> str:
    base = os.environ.get("ANYAICAM_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return base
    reset = os.environ.get("ANYAICAM_PASSWORD_RESET_URL", "").strip()
    parts = urlsplit(reset)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _display_timezone():
    try:
        import main
        return main.APPLIANCE_TIMEZONE
    except Exception:
        return ZoneInfo(os.environ.get("ANYAICAM_DISPLAY_TIMEZONE", DEFAULT_DISPLAY_TIMEZONE))


def local_time_label(timestamp: str | None, tz=None) -> str:
    """'Sat, Sep 27 at 1:57 PM CDT' from a stored (UTC, often naive) timestamp."""
    if not timestamp:
        return ""
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return str(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(tz or _display_timezone())
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a, %b')} {local.day} at {hour}:{local.strftime('%M %p')} {local.tzname() or ''}".strip()


def alert_context(db, notification_id: str) -> dict | None:
    """Everything a rich alert email needs, read from the database."""
    row = db.execute(
        "SELECT n.id, n.customer_id, n.camera_id, n.event_id, n.event_type, n.title, n.message, n.timestamp, n.created_at, "
        "c.name AS camera_name FROM notifications n LEFT JOIN cameras c ON c.id = n.camera_id WHERE n.id = ?",
        (notification_id,),
    ).fetchone()
    if not row:
        return None
    context = {key: row[key] for key in row.keys()}
    context["thumbnail_s3_key"] = None
    context["has_clip"] = False
    if context["event_type"] == "aac_voice_call" and context["event_id"]:
        try:
            call = db.execute("SELECT thumbnail_s3_key FROM aac_voice_call_events WHERE id = ?", (context["event_id"],)).fetchone()
            context["thumbnail_s3_key"] = call["thumbnail_s3_key"] if call else None
        except Exception:
            pass
    elif context["event_id"]:
        media = db.execute(
            "SELECT thumbnail_s3_key, s3_key FROM detection_event_media WHERE detection_event_id = ? "
            "ORDER BY (length(coalesce(thumbnail_s3_key,''))>0) DESC, started_at DESC LIMIT 1",
            (context["event_id"],),
        ).fetchone()
        if media:
            context["thumbnail_s3_key"] = media["thumbnail_s3_key"] or None
            context["has_clip"] = bool(media["s3_key"])
    return context


def media_ready(context: dict) -> bool:
    return bool(context.get("thumbnail_s3_key"))


def event_path(context: dict) -> str:
    event_type, event_id, camera_id = context.get("event_type"), context.get("event_id"), context.get("camera_id")
    if event_type == "aac_voice_call" and event_id:
        return f"/aac/voice-call/{event_id}"
    if camera_id and event_id and event_type in MEDIA_EVENT_TYPES:
        try:
            import main
            return main._customer_event_playback_href(camera_id, context.get("timestamp"), event_id, bool(context.get("has_clip")))
        except Exception:
            return f"/events?event_id={event_id}"
    return "/notifications"


def thumbnail_bytes(context: dict) -> bytes | None:
    key = context.get("thumbnail_s3_key")
    if not key:
        return None
    try:
        import main
        return main._card_thumbnail_bytes(key)
    except Exception as error:
        logger.warning("notification_email.thumbnail_unavailable error=%s", type(error).__name__)
        return None


def build_alert_email(context: dict, *, image: bytes | None = None, base_url: str | None = None, tz=None) -> dict:
    """{'subject', 'text', 'html', 'images'} for one alert."""
    base = public_base_url() if base_url is None else base_url
    title = str(context.get("title") or "Camera alert")
    camera = str(context.get("camera_name") or "").strip()
    when = local_time_label(context.get("timestamp") or context.get("created_at"), tz)
    message = str(context.get("message") or "").strip()
    link = base + event_path(context)
    button = "Open the visitor call" if context.get("event_type") == "aac_voice_call" else (
        "View event video" if context.get("event_type") in MEDIA_EVENT_TYPES else "View alerts")
    subject = " · ".join(part for part in (title, camera, when) if part)

    lines = [title]
    if camera:
        lines.append(f"Camera: {camera}")
    if when:
        lines.append(f"Time: {when}")
    if message and message != title:
        lines.append("")
        lines.append(message)
    lines += ["", f"{button}: {link}", "", f"Manage alert emails: {base}/notifications"]
    text = "\n".join(lines)

    esc = html.escape
    details = "".join(
        f'<tr><td style="color:#667085;padding:2px 12px 2px 0">{esc(label)}</td><td style="color:#101828"><strong>{esc(value)}</strong></td></tr>'
        for label, value in (("Camera", camera), ("Time", when)) if value
    )
    image_html = (f'<img src="cid:{THUMBNAIL_CID}" alt="Event snapshot from {esc(camera or "the camera")}" '
                  f'style="display:block;width:100%;max-width:560px;border-radius:8px;margin:12px 0">') if image else ""
    message_html = f'<p style="color:#344054;margin:12px 0">{esc(message)}</p>' if message and message != title else ""
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;padding:16px">'
        f'<h2 style="margin:0 0 8px;color:#101828">{esc(title)}</h2>'
        f'<table style="border-collapse:collapse;font-size:14px">{details}</table>'
        f"{image_html}{message_html}"
        f'<p style="margin:16px 0"><a href="{esc(link, quote=True)}" style="background:#0e7c7b;color:#ffffff;padding:10px 16px;'
        f'border-radius:6px;text-decoration:none;display:inline-block">{esc(button)}</a></p>'
        f'<p style="color:#667085;font-size:12px">AnyAiCam alert. <a href="{esc(base + "/notifications", quote=True)}" style="color:#667085">Manage alert emails</a></p>'
        "</div>"
    )
    images = [(THUMBNAIL_CID, image)] if image else []
    return {"subject": subject, "text": text, "html": html_body, "images": images}
