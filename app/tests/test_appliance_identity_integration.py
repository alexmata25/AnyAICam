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


def test_role_removal_still_force_expires_the_session_under_option_b(client, db_path):
    # 2026-09-04, Option B: restores this test's original assertion.
    # Option A (shipped first, same day, as the immediate mitigation)
    # had disabled reconcile_sessions_against_manifest() entirely, so
    # this test briefly asserted the session survived -- see git
    # history for that version. That was never the intended end state:
    # a role removal force-expiring the now-stale session is exactly
    # the security property this whole mechanism exists for. Option B
    # (appliance_identity._candidate_session_user_ids_for_appliance())
    # scopes the underlying query correctly instead of disabling it, so
    # this genuinely-affected session (the grant that gets revoked
    # below is scoped directly to THIS appliance) is force-expired
    # again, while test_a_genuinely_unrelated_ungranted_customer_
    # session_survives_both_endpoints (below) proves an unrelated one
    # still never is.
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


def _heartbeat_payload(cached_manifest_version=None):
    payload = {"uptime_seconds": 1000, "cpu": 10, "memory": 20, "camera_count": 0}
    if cached_manifest_version is not None:
        payload["cached_manifest_version"] = cached_manifest_version
    return payload


def test_heartbeat_ack_carries_current_manifest_version(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "amata@anyaicam.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")

    response = client.post("/api/appliance/heartbeat", json=_heartbeat_payload(), headers=_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    assert response.json()["current_manifest_version"] > 0


def test_heartbeat_with_matching_cached_version_does_not_refresh(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "amata@anyaicam.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")
            live_version = appliance_identity.current_manifest_version(db, cloud_id="AIC-1")

    response = client.post("/api/appliance/heartbeat", json=_heartbeat_payload(cached_manifest_version=live_version), headers=_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    assert response.json()["manifest_refreshed"] is False
    assert response.json()["current_manifest_version"] == live_version


def test_heartbeat_with_stale_cached_version_still_revokes_under_option_b(client, db_path):
    # 2026-09-04, Option B: restores this test's original assertion --
    # see test_role_removal_still_force_expires_the_session_under_
    # option_b()'s comment above for the full Option A -> Option B
    # lineage. manifest_refreshed correctly reports True here exactly
    # as it always has (untouched by either option).
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "tech@example.com", "Sup3rSecret!")
            grant_id = appliance_identity.create_grant(db, user_id="u1", role="technician", scope_type="appliance", scope_id="AIC-1", granted_by="test")
            manifest = appliance_identity.build_manifest(db, cloud_id="AIC-1")
            live_identity = next(i for i in manifest["identities"] if i["user_id"] == "u1")
            stale_version = live_identity["authorization_version"]
            db.execute(
                "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sess-hb-1", "u1", "tech@example.com", "technician", "Web browser", "cookie", "2026-08-27T00:00:00", "2026-08-27T00:00:00", "2026-08-27T08:00:00", stale_version),
            )
            appliance_identity.revoke_grant(db, grant_id=grant_id)  # role removed -- session should not survive the next heartbeat

    response = client.post("/api/appliance/heartbeat", json=_heartbeat_payload(cached_manifest_version=stale_version), headers=_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    assert response.json()["manifest_refreshed"] is True

    with _db(db_path):
        with connection() as db:
            session = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-hb-1'").fetchone()
    assert session["revoked_at"] is not None


def test_heartbeat_creates_the_audit_entry_only_for_a_genuinely_affected_session(client, db_path):
    # Direct regression guard for the live symptom (47 audit_logs rows
    # of action='appliance.sessions_revoked_on_manifest_refresh',
    # trigger='heartbeat' in under a day of production traffic, every
    # customer_owner session revoked within 8-74s of login) -- proves
    # the fix via the exact signal that was flooding the audit log, not
    # just via user_sessions.revoked_at. The genuinely-affected session
    # (role removed, scoped directly to this appliance) DOES still
    # produce exactly one audit entry -- the intended behavior,
    # restored -- while a second, unrelated, ungranted customer session
    # seeded alongside it produces none.
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_operator(db, "u1", "tech@example.com", "Sup3rSecret!")
            grant_id = appliance_identity.create_grant(db, user_id="u1", role="technician", scope_type="appliance", scope_id="AIC-1", granted_by="test")
            manifest = appliance_identity.build_manifest(db, cloud_id="AIC-1")
            live_identity = next(i for i in manifest["identities"] if i["user_id"] == "u1")
            stale_version = live_identity["authorization_version"]
            db.execute(
                "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sess-hb-2", "u1", "tech@example.com", "technician", "Web browser", "cookie", "2026-08-27T00:00:00", "2026-08-27T00:00:00", "2026-08-27T08:00:00", stale_version),
            )
            appliance_identity.revoke_grant(db, grant_id=grant_id)
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-3','Unrelated Partner','approved','real','2026-08-27T00:00:00')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-3','partner-3','Unrelated Customer','cust3@example.test','active','real','2026-08-27T00:00:00')")
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES('u-cust3','partner-3','other-customer@example.test','Customer Owner','customer_owner',?,1,'cust-3','2026-08-27T00:00:00')", (password_hash("Sup3rSecret!"),))
            db.execute(
                "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sess-unrelated", "u-cust3", "other-customer@example.test", "customer_owner", "Web browser", "cookie", "2026-08-27T00:00:00", "2026-08-27T00:00:00", "2026-08-27T08:00:00", None),
            )

    client.post("/api/appliance/heartbeat", json=_heartbeat_payload(cached_manifest_version=stale_version), headers=_auth_headers("appl-1", "cred-1"))
    client.get("/api/appliance/AIC-1/identity-manifest", headers=_auth_headers("appl-1", "cred-1"))

    with _db(db_path):
        with connection() as db:
            rows = db.execute("SELECT details_json FROM audit_logs WHERE action='appliance.sessions_revoked_on_manifest_refresh'").fetchall()
            affected = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-hb-2'").fetchone()
            unrelated = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-unrelated'").fetchone()
    assert len(rows) >= 1, "the genuinely-affected session must still produce a revocation audit entry"
    assert affected["revoked_at"] is not None
    assert unrelated["revoked_at"] is None


def test_a_genuinely_unrelated_ungranted_customer_session_survives_both_endpoints(client, db_path):
    # The real production scenario, not test_unrelated_session_is_
    # untouched_by_reconciliation()'s global-admin case (which never
    # exercised the actual gap -- a global grant legitimately appears in
    # every appliance's own manifest identities regardless of scoping).
    # This customer has ZERO identity_grants rows at all -- ordinary
    # cloud customer_owner, ships zero appliance-identity involvement --
    # exactly the shape of every session that was being force-revoked
    # live in production by an unrelated appliance's own heartbeat.
    # Proves the FIX (Option B's scoping), not just Option A's
    # disablement -- reconciliation genuinely runs here, it just
    # correctly never considers this session a candidate at all.
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-2','Other Partner','approved','real','2026-08-27T00:00:00')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','partner-2','Other Customer','cust2@example.test','active','real','2026-08-27T00:00:00')")
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES('u-cust','partner-2','customer@example.test','Customer Owner','customer_owner',?,1,'cust-2','2026-08-27T00:00:00')", (password_hash("Sup3rSecret!"),))
            db.execute(
                "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sess-cust", "u-cust", "customer@example.test", "customer_owner", "Web browser", "cookie", "2026-08-27T00:00:00", "2026-08-27T00:00:00", "2026-08-27T08:00:00", None),
            )

    client.post("/api/appliance/heartbeat", json=_heartbeat_payload(cached_manifest_version=None), headers=_auth_headers("appl-1", "cred-1"))
    client.get("/api/appliance/AIC-1/identity-manifest", headers=_auth_headers("appl-1", "cred-1"))

    with _db(db_path):
        with connection() as db:
            session = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-cust'").fetchone()
    assert session["revoked_at"] is None


def test_heartbeat_with_no_cached_version_always_refreshes(client, db_path):
    # An appliance that has never cached anything (right after
    # activation) always refreshes -- never silently skips.
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")

    response = client.post("/api/appliance/heartbeat", json=_heartbeat_payload(), headers=_auth_headers("appl-1", "cred-1"))
    assert response.json()["manifest_refreshed"] is True


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


# =============================================================== Option B: session-reconciliation scoping matrix
#
# One test per identity_grants.scope_type, proving _candidate_session_
# user_ids_for_appliance() draws exactly the same line grant_resolves()
# already draws for manifest membership -- a session is ever even
# considered by an appliance's reconciliation if and only if its user
# holds (or held) a grant whose scope actually reaches that appliance.


def test_one_appliance_can_never_revoke_a_session_that_belongs_to_a_different_customers_appliance(client, db_path):
    # The headline property: two REAL, distinct appliances under two
    # different customers -- not a single appliance plus a totally
    # ungranted bystander (that's test_a_genuinely_unrelated_ungranted_
    # customer_session_survives_both_endpoints, above). AIC-2's own
    # heartbeat/manifest-fetch, reconciling AIC-2's own sessions, must
    # never touch a session that belongs entirely to AIC-1's customer.
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", partner_id="partner-1", customer_id="cust-1", site_id="site-1")
            _seed_appliance(db, "appl-2", "AIC-2", "cred-2", partner_id="partner-2", customer_id="cust-2", site_id="site-2")
            # customer_owner is the role VALID_ROLE_SCOPES actually allows
            # at scope_type='customer' (technician is appliance/site only).
            _seed_operator(db, "u1", "cust1-owner@example.com", "Sup3rSecret!", partner_id="partner-1")
            appliance_identity.create_grant(db, user_id="u1", role="customer_owner", scope_type="customer", scope_id="cust-1", granted_by="test")
            manifest1 = appliance_identity.build_manifest(db, cloud_id="AIC-1")
            live_identity = next(i for i in manifest1["identities"] if i["user_id"] == "u1")
            db.execute(
                "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sess-cust1-only", "u1", "cust1-owner@example.com", "customer_owner", "Web browser", "cookie", "2026-08-27T00:00:00", "2026-08-27T00:00:00", "2026-08-27T08:00:00", live_identity["authorization_version"]),
            )

    # AIC-2 -- a different appliance, different customer entirely --
    # heartbeats and fetches its own manifest repeatedly.
    for _ in range(3):
        client.post("/api/appliance/heartbeat", json=_heartbeat_payload(cached_manifest_version=None), headers=_auth_headers("appl-2", "cred-2"))
        client.get("/api/appliance/AIC-2/identity-manifest", headers=_auth_headers("appl-2", "cred-2"))

    with _db(db_path):
        with connection() as db:
            session = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-cust1-only'").fetchone()
    assert session["revoked_at"] is None, "AIC-2's own reconciliation must never revoke a session scoped entirely to a different customer's appliance"

    # And AIC-1 -- the actually-relevant appliance -- still can, proving
    # this isn't just reconciliation being broken/no-op'd again.
    with _db(db_path):
        with connection() as db:
            grant = db.execute("SELECT id FROM identity_grants WHERE user_id='u1'").fetchone()
            appliance_identity.revoke_grant(db, grant_id=grant["id"])
    client.get("/api/appliance/AIC-1/identity-manifest", headers=_auth_headers("appl-1", "cred-1"))
    with _db(db_path):
        with connection() as db:
            session = db.execute("SELECT revoked_at FROM user_sessions WHERE id='sess-cust1-only'").fetchone()
    assert session["revoked_at"] is not None, "AIC-1, the appliance this session's grant was actually scoped to, must still be able to revoke it"


# NOTE on all six tests below: initialize_database() (via the client
# fixture) always runs partner_db.bootstrap_admin(), which itself
# create_grant()s a real scope_type='global' identity_grants row for a
# seeded bootstrap administrator -- so an empty-database candidate set
# is never actually empty, and a global grant's own test can't use a
# fixed cloud_id, since the bootstrap admin is already a candidate for
# literally every one. Every assertion below is written as a before/
# after delta against that baseline (captured fresh in each test,
# before creating the grant under test) rather than an exact-set
# equality, so these tests exercise exactly the one grant they're
# named for and stay correct regardless of what bootstrap_admin() does.


def test_customer_scoped_grant_is_a_candidate_only_for_appliances_under_that_customer(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", customer_id="cust-1")
            _seed_appliance(db, "appl-2", "AIC-2", "cred-2", customer_id="cust-2")
            appliance_1 = {"partner_id": "partner-1", "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-1"}
            appliance_2 = {"partner_id": "partner-1", "customer_id": "cust-2", "site_id": "site-1", "cloud_id": "AIC-2"}
            baseline_1 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            baseline_2 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_2)
            # customer_owner/customer_viewer are the only roles VALID_ROLE_
            # SCOPES allows at scope_type='customer' -- not technician.
            _seed_operator(db, "u1", "cust1-owner@example.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="customer_owner", scope_type="customer", scope_id="cust-1", granted_by="test")
            candidates_for_1 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            candidates_for_2 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_2)
    assert candidates_for_1 - baseline_1 == {"u1"}
    assert candidates_for_2 - baseline_2 == set(), "a customer-scoped grant for cust-1 must not make its holder a candidate for a different customer's appliance"


def test_appliance_scoped_grant_is_a_candidate_only_for_that_one_appliance(client, db_path):
    with _db(db_path):
        with connection() as db:
            appliance_1 = {"partner_id": "partner-1", "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-1"}
            appliance_2 = {"partner_id": "partner-1", "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-2"}
            baseline_1 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            baseline_2 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_2)
            _seed_operator(db, "u1", "appliance-scoped@example.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="technician", scope_type="appliance", scope_id="AIC-1", granted_by="test")
            candidates_for_1 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            candidates_for_2 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_2)
    assert candidates_for_1 - baseline_1 == {"u1"}
    assert candidates_for_2 - baseline_2 == set(), "an appliance-scoped grant for AIC-1 must not make its holder a candidate for a different appliance, even under the same customer/site"


def test_partner_scoped_grant_is_a_candidate_for_every_appliance_under_that_partner_only(client, db_path):
    with _db(db_path):
        with connection() as db:
            appliance_same = {"partner_id": "partner-1", "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-1"}
            appliance_other = {"partner_id": "partner-2", "customer_id": "cust-2", "site_id": "site-2", "cloud_id": "AIC-2"}
            baseline_same = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_same)
            baseline_other = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_other)
            _seed_operator(db, "u1", "partner-scoped@example.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="partner_owner", scope_type="partner", scope_id="partner-1", granted_by="test")
            candidates_same_partner = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_same)
            candidates_other_partner = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_other)
    assert candidates_same_partner - baseline_same == {"u1"}
    assert candidates_other_partner - baseline_other == set()


def test_global_grant_is_a_candidate_for_every_appliance(client, db_path):
    with _db(db_path):
        with connection() as db:
            appliance_1 = {"partner_id": "partner-1", "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-1"}
            appliance_9 = {"partner_id": "partner-9", "customer_id": "cust-9", "site_id": "site-9", "cloud_id": "AIC-9"}
            baseline_1 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            baseline_9 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_9)
            _seed_operator(db, "u1", "global-admin@example.com", "Sup3rSecret!")
            appliance_identity.create_grant(db, user_id="u1", role="administrator", scope_type="global", scope_id=None, granted_by="test")
            candidates_1 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            candidates_9 = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_9)
    assert candidates_1 - baseline_1 == {"u1"}
    assert candidates_9 - baseline_9 == {"u1"}, "a global grant must be a candidate for every appliance, not just the ones seen so far"


def test_a_grant_revoked_at_the_db_level_still_makes_its_holder_a_candidate(client, db_path):
    # The one deliberate exception to "only current grants count": a
    # user whose ONLY relevant grant was just revoked must still be a
    # candidate, or reconcile_sessions_against_manifest() could never
    # actually catch (and force-expire) the now-stale session -- see
    # test_role_removal_still_force_expires_the_session_under_option_b.
    with _db(db_path):
        with connection() as db:
            appliance_1 = {"partner_id": "partner-1", "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-1"}
            baseline = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            _seed_operator(db, "u1", "just-revoked@example.com", "Sup3rSecret!")
            grant_id = appliance_identity.create_grant(db, user_id="u1", role="technician", scope_type="appliance", scope_id="AIC-1", granted_by="test")
            appliance_identity.revoke_grant(db, grant_id=grant_id)
            candidates = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
    assert candidates - baseline == {"u1"}


def test_a_user_with_no_identity_grants_at_all_is_never_a_candidate_for_any_appliance(client, db_path):
    # The ordinary shape of a plain cloud customer_owner -- confirms
    # directly at the _candidate_session_user_ids_for_appliance() level
    # (test_a_genuinely_unrelated_ungranted_customer_session_survives_
    # both_endpoints, above, confirms it end-to-end through the HTTP
    # routes).
    with _db(db_path):
        with connection() as db:
            appliance_1 = {"partner_id": "partner-1", "customer_id": "cust-1", "site_id": "site-1", "cloud_id": "AIC-1"}
            baseline = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
            _seed_operator(db, "u1", "no-grants-at-all@example.com", "Sup3rSecret!")
            candidates = appliance_identity._candidate_session_user_ids_for_appliance(db, appliance=appliance_1)
    assert candidates - baseline == set()
    assert "u1" not in candidates
