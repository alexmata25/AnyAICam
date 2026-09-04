"""Smart Alerts fix (2026-09-04): POST /api/appliance/analytics/{camera_id}
/events -- the currently-active analytics-event-sync ingestion path --
now also calls the existing, unmodified notification_engine.
fanout_appliance_event() after a detection event is successfully
stored, exactly like the older POST /api/appliance/events route
already did (that older route stopped being called by any real
appliance in late August, which is why Smart Alerts had gone to zero
real notifications even though detection_events kept filling up
normally the whole time -- see the session's own root-cause report).

No second notification system, no change to detection_events/Events/
Playback/analytics-sync/event-media behavior -- this reuses the exact
same fanout_appliance_event() the old route always called, at the one
new call site.

Same fixture/seeding conventions as test_appliance_cloud_analytics_
events.py (that file's own tests, unmodified, are the proof this fix
doesn't touch detection_events storage/validation/idempotency/
auth/forbidden-fields behavior at all -- this file only adds the
notification-fanout dimension on top).
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_analytics_event_smart_alerts_fanout.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed(db, appliance_id, cloud_id, credential, camera_id, customer_id="cust-1", site_id="site-1"):
    now = "2026-08-21T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, "partner-1", "Test Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (camera_id, customer_id, site_id, appliance_id, "Camera 1", now))


def _seed_customer_owner(db, user_id, email, customer_id="cust-1"):
    """A real, notification-eligible recipient -- fanout_appliance_event()
    only ever fans out to approved/active customer_owner|customer_viewer
    partner_users rows for the event's own customer_id."""
    db.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Owner", "customer_owner", password_hash("x"), 1, customer_id, "active", "2026-08-21T00:00:00"),
    )


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _valid_payload(**overrides):
    payload = {
        "local_event_id": "local-evt-abc123",
        "event_type": "person",
        "confidence": 0.87,
        "object_count": 2,
        "detections": [{"class_name": "person", "confidence": 0.87, "x": 10, "y": 20, "width": 30, "height": 40}],
        "event_timestamp": "2026-08-21T18:00:00",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_analytics_event_smart_alerts_fanout.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _detection_event_row(db_path, camera_id, local_event_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute(
                "SELECT * FROM detection_events WHERE camera_id=? AND local_event_id=?",
                (camera_id, local_event_id),
            ).fetchone()


def _notifications_for(db_path, customer_id):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT * FROM notifications WHERE customer_id=?", (customer_id,)).fetchall()


# --------------------------------------------------------------- happy path: AI/motion events fan out


@pytest.mark.parametrize("event_type", ["person", "vehicle", "motion", "smart_motion", "people_counting"])
def test_supported_detection_event_types_create_a_smart_alert_notification(client, db_path, monkeypatch, event_type):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _seed_customer_owner(db, "owner-1", "owner@example.test")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(local_event_id=f"evt-{event_type}", event_type=event_type),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    # Existing Events/analytics-sync ingestion behavior is completely
    # unaffected -- the detection_events row is still stored exactly as
    # test_appliance_cloud_analytics_events.py's own (unmodified) tests
    # already prove; re-asserted here alongside the new notification to
    # show the two are both true for the same request.
    row = _detection_event_row(db_path, "cam-1", f"evt-{event_type}")
    assert row is not None
    assert row["event_type"] == event_type

    notifications = _notifications_for(db_path, "cust-1")
    assert len(notifications) == 1
    assert notifications[0]["event_type"] == event_type
    assert notifications[0]["camera_id"] == "cam-1"
    assert notifications[0]["event_id"] == response.json()["event_id"]


# --------------------------------------------------------------- unsupported types never fan out


@pytest.mark.parametrize("event_type", ["dog", "cat", "car", "backpack", "suitcase"])
def test_unsupported_event_types_do_not_create_a_notification(client, db_path, monkeypatch, event_type):
    # These are all real event_types this same pipeline stores in
    # detection_events (confirmed live) -- fanout_appliance_event()'s
    # own SUPPORTED set (unmodified, not re-checked at the new call
    # site) is what filters them out, not anything added here.
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _seed_customer_owner(db, "owner-1", "owner@example.test")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(local_event_id=f"evt-{event_type}", event_type=event_type),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    row = _detection_event_row(db_path, "cam-1", f"evt-{event_type}")
    assert row is not None  # still stored -- only the notification is skipped
    assert _notifications_for(db_path, "cust-1") == []


# --------------------------------------------------------------- idempotency: no duplicate notifications


def test_a_replayed_duplicate_event_does_not_create_a_second_notification(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _seed_customer_owner(db, "owner-1", "owner@example.test")

    first = client.post("/api/appliance/analytics/cam-1/events", headers=_auth_headers("appl-1", "test-credential"), json=_valid_payload())
    second = client.post("/api/appliance/analytics/cam-1/events", headers=_auth_headers("appl-1", "test-credential"), json=_valid_payload())

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert second.json()["event_id"] == first.json()["event_id"]
    assert len(_notifications_for(db_path, "cust-1")) == 1


# --------------------------------------------------------------- customer scoping


def test_a_different_customers_user_never_receives_this_customers_notification(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1", customer_id="cust-1", site_id="site-1")
            _seed(db, "appl-2", "AIC-TEST0002", "other-credential", "cam-2", customer_id="cust-2", site_id="site-2")
            _seed_customer_owner(db, "owner-1", "owner1@example.test", customer_id="cust-1")
            _seed_customer_owner(db, "owner-2", "owner2@example.test", customer_id="cust-2")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(),
    )

    assert response.status_code == 200
    cust1_notifications = _notifications_for(db_path, "cust-1")
    cust2_notifications = _notifications_for(db_path, "cust-2")
    assert len(cust1_notifications) == 1
    assert cust1_notifications[0]["user_id"] == "owner-1"
    assert cust2_notifications == []  # cust-2's own user must never see cust-1's event


# --------------------------------------------------------------- fanout failures never break ingestion


def test_a_fanout_exception_still_leaves_the_event_accepted_and_stored(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    monkeypatch.setattr(appliance_cloud, "fanout_appliance_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, "appl-1", "AIC-TEST0001", "test-credential", "cam-1")
            _seed_customer_owner(db, "owner-1", "owner@example.test")

    response = client.post(
        "/api/appliance/analytics/cam-1/events",
        headers=_auth_headers("appl-1", "test-credential"),
        json=_valid_payload(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert _detection_event_row(db_path, "cam-1", "local-evt-abc123") is not None
