"""Dashboard "Recording" stat fix (2026-09-21): the system-summary stat
used to be the literal string "Continuous", never read from any camera
row -- confirmed live on Ryzen: every one of that customer's 5 cameras
is genuinely running local_recording_mode="event" (writing to
_event_buffer/), so the dashboard's own stat was flatly wrong for
exactly the deployment it was supposed to describe. Now derived from
cameras.local_recording_mode using the SAME default-is-"continuous"/
only-"event"-if-the-column-says-so interpretation this file's own
canonical reader of that column (the local recording pipeline's
_local_recording_settings_for_camera()) already establishes.

Reuses test_dashboard_camera_tenant_scoping.py's own established harness
(http_client against https://app.anyaicam.com, same seed helpers).
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_dashboard_recording_mode_stat.db"


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


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name, local_recording_mode=None):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,local_recording_mode,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, "configured", camera_number, local_recording_mode, "2026-01-01"),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _recording_stat(html: str) -> str:
    marker = '<span class="stat-label">Recording</span><span class="stat-value">'
    start = html.index(marker) + len(marker)
    return html[start:html.index("</span>", start)]


def test_five_event_mode_cameras_shows_event_not_continuous(http_client, db_path):
    """The exact confirmed-live Ryzen shape: 5 real cameras, all
    local_recording_mode="event" (writing to _event_buffer/)."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    for n in range(1, 6):
        _seed_camera(conn, f"cam-{n}", customer_id="cust-1", camera_number=n, name=f"Camera {n}", local_recording_mode="event")
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    assert _recording_stat(response.text) == "Event"


def test_cameras_with_no_recording_mode_set_default_to_continuous(http_client, db_path):
    """NULL local_recording_mode (the schema default) means Continuous
    -- the same interpretation the real recording pipeline uses, not a
    fix-specific invention."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    for n in range(1, 4):
        _seed_camera(conn, f"cam-{n}", customer_id="cust-1", camera_number=n, name=f"Camera {n}", local_recording_mode=None)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert _recording_stat(response.text) == "Continuous"


def test_a_mix_of_event_and_continuous_cameras_shows_mixed(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Camera 1", local_recording_mode="event")
    _seed_camera(conn, "cam-2", customer_id="cust-1", camera_number=2, name="Camera 2", local_recording_mode=None)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert _recording_stat(response.text) == "Mixed"


def test_a_customer_with_zero_real_cameras_shows_a_dash_not_a_guess(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert _recording_stat(response.text) == "—"


def test_a_second_customers_cameras_never_affect_the_first_customers_stat(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_tenant(conn, "cust-b")
    _seed_camera(conn, "a-cam-1", customer_id="cust-a", camera_number=1, name="A Camera 1", local_recording_mode=None)
    _seed_camera(conn, "b-cam-1", customer_id="cust-b", camera_number=1, name="B Camera 1", local_recording_mode="event")
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert _recording_stat(response.text) == "Continuous"
