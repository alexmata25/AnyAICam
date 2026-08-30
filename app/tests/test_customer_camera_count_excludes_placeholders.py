"""Regression coverage for a confirmed-live bug: GET /api/customer/cameras
counted `status=='configured'` as "a configured camera," which an
onboarding placeholder (device_key=NULL, never a real discovered
device) satisfies just as easily as a real, provisioned camera --
Step 5's "Save camera setup" sets status='configured' unconditionally
for every row it submits, placeholders included, the instant a
customer renames one and saves. Confirmed live: a customer with an
entitlement of 5 who had provisioned all 5 real cameras still saw
"9 of 5 cameras configured" once the 4 onboarding placeholders (never
provisioned, never given a device_key) were renamed and saved through
the same UI flow.

Fixed by counting rows with a real device_key instead of by status --
a real, provisioned camera always has one (see appliance_cloud.py's
appliance_submit_provisioning(), the only code path that ever creates
a cameras row from a confirmed device); a placeholder never does.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_camera_count_excludes_placeholders.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id="cust-1", partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Real Customer", "customer@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Main Site", "2026-01-01"))
    conn.commit()


def _seed_plan(conn, customer_id="cust-1", camera_quantity=5):
    conn.execute(
        "INSERT INTO plans(id,customer_id,camera_quantity,created_at) VALUES(?,?,?,?)",
        (f"plan-{customer_id}", customer_id, camera_quantity, "2026-01-01"),
    )
    conn.commit()


def _seed_placeholder_camera(conn, camera_id, *, customer_id="cust-1", name, status="pending_installation"):
    """An onboarding-time placeholder: no device_key, no camera_number --
    exactly what Step 5's server-rendered table starts every camera
    row as before a real device is ever discovered/provisioned."""
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,device_key,camera_number,created_at) VALUES(?,?,?,?,?,NULL,NULL,?)",
        (camera_id, customer_id, "site-1", name, status, "2026-01-01"),
    )
    conn.commit()


def _seed_real_camera(conn, camera_id, *, customer_id="cust-1", name, device_key, camera_number, status="configured"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,device_key,camera_number,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, "site-1", name, status, device_key, camera_number, "2026-01-01"),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def test_renamed_placeholder_saved_through_step5_never_counts_as_configured(http_client, db_path):
    """The exact live bug: a placeholder that went through 'Save camera
    setup' (status='configured', still device_key=NULL) must not count."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_plan(conn, camera_quantity=5)
    _seed_placeholder_camera(conn, "ph-1", name="Driveway Right", status="configured")
    _seed_placeholder_camera(conn, "ph-2", name="Driveway Left", status="configured")
    _seed_placeholder_camera(conn, "ph-3", name="Living Room", status="configured")
    _seed_placeholder_camera(conn, "ph-4", name="Front Door (placeholder)", status="configured")
    response = http_client.get("/api/customer/cameras", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    data = response.json()
    assert data["configured_camera_count"] == 0
    assert data["expected_camera_count"] == 5
    assert data["onboarding_complete"] is False


def test_real_provisioned_cameras_count_correctly_alongside_placeholders(http_client, db_path):
    """The confirmed-live scenario: 4 renamed-but-never-provisioned
    placeholders plus 5 real, device_key-bearing cameras (9 total rows)
    must report exactly 5 configured, not 9."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_plan(conn, camera_quantity=5)
    for i, name in enumerate(["Driveway Right", "Driveway Left", "Living Room", "Front Door"], start=1):
        _seed_placeholder_camera(conn, f"ph-{i}", name=name, status="configured")
    for i in range(1, 6):
        _seed_real_camera(conn, f"real-{i}", name=f"Camera {i}", device_key=f"urn:uuid:real-device-{i}", camera_number=i)
    response = http_client.get("/api/customer/cameras", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    data = response.json()
    assert len(data["cameras"]) == 9  # all rows still returned -- nothing hidden or deleted by this endpoint
    assert data["configured_camera_count"] == 5
    assert data["expected_camera_count"] == 5
    assert data["onboarding_complete"] is True


def test_a_real_camera_not_yet_marked_configured_status_still_counts(http_client, db_path):
    """device_key is the signal, not status -- a real, provisioned
    camera whose status hasn't been explicitly set to 'configured' yet
    (e.g. mid-provisioning) still has a device_key and still counts,
    matching "provisioned state," not a UI-driven status string."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_plan(conn, camera_quantity=1)
    _seed_real_camera(conn, "real-1", name="Camera 1", device_key="urn:uuid:real-device-1", camera_number=1, status="pending_installation")
    response = http_client.get("/api/customer/cameras", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    data = response.json()
    assert data["configured_camera_count"] == 1


def test_zero_placeholders_and_zero_real_cameras_reports_zero_configured(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_plan(conn, camera_quantity=5)
    response = http_client.get("/api/customer/cameras", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    data = response.json()
    assert data["configured_camera_count"] == 0
    assert data["cameras"] == []
