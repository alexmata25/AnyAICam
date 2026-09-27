"""Operator allowlist for email alerts (ANYAICAM_EMAIL_ALERT_EVENT_TYPES,
2026-09-27). Staging's test Gmail account was on course for about 268 alert
emails a day (person and Smart Motion). The allowlist narrows EMAIL alerts
to chosen event types without touching the customer's saved preferences,
SMS (which shares the same event-type list), in-app notifications or
transactional mail -- and older failed emails of excluded types are never
retried.
"""
from datetime import datetime, timedelta

import pytest

import notification_engine
import notification_retry_worker as retry_worker
from partner_db import connection
from test_notification_external_delivery import _FakeChannel, _appliance, _deliveries, _isolated_db, _seed, _set_preferences  # noqa: F401
from test_notification_retry_worker import _insert_delivery, _seed_notification

CRITICAL = "aac_voice_call,camera_offline,appliance_offline,storage_problem"


@pytest.fixture()
def channels(monkeypatch):
    fakes = {"in_app": _FakeChannel(status="stored", provider="local"), "email": _FakeChannel(), "sms": _FakeChannel(status="preview")}
    monkeypatch.setattr(notification_engine, "CHANNELS", fakes)
    monkeypatch.setattr(retry_worker, "CHANNELS", fakes)
    return fakes


def _owner_with_email_and_sms(event_types):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="alerts@example.test", email_enabled=True,
                         phone_number="+15555550100", sms_enabled=True, event_types=event_types)


def test_unset_allowlist_changes_nothing(monkeypatch):
    monkeypatch.delenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", raising=False)
    assert notification_engine.email_alert_allowed("person") and notification_engine.email_alert_allowed("aac_voice_call")
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", "  ")
    assert notification_engine.email_alert_allowed("person")


def test_allowlist_parsing(monkeypatch):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", " aac_voice_call , camera_offline,,")
    assert notification_engine.email_alert_allowed("aac_voice_call") and notification_engine.email_alert_allowed("camera_offline")
    assert not notification_engine.email_alert_allowed("person") and not notification_engine.email_alert_allowed("")


def test_noisy_event_gets_in_app_and_sms_but_no_email(monkeypatch, channels):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", CRITICAL)
    _owner_with_email_and_sms(["person", "smart_motion", "aac_voice_call"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "person"})
    assert sorted(d["channel"] for d in _deliveries()) == ["in_app", "sms"]  # SMS unaffected
    assert channels["email"].calls == []


def test_critical_event_still_emails(monkeypatch, channels):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", CRITICAL)
    _owner_with_email_and_sms(["person", "smart_motion", "aac_voice_call"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "aac_voice_call"})
    assert sorted(d["channel"] for d in _deliveries()) == ["email", "in_app", "sms"]
    assert channels["email"].calls[0][1] == "alerts@example.test"


def test_allowlist_never_adds_a_type_the_customer_did_not_choose(monkeypatch, channels):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", CRITICAL)
    _owner_with_email_and_sms(["person"])  # customer did not select camera_offline
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "camera_offline"})
    assert [d["channel"] for d in _deliveries()] == ["in_app"]


def test_saved_preferences_are_not_modified(monkeypatch, channels):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", CRITICAL)
    _owner_with_email_and_sms(["person", "smart_motion"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "person"})
    with connection() as db:
        saved = db.execute("SELECT event_types_json, email_enabled, sms_enabled FROM customer_notification_channels").fetchone()
    assert saved["event_types_json"] == '["person", "smart_motion"]' and saved["email_enabled"] == 1 and saved["sms_enabled"] == 1


def _failed_email_retry(event_type):
    old_enough = (datetime.now() - timedelta(hours=1)).isoformat()
    with connection() as db:
        notification_id = _seed_notification(db)
        db.execute("UPDATE notifications SET event_type=? WHERE id=?", (event_type, notification_id))
        _insert_delivery(db, notification_id=notification_id, channel="email", status="failed", attempt=1, created_at=old_enough)
    return notification_id


def test_failed_emails_of_excluded_types_are_not_retried(monkeypatch, channels):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", CRITICAL)
    _failed_email_retry("person")
    stats = retry_worker.retry_failed_deliveries()
    assert stats["attempted"] == 0 and channels["email"].calls == []


def test_failed_emails_of_allowed_types_are_still_retried(monkeypatch, channels):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", CRITICAL)
    _failed_email_retry("aac_voice_call")
    stats = retry_worker.retry_failed_deliveries()
    assert stats["attempted"] == 1 and len(channels["email"].calls) == 1
    assert set(channels["email"].calls[0][0]) == {"id", "title", "message"}  # same payload shape as before


def test_without_allowlist_retries_are_unchanged(monkeypatch, channels):
    monkeypatch.delenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", raising=False)
    _failed_email_retry("person")
    assert retry_worker.retry_failed_deliveries()["attempted"] == 1
