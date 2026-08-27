"""End-to-end mock-cloud scenario proving the full Samsung-readiness
chain works through the SAME real APIs a real appliance and a real
admin browser would use -- no test-only direct-database shortcuts for
any of the actual business actions (bootstrap, granting, onboarding,
activation, manifest fetch, delegated login, revocation, heartbeat
reconciliation). Session tokens are minted via partner_portal._token()/
main.create_session() only to *establish* an already-authenticated
caller for the next real API call -- the same scaffolding technique
used throughout this session's other integration tests, never a
substitute for the action itself.

Still entirely against the mock backend: nothing here touches Samsung,
Ryzen, or production AWS.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import appliance_activation
import appliance_cloud
import appliance_identity
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database


OPERATOR_EMAIL = "amata@anyaicam.com"
OPERATOR_PASSWORD = "Sup3rSecret!Cloud"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", tmp_path / "appliance_identity.json")
    # bootstrap_admin() reads these at initialize_database() time -- the
    # real production path for seeding the first cloud operator account,
    # not a shortcut. Only set for this one fixture's db init.
    monkeypatch.setenv("ANYAICAM_ADMIN_EMAIL", OPERATOR_EMAIL)
    monkeypatch.setenv("ANYAICAM_ADMIN_PASSWORD", OPERATOR_PASSWORD)
    # Never point own_appliance_identity() at env vars in this test --
    # the whole point is proving the *persisted-file* path (what real
    # Samsung will use) works end to end.
    monkeypatch.delenv("ANYAICAM_APPLIANCE_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CLOUD_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CREDENTIAL", raising=False)
    # See test_appliance_activation_ux.py's fixture comment: activation_limiter
    # is a shared, never-auto-reset module singleton across the whole suite.
    appliance_cloud.activation_limiter.events.clear()

    db_path = tmp_path / "test_e2e.db"
    with override_target(sqlite_path=db_path):
        initialize_database()  # runs bootstrap_admin() for real -- step 1
        import pricing_config
        percentage_config = pricing_config.load_pricing()
        percentage_config["partner"]["pricing_mode"] = "percentage"
        percentage_config["partner"]["percentage_discount"] = 20
        monkeypatch.setattr(pricing_config, "load_pricing", lambda: percentage_config)
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as test_client:
            yield test_client, db_path
    appliance_identity.reset_cloud_identity_backend_for_tests()
    monkeypatch.delenv("ANYAICAM_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD", raising=False)


def _db(db_path):
    return override_target(sqlite_path=str(db_path))


def _legacy_admin_session():
    """A real Admin Portal session, used only to call the admin-only
    grant-management API (step 2) -- distinct from amata@anyaicam.com's
    cloud/partner identity, exactly matching this session's own
    "admin@local is bootstrap/emergency only" design: an operator's own
    grants come from the cloud grant system, never from being admin@local."""
    main.save_users([{"id": "bootstrap-admin", "email": "admin@local", "role": "administrator", "enabled": True, "camera_ids": []}])
    return main.create_session("bootstrap-admin")


def test_full_samsung_readiness_chain_via_real_apis(env):
    client, db_path = env

    # ---- Step 1: mock cloud creates the company/operator identity (bootstrap_admin(), already ran in the fixture) ----
    with _db(db_path):
        with connection() as db:
            operator = db.execute("SELECT id,role,authorization_version FROM partner_users WHERE lower(email)=?", (OPERATOR_EMAIL,)).fetchone()
    assert operator is not None
    assert operator["role"] == "administrator"
    operator_user_id = operator["id"]

    # ---- Step 2: amata@anyaicam.com receives explicit Administrator + Partner grants, via the real admin-only grant API ----
    admin_token = _legacy_admin_session()
    admin_grant = client.post(
        "/api/operations/identity-grants",
        json={"email": OPERATOR_EMAIL, "role": "administrator", "scope_type": "global"},
        cookies={main.SESSION_COOKIE_NAME: admin_token},
    )
    assert admin_grant.status_code == 200
    partner_grant = client.post(
        "/api/operations/identity-grants",
        json={"email": OPERATOR_EMAIL, "role": "partner_owner", "scope_type": "partner", "scope_id": "anyaicam-primary"},
        cookies={main.SESSION_COOKIE_NAME: admin_token},
    )
    assert partner_grant.status_code == 200
    partner_grant_id = partner_grant.json()["grant_id"]

    grants_list = client.get("/api/operations/identity-grants", cookies={main.SESSION_COOKIE_NAME: admin_token}).json()["grants"]
    assert {g["role"] for g in grants_list if g["email"] == OPERATOR_EMAIL} == {"administrator", "partner_owner"}

    # ---- Step 3: mock cloud creates customer/site/appliance + activation token, via the real onboarding API, authenticated as the operator's DIRECT partner_db identity ----
    operator_direct_token = partner_portal._token(OPERATOR_EMAIL, "administrator", "anyaicam-primary", None, None)
    onboard = client.post(
        "/api/partner/customers/onboard",
        json={
            "name": "Ryzen Test Customer", "company": "Acme", "email": "customer@example.test", "phone": "555-0100",
            "status": "trial", "sites": [{"name": "HQ"}],
            "appliance_type": "AnyAiCam mini PC", "deployment_mode": "local",
            "pricing": {"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 5, "addons": []},
        },
        cookies={partner_portal.SESSION_COOKIE: operator_direct_token},
    )
    assert onboard.status_code == 200
    activation = onboard.json()["activation_tokens"][0]
    cloud_id = activation["cloud_id"]
    activation_token = activation["activation_token"]

    # ---- Step 4: the appliance activates with the real Cloud ID/token ----
    activate = client.post("/api/appliance/activate", json={"cloud_id": cloud_id, "activation_token": activation_token})
    assert activate.status_code == 200
    appliance_credential = activate.json()["credential"]
    appliance_id = activate.json()["appliance_id"]

    # ---- Step 5: activation identity survives a simulated restart ----
    reloaded = appliance_activation.load_persisted_identity()
    assert reloaded is not None
    assert reloaded["cloud_id"] == cloud_id
    assert reloaded["credential"] == appliance_credential
    assert main.own_appliance_identity()["cloud_id"] == cloud_id  # what portal_login_submit() will actually read

    # ---- Step 6: the appliance receives its signed manifest, real endpoint, real credential ----
    import secrets as _secrets
    import time as _time
    manifest_response = client.get(
        f"/api/appliance/{cloud_id}/identity-manifest",
        headers={"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(_time.time())), "X-Request-Nonce": _secrets.token_hex(16), "Authorization": f"Bearer {appliance_credential}"},
    )
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    manifest_emails = {i["email"]: i for i in manifest["identities"]}
    assert OPERATOR_EMAIL in manifest_emails
    assert {g["role"] for g in manifest_emails[OPERATOR_EMAIL]["grants"]} == {"administrator", "partner_owner"}
    # ---- "no admin@local account appears in the cloud manifest" ----
    assert "admin@local" not in manifest_emails

    from appliance_identity import verify_manifest
    keys = client.get("/api/appliance/signing-keys").json()["keys"]
    verify_manifest(manifest, expected_cloud_id=cloud_id, public_keys=keys)  # does not raise -- real Ed25519 verification
    pre_revoke_manifest_version = manifest["manifest_version"]

    # ---- Step 7/8: amata@anyaicam.com signs in through the blue Portal login, selecting Administrator ----
    admin_login = client.post(
        "/api/portal-login",
        json={"email": OPERATOR_EMAIL, "password": OPERATOR_PASSWORD, "portal": "administrator"},
        follow_redirects=False,
    )
    assert admin_login.status_code == 303
    assert partner_portal.SESSION_COOKIE in admin_login.cookies
    admin_session_cookie = admin_login.cookies[partner_portal.SESSION_COOKIE]

    # ---- Step 9: Partner selection reaches the Partner Portal ----
    partner_login = client.post(
        "/api/portal-login",
        json={"email": OPERATOR_EMAIL, "password": OPERATOR_PASSWORD, "portal": "partner"},
        follow_redirects=False,
    )
    assert partner_login.status_code == 303
    partner_session_cookie = partner_login.cookies[partner_portal.SESSION_COOKIE]

    # Both destinations legitimately land on the same Partner Portal
    # customer view today (customer_policy.role_destination() maps both
    # 'administrator' and 'partner_owner' there) -- what distinguishes
    # them is the SESSION's own role, not the URL, so verify that
    # directly against each session's own DB row.
    with _db(db_path):
        with connection() as db:
            admin_role = db.execute("SELECT role FROM user_sessions WHERE email=? ORDER BY created_at DESC LIMIT 1", (OPERATOR_EMAIL,)).fetchone()["role"]
    assert admin_role in {"administrator", "partner_owner"}  # whichever was most recently established (partner, here)

    with _db(db_path):
        with connection() as db:
            sessions = db.execute("SELECT id,role FROM user_sessions WHERE email=? ORDER BY created_at ASC", (OPERATOR_EMAIL,)).fetchall()
    roles_seen = {row["role"] for row in sessions}
    assert roles_seen == {"administrator", "partner_owner"}
    admin_session_id = next(row["id"] for row in sessions if row["role"] == "administrator")
    partner_session_id = next(row["id"] for row in sessions if row["role"] == "partner_owner")

    # ---- Step 10: revoke the Partner grant ----
    revoke = client.post(f"/api/operations/identity-grants/{partner_grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: admin_token})
    assert revoke.status_code == 200

    # ---- Step 11: next heartbeat invalidates Partner authorization while Administrator remains valid ----
    heartbeat = client.post(
        "/api/appliance/heartbeat",
        json={"uptime_seconds": 5000, "cpu": 5, "memory": 10, "camera_count": 5, "cached_manifest_version": pre_revoke_manifest_version},
        headers={"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(_time.time())), "X-Request-Nonce": _secrets.token_hex(16), "Authorization": f"Bearer {appliance_credential}"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["manifest_refreshed"] is True
    assert heartbeat.json()["current_manifest_version"] > pre_revoke_manifest_version

    with _db(db_path):
        with connection() as db:
            partner_session_row = db.execute("SELECT revoked_at FROM user_sessions WHERE id=?", (partner_session_id,)).fetchone()
            admin_session_row = db.execute("SELECT revoked_at FROM user_sessions WHERE id=?", (admin_session_id,)).fetchone()
    assert partner_session_row["revoked_at"] is not None  # Partner authorization invalidated
    assert admin_session_row["revoked_at"] is None         # Administrator remains valid

    # The Administrator session cookie must still work against a real
    # authenticated page after the Partner-only revocation.
    still_works = client.get("/partner", cookies={partner_portal.SESSION_COOKIE: admin_session_cookie})
    assert still_works.status_code == 200
