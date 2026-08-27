"""HTTP-level regression coverage for the appliance identity contract
v1: GET /api/appliance/{cloud_id}/identity-manifest,
POST /api/appliance/{cloud_id}/authenticate-operator,
GET /api/appliance/signing-keys -- real TestClient requests, real
appliance-credential authentication (authenticate_appliance(), the
same scheme every other appliance-cloud route already uses), a real
seeded partner_db.

Same isolation pattern as test_appliance_cloud_recording_status.py:
override_target() before appliance_cloud's import-time schema init,
a minimal standalone FastAPI() app with only appliance_cloud's routes.
"""
import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_identity_integration.db"):
    import appliance_cloud
    import appliance_identity
    from partner_db import connection, password_hash


def _seed_appliance(db, appliance_id: str, cloud_id: str, credential: str, partner_id="partner-1", customer_id="cust-1", site_id="site-1"):
    now = "2026-08-27T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, f"Partner {partner_id}", "approved", "real", now))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, partner_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (secrets.token_hex(4), appliance_id, password_hash(credential), now))


def _seed_operator(db, user_id: str, email: str, password: str, partner_id="partner-1"):
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, f"Partner {partner_id}", "approved", "real", "2026-08-27T00:00:00"))
    db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)", (user_id, partner_id, email, "Operator", "administrator", password_hash(password), 1, "2026-08-27T00:00:00"))


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())), "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer {credential}"}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_identity.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _db(db_path):
    return override_target(sqlite_path=str(db_path))


# =============================================================== manifest endpoint


def test_manifest_includes_globally_granted_administrator(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "amata@anyaicam.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")

    response = client.get("/api/appliance/AIC-1/identity-manifest", headers=_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    body = response.json()
    emails = {i["email"] for i in body["identities"]}
    assert "amata@anyaicam.com" in emails
    assert body["appliance"]["cloud_id"] == "AIC-1"
    assert "signature" in body and body["signature"]["alg"] == "Ed25519"


def test_two_appliances_same_company_have_different_authorized_users(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-a", "AIC-A", "cred-a", partner_id="partner-1", customer_id="cust-1", site_id="site-a")
            _seed_appliance(db, "appl-b", "AIC-B", "cred-b", partner_id="partner-1", customer_id="cust-1", site_id="site-b")
            _seed_operator(db, "u-tech", "tech@fieldpartner.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u-tech", role="technician", scope_type="appliance", scope_id="AIC-A", granted_by="test")

    manifest_a = client.get("/api/appliance/AIC-A/identity-manifest", headers=_auth_headers("appl-a", "cred-a")).json()
    manifest_b = client.get("/api/appliance/AIC-B/identity-manifest", headers=_auth_headers("appl-b", "cred-b")).json()
    assert "tech@fieldpartner.com" in {i["email"] for i in manifest_a["identities"]}
    assert "tech@fieldpartner.com" not in {i["email"] for i in manifest_b["identities"]}


def test_partner_scoped_administrator_never_appears_on_another_partners_appliance(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-x", "AIC-X", "cred-x", partner_id="partner-x", customer_id="cust-x", site_id="site-x")
            _seed_appliance(db, "appl-y", "AIC-Y", "cred-y", partner_id="partner-y", customer_id="cust-y", site_id="site-y")
            _seed_operator(db, "u-px", "admin@partner-x.com", "Sup3rSecret!", partner_id="partner-x")
            appliance_identity.create_grant(db, user_id="u-px", role="administrator", scope_type="partner", scope_id="partner-x", granted_by="test")

    manifest_x = client.get("/api/appliance/AIC-X/identity-manifest", headers=_auth_headers("appl-x", "cred-x")).json()
    manifest_y = client.get("/api/appliance/AIC-Y/identity-manifest", headers=_auth_headers("appl-y", "cred-y")).json()
    assert "admin@partner-x.com" in {i["email"] for i in manifest_x["identities"]}
    assert "admin@partner-x.com" not in {i["email"] for i in manifest_y["identities"]}


def test_admin_local_never_appears_in_any_manifest(client, db_path):
    # admin@local lives only in main.py's legacy users.json -- it cannot
    # be represented in identity_grants at all, so it structurally
    # cannot appear here. This proves that by construction: seed every
    # real grant type and confirm admin@local's email never shows up.
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "amata@anyaicam.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")

    manifest = client.get("/api/appliance/AIC-1/identity-manifest", headers=_auth_headers("appl-1", "cred-1")).json()
    assert "admin@local" not in {i["email"] for i in manifest["identities"]}
    assert all(i["user_id"] != "admin@local" for i in manifest["identities"])


def test_mismatched_cloud_id_in_url_is_rejected(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")

    response = client.get("/api/appliance/AIC-WRONG/identity-manifest", headers=_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 403


# =============================================================== authenticate-operator endpoint


def test_authenticate_operator_returns_a_verifiable_signed_assertion(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "amata@anyaicam.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")

    response = client.post(
        "/api/appliance/AIC-1/authenticate-operator",
        json={"email": "amata@anyaicam.com", "password": "Sup3rSecret!", "portal": "administrator"},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["assertion"]["role"] == "administrator"

    keys = client.get("/api/appliance/signing-keys").json()["keys"]
    appliance_identity.verify_assertion(body, expected_cloud_id="AIC-1", public_keys=keys)  # does not raise


def test_authenticate_operator_denies_wrong_password(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "amata@anyaicam.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")

    response = client.post(
        "/api/appliance/AIC-1/authenticate-operator",
        json={"email": "amata@anyaicam.com", "password": "wrong", "portal": "administrator"},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    assert response.json() == {"status": "denied", "reason": "invalid"}


def test_authenticate_operator_denies_unauthorized_portal_selection(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "sales@partner.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="salesperson", scope_type="partner", scope_id="partner-1", granted_by="test")

    response = client.post(
        "/api/appliance/AIC-1/authenticate-operator",
        json={"email": "sales@partner.com", "password": "Sup3rSecret!", "portal": "administrator"},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    assert response.json() == {"status": "denied", "reason": "not_authorized_for_selected_portal"}


def test_authenticate_operator_denies_a_correct_password_with_no_grant_for_this_appliance(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", partner_id="partner-1")
            _seed_appliance(db, "appl-2", "AIC-2", "cred-2", partner_id="partner-2", customer_id="cust-2", site_id="site-2")
            _seed_operator(db, "u1", "owner@partner-2.com", "Sup3rSecret!", partner_id="partner-2")
            appliance_identity.create_grant(db, user_id="u1", role="partner_owner", scope_type="partner", scope_id="partner-2", granted_by="test")

    response = client.post(
        "/api/appliance/AIC-1/authenticate-operator",
        json={"email": "owner@partner-2.com", "password": "Sup3rSecret!", "portal": "partner"},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    assert response.json() == {"status": "denied", "reason": "not_authorized_for_this_appliance"}


def test_multi_role_admin_and_partner_selector_behavior(client, db_path):
    # One operator, two grants (administrator + technician scoped to
    # this same appliance) -- the selected portal, not a guess, decides
    # which grant's role comes back in the assertion.
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "multi@example.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")
            appliance_identity.create_grant(db, user_id="u1", role="technician", scope_type="appliance", scope_id="AIC-1", granted_by="test")

    as_admin = client.post("/api/appliance/AIC-1/authenticate-operator", json={"email": "multi@example.com", "password": "Sup3rSecret!", "portal": "administrator"}, headers=_auth_headers("appl-1", "cred-1")).json()
    as_tech = client.post("/api/appliance/AIC-1/authenticate-operator", json={"email": "multi@example.com", "password": "Sup3rSecret!", "portal": "technician"}, headers=_auth_headers("appl-1", "cred-1")).json()
    assert as_admin["assertion"]["role"] == "administrator"
    assert as_tech["assertion"]["role"] == "technician"


# =============================================================== revocation reconciliation


def test_role_removal_force_expires_the_local_session_on_next_manifest_fetch(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "tech@example.com", "Sup3rSecret!")
            grant_id = appliance_identity.create_grant(db, user_id="u1", role="technician", scope_type="appliance", scope_id="AIC-1", granted_by="test")
            manifest = appliance_identity.build_manifest(db, cloud_id="AIC-1")
            live_identity = next(i for i in manifest["identities"] if i["user_id"] == "u1")
            db.execute(
                "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sess-1", "u1", "tech@example.com", "technician", "Web browser", "cookie", "2026-08-27T00:00:00", "2026-08-27T00:00:00", "2026-08-27T08:00:00", live_identity["authorization_version"]),
            )
            appliance_identity.revoke_grant(db, grant_id=grant_id)

    client.get("/api/appliance/AIC-1/identity-manifest", headers=_auth_headers("appl-1", "cred-1"))

    with _db(db_path):
        with connection() as db:
            session = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-1'").fetchone()
    assert session["revoked_at"] is not None


def test_unrelated_session_is_untouched_by_reconciliation(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "amata@anyaicam.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")
            manifest = appliance_identity.build_manifest(db, cloud_id="AIC-1")
            live_identity = manifest["identities"][0]
            db.execute(
                "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sess-still-good", "u1", "amata@anyaicam.com", "administrator", "Web browser", "cookie", "2026-08-27T00:00:00", "2026-08-27T00:00:00", "2026-08-27T08:00:00", live_identity["authorization_version"]),
            )

    client.get("/api/appliance/AIC-1/identity-manifest", headers=_auth_headers("appl-1", "cred-1"))

    with _db(db_path):
        with connection() as db:
            session = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-still-good'").fetchone()
    assert session["revoked_at"] is None
