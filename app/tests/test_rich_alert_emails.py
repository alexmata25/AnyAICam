"""Rich alert emails (2026-09-27): camera name, local time, inline event
thumbnail and a link to the event video -- and detection alerts held until
their media reaches the cloud (it arrives 60-150 s after the event)."""
import email
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import email_service
import notification_email
import notification_engine
import notification_retry_worker as retry_worker
import notification_service
from partner_db import connection
from test_notification_external_delivery import _FakeChannel, _appliance, _deliveries, _isolated_db, _seed, _set_preferences  # noqa: F401

CHICAGO = ZoneInfo("America/Chicago")
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def _context(**overrides):
    base = {"id": "n1", "customer_id": "cust-1", "camera_id": "cam-1", "event_id": "ev-1", "event_type": "person",
            "title": "Person detected", "message": "Front Door detected a person", "timestamp": "2026-09-27T18:57:46",
            "created_at": "2026-09-27T18:57:46", "camera_name": "Front Door", "thumbnail_s3_key": "k/thumb.jpg", "has_clip": True}
    base.update(overrides)
    return base


# ---------------------------------------------------------------- content

def test_local_time_is_converted_from_utc():
    assert notification_email.local_time_label("2026-09-27T18:57:46", CHICAGO) == "Sun, Sep 27 at 1:57 PM CDT"
    assert notification_email.local_time_label("2026-12-01T06:05:00+00:00", CHICAGO) == "Tue, Dec 1 at 12:05 AM CST"
    assert notification_email.local_time_label(None) == ""


def test_detection_email_has_camera_time_thumbnail_and_video_link(monkeypatch):
    import main
    monkeypatch.setattr(main, "_customer_event_playback_href", lambda cam, ts, ev, clip: f"/playback?camera={cam}&event={ev}&clip={int(clip)}")
    content = notification_email.build_alert_email(_context(), image=JPEG, base_url="https://portal.example", tz=CHICAGO)
    assert content["subject"] == "Person detected · Front Door · Sun, Sep 27 at 1:57 PM CDT"
    for part in ("Camera: Front Door", "Time: Sun, Sep 27 at 1:57 PM CDT", "View event video: https://portal.example/playback?camera=cam-1&event=ev-1&clip=1"):
        assert part in content["text"]
    assert 'src="cid:event-thumbnail"' in content["html"] and "https://portal.example/playback?camera=cam-1&amp;event=ev-1&amp;clip=1" in content["html"]
    assert content["images"] == [("event-thumbnail", JPEG)]


def test_without_a_thumbnail_the_email_still_has_everything_else(monkeypatch):
    content = notification_email.build_alert_email(_context(thumbnail_s3_key=None), image=None, base_url="https://p", tz=CHICAGO)
    assert "cid:" not in content["html"] and content["images"] == [] and "Camera: Front Door" in content["text"]


def test_visitor_call_links_to_the_call_screen():
    content = notification_email.build_alert_email(_context(event_type="aac_voice_call", event_id="call-9", title="AAC Voice Call"), base_url="https://p", tz=CHICAGO)
    assert "Open the visitor call: https://p/aac/voice-call/call-9" in content["text"]


def test_problem_alerts_link_to_the_alerts_list():
    content = notification_email.build_alert_email(_context(event_type="camera_offline", event_id=None, title="Camera offline"), base_url="https://p", tz=CHICAGO)
    assert "View alerts: https://p/notifications" in content["text"]


def test_camera_names_are_escaped_in_html():
    content = notification_email.build_alert_email(_context(camera_name='<script>x</script> & "Door"'), base_url="https://p", tz=CHICAGO)
    assert "<script>" not in content["html"] and "&lt;script&gt;" in content["html"]


def test_base_url_falls_back_to_the_reset_url_origin(monkeypatch):
    monkeypatch.delenv("ANYAICAM_PUBLIC_URL", raising=False)
    monkeypatch.setenv("ANYAICAM_PASSWORD_RESET_URL", "https://portal.example/reset-password")
    assert notification_email.public_base_url() == "https://portal.example"
    monkeypatch.setenv("ANYAICAM_PUBLIC_URL", "https://staging.example/")
    assert notification_email.public_base_url() == "https://staging.example"


# ---------------------------------------------------------------- SMTP message structure

def test_smtp_message_embeds_the_thumbnail_inline(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, message): captured["raw"] = message.as_bytes()

    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_service, "settings", type("S", (), {"email_backend": "smtp", "smtp_host": "smtp.example", "smtp_port": 587,
                                                                  "smtp_username": "", "smtp_password": "", "email_from": "alerts@example.test"})())
    result = email_service.SMTPEmail().send("appliance_alert", "to@example.test", "Subj", "plain", html='<img src="cid:event-thumbnail">', images=[("event-thumbnail", JPEG)])
    assert result["status"] == "sent"
    parsed = email.message_from_bytes(captured["raw"])
    parts = {part.get_content_type(): part for part in parsed.walk()}
    assert "text/plain" in parts and "text/html" in parts and "multipart/related" in parts
    assert parts["image/jpeg"]["Content-ID"] == "<event-thumbnail>" and parts["image/jpeg"].get_payload(decode=True) == JPEG


def test_existing_callers_without_images_are_unchanged(monkeypatch, tmp_path):
    record = email_service.PreviewEmail(root=tmp_path).send("password_reset", "a@example.test", "Reset", "link")
    assert record["status"] == "preview" and record["images"] == []


# ---------------------------------------------------------------- delivery: held for media, then sent

@pytest.fixture()
def channels(monkeypatch):
    fakes = {"in_app": _FakeChannel(status="stored", provider="local"), "email": _FakeChannel(), "sms": _FakeChannel(status="preview")}
    monkeypatch.setattr(notification_engine, "CHANNELS", fakes)
    monkeypatch.setattr(retry_worker, "CHANNELS", fakes)
    monkeypatch.delenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", raising=False)
    return fakes


def _owner(event_types):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="alerts@example.test", email_enabled=True, event_types=event_types)


def _add_media(event_id, *, thumbnail=True):
    with connection() as db:
        db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (event_id, "cust-1", "site-cust-1", "appl-cust-1", "cam-1", event_id, "person", "2026-09-27T18:00:00", "2026-09-27T18:00:00"))
        db.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,started_at,ended_at,duration_seconds,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("m-" + event_id, event_id, "cust-1", "cam-1", "clips/x.mp4", "thumbs/x.jpg" if thumbnail else "", "2026-09-27T18:00:00", "2026-09-27T18:00:10", 10, 1024, "2026-09-27T18:00:00"))


def test_detection_alert_email_is_held_not_sent_immediately(channels):
    _owner(["person"])
    notification_engine.fanout_appliance_event(_appliance(), {"id": "ev-1", "camera_id": "cam-1", "event_type": "person"})
    email_rows = [d for d in _deliveries() if d["channel"] == "email"]
    assert [(d["status"], d["attempt"]) for d in email_rows] == [("pending_media", 0)]
    assert channels["email"].calls == []


def test_held_email_is_sent_once_the_thumbnail_arrives(channels):
    _owner(["person"])
    notification_engine.fanout_appliance_event(_appliance(), {"id": "ev-1", "camera_id": "cam-1", "event_type": "person"})
    assert retry_worker.send_pending_media_emails()["held"] == 1 and channels["email"].calls == []
    _add_media("ev-1")
    stats = retry_worker.send_pending_media_emails()
    assert stats["sent"] == 1 and len(channels["email"].calls) == 1
    assert retry_worker.send_pending_media_emails()["pending"] == 0  # never sent twice
    statuses = [(d["status"], d["attempt"]) for d in _deliveries() if d["channel"] == "email"]
    assert statuses == [("pending_media", 0), ("sent", 1)]


def test_held_email_goes_out_after_the_deadline_without_media(channels):
    _owner(["person"])
    notification_engine.fanout_appliance_event(_appliance(), {"id": "ev-2", "camera_id": "cam-1", "event_type": "person"})
    later = datetime.now() + timedelta(seconds=notification_email.MEDIA_WAIT_SECONDS + 1)
    assert retry_worker.send_pending_media_emails(now=later)["sent"] == 1


def test_visitor_calls_and_problems_are_emailed_immediately(channels):
    _owner(["aac_voice_call", "camera_offline"])
    notification_engine.fanout_appliance_event(_appliance(), {"id": "call-1", "camera_id": "cam-1", "event_type": "aac_voice_call"})
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "camera_offline"})
    assert len(channels["email"].calls) == 2
    assert not [d for d in _deliveries() if d["status"] == "pending_media"]


def test_held_emails_respect_a_narrowed_allowlist(channels, monkeypatch):
    _owner(["person"])
    notification_engine.fanout_appliance_event(_appliance(), {"id": "ev-3", "camera_id": "cam-1", "event_type": "person"})
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", "aac_voice_call")
    _add_media("ev-3")
    stats = retry_worker.send_pending_media_emails()
    assert stats["skipped"] == 1 and channels["email"].calls == []


def test_pending_rows_are_not_treated_as_failures_by_the_retry_scan(channels):
    _owner(["person"])
    notification_engine.fanout_appliance_event(_appliance(), {"id": "ev-4", "camera_id": "cam-1", "event_type": "person"})
    assert retry_worker.retry_failed_deliveries()["candidates"] == 0


# ---------------------------------------------------------------- channel falls back safely

def test_email_channel_sends_rich_content_and_falls_back_to_plain(monkeypatch):
    sent = []

    class Capture:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            sent.append((subject, bool(html), images))
            return {"status": "sent"}

    monkeypatch.setattr(notification_service, "get_email_service", lambda: Capture())
    monkeypatch.setattr(notification_service, "_rich_alert_content", lambda n: {"subject": "Rich", "text": "t", "html": "<p>h</p>", "images": [("event-thumbnail", JPEG)]})
    notification_service.EmailChannel().send({"id": "n1", "title": "Person detected"}, "to@example.test")
    monkeypatch.setattr(notification_service, "_rich_alert_content", lambda n: None)
    notification_service.EmailChannel().send({"id": "n1", "title": "Person detected", "message": "m"}, "to@example.test")
    assert sent == [("Rich", True, [("event-thumbnail", JPEG)]), ("Person detected", False, None)]
