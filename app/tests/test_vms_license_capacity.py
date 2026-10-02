"""VMS license at provisioning and operation, never at claim (2026-10-01).

Owner decision: any customer may securely claim an appliance; the VMS
software license is enforced when cameras are provisioned and when the
appliance runs them, so a copied installer cannot produce an unlicensed
usable VMS. Usable capacity = min(camera plan, VMS license); an appliance
purchase includes a license matching the plan."""
import json
import sqlite3

import pytest

from database_backend import override_target
from test_camera_slot_enforcement import (  # noqa: F401 -- fixtures and helpers
    _appliance_headers,
    _provision_request,
    _seed_and_activate_appliance,
    _seed_tenant,
    client,
    db_path,
)


def _entitle(db_path, customer_id="cust-1", *, plan=0, license=0, appliance_order=False):
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        if plan:
            ce.upsert_entitlement(customer_id=customer_id, product="camera_slots_local", camera_slot_quantity=plan, status="active")
        if license:
            ce.upsert_entitlement(customer_id=customer_id, product=ce.VMS_LICENSE_PRODUCT, camera_slot_quantity=license, status="active")
        if appliance_order:
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,amount_cents,status,created_at,updated_at) "
                         "VALUES('ord-1',?,?,?,?,?,?,?,?)",
                         (customer_id, "AIC-APPLIANCE-8", "AnyAiCam appliance (8 cameras)", "price_test", 49900, "paid", "2026-01-01", "2026-01-01"))
            conn.commit()
            conn.close()


def _activated(client, db_path):
    token = _seed_and_activate_appliance(db_path)
    response = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    assert response.status_code == 200, response.text  # activation itself is never license-gated
    return response.json()["credential"]


# ------------------------------------------------------------------ the rule

@pytest.mark.parametrize("plan,license,order,expected", [
    (8, 0, False, 0),      # copied installer: camera plan but no software license
    (0, 8, False, 0),      # license but no camera plan
    (8, 4, False, 4),      # DIY: the smaller of the two
    (4, 8, False, 4),
    (8, 0, True, 8),       # appliance purchase includes a license matching the plan
    (0, 0, False, 0),
])
def test_usable_capacity_is_the_smaller_of_plan_and_license(client, db_path, plan, license, order, expected):
    _seed_tenant(db_path)
    _entitle(db_path, plan=plan, license=license, appliance_order=order)
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        assert ce.usable_camera_capacity("cust-1") == expected
        breakdown = ce.capacity_breakdown("cust-1")
        assert breakdown["camera_slot_quantity"] == expected and breakdown["plan_camera_slots"] == plan


# ------------------------------------------------------------------ claim is open, provisioning is not

def test_an_unlicensed_customer_activates_but_cannot_provision_a_camera(client, db_path):
    _seed_tenant(db_path)
    _entitle(db_path, plan=8)  # a camera plan, no VMS license
    credential = _activated(client, db_path)
    refused = _provision_request(client, appliance_id="appl-1", device_key="dev-1")
    assert refused.status_code == 403 and "VMS software license is required" in refused.json()["detail"]
    refresh = client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", credential))
    assert refresh.status_code == 200
    assert refresh.json()["camera_slot_quantity"] == 0 and refresh.json()["vms_license_required"] is True


def test_the_appliance_confirmation_gate_also_enforces_the_license(client, db_path):
    """A job queued while licensed is refused at confirmation once the
    license no longer covers it."""
    _seed_tenant(db_path)
    _entitle(db_path, plan=8, license=8)
    credential = _activated(client, db_path)
    job = _provision_request(client, appliance_id="appl-1", device_key="dev-1").json()["job_id"]
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        ce.upsert_entitlement(customer_id="cust-1", product=ce.VMS_LICENSE_PRODUCT, camera_slot_quantity=0, status="cancelled")
    confirm = client.post(f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job}", json={"success": True, "message": "Verified."},
                          headers=_appliance_headers("appl-1", credential))
    assert confirm.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM cameras WHERE customer_id='cust-1' AND device_key='dev-1'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM camera_provisioning_requests WHERE id=?", (job,)).fetchone()[0] == "failed"


def test_a_diy_license_smaller_than_the_plan_limits_cameras(client, db_path):
    _seed_tenant(db_path)
    _entitle(db_path, plan=8, license=2)
    credential = _activated(client, db_path)
    for n in (1, 2):
        job = _provision_request(client, appliance_id="appl-1", device_key=f"dev-{n}").json()["job_id"]
        client.post(f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job}", json={"success": True, "message": "ok"},
                    headers=_appliance_headers("appl-1", credential))
    third = _provision_request(client, appliance_id="appl-1", device_key="dev-3")
    assert third.status_code == 403 and "licensed for 2 camera" in third.json()["detail"]


def test_the_appliance_configuration_carries_the_usable_capacity(client, db_path):
    _seed_tenant(db_path)
    _entitle(db_path, plan=8, license=4)
    credential = _activated(client, db_path)
    config = client.get("/api/appliance/configuration", headers=_appliance_headers("appl-1", credential))
    assert config.status_code == 200
    assert config.json()["usable_camera_capacity"]["camera_slot_quantity"] == 4


# ------------------------------------------------------------------ the appliance runs only what is licensed

@pytest.fixture()
def edge(tmp_path, monkeypatch):
    import vms_capacity
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    monkeypatch.setattr(vms_capacity, "STATE_FILE", tmp_path / "vms_capacity.json")
    state = {"activated": True}
    monkeypatch.setattr(vms_capacity, "_activated", lambda: state["activated"])
    return vms_capacity, state


def test_a_never_claimed_copied_install_runs_no_camera(edge, monkeypatch):
    vms_capacity, state = edge
    state["activated"] = False
    assert vms_capacity.licensed_camera_numbers([1, 2, 3, 4]) == []
    import main
    monkeypatch.setattr(main, "get_camera_numbers", lambda customer_id=None: [1, 2, 3, 4])
    monkeypatch.setenv("CAMERA1_HOST", "192.0.2.10")  # legacy env-configured camera on a copied install
    with pytest.raises(main.CameraNotLicensedError):
        main.camera_url(1)


def test_a_synced_capacity_limits_which_cameras_run(edge, monkeypatch):
    vms_capacity, _ = edge
    assert vms_capacity.persist_capacity({"camera_slot_quantity": 2, "plan_camera_slots": 8, "vms_license_capacity": 2}) is True
    assert vms_capacity.licensed_camera_numbers([3, 1, 2]) == [1, 2]
    import main
    monkeypatch.setattr(main, "get_camera_numbers", lambda customer_id=None: [1, 2, 3])
    monkeypatch.setenv("CAMERA3_HOST", "192.0.2.13")
    with pytest.raises(main.CameraNotLicensedError):
        main.camera_url(3)
    # A licensed camera still resolves normally.
    monkeypatch.setenv("CAMERA1_HOST", "192.0.2.11")
    assert "192.0.2.11" in main.camera_url(1)
    assert vms_capacity.persist_capacity({"camera_slot_quantity": 0}) is True
    assert vms_capacity.licensed_camera_numbers([1, 2, 3]) == []


def test_an_activated_appliance_runs_nothing_before_its_first_authoritative_sync(edge, monkeypatch):
    """Fail closed (2026-10-02, Codex): before the cloud has said what this
    appliance may run, nothing runs -- legacy CAMERA<n>_* settings included."""
    vms_capacity, state = edge
    state["activated"] = True
    assert vms_capacity.licensed_camera_numbers([1, 2, 3]) == []
    import main
    monkeypatch.setattr(main, "get_camera_numbers", lambda customer_id=None: [1, 2])
    for n in (1, 2):
        monkeypatch.setenv(f"CAMERA{n}_HOST", f"192.0.2.{n}")
        monkeypatch.setenv(f"CAMERA{n}_USERNAME", "admin")
        monkeypatch.setenv(f"CAMERA{n}_PASSWORD", "secret")
        with pytest.raises(main.CameraNotLicensedError):
            main.camera_url(n)


@pytest.mark.parametrize("capacity,expected", [(0, []), (1, [1]), (2, [1, 2]), (8, [1, 2, 3])])
def test_authoritative_capacity_allows_exactly_n(edge, capacity, expected):
    vms_capacity, _ = edge
    vms_capacity.persist_capacity({"camera_slot_quantity": capacity})
    assert vms_capacity.licensed_camera_numbers([3, 1, 2]) == expected


def test_a_later_valid_sync_enables_the_licensed_cameras(edge, monkeypatch):
    vms_capacity, state = edge
    state["activated"] = True
    import main
    monkeypatch.setattr(main, "get_camera_numbers", lambda customer_id=None: [1, 2])
    monkeypatch.setenv("CAMERA1_HOST", "192.0.2.21")
    monkeypatch.setenv("CAMERA2_HOST", "192.0.2.22")
    with pytest.raises(main.CameraNotLicensedError):
        main.camera_url(1)
    assert vms_capacity.persist_capacity({"camera_slot_quantity": 1}) is True  # first configuration sync
    assert "192.0.2.21" in main.camera_url(1)
    with pytest.raises(main.CameraNotLicensedError):
        main.camera_url(2)


def test_claiming_never_depends_on_capacity(client, db_path):
    """Registration/activation stays independent of the licence and of
    capacity (the owner's decision): an unlicensed customer still activates."""
    _seed_tenant(db_path)
    _entitle(db_path)  # no plan, no licence
    assert _activated(client, db_path)


def test_an_older_cloud_without_capacity_changes_nothing(edge):
    vms_capacity, _ = edge
    assert vms_capacity.persist_capacity(None) is False
    assert vms_capacity.persist_capacity({"product_mode": "local"}) is False
    assert vms_capacity.load_capacity() is None


def test_the_cloud_never_restricts_itself(edge, monkeypatch):
    vms_capacity, state = edge
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    state["activated"] = False
    assert vms_capacity.licensed_camera_numbers([1]) is None


def test_configuration_sync_stores_the_capacity(edge, monkeypatch):
    vms_capacity, _ = edge
    import edge_camera_sync
    vms_capacity.persist_capacity({"camera_slot_quantity": 5, "plan_camera_slots": 8, "vms_license_capacity": 5})
    assert json.loads(vms_capacity.STATE_FILE.read_text())["camera_slot_quantity"] == 5
    source = open(edge_camera_sync.__file__, encoding="utf-8").read()
    assert 'vms_capacity.persist_capacity(response.get("usable_camera_capacity"))' in source
