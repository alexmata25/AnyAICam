"""Regression coverage for a confirmed-live Samsung bug found while
diagnosing why "Add this camera" appeared to do nothing: the appliance
had no ANYAICAM_CAMERA_CREDENTIAL_KEY configured (see the installer fix
in 06-deploy-vms.sh's ensure_vms_env() and validate.sh), so submitting a
camera WITH credentials always 503'd on request_camera_provisioning()
(partner_workspace.py) before ever storing anything. The click handler
DID call showToast(r.detail) on failure, but a toast auto-hides after
~3 seconds and left no lasting trace -- read-only verification (audit_
logs, camera_provisioning_requests, cameras) was the only way to
confirm the submission had actually failed at all. Fixed so a failed
"Add this camera" attempt also writes a persistent, visibly-styled
error into the Discover step's own status line (#scan-message, the
same element normal scan progress already uses), not just a fleeting
toast -- and so a non-JSON/network-level failure (the fetch or
response.json() call itself throwing) is caught and shown the same way
instead of surfacing as an unhandled promise rejection with no visible
UI feedback.

No credential or secret value is ever put into this error message --
only whatever safe, generic `detail` string the server already returns
(e.g. request_camera_provisioning()'s existing 503 text), or a generic
fallback naming the HTTP status when the server didn't return one.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_setup_provisioning_error_display.db"


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


def _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1", cloud_id=None):
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
        (appliance_id, customer_id, "site-1", cloud_id or f"AIC-{appliance_id}", "activated", "2026-01-01"),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


@pytest.fixture(autouse=True)
def _seeded_customer(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    conn.close()


def test_provisioning_failure_shows_a_persistent_error_not_only_a_toast(http_client, db_path):
    """The exact live gap: showToast() alone was too easy to miss, and
    left nothing to read back afterward -- must also write into the
    persistent #scan-message status line."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "function showProvisioningError(message)" in body
    assert "document.getElementById('scan-message')" in body
    assert "showProvisioningError(r.detail||" in body


def test_provisioning_network_or_parse_failure_is_also_surfaced(http_client, db_path):
    """A non-JSON/network-level failure (fetch or response.json() itself
    throwing) must not silently vanish as an unhandled promise
    rejection -- caught and routed through the same visible error path."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "try{response=await fetch('/api/customer/cameras/provision'" in body
    assert "}catch(error){return showProvisioningError(" in body


def test_provisioning_error_message_never_carries_credential_fields(http_client, db_path):
    """The error path must only ever display the server's own safe
    `detail` string or a generic fallback -- never echo back the
    username/password the customer just typed. Checked directly against
    the exact statement that builds the displayed error, not a loose
    substring search over the whole handler (which legitimately
    mentions `username`/`password` earlier, when building the request)."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    error_call_start = body.index("if(!response.ok)return showProvisioningError(")
    error_call_statement = body[error_call_start:body.index(";", error_call_start)]
    assert "username" not in error_call_statement
    assert "password" not in error_call_statement


def test_successful_provisioning_still_clears_any_prior_error_styling(http_client, db_path):
    """A later successful "Add this camera" must not leave a stale red
    error message/style behind from an earlier failed attempt."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "document.getElementById('scan-message').style.color=''" in body


def test_real_503_detail_text_is_exactly_what_would_be_displayed(db_path, monkeypatch):
    """Confirms the server-side message this UI fix actually surfaces is
    itself safe to show a customer verbatim -- generic, no secret or
    internal detail, matching the confirmed-live 503 from the missing
    ANYAICAM_CAMERA_CREDENTIAL_KEY case this whole investigation started
    from."""
    import partner_workspace
    from types import SimpleNamespace

    def _route(path, method="GET"):
        for r in main.app.routes:
            if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
                return r.endpoint
        raise AssertionError(f"no route registered for {method} {path}")

    def _fake_request(headers=None):
        return SimpleNamespace(headers=headers or {}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))

    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    monkeypatch.delenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", raising=False)
    with override_target(sqlite_path=db_path):
        # The autouse _seeded_customer fixture already seeded cust-1 /
        # appl-1 against this same db_path via the http_client fixture.
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.com"})
        with pytest.raises(Exception) as excinfo:
            request_camera_provisioning(
                _fake_request(),
                {"appliance_id": "appl-1", "device_key": "onvif-1", "name": "Front Door", "username": "admin", "password": "hunter2"},
            )
    assert excinfo.value.status_code == 503
    assert "hunter2" not in str(excinfo.value.detail)
    assert "admin" not in str(excinfo.value.detail)
