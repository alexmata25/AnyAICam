"""Live-view consolidation (2026-09-21): /customer-live ("Your cameras",
live_view_page.py) is the canonical customer-facing Live View destination
-- customer-scoped from the start (never the shared, multi-tenant
get_camera_numbers() sequence that produced the confirmed-live "duplicate
Camera 1" cross-tenant bug on the old GET / grid page, see that route's
own docstring). This file proves /dashboard's Live-related links now
point a real customer session at /customer-live (and its per-camera
single-camera tools page, /customer/cameras/{id}/live) instead of the old
GET / grid or the old GET /camera/{camera_number} page -- while a
staff/admin session viewing the same shared /dashboard route keeps its
own existing "/" link unchanged, since /customer-live 303-redirects any
non-customer_owner/customer_viewer role straight to /partner-login.

Reuses test_dashboard_camera_tenant_scoping.py's own established harness
(http_client against https://app.anyaicam.com -- required so
TrustedHostMiddleware doesn't 400 every request on this dev host, per
that file's own comment) and seed helpers.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_dashboard_live_view_links.db"


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


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator")


def _seed_five_cameras(conn, customer_id="cust-1"):
    _seed_tenant(conn, customer_id)
    for n in range(1, 6):
        _seed_camera(conn, f"cam-{n}", customer_id=customer_id, camera_number=n, name=f"Camera {n}")


# ------------------------------------------------- /dashboard's Live links


def test_open_live_view_button_links_to_customer_live_for_a_real_customer(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_five_cameras(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    assert 'href="/customer-live">Open live view</a>' in response.text
    assert 'href="/">Open live view</a>' not in response.text


def test_cameras_online_stat_link_points_to_customer_live_for_a_real_customer(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_five_cameras(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert 'href="/customer-live"><strong id="today-cameras-count">' in html


def test_camera_preview_cards_link_to_the_new_single_camera_page_not_the_old_one(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_five_cameras(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    for n in range(1, 6):
        assert f'href="/customer/cameras/cam-{n}/live" id="dashboard-camera-{n}"' in html
        assert f'href="/camera/{n}" id="dashboard-camera-{n}"' not in html


def test_two_appliances_with_the_same_camera_number_never_link_to_the_wrong_camera(http_client, db_path):
    """camera_number is only unique PER APPLIANCE, not across a
    customer's whole fleet -- nothing in provisioning prevents a
    customer with two appliances each having their own "Camera 1".
    _dashboard_camera_ids_by_number must never let one appliance's row
    silently overwrite the other's, which would send one of the two
    resulting dashboard cards to a real but WRONG camera's tools page.
    An ambiguous camera_number must fall back to the safe generic
    /customer-live href instead of guessing which id is correct."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-1','Second Site','2026-01-01')")
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('app-2','cust-1','site-2','cloud-2','2026-01-01')")
    _seed_camera(conn, "cam-1a", customer_id="cust-1", camera_number=1, name="Front Door")
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        ("cam-1b", "cust-1", "site-2", "Back Door", "configured", 1, "2026-01-01"),
    )
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "/customer/cameras/cam-1a/live" not in html
    assert "/customer/cameras/cam-1b/live" not in html
    assert 'href="/customer-live" id="dashboard-camera-1"' in html


def test_a_customer_with_no_camera_id_resolved_falls_back_to_customer_live_not_a_broken_link(http_client, db_path):
    """A camera_number this dashboard couldn't map to a real camera id
    (should not happen in practice, since _dashboard_camera_numbers and
    _dashboard_camera_ids_by_number are built from the exact same
    _customer_dashboard_cameras list) still gets a working, canonical
    fallback href rather than reproducing the old /camera/{n} link."""
    conn = sqlite3.connect(db_path)
    _seed_five_cameras(conn)
    conn.commit()
    conn.close()
    import inspect
    source = inspect.getsource(main.dashboard)
    assert "_dashboard_camera_hrefs" in source
    assert "_dashboard_live_view_href" in source
    assert "else _dashboard_live_view_href" in source


def test_staff_session_open_live_view_button_still_links_to_the_legacy_grid(http_client, db_path):
    """A staff/admin session viewing this same shared /dashboard route
    must keep its existing "/" link -- /customer-live 303-redirects any
    non-customer_owner/customer_viewer role to /partner-login, so
    hardcoding it here would break this exact button for staff."""
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()})
    assert response.status_code == 200
    assert 'href="/">Open live view</a>' in response.text


# --------------------------------------------- canonical /customer-live route


def test_customer_live_is_reachable_for_a_real_customer_owner_session(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_five_cameras(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    assert "Camera 1" in response.text


def test_customer_live_rejects_a_non_customer_role(http_client, db_path):
    response = http_client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()})
    assert response.status_code == 303
    assert response.headers["location"] == "/partner-login"


def test_customer_live_rejects_no_session_at_all(http_client, db_path):
    response = http_client.get("/customer-live")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/customer-login.html")
