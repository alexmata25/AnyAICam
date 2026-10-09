"""Cameras tab: add and remove cameras within the per-camera plan (2026-10-08).

The complete workflow through the real routes: an AI Local plan for 2
cameras -> two cameras added (customer request + the appliance's confirmed
provisioning) -> a third is refused with an instruction to increase the
plan's camera quantity -> one camera removed (its place freed, the plan and
billing untouched, no Stripe call, the appliance stops running it) -> a new
camera fits again. GET /api/customer/cameras and the add/remove answers
carry camera_capacity so the Cameras tab can update at once. Data safety of
removal (camera row, recordings, events kept) is test_customer_camera_removal.py.
Fixtures and helpers: test_camera_slot_enforcement.py."""
import sqlite3

import pytest

from database_backend import override_target
from test_camera_slot_enforcement import (  # noqa: F401  (pytest fixtures)
    _appliance_headers, _owner_cookie, _provision_request, _seed_and_activate_appliance, _seed_tenant,
    _session_cookie_name, client, db_path,
)

PLAN_FULL = "increase the number of cameras on your plan in My subscription"


def _per_camera_plan(db_path, quantity, plan_key="ai_local", status="active"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
            "stripe_subscription_item_id,stripe_price_id,status,stripe_status,local_retention_days,created_at,updated_at) "
            "VALUES('v2-1','cust-1',?,?,'cus_test','sub_test','si_test','price_test',?,'active',7,'2026-10-08','2026-10-08')",
            (plan_key, quantity, status))
        conn.commit()


def _activated(client, db_path):
    token = _seed_and_activate_appliance(db_path)
    response = client.post("/api/appliance/activate", json={"cloud_id": "AIC-SLOT1", "activation_token": token})
    assert response.status_code == 200
    return response.json()["credential"]


def _add(client, credential, device_key):
    response = _provision_request(client, appliance_id="appl-1", device_key=device_key, name=device_key)
    if response.status_code != 200:
        return response
    job_id = response.json()["job_id"]
    confirm = client.post(f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id}", json={"success": True, "message": "Verified."},
                          headers=_appliance_headers("appl-1", credential))
    assert confirm.status_code == 200, confirm.text
    return response


def _cameras(client):
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    response = client.get("/api/customer/cameras")
    assert response.status_code == 200
    return response.json()


def _remove(client, camera_id):
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    return client.delete(f"/api/customer/cameras/{camera_id}")


def _camera_id(db_path, device_key):
    with override_target(sqlite_path=str(db_path)):
        return sqlite3.connect(db_path).execute("SELECT id FROM cameras WHERE device_key=?", (device_key,)).fetchone()[0]


def test_add_up_to_the_plan_then_refuse_then_remove_frees_a_place(client, db_path, monkeypatch):
    import main
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 2)
    credential = _activated(client, db_path)
    assert _cameras(client)["camera_capacity"] == {"usable": 2, "in_use": 0, "pending": 0, "available": 2,
                                                   "can_add_camera": True, "can_manage": True, "message": None,
                                                   "code": None, "action_url": "/subscription-portal"}

    first = _add(client, credential, "dev-1")
    assert first.status_code == 200 and first.json()["camera_capacity"]["pending"] == 1  # the request itself holds the place
    assert _add(client, credential, "dev-2").status_code == 200
    listed = _cameras(client)
    assert {c["device_key"] for c in listed["cameras"]} == {"dev-1", "dev-2"}
    assert listed["camera_capacity"]["can_add_camera"] is False and PLAN_FULL in listed["camera_capacity"]["message"]
    assert listed["camera_capacity"]["code"] == "camera_limit_reached"

    third = _add(client, credential, "dev-3")
    assert third.status_code == 403
    assert "licensed for 2 camera" in third.json()["detail"] and PLAN_FULL in third.json()["detail"]
    assert third.json()["code"] == "camera_limit_reached"

    # Removal never touches billing: no Stripe call, the plan stays as bought.
    monkeypatch.setattr(main, "stripe_api_post", lambda *a, **k: pytest.fail("removing a camera must not call Stripe"))
    removed = _remove(client, _camera_id(db_path, "dev-1"))
    assert removed.status_code == 200
    assert removed.json()["camera_capacity"] == {"usable": 2, "in_use": 1, "pending": 0, "available": 1,
                                                 "can_add_camera": True, "can_manage": True, "message": None,
                                                 "code": None, "action_url": "/subscription-portal"}
    with override_target(sqlite_path=str(db_path)):
        plan = sqlite3.connect(db_path).execute("SELECT camera_quantity,status FROM camera_plan_entitlements_v2 WHERE customer_id='cust-1'").fetchone()
    assert plan == (2, "active")
    assert [c["device_key"] for c in _cameras(client)["cameras"]] == ["dev-2"]  # gone from the list at once

    assert _add(client, credential, "dev-3").status_code == 200  # the freed place is usable again
    assert _cameras(client)["camera_capacity"]["can_add_camera"] is False


def test_a_removed_camera_is_no_longer_run_by_the_appliance(client, db_path):
    import main
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 2)
    credential = _activated(client, db_path)
    _add(client, credential, "dev-1")
    _add(client, credential, "dev-2")
    with override_target(sqlite_path=str(db_path)):
        before = main.get_camera_numbers("cust-1")
    assert len(before) == 2
    assert _remove(client, _camera_id(db_path, "dev-1")).status_code == 200
    with override_target(sqlite_path=str(db_path)):
        after = main.get_camera_numbers("cust-1")
        row = sqlite3.connect(db_path).execute(
            "SELECT status,camera_number,device_key FROM cameras WHERE name='dev-1'").fetchone()
    # The appliance runs (records, streams) only cameras with a camera number;
    # the removed camera's row -- what config sync sends it -- has none.
    assert len(after) == 1 and row == ("removed", None, None)


def test_without_a_camera_plan_the_customer_is_told_to_choose_one(client, db_path):
    _seed_tenant(db_path)
    credential = _activated(client, db_path)
    refused = _add(client, credential, "dev-1")
    assert refused.status_code == 403 and "A camera plan is required" in refused.json()["detail"]
    assert refused.json()["code"] == "camera_plan_required"
    capacity = _cameras(client)["camera_capacity"]
    assert capacity["usable"] == 0 and capacity["can_add_camera"] is False and "A camera plan is required" in capacity["message"]


def test_a_suspended_plan_adds_no_cameras(client, db_path):
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 4, status="suspended")
    credential = _activated(client, db_path)
    assert _add(client, credential, "dev-1").status_code == 403


def test_the_per_camera_prices_are_unchanged():
    import per_camera_billing
    assert {k: p["monthly_cents_per_camera"] for k, p in per_camera_billing.PLANS.items()} == {
        "basic_local": 999, "ai_local": 1499, "hybrid": 2499}


# ---------------------------------------------------------- pending add requests
def _queue(db_path, job_id, status, updated_at):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,status,message,"
                     "created_at,updated_at) VALUES(?,'cust-1','appl-1','site-1',?,'Camera',?,'',?,?)",
                     (job_id, f"dev-{job_id}", status, updated_at, updated_at))
        conn.commit()


def test_a_request_being_verified_holds_its_place_and_a_timed_out_one_does_not(client, db_path):
    from datetime import datetime, timedelta
    import customer_entitlements as ce
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 2)
    _activated(client, db_path)
    now = datetime.now()
    _queue(db_path, "j-verifying", "verifying", now.isoformat())
    _queue(db_path, "j-stale", "queued", (now - timedelta(seconds=ce.CAMERA_ADD_PENDING_SECONDS + 60)).isoformat())
    capacity = _cameras(client)["camera_capacity"]
    assert (capacity["pending"], capacity["available"], capacity["can_add_camera"]) == (1, 1, True)


def test_the_appliance_confirmation_gate_records_why_it_refused(client, db_path):
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 1)
    credential = _activated(client, db_path)
    _add(client, credential, "dev-1")
    # A request queued while there was room, confirmed after the plan filled up.
    _queue(db_path, "job-raced", "queued", __import__("datetime").datetime.now().isoformat())
    confirm = client.post("/api/appliance/AIC-SLOT1/provisioning-jobs/job-raced", json={"success": True},
                          headers=_appliance_headers("appl-1", credential))
    assert confirm.status_code == 200
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    job = client.get("/api/customer/camera-provisioning/job-raced").json()
    assert job["status"] == "failed" and job["failure_code"] == "camera_limit_reached" and PLAN_FULL in job["message"]


def test_a_removed_cameras_device_added_again_is_a_new_camera_using_a_place(client, db_path):
    """Re-adding the same physical camera after removal provisions a NEW camera
    row (the removed row keeps its history under its old name; it is never
    recycled for a device), and it counts against the plan again."""
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 1)
    credential = _activated(client, db_path)
    _add(client, credential, "dev-1")
    old_id = _camera_id(db_path, "dev-1")
    assert _remove(client, old_id).status_code == 200
    assert _add(client, credential, "dev-1").status_code == 200
    new_id = _camera_id(db_path, "dev-1")
    assert new_id != old_id
    with override_target(sqlite_path=str(db_path)):
        assert sqlite3.connect(db_path).execute("SELECT status FROM cameras WHERE id=?", (old_id,)).fetchone()[0] == "removed"
    assert _cameras(client)["camera_capacity"]["can_add_camera"] is False


def test_a_removed_cameras_number_is_never_given_to_another_camera(client, db_path):
    """The appliance files local recordings by camera number (camera{n}); a
    camera added after a removal must never inherit the removed one's number,
    or its local playback would show the removed camera's recordings."""
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 2)
    credential = _activated(client, db_path)
    _add(client, credential, "dev-1")
    _add(client, credential, "dev-2")
    first_id = _camera_id(db_path, "dev-1")
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        numbers = dict(conn.execute("SELECT device_key,camera_number FROM cameras WHERE device_key IS NOT NULL").fetchall())
    assert sorted(numbers.values()) == [1, 2]
    assert _remove(client, first_id).status_code == 200
    _add(client, credential, "dev-3")
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        new_number = conn.execute("SELECT camera_number FROM cameras WHERE device_key='dev-3'").fetchone()[0]
        removed = conn.execute("SELECT camera_number,former_camera_number FROM cameras WHERE id=?", (first_id,)).fetchone()
    assert removed == (None, numbers["dev-1"])
    assert new_number not in (numbers["dev-1"], numbers["dev-2"]) and new_number == 3
