"""event_media_policy.daily_seconds_used(): a shared (Smart Motion)
detection_event_media row must never be double-counted against the
daily physical-footage allowance -- it references bytes a root row
already accounted for once, not new recorded footage (2026-09-14 Phase
A, Codex security review finding #5).
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database

import event_media_policy as policy


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_event_media_policy.db"


def _seed_camera(conn, camera_id="cam-1", customer_id="cust-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", (customer_id, "partner-1", "Co", f"{customer_id}@example.com", "active", "2026-01-01"))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Site", "2026-01-01"))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1',?,?,?,?)", (customer_id, "site-1", "AIC-1", "2026-01-01"))
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (camera_id, customer_id, "site-1", "appl-1", "Camera", "2026-01-01"))


def _seed_detection_event(conn, event_id, camera_id, customer_id, event_type, local_event_id, timestamp):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, "site-1", "appl-1", camera_id, local_event_id, event_type, timestamp, timestamp),
    )


def _seed_media(conn, media_id, detection_event_id, camera_id, customer_id, duration_seconds, source_media_id=None):
    conn.execute(
        "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,started_at,ended_at,duration_seconds,source_media_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (media_id, detection_event_id, customer_id, camera_id, f"recordings/x/{media_id}.mp4", "2026-08-21T10:00:00", "2026-08-21T10:00:10", duration_seconds, source_media_id, "2026-08-21T10:00:10"),
    )


def test_shared_row_is_excluded_from_daily_usage(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_camera(conn)
        _seed_detection_event(conn, "evt-root", "cam-1", "cust-1", "motion", "local-root", "2026-08-21T10:00:00")
        _seed_detection_event(conn, "evt-child", "cam-1", "cust-1", "smart_motion", "local-child", "2026-08-21T10:00:00")
        _seed_media(conn, "media-root", "evt-root", "cam-1", "cust-1", 15.6)
        _seed_media(conn, "media-child", "evt-child", "cam-1", "cust-1", 15.6, source_media_id="media-root")
        conn.commit()

        used = policy.daily_seconds_used(conn, "cam-1", datetime(2026, 8, 21, 12, 0, 0))

    # Only the root's own 15.6s counted -- the shared row's identical
    # duration for the SAME physical clip is never added a second time.
    assert used == 15.6


def test_two_independent_root_events_both_count_normally(db_path):
    """Confirms the fix doesn't over-correct -- two genuinely separate
    physical clips (two distinct root rows) still both count."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_camera(conn)
        _seed_detection_event(conn, "evt-a", "cam-1", "cust-1", "motion", "local-a", "2026-08-21T09:00:00")
        _seed_detection_event(conn, "evt-b", "cam-1", "cust-1", "motion", "local-b", "2026-08-21T11:00:00")
        _seed_media(conn, "media-a", "evt-a", "cam-1", "cust-1", 10.0)
        _seed_media(conn, "media-b", "evt-b", "cam-1", "cust-1", 12.0)
        conn.commit()

        used = policy.daily_seconds_used(conn, "cam-1", datetime(2026, 8, 21, 12, 0, 0))

    assert used == 22.0


def test_shared_registration_never_calls_allows_event_media(monkeypatch):
    """The new shared route (appliance_cloud.py's
    analytics_event_media_shared()) must never call allows_event_media()
    at all -- referencing already-accounted footage is not new physical
    usage, so there's nothing to gate a second time. Enforced here by
    asserting the real cloud module never imports/calls it from that
    function's own source text."""
    import inspect
    import appliance_cloud

    source = inspect.getsource(appliance_cloud)
    shared_start = source.index("def analytics_event_media_shared")
    shared_body = source[shared_start:source.index("\n    @app.post", shared_start + 1)]
    assert "allows_event_media" not in shared_body
