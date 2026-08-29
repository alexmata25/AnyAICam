"""Regression coverage for a confirmed-live Samsung bug found
immediately after the credential-encryption-key fix started actually
delivering provisioning jobs to the appliance for the first time: once
a real ONVIF/RTSP-credentialed provisioning attempt failed (the
appliance genuinely tried and the device rejected the credentials),
"Discovered camera 1"'s button still read "Added" and stayed disabled
-- with no real camera row ever created -- leaving the customer with
no way to retry.

Root cause: POST /api/customer/cameras/provision only ever reports
whether the request was successfully QUEUED (HTTP 200, `{"status":
"queued"}`) -- the actual accept/reject decision happens later,
asynchronously, once the appliance polls for the job, attempts
verification, and posts the result back (see appliance_cloud.py's
appliance_submit_provisioning()). The click handler used to treat a
200 from the initial POST alone as "the camera was added" and never
polled the job's real outcome via the ALREADY-EXISTING GET
/api/customer/camera-provisioning/{job_id} endpoint at all.

Fixed with a client-side pollProvisioning() loop (mirroring pollScan()'s
existing poll-until-terminal pattern) that only marks the button
"Added" once the job's own status is 'provisioned' -- resetting it back
to enabled, original "Add this camera" text on 'failed' (or on any
error reaching the initial POST itself), while showProvisioningError()
keeps the failure message visibly displayed via #scan-message. No new
camera row is ever created and no physical camera setting is touched
by any of this -- it only changes how the existing, already-tested
async outcome is reflected in the UI.
"""

import secrets
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

import main
import partner_portal
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
    return tmp_path / "test_customer_setup_provisioning_retry_state.db"


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


def _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1", cloud_id=None, credential="token-appl-1"):
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
        (appliance_id, customer_id, "site-1", cloud_id or f"AIC-{appliance_id}", "activated", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
        (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-01-01"),
    )
    conn.commit()


def _appliance_auth_headers(appliance_id="appl-1", credential="token-appl-1"):
    return {
        "x-appliance-id": appliance_id,
        "x-request-timestamp": str(int(time.time())),
        "x-request-nonce": secrets.token_hex(16),
        "authorization": f"Bearer {credential}",
    }


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


@pytest.fixture(autouse=True)
def _seeded_customer(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    conn.close()


# --------------------------------------------------------- page wiring

def test_button_is_disabled_and_pending_immediately_on_click(http_client, db_path):
    """Prevents double-submission and stops the button from sitting in
    its original clickable state while a request is genuinely in
    flight -- distinct from the final "Added" state."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "button.disabled=true;button.textContent='Adding…'" in body


def test_page_polls_the_real_provisioning_outcome_instead_of_trusting_the_initial_200(http_client, db_path):
    """The exact live bug: a 200 from POST .../provision only means the
    job was queued, not that the camera was actually added -- must poll
    the job's real terminal outcome."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "async function pollProvisioning(jobId,button)" in body
    assert "/api/customer/camera-provisioning/${jobId}" in body
    assert "pollProvisioning(r.job_id,button)" in body


def test_added_label_is_reachable_only_from_the_provisioned_status(http_client, db_path):
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    poll_fn = body[body.index("async function pollProvisioning"):body.index("document.getElementById('scan-results').addEventListener")]
    assert "if(r.status==='provisioned'){" in poll_fn
    added_branch = poll_fn[poll_fn.index("if(r.status==='provisioned')"):poll_fn.index("if(r.status==='failed')")]
    assert "button.textContent='Added'" in added_branch
    assert "button.disabled=true" in added_branch


def test_failed_status_resets_the_button_to_a_retryable_state(http_client, db_path):
    """The exact reported symptom: a failed provisioning request must
    give the customer back a clickable "Add this camera" button, not
    leave it permanently stuck on "Added"."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    poll_fn = body[body.index("async function pollProvisioning"):body.index("document.getElementById('scan-results').addEventListener")]
    failed_branch = poll_fn[poll_fn.index("if(r.status==='failed')"):]
    assert "button.disabled=false" in failed_branch
    assert "button.textContent='Add this camera'" in failed_branch
    assert "showProvisioningError(r.message" in failed_branch


def test_failure_during_the_initial_queue_request_is_also_retryable(http_client, db_path):
    """A failure reaching POST .../provision at all (e.g. the 503 from
    a missing credential-encryption key) must reset the button just
    like a later async failure does -- not only a terminal 'failed'
    status from the polled job."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    click_handler = body[body.index("document.getElementById('scan-results').addEventListener"):]
    on_post_failure = click_handler[click_handler.index("if(!response.ok){"):click_handler.index("document.getElementById('scan-message').style.color=''")]
    assert "button.disabled=false" in on_post_failure
    assert "button.textContent='Add this camera'" in on_post_failure


# ------------------------------------------------- server-side outcome shape
#
# Confirms the exact data pollProvisioning() depends on: a failed
# appliance-side verification really does leave the job's own GET
# endpoint reporting status='failed' with the appliance's message, and
# never creates a camera row -- the real end-to-end path this UI fix
# now surfaces correctly instead of ignoring.

def test_real_failed_provisioning_leaves_a_retryable_no_camera_state(db_path, monkeypatch):
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    appliance_provisioning_jobs = _route("/api/appliance/{cloud_id}/provisioning-jobs", "GET")
    appliance_submit_provisioning = _route("/api/appliance/{cloud_id}/provisioning-jobs/{job_id}", "POST")
    camera_provisioning_status = _route("/api/customer/camera-provisioning/{job_id}", "GET")
    with override_target(sqlite_path=db_path):
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.com"})
        job = request_camera_provisioning(
            _fake_request(),
            {"appliance_id": "appl-1", "device_key": "urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af", "name": "Front Door"},
        )
        assert job["status"] == "queued"  # the initial response the old UI incorrectly treated as final
        appliance_provisioning_jobs(_fake_request(_appliance_auth_headers()), "AIC-appl-1")
        appliance_submit_provisioning(
            _fake_request(_appliance_auth_headers()), "AIC-appl-1", job["job_id"],
            {"success": False, "message": "Device rejected the provided credentials."},
        )
        status = camera_provisioning_status(_fake_request(), job["job_id"])
        conn = sqlite3.connect(db_path)
        camera_count = conn.execute("SELECT count(*) FROM cameras WHERE device_key='urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af'").fetchone()[0]
    assert status["status"] == "failed"
    assert status["message"] == "Device rejected the provided credentials."
    assert camera_count == 0
