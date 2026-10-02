"""Regression coverage for the Dashboard's per-camera preview tiles
(2026-09-23):

Every tile previously tried to attach an HLS player to
/hls/camera{N}.m3u8 -- a local-appliance path never served by this
cloud portal at all (confirmed live: 404 on every load, for every
camera, on the real deployed staging environment). Every tile was
permanently stuck on "Connecting to Camera N" with no way to ever
resolve, and a separate 15s watchdog kept re-invoking the same dead
function forever.

Replaced with a periodic-snapshot approach: each tile polls
GET /api/customer/cameras/{camera_id}/latest-thumbnail, which reuses
the most recent Event-mode clip's own already-captured thumbnail
(_customer_camera_latest_thumbnail_s3_key(), same DB shape as the
already-tested _customer_event_thumbnail_s3_key()). A genuine
periodic-capture pipeline (new appliance-side code) and a real
live-relay session per tile (5 concurrent sessions on every dashboard
load, no cloud-relay entitlement on this account's own plan) were both
explicitly ruled out -- this is a read-only reuse of an existing,
already-working data source.

Same fixture idiom as test_dashboard_live_view_links.py: real HTTP
through the real app (TestClient(main.app), base_url=
"https://app.anyaicam.com" so TrustedHostMiddleware doesn't 400 every
request), a throwaway sqlite DB via override_target().

Camera-count-agnostic coverage: the fleet size varies per test
(3 and 7 cameras, neither matching the 5-camera Ryzen pilot) to prove
the tile-discovery loop is driven by what's actually rendered, not a
fixed range -- the previous JS hardcoded 1..4 and silently never even
attempted a 5th camera.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_dashboard_camera_snapshots.db"


@pytest.fixture()
def http_client(db_path):
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


def _seed_event_with_thumbnail(conn, event_id, camera_id, customer_id, site_id, appliance_id, started_at, thumbnail_s3_key):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, site_id, appliance_id, camera_id, event_id, "motion", 0.9, 1, None, started_at, started_at),
    )
    conn.execute(
        "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,"
        "started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"media-{event_id}", event_id, customer_id, camera_id, f"events/{event_id}.mp4", thumbnail_s3_key, started_at, started_at, started_at),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _seed_fleet(conn, customer_id, camera_count):
    _seed_tenant(conn, customer_id)
    camera_ids = []
    for n in range(1, camera_count + 1):
        camera_id = f"cam-{customer_id}-{n}"
        _seed_camera(conn, camera_id, customer_id=customer_id, camera_number=n, name=f"Camera {n}")
        camera_ids.append(camera_id)
    return camera_ids


@pytest.mark.parametrize("camera_count", [3, 7])
def test_dead_hls_path_is_completely_gone_from_the_rendered_page(http_client, db_path, camera_count):
    conn = sqlite3.connect(db_path)
    _seed_fleet(conn, "cust-1", camera_count)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    # The dead runtime reference must be gone (a doc comment elsewhere
    # on the page may still name the old path for context, which is
    # fine); the actual source assignment and the function/element it
    # drove must not be.
    assert "const source=`/hls/camera" not in response.text
    assert "function attachDashboardStream" not in response.text
    assert "attachDashboardStream(cameraNumber)" not in response.text
    assert 'id="dashboard-video-' not in response.text


@pytest.mark.parametrize("camera_count", [3, 7])
def test_every_rendered_tile_gets_a_snapshot_img_element(http_client, db_path, camera_count):
    conn = sqlite3.connect(db_path)
    _seed_fleet(conn, "cust-1", camera_count)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    for n in range(1, camera_count + 1):
        assert f'id="dashboard-snapshot-{n}"' in html
    assert "attachDashboardSnapshot" in html
    # Discovery must be DOM-driven, not a fixed range -- the bug this
    # replaces was exactly a hardcoded 1..4 loop.
    assert "cameraNumber<=4" not in html
    assert 'id^="dashboard-camera-"' in html


def test_latest_thumbnail_s3_key_returns_the_newest_ready_event(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Camera 1")
        _seed_event_with_thumbnail(conn, "evt-old", "cam-1", "cust-1", "site-cust-1", "app-cust-1", "2026-09-22T10:00:00", "events/evt-old-thumb.jpg")
        _seed_event_with_thumbnail(conn, "evt-new", "cam-1", "cust-1", "site-cust-1", "app-cust-1", "2026-09-22T12:00:00", "events/evt-new-thumb.jpg")
        conn.commit()
        key = main._customer_camera_latest_thumbnail_s3_key("cam-1")
    assert key == "events/evt-new-thumb.jpg"


def test_latest_thumbnail_s3_key_skips_events_still_processing_with_no_media(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Camera 1")
        _seed_event_with_thumbnail(conn, "evt-ready", "cam-1", "cust-1", "site-cust-1", "app-cust-1", "2026-09-22T10:00:00", "events/evt-ready-thumb.jpg")
        _seed_event_with_thumbnail(conn, "evt-pending", "cam-1", "cust-1", "site-cust-1", "app-cust-1", "2026-09-22T12:00:00", "")
        conn.commit()
        key = main._customer_camera_latest_thumbnail_s3_key("cam-1")
    assert key == "events/evt-ready-thumb.jpg"


def test_latest_thumbnail_route_requires_authorization(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: False)
        with pytest.raises(main.HTTPException) as excinfo:
            main.customer_camera_latest_thumbnail(camera_id="cam-not-mine", request=None)
    assert excinfo.value.status_code == 403


def test_latest_thumbnail_route_404s_honestly_when_nothing_exists_yet(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Camera 1")
        conn.commit()
        monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)
        with pytest.raises(main.HTTPException) as excinfo:
            main.customer_camera_latest_thumbnail(camera_id="cam-1", request=None)
    assert excinfo.value.status_code == 404


def test_two_cameras_never_leak_each_others_latest_thumbnail(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1")
        _seed_camera(conn, "cam-a", customer_id="cust-1", camera_number=21, name="Camera A")
        _seed_camera(conn, "cam-b", customer_id="cust-1", camera_number=52, name="Camera B")
        _seed_event_with_thumbnail(conn, "evt-a", "cam-a", "cust-1", "site-cust-1", "app-cust-1", "2026-09-22T10:00:00", "events/a-thumb.jpg")
        _seed_event_with_thumbnail(conn, "evt-b", "cam-b", "cust-1", "site-cust-1", "app-cust-1", "2026-09-22T10:00:00", "events/b-thumb.jpg")
        conn.commit()
        key_a = main._customer_camera_latest_thumbnail_s3_key("cam-a")
        key_b = main._customer_camera_latest_thumbnail_s3_key("cam-b")
    assert key_a == "events/a-thumb.jpg"
    assert key_b == "events/b-thumb.jpg"
