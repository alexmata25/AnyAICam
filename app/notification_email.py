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
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

logger = logging.getLogger("anyaicam.notification_email")

# Event types whose alert is about something a camera captured, so the
# cloud receives a thumbnail/clip for it shortly after the event.
MEDIA_EVENT_TYPES = frozenset({"person", "vehicle", "smart_motion", "motion", "ppe", "lpr", "facial_recognition", "people_counting"})
MEDIA_WAIT_SECONDS = max(0, int(os.environ.get("ANYAICAM_ALERT_EMAIL_MEDIA_WAIT_SECONDS", "180")))
THUMBNAIL_CID = "event-thumbnail"
DEFAULT_DISPLAY_TIMEZONE = "America/Chicago"  # main.APPLIANCE_TIMEZONE
# The customer's own alert email settings (/notifications is the admin
# page and turns a customer away). /settings/notifications is the page that
# actually holds email on/off, event types, cameras and quiet hours, and it
# shows a sign-in prompt to a signed-out reader (2026-09-29).
MANAGE_ALERTS_PATH = "/settings/notifications"

# Alert categories (2026-09-29): the email's look and subject follow what
# the alert means. An intrusion alarm is urgent and visibly distinct; a
# visitor call asks to be answered now; a system problem needs attention;
# ordinary camera activity is calm and never looks like an emergency.
ALARM_EVENT_TYPES = frozenset({"intrusion_alarm"})
CALL_EVENT_TYPES = frozenset({"aac_voice_call"})
PROBLEM_EVENT_TYPES = frozenset({"camera_offline", "appliance_offline", "recording_stopped", "low_disk", "storage_problem", "high_cpu"})


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
    if event_type == "intrusion_alarm" and camera_id:
        alarm = f"?alarm={quote(str(event_id), safe='')}" if event_id else ""
        return f"/customer/cameras/{quote(str(camera_id), safe='')}/live{alarm}"
    if camera_id and event_id and event_type in MEDIA_EVENT_TYPES:
        try:
            import main
            return main._customer_event_playback_href(camera_id, context.get("timestamp"), event_id, bool(context.get("has_clip")))
        except Exception:
            return f"/events?event_id={event_id}"
    if camera_id:
        return f"/customer/cameras/{quote(str(camera_id), safe='')}/live"
    return "/dashboard"


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


def _short_time(timestamp: str | None, tz=None) -> str:
    """'1:57 PM' -- the subject line's compact time (the body has the date)."""
    if not timestamp:
        return ""
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(tz or _display_timezone())
    return f"{local.strftime('%I').lstrip('0') or '12'}:{local.strftime('%M %p')}"


def _category(event_type: str) -> str:
    if event_type in ALARM_EVENT_TYPES:
        return "alarm"
    if event_type in CALL_EVENT_TYPES:
        return "call"
    if event_type in PROBLEM_EVENT_TYPES:
        return "problem"
    return "activity"


def _headline(context: dict, category: str, camera: str) -> str:
    """The one sentence a customer reads first (email heading and the
    basis of the subject)."""
    event_type = str(context.get("event_type") or "")
    title = str(context.get("title") or "").strip()
    if category == "alarm":
        return "INTRUSION ALARM"
    if category == "call":
        return f"Someone is at {camera}" if camera else "Someone is at your door"
    try:
        from customer_analytics_panel import event_type_label, event_type_message
    except Exception:
        event_type_label = lambda value: title or "Camera alert"  # noqa: E731
        event_type_message = lambda value: title or "Camera alert"  # noqa: E731
    if category == "problem":
        return event_type_label(event_type) if event_type else (title or "Needs attention")
    if event_type == "facial_recognition" and str(context.get("message") or "").strip():
        return str(context.get("message")).strip().rstrip(".")
    if event_type:
        return event_type_message(event_type)
    return title or "Camera alert"


def build_alert_email(context: dict, *, image: bytes | None = None, base_url: str | None = None, tz=None) -> dict:
    """{'subject', 'text', 'html', 'images'} for one alert."""
    base = public_base_url() if base_url is None else base_url
    event_type = str(context.get("event_type") or "")
    category = _category(event_type)
    camera = str(context.get("camera_name") or "").strip()
    stamp = context.get("timestamp") or context.get("created_at")
    when = local_time_label(stamp, tz)
    short = _short_time(stamp, tz)
    message = str(context.get("message") or "").strip()
    headline = _headline(context, category, camera)
    if message.rstrip(".").lower() == headline.lower():
        message = ""  # it only restates the heading
    path = event_path(context)
    link = base + path
    if path.startswith("/aac/voice-call/"):
        button = "Open the visitor call"
    elif category == "alarm":
        button = "Open live camera"
    elif path.startswith(("/playback", "/events")):
        button = "View event video"
    elif path.endswith("/live"):
        button = "View camera"
    else:
        button = "Open AnyAiCam"

    where = f" at {camera}" if camera and camera.lower() not in headline.lower() else ""
    if category == "alarm":
        subject = f"INTRUSION ALARM{where}" + (f" · {short}" if short else "")
    elif category == "call":
        subject = f"{headline} — answer the call"
    elif category == "problem":
        subject = f"{headline}: {camera}" if camera else headline
        subject += f" · {short}" if short else ""
    else:
        subject = f"{headline}{where}" + (f" · {short}" if short else "")

    preheader = {
        "alarm": f"A protected area was entered{where}. Check the live camera now.",
        "call": "Tap to see and talk to your visitor.",
        "problem": "Your AnyAiCam system needs attention.",
    }.get(category, f"{headline}{where}" + (f" on {when}" if when else "") + ".")

    lines = [headline]
    if category == "alarm" and camera:
        lines.append(f"Camera: {camera}")
    elif camera:
        lines.append(f"Camera: {camera}")
    if when:
        lines.append(f"Time: {when}")
    if message and message not in (headline, context.get("title")):
        lines.append("")
        lines.append(message)
    lines += ["", f"{button}: {link}"]
    if category == "alarm":
        # One tap to the phone dialer; AnyAiCam never calls 911 itself.
        lines += ["Emergency? Call 911: tel:911"]
    lines += ["", f"Manage alert emails: {base}{MANAGE_ALERTS_PATH}"]
    text = "\n".join(lines)

    esc = html.escape
    accent = {"alarm": "#b42318", "call": "#0e7c7b", "problem": "#b54708"}.get(category, "#0e7c7b")
    banner = {
        "alarm": ('<div style="background:#b42318;color:#ffffff;font-weight:bold;font-size:18px;letter-spacing:.5px;'
                  'padding:12px 16px;border-radius:8px;margin:0 0 14px">INTRUSION ALARM</div>'),
        "call": ('<div style="background:#e6f4f3;color:#0b5f5e;font-weight:bold;padding:10px 14px;border-radius:8px;'
                 'margin:0 0 14px">Visitor at your door — answer now</div>'),
        "problem": ('<div style="background:#fef0c7;color:#93370d;font-weight:bold;padding:10px 14px;border-radius:8px;'
                    'margin:0 0 14px">Needs your attention</div>'),
    }.get(category, "")
    heading = "" if category == "alarm" and not camera else (
        f'<h2 style="margin:0 0 8px;font-size:20px;color:{accent if category == "alarm" else "#101828"}">'
        f'{esc(camera if category == "alarm" else headline)}</h2>')
    details = "".join(
        f'<tr><td style="color:#667085;padding:2px 12px 2px 0">{esc(label)}</td><td style="color:#101828"><strong>{esc(value)}</strong></td></tr>'
        for label, value in (("Camera", camera), ("Time", when)) if value
    )
    image_html = (f'<img src="cid:{THUMBNAIL_CID}" alt="Event snapshot from {esc(camera or "the camera")}" '
                  f'style="display:block;width:100%;max-width:560px;height:auto;border-radius:8px;margin:12px 0">') if image else ""
    message_html = (f'<p style="color:#344054;margin:12px 0">{esc(message)}</p>'
                    if message and message not in (headline, context.get("title")) else "")
    button_html = (
        f'<a href="{esc(link, quote=True)}" style="background:{accent};color:#ffffff;padding:12px 18px;border-radius:6px;'
        f'text-decoration:none;display:inline-block;font-weight:bold;margin:0 8px 8px 0">{esc(button)}</a>'
        + ('<a href="tel:911" style="background:#ffffff;color:#b42318;border:2px solid #b42318;padding:10px 16px;border-radius:6px;'
           'text-decoration:none;display:inline-block;font-weight:bold;margin:0 8px 8px 0">Call 911</a>' if category == "alarm" else '')
    )
    html_body = (
        '<div style="background:#f2f4f7;padding:16px 8px">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{esc(preheader)}</div>'
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;background:#ffffff;'
        'border-radius:10px;padding:20px 18px">'
        '<div style="font-weight:bold;font-size:15px;color:#0e7c7b;letter-spacing:.3px;margin:0 0 14px">AnyAiCam</div>'
        f"{banner}{heading}"
        f'<table style="border-collapse:collapse;font-size:14px">{details}</table>'
        f"{image_html}{message_html}"
        f'<p style="margin:16px 0 4px">{button_html}</p>'
        '</div>'
        '<p style="font-family:Arial,Helvetica,sans-serif;color:#667085;font-size:12px;text-align:center;max-width:600px;'
        'margin:12px auto 0;line-height:1.5">You are receiving this because alert emails are on for your AnyAiCam account.<br>'
        f'<a href="{esc(base + MANAGE_ALERTS_PATH, quote=True)}" style="color:#667085">Manage alert emails</a></p>'
        "</div>"
    )
    images = [(THUMBNAIL_CID, image)] if image else []
    return {"subject": subject, "text": text, "html": html_body, "images": images}
