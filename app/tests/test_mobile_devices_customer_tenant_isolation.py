"""Regression coverage for a cross-tenant data leak on the customer-facing
"Mobile devices" page and its /api/mobile/* endpoints (2026-09-23):

create_mobile_pairing() (POST /api/mobile/pairing), mobile_devices_api()
(GET /api/mobile/devices), and update_mobile_device() (PUT /api/mobile/
devices/{id}) all called current_user(request) alone -- the legacy
local-VMS session cookie (anyaicam_session), never partner_identity()'s
partner-portal cookie (anyaicam_partner_session) that a real customer_
owner/customer_viewer session actually carries. This is the exact same
auth-mismatch class subscription_portal_page's own 2026-09-21 fix already
documents, just not yet applied here.

Confirmed live: current_user() fell through to its anonymous fallback
({"id": "anonymous", "role": "viewer", ...}) for a real customer session
on portal-staging.anyaicam.com. Since mobile_device_owner_matches() only
checks device.get("user_id") == user.get("id"), and every real customer
session produced the identical "anonymous" id, this meant:
  - Every pairing code any customer generated was stamped user_id=
    "anonymous", not that customer.
  - GET /api/mobile/devices returned every "anonymous"-owned device to
    every customer session -- i.e. every real customer's paired phones,
    to every other real customer.
  - PUT /api/mobile/devices/{id} (revoke / toggle notifications) would
    let any customer manage any other customer's paired device.

Fixed via a new _mobile_devices_identity(request) helper: a real
customer_owner/customer_viewer session (detected via partner_identity())
is scoped by "customer:<customer_id>" instead; every other session kind
(local/staff, or no session at all) falls through to the exact original
current_user(request) call, unchanged -- this same JSON-file-backed
feature also serves the local single-tenant VMS's own device-pairing
flow, which this fix must not touch.

mobile_devices.json/mobile_pairing_codes.json are real files at
RECORDINGS_FOLDER, not part of the sqlite backend override_target()
isolates -- so MOBILE_DEVICES_FILE/MOBILE_PAIRING_CODES_FILE are
monkeypatched to a per-test tmp_path for isolation, matching the
per-test db_path convention used elsewhere in this suite.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_mobile_devices_tenant_isolation.db"


@pytest.fixture(autouse=True)
def _isolated_mobile_files(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "MOBILE_DEVICES_FILE", tmp_path / "mobile_devices.json")
    monkeypatch.setattr(main, "MOBILE_PAIRING_CODES_FILE", tmp_path / "mobile_pairing_codes.json")


@pytest.fixture()
def http_client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://portal-staging.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )


def _seed(db_path, *customer_ids):
    conn = sqlite3.connect(db_path)
    for customer_id in customer_ids:
        _seed_tenant(conn, customer_id)
    conn.commit()
    conn.close()


def _owner_cookie(customer_id, email="owner@example.test"):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator")


def _create_pairing(client, cookies, device_name="Test Phone"):
    response = client.post("/api/mobile/pairing", json={"device_name": device_name}, cookies=cookies)
    assert response.status_code == 200
    return response.json()["pairing"]


def _claim(client, code, cookies, device_name="Test Phone"):
    response = client.post("/api/mobile/pairing/claim", json={"code": code, "device_name": device_name}, cookies=cookies)
    assert response.status_code == 200
    return response.json()["device"]


# --------------------------------------------------------------- identity


def test_pairing_code_created_by_a_real_customer_is_scoped_to_that_customer(http_client, db_path):
    """The core fix: not "anonymous"."""
    _seed(db_path, "cust-1")
    pairing = _create_pairing(http_client, {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert pairing["user_id"] == "customer:cust-1"
    assert pairing["user_id"] != "anonymous"


def test_non_customer_session_falls_through_to_current_user_unaffected(http_client, db_path):
    """A staff/administrator partner-portal session (a real role, just
    not customer_owner/customer_viewer) must still fall through to the
    exact original current_user(request) call -- which itself checks a
    different cookie (anyaicam_session) an administrator session doesn't
    carry either, so this still resolves to current_user()'s own
    anonymous fallback, unchanged. Proves the fix is additive to the
    customer_owner/customer_viewer branch only, never a rewrite of
    current_user()'s own fallback semantics for every other session
    kind. (POST /api/mobile/pairing requires some authenticated partner-
    portal session to reach the route body at all -- a separate,
    pre-existing, unrelated middleware gate -- so a truly cookie-less
    request can't reach this code path to begin with.)"""
    pairing = _create_pairing(http_client, {partner_portal.SESSION_COOKIE: _admin_cookie()})
    assert pairing["user_id"] == "anonymous"


# ------------------------------------------------------------- isolation


@pytest.mark.parametrize("customer_a,customer_b", [("cust-a", "cust-b"), ("acct-11", "acct-42")])
def test_two_customers_never_see_each_others_paired_devices(http_client, db_path, customer_a, customer_b):
    _seed(db_path, customer_a, customer_b)
    cookie_a = {partner_portal.SESSION_COOKIE: _owner_cookie(customer_a)}
    cookie_b = {partner_portal.SESSION_COOKIE: _owner_cookie(customer_b)}

    pairing = _create_pairing(http_client, cookie_a, device_name="Customer A's Phone")
    _claim(http_client, pairing["code"], cookie_a, device_name="Customer A's Phone")

    devices_a = http_client.get("/api/mobile/devices", cookies=cookie_a).json()["devices"]
    devices_b = http_client.get("/api/mobile/devices", cookies=cookie_b).json()["devices"]

    assert any(d["device_name"] == "Customer A's Phone" for d in devices_a)
    assert not any(d["device_name"] == "Customer A's Phone" for d in devices_b)
    assert devices_b == []


def test_a_customer_cannot_revoke_another_customers_device(http_client, db_path):
    _seed(db_path, "cust-a", "cust-b")
    cookie_a = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}
    cookie_b = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b")}

    pairing = _create_pairing(http_client, cookie_a)
    device = _claim(http_client, pairing["code"], cookie_a)

    response = http_client.put(f"/api/mobile/devices/{device['id']}", json={"revoked": True}, cookies=cookie_b)
    assert response.status_code == 403


def test_a_customer_can_manage_their_own_device(http_client, db_path):
    _seed(db_path, "cust-a")
    cookie_a = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}

    pairing = _create_pairing(http_client, cookie_a)
    device = _claim(http_client, pairing["code"], cookie_a)

    response = http_client.put(f"/api/mobile/devices/{device['id']}", json={"revoked": True}, cookies=cookie_a)
    assert response.status_code == 200
    assert response.json()["device"]["revoked"] is True

    devices = http_client.get("/api/mobile/devices", cookies=cookie_a).json()["devices"]
    assert next(d for d in devices if d["id"] == device["id"])["revoked"] is True


def test_a_customer_viewer_shares_the_owners_device_scope(http_client, db_path):
    """Device management here is account-level (matching how cameras and
    every other customer resource in this app are shared across an
    owner and its viewers), not siloed per individual login."""
    _seed(db_path, "cust-a")
    owner_cookie = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}
    viewer_cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("viewer@example.test", "customer_viewer", None, "cust-a", None)}

    pairing = _create_pairing(http_client, owner_cookie, device_name="Owner's Phone")
    _claim(http_client, pairing["code"], owner_cookie, device_name="Owner's Phone")

    devices = http_client.get("/api/mobile/devices", cookies=viewer_cookie).json()["devices"]
    assert any(d["device_name"] == "Owner's Phone" for d in devices)
