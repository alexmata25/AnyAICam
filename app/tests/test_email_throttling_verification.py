"""Email/SMS throttling, verified end to end (2026-09-28).

Goal: fewer repetitive emails, never zero. Controlled clock, real SQLite,
fake channels (no network):
- repeated alerts from one camera + event type are reduced to one per
  cooldown window, and email resumes once the window has passed -- even
  while the activity never stops (the sliding-window bug);
- different cameras and event types never suppress each other;
- Front Door video events and Voice Calls email; email disabled means none;
- the operator email allowlist narrows email only;
- INTRUSION ALARM is critical and immediate through quiet hours, the
  cooldown and the allowlist, but still honours the customer's email
  switch and camera access.
"""
import json
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database

with override_target(sqlite_path="/tmp/test_email_throttling_verification_import.db"):
    import notification_engine
    from partner_db import connection

import notification_email

T0 = datetime(2026, 9, 28, 14, 0, 0)


class _Clock:
    now = T0


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _Clock.now


class _FakeChannel:
    def __init__(self, status="sent"):
        self.status = status
        self.calls = []

    def send(self, notification, recipient):
        self.calls.append((notification, recipient))
        return {"channel": "fake", "status": self.status, "provider": "fake", "error": None}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_engine, "datetime", _FakeDatetime)
    monkeypatch.setattr(notification_engine, "NOTIFICATION_CHANNEL_COOLDOWN_SECONDS", 300)
    monkeypatch.setattr(notification_email, "MEDIA_WAIT_SECONDS", 0)
    monkeypatch.delenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", raising=False)
    _Clock.now = T0
    with override_target(sqlite_path=tmp_path / "throttle.db"):
        initialize_database()
        yield


@pytest.fixture()
def ch(monkeypatch):
    fakes = {"in_app": _FakeChannel("stored"), "email": _FakeChannel(), "sms": _FakeChannel("preview")}
    monkeypatch.setattr(notification_engine, "CHANNELS", fakes)
    return fakes


def _seed(*, email_enabled=True, sms_enabled=False, quiet=False, event_types=("person", "smart_motion", "aac_voice_call", "car", "vehicle"),
          viewer_without_access=False):
    now = "2026-09-28T00:00:00"
    with connection() as db:
        db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (now,))
        db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (now,))
        db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home',?)", (now,))
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
        for cam, name in (("front", "Front Door"), ("drive", "Driveway Right")):
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (cam, "cust-1", "site-1", "appl-1", name, now))
        users = [("owner-1", "customer_owner", "owner@example.test")]
        if viewer_without_access:
            users = [("viewer-1", "customer_viewer", "viewer@example.test")]
        for uid, role, mail in users:
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (uid, "p1", mail, "U", role, "x", 1, "cust-1", now))
            db.execute(
                "INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,phone_number,sms_enabled,event_types_json,camera_scope,quiet_hours_enabled,quiet_start,quiet_end,delivery_mode,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, "cust-1", mail, int(email_enabled), "+15555550100", int(sms_enabled), json.dumps(list(event_types)), "all", int(quiet), "00:00", "23:59", "immediate", now),
            )


def _event(event_type="person", camera="front", minutes=0.0, eid=None):
    _Clock.now = T0 + timedelta(minutes=minutes)
    return notification_engine.fanout_appliance_event(
        {"customer_id": "cust-1", "site_id": "site-1"},
        {"id": eid, "camera_id": camera, "event_type": event_type, "timestamp": _Clock.now.isoformat()},
    )


def test_repeats_are_reduced_and_email_resumes_after_the_window(ch):
    _seed()
    _event(minutes=0)      # emailed
    _event(minutes=2)      # within 5 min of the last email: suppressed
    _event(minutes=4)      # suppressed
    _event(minutes=5.5)    # window since the last EMAIL has passed: emailed
    assert len(ch["email"].calls) == 2
    assert len(ch["in_app"].calls) == 4  # every event still reaches Smart Alerts


def test_continuous_activity_is_bounded_not_permanently_suppressed(ch):
    _seed()
    for minute in range(0, 21, 2):  # an event every 2 minutes for 20 minutes, never quiet
        _event(minutes=minute)
    sent_at = [0, 6, 12, 18]  # one per 5-minute window, measured from each email
    assert len(ch["email"].calls) == len(sent_at)


def test_different_cameras_and_event_types_do_not_suppress_each_other(ch):
    _seed()
    _event("person", "front", 0)
    _event("person", "drive", 1)
    _event("vehicle", "front", 2)  # the edge maps car/truck/... to "vehicle"
    _event("smart_motion", "front", 3)
    assert len(ch["email"].calls) == 4


def test_front_door_video_event_and_voice_call_email(ch):
    _seed()
    _event("person", "front", 0)
    _event("aac_voice_call", "front", 1)
    subjects = [n["title"] for n, _ in ch["email"].calls]
    assert len(subjects) == 2 and all(r == "owner@example.test" for _, r in ch["email"].calls)


def test_email_disabled_means_no_email(ch, monkeypatch):
    monkeypatch.setenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", "person,aac_voice_call")
    _seed(email_enabled=False, sms_enabled=True)
    _event("person", "front", 0)
    _event("aac_voice_call", "front", 10)
    assert ch["email"].calls == [] and len(ch["sms"].calls) == 2


def test_operator_allowlist_narrows_email_only(ch, monkeypatch):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", "aac_voice_call,camera_offline")
    monkeypatch.setenv("ANYAICAM_SMS_ALERT_EVENT_TYPES", "person,aac_voice_call")
    _seed(sms_enabled=True)
    _event("person", "front", 0)
    _event("aac_voice_call", "drive", 1)
    assert [n["title"] for n, _ in ch["email"].calls] == [notification_engine.event_type_label("aac_voice_call")]
    assert len(ch["sms"].calls) == 2 and len(ch["in_app"].calls) == 2


def test_intrusion_alarm_is_immediate_through_quiet_hours_cooldown_and_allowlist(ch, monkeypatch):
    monkeypatch.setenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", "aac_voice_call")
    _seed(quiet=True, event_types=("person",))
    _event("person", "front", 0)
    _event("intrusion_alarm", "front", 0.5)
    _event("intrusion_alarm", "front", 1)
    assert len(ch["email"].calls) == 2  # person blocked by quiet hours + allowlist; both alarms delivered
    with connection() as db:
        assert {r[0] for r in db.execute("SELECT severity FROM notifications WHERE event_type='intrusion_alarm'")} == {"critical"}


def test_intrusion_alarm_still_respects_email_switch_and_camera_access(ch):
    _seed(email_enabled=False)
    _event("intrusion_alarm", "front", 0)
    assert ch["email"].calls == []


def test_intrusion_alarm_never_reaches_a_viewer_without_camera_access(ch):
    _seed(viewer_without_access=True)
    _event("intrusion_alarm", "front", 0)
    assert ch["email"].calls == [] and ch["in_app"].calls == []


def test_quiet_hours_use_the_customers_local_time_not_the_hosts_utc_clock():
    from datetime import timezone
    from zoneinfo import ZoneInfo
    chicago = ZoneInfo("America/Chicago")
    evening_utc = datetime(2026, 9, 28, 22, 12, tzinfo=timezone.utc)  # 5:12 PM CDT
    assert notification_engine._quiet_hours_clock(evening_utc, chicago) == "17:12"
    assert not notification_engine._within_quiet_hours("17:12", "22:00", "07:00")
    late_utc = datetime(2026, 9, 29, 4, 30, tzinfo=timezone.utc)  # 11:30 PM CDT
    assert notification_engine._within_quiet_hours(notification_engine._quiet_hours_clock(late_utc, chicago), "22:00", "07:00")


def test_fifteen_minute_video_cooldown_keeps_voice_calls_on_five_and_alarms_immediate(ch, monkeypatch):
    monkeypatch.setattr(notification_engine, "NOTIFICATION_CHANNEL_COOLDOWN_SECONDS", 900)
    monkeypatch.setattr(notification_engine, "VOICE_CALL_CHANNEL_COOLDOWN_SECONDS", 300)
    _seed()
    _event("person", "front", 0)            # emailed
    _event("person", "front", 10)           # within 15 min: held back
    _event("person", "front", 16)           # 15 min since the last email: emailed
    _event("aac_voice_call", "front", 0.5)  # emailed
    _event("aac_voice_call", "front", 6)    # second visitor after 5 min: emailed
    _event("intrusion_alarm", "front", 1)
    _event("intrusion_alarm", "front", 1.5)
    titles = [n["title"] for n, _ in ch["email"].calls]
    assert titles.count(notification_engine.event_type_label("person")) == 2
    assert titles.count(notification_engine.event_type_label("aac_voice_call")) == 2
    assert titles.count(notification_engine.event_type_label("intrusion_alarm")) == 2
