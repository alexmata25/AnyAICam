"""End-to-end regression coverage for /customer-account's "installed/
configured" gate against the real app (TestClient(main.app)), a real
signed partner_identity() cookie, and a throwaway sqlite DB.

Root cause (see camera_install_state.py's own module docstring): the
gate used to require cameras.status='configured' -- a column only ever
written by the customer's own manual "Save camera setup" step in
/customer/setup -- to consider a camera installed. An installer-
provisioned customer (a partner/technician set up the appliance and
cameras directly; the customer never opened their own setup wizard) has
real, online, recording cameras that were never touched by that step,
so they were redirected to /customer/setup indefinitely and shown
"Pending installation" for every camera regardless of the appliance's
own live heartbeat (appliance_camera_status) already reporting them
online and recording. Fixed by treating that real appliance-reported
state as an independent, equally-valid "installed" signal.
"""

import sqlite3

import pytest

import partner_portal
import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_account_install_state.db"


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


def _seed_activated_appliance(conn, appliance_id="appl-1", customer_id="cust-1"):
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
        (appliance_id, customer_id, "site-1", f"AIC-{appliance_id}", "activated", "2026-01-01"),
    )
    conn.commit()


def _seed_camera(conn, camera_id, *, customer_id="cust-1", appliance_id="appl-1", camera_number, name, status=None, device_key=None):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,device_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, "site-1", appliance_id, camera_number, name, status, device_key, "2026-01-01"),
    )
    conn.commit()


def _seed_camera_slot_entitlement(conn, customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8):
    conn.execute(
        "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_checkout_session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (f"ent-{customer_id}-{product}", customer_id, product, camera_slot_quantity, "active", f"cs_test_{customer_id}", "2026-01-01", "2026-01-01"),
    )
    conn.commit()


def _seed_appliance_camera_status(conn, *, appliance_id, camera_id, online, recording):
    conn.execute(
        "INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,updated_at) VALUES(?,?,?,?,?,?,?)",
        (appliance_id, camera_id, camera_id, int(online), int(recording), 0, "2026-08-27T00:00:00"),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _viewer_cookie(customer_id="cust-1"):
    return partner_portal._token("viewer@example.test", "customer_viewer", None, customer_id, None)


def test_installer_provisioned_camera_never_through_wizard_shows_installed_not_pending(http_client, db_path):
    """The exact bug: 5 real Ryzen-style cameras, set up by an
    installer, never went through /customer/setup -- cameras.status is
    NULL for all of them -- but the appliance is actively reporting
    them online and recording."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    for n in range(1, 6):
        camera_id = f"cam-{n}"
        _seed_camera(conn, camera_id, camera_number=n, name=f"Camera {n}", status=None)
        _seed_appliance_camera_status(conn, appliance_id="appl-1", camera_id=camera_id, online=True, recording=True)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})

    assert response.status_code == 200  # never redirected to /customer/setup
    for n in range(1, 6):
        assert f"Camera {n}" in response.text
    assert response.text.count("Online · Recording") == 5
    assert "Pending installation" not in response.text


def test_truly_unprovisioned_customer_still_goes_to_setup(http_client, db_path):
    """A genuinely new customer -- activated appliance, but zero
    cameras ever reported in or configured -- must still land on
    /customer/setup. This gate is not being disabled, only widened."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 303
    assert response.headers["location"] == "/customer/setup"


def test_camera_that_completed_the_wizard_still_works_unchanged(http_client, db_path):
    """Backward compatibility: a customer who manually completed Save
    camera setup (status='configured') on a real, already-discovered
    camera (device_key set -- the one column only ever written by a
    confirmed device, see appliance_cloud.py's
    appliance_submit_provisioning()) keeps working exactly as before,
    even with no live appliance report at all."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera(conn, "cam-1", camera_number=1, name="Front Door", status="configured", device_key="urn:uuid:real-device-1")

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Front Door" in response.text
    assert "Configured" in response.text


def test_camera_reporting_recording_only_is_installed_with_recording_label(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera(conn, "cam-1", camera_number=1, name="Loading Dock", status=None)
    _seed_appliance_camera_status(conn, appliance_id="appl-1", camera_id="cam-1", online=False, recording=True)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Recording" in response.text
    assert "Pending installation" not in response.text


def test_mixed_fleet_only_the_truly_unprovisioned_camera_stays_pending(http_client, db_path):
    """3-of-4 real, reporting cameras plus one genuinely never-
    provisioned camera (no status, no appliance report, no recordings)
    -- the real ones show installed, the unprovisioned one is simply
    absent from the customer's own camera list (not shown as a fake
    'Pending installation' tile mixed in with real ones)."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    for n in range(1, 4):
        camera_id = f"cam-{n}"
        _seed_camera(conn, camera_id, camera_number=n, name=f"Camera {n}", status=None)
        _seed_appliance_camera_status(conn, appliance_id="appl-1", camera_id=camera_id, online=True, recording=True)
    _seed_camera(conn, "cam-unprovisioned", camera_number=4, name="Camera 4", status=None)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    for n in range(1, 4):
        assert f"Camera {n}" in response.text
    assert "Camera 4" not in response.text


def test_customer_viewer_sees_installer_provisioned_cameras_too(http_client, db_path):
    """customer_viewer is never redirect-gated at all -- confirms the
    same widened installed-state applies to the camera list it reaches
    directly, not just the customer_owner gate."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera(conn, "cam-1", camera_number=1, name="Warehouse", status=None)
    _seed_appliance_camera_status(conn, appliance_id="appl-1", camera_id="cam-1", online=True, recording=False)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie()})
    assert response.status_code == 200
    assert "Warehouse" in response.text
    assert "Online" in response.text


def test_route_creates_no_new_camera_or_appliance_rows(http_client, db_path):
    """The gate fix is read-only decision logic -- verifies the route
    itself never inserts/duplicates appliances or cameras."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    for n in range(1, 6):
        camera_id = f"cam-{n}"
        _seed_camera(conn, camera_id, camera_number=n, name=f"Camera {n}", status=None)
        _seed_appliance_camera_status(conn, appliance_id="appl-1", camera_id=camera_id, online=True, recording=True)
    conn.close()

    before_cameras = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    before_appliances = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM appliances").fetchone()[0]

    http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})

    after_cameras = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    after_appliances = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM appliances").fetchone()[0]
    assert after_cameras == before_cameras == 5
    assert after_appliances == before_appliances == 1


# ======================================================= placeholder camera slot display
#
# Confirmed live on staging (anyaicamtest@gmail.com): an onboarding
# placeholder camera (device_key=NULL, status forced to 'configured' by
# Step 5's bulk "Save camera setup" -- see camera_install_state.py's own
# module docstring) rendered on /customer-account exactly like a real
# camera: "Camera 1 / Configured / Live view", for a Samsung appliance
# that has never checked in (software Not installed, offline, last
# check-in Never) and never discovered anything. The customer had
# purchased Local 1-8 (8 licensed slots) through Stripe; only one
# placeholder row existed from onboarding.


def test_offline_uninstalled_appliance_with_placeholder_slot_does_not_show_configured(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)
    # The exact live shape: Step 5's bulk save set status='configured'
    # on a placeholder that was never actually discovered or given a
    # device_key.
    _seed_camera(conn, "ph-1", camera_number=None, name="Camera 1", status="configured", device_key=None)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Configured" not in response.text
    assert "Waiting for appliance discovery" in response.text


def test_placeholder_slot_has_no_live_view_link(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)
    _seed_camera(conn, "ph-1", camera_number=None, name="Camera 1", status="configured", device_key=None)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "Live view" not in response.text
    assert "/customer/cameras/" not in response.text  # no live/playback link of any kind


def test_real_configured_camera_still_shows_live_view_alongside_placeholders(http_client, db_path):
    """A mix of one real, discovered camera and unfilled licensed slots
    -- the real one keeps its normal Live view action, the unfilled
    slots get none."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)
    _seed_camera(conn, "real-1", camera_number=1, name="Front Door", status="configured", device_key="urn:uuid:real-device-1")

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Front Door" in response.text
    assert f'/customer/cameras/real-1/live' in response.text
    assert "Waiting for appliance discovery" in response.text  # the other 7 unfilled slots
    assert response.text.count("Waiting for appliance discovery") == 7


def test_licensed_slot_capacity_reflects_the_stripe_entitlement_not_placeholder_row_count(http_client, db_path):
    """Only one placeholder row exists (from onboarding), but the
    customer purchased Local 1-8 -- the dashboard must show 8 licensed
    slots, not 1."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, product="camera_slots_local", camera_slot_quantity=8)
    _seed_camera(conn, "ph-1", camera_number=None, name="Camera 1", status="configured", device_key=None)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "0 of 8 cameras configured" in response.text
    assert response.text.count("Camera slot") == 8  # all 8 licensed slots represented, not just the 1 placeholder row


def test_configured_camera_count_stays_zero_with_only_a_placeholder(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)
    _seed_camera(conn, "ph-1", camera_number=None, name="Camera 1", status="configured", device_key=None)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "0 of 8" in response.text


def test_activated_customer_with_purchased_slots_and_no_real_cameras_sees_dashboard_not_setup(http_client, db_path):
    """The gate no longer redirects an activated customer to /customer/
    setup just because they have zero real cameras -- if they've
    purchased camera slots, they see the dashboard with honest
    "waiting for discovery" placeholders instead."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)

    response = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert response.text.count("Camera slot") == 8


def test_no_fake_camera_discovery_occurs_from_viewing_the_dashboard(http_client, db_path):
    """Viewing the dashboard must never create, claim, or mark any
    camera row as discovered/provisioned -- purely a read/display fix."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)
    _seed_camera(conn, "ph-1", camera_number=None, name="Camera 1", status="configured", device_key=None)
    conn.close()

    http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})

    check = sqlite3.connect(db_path)
    row = check.execute("SELECT device_key, status FROM cameras WHERE id='ph-1'").fetchone()
    assert row == (None, "configured")  # untouched by viewing the page
    assert check.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 1


def test_viewing_the_dashboard_does_not_alter_stripe_entitlements(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_activated_appliance(conn)
    _seed_camera_slot_entitlement(conn, camera_slot_quantity=8)
    conn.close()

    http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})

    check = sqlite3.connect(db_path)
    row = check.execute("SELECT camera_slot_quantity, status FROM customer_entitlements WHERE customer_id='cust-1'").fetchone()
    assert row == (8, "active")
