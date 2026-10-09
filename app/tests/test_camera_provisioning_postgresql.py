"""PostgreSQL concurrency regressions for camera slot reservation.

Requires ANYAICAM_TEST_POSTGRES_URL to point at a disposable PostgreSQL
database. These tests use separate real connections and the same customer-row
lock helper used by both customer requests and appliance callbacks; SQLite
tests alone cannot establish PostgreSQL row-lock behavior.
"""
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

TEST_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "")
psycopg = pytest.importorskip("psycopg") if TEST_URL else None
if TEST_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"] = "postgresql"
    os.environ["ANYAICAM_DATABASE_URL"] = TEST_URL

from database_backend import lock_customer_camera_capacity, override_target
from partner_db import connection, initialize_database

pytestmark = pytest.mark.skipif(not TEST_URL, reason="set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")


def _hold_first_customer_lock(monkeypatch):
    """Pause one real endpoint after acquiring PG's row lock, before counting."""
    import database_backend

    original = database_backend.lock_customer_camera_capacity
    first_locked = threading.Event()
    second_started = threading.Event()
    second_locked = threading.Event()
    release_first = threading.Event()
    caller = threading.local()

    def controlled_lock(db, customer_id):
        if caller.role == "second":
            second_started.set()
        acquired = original(db, customer_id)
        if caller.role == "first":
            first_locked.set()
            assert release_first.wait(10)
        elif caller.role == "second":
            second_locked.set()
        return acquired

    monkeypatch.setattr(database_backend, "lock_customer_camera_capacity", controlled_lock)

    def as_role(role, callback, *args):
        caller.role = role
        return callback(*args)

    return as_role, first_locked, second_started, second_locked, release_first


def _tenant():
    suffix = uuid.uuid4().hex[:12]
    ids = {key: f"lock-test-{key}-{suffix}" for key in ("partner", "customer", "site", "appliance")}
    with override_target(database_url=TEST_URL):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES(?,?,?)", (ids["partner"], "Lock test", "now"))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
                       (ids["customer"], ids["partner"], "Lock test", suffix + "@example.test", "active", "real", "now"))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
                       (ids["site"], ids["customer"], "Lock test", "now"))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
                       (ids["appliance"], ids["customer"], ids["site"], "AIC-" + suffix.upper(), "now"))
    return ids


def _cleanup(ids):
    with override_target(database_url=TEST_URL):
        with connection() as db:
            db.execute("DELETE FROM camera_provisioning_requests WHERE customer_id=?", (ids["customer"],))
            db.execute("DELETE FROM cameras WHERE customer_id=?", (ids["customer"],))
            db.execute("DELETE FROM appliances WHERE id=?", (ids["appliance"],))
            db.execute("DELETE FROM sites WHERE id=?", (ids["site"],))
            db.execute("DELETE FROM customers WHERE id=?", (ids["customer"],))
            db.execute("DELETE FROM partners WHERE id=?", (ids["partner"],))


def test_postgres_customer_row_lock_serializes_pending_camera_reservations():
    ids = _tenant()
    locked_and_counted = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def reserve(job_id, started=None, hold=False):
        with override_target(database_url=TEST_URL):
            with connection() as db:
                if started:
                    started.set()
                assert lock_customer_camera_capacity(db, ids["customer"])
                configured = db.execute("SELECT COUNT(*) AS n FROM cameras WHERE customer_id=? AND device_key IS NOT NULL",
                                        (ids["customer"],)).fetchone()["n"]
                pending = db.execute("SELECT COUNT(*) AS n FROM camera_provisioning_requests WHERE customer_id=? AND status IN ('queued','verifying')",
                                     (ids["customer"],)).fetchone()["n"]
                if hold:
                    locked_and_counted.set()
                    assert release_first.wait(10)
                if configured + pending >= 1:
                    return False
                db.execute(
                    "INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,analytics_json,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'[]','queued','now','now')",
                    (job_id, ids["customer"], ids["appliance"], ids["site"], job_id, "Camera"),
                )
                return True

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reserve, "request-a", None, True)
            assert locked_and_counted.wait(5)
            second = pool.submit(reserve, "request-b", second_started)
            assert second_started.wait(5)  # second worker started; its row lock waits behind the first
            release_first.set()
            assert first.result(timeout=10) is True
            assert second.result(timeout=10) is False
        with override_target(database_url=TEST_URL):
            with connection() as db:
                count = db.execute("SELECT COUNT(*) AS n FROM camera_provisioning_requests WHERE customer_id=? AND status='queued'",
                                   (ids["customer"],)).fetchone()["n"]
        assert count == 1
    finally:
        release_first.set()
        _cleanup(ids)


def test_postgres_customer_row_lock_serializes_appliance_camera_creation():
    ids = _tenant()
    first_counted = threading.Event()
    release_first = threading.Event()

    def provision(device_key, hold=False):
        with override_target(database_url=TEST_URL):
            with connection() as db:
                assert lock_customer_camera_capacity(db, ids["customer"])
                configured = db.execute("SELECT COUNT(*) AS n FROM cameras WHERE customer_id=? AND device_key IS NOT NULL",
                                        (ids["customer"],)).fetchone()["n"]
                if hold:
                    first_counted.set()
                    assert release_first.wait(10)
                if configured >= 1:
                    return False
                db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,device_key,name,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (device_key, ids["customer"], ids["site"], ids["appliance"], device_key, "Camera", "configured", "now"))
                return True

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(provision, "camera-a", True)
            assert first_counted.wait(5)
            second = pool.submit(provision, "camera-b")
            release_first.set()
            assert first.result(timeout=10) is True
            assert second.result(timeout=10) is False
        with override_target(database_url=TEST_URL):
            with connection() as db:
                rows = db.execute("SELECT device_key FROM cameras WHERE customer_id=? AND device_key IS NOT NULL",
                                  (ids["customer"],)).fetchall()
        assert [item["device_key"] for item in rows] == ["camera-a"]
    finally:
        release_first.set()
        _cleanup(ids)


def test_postgres_real_customer_route_reserves_one_final_slot(monkeypatch):
    """Exercise endpoint with the second real PG transaction blocked at the row lock."""
    ids = _tenant()
    import main
    import partner_workspace
    import customer_entitlements

    customer_entitlements.upsert_entitlement(customer_id=ids["customer"], product="camera_slots_local",
                                             camera_slot_quantity=1, status="active")
    customer_entitlements.upsert_entitlement(customer_id=ids["customer"], product="vms_license",
                                             camera_slot_quantity=1, status="active")
    monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {
        "role": "customer_owner", "customer_id": ids["customer"], "email": "pg-owner@example.test",
    })
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/api/customer/cameras/provision"
                    and "POST" in (getattr(route, "methods", set()) or set()))

    def request(device_key):
        with override_target(database_url=TEST_URL):
            return endpoint(SimpleNamespace(headers={}, cookies={}, query_params={}), {
                "appliance_id": ids["appliance"], "device_key": device_key, "name": device_key,
            })

    call, first_locked, second_started, second_locked, release_first = _hold_first_customer_lock(monkeypatch)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, "first", request, "route-a")
            assert first_locked.wait(5)
            second = pool.submit(call, "second", request, "route-b")
            assert second_started.wait(5)
            assert not second_locked.wait(0.25), "second request bypassed PostgreSQL customer row lock"
            release_first.set()
            results = [first.result(timeout=10), second.result(timeout=10)]
        status = sorted(item.status_code if hasattr(item, "status_code") else 200 for item in results)
        assert status == [200, 403]
        with override_target(database_url=TEST_URL):
            with connection() as db:
                assert db.execute("SELECT COUNT(*) AS n FROM camera_provisioning_requests WHERE customer_id=?",
                                  (ids["customer"],)).fetchone()["n"] == 1
    finally:
        release_first.set()
        _cleanup(ids)


def test_postgres_real_appliance_callback_cannot_provision_two_cameras_into_one_slot(monkeypatch):
    """Exercise the authenticated provisioning callback and its row lock."""
    ids = _tenant()
    import main
    from partner_db import password_hash

    customer_id = ids["customer"]
    appliance_id = ids["appliance"]
    credential = "postgres-camera-lock-test-credential"
    job_ids = ("pg-job-a-" + uuid.uuid4().hex[:8], "pg-job-b-" + uuid.uuid4().hex[:8])
    with override_target(database_url=TEST_URL):
        import customer_entitlements
        customer_entitlements.upsert_entitlement(customer_id=customer_id, product="camera_slots_local",
                                                 camera_slot_quantity=1, status="active")
        customer_entitlements.upsert_entitlement(customer_id=customer_id, product="vms_license",
                                                 camera_slot_quantity=1, status="active")
        with connection() as db:
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
                       ("pg-cred-" + uuid.uuid4().hex[:8], appliance_id, password_hash(credential), "now"))
            for job_id, device_key in zip(job_ids, ("callback-a", "callback-b")):
                db.execute(
                    "INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,analytics_json,status,message,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'[]','verifying','Queued','now','now')",
                    (job_id, customer_id, appliance_id, ids["site"], device_key, device_key),
                )
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/api/appliance/{cloud_id}/provisioning-jobs/{job_id}"
                    and "POST" in (getattr(route, "methods", set()) or set()))

    def confirm(job_id):
        with override_target(database_url=TEST_URL):
            headers = {
                "x-appliance-id": appliance_id,
                "x-request-timestamp": str(int(time.time())),
                "x-request-nonce": secrets.token_hex(16),
                "authorization": "Bearer " + credential,
            }
            return endpoint(SimpleNamespace(headers=headers, client=None), ids["cloud_id"], job_id,
                            {"success": True, "message": "Verified"})

    call, first_locked, second_started, second_locked, release_first = _hold_first_customer_lock(monkeypatch)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, "first", confirm, job_ids[0])
            assert first_locked.wait(5)
            second = pool.submit(call, "second", confirm, job_ids[1])
            assert second_started.wait(5)
            assert not second_locked.wait(0.25), "second callback bypassed PostgreSQL customer row lock"
            release_first.set()
            results = [first.result(timeout=10), second.result(timeout=10)]
        assert len(results) == 2
        with override_target(database_url=TEST_URL):
            with connection() as db:
                cameras = db.execute("SELECT device_key,camera_number FROM cameras WHERE customer_id=? AND device_key IS NOT NULL",
                                     (customer_id,)).fetchall()
                states = db.execute("SELECT status,failure_code FROM camera_provisioning_requests WHERE id IN (?,?) ORDER BY id",
                                    job_ids).fetchall()
        assert len(cameras) == 1
        assert cameras[0]["camera_number"] is not None
        assert sorted(item["status"] for item in states) == ["failed", "provisioned"]
    finally:
        release_first.set()
        _cleanup(ids)
