"""Regression coverage for two confirmed-live Samsung bugs, reported
back-to-back while onboarding a real customer with 5 discovered
cameras:

1. Refreshing /customer/setup always reset the wizard to Step 1, even
   though POST /api/customer/setup/progress faithfully persists
   current_step and the selected appliance_id into
   customer_setup_drafts on every "Save and continue" click. Nothing
   server-side ever read that draft back when rendering the page --
   the initial JS state (`let setupStep=1`) and the appliance <select>
   were both hardcoded regardless of saved progress.

2. The Discover step (Step 4) never showed a completed scan job's
   candidates again once the page was reopened -- `scanJob` started
   `null` on every load and was only ever set by the "Request
   appliance scan" button's own click handler. Worse, even trusting
   the draft's own saved scan_job field would not have helped: it was
   confirmed live to still read null after a real job had completed,
   because the client only ever writes whatever its in-memory variable
   happens to hold at the moment "Save and continue" is clicked -- not
   necessarily the job that finished. Fixed with a new read-only
   endpoint, GET /api/customer/appliances/{id}/scans/latest, that looks
   up camera_scan_jobs directly (the real source of truth) and a
   loadLatestScan() the page calls on load and whenever the appliance
   selection changes.

A third, closely related bug surfaced while tracing why "Add this
camera" would still fail even once Step 4 correctly redisplayed
discovered candidates: request_camera_provisioning() (partner_
workspace.py) requires appliance_id in the POST body, but the
"Add this camera" button's fetch never included it -- so clicking it
would 400 unconditionally, independent of the other two bugs. Fixed by
adding appliance_id:selectedAppliance() to that payload.

None of these fixes touch scan-job creation, camera/network settings,
or manually insert any camera row -- they only rehydrate existing,
already-saved state and correct a request payload.
"""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import main
import partner_portal
import partner_workspace
from database_backend import override_target
from partner_db import initialize_database


def _route(path, method="GET"):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or {method}):
            return r.endpoint
    raise AssertionError(f"no route registered for {method} {path}")


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_setup_wizard_rehydration.db"


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


def _seed_scan_job(conn, job_id, *, customer_id="cust-1", appliance_id="appl-1", created_at, status="complete", results=None):
    conn.execute(
        "INSERT INTO camera_scan_jobs(id,customer_id,appliance_id,status,progress,results_json,message,created_at,updated_at) "
        "VALUES(?,?,?,?,100,?,?,?,?)",
        (job_id, customer_id, appliance_id, status, json.dumps(results or []), "Found devices.", created_at, created_at),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _fake_request(headers=None):
    from types import SimpleNamespace
    return SimpleNamespace(headers=headers or {}, cookies={}, query_params=SimpleNamespace(get=lambda k, d=None: d))


# ----------------------------------------------------- wizard step rehydration

def test_setup_page_defaults_to_step_1_without_a_saved_draft(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "let setupStep=1," in response.text


def test_setup_page_rehydrates_the_saved_step_on_reopen(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    conn.execute(
        "INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES(?,?,?,?)",
        ("cust-1", 4, json.dumps({"appliance_id": "appl-1", "scan_job": None}), "2026-08-29T00:00:00"),
    )
    conn.commit()
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "let setupStep=4," in response.text


def test_setup_page_rehydrates_the_saved_appliance_selection(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn, appliance_id="appl-1", cloud_id="AIC-FIRST")
    _seed_appliance(conn, appliance_id="appl-2", cloud_id="AIC-SECOND")
    conn.execute(
        "INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES(?,?,?,?)",
        ("cust-1", 4, json.dumps({"appliance_id": "appl-2", "scan_job": None}), "2026-08-29T00:00:00"),
    )
    conn.commit()
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert 'value="appl-2" selected' in response.text
    assert 'value="appl-1" selected' not in response.text


def test_setup_page_ignores_a_draft_appliance_id_that_no_longer_exists(http_client, db_path):
    """Defensive: a stale/deleted appliance_id in the draft must not
    crash the page or leave nothing selected -- falls back to the
    first real appliance on this account."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn, appliance_id="appl-1", cloud_id="AIC-ONLY")
    conn.execute(
        "INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES(?,?,?,?)",
        ("cust-1", 4, json.dumps({"appliance_id": "appl-deleted", "scan_job": None}), "2026-08-29T00:00:00"),
    )
    conn.commit()
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'value="appl-1" selected' in response.text


# ------------------------------------------------------- latest-scan endpoint

def test_latest_scan_endpoint_returns_none_when_no_job_exists(db_path, monkeypatch):
    latest_scan = _route("/api/customer/appliances/{appliance_id}/scans/latest", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = latest_scan(_fake_request(), "appl-1")
    assert result == {"job_id": None}


def test_latest_scan_endpoint_finds_a_completed_job_without_creating_a_new_one(db_path, monkeypatch):
    latest_scan = _route("/api/customer/appliances/{appliance_id}/scans/latest", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn)
        _seed_scan_job(conn, "job-old", created_at="2026-08-28T00:00:00", results=[{"device_key": "onvif-1"}])
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = latest_scan(_fake_request(), "appl-1")
        remaining_jobs = conn.execute("SELECT count(*) FROM camera_scan_jobs").fetchone()[0]
    assert result == {"job_id": "job-old"}
    assert remaining_jobs == 1  # no new scan job was created by looking this up


def test_latest_scan_endpoint_returns_the_most_recent_job_when_several_exist(db_path, monkeypatch):
    latest_scan = _route("/api/customer/appliances/{appliance_id}/scans/latest", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn)
        _seed_scan_job(conn, "job-old", created_at="2026-08-27T00:00:00")
        _seed_scan_job(conn, "job-new", created_at="2026-08-29T00:00:00")
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        result = latest_scan(_fake_request(), "appl-1")
    assert result == {"job_id": "job-new"}


def test_latest_scan_endpoint_is_scoped_to_the_caller_customer(db_path, monkeypatch):
    latest_scan = _route("/api/customer/appliances/{appliance_id}/scans/latest", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn, customer_id="cust-1")
        _seed_tenant(conn, customer_id="cust-2")
        _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1")
        _seed_scan_job(conn, "job-1", customer_id="cust-1", appliance_id="appl-1", created_at="2026-08-29T00:00:00")
        # A different customer must not be able to read cust-1's appliance's
        # latest scan job just by guessing its appliance_id.
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity("cust-2"))
        with pytest.raises(Exception) as excinfo:
            latest_scan(_fake_request(), "appl-1")
    assert getattr(excinfo.value, "status_code", None) == 404


def test_latest_scan_endpoint_404s_for_an_unknown_appliance(db_path, monkeypatch):
    latest_scan = _route("/api/customer/appliances/{appliance_id}/scans/latest", "GET")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(Exception) as excinfo:
            latest_scan(_fake_request(), "no-such-appliance")
    assert getattr(excinfo.value, "status_code", None) == 404


# ------------------------------------------------- page wiring (source-text)
#
# The behavior above is only reachable if the page's own script actually
# calls the new endpoint and sends the fields the provisioning route
# requires. Verified directly against the rendered page here rather than
# only unit-testing the route in isolation, since the bug that shipped
# live was entirely in this wiring, not in any one route's logic.

def test_setup_page_loads_the_latest_scan_on_open_and_on_appliance_change(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "async function loadLatestScan()" in body
    assert "/scans/latest" in body
    assert "showSetup();loadLatestScan();" in body  # called once on page bootstrap
    assert "onchange=async()=>{await loadLatestScan();showSetup()}" in body  # and on appliance change


def test_setup_page_sends_appliance_id_when_provisioning_a_discovered_camera(http_client, db_path):
    """The exact third bug: without this field, POST /api/customer/
    cameras/provision 400s unconditionally ('appliance_id and
    device_key are required'), so "Add this camera" could never have
    worked even once Step 4 correctly redisplayed candidates."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "appliance_id:selectedAppliance(),device_key:deviceKey" in response.text


def test_provision_camera_request_still_requires_appliance_id_server_side(db_path, monkeypatch):
    """Confirms the server-side contract the client must satisfy --
    kept here so a future change to either side has to update both."""
    request_camera_provisioning = _route("/api/customer/cameras/provision", "POST")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_appliance(conn)
        monkeypatch.setattr(partner_workspace, "partner_identity", lambda request: _owner_identity())
        with pytest.raises(Exception) as excinfo:
            request_camera_provisioning(_fake_request(), {"device_key": "onvif-1", "name": "Camera"})
    assert getattr(excinfo.value, "status_code", None) == 400
