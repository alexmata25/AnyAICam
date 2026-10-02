"""Regression coverage for the Dashboard's "Recent events" widget
(2026-09-23):

Both the initial server-rendered state and the client-side periodic
refresh read from load_motion_events()/'/api/events' -- the legacy,
non-tenant-scoped event source. Any real customer whose activity flows
through the modern detection_events pipeline (every other
customer-facing surface -- Events, Smart Alerts, Live's own Analytics
panel, Playback's timeline -- already reads that pipeline) always got
zero rows back from the legacy source, so this widget permanently
showed "Recent events will appear here after motion is detected." even
with heavy, continuous real activity.

Fixed by reading the same real, tenant-scoped source those other
surfaces already use (_customer_recent_events_bounded(), the exact
function backing /api/customer/events/recent) for both the initial
render and the poll. Its returned shape (thumbnail, event_type,
camera, confidence, timestamp, linked_recording) already matches what
render_event_card()/buildEventCard() expect, so this is a pure data-
source swap -- neither renderer changed.

Same fixture idiom as test_dashboard_live_view_links.py /
test_dashboard_camera_snapshots.py: real HTTP through the real app
(TestClient(main.app), base_url="https://app.anyaicam.com"), a
throwaway sqlite DB via override_target().

Camera-count-agnostic: fleets of 2 and 6 cameras (neither matching the
5-camera Ryzen pilot), events spread across different cameras.
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
    return tmp_path / "test_dashboard_recent_events.db"


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


def _seed_detection_event(conn, event_id, camera_id, customer_id, site_id, appliance_id, event_type, confidence, timestamp):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, site_id, appliance_id, camera_id, event_id, event_type, confidence, 1, None, timestamp, timestamp),
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


@pytest.mark.parametrize("camera_count", [2, 6])
def test_dead_legacy_event_source_is_gone_from_the_rendered_page(http_client, db_path, camera_count):
    conn = sqlite3.connect(db_path)
    _seed_fleet(conn, "cust-1", camera_count)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    assert "recent_events = load_motion_events()" not in response.text
    assert "fetch('/api/events?limit=6'" not in response.text
    assert "fetch('/api/customer/events/recent'" in response.text


@pytest.mark.parametrize("camera_count", [2, 6])
def test_initial_render_shows_real_recent_activity_not_the_empty_state(http_client, db_path, camera_count):
    conn = sqlite3.connect(db_path)
    camera_ids = _seed_fleet(conn, "cust-1", camera_count)
    _seed_detection_event(conn, "evt-1", camera_ids[0], "cust-1", f"site-cust-1", "app-cust-1", "person", 0.94, "2026-09-22T20:00:00")
    _seed_detection_event(conn, "evt-2", camera_ids[-1], "cust-1", f"site-cust-1", "app-cust-1", "vehicle", 0.81, "2026-09-22T20:05:00")
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "Recent events will appear here after motion is detected." not in html.split('id="dashboard-event-grid"')[1].split("</section>")[0]
    assert "Person detected" in html
    assert "Vehicle detected" in html


def test_empty_state_still_shows_honestly_when_there_really_is_no_activity(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_fleet(conn, "cust-1", 3)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "Recent events will appear here after motion is detected." in html.split('id="dashboard-event-grid"')[1].split("</section>")[0]


def test_two_customers_never_see_each_others_recent_events(http_client, db_path):
    conn = sqlite3.connect(db_path)
    ids_a = _seed_fleet(conn, "cust-a", 2)
    ids_b = _seed_fleet(conn, "cust-b", 2)
    _seed_detection_event(conn, "evt-a", ids_a[0], "cust-a", "site-cust-a", "app-cust-a", "person", 0.9, "2026-09-22T20:00:00")
    _seed_detection_event(conn, "evt-b", ids_b[0], "cust-b", "site-cust-b", "app-cust-b", "vehicle", 0.9, "2026-09-22T20:00:00")
    conn.commit()
    conn.close()
    response_a = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert "Person detected" in response_a.text
    assert "Vehicle detected" not in response_a.text
