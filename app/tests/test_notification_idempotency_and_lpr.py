"""One notification per customer member per appliance event, and opt-in
license-plate alerts (2026-10-02).

The same camera event can reach the cloud through the analytics-event route
(analytics_event_available) and the legacy /api/appliance/events forwarding
(analytics_sync._forward_notification), each retried and replayed. Each route
only deduplicated against itself, so an event arriving both ways notified
twice. Both now claim camera_id:local_event_id in notification_event_keys.

A plate read was stored as 'plate' and never notified on the direct route;
it is now notified as 'lpr' -- but only to people who chose License plate
recognition in their notification settings, in-app included.
"""
import json
import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_notification_idempotency_and_lpr.db"):
    import appliance_cloud
    import notification_engine
    from partner_db import connection, initialize_database, password_hash

NOW = "2026-10-02T00:00:00"


def _headers():
    return {"X-Appliance-Id": "appl-1", "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16), "Authorization": "Bearer cred-1"}


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    appliance_cloud.request_limiter.events.clear()
    with override_target(sqlite_path=str(tmp_path / "cloud.db")):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (NOW,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home',?)", (NOW,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (NOW,))
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('c1','appl-1',?,?)", (password_hash("cred-1"), NOW))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('cam-1','cust-1','site-1','appl-1','Driveway',?)", (NOW,))
            for user_id, role, mode in (("owner-1", "customer_owner", "all"), ("member-1", "customer_viewer", "all")):
                db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,camera_access_mode,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (user_id, "p1", f"{user_id}@example.test", user_id, role, "x", 1, "cust-1", "active", mode, NOW))
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as client:
            yield client
    appliance_cloud.request_limiter.events.clear()


def _analytics(client, local_id, event_type="person", detections=None):
    return client.post("/api/appliance/analytics/cam-1/events", headers=_headers(), json={
        "local_event_id": local_id, "event_type": event_type, "confidence": 0.9, "object_count": 1,
        "detections": detections or [], "event_timestamp": "2026-10-02T12:00:00"})


def _legacy(client, local_id, event_type="person"):
    return client.post("/api/appliance/events", headers=_headers(), json={"events": [
        {"id": local_id, "event_type": event_type, "camera_id": "cam-1", "timestamp": "2026-10-02T12:00:00"}]})


def _notifications(user_id=None):
    with connection() as db:
        query = "SELECT user_id,event_type FROM notifications" + (" WHERE user_id=?" if user_id else "")
        return [tuple(r) for r in db.execute(query, (user_id,) if user_id else ())]


def _opt_in(user_id, event_types):
    with connection() as db:
        db.execute("INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,phone_number,sms_enabled,event_types_json,"
                   "camera_scope,quiet_hours_enabled,quiet_start,quiet_end,delivery_mode,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (user_id, "cust-1", "", 0, "", 0, json.dumps(event_types), "all", 0, "22:00", "07:00", "immediate", NOW))


# ------------------------------------------------------------------ idempotency

def test_an_event_arriving_both_ways_with_retries_notifies_each_person_once(cloud):
    assert _analytics(cloud, "evt-1").json()["status"] == "accepted"
    assert _analytics(cloud, "evt-1").json()["status"] == "duplicate"   # analytics retry
    assert _legacy(cloud, "evt-1").json()["notifications_created"] == 0  # legacy forwarding of the same event
    assert _legacy(cloud, "evt-1").json()["duplicates"] == 1             # legacy replay
    assert sorted(_notifications()) == [("member-1", "person"), ("owner-1", "person")]


def test_the_legacy_route_first_then_the_analytics_route_also_notifies_once(cloud):
    assert _legacy(cloud, "evt-2").json()["notifications_created"] == 2
    assert _analytics(cloud, "evt-2").json()["status"] == "accepted"
    assert len(_notifications("owner-1")) == 1 and len(_notifications("member-1")) == 1


def test_different_events_still_notify_separately(cloud):
    _analytics(cloud, "evt-3")
    _legacy(cloud, "evt-4")
    assert len(_notifications("owner-1")) == 2


def test_one_row_per_person_is_enforced_by_the_database(cloud):
    """Per-recipient idempotency (test_notification_partial_failure.py):
    every person's row carries the event identity under UNIQUE(user_id,dedupe_key)."""
    _analytics(cloud, "evt-9")
    with connection() as db:
        keys = sorted(r[0] for r in db.execute("SELECT dedupe_key FROM notifications"))
        assert keys == ["cam-1:evt-9", "cam-1:evt-9"]
        import sqlite3 as _sqlite3
        with pytest.raises(_sqlite3.IntegrityError):
            db.execute("INSERT INTO notifications(id,user_id,customer_id,event_type,severity,title,message,timestamp,created_at,dedupe_key) "
                       "VALUES('dup','owner-1','cust-1','person','info','t','m','t','t','cam-1:evt-9')")
    assert notification_engine.event_dedupe_key(None, "x") is None


# ------------------------------------------------------------------ LPR opt-in

PLATE = [{"plate": "ABC1234", "plate_confidence": 0.93}]


def test_plate_reads_notify_nobody_by_default(cloud):
    assert _analytics(cloud, "plate-1", "plate", PLATE).json()["status"] == "accepted"
    assert _legacy(cloud, "plate-1b", "lpr").json()["notifications_created"] == 0
    assert _notifications() == []


def test_only_people_who_chose_plate_alerts_get_them_once(cloud):
    _opt_in("owner-1", ["lpr", "person"])
    _opt_in("member-1", ["person"])
    assert _analytics(cloud, "plate-2", "plate", PLATE).json()["status"] == "accepted"
    assert _legacy(cloud, "plate-2", "lpr").json()["notifications_created"] == 0  # same read forwarded: deduplicated
    assert _notifications() == [("owner-1", "lpr")]


def test_lpr_is_opt_in_and_a_known_notification_type():
    assert "lpr" in notification_engine.SUPPORTED and "lpr" in notification_engine.OPT_IN_EVENT_TYPES
    assert notification_engine.notification_event_type("plate") == "lpr"
    assert notification_engine.notification_event_type("person") == "person"
    import notification_engine as ne
    assert "lpr" not in ne.DEFAULT_SMS_ALERT_EVENT_TYPES
    import analytics_sync
    assert analytics_sync.LPR_NOTIFY_ENABLED is False  # the appliance's legacy forwarding stays off unless an operator turns it on
