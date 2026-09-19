"""notification_engine.fanout_appliance_event(): focused tests for the
Smart Motion milestone's one-line addition to SUPPORTED ('smart_motion').
Proves the same event, before this change, would have been silently
dropped (event_type not in SUPPORTED -> 0 notifications created), and
that after the change it produces a clear, camera-identified, in-app
notification by default -- via the *existing* fanout path, with no
new notification system.

Imports notification_engine (which imports partner_db, triggering its
import-time schema init) -- redirects to a throwaway sqlite file via
override_target() before that import, matching
test_appliance_cloud_analytics_events.py's own documented pattern.
"""

import pytest

from database_backend import override_target
from partner_db import initialize_database

with override_target(sqlite_path="/tmp/test_notification_engine_smart_motion_import.db"):
    import notification_engine
    from partner_db import connection


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    # The import-time override above (matching this file's own
    # documented pattern) only ever scoped module import, not actual
    # test execution -- every test function's own connection() call ran
    # against whatever ANYAICAM_PARTNER_DB/the process-default target
    # actually was, a real, persistent file that accumulates rows
    # across every pytest invocation rather than a fresh one per run.
    # Each test below uses a fixed, unique-looking customer_id (e.g.
    # 'cust-default'), so a second run against that same accumulated
    # file eventually collides on a UNIQUE constraint -- not a bug in
    # notification_engine.py itself, purely this test file's own
    # isolation gap. A fresh tmp_path-backed db per test closes it.
    with override_target(sqlite_path=tmp_path / "test_notification_engine_smart_motion.db"):
        initialize_database()
        yield


def _seed_customer_owner(db, *, customer_id="cust-1", camera_id="cam-1"):
    now = "2026-08-23T00:00:00"
    partner_id = f"partner-{customer_id}"
    site_id = f"site-{customer_id}"
    appliance_id = f"appl-{customer_id}"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, "Test Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, f"AIC-{customer_id.upper()}", now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (camera_id, customer_id, site_id, appliance_id, "Camera 1", now))
    db.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"owner-{customer_id}", partner_id, f"owner-{customer_id}@example.test", "Owner", "customer_owner", "x", 1, customer_id, now),
    )


def _appliance(customer_id="cust-1", site_id=None):
    return {"customer_id": customer_id, "site_id": site_id or f"site-{customer_id}"}


def _notification_rows(camera_id="cam-1"):
    with connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM notifications WHERE camera_id=? ORDER BY created_at", (camera_id,)
            ).fetchall()
        ]


def test_smart_motion_is_in_the_supported_set():
    assert "smart_motion" in notification_engine.SUPPORTED


def test_an_unsupported_event_type_creates_zero_notifications():
    with connection() as db:
        _seed_customer_owner(db, customer_id="cust-unsupported", camera_id="cam-unsupported")
    created = notification_engine.fanout_appliance_event(
        _appliance("cust-unsupported"),
        {"id": "evt-1", "camera_id": "cam-unsupported", "event_type": "totally_unknown_type", "timestamp": "2026-08-23T00:00:01"},
    )
    assert created == 0
    assert _notification_rows("cam-unsupported") == []


def test_a_smart_motion_event_creates_a_real_in_app_notification():
    with connection() as db:
        _seed_customer_owner(db, customer_id="cust-sm", camera_id="cam-sm")
    created = notification_engine.fanout_appliance_event(
        _appliance("cust-sm"),
        {"id": "evt-sm-1", "camera_id": "cam-sm", "event_type": "smart_motion", "timestamp": "2026-08-23T00:00:01"},
    )
    assert created == 1
    rows = _notification_rows("cam-sm")
    assert len(rows) == 1
    notification = rows[0]
    # Event type clarity: title is human-readable, not the raw event_type.
    assert notification["title"] == "Smart Motion"
    assert notification["event_type"] == "smart_motion"
    # Camera identification: the specific camera is on the row, not just the customer.
    assert notification["camera_id"] == "cam-sm"
    assert notification["event_id"] == "evt-sm-1"


def test_smart_motion_delivers_in_app_by_default_with_no_preference_row():
    with connection() as db:
        _seed_customer_owner(db, customer_id="cust-default", camera_id="cam-default")
    notification_engine.fanout_appliance_event(
        _appliance("cust-default"),
        {"id": "evt-default-1", "camera_id": "cam-default", "event_type": "smart_motion", "timestamp": "2026-08-23T00:00:01"},
    )
    with connection() as db:
        deliveries = [
            dict(row)
            for row in db.execute(
                "SELECT nd.* FROM notification_deliveries nd JOIN notifications n ON n.id=nd.notification_id WHERE n.camera_id=?",
                ("cam-default",),
            ).fetchall()
        ]
    assert len(deliveries) == 1
    assert deliveries[0]["channel"] == "in_app"
    assert deliveries[0]["status"] in {"sent", "stored", "delivered"}


def test_person_events_are_unaffected_by_the_smart_motion_addition():
    with connection() as db:
        _seed_customer_owner(db, customer_id="cust-person", camera_id="cam-person")
    created = notification_engine.fanout_appliance_event(
        _appliance("cust-person"),
        {"id": "evt-person-1", "camera_id": "cam-person", "event_type": "person", "timestamp": "2026-08-23T00:00:01"},
    )
    assert created == 1
    rows = _notification_rows("cam-person")
    assert rows[0]["title"] == "Person"
    assert rows[0]["event_type"] == "person"


def test_two_real_smart_motion_events_on_the_same_camera_each_get_their_own_notification():
    # fanout_appliance_event() itself does not deduplicate by design --
    # that guarantee lives one layer up, in analytics_sync.py's
    # persisted synced-id cursor (see test_analytics_sync.py on the
    # edge lineage), which ensures this function is only ever called
    # once per real event. This test documents that boundary: two
    # *distinct* real events correctly produce two notifications, not
    # a false "dedup" that would also swallow legitimate repeat motion.
    with connection() as db:
        _seed_customer_owner(db, customer_id="cust-two", camera_id="cam-two")
    notification_engine.fanout_appliance_event(
        _appliance("cust-two"),
        {"id": "evt-two-1", "camera_id": "cam-two", "event_type": "smart_motion", "timestamp": "2026-08-23T00:00:01"},
    )
    notification_engine.fanout_appliance_event(
        _appliance("cust-two"),
        {"id": "evt-two-2", "camera_id": "cam-two", "event_type": "smart_motion", "timestamp": "2026-08-23T00:05:00"},
    )
    rows = _notification_rows("cam-two")
    assert len(rows) == 2
    assert {row["event_id"] for row in rows} == {"evt-two-1", "evt-two-2"}
