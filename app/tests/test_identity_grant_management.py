"""Regression coverage for the admin-only, audited grant-management
API/UI (POST/GET /api/operations/identity-grants, /operations/identity-
grants) -- gap-closing item #1 of the Samsung-readiness build. Every
test here exercises the real HTTP routes an admin's browser would hit.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash


@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    db_path = tmp_path / "test_grants.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client, db_path


def _admin_session():
    main.save_users([{"id": "admin-1", "email": "admin@example.test", "role": "administrator", "enabled": True, "camera_ids": []}])
    return main.create_session("admin-1")


def _viewer_session():
    main.save_users([{"id": "viewer-1", "email": "viewer@example.test", "role": "viewer", "enabled": True, "camera_ids": []}])
    return main.create_session("viewer-1")


def _seed_operator(db_path, email="amata@anyaicam.com"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            now = "2026-08-27T00:00:00"
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", now))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)", ("u-op", "partner-1", email, "Operator", "administrator", password_hash("x"), 1, now))
    return "u-op"


# =============================================================== access control


def test_grant_creation_requires_admin_portal_access(http_client):
    client, db_path = http_client
    token = _viewer_session()
    response = client.post("/api/operations/identity-grants", json={"email": "a@example.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 403


def test_grant_list_requires_admin_portal_access(http_client):
    client, db_path = http_client
    token = _viewer_session()
    response = client.get("/api/operations/identity-grants", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 403


def test_grant_page_denies_a_non_admin_with_an_honest_page_not_a_redirect(http_client):
    client, db_path = http_client
    token = _viewer_session()
    response = client.get("/operations/identity-grants", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 200  # permission_denied_page(), same convention as every other Operations page
    assert "identity grants" in response.text.lower() or "permission" in response.text.lower()


# =============================================================== creating a grant


def test_admin_can_grant_an_existing_operator_administrator_access(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()

    response = client.post(
        "/api/operations/identity-grants",
        json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "granted"

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT role,scope_type,scope_id FROM identity_grants WHERE user_id='u-op'").fetchone()
    assert row["role"] == "administrator"
    assert row["scope_type"] == "global"


def test_granting_bumps_authorization_version(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()

    client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token})

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            version = db.execute("SELECT authorization_version FROM partner_users WHERE id='u-op'").fetchone()["authorization_version"]
    assert version == 2  # default 1, +1 from the grant


def test_granting_an_email_with_no_existing_account_is_rejected_not_silently_created(http_client):
    client, db_path = http_client
    token = _admin_session()

    response = client.post(
        "/api/operations/identity-grants",
        json={"email": "nobody@example.com", "role": "administrator", "scope_type": "global"},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 404
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            assert db.execute("SELECT id FROM partner_users WHERE lower(email)='nobody@example.com'").fetchone() is None


def test_an_invalid_role_scope_combination_is_rejected(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()

    response = client.post(
        "/api/operations/identity-grants",
        json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "appliance", "scope_id": "AIC-1"},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 400


def test_an_unknown_role_is_rejected(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()

    response = client.post(
        "/api/operations/identity-grants",
        json={"email": "amata@anyaicam.com", "role": "superuser", "scope_type": "global"},
        cookies={main.SESSION_COOKIE_NAME: token},
    )
    assert response.status_code == 400


def test_grant_creation_is_audited(http_client, tmp_path, monkeypatch):
    client, db_path = http_client
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit_log.jsonl")
    _seed_operator(db_path)
    token = _admin_session()

    client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token})

    entries = main.load_audit_entries()
    assert any(e.get("action") == "grant" and "amata@anyaicam.com" in e.get("detail", "") for e in entries)


def test_grant_revocation_is_audited(http_client, tmp_path, monkeypatch):
    client, db_path = http_client
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit_log.jsonl")
    _seed_operator(db_path)
    token = _admin_session()
    grant_id = client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token}).json()["grant_id"]

    client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: token})

    entries = main.load_audit_entries()
    assert any(e.get("action") == "revoke" and grant_id in e.get("detail", "") for e in entries)


# =============================================================== listing grants


def test_grant_list_shows_active_and_revoked_grants(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()
    client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token})

    response = client.get("/api/operations/identity-grants", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 200
    grants = response.json()["grants"]
    assert len(grants) == 1
    assert grants[0]["email"] == "amata@anyaicam.com"
    assert grants[0]["revoked_at"] is None


def test_grant_management_page_renders_current_grants(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()
    client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token})

    response = client.get("/operations/identity-grants", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 200
    assert "amata@anyaicam.com" in response.text
    assert "Administrator" in response.text


# =============================================================== revoking a grant


def test_admin_can_revoke_a_grant(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()
    grant_id = client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token}).json()["grant_id"]

    response = client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT revoked_at FROM identity_grants WHERE id=?", (grant_id,)).fetchone()
    assert row["revoked_at"] is not None


def test_revoking_bumps_authorization_version_again(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    token = _admin_session()
    grant_id = client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: token}).json()["grant_id"]

    client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: token})

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            version = db.execute("SELECT authorization_version FROM partner_users WHERE id='u-op'").fetchone()["authorization_version"]
    assert version == 3  # 1 default, +1 grant, +1 revoke


def test_revoking_an_unknown_grant_is_a_404(http_client):
    client, db_path = http_client
    token = _admin_session()
    response = client.post("/api/operations/identity-grants/does-not-exist/revoke", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 404


def test_revoking_requires_admin_access(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    grant_id = client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "global"}, cookies={main.SESSION_COOKIE_NAME: admin_token}).json()["grant_id"]

    viewer_token = _viewer_session()
    response = client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: viewer_token})
    assert response.status_code == 403
