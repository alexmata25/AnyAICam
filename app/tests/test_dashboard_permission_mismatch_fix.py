"""Dashboard/Live permission-mismatch fix (2026-09-21): dashboard()'s
camera list is scoped by can_playback (_customer_playback_cameras()) --
correct for what to SHOW -- but /customer/cameras/{id}/live itself
requires can_live (_customer_live_cameras(), live_view_page.py). A
customer_viewer granted can_playback but not can_live for a camera used
to get a dashboard card whose link led straight to a page their own JS
then 403'd them out of.

Fix: the card's ACTION now depends on this identity's real can_live
grant for that specific camera -- can_live routes to the Live tools
page exactly as before; can_playback-only routes to /playback?camera=
{id} instead (with a "Playback only" label so it's clearly not the same
action), never to a page that will refuse them. Visibility itself is
unchanged: a camera a viewer can see on the dashboard today is still
exactly as visible after this fix -- only where its card leads differs.
customer_owner has implicit full-fleet access to both permissions (see
_customer_live_cameras()'s own owner branch), so this only ever changes
behavior for a real customer_viewer with a real per-camera permission
mismatch.

Reuses test_dashboard_live_view_links.py's own established harness.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_dashboard_permission_mismatch_fix.db"


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


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, "configured", camera_number, "2026-01-01"),
    )


def _seed_viewer(conn, user_id, *, customer_id, email):
    conn.execute(
        "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, email, "customer_viewer", customer_id, "x", "custom", "2026-01-01"),
    )


def _seed_permission(conn, *, user_id, camera_id, can_playback, can_live):
    conn.execute(
        "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback) VALUES(?,?,?,?)",
        (user_id, camera_id, int(can_live), int(can_playback)),
    )


def _viewer_cookie(email, customer_id):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _href_for(html, camera_number):
    marker = f'id="dashboard-camera-{camera_number}"'
    start = html.rindex('href="', 0, html.index(marker)) + len('href="')
    return html[start:html.index('"', start)]


def test_viewer_with_can_live_still_gets_the_live_tools_link(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_playback=True, can_live=True)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")})
    assert response.status_code == 200
    assert _href_for(response.text, 1) == "/customer/cameras/cam-1/live"
    assert "Playback only" not in response.text


def test_viewer_with_playback_only_is_routed_to_playback_not_live_tools(http_client, db_path):
    """The core fix: must never send a playback-only viewer to a page
    that will 403 them."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_playback=True, can_live=False)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")})
    assert response.status_code == 200
    html = response.text
    assert _href_for(html, 1) == "/playback?camera=cam-1"
    assert "/customer/cameras/cam-1/live" not in html
    assert "Playback only" in html


def test_viewer_visibility_is_not_reduced_camera_still_appears_on_dashboard(http_client, db_path):
    """Visibility must stay exactly as it was -- only the destination
    changes. A playback-only camera still renders a real card."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_playback=True, can_live=False)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")})
    assert response.text.count('class="dashboard-camera-card"') == 1
    assert 'id="dashboard-camera-1"' in response.text


def test_a_viewer_with_neither_permission_never_sees_the_camera_at_all(http_client, db_path):
    """Unchanged pre-existing behavior: _customer_playback_cameras()
    already excludes a camera with no can_playback grant -- this fix
    must not accidentally start showing it."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_playback=False, can_live=False)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")})
    assert response.text.count('class="dashboard-camera-card"') == 0


def test_mixed_fleet_each_camera_routes_independently(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
    _seed_camera(conn, "cam-2", customer_id="cust-1", camera_number=2, name="Back Yard")
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_playback=True, can_live=True)
    _seed_permission(conn, user_id="user-1", camera_id="cam-2", can_playback=True, can_live=False)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")})
    html = response.text
    assert _href_for(html, 1) == "/customer/cameras/cam-1/live"
    assert _href_for(html, 2) == "/playback?camera=cam-2"


def test_customer_owner_always_gets_live_tools_full_fleet_access(http_client, db_path):
    """customer_owner has implicit can_live for every camera -- no
    per-camera permission row exists or is needed."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    for n in range(1, 4):
        _seed_camera(conn, f"cam-{n}", customer_id="cust-1", camera_number=n, name=f"Camera {n}")
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    for n in range(1, 4):
        assert _href_for(html, n) == f"/customer/cameras/cam-{n}/live"
    assert "Playback only" not in html


def test_multi_appliance_ambiguous_camera_number_still_falls_back_safely_for_a_viewer(http_client, db_path):
    """The earlier multi-appliance collision fix and this permission fix
    must compose correctly: an ambiguous camera_number still falls back
    to the safe generic href, never a can_live lookup on the wrong id."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-1','Second Site','2026-01-01')")
    _seed_camera(conn, "cam-1a", customer_id="cust-1", camera_number=1, name="Front Door")
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        ("cam-1b", "cust-1", "site-2", "Back Door", "configured", 1, "2026-01-01"),
    )
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1a", can_playback=True, can_live=True)
    _seed_permission(conn, user_id="user-1", camera_id="cam-1b", can_playback=True, can_live=True)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")})
    html = response.text
    assert "/customer/cameras/cam-1a/live" not in html
    assert "/customer/cameras/cam-1b/live" not in html
    assert "/playback?camera=cam-1a" not in html
    assert _href_for(html, 1) == "/customer-live"
