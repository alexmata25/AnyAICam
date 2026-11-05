"""Windows 1.3.4.1: running-camera limits at every sold tier.

The 8-camera path was the only one proven. Enforcement is generic, so this closes a coverage gap and is not a fix for
a known defect. Uses the real signed path and the real provisioning routes from the two suites it extends.
"""
import pytest

import appliance_cloud
import vms_capacity
from test_appliance_feature_entitlements import (  # noqa: F401 -- fixtures
    appliance, cloud_client, cloud_db, edge_db, edge_env)
from test_camera_slot_enforcement import (  # noqa: F401 -- fixtures and helpers
    _appliance_headers, _grant_slots, _provision_request, _seed_and_activate_appliance, _seed_tenant, client, db_path)
from test_signed_camera_capacity import _sync, licensed  # noqa: F401

TIERS = [8, 16, 32, 64]


def provisioned(count):
    return list(range(1, count + 1))


def forget_request_rates():
    # A 64-camera install in one test passes the per-minute appliance limit, which has its own suite.
    for value in vars(appliance_cloud).values():
        if hasattr(getattr(value, "events", None), "clear"):
            value.events.clear()


@pytest.mark.real_entitlements
@pytest.mark.parametrize("tier", TIERS)
def test_a_signed_tier_runs_its_last_camera_and_refuses_the_next(licensed, edge_db, tier):
    licensed["n"] = tier
    _sync(edge_db)
    assert vms_capacity.load_capacity()["camera_slot_quantity"] == tier
    assert vms_capacity.licensed_camera_numbers(provisioned(tier + 1)) == provisioned(tier)
    assert vms_capacity.camera_licensed(tier, provisioned(tier + 1))
    assert not vms_capacity.camera_licensed(tier + 1, provisioned(tier + 1))


@pytest.mark.real_entitlements
def test_moving_down_a_tier_keeps_the_lowest_numbered_cameras_and_moving_back_restores_them(licensed, edge_db):
    for tier, expected in ((32, 32), (16, 16), (8, 8), (64, 40)):
        licensed["n"] = tier
        _sync(edge_db)
        assert vms_capacity.licensed_camera_numbers(provisioned(40)) == provisioned(expected)


@pytest.mark.real_entitlements
def test_resetting_the_local_identity_stops_cameras_with_a_reason_and_keeps_the_accepted_grant(licensed, edge_db, monkeypatch):
    licensed["n"] = 16
    _sync(edge_db)
    kept = vms_capacity.ACCEPTED_FILE.read_bytes()
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: None)  # what the reset route leaves
    status = vms_capacity.capacity_status()
    assert status["state"] == "fail_closed" and status["reason"] == "identity_mismatch"
    assert vms_capacity.licensed_camera_numbers(provisioned(16)) == []
    assert vms_capacity.ACCEPTED_FILE.read_bytes() == kept


@pytest.mark.parametrize("tier", [16, 32, 64])
def test_the_cloud_request_gate_allows_the_last_camera_of_a_tier_and_refuses_the_next(client, db_path, tier):
    _seed_tenant(db_path)
    _grant_slots(db_path, "cust-1", tier)
    token = _seed_and_activate_appliance(db_path)
    activated = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    assert activated.status_code == 200
    credential = activated.json()["credential"]
    for number in provisioned(tier):
        forget_request_rates()
        response = _provision_request(client, appliance_id="appl-1", device_key=f"dev-{number}", name=f"Camera {number}")
        assert response.status_code == 200, f"camera {number}: {response.text}"
        confirmed = client.post(f"/api/appliance/AIC-SLOT1/provisioning-jobs/{response.json()['job_id']}",
                                json={"success": True, "message": "Verified."},
                                headers=_appliance_headers("appl-1", credential))
        assert confirmed.status_code == 200, f"camera {number}: {confirmed.text}"
    assert _provision_request(client, appliance_id="appl-1", device_key="dev-next", name="Next").status_code == 403
