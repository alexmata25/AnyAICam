"""Customer-facing Intrusion Zone / Line-Crossing rule editor (2026-09-21,
customer_analytics_rules.py) -- tenant isolation, camera ownership
validation, create/update/delete, invalid geometry, direction, and owner
vs viewer permissions, per the user's own explicit regression-test list.

Does NOT test detection/execution -- see customer_analytics_rules.py's own
module docstring: a saved rule here is authorized, validated, and stored
only. There is no edge worker yet that evaluates it.

Reuses the established TestClient-against-https://app.anyaicam.com harness
(test_dashboard_live_view_links.py's own comment explains why the host
matters -- TrustedHostMiddleware) and partner_portal._token() cookie
helper.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_analytics_rules.db"


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


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name="Front Door"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", f"app-{customer_id}", name, "configured", camera_number, "2026-01-01"),
    )


def _seed_viewer(conn, user_id, *, customer_id, email):
    conn.execute(
        "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, email, "customer_viewer", customer_id, "x", "custom", "2026-01-01"),
    )


def _seed_permission(conn, *, user_id, camera_id, can_live=0, can_playback=0, can_settings=0):
    conn.execute(
        "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_settings) VALUES(?,?,?,?,?)",
        (user_id, camera_id, can_live, can_playback, can_settings),
    )


def _owner_cookie(customer_id, email="owner@example.test"):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _viewer_cookie(email, customer_id):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


LINE = {"rule_type": "line_crossing", "name": "Driveway line", "direction": "both", "geometry": [{"x": 0.1, "y": 0.2}, {"x": 0.9, "y": 0.8}]}
ZONE = {"rule_type": "intrusion", "name": "Backyard zone", "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9}, {"x": 0.1, "y": 0.9}]}


def _rules_url(camera_id, rule_id=None):
    base = f"/api/customer/cameras/{camera_id}/analytics-rules"
    return f"{base}/{rule_id}" if rule_id else base


# ------------------------------------------------------------- create/update/delete


def test_owner_can_create_a_line_crossing_rule(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = http_client.post(_rules_url("cam-1"), json=LINE, cookies=cookies)
    assert response.status_code == 200
    body = response.json()
    assert body["rule_type"] == "line_crossing"
    assert body["direction"] == "both"
    assert len(body["geometry"]) == 2
    assert body["enabled"] is True


def test_owner_can_create_an_intrusion_zone_rule(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = http_client.post(_rules_url("cam-1"), json=ZONE, cookies=cookies)
    assert response.status_code == 200
    body = response.json()
    assert body["rule_type"] == "intrusion"
    assert body["direction"] is None
    assert len(body["geometry"]) == 4


def test_owner_can_update_and_then_delete_a_rule(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    rule_id = http_client.post(_rules_url("cam-1"), json=LINE, cookies=cookies).json()["id"]

    updated = http_client.put(_rules_url("cam-1", rule_id), json={"name": "Renamed", "enabled": False}, cookies=cookies)
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "Renamed"
    assert body["enabled"] is False
    # geometry/direction preserved when omitted from the update payload
    assert body["direction"] == "both"
    assert len(body["geometry"]) == 2

    deleted = http_client.delete(_rules_url("cam-1", rule_id), cookies=cookies)
    assert deleted.status_code == 200

    listing = http_client.get(_rules_url("cam-1"), cookies=cookies)
    assert listing.json()["rules"] == []


def test_deleting_an_unknown_rule_id_is_a_404_not_a_silent_success(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = http_client.delete(_rules_url("cam-1", "does-not-exist"), cookies=cookies)
    assert response.status_code == 404


# ------------------------------------------------------------- invalid geometry / direction


@pytest.mark.parametrize(
    "payload,expected_fragment",
    [
        ({"rule_type": "line_crossing", "name": "x", "direction": "both", "geometry": [{"x": 0.1, "y": 0.1}]}, "exactly 2 points"),
        ({"rule_type": "line_crossing", "name": "x", "direction": "sideways", "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}]}, "direction must be one of"),
        ({"rule_type": "intrusion", "name": "x", "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}]}, "at least 3 points"),
        ({"rule_type": "intrusion", "name": "x", "direction": "both", "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9}]}, "does not apply"),
        ({"rule_type": "intrusion", "name": "x", "geometry": [{"x": 1.5, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9}]}, "normalized between 0 and 1"),
        ({"rule_type": "bogus", "name": "x", "geometry": []}, "rule_type must be one of"),
        ({"rule_type": "line_crossing", "name": "", "direction": "both", "geometry": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}]}, "name is required"),
    ],
)
def test_invalid_geometry_and_direction_are_rejected_with_a_clear_400(http_client, db_path, payload, expected_fragment):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = http_client.post(_rules_url("cam-1"), json=payload, cookies=cookies)
    assert response.status_code == 400
    assert expected_fragment in response.json()["detail"]


def test_a_too_large_polygon_is_rejected(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    geometry = [{"x": (i % 10) / 10, "y": (i % 10) / 10} for i in range(21)]
    response = http_client.post(_rules_url("cam-1"), json={"rule_type": "intrusion", "name": "x", "geometry": geometry}, cookies=cookies)
    assert response.status_code == 400
    assert "at most" in response.json()["detail"]


# ------------------------------------------------------------- tenant isolation / camera ownership


def test_a_camera_belonging_to_a_different_customer_is_404_not_leaked(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_tenant(conn, "cust-2")
    _seed_camera(conn, "cam-2", customer_id="cust-2", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = http_client.post(_rules_url("cam-2"), json=LINE, cookies=cookies)
    assert response.status_code == 404


def test_one_customers_rules_never_appear_in_another_customers_listing(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_tenant(conn, "cust-2")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    _seed_camera(conn, "cam-2", customer_id="cust-2", camera_number=1)
    conn.commit()
    conn.close()
    cookies_1 = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    cookies_2 = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-2", email="owner2@example.test")}
    http_client.post(_rules_url("cam-1"), json=LINE, cookies=cookies_1)
    http_client.post(_rules_url("cam-2"), json=ZONE, cookies=cookies_2)

    listing_1 = http_client.get(_rules_url("cam-1"), cookies=cookies_1).json()
    assert len(listing_1["rules"]) == 1
    assert listing_1["rules"][0]["rule_type"] == "line_crossing"

    # cust-1 can never even resolve cust-2's own camera_id to read its rules
    cross_tenant = http_client.get(_rules_url("cam-2"), cookies=cookies_1)
    assert cross_tenant.status_code == 404


def test_a_customer_cannot_update_or_delete_a_rule_scoped_to_another_customers_camera(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_tenant(conn, "cust-2")
    _seed_camera(conn, "cam-2", customer_id="cust-2", camera_number=1)
    conn.commit()
    conn.close()
    cookies_2 = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-2", email="owner2@example.test")}
    rule_id = http_client.post(_rules_url("cam-2"), json=LINE, cookies=cookies_2).json()["id"]

    cookies_1 = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    assert http_client.put(_rules_url("cam-2", rule_id), json={"name": "hijacked"}, cookies=cookies_1).status_code == 404
    assert http_client.delete(_rules_url("cam-2", rule_id), cookies=cookies_1).status_code == 404


# ------------------------------------------------------------- owner vs viewer permissions


def test_viewer_with_no_camera_permission_at_all_is_forbidden(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")}
    response = http_client.get(_rules_url("cam-1"), cookies=cookies)
    assert response.status_code == 403


def test_viewer_with_can_playback_but_no_can_settings_can_read_but_not_write(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_playback=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")}

    listing = http_client.get(_rules_url("cam-1"), cookies=cookies)
    assert listing.status_code == 200
    assert listing.json()["can_edit"] is False

    create = http_client.post(_rules_url("cam-1"), json=LINE, cookies=cookies)
    assert create.status_code == 403
    assert "Camera Settings" in create.json()["detail"]


def test_viewer_with_can_settings_grant_can_create_update_and_delete(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_live=1, can_settings=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")}

    listing = http_client.get(_rules_url("cam-1"), cookies=cookies)
    assert listing.json()["can_edit"] is True

    created = http_client.post(_rules_url("cam-1"), json=ZONE, cookies=cookies)
    assert created.status_code == 200
    rule_id = created.json()["id"]

    updated = http_client.put(_rules_url("cam-1", rule_id), json={"enabled": False}, cookies=cookies)
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False

    deleted = http_client.delete(_rules_url("cam-1", rule_id), cookies=cookies)
    assert deleted.status_code == 200


def test_viewer_cannot_grant_themselves_write_by_omitting_can_settings_from_a_can_live_grant(http_client, db_path):
    """can_live/can_playback grant visibility only -- can_settings is the
    one, separate gate for mutating a camera's rules, exactly like every
    other per-camera write permission in this codebase (see
    door_access.py's own can_unlock split)."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    _seed_viewer(conn, "user-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="user-1", camera_id="cam-1", can_live=1, can_playback=1, can_settings=0)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")}
    response = http_client.post(_rules_url("cam-1"), json=LINE, cookies=cookies)
    assert response.status_code == 403


def test_the_analytics_rules_page_renders_for_an_authorized_owner(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Driveway")
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    response = http_client.get("/customer/cameras/cam-1/analytics-rules", cookies=cookies)
    assert response.status_code == 200
    assert "Driveway" in response.text
    assert "edge worker" in response.text  # the explicit no-execution-yet disclosure


def test_the_analytics_rules_page_redirects_a_non_customer_role(http_client, db_path):
    response = http_client.get("/customer/cameras/cam-1/analytics-rules")
    assert response.status_code == 303


def test_the_analytics_rules_page_never_claims_live_detection_is_active(http_client, db_path):
    """Companion to the 'edge worker' disclosure check above -- guards
    against a future edit accidentally adding UI copy (a status pill,
    a word like 'Active'/'Detecting'/'Monitoring') that would imply
    this feature evaluates anything, which it does not (see the
    runtime-path trace in docs/intrusion-line-crossing-customer-ui-gap.md)."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    html = http_client.get("/customer/cameras/cam-1/analytics-rules", cookies=cookies).text
    for forbidden in ("Detecting", "Monitoring", "Active now", "is watching"):
        assert forbidden not in html


# ------------------------------------------------------------- runtime-path boundary
#
# The rest of this file locks in, as executable regression tests rather
# than only prose, the exact boundary documented in
# docs/intrusion-line-crossing-customer-ui-gap.md's "Update, 2026-09-21"
# section: saving a rule through this feature persists geometry only --
# it must never produce a side effect that looks like a real detection
# (a local analytics-events-file append, a detection_events row, or a
# reference from any existing worker/sync module). If a future change
# makes any of these fail, that change has started faking execution and
# needs its own real edge-worker implementation plus an update to that
# doc, not a quiet pass here.


def test_creating_updating_and_deleting_a_rule_never_writes_the_local_analytics_events_file(http_client, db_path, monkeypatch, tmp_path):
    import main

    fake_events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", fake_events_file)

    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}

    rule_id = http_client.post(_rules_url("cam-1"), json=LINE, cookies=cookies).json()["id"]
    http_client.put(_rules_url("cam-1", rule_id), json={"enabled": False}, cookies=cookies)
    http_client.delete(_rules_url("cam-1", rule_id), cookies=cookies)

    assert not fake_events_file.exists()


def test_creating_and_updating_a_rule_never_creates_a_detection_events_row(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}

    rule_id = http_client.post(_rules_url("cam-1"), json=ZONE, cookies=cookies).json()["id"]
    http_client.put(_rules_url("cam-1", rule_id), json={"enabled": False}, cookies=cookies)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM detection_events").fetchone()[0]
    conn.close()
    assert count == 0


def test_no_existing_worker_or_sync_module_reads_the_new_rules_table_yet(monkeypatch):
    """This is the concrete, checkable form of 'no execution path exists
    yet' -- see the runtime-path trace in
    docs/intrusion-line-crossing-customer-ui-gap.md. Reference to the
    table name would show up here the moment someone starts wiring a
    real consumer -- at which point this test (and that doc) need a
    deliberate update, not a silent pass, per the explicit instruction
    not to imply this feature is operational."""
    import inspect

    import analytics_sync
    import appliance_cloud
    import event_media_uploader
    import main

    assert "customer_analytics_rules" not in inspect.getsource(main.people_counting_worker)
    assert "customer_analytics_rules" not in inspect.getsource(analytics_sync)
    assert "customer_analytics_rules" not in inspect.getsource(appliance_cloud)
    assert "customer_analytics_rules" not in inspect.getsource(event_media_uploader)
