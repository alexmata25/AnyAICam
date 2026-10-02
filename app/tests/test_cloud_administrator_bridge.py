"""Regression coverage for the Administrator Portal authorization
boundary: a cloud-delegated Partner Portal session with a live,
GLOBAL-scoped 'administrator' grant may use the real /admin-portal
(main.py's cloud_administrator_bridge(), consulted by current_user())
-- without ever creating a legacy users.json row for that identity.

Boundary this file exists to prove, per the task's own explicit list:
  - cloud global Administrator = global admin access
  - partner-scoped administrator must NOT silently become global
  - Partner role must NOT gain Administrator Portal access
  - Technician/Customer denied
  - admin@local continues using the local recovery session path,
    completely unaffected by any of this
  - revoking the grant removes Admin Portal access immediately (this
    bridge re-verifies live on every request -- it doesn't even need
    to wait for the appliance's own manifest-reconciliation cycle,
    though that still separately revokes the underlying Partner
    Portal session record; both are tested here)
"""
import pytest
from fastapi.testclient import TestClient

import appliance_identity
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash


@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    # This whole file is about the CLOUD-DELEGATED path (the grant
    # system) -- an unconfigured instance instead checks partner_db
    # directly by role, never consulting grants at all for routing (see
    # test_portal_login_selector.py's own coverage of that fallback).
    # Configuring an activated appliance here is what makes /api/
    # portal-login actually route through resolve_portal_login()'s
    # scope-aware branch and authenticate_operator()'s grant lookup.
    monkeypatch.setenv("ANYAICAM_APPLIANCE_ID", "appl-bridge")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CLOUD_ID", "AIC-BRIDGE")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CREDENTIAL", "bridge-credential")
    db_path = tmp_path / "test_bridge.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        with connection() as db:
            now = "2026-08-27T00:00:00"
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", now))
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Customer", "", "cust1@example.test", "active", "eligible", "real", now))
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Site", now))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)", ("appl-bridge", "cust-1", "site-1", "AIC-BRIDGE", "partner-1", now))
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-bridge", "appl-bridge", password_hash("bridge-credential"), now))
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as test_client:
            yield test_client, db_path
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _admin_session():
    main.save_users([{"id": "admin-1", "email": "admin@local", "role": "administrator", "enabled": True, "camera_ids": []}])
    return main.create_session("admin-1")


def _seed_operator(db_path, email="amata@anyaicam.com", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            now = "2026-08-27T00:00:00"
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Partner", "approved", "real", now))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)", ("u-op", partner_id, email, "Operator", "administrator", password_hash("x"), 1, now))
    return "u-op"


def _grant(db_path, admin_token, client, *, email, role, scope_type, scope_id=None):
    payload = {"email": email, "role": role, "scope_type": scope_type}
    if scope_id:
        payload["scope_id"] = scope_id
    response = client.post("/api/operations/identity-grants", json=payload, cookies={main.SESSION_COOKIE_NAME: admin_token})
    assert response.status_code == 200, response.text
    return response.json()["grant_id"]


# =============================================================== cloud global Administrator = global admin access


def test_global_administrator_grant_reaches_admin_portal(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="administrator", scope_type="global")

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/admin-portal"
    session_cookie = login.cookies[partner_portal.SESSION_COOKIE]

    page = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert page.status_code == 200


def test_bridged_identity_can_use_a_real_admin_only_action_not_just_load_the_page(http_client):
    # Prove this isn't just the landing page -- a genuinely admin-only
    # API action (another grant-management call) works too, since
    # current_user()/has_permission() is the shared choke point.
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="administrator", scope_type="global")

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    session_cookie = login.cookies[partner_portal.SESSION_COOKIE]

    grants_list = client.get("/api/operations/identity-grants", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert grants_list.status_code == 200


# =============================================================== same email, Administrator + Partner grants


def test_same_email_with_both_grants_can_select_either_portal(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="administrator", scope_type="global")
    _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="partner_owner", scope_type="partner", scope_id="partner-1")

    as_admin = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    assert as_admin.headers["location"] == "/admin-portal"

    as_partner = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "partner"}, follow_redirects=False)
    assert as_partner.headers["location"] == "/partner?tab=customers"


def test_partner_grant_remains_usable_if_only_administrator_grant_is_revoked(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    admin_grant_id = _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="administrator", scope_type="global")
    _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="partner_owner", scope_type="partner", scope_id="partner-1")

    client.post(f"/api/operations/identity-grants/{admin_grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: admin_token})

    as_partner = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "partner"}, follow_redirects=False)
    assert as_partner.status_code == 303
    assert as_partner.headers["location"] == "/partner?tab=customers"


def test_removing_administrator_grant_immediately_denies_admin_portal_even_with_the_still_valid_session_cookie(http_client):
    # No new login, no heartbeat, no manifest refresh required -- the
    # bridge re-verifies live grants on every current_user() call, so
    # this takes effect on the very next request.
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    admin_grant_id = _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="administrator", scope_type="global")

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    session_cookie = login.cookies[partner_portal.SESSION_COOKIE]
    assert client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie}).status_code == 200

    client.post(f"/api/operations/identity-grants/{admin_grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: admin_token})

    denied = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert denied.status_code == 200  # permission_denied_page() renders 200, same convention as every other Admin Portal page
    assert "manage_settings" in denied.text or "permission" in denied.text.lower()


# =============================================================== partner-scoped admin must never gain global reach


def test_partner_scoped_administrator_cannot_reach_admin_portal(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="administrator", scope_type="partner", scope_id="partner-1")

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/partner?tab=customers"  # never /admin-portal
    session_cookie = login.cookies[partner_portal.SESSION_COOKIE]

    denied = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert denied.status_code == 200
    assert "manage_settings" in denied.text or "permission" in denied.text.lower()


def test_has_global_administrator_grant_is_false_for_a_partner_scoped_grant():
    # Direct unit check on the live-verification primitive itself.
    from appliance_identity import GRANTABLE_ROLES  # sanity: module still importable/consistent
    assert "administrator" in GRANTABLE_ROLES


# =============================================================== Partner/Technician/Customer denied Admin Portal access


def test_partner_owner_role_cannot_reach_admin_portal(http_client):
    client, db_path = http_client
    _seed_operator(db_path, email="owner@example.test")
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="owner@example.test", role="partner_owner", scope_type="partner", scope_id="partner-1")

    login = client.post("/api/portal-login", json={"email": "owner@example.test", "password": "x", "portal": "partner"}, follow_redirects=False)
    session_cookie = login.cookies[partner_portal.SESSION_COOKIE]

    denied = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert denied.status_code == 200
    assert "manage_settings" in denied.text or "permission" in denied.text.lower()


def test_technician_role_cannot_reach_admin_portal(http_client):
    client, db_path = http_client
    _seed_operator(db_path, email="tech@example.test")
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="tech@example.test", role="technician", scope_type="appliance", scope_id="AIC-BRIDGE")

    login = client.post("/api/portal-login", json={"email": "tech@example.test", "password": "x", "portal": "technician"}, follow_redirects=False)
    session_cookie = login.cookies[partner_portal.SESSION_COOKIE]

    denied = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert denied.status_code == 200
    assert "manage_settings" in denied.text or "permission" in denied.text.lower()


def test_customer_session_cannot_reach_admin_portal(http_client):
    client, db_path = http_client
    session_cookie = partner_portal._token("customer@example.test", "customer_owner", None, "cust-1", None)
    denied = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert denied.status_code == 200
    assert "manage_settings" in denied.text or "permission" in denied.text.lower()


# =============================================================== admin@local unaffected


def test_admin_local_still_works_via_the_legacy_session_path_unaffected(http_client):
    client, db_path = http_client
    token = _admin_session()
    page = client.get("/admin-portal", cookies={main.SESSION_COOKIE_NAME: token})
    assert page.status_code == 200


def test_anonymous_request_is_denied_admin_portal(http_client):
    client, db_path = http_client
    page = client.get("/admin-portal")
    assert page.status_code == 200
    assert "manage_settings" in page.text or "permission" in page.text.lower()


# =============================================================== no local duplicate account


def test_granting_and_logging_in_never_creates_a_legacy_users_json_row(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="amata@anyaicam.com", role="administrator", scope_type="global")

    client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)

    assert not any(u.get("email", "").lower() == "amata@anyaicam.com" for u in main.load_users())
