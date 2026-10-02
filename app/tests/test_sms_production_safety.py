"""SMS production safety (2026-10-01), before a real provider is enabled.

On staging, SMS shared the email event-type list and would have sent up to
345 texts a day to one customer, almost all person/motion/PPE/Smart Motion.
- ANYAICAM_SMS_ALERT_EVENT_TYPES: SMS carries intrusion_alarm, aac_voice_call,
  camera_offline, appliance_offline and storage_problem by default; ordinary
  detections stay on email/push/in-app.
- ANYAICAM_SMS_DAILY_CAP_PER_USER (default 20): non-emergency SMS per user
  per rolling 24 hours.
- INTRUSION ALARM always reaches SMS: never filtered by the allowlist, quiet
  hours, the ordinary cooldown or the daily cap, and never counted toward it.
- Twilio: message SID and error code are stored; permanent rejections (bad
  number, not mobile, STOP) are final and never retried.
- Test SMS: at most 3 per user per hour; consent/STOP wording on the page.
No real network: Twilio's endpoint is a fake urlopen, channels are spies.
"""
import io
import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database

with override_target(sqlite_path="/tmp/test_sms_production_safety_import.db"):
    import notification_engine
    import notification_retry_worker as retry_worker
    import notification_service
    import sms_service
    from partner_db import connection

from test_notification_external_delivery import _FakeChannel, _appliance, _deliveries, _seed, _set_preferences

ORDINARY = ["person", "motion", "smart_motion", "vehicle", "ppe", "lpr", "line_crossing"]
URGENT = ["aac_voice_call", "camera_offline", "appliance_offline", "storage_problem"]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.delenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", raising=False)
    monkeypatch.delenv("ANYAICAM_SMS_DAILY_CAP_PER_USER", raising=False)
    monkeypatch.delenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", raising=False)
    with override_target(sqlite_path=tmp_path / "test_sms_production_safety.db"):
        initialize_database()
        yield


@pytest.fixture()
def channels(monkeypatch):
    fakes = {"in_app": _FakeChannel(status="stored", provider="local"), "email": _FakeChannel(), "sms": _FakeChannel()}
    monkeypatch.setattr(notification_engine, "CHANNELS", fakes)
    monkeypatch.setattr(retry_worker, "CHANNELS", fakes)
    return fakes


def _owner(event_types, *, quiet_hours=False, email=True):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=email,
                         phone_number="+15555550100", sms_enabled=True, event_types=event_types,
                         quiet_hours_enabled=quiet_hours, quiet_start="00:00", quiet_end="23:59")
    return user_id


def _fan(event_type, camera_id="cam-1"):
    return notification_engine.fanout_appliance_event(_appliance(), {"camera_id": camera_id, "event_type": event_type})


def _sms_rows():
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT d.*, n.event_type FROM notification_deliveries d JOIN notifications n ON n.id=d.notification_id "
            "WHERE d.channel='sms' ORDER BY d.created_at").fetchall()]


# ------------------------------------------------------------------ allowlist

@pytest.mark.parametrize("event_type", ORDINARY)
def test_ordinary_detections_do_not_text_by_default_but_still_email(channels, event_type):
    _owner(ORDINARY)
    _fan(event_type)
    assert channels["sms"].calls == []
    assert len(channels["email"].calls) == 1 and len(channels["in_app"].calls) == 1  # email/in-app unchanged
    assert _sms_rows() == []


@pytest.mark.parametrize("event_type", URGENT)
def test_urgent_types_text_by_default(channels, event_type):
    _owner(URGENT)
    _fan(event_type)
    assert [call[1] for call in channels["sms"].calls] == ["+15555550100"]


def test_the_default_list_is_exactly_the_approved_one(monkeypatch):
    assert notification_engine.DEFAULT_SMS_ALERT_EVENT_TYPES == {"intrusion_alarm", *URGENT}
    monkeypatch.setenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", " , ")
    assert notification_engine._sms_alert_event_types() == notification_engine.DEFAULT_SMS_ALERT_EVENT_TYPES


def test_operator_list_replaces_the_default_but_never_removes_intrusion_alarm(monkeypatch, channels):
    monkeypatch.setenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", "person")
    assert notification_engine.sms_alert_allowed("person")
    assert not notification_engine.sms_alert_allowed("camera_offline")
    assert notification_engine.sms_alert_allowed("intrusion_alarm")
    _owner(["person", "camera_offline"])
    _fan("intrusion_alarm")
    assert len(channels["sms"].calls) == 1


def test_sms_allowlist_never_adds_a_type_the_customer_did_not_choose(channels):
    _owner(["person"])  # camera_offline not selected
    _fan("camera_offline")
    assert channels["sms"].calls == []


# ------------------------------------------------------------------ INTRUSION ALARM stays urgent

def test_intrusion_sms_goes_out_during_quiet_hours(channels):
    _owner([], quiet_hours=True)
    _fan("intrusion_alarm")
    assert len(channels["sms"].calls) == 1
    assert channels["sms"].calls[0][0]["title"] == notification_engine.event_type_label("intrusion_alarm")


def test_quiet_hours_still_hold_back_ordinary_urgent_types(channels):
    _owner(URGENT, quiet_hours=True)
    _fan("camera_offline")
    assert channels["sms"].calls == []


def test_intrusion_sms_is_not_held_by_the_ordinary_cooldown(channels):
    _owner(URGENT)
    _fan("camera_offline")
    _fan("camera_offline")  # same camera and type within 5 minutes: cooldown
    assert len(channels["sms"].calls) == 1
    _fan("intrusion_alarm")
    _fan("intrusion_alarm")
    _fan("intrusion_alarm")
    assert len(channels["sms"].calls) == 4  # every alarm is its own urgent SMS


def test_intrusion_sms_goes_out_after_the_daily_cap_is_reached(channels, monkeypatch):
    monkeypatch.setenv("ANYAICAM_SMS_DAILY_CAP_PER_USER", "2")
    monkeypatch.setattr(notification_engine, "NOTIFICATION_CHANNEL_COOLDOWN_SECONDS", 0)
    _owner(URGENT)
    for _ in range(3):
        _fan("camera_offline")
    assert len(channels["sms"].calls) == 2
    assert [row["status"] for row in _sms_rows()] == ["sent", "sent", "skipped_daily_cap"]
    _fan("intrusion_alarm")
    _fan("intrusion_alarm")
    assert len(channels["sms"].calls) == 4
    assert [row["status"] for row in _sms_rows() if row["event_type"] == "intrusion_alarm"] == ["sent", "sent"]


def test_intrusion_sms_never_counts_toward_the_daily_cap(channels, monkeypatch):
    monkeypatch.setenv("ANYAICAM_SMS_DAILY_CAP_PER_USER", "1")
    _owner(URGENT)
    for _ in range(5):
        _fan("intrusion_alarm")
    _fan("camera_offline")
    assert len(channels["sms"].calls) == 6


def test_customer_can_still_turn_off_alarm_sms_in_security_settings(channels, monkeypatch):
    monkeypatch.setattr(notification_engine, "_security_sms_wanted", lambda customer_id, site_id: False)
    _owner([])
    _fan("intrusion_alarm")
    assert channels["sms"].calls == [] and len(channels["email"].calls) == 1


# ------------------------------------------------------------------ daily cap

def test_daily_cap_defaults_to_20_and_is_configurable(monkeypatch):
    assert notification_engine._sms_daily_cap() == 20
    monkeypatch.setenv("ANYAICAM_SMS_DAILY_CAP_PER_USER", "50")
    assert notification_engine._sms_daily_cap() == 50
    monkeypatch.setenv("ANYAICAM_SMS_DAILY_CAP_PER_USER", "0")  # 0 = no cap
    with connection() as db:
        assert not notification_engine.sms_daily_cap_reached(db, user_id="anyone", now=datetime.now())


def test_daily_cap_is_per_user_and_rolls_over_after_24_hours(channels, monkeypatch):
    monkeypatch.setenv("ANYAICAM_SMS_DAILY_CAP_PER_USER", "1")
    user_id = _owner(URGENT)
    _fan("camera_offline")
    with connection() as db:
        assert notification_engine.sms_daily_cap_reached(db, user_id=user_id, now=datetime.now())
        assert not notification_engine.sms_daily_cap_reached(db, user_id="someone-else", now=datetime.now())
        assert not notification_engine.sms_daily_cap_reached(db, user_id=user_id, now=datetime.now() + timedelta(hours=25))


def test_capped_sms_never_reaches_the_provider_or_the_retry_worker(channels, monkeypatch):
    monkeypatch.setenv("ANYAICAM_SMS_DAILY_CAP_PER_USER", "1")
    monkeypatch.setattr(notification_engine, "NOTIFICATION_CHANNEL_COOLDOWN_SECONDS", 0)
    _owner(URGENT)
    _fan("camera_offline")
    _fan("storage_problem")
    assert len(channels["sms"].calls) == 1
    retry_worker.retry_failed_deliveries()
    assert len(channels["sms"].calls) == 1


# ------------------------------------------------------------------ Twilio hardening

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _twilio(monkeypatch, *, body=None, http_status=None):
    def fake_urlopen(request, timeout=None, context=None):
        payload = json.dumps(body or {}).encode()
        if http_status:
            raise urllib.error.HTTPError(request.full_url, http_status, "error", {}, io.BytesIO(payload))
        return _Response(payload)
    monkeypatch.setattr(sms_service.urllib.request, "urlopen", fake_urlopen)
    return sms_service.TwilioSms("ACtest", "token-never-logged", "+15550001111")


def test_twilio_success_keeps_the_message_sid(monkeypatch):
    result = _twilio(monkeypatch, body={"sid": "SM0123456789abcdef", "status": "queued"}).send("appliance_alert", "+15555550100", "hi")
    assert result["status"] == "sent" and result["provider"] == "twilio"
    assert result["provider_message_id"] == "SM0123456789abcdef"


@pytest.mark.parametrize("code", ["21211", "21610", "21614"])
def test_twilio_permanent_errors_are_rejected_not_failed(monkeypatch, code):
    result = _twilio(monkeypatch, body={"code": int(code), "message": "x"}, http_status=400).send("appliance_alert", "+15555550100", "hi")
    assert result["status"] == "rejected" and result["provider_error_code"] == code
    assert "token-never-logged" not in json.dumps(result)


def test_twilio_temporary_errors_stay_retryable(monkeypatch):
    result = _twilio(monkeypatch, body={"code": 20429, "message": "Too many requests"}, http_status=429).send("appliance_alert", "+15555550100", "hi")
    assert result["status"] == "failed" and result["provider_error_code"] == "20429"


def test_sid_and_error_code_are_stored_on_the_delivery_row(monkeypatch):
    monkeypatch.setattr(sms_service, "get_sms_service", lambda: _twilio(monkeypatch, body={"sid": "SMabc"}))
    monkeypatch.setattr(notification_service, "get_sms_service", sms_service.get_sms_service)
    monkeypatch.setattr(notification_engine, "CHANNELS", {"in_app": _FakeChannel(status="stored"), "email": _FakeChannel(), "sms": notification_service.SmsChannel()})
    _owner(URGENT, email=False)
    _fan("camera_offline")
    (row,) = _sms_rows()
    assert (row["status"], row["provider"], row["provider_message_id"]) == ("sent", "twilio", "SMabc")


def test_a_rejected_sms_is_never_retried(channels):
    _owner(URGENT)
    channels["sms"].status = "rejected"
    _fan("camera_offline")
    channels["sms"].status = "sent"
    stats = retry_worker.retry_failed_deliveries()
    assert stats["candidates"] == 0 and len(channels["sms"].calls) == 1


def test_older_failed_ordinary_sms_is_not_retried_once_sms_is_narrowed(channels, monkeypatch):
    monkeypatch.setenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", "person")
    _owner(["person"])
    channels["sms"].status = "failed"
    _fan("person")
    monkeypatch.delenv("ANYAICAM_SMS_ALERT_EVENT_TYPES")
    with connection() as db:
        db.execute("UPDATE notification_deliveries SET created_at=? WHERE channel='sms'", ((datetime.now() - timedelta(hours=1)).isoformat(),))
    stats = retry_worker.retry_failed_deliveries()
    assert stats["attempted"] == 0 and len(channels["sms"].calls) == 1


# ------------------------------------------------------------------ Test SMS and settings page

@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    db_path = tmp_path / "test_sms_settings.db"
    sent = []

    class _Preview:
        def send(self, message_type, to, body):
            sent.append(to)
            return {"status": "preview", "to": to}

    monkeypatch.setattr(sms_service, "get_sms_service", lambda: _Preview())
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active','2026-01-01')")
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                     "VALUES('user-1','partner-1','owner@example.test','Owner','customer_owner','x',1,'cust-1','2026-01-01')")
        conn.commit()
        conn.close()
        with TestClient(main.app, follow_redirects=False) as client:
            client.sent = sent
            yield client


def _cookie():
    import partner_portal
    return {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)}


def test_test_sms_is_limited_to_three_per_hour(http_client):
    http_client.put("/api/customer/notifications/preferences", cookies=_cookie(),
                    json={"email_enabled": False, "email_address": "", "sms_enabled": True, "phone_number": "+15551234567",
                          "event_types": [], "camera_scope": "all", "camera_ids": []})
    answers = [http_client.post("/api/customer/notifications/test-sms", cookies=_cookie()) for _ in range(4)]
    assert [a.status_code for a in answers] == [200, 200, 200, 429]
    assert "try again in an hour" in answers[3].json()["detail"]
    assert len(http_client.sent) == 3  # the fourth never reached the provider


def test_settings_page_shows_sms_consent_and_stop_wording(http_client):
    page = http_client.get("/settings/notifications", cookies=_cookie()).text
    assert 'id="notif-sms-consent"' in page
    assert "Reply STOP to opt out" in page and "Message and data rates may apply" in page
