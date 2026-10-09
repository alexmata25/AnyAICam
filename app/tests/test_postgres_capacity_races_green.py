"""PostgreSQL concurrency, duplicates, retries and capacity changes for the
universal per-camera plan (Green, 2026-10-09 validation of 92a16d7).

Requires ANYAICAM_TEST_POSTGRES_URL (a disposable database). Unlike
test_camera_provisioning_postgresql.py, which pauses one transaction at the
lock, these release N real transactions at once (a barrier) through the
real customer route and appliance callback, with capacity from a per-camera
plan (camera_plan_entitlements_v2), as every customer now has.
"""
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from test_camera_provisioning_postgresql import TEST_URL, _cleanup, _tenant  # noqa: F401  (sets the PG backend)

pytestmark = pytest.mark.skipif(not TEST_URL, reason="set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")

CREDENTIAL = "green-race-credential"


def _db():
    from database_backend import override_target
    from partner_db import connection
    return override_target(database_url=TEST_URL), connection


def _sql(sql, args=()):
    target, connection = _db()
    with target:
        with connection() as db:
            cursor = db.execute(sql, args)
            try:
                return [dict(r) for r in cursor.fetchall()]
            except Exception:
                return []


def _per_camera_plan(customer_id, quantity, status="active"):
    _sql("DELETE FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer_id,))
    _sql("INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
         "stripe_subscription_item_id,stripe_price_id,status,stripe_status,local_retention_days,created_at,updated_at) "
         "VALUES(?,?,'ai_local',?,?,?,'si','price','" + status + "','active',7,'now','now')",
         ("v2-" + uuid.uuid4().hex[:8], customer_id, quantity, "cus_" + customer_id[-8:], "sub_" + uuid.uuid4().hex[:8]))


@pytest.fixture()
def tenant(monkeypatch):
    import partner_workspace
    ids = _tenant()
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {
        "role": "customer_owner", "customer_id": ids["customer"], "email": "race-owner@example.test"})
    from partner_db import password_hash
    _sql("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
         ("cred-" + uuid.uuid4().hex[:8], ids["appliance"], password_hash(CREDENTIAL), "now"))
    yield ids
    _sql("DELETE FROM camera_plan_entitlements_v2 WHERE customer_id=?", (ids["customer"],))
    _sql("DELETE FROM hardware_orders WHERE customer_id=?", (ids["customer"],))
    _cleanup(ids)


def _endpoint(path):
    import main
    return next(route.endpoint for route in main.app.routes
                if getattr(route, "path", None) == path and "POST" in (getattr(route, "methods", set()) or set()))


def _request(ids, device_key):
    target, _ = _db()
    with target:
        answer = _endpoint("/api/customer/cameras/provision")(
            SimpleNamespace(headers={}, cookies={}, query_params={}),
            {"appliance_id": ids["appliance"], "device_key": device_key, "name": device_key})
    status = getattr(answer, "status_code", 200)
    return status, (answer if status == 200 else None)


def _confirm(ids, job_id):
    target, _ = _db()
    headers = {"x-appliance-id": ids["appliance"], "x-request-timestamp": str(int(time.time())),
               "x-request-nonce": secrets.token_hex(16), "authorization": "Bearer " + CREDENTIAL}
    with target:
        try:
            _endpoint("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}")(
                SimpleNamespace(headers=headers, client=None), ids["cloud_id"], job_id, {"success": True, "message": "Verified"})
            return "ok"
        except Exception as error:  # an HTTPException is an answer, anything else is a defect
            return getattr(error, "status_code", None) or type(error).__name__


def _all_at_once(calls):
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait(10)
        return call()
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [future.result(timeout=60) for future in [pool.submit(run, call) for call in calls]]


def _jobs(ids):
    return _sql("SELECT id,device_key,status,failure_code FROM camera_provisioning_requests WHERE customer_id=? ORDER BY created_at",
                (ids["customer"],))


def _cameras(ids):
    return _sql("SELECT device_key,camera_number FROM cameras WHERE customer_id=? AND device_key IS NOT NULL", (ids["customer"],))


def test_a_burst_of_adds_reserves_exactly_the_plans_free_places(tenant):
    _per_camera_plan(tenant["customer"], 3)
    results = _all_at_once([lambda n=n: _request(tenant, f"burst-{n}") for n in range(8)])
    assert sorted(status for status, _ in results) == [200] * 3 + [403] * 5
    assert len(_jobs(tenant)) == 3


def test_confirming_every_reserved_add_at_once_never_exceeds_the_plan(tenant):
    _per_camera_plan(tenant["customer"], 3)
    for n in range(3):
        assert _request(tenant, f"cam-{n}")[0] == 200
    jobs = _jobs(tenant)
    outcomes = _all_at_once([lambda j=j["id"]: _confirm(tenant, j) for j in jobs])
    assert outcomes == ["ok"] * 3
    cameras = _cameras(tenant)
    assert len(cameras) == 3
    assert len({c["camera_number"] for c in cameras}) == 3 and None not in {c["camera_number"] for c in cameras}


def test_duplicate_adds_of_one_device_end_as_one_camera(tenant):
    _per_camera_plan(tenant["customer"], 3)
    results = _all_at_once([lambda: _request(tenant, "same-device") for _ in range(6)])
    accepted = [status for status, _ in results if status == 200]
    assert 1 <= len(accepted) <= 3  # each pending duplicate holds a place until confirmed or timed out
    outcomes = _all_at_once([lambda j=j["id"]: _confirm(tenant, j) for j in _jobs(tenant)])
    assert all(outcome == "ok" for outcome in outcomes), outcomes
    assert [c["device_key"] for c in _cameras(tenant)] == ["same-device"]


def test_a_retried_confirmation_of_one_job_creates_one_camera_and_no_server_error(tenant):
    _per_camera_plan(tenant["customer"], 2)
    assert _request(tenant, "retry-cam")[0] == 200
    job = _jobs(tenant)[0]["id"]
    outcomes = _all_at_once([lambda: _confirm(tenant, job) for _ in range(4)])
    assert all(outcome in ("ok", 409) for outcome in outcomes), outcomes
    assert len(_cameras(tenant)) == 1


def test_a_plan_reduced_while_adds_are_pending_admits_only_the_new_quantity(tenant):
    _per_camera_plan(tenant["customer"], 2)
    for n in range(2):
        assert _request(tenant, f"pending-{n}")[0] == 200
    _per_camera_plan(tenant["customer"], 1)  # quantity lowered at renewal
    outcomes = _all_at_once([lambda j=j["id"]: _confirm(tenant, j) for j in _jobs(tenant)])
    assert all(outcome == "ok" for outcome in outcomes), outcomes
    assert len(_cameras(tenant)) == 1
    states = sorted((j["status"], j["failure_code"]) for j in _jobs(tenant))
    assert states == [("failed", "camera_limit_reached"), ("provisioned", None)]


def test_a_plan_raised_admits_more_cameras_at_once(tenant):
    _per_camera_plan(tenant["customer"], 1)
    assert sorted(s for s, _ in _all_at_once([lambda n=n: _request(tenant, f"up-{n}") for n in range(3)])) == [200, 403, 403]
    _per_camera_plan(tenant["customer"], 3)
    assert sorted(s for s, _ in _all_at_once([lambda n=n: _request(tenant, f"up2-{n}") for n in range(3)])) == [200, 200, 403]


def test_a_suspended_plan_admits_nothing(tenant):
    _per_camera_plan(tenant["customer"], 3, status="suspended")
    results = _all_at_once([lambda n=n: _request(tenant, f"susp-{n}") for n in range(3)])
    assert [status for status, _ in results] == [403] * 3


def test_capacity_without_a_per_camera_plan_reads_on_postgresql(tenant):
    """The LIKE 'AIC-APPLIANCE-%' query (appliance_includes_vms_license) on
    PostgreSQL: it raised before d338ef3 for every customer without an
    active per-camera plan."""
    import customer_entitlements
    target, _ = _db()
    with target:
        assert customer_entitlements.appliance_includes_vms_license(tenant["customer"]) is False
        breakdown = customer_entitlements.capacity_breakdown(tenant["customer"])
    assert breakdown["camera_slot_quantity"] == 0 and breakdown["vms_license_required"] is True
