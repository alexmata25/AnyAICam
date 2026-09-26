"""notification_engine.fanout_appliance_event(): the real customer
email/SMS preferences (customer_notification_channels, notification_
preferences.py -- what the Notifications settings page actually
writes) are now actually consulted for external delivery. Before this
fix, fanout_appliance_event() read a different, similarly-named table
(notification_preferences) that has zero writers anywhere in this
codebase -- external delivery was always silently off regardless of
what a customer configured; this file proves the real link now works,
end to end, using fake CHANNELS spies (no real network calls) so every
test is fast and deterministic.

Same import/isolation pattern as this suite's sibling,
test_notification_engine_smart_motion.py.
"""

import pytest

from database_backend import override_target
from partner_db import initialize_database

with override_target(sqlite_path="/tmp/test_notification_external_delivery_import.db"):
    import notification_engine
    from partner_db import connection


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    with override_target(sqlite_path=tmp_path / "test_notification_external_delivery.db"):
        initialize_database()
        yield


class _FakeChannel:
    def __init__(self, status="sent", provider="fake", error=None):
        self.status = status
        self.provider = provider
        self.error = error
        self.calls = []

    def send(self, notification, recipient):
        self.calls.append((notification, recipient))
        return {"channel": "fake", "status": self.status, "provider": self.provider, "error": self.error}


@pytest.fixture()
def fake_channels(monkeypatch):
    channels = {"in_app": _FakeChannel(status="stored", provider="local"), "email": _FakeChannel(), "sms": _FakeChannel()}
    monkeypatch.setattr(notification_engine, "CHANNELS", channels)
    return channels


def _seed(db, *, customer_id="cust-1", camera_id="cam-1", user_id=None):
    now = "2026-09-16T00:00:00"
    partner_id = f"partner-{customer_id}"
    site_id = f"site-{customer_id}"
    appliance_id = f"appl-{customer_id}"
    user_id = user_id or f"owner-{customer_id}"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, "Test Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, f"AIC-{customer_id.upper()}", now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (camera_id, customer_id, site_id, appliance_id, "Camera 1", now))
    db.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, partner_id, f"{user_id}@example.test", "Owner", "customer_owner", "x", 1, customer_id, now),
    )
    return user_id


def _set_preferences(db, *, user_id, customer_id, email_address="", email_enabled=False, phone_number="", sms_enabled=False, event_types=None, camera_scope="all", camera_ids=None, quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00"):
    import json
    db.execute(
        "INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,phone_number,sms_enabled,event_types_json,camera_scope,quiet_hours_enabled,quiet_start,quiet_end,delivery_mode,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, customer_id, email_address, int(email_enabled), phone_number, int(sms_enabled), json.dumps(event_types or []), camera_scope, int(quiet_hours_enabled), quiet_start, quiet_end, "immediate", "2026-09-16T00:00:00"),
    )
    for camera_id in camera_ids or []:
        db.execute("INSERT INTO customer_notification_channel_cameras(user_id,camera_id) VALUES(?,?)", (user_id, camera_id))


def _appliance(customer_id="cust-1", site_id=None):
    return {"customer_id": customer_id, "site_id": site_id or f"site-{customer_id}"}


def _deliveries(notification_id=None, camera_id="cam-1"):
    with connection() as db:
        if notification_id:
            return [dict(r) for r in db.execute("SELECT * FROM notification_deliveries WHERE notification_id=?", (notification_id,)).fetchall()]
        return [dict(r) for r in db.execute(
            "SELECT nd.* FROM notification_deliveries nd JOIN notifications n ON n.id=nd.notification_id WHERE n.camera_id=?", (camera_id,)
        ).fetchall()]


# --------------------------------------------------------------- OFF means no delivery attempt


def test_email_disabled_means_no_email_delivery_row_at_all(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_enabled=False)
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    deliveries = _deliveries()
    assert [d["channel"] for d in deliveries] == ["in_app"]
    assert fake_channels["email"].calls == []


def test_no_preferences_saved_at_all_means_no_external_delivery(fake_channels):
    """A customer who has never opened the Notifications settings page
    gets exactly the pre-existing default: in-app only."""
    with connection() as db:
        _seed(db)
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    deliveries = _deliveries()
    assert [d["channel"] for d in deliveries] == ["in_app"]


# --------------------------------------------------------------- ON reaches the provider path


def test_email_enabled_and_event_type_selected_reaches_the_provider(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"])
    created = notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert created == 1
    assert len(fake_channels["email"].calls) == 1
    notification, recipient = fake_channels["email"].calls[0]
    assert recipient == "owner@example.test"
    deliveries = {d["channel"]: d for d in _deliveries()}
    assert deliveries["email"]["status"] == "sent"
    assert deliveries["email"]["recipient"] == "owner@example.test"
    assert deliveries["email"]["attempt"] == 1


def test_sms_enabled_and_event_type_selected_reaches_the_provider(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", phone_number="+15551234567", sms_enabled=True, event_types=["smart_motion"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert len(fake_channels["sms"].calls) == 1
    assert fake_channels["sms"].calls[0][1] == "+15551234567"


def test_event_type_not_selected_suppresses_email_even_though_enabled(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["lpr"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert fake_channels["email"].calls == []


# --------------------------------------------------------------- camera scope


def test_selected_camera_scope_excludes_an_unselected_camera(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('cam-2','cust-1','site-cust-1','appl-cust-1','Camera 2','2026-09-16')")
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"], camera_scope="selected", camera_ids=["cam-2"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert fake_channels["email"].calls == []


def test_selected_camera_scope_includes_the_selected_camera(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"], camera_scope="selected", camera_ids=["cam-1"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert len(fake_channels["email"].calls) == 1


# --------------------------------------------------------------- quiet hours


def test_quiet_hours_suppresses_email_but_not_in_app(fake_channels, monkeypatch):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"], quiet_hours_enabled=True, quiet_start="00:00", quiet_end="23:59")
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert fake_channels["email"].calls == []
    deliveries = [d["channel"] for d in _deliveries()]
    assert deliveries == ["in_app"]


# --------------------------------------------------------------- duplicate/spam prevention


def test_repeated_events_within_the_cooldown_window_suppress_the_second_email(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion", "id": "evt-1"})
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion", "id": "evt-2"})
    assert len(fake_channels["email"].calls) == 1  # not 2
    # Both events still each got their own in-app notification row -- the
    # existing Smart Alerts list behavior is completely unaffected.
    with connection() as db:
        count = db.execute("SELECT COUNT(*) FROM notifications WHERE camera_id='cam-1'").fetchone()[0]
    assert count == 2


def test_cooldown_does_not_cross_different_event_types(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion", "lpr"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "lpr"})
    assert len(fake_channels["email"].calls) == 2


def test_cooldown_does_not_cross_different_cameras(fake_channels):
    with connection() as db:
        user_id = _seed(db)
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('cam-2','cust-1','site-cust-1','appl-cust-1','Camera 2','2026-09-16')")
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"])
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-2", "event_type": "smart_motion"})
    assert len(fake_channels["email"].calls) == 2


def test_after_the_cooldown_window_elapses_the_next_event_is_delivered(fake_channels, monkeypatch):
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"])
    monkeypatch.setattr(notification_engine, "NOTIFICATION_CHANNEL_COOLDOWN_SECONDS", 0)
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert len(fake_channels["email"].calls) == 2


# --------------------------------------------------------------- tenant isolation


def test_a_second_customers_preferences_never_affect_the_first(fake_channels):
    with connection() as db:
        user_a = _seed(db, customer_id="cust-a", camera_id="cam-a")
        user_b = _seed(db, customer_id="cust-b", camera_id="cam-b")
        _set_preferences(db, user_id=user_a, customer_id="cust-a", email_enabled=False)
        _set_preferences(db, user_id=user_b, customer_id="cust-b", email_address="b@example.test", email_enabled=True, event_types=["smart_motion"])
    notification_engine.fanout_appliance_event(_appliance(customer_id="cust-a"), {"camera_id": "cam-a", "event_type": "smart_motion"})
    assert fake_channels["email"].calls == []
    notification_engine.fanout_appliance_event(_appliance(customer_id="cust-b"), {"camera_id": "cam-b", "event_type": "smart_motion"})
    assert len(fake_channels["email"].calls) == 1
    assert fake_channels["email"].calls[0][1] == "b@example.test"


# --------------------------------------------------------------- failure handling


def test_a_provider_exception_is_recorded_as_an_error_delivery_never_raised(fake_channels):
    fake_channels["email"] = _FakeChannel()
    class _Boom:
        def send(self, notification, recipient):
            raise RuntimeError("SMTP connection refused")
    fake_channels["email"] = _Boom()
    with connection() as db:
        user_id = _seed(db)
        _set_preferences(db, user_id=user_id, customer_id="cust-1", email_address="owner@example.test", email_enabled=True, event_types=["smart_motion"])
    created = notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "smart_motion"})
    assert created == 1  # the notification itself still succeeds
    deliveries = {d["channel"]: d for d in _deliveries()}
    assert deliveries["email"]["status"] == "error"
    assert "SMTP connection refused" in deliveries["email"]["error"]


# --------------------------------------------------------------- ppe support


def test_ppe_is_now_in_the_supported_set():
    assert "ppe" in notification_engine.SUPPORTED


def test_a_real_ppe_event_now_creates_a_notification(fake_channels):
    with connection() as db:
        _seed(db)
    created = notification_engine.fanout_appliance_event(_appliance(), {"camera_id": "cam-1", "event_type": "ppe"})
    assert created == 1
