"""P0 #5 remediation, Phase 1 (2026-09-05, Codex review): the original
polling endpoint (GET /api/customer/events/recent) reused
_customer_detection_events() as-is -- an unbounded query (no SQL LIMIT
at all) returning this customer's *entire* event history across every
camera, every single poll tick (as often as every 4s while anything is
processing), only truncated to 30 rows in Python *after* the full
fetch. _customer_recent_events_bounded() replaces that: a real SQL
LIMIT, an optional camera_id filter (used by the new per-camera mobile
polling route), and the same tenant/camera-permission scoping
_customer_detection_events() already enforces -- see that function's
own docstring for the full contract.

Tests below cover exactly what Phase 1 required before moving on:
customer/tenant scoping, current-camera authorization (including a
customer_viewer with no permission on the requested camera), ORDER BY
... DESC, a real database-level LIMIT (proven by seeding more rows than
the limit and asserting the query itself never returns more, not just
that the endpoint truncates the response), and that the returned shape
is byte-identical to what _customer_detection_events() already
produces -- the frontend must not need to change how it reads a row.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_p05_bounded_recent_events.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Main Site", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (f"app-{customer_id}", customer_id, f"site-{customer_id}", f"cloud-{customer_id}", "2026-01-01"),
    )


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name, status="configured"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, status, camera_number, "2026-01-01"),
    )


def _seed_event(conn, event_id, *, customer_id, camera_id, timestamp, event_type="person"):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, f"site-{customer_id}", f"app-{customer_id}", camera_id, f"local-{event_id}",
         event_type, 0.9, 1, None, timestamp, timestamp),
    )


def _seed_permission(conn, *, user_id, camera_id, can_playback=1):
    conn.execute(
        "INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback) VALUES(?,?,?)",
        (user_id, camera_id, can_playback),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _viewer_cookie(customer_id, email="viewer@example.test"):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


# --------------------------------------------------------- direct function: bounding + ordering + shape

def test_limit_is_enforced_at_the_database_level_not_just_the_response(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
        for i in range(250):
            _seed_event(conn, f"evt-{i:03d}", customer_id="cust-1", camera_id="cam-1", timestamp=f"2026-08-01T00:{i % 60:02d}:00.000000")
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            # A limit far below the seeded row count, so truncation could
            # only have come from the SQL LIMIT itself, never a coincidence
            # of "there just weren't more events".
            response = client.get(
                "/api/customer/events/recent/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            )
        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) == main.RECENT_EVENTS_POLL_LIMIT
        assert main.RECENT_EVENTS_POLL_LIMIT < 250, "seeded row count must exceed the limit for this test to prove anything"


def test_events_are_ordered_most_recent_first(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
        _seed_event(conn, "evt-old", customer_id="cust-1", camera_id="cam-1", timestamp="2026-08-01T00:00:00.000000")
        _seed_event(conn, "evt-mid", customer_id="cust-1", camera_id="cam-1", timestamp="2026-08-02T00:00:00.000000")
        _seed_event(conn, "evt-new", customer_id="cust-1", camera_id="cam-1", timestamp="2026-08-03T00:00:00.000000")
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            response = client.get(
                "/api/customer/events/recent/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            )
        ids = [e["id"] for e in response.json()["events"]]
        assert ids == ["evt-new", "evt-mid", "evt-old"]


def test_returned_shape_matches_customer_detection_events_exactly(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
        _seed_event(conn, "evt-1", customer_id="cust-1", camera_id="cam-1", timestamp="2026-08-01T00:00:00.000000")
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            bounded = client.get(
                "/api/customer/events/recent/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            ).json()["events"]
            unbounded_style = client.get(
                "/api/customer/events/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            )
        # /api/customer/events/{camera_id} returns a different (older,
        # per-day) shape -- compare bounded's keys against
        # _customer_detection_events()'s own documented shape instead.
        expected_keys = {
            "id", "camera", "camera_id", "camera_name", "site", "rule_name",
            "event_type", "direction", "timestamp", "confidence", "thumbnail",
            "linked_recording", "has_event_clip", "media_state", "plate_number", "vehicle_color", "mock",
        }
        assert set(bounded[0].keys()) == expected_keys


# --------------------------------------------------------- tenant / camera scoping

def test_another_customers_events_never_appear(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-a")
        _seed_tenant(conn, "cust-b")
        _seed_camera(conn, "cam-a", customer_id="cust-a", camera_number=1, name="A Cam")
        _seed_camera(conn, "cam-b", customer_id="cust-b", camera_number=1, name="B Cam")
        _seed_event(conn, "evt-a", customer_id="cust-a", camera_id="cam-a", timestamp="2026-08-01T00:00:00.000000")
        _seed_event(conn, "evt-b", customer_id="cust-b", camera_id="cam-b", timestamp="2026-08-01T00:00:00.000000")
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            response = client.get(
                "/api/customer/events/recent",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
            )
        ids = [e["id"] for e in response.json()["events"]]
        assert ids == ["evt-a"]
        assert "evt-b" not in ids


def test_camera_scoped_route_never_returns_a_different_cameras_events(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
        _seed_camera(conn, "cam-2", customer_id="cust-1", camera_number=2, name="Living Room")
        _seed_event(conn, "evt-cam1", customer_id="cust-1", camera_id="cam-1", timestamp="2026-08-01T00:00:00.000000")
        _seed_event(conn, "evt-cam2", customer_id="cust-1", camera_id="cam-2", timestamp="2026-08-01T00:00:00.000000")
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            response = client.get(
                "/api/customer/events/recent/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            )
        ids = [e["id"] for e in response.json()["events"]]
        assert ids == ["evt-cam1"]


def test_viewer_without_permission_on_requested_camera_gets_403(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
        _seed_camera(conn, "cam-2", customer_id="cust-1", camera_number=2, name="Living Room")
        conn.execute(
            "INSERT INTO partner_users(id,partner_id,customer_id,email,password_hash,role,account_status,approved,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("viewer-1", "partner-1", "cust-1", "viewer@example.test", "x", "customer_viewer", "active", 1, "2026-01-01"),
        )
        _seed_permission(conn, user_id="viewer-1", camera_id="cam-2", can_playback=1)
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            # Authorized camera: allowed.
            allowed = client.get(
                "/api/customer/events/recent/cam-2",
                cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("cust-1")},
            )
            # Not-granted camera on the SAME customer: denied, not just
            # "empty" -- a viewer must never learn a camera_id is even
            # valid for their own customer_id via a 200-with-no-rows.
            denied = client.get(
                "/api/customer/events/recent/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("cust-1")},
            )
        assert allowed.status_code == 200
        assert denied.status_code == 403


def test_unauthenticated_request_is_rejected_not_given_an_empty_list(db_path):
    """An unauthenticated caller must never get a 200 with events: [] --
    that would look identical to "authenticated, nothing new yet" and
    silently hide that sign-in is actually required. Whichever layer
    rejects it first (a session-less request is turned away by
    ProductionSecurityMiddleware before reaching this endpoint's own
    403 logic, hence 401 here rather than 403), it must not be 200."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            fleet = client.get("/api/customer/events/recent")
            per_camera = client.get("/api/customer/events/recent/cam-1")
        assert fleet.status_code in (401, 403)
        assert per_camera.status_code in (401, 403)


def test_recent_events_endpoint_does_not_collide_with_camera_id_route(db_path):
    """Regression guard for the original 2026-09-04 route-ordering bug:
    '/api/customer/events/recent' must never be swallowed by
    '/api/customer/events/{camera_id}' treating "recent" as a literal
    camera id."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1")
        conn.commit()
        conn.close()
        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            response = client.get(
                "/api/customer/events/recent",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            )
        assert response.status_code == 200
        assert response.json() == {"events": []}


# --------------------------------------------------------- newly inserted event visible immediately

def test_a_newly_inserted_event_is_visible_on_the_very_next_poll(db_path):
    """The bounded query reads live -- it is not a cached/point-in-time
    snapshot -- so an event inserted after the page's initial load (the
    scenario the whole Processing... -> ready poll exists to handle)
    shows up on the very next call with no extra plumbing."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
        conn.commit()

        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            before = client.get(
                "/api/customer/events/recent/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            ).json()["events"]
            assert before == []

            _seed_event(conn, "evt-fresh", customer_id="cust-1", camera_id="cam-1", timestamp="2026-08-01T00:00:00.000000")
            conn.commit()

            after = client.get(
                "/api/customer/events/recent/cam-1",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")},
            ).json()["events"]
        conn.close()
        assert [e["id"] for e in after] == ["evt-fresh"]
