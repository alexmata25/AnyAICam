"""Multi-appliance isolation defect (2026-09-12): a customer who owns
more than one appliance saw a different appliance's cameras counted,
listed, and displayed as if they belonged to whichever appliance was
currently selected in the setup wizard.

Confirmed live on anyaicam-staging: a customer had an old historical
appliance (5 seeded placeholder camera rows, non-null but synthetic
device_keys predating the discovery/provisioning flow) and a newly-
claimed Ryzen appliance (zero real cameras). Selecting Ryzen in the
customer setup wizard showed "5 of 8 cameras configured" -- every one
of those 5 rows actually belonged to the old appliance. Root cause:
GET /api/customer/cameras and the wizard's own Step 5 camera table both
queried `WHERE customer_id=?` with no appliance_id filter at all.

This file proves the fix across every customer-facing surface that
touches cameras for a specific appliance: listing/counting, the wizard
page itself, saving camera edits, discovery-based provisioning, and
Live View -- plus that a customer can never use another customer's
appliance_id anywhere in this surface. Uses the same TestClient +
override_target pattern already established in test_camera_slot_
checkout.py.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_multi_appliance_isolation.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _owner_cookie(customer_id="cust-1", email="owner@example.test"):
    import partner_portal
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _seed_world(db_path):
    """cust-1 owns two appliances: `appl-ryzen` (one real camera, real
    ONVIF-shaped device_key) and `appl-old` (5 historical placeholder
    cameras, synthetic device_keys -- matching exactly the live incident
    this fix addresses). cust-2 owns a third, unrelated appliance/camera
    for cross-customer rejection tests."""
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Partner','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Cust One','owner@example.test','active','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-1','Cust Two','other@example.test','active','2026-01-01')")
        # live_view_sessions._authorized_camera() looks up a partner_users
        # row by email (in addition to the signed session identity) --
        # seed the real login record a customer_owner would actually have.
        conn.execute(
            "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
            "VALUES('user-1','partner-1','owner@example.test','Owner','customer_owner','x',1,'cust-1','2026-01-01')"
        )
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Ryzen Home Site','2026-01-01')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Other Site','2026-01-01')")
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,created_at) "
            "VALUES('appl-ryzen','cust-1','site-1','637AD320-DAAA-436E-89C9-70A84F4F54A9','pending','online','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,created_at) "
            "VALUES('appl-old','cust-1','site-1','AIC-C90CF0C9','pending','degraded','2025-01-01')"
        )
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,created_at) "
            "VALUES('appl-other','cust-2','site-2','AIC-OTHER0','pending','online','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,resolution,created_at,device_key,camera_number) "
            "VALUES('cam-ryzen-1','cust-1','site-1','appl-ryzen','Ryzen Camera 1','configured','4mp','2026-02-01','urn:uuid:real-device-1',1)"
        )
        for n in range(1, 6):
            conn.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,resolution,created_at,device_key) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f"cam-old-{n}", "cust-1", "site-1", "appl-old", f"Camera {n}", "configured", "4mp", "2025-01-01", f"appl-old-camera-{n}"),
            )
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,resolution,created_at,device_key) "
            "VALUES('cam-other-1','cust-2','site-2','appl-other','Other Camera','configured','4mp','2026-01-01','urn:uuid:other-device-1')"
        )
        conn.execute(
            "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
            "VALUES('ent-1','cust-1','camera_slots_local',8,'active','2026-01-01','2026-01-01')"
        )
        conn.commit()


# ------------------------------------------------------- listing/counting


def test_camera_list_scoped_to_selected_appliance_excludes_other_appliance(client, db_path):
    _seed_world(db_path)
    cookie = _owner_cookie()

    ryzen = client.get("/api/customer/cameras", params={"appliance_id": "appl-ryzen"}, cookies={"anyaicam_partner_session": cookie})
    assert ryzen.status_code == 200, ryzen.text
    ryzen_ids = {c["id"] for c in ryzen.json()["cameras"]}
    assert ryzen_ids == {"cam-ryzen-1"}

    old = client.get("/api/customer/cameras", params={"appliance_id": "appl-old"}, cookies={"anyaicam_partner_session": cookie})
    assert old.status_code == 200, old.text
    old_ids = {c["id"] for c in old.json()["cameras"]}
    assert old_ids == {f"cam-old-{n}" for n in range(1, 6)}
    assert "cam-ryzen-1" not in old_ids


def test_camera_list_without_appliance_id_is_account_wide_backward_compat(client, db_path):
    _seed_world(db_path)
    response = client.get("/api/customer/cameras", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 200
    ids = {c["id"] for c in response.json()["cameras"]}
    assert ids == {"cam-ryzen-1"} | {f"cam-old-{n}" for n in range(1, 6)}


def test_configured_count_is_per_appliance_expected_slots_stay_account_wide(client, db_path):
    """The exact live incident: selecting Ryzen must show 0 configured
    (it has one real camera seeded here, so 1) not 5, while the shared
    8-slot Stripe entitlement is correctly reported for both."""
    _seed_world(db_path)
    cookie = _owner_cookie()

    ryzen = client.get("/api/customer/cameras", params={"appliance_id": "appl-ryzen"}, cookies={"anyaicam_partner_session": cookie}).json()
    assert ryzen["configured_camera_count"] == 1
    assert ryzen["expected_camera_count"] == 8

    old = client.get("/api/customer/cameras", params={"appliance_id": "appl-old"}, cookies={"anyaicam_partner_session": cookie}).json()
    assert old["configured_camera_count"] == 5
    assert old["expected_camera_count"] == 8


def test_cross_customer_appliance_id_is_rejected_not_leaked(client, db_path):
    _seed_world(db_path)
    response = client.get("/api/customer/cameras", params={"appliance_id": "appl-other"}, cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 404
    assert "cam-other-1" not in response.text


# --------------------------------------------------------------- the wizard page


def test_setup_wizard_shows_only_the_selected_appliances_camera_names(client, db_path):
    _seed_world(db_path)
    conn = sqlite3.connect(db_path)
    with override_target(sqlite_path=str(db_path)):
        conn.execute(
            "INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES('cust-1',5,?,'2026-01-01')",
            ('{"appliance_id": "appl-ryzen", "scan_job": null}',),
        )
        conn.commit()

    page = client.get("/customer/setup", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert page.status_code == 200
    assert "Ryzen Camera 1" in page.text
    assert "value=\"Camera 1\"" not in page.text  # the old appliance's own "Camera 1" name must not appear

    with override_target(sqlite_path=str(db_path)):
        conn2 = sqlite3.connect(db_path)
        conn2.execute("UPDATE customer_setup_drafts SET data_json=? WHERE customer_id='cust-1'", ('{"appliance_id": "appl-old", "scan_job": null}',))
        conn2.commit()

    page2 = client.get("/customer/setup", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert page2.status_code == 200
    assert "Ryzen Camera 1" not in page2.text
    assert "value=\"Camera 1\"" in page2.text


# ------------------------------------------------------------- saving edits


def test_put_cameras_cannot_modify_a_different_appliances_camera(client, db_path):
    _seed_world(db_path)
    response = client.put(
        "/api/customer/cameras",
        json={"appliance_id": "appl-ryzen", "cameras": [{"id": "cam-old-1", "name": "Tampered Name", "site_id": "site-1", "resolution": "4mp", "status": "configured"}]},
        cookies={"anyaicam_partner_session": _owner_cookie()},
    )
    assert response.status_code == 200  # the endpoint never errors on a filtered-out item, it just skips it

    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        name = con.execute("SELECT name FROM cameras WHERE id='cam-old-1'").fetchone()[0]
    assert name == "Camera 1", "a camera belonging to a different appliance must never be touched"


def test_put_cameras_updates_the_matching_appliances_own_camera(client, db_path):
    _seed_world(db_path)
    response = client.put(
        "/api/customer/cameras",
        json={"appliance_id": "appl-ryzen", "cameras": [{"id": "cam-ryzen-1", "name": "Front Door", "site_id": "site-1", "resolution": "4mp", "status": "configured"}]},
        cookies={"anyaicam_partner_session": _owner_cookie()},
    )
    assert response.status_code == 200, response.text
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        name = con.execute("SELECT name FROM cameras WHERE id='cam-ryzen-1'").fetchone()[0]
    assert name == "Front Door"


def test_put_cameras_without_appliance_id_preserves_legacy_customer_scoped_behavior(client, db_path):
    """Backward compatibility: omitting appliance_id must still work
    exactly as before this fix for any caller that has no appliance
    context at all."""
    _seed_world(db_path)
    response = client.put(
        "/api/customer/cameras",
        json={"cameras": [{"id": "cam-old-2", "name": "Renamed Legacy", "site_id": "site-1", "resolution": "4mp", "status": "configured"}]},
        cookies={"anyaicam_partner_session": _owner_cookie()},
    )
    assert response.status_code == 200, response.text
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        name = con.execute("SELECT name FROM cameras WHERE id='cam-old-2'").fetchone()[0]
    assert name == "Renamed Legacy"


# --------------------------------------------------------------- provisioning


def test_provisioning_targets_the_explicit_appliance_and_site(client, db_path, monkeypatch):
    _seed_world(db_path)
    import main
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "")  # no credentials submitted -> key not required
    response = client.post(
        "/api/customer/cameras/provision",
        json={"appliance_id": "appl-ryzen", "device_key": "urn:uuid:new-real-device-2", "name": "Back Yard"},
        cookies={"anyaicam_partner_session": _owner_cookie()},
    )
    assert response.status_code == 200, response.text
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        req = con.execute("SELECT appliance_id,site_id,device_key FROM camera_provisioning_requests WHERE device_key=?", ("urn:uuid:new-real-device-2",)).fetchone()
    assert req is not None
    assert req["appliance_id"] == "appl-ryzen"
    assert req["site_id"] == "site-1"


def test_provisioning_rejects_an_appliance_id_belonging_to_another_customer(client, db_path):
    _seed_world(db_path)
    response = client.post(
        "/api/customer/cameras/provision",
        json={"appliance_id": "appl-other", "device_key": "urn:uuid:attacker-attempt", "name": "Hijack"},
        cookies={"anyaicam_partner_session": _owner_cookie()},
    )
    assert response.status_code == 404
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        count = con.execute("SELECT COUNT(*) FROM camera_provisioning_requests WHERE device_key=?", ("urn:uuid:attacker-attempt",)).fetchone()[0]
    assert count == 0


def test_scan_endpoint_rejects_an_appliance_id_belonging_to_another_customer(client, db_path):
    """Locks in already-correct behavior confirmed during this audit --
    regression guard against it ever regressing."""
    _seed_world(db_path)
    response = client.post("/api/customer/appliances/appl-other/scan", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 404


# ----------------------------------------------------------------- Live View


def test_live_view_start_queues_a_command_to_the_cameras_own_appliance_only(client, db_path):
    _seed_world(db_path)
    response = client.post("/api/customer/cameras/cam-ryzen-1/live/start", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 200, response.text
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        commands = [dict(r) for r in con.execute("SELECT appliance_id,command FROM appliance_commands WHERE command='start_live_relay'")]
    assert len(commands) == 1
    assert commands[0]["appliance_id"] == "appl-ryzen"
    assert all(c["appliance_id"] != "appl-old" for c in commands)


def test_live_view_start_rejects_a_camera_belonging_to_another_customer(client, db_path):
    _seed_world(db_path)
    response = client.post("/api/customer/cameras/cam-other-1/live/start", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 404


def test_live_view_start_rejects_a_camera_id_that_does_not_exist(client, db_path):
    _seed_world(db_path)
    response = client.post("/api/customer/cameras/does-not-exist/live/start", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 404


def _activate(db_path, appliance_id: str) -> None:
    """/customer-account and /customer-live (unlike the API routes above)
    gate on at least one activation_status='activated' appliance existing
    for the customer -- _seed_world() leaves both test appliances 'pending'
    (matching cust-1's real appl-ryzen state), so these two page-level
    tests need the historical appliance activated first, matching real
    production data (AIC-C90CF0C9 is activation_status='activated' on
    anyaicam-staging; only the newer Ryzen claim is still 'pending')."""
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE appliances SET activation_status='activated' WHERE id=?", (appliance_id,))
        conn.commit()


# ---------------------------------------------------------- /customer-account
#
# Same defect class, a call site 0a92bea's audit missed: this dashboard's own
# `all_customer_cameras` query was `WHERE customer_id=?` with no appliance
# filter at all. Confirmed live on anyaicam-staging: a customer with two
# appliances saw both rendered as one undifferentiated grid, with the other
# appliance's "Camera 1"/"Camera 2"/"Camera 3" indistinguishable from this
# appliance's own. This page has no appliance-selector control of its own
# (no dropdown, no persisted "current appliance"), so an explicit
# appliance_id query param is verified/scoped exactly like GET
# /api/customer/cameras, and the no-appliance_id default groups by appliance
# instead of merging.


def test_customer_account_scoped_to_selected_appliance_excludes_other_appliance(client, db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-old")
    cookie = {"anyaicam_partner_session": _owner_cookie()}

    ryzen = client.get("/customer-account", params={"appliance_id": "appl-ryzen"}, cookies=cookie)
    assert ryzen.status_code == 200, ryzen.text
    assert "Ryzen Camera 1" in ryzen.text
    for n in range(1, 6):
        assert f">Camera {n}<" not in ryzen.text
    assert "1 of 8 camera" in ryzen.text

    old = client.get("/customer-account", params={"appliance_id": "appl-old"}, cookies=cookie)
    assert old.status_code == 200, old.text
    assert "Ryzen Camera 1" not in old.text
    for n in range(1, 6):
        assert f">Camera {n}<" in old.text
    assert "5 of 8 camera" in old.text


def test_customer_account_cross_customer_appliance_id_is_rejected_not_leaked(client, db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-old")
    response = client.get(
        "/customer-account", params={"appliance_id": "appl-other"},
        cookies={"anyaicam_partner_session": _owner_cookie()},
    )
    assert response.status_code == 404
    assert "Other Camera" not in response.text


def test_customer_account_without_appliance_id_groups_by_appliance_never_merges(client, db_path):
    """The exact live incident, reproduced end-to-end: with no selector,
    both appliances' real cameras must still both appear (nothing hidden,
    nothing silently chosen), but grouped under their own appliance
    heading rather than tiled together indistinguishably."""
    _seed_world(db_path)
    _activate(db_path, "appl-old")
    response = client.get("/customer-account", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 200, response.text
    assert "Ryzen Camera 1" in response.text
    for n in range(1, 6):
        assert f">Camera {n}<" in response.text
    # Both appliances' own cloud_id headings must appear, clearly attributing
    # each group -- proof the two appliances' cameras were never merged into
    # one indistinguishable list.
    assert "637AD320-DAAA-436E-89C9-70A84F4F54A9" in response.text
    assert "AIC-C90CF0C9" in response.text
    # The account-wide entitlement total is unaffected by grouping.
    assert "6 of 8 camera" in response.text


# ------------------------------------------------------------- /customer-live


def test_customer_live_scoped_to_selected_appliance_excludes_other_appliance(client, db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-old")
    cookie = {"anyaicam_partner_session": _owner_cookie()}

    ryzen = client.get("/customer-live", params={"appliance_id": "appl-ryzen"}, cookies=cookie)
    assert ryzen.status_code == 200, ryzen.text
    assert "Ryzen Camera 1" in ryzen.text
    for n in range(1, 6):
        assert f">Camera {n}<" not in ryzen.text

    old = client.get("/customer-live", params={"appliance_id": "appl-old"}, cookies=cookie)
    assert old.status_code == 200, old.text
    assert "Ryzen Camera 1" not in old.text
    for n in range(1, 6):
        assert f">Camera {n}<" in old.text


def test_customer_live_cross_customer_appliance_id_is_rejected_not_leaked(client, db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-old")
    response = client.get(
        "/customer-live", params={"appliance_id": "appl-other"},
        cookies={"anyaicam_partner_session": _owner_cookie()},
    )
    assert response.status_code == 404
    assert "Other Camera" not in response.text


def test_customer_live_without_appliance_id_groups_tiles_by_appliance_never_merges(client, db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-old")
    response = client.get("/customer-live", cookies={"anyaicam_partner_session": _owner_cookie()})
    assert response.status_code == 200, response.text
    assert "Ryzen Camera 1" in response.text
    for n in range(1, 6):
        assert f">Camera {n}<" in response.text
    assert "637AD320-DAAA-436E-89C9-70A84F4F54A9" in response.text
    assert "AIC-C90CF0C9" in response.text
