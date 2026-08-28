"""Onboarding Stage 2, cloud side: scan-job state lifecycle, camera
provisioning, credential encryption, device-key dedup, and tenancy.

Auth-hardening pass: appliance-facing scan-jobs (already correct) and
provisioning-jobs (previously "-legacy", gated only by a bearer token
checked against the appliance's one-time activation-token hash, no
nonce/replay protection) now both live in appliance_cloud.py behind
authenticate_appliance() -- bearer identity + nonce + timestamp +
replay-table + explicit appliance/customer/site tenancy checks, the
same mechanism every other appliance_cloud.py route uses. The old
camera_scan_jobs status vocabulary mismatch ('scanning'/'completed'/
'failed' vs. the real poller's 'running'/'complete'/'error') is also
fixed here, canonical vocabulary only.

Route functions are pulled directly off the registered FastAPI route
table (closures inside register_partner_workspace_routes /
register_appliance_cloud_routes, not importable top-level names) and
called as plain functions -- same established pattern as
test_onboarding_commissioned_camera_gate.py.
"""

import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import main
import partner_workspace
from database_backend import override_target
from partner_db import initialize_database, password_hash


def _route(path, method="GET"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


def _fake_request(headers=None):
    return SimpleNamespace(headers=headers or {}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_discovery.db"


def _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1", online="online", credential=None):
    credential = credential or f"token-{appliance_id}"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,status,created_at) "
        "VALUES(?, 'partner-1','Test Co','Test Co','test-'||?||'@example.com','active','2026-01-01')",
        (customer_id, customer_id),
    )
    site_id = f"site-{customer_id}"
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,'Main','2026-01-01')", (site_id, customer_id))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,created_at) "
        "VALUES(?,?,?,?, 'activated', ?, '2026-01-01')",
        (appliance_id, customer_id, site_id, cloud_id, online),
    )
    # authenticate_appliance() checks appliance_credentials, not
    # activation_token_hash -- that swap is the point of this hardening
    # pass, so every appliance-facing test seeds a real credential row.
    conn.execute(
        "INSERT OR IGNORE INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
        (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-01-01"),
    )
    conn.commit()
    return site_id


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _appliance_auth_headers(appliance_id="appl-1", credential=None, timestamp=None, nonce=None):
    credential = credential or f"token-{appliance_id}"
    return {
        "x-appliance-id": appliance_id,
        "x-request-timestamp": str(int(timestamp if timestamp is not None else time.time())),
        "x-request-nonce": nonce or secrets.token_hex(16),
        "authorization": f"Bearer {credential}",
    }


# ---------------------------------------------------------------- scan jobs

def test_scan_job_starts_queued_when_appliance_online(db_path, monkeypatch):
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, online="online")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = request_camera_scan(_fake_request(), "appl-1")
    assert result["status"] == "queued"


def test_scan_job_waits_for_appliance_when_offline(db_path, monkeypatch):
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, online="offline")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = request_camera_scan(_fake_request(), "appl-1")
    assert result["status"] == "waiting_for_appliance"


def test_appliance_polling_moves_queued_job_to_running(db_path, monkeypatch):
    # Canonical vocabulary: 'running', not the old dead-code '-legacy'
    # path's 'scanning'.
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    secure_scan_jobs = _route("/api/appliance/{cloud_id}/scan-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        request_camera_scan(_fake_request(), "appl-1")
        result = secure_scan_jobs(_fake_request(_appliance_auth_headers()), "AIC-TEST1")
    assert len(result["jobs"]) == 1
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute("SELECT status FROM camera_scan_jobs").fetchone()
    assert row["status"] == "running"


def test_appliance_submit_complete_does_not_create_camera_rows(db_path, monkeypatch):
    # Discovered devices stay in results_json only until the customer
    # explicitly selects + provisions one -- nothing should ever land
    # in `cameras` just from a scan completing.
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    secure_scan_results = _route("/api/appliance/{cloud_id}/scan-jobs/{job_id}", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_scan(_fake_request(), "appl-1")
        secure_scan_results(
            _fake_request(_appliance_auth_headers()), "AIC-TEST1", job["job_id"],
            {"status": "complete", "progress": 100, "message": "Found 2 devices.",
             "results": [{"device_key": "onvif-uuid-1", "ip": "192.168.1.50", "manufacturer": "Hikvision"}]},
        )
        conn2 = sqlite3.connect(db_path)
        camera_count = conn2.execute("SELECT count(*) c FROM cameras").fetchone()[0]
    assert camera_count == 0


def test_appliance_submit_strips_any_credentials_from_results(db_path, monkeypatch):
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    secure_scan_results = _route("/api/appliance/{cloud_id}/scan-jobs/{job_id}", "POST")
    camera_scan_status = _route("/api/customer/camera-scans/{job_id}", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_scan(_fake_request(), "appl-1")
        secure_scan_results(
            _fake_request(_appliance_auth_headers()), "AIC-TEST1", job["job_id"],
            {"status": "complete", "progress": 100,
             "results": [{"device_key": "onvif-uuid-1", "ip": "192.168.1.50", "username": "admin", "password": "hunter2"}]},
        )
        status = camera_scan_status(_fake_request(), job["job_id"])
    assert "username" not in status["results"][0]
    assert "password" not in status["results"][0]


def test_scan_job_times_out_when_never_picked_up(db_path, monkeypatch):
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    camera_scan_status = _route("/api/customer/camera-scans/{job_id}", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_scan(_fake_request(), "appl-1")
        stale = (datetime.now() - timedelta(seconds=700)).isoformat()
        conn2 = sqlite3.connect(db_path)
        conn2.execute("UPDATE camera_scan_jobs SET updated_at=? WHERE id=?", (stale, job["job_id"]))
        conn2.commit()
        status = camera_scan_status(_fake_request(), job["job_id"])
    assert status["status"] == "timed_out"


def test_completed_scan_job_is_not_incorrectly_timed_out(db_path, monkeypatch):
    # Regression test for the status-vocabulary bug this pass fixes: a
    # job the real poller already finished as 'complete' (or 'error')
    # must be recognized as terminal, not force-flipped to 'timed_out'
    # ~180s later just because CAMERA_SCAN_TERMINAL_STATES didn't know
    # that spelling.
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    camera_scan_status = _route("/api/customer/camera-scans/{job_id}", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_scan(_fake_request(), "appl-1")
        old = (datetime.now() - timedelta(seconds=700)).isoformat()
        conn2 = sqlite3.connect(db_path)
        conn2.execute("UPDATE camera_scan_jobs SET status='complete',updated_at=? WHERE id=?", (old, job["job_id"]))
        conn2.commit()
        status = camera_scan_status(_fake_request(), job["job_id"])
    assert status["status"] == "complete"


def test_customer_can_cancel_a_scan_job(db_path, monkeypatch):
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    cancel_camera_scan = _route("/api/customer/camera-scans/{job_id}/cancel", "POST")
    camera_scan_status = _route("/api/customer/camera-scans/{job_id}", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_scan(_fake_request(), "appl-1")
        cancel_camera_scan(_fake_request(), job["job_id"])
        status = camera_scan_status(_fake_request(), job["job_id"])
    assert status["status"] == "cancelled"


# ---------------------------------------------------------------- credentials

def test_provisioning_without_credentials_needs_no_encryption_key(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    monkeypatch.delenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", raising=False)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door"})
    assert result["status"] == "queued"


def test_provisioning_with_credentials_fails_closed_without_key(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    monkeypatch.delenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", raising=False)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(Exception) as excinfo:
            request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 503


def test_credentials_are_encrypted_at_rest_and_never_plaintext(db_path, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", key.decode())
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door", "username": "admin", "password": "hunter2"})
        conn2 = sqlite3.connect(db_path)
        stored = conn2.execute("SELECT encrypted_credentials FROM camera_provisioning_requests WHERE id=?", (job["job_id"],)).fetchone()[0]
    assert stored is not None
    assert b"hunter2" not in bytes(stored)
    assert b"admin" not in bytes(stored)
    decrypted = json.loads(Fernet(key).decrypt(bytes(stored)))
    assert decrypted == {"username": "admin", "password": "hunter2"}


def test_credentials_cleared_the_instant_appliance_polls_for_them(db_path, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", key.decode())
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door", "username": "admin", "password": "hunter2"})
        result = appliance_provisioning_jobs(_fake_request(_appliance_auth_headers()), "AIC-TEST1")
        conn2 = sqlite3.connect(db_path)
        stored_after = conn2.execute("SELECT encrypted_credentials FROM camera_provisioning_requests WHERE id=?", (job["job_id"],)).fetchone()[0]
    assert result["jobs"][0]["credentials"] == {"username": "admin", "password": "hunter2"}
    assert stored_after is None  # cleared from storage the instant it was delivered


def test_provisioning_audit_never_includes_credential_fields(db_path, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", key.decode())
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    audit_calls = []
    monkeypatch.setattr(partner_workspace, "audit", lambda *args, **kwargs: audit_calls.append((args, kwargs)))
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door", "username": "admin", "password": "hunter2"})
    serialized = json.dumps(audit_calls)
    assert "hunter2" not in serialized
    assert "admin" not in serialized or "admin" not in [c for a in audit_calls for c in a[0]]


# ------------------------------------------------------- provisioning result / dedup

def test_successful_provisioning_creates_a_commissioned_camera(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_submit_provisioning = _route("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door"})
        appliance_submit_provisioning(_fake_request(_appliance_auth_headers()), "AIC-TEST1", job["job_id"], {"success": True, "message": "Verified."})
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        camera = conn2.execute("SELECT * FROM cameras WHERE device_key='onvif-uuid-1'").fetchone()
    assert camera is not None
    assert camera["status"] == "configured"
    assert camera["name"] == "Front Door"


def test_failed_provisioning_creates_no_camera(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_submit_provisioning = _route("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door"})
        appliance_submit_provisioning(_fake_request(_appliance_auth_headers()), "AIC-TEST1", job["job_id"], {"success": False, "message": "Authentication failed."})
        conn2 = sqlite3.connect(db_path)
        camera_count = conn2.execute("SELECT count(*) c FROM cameras").fetchone()[0]
        status = conn2.execute("SELECT status,message FROM camera_provisioning_requests WHERE id=?", (job["job_id"],)).fetchone()
    assert camera_count == 0
    assert status[0] == "failed"


def test_reprovisioning_the_same_device_key_updates_not_duplicates(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_submit_provisioning = _route("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        job1 = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door"})
        appliance_submit_provisioning(_fake_request(_appliance_auth_headers()), "AIC-TEST1", job1["job_id"], {"success": True})
        # Same physical device rediscovered and reprovisioned (e.g. after
        # a rename) -- must update the existing row, not create a second.
        job2 = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door Renamed"})
        appliance_submit_provisioning(_fake_request(_appliance_auth_headers()), "AIC-TEST1", job2["job_id"], {"success": True})
        conn2 = sqlite3.connect(db_path)
        cameras = conn2.execute("SELECT name FROM cameras WHERE device_key='onvif-uuid-1'").fetchall()
    assert len(cameras) == 1
    assert cameras[0][0] == "Front Door Renamed"


# ---------------------------------------------------------- auth hardening

def test_valid_auth_succeeds(db_path, monkeypatch):
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        result = appliance_provisioning_jobs(_fake_request(_appliance_auth_headers()), "AIC-TEST1")
    assert result == {"jobs": []}


def test_missing_auth_headers_rejected(db_path, monkeypatch):
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        with pytest.raises(Exception) as excinfo:
            appliance_provisioning_jobs(_fake_request({}), "AIC-TEST1")
    assert getattr(excinfo.value, "status_code", None) == 401


def test_expired_timestamp_rejected(db_path, monkeypatch):
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        stale_headers = _appliance_auth_headers(timestamp=time.time() - 400)  # outside the 300s window
        with pytest.raises(Exception) as excinfo:
            appliance_provisioning_jobs(_fake_request(stale_headers), "AIC-TEST1")
    assert getattr(excinfo.value, "status_code", None) == 401


def test_replayed_nonce_rejected(db_path, monkeypatch):
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        headers = _appliance_auth_headers(nonce="fixed-nonce-0123456789ab")
        appliance_provisioning_jobs(_fake_request(headers), "AIC-TEST1")  # first use succeeds
        with pytest.raises(Exception) as excinfo:
            appliance_provisioning_jobs(_fake_request(headers), "AIC-TEST1")  # exact replay
    assert getattr(excinfo.value, "status_code", None) == 409


def test_wrong_appliance_credential_rejected(db_path, monkeypatch):
    # appl-1's own X-Appliance-Id, but a credential that doesn't match
    # any of appl-1's stored appliance_credentials rows.
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        headers = _appliance_auth_headers(credential="not-the-real-credential")
        with pytest.raises(Exception) as excinfo:
            appliance_provisioning_jobs(_fake_request(headers), "AIC-TEST1")
    assert getattr(excinfo.value, "status_code", None) == 403


def test_appliance_cannot_retrieve_a_different_appliances_jobs_by_cloud_id(db_path, monkeypatch):
    # The strongest form of this guarantee: appl-1's own valid bearer
    # credential doesn't even authenticate against appl-2's cloud_id --
    # authenticate_appliance() resolves identity from X-Appliance-Id
    # (appl-1) and the route rejects the moment appliance['cloud_id']
    # disagrees with the cloud_id in the URL, before any job lookup.
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        _seed(conn, customer_id="cust-2", appliance_id="appl-2", cloud_id="AIC-TEST2")
        with pytest.raises(Exception) as excinfo:
            appliance_provisioning_jobs(_fake_request(_appliance_auth_headers("appl-1")), "AIC-TEST2")
    assert getattr(excinfo.value, "status_code", None) == 403


def test_appliance_only_ever_sees_its_own_provisioning_jobs(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        _seed(conn, customer_id="cust-2", appliance_id="appl-2", cloud_id="AIC-TEST2")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity("cust-2"))
        request_camera_provisioning(_fake_request(), {"appliance_id": "appl-2", "device_key": "onvif-uuid-1", "name": "Cust2 Camera"})
        appl1_view = appliance_provisioning_jobs(_fake_request(_appliance_auth_headers("appl-1")), "AIC-TEST1")
    assert appl1_view["jobs"] == []


def test_wrong_tenant_job_is_not_delivered_even_if_appliance_id_matched(db_path, monkeypatch):
    # Defense in depth: a provisioning_requests row whose own
    # customer_id/site_id don't match the authenticated appliance's
    # actual tenant (simulating corrupted or mis-inserted data) must
    # never be delivered, even though the appliance_id column matches.
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        site_id = _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,"
            "recording_mode,analytics_json,encrypted_credentials,status,message,created_at,updated_at) "
            "VALUES('job-mismatch','wrong-customer','appl-1','wrong-site','onvif-uuid-1','Front Door','motion','[]',NULL,'queued','',?,?)",
            (now, now),
        )
        conn.commit()
        result = appliance_provisioning_jobs(_fake_request(_appliance_auth_headers("appl-1")), "AIC-TEST1")
    assert result["jobs"] == []
    conn2 = sqlite3.connect(db_path)
    status = conn2.execute("SELECT status FROM camera_provisioning_requests WHERE id='job-mismatch'").fetchone()[0]
    assert status == "queued"  # left untouched, not silently marked verifying/delivered


def test_unauthorized_credential_retrieval_is_rejected(db_path, monkeypatch):
    # The route that actually hands back decrypted camera credentials
    # must reject an unauthenticated caller before ever touching
    # encrypted_credentials, not just after.
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", key.decode())
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-1", "name": "Front Door", "username": "admin", "password": "hunter2"})
        with pytest.raises(Exception) as excinfo:
            appliance_provisioning_jobs(_fake_request({}), "AIC-TEST1")  # no auth headers at all
        conn2 = sqlite3.connect(db_path)
        still_encrypted = conn2.execute("SELECT encrypted_credentials FROM camera_provisioning_requests").fetchone()[0]
    assert getattr(excinfo.value, "status_code", None) == 401
    assert still_encrypted is not None  # untouched -- rejection happened before delivery


# ---------------------------------------------------------------- tenancy

def test_customer_cannot_request_scan_on_another_customers_appliance(db_path, monkeypatch):
    request_camera_scan = _route("/api/customer/appliances/{appliance_id}/scan", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        _seed(conn, customer_id="cust-2", appliance_id="appl-2", cloud_id="AIC-TEST2")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity("cust-1"))
        with pytest.raises(Exception) as excinfo:
            request_camera_scan(_fake_request(), "appl-2")  # belongs to cust-2
    assert getattr(excinfo.value, "status_code", None) == 404


# ---------------------------------------------------------------- blocker #2: customer-scoped device_key identity
#
# appliance_submit_provisioning() used to dedup by (appliance_id,
# device_key) -- inconsistent with provision_customer_camera()'s own
# (customer_id, device_key) rule, and capable of violating idx_cameras_
# customer_device_key's UNIQUE constraint outright the first time the
# same physical camera was rediscovered via a second appliance under
# the same customer. Fixed to dedup by (customer_id, device_key),
# reassigning appliance_id/site_id to the job's own values on a match
# so the camera row never points at a stale appliance after a
# successful reprovision.


def test_camera_moved_to_a_second_appliance_under_the_same_customer_reassigns_not_duplicates(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_submit_provisioning = _route("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        # A second appliance under the SAME customer, at a genuinely
        # different site -- _seed() itself always reuses site-{customer_id},
        # so the second site/appliance are inserted directly here to
        # actually exercise cross-site reassignment, not just cross-
        # appliance-same-site.
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-cust-1-b','cust-1','Warehouse','2026-01-01')")
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,created_at) "
            "VALUES('appl-1b','cust-1','site-cust-1-b','AIC-TEST1B','activated','online','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-appl-1b','appl-1b',?,'2026-01-01')",
            (password_hash("token-appl-1b"),),
        )
        conn.commit()
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())

        job1 = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-moved", "name": "Loading Dock"})
        appliance_submit_provisioning(_fake_request(_appliance_auth_headers()), "AIC-TEST1", job1["job_id"], {"success": True})

        # Same physical camera, now discovered via the SECOND appliance
        # (a different site) -- must reassign, not duplicate, and must
        # not raise sqlite3.IntegrityError against the customer-scoped
        # unique index.
        job2 = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1b", "device_key": "onvif-uuid-moved", "name": "Loading Dock"})
        appliance_submit_provisioning(
            _fake_request(_appliance_auth_headers(appliance_id="appl-1b", credential="token-appl-1b")),
            "AIC-TEST1B", job2["job_id"], {"success": True},
        )

        conn2 = sqlite3.connect(db_path)
        cameras = conn2.execute("SELECT appliance_id,site_id FROM cameras WHERE customer_id='cust-1' AND device_key='onvif-uuid-moved'").fetchall()
    assert len(cameras) == 1  # no duplicate row
    assert cameras[0][0] == "appl-1b"     # appliance_id reassigned to the new appliance
    assert cameras[0][1] == "site-cust-1-b"  # site_id moved with it


def test_same_device_key_under_a_different_customer_is_a_separate_camera(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_submit_provisioning = _route("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        _seed(conn, customer_id="cust-2", appliance_id="appl-2", cloud_id="AIC-TEST2")

        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity("cust-1"))
        job1 = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-shared", "name": "Front Door"})
        appliance_submit_provisioning(_fake_request(_appliance_auth_headers()), "AIC-TEST1", job1["job_id"], {"success": True})

        # A coincidentally-identical device_key under an ENTIRELY
        # different customer/appliance -- must be allowed as its own
        # separate camera row, never merged with cust-1's.
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity("cust-2"))
        job2 = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-2", "device_key": "onvif-uuid-shared", "name": "Front Door"})
        appliance_submit_provisioning(
            _fake_request(_appliance_auth_headers(appliance_id="appl-2", credential="token-appl-2")),
            "AIC-TEST2", job2["job_id"], {"success": True},
        )

        conn2 = sqlite3.connect(db_path)
        cameras = conn2.execute("SELECT customer_id FROM cameras WHERE device_key='onvif-uuid-shared' ORDER BY customer_id").fetchall()
    assert [row[0] for row in cameras] == ["cust-1", "cust-2"]  # two real rows, one per customer, no collision


def test_multiple_null_device_key_cameras_coexist_under_one_customer(db_path):
    """The partial unique index (WHERE device_key IS NOT NULL) must
    keep allowing multiple placeholder/pending cameras with no
    device_key yet under the same customer -- unaffected by the
    customer-scoping fix, since NULL never equals NULL in SQL and the
    index itself is explicitly scoped to exclude NULL."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,device_key) "
            "VALUES('cam-a','cust-1','site-cust-1','appl-1','Pending A','pending_installation','2026-01-01',NULL)"
        )
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,device_key) "
            "VALUES('cam-b','cust-1','site-cust-1','appl-1','Pending B','pending_installation','2026-01-01',NULL)"
        )
        conn.commit()  # must not raise -- two NULL device_key rows under the same customer
        count = conn.execute("SELECT count(*) FROM cameras WHERE customer_id='cust-1' AND device_key IS NULL").fetchone()[0]
    assert count == 2


def _all_routes(path, method):
    """Unlike _route() (first match only), returns every endpoint
    registered at this exact path+method -- needed here because
    /api/customer/cameras/provision has TWO functions registered at
    the identical path (partner_workspace.py lines 519 and 571):
    request_camera_provisioning (what real HTTP traffic actually
    reaches, Starlette matching in registration order) and the later,
    fully shadowed/unreachable provision_customer_camera. That
    duplicate-route bug is real, pre-existing, and unrelated to this
    fix -- flagged in the blocker #2 report, not fixed here. This
    helper exists only so the identity-rule parity this test checks
    can still be verified directly against provision_customer_camera's
    own logic, bypassing the routing collision rather than being
    silently blocked by it.
    """
    return [
        r.endpoint for r in main.app.routes
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method})
    ]


def test_provisioning_callback_and_customer_portal_provisioning_agree_on_identity(db_path, monkeypatch):
    """Parity check: a camera created through the appliance-callback
    path (appliance_submit_provisioning) is found and updated -- not
    duplicated -- by the customer-portal path (provision_customer_
    camera) for the same customer_id+device_key. Both routes must be
    reading the exact same identity rule.

    provision_customer_camera is called directly here (see
    _all_routes()'s own docstring) because it is currently shadowed at
    its own registered path by an identically-pathed earlier route --
    a separate, pre-existing bug this test intentionally works around
    rather than silently accepts as "cannot be tested"."""
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_submit_provisioning = _route("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}", "POST")
    matches = _all_routes("/api/customer/cameras/provision", "POST")
    assert len(matches) == 2, "expected exactly the two known duplicate-path registrations -- update this test if that changes"
    provision_customer_camera = matches[1]
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed(conn, customer_id="cust-1", appliance_id="appl-1", cloud_id="AIC-TEST1")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())

        # 1. Appliance-callback path creates the camera first.
        job1 = request_camera_provisioning(_fake_request(), {"appliance_id": "appl-1", "device_key": "onvif-uuid-parity", "name": "Back Gate"})
        appliance_submit_provisioning(_fake_request(_appliance_auth_headers()), "AIC-TEST1", job1["job_id"], {"success": True})

        # 2. Customer-portal path, same customer_id+device_key -- must
        # update the SAME row, not create a second.
        provision_customer_camera(_fake_request(), {"device_key": "onvif-uuid-parity", "name": "Back Gate Renamed"})

        conn2 = sqlite3.connect(db_path)
        cameras = conn2.execute("SELECT name FROM cameras WHERE customer_id='cust-1' AND device_key='onvif-uuid-parity'").fetchall()
    assert len(cameras) == 1
    assert cameras[0][0] == "Back Gate Renamed"
