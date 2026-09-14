"""Customer Investigate reliability (2026-09-14 Investigate phase).

Root cause, confirmed with evidence against real staging data before
any change was made: _render_customer_investigate() called the shared
_customer_detection_events(request) with no limit -- an entirely
unbounded query, embedding a customer's *complete* detection_events
history into the rendered page, then kept only the 500 most recent
rows *overall* in Python for client-side filtering. Measured against
a real customer (13,796 total events, 361 of them smart_motion): the
naive "most recent 500 overall" window contained only 16 of those 361
smart_motion events -- 345 (95.6%) were silently unreachable by any
Investigate search or filter, indistinguishable from "no such event
exists," because smart_motion is a small fraction of this customer's
total volume (dominated by ordinary YOLO detections).

_customer_investigate_events() (main.py) is the fix: two independently
SQL-bounded queries -- every smart_motion event up to a very high
ceiling (INVESTIGATE_SMART_MOTION_CEILING) merged with the most recent
ordinary events up to a separate ceiling (INVESTIGATE_OTHER_EVENTS_
CEILING) -- so smart_motion visibility can never again be crowded out
by a customer's own ordinary-event volume, however large. The shared
_customer_detection_events() (used by /api/analytics/events and
/api/analytics/summary, both of which genuinely need the customer's
complete history to compute correct filtered/aggregated results) is
completely untouched -- this is Investigate's own defect, not a
change to the shared analytics-events query other callers correctly
still rely on.

Same fixtures/pattern as test_customer_investigate_and_alerts.py
(which already thoroughly covers Investigate's page-level rendering,
per-camera authorization, and dual-mode dispatch -- this file adds the
event-selection-policy coverage that was missing there).
"""

import sqlite3

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_investigate_reliability.db"


def _seed_tenant(conn, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", cloud_id="AIC-TEST"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, "partner-1", f"Customer {customer_id}", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (appliance_id, customer_id, site_id, cloud_id, "2026-01-01"),
    )


def _seed_camera(conn, camera_id, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_number=1, name="Front Door"):
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, name, "2026-01-01"),
    )


def _seed_event(conn, event_id, camera_id, event_type, timestamp, *, customer_id="cust-1", site_id="site-1", appliance_id="appl-1",
                 local_event_id=None, parent_detection_event_id=None):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at,parent_detection_event_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, site_id, appliance_id, camera_id, local_event_id or f"local-{event_id}",
         event_type, 0.9, 1, None, timestamp, "2026-01-01T00:00:00", parent_detection_event_id),
    )


def _seed_media(conn, media_id, detection_event_id, s3_key, thumbnail_s3_key=None, source_media_id=None):
    conn.execute(
        "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,"
        "started_at,ended_at,duration_seconds,size_bytes,source_media_id,created_at) "
        "SELECT ?,?,customer_id,camera_id,?,?,event_timestamp,event_timestamp,10.0,1000,?,'2026-01-01T00:00:00' "
        "FROM detection_events WHERE id=?",
        (media_id, detection_event_id, s3_key, thumbnail_s3_key, source_media_id, detection_event_id),
    )


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _viewer_identity(customer_id, email):
    return {"role": "customer_viewer", "customer_id": customer_id, "email": email}


class _FakeRequest:
    pass


# --------------------------------------------------------- the confirmed defect: smart_motion visibility under volume pressure


def test_smart_motion_is_never_crowded_out_by_ordinary_event_volume(db_path, monkeypatch):
    """The exact scenario measured against real staging data: many more
    ordinary events than smart_motion events, all newer than the
    smart_motion events. Before this fix, a single most-recent-N-overall
    window would starve smart_motion out almost entirely. After it,
    every smart_motion event is still reachable regardless of how much
    ordinary-event volume exists."""
    monkeypatch.setattr(main, "INVESTIGATE_SMART_MOTION_CEILING", 50)
    monkeypatch.setattr(main, "INVESTIGATE_OTHER_EVENTS_CEILING", 5)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        # 20 smart_motion events, all OLDER than the 30 ordinary events below.
        for i in range(20):
            _seed_event(conn, f"sm-{i:02d}", "cam-1", "smart_motion", f"2026-08-01T00:{i:02d}:00")
        # 30 ordinary (car) events, all newer -- far more than the
        # other-events ceiling of 5, so most of them must be excluded,
        # but none of that exclusion should touch the smart_motion set.
        for i in range(30):
            _seed_event(conn, f"car-{i:02d}", "cam-1", "car", f"2026-08-02T00:{i:02d}:00")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())

        events = main._customer_investigate_events(_FakeRequest())
    smart_motion_ids = {e["id"] for e in events if e["event_type"] == "smart_motion"}
    assert smart_motion_ids == {f"sm-{i:02d}" for i in range(20)}, "every smart_motion event must remain visible regardless of ordinary-event volume"
    other_ids = {e["id"] for e in events if e["event_type"] != "smart_motion"}
    assert len(other_ids) == 5, "ordinary events are still correctly bounded to their own ceiling"


def test_smart_motion_ceiling_is_a_real_sql_limit_not_unbounded(db_path, monkeypatch):
    """Even smart_motion's own generous ceiling is real -- it must not
    silently become an unbounded query again."""
    monkeypatch.setattr(main, "INVESTIGATE_SMART_MOTION_CEILING", 3)
    monkeypatch.setattr(main, "INVESTIGATE_OTHER_EVENTS_CEILING", 500)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        for i in range(10):
            _seed_event(conn, f"sm-{i:02d}", "cam-1", "smart_motion", f"2026-08-01T00:{i:02d}:00")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        events = main._customer_investigate_events(_FakeRequest())
    assert len(events) == 3


# --------------------------------------------------------- deterministic ordering


def test_a_correlated_motion_and_smart_motion_pair_orders_deterministically(db_path, monkeypatch):
    """Motion and its correlated Smart Motion child share the exact
    same event_timestamp in real production data (see appliance_
    cloud.py's own parent-correlation design) -- a timestamp-only sort
    is not guaranteed stable across repeated calls. The id tie-break
    fixes this."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        tie = "2026-08-01T00:00:00"
        _seed_event(conn, "motion-a", "cam-1", "motion", tie)
        _seed_event(conn, "smart-a", "cam-1", "smart_motion", tie, parent_detection_event_id="motion-a")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        first = [e["id"] for e in main._customer_investigate_events(_FakeRequest())]
        second = [e["id"] for e in main._customer_investigate_events(_FakeRequest())]
    assert first == second
    assert set(first) == {"motion-a", "smart-a"}


# --------------------------------------------------------- media association


def test_smart_motion_events_own_shared_media_row_is_what_surfaces_not_the_parents(db_path, monkeypatch):
    """A Smart Motion event's thumbnail/has_event_clip must reflect ITS
    OWN detection_event_media row (the Phase A shared/reference row,
    source_media_id pointing at the parent), never accidentally the
    parent Motion event's row or a different event's media entirely."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        ts = "2026-08-01T00:00:00"
        _seed_event(conn, "motion-a", "cam-1", "motion", ts)
        _seed_event(conn, "smart-a", "cam-1", "smart_motion", ts, parent_detection_event_id="motion-a")
        _seed_media(conn, "media-parent", "motion-a", "recordings/.../motion_a.mp4", "recordings/.../motion_a.jpg")
        _seed_media(conn, "media-child", "smart-a", "recordings/.../motion_a.mp4", "recordings/.../motion_a.jpg", source_media_id="media-parent")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        events = {e["id"]: e for e in main._customer_investigate_events(_FakeRequest())}
    assert events["smart-a"]["has_event_clip"] is True
    assert events["smart-a"]["thumbnail"] == "/api/customer/events/cam-1/smart-a/thumbnail"
    assert events["motion-a"]["has_event_clip"] is True
    assert events["motion-a"]["thumbnail"] == "/api/customer/events/cam-1/motion-a/thumbnail"


def test_an_event_with_no_media_row_yet_has_no_thumbnail_and_no_clip_flag(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_event(conn, "pending-a", "cam-1", "smart_motion", "2026-08-01T00:00:00")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        events = {e["id"]: e for e in main._customer_investigate_events(_FakeRequest())}
    assert events["pending-a"]["has_event_clip"] is False
    assert events["pending-a"]["thumbnail"] is None


# --------------------------------------------------------- empty results / non-customer identity


def test_a_customer_with_zero_events_gets_a_clean_empty_list(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity())
        events = main._customer_investigate_events(_FakeRequest())
    assert events == []


def test_non_customer_identity_returns_none_not_an_empty_list(monkeypatch):
    """Same None-vs-[] contract as _customer_detection_events(): None
    means "not a portal customer identity at all" (caller must fall
    through to the legacy page), not "this customer has no events"."""
    import partner_portal
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    assert main._customer_investigate_events(_FakeRequest()) is None


# --------------------------------------------------------- viewer camera-permission scoping (applies to BOTH bounded queries)


def test_viewer_without_permission_on_a_camera_sees_neither_its_smart_motion_nor_its_ordinary_events(db_path, monkeypatch):
    """The permission join must be applied identically to both halves
    of the merged query -- a viewer who was never granted a camera
    must not see smart_motion events on it either, just because
    smart_motion has its own separate query."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-granted", camera_number=1)
        _seed_camera(conn, "cam-not-granted", camera_number=2)
        _seed_event(conn, "sm-granted", "cam-granted", "smart_motion", "2026-08-01T00:00:00")
        _seed_event(conn, "sm-not-granted", "cam-not-granted", "smart_motion", "2026-08-01T00:00:00")
        _seed_event(conn, "car-granted", "cam-granted", "car", "2026-08-01T00:01:00")
        _seed_event(conn, "car-not-granted", "cam-not-granted", "car", "2026-08-01T00:01:00")
        conn.execute(
            "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
            "VALUES('viewer-1','partner-1','viewer@example.com','Viewer','customer_viewer','x',1,'cust-1','2026-01-01')"
        )
        conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback) VALUES('viewer-1','cam-granted',1)")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        events = {e["id"] for e in main._customer_investigate_events(_FakeRequest())}
    assert events == {"sm-granted", "car-granted"}


def test_viewer_with_no_partner_users_row_gets_a_clean_empty_list_not_an_error(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_event(conn, "sm-1", "cam-1", "smart_motion", "2026-08-01T00:00:00")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "ghost@example.com"))
        events = main._customer_investigate_events(_FakeRequest())
    assert events == []


# --------------------------------------------------------- tenant isolation


def test_never_leaks_another_customers_smart_motion_or_ordinary_events(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1", "site-1", "appl-1", "AIC-ONE")
        _seed_tenant(conn, "cust-2", "site-2", "appl-2", "AIC-TWO")
        _seed_camera(conn, "cam-1", customer_id="cust-1", site_id="site-1", appliance_id="appl-1")
        _seed_camera(conn, "cam-2", customer_id="cust-2", site_id="site-2", appliance_id="appl-2")
        _seed_event(conn, "sm-mine", "cam-1", "smart_motion", "2026-08-01T00:00:00", customer_id="cust-1", site_id="site-1", appliance_id="appl-1")
        _seed_event(conn, "sm-theirs", "cam-2", "smart_motion", "2026-08-01T00:00:00", customer_id="cust-2", site_id="site-2", appliance_id="appl-2")
        _seed_event(conn, "car-mine", "cam-1", "car", "2026-08-01T00:01:00", customer_id="cust-1", site_id="site-1", appliance_id="appl-1")
        _seed_event(conn, "car-theirs", "cam-2", "car", "2026-08-01T00:01:00", customer_id="cust-2", site_id="site-2", appliance_id="appl-2")
        conn.commit()

        import partner_portal
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        events = {e["id"] for e in main._customer_investigate_events(_FakeRequest())}
    assert events == {"sm-mine", "car-mine"}
    assert "sm-theirs" not in events
    assert "car-theirs" not in events


# --------------------------------------------------------- page-level: the fix is actually wired in


def test_render_customer_investigate_calls_the_new_bounded_function_not_the_unbounded_one(monkeypatch):
    """Confirms _render_customer_investigate() was actually switched to
    the fix, not just that the fix function itself works in isolation."""
    called = {"new": False, "old": False}

    def fake_new(request):
        called["new"] = True
        return []

    def fake_old(request, **kwargs):
        called["old"] = True
        return []

    monkeypatch.setattr(main, "_customer_investigate_events", fake_new)
    monkeypatch.setattr(main, "_customer_detection_events", fake_old)
    main._render_customer_investigate([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _FakeRequest())
    assert called["new"] is True
    assert called["old"] is False
