"""Regression coverage for POST /api/portal-login's cloud-delegated
path -- own_appliance_identity() + appliance_identity.py wired into the
existing Administrator/Partner/Technician selector.

Governing behavior under test: when this instance is NOT configured as
an activated appliance (own_appliance_identity() returns None -- e.g.
Samsung today), portal_login_submit() must behave byte-for-byte as it
did before this contract existed. Only once the three ANYAICAM_
APPLIANCE_* env vars are set does password verification for the
Partner/Administrator/Technician buckets move to the signed,
grant-scoped cloud-identity contract.
"""
import secrets
import sqlite3
import time

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
    db_path = tmp_path / "test_delegated.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as test_client:
            yield test_client, db_path
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _seed_appliance_and_operator(db_path, *, cloud_id="AIC-SELF0001", email="amata@anyaicam.com", password="Sup3rSecret!", role="administrator", scope_type="global", scope_id=None, partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            now = "2026-08-27T00:00:00"
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Partner", "approved", "real", now))
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("cust-1", partner_id, "Customer", "", "cust1@example.test", "active", "eligible", "real", now))
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Site", now))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)", ("appl-self", "cust-1", "site-1", cloud_id, partner_id, now))
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-self", "appl-self", password_hash("self-credential"), now))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)", ("u-self", partner_id, email, "Operator", role, password_hash(password), 1, now))
            appliance_identity.create_grant(db, user_id="u-self", role=role, scope_type=scope_type, scope_id=scope_id, granted_by="test")


def _configure_own_appliance(monkeypatch, cloud_id="AIC-SELF0001"):
    monkeypatch.setenv("ANYAICAM_APPLIANCE_ID", "appl-self")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CLOUD_ID", cloud_id)
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CREDENTIAL", "self-credential")


# =============================================================== not configured -- unaffected (the Samsung-today case)


def test_unconfigured_instance_is_completely_unaffected_by_the_delegated_path(http_client, monkeypatch):
    monkeypatch.delenv("ANYAICAM_APPLIANCE_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CLOUD_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CREDENTIAL", raising=False)
    client, db_path = http_client
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", "2026-08-27T00:00:00"))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)", ("u1", "partner-1", "owner@example.test", "Owner", "partner_owner", password_hash("Sup3rSecret!"), 1, "2026-08-27T00:00:00"))

    response = client.post("/api/portal-login", json={"email": "owner@example.test", "password": "Sup3rSecret!", "portal": "partner"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/partner?tab=customers"


# =============================================================== configured -- delegated contract in effect


def test_configured_instance_delegates_administrator_login_through_the_signed_assertion(http_client, monkeypatch):
    client, db_path = http_client
    _seed_appliance_and_operator(db_path)
    _configure_own_appliance(monkeypatch)

    response = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "Sup3rSecret!", "portal": "administrator"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/partner?tab=customers"
    assert partner_portal.SESSION_COOKIE in response.cookies

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT authorization_version_at_login FROM user_sessions WHERE user_id='u-self'").fetchone()
    assert row["authorization_version_at_login"] == 2  # default 1, +1 from create_grant()'s bump


def test_configured_instance_rejects_a_grant_that_does_not_resolve_to_this_appliance(http_client, monkeypatch):
    client, db_path = http_client
    # technician/appliance is a valid role-scope combination (unlike
    # administrator/appliance, which validate_grant_role_scope() now
    # rejects outright) -- this test is about scope resolution, not
    # role-scope validity, so it needs a combination that's valid in
    # general but simply doesn't resolve to *this* appliance.
    _seed_appliance_and_operator(db_path, email="tech@example.com", role="technician", scope_type="appliance", scope_id="AIC-SOME-OTHER-BOX")
    _configure_own_appliance(monkeypatch)

    response = client.post("/api/portal-login", json={"email": "tech@example.com", "password": "Sup3rSecret!", "portal": "technician"})
    assert response.status_code in (401, 403)


def test_configured_instance_wrong_password_is_denied(http_client, monkeypatch):
    client, db_path = http_client
    _seed_appliance_and_operator(db_path)
    _configure_own_appliance(monkeypatch)

    response = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "wrong-password", "portal": "administrator"})
    assert response.status_code == 401


def test_configured_instance_technician_selector_reaches_appliance_dashboard(http_client, monkeypatch):
    client, db_path = http_client
    _seed_appliance_and_operator(db_path, email="tech@example.com", role="technician", scope_type="appliance", scope_id="AIC-SELF0001")
    _configure_own_appliance(monkeypatch)

    response = client.post("/api/portal-login", json={"email": "tech@example.com", "password": "Sup3rSecret!", "portal": "technician"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/partner/appliance-dashboard"


# =============================================================== new offline login denied while cloud unavailable


def test_cloud_unavailable_denies_a_brand_new_login_honestly(http_client, monkeypatch):
    client, db_path = http_client
    _seed_appliance_and_operator(db_path)
    _configure_own_appliance(monkeypatch)

    def _raise_unavailable(**kwargs):
        raise appliance_identity.CloudIdentityUnavailable("network unreachable")

    backend = appliance_identity.get_cloud_identity_backend()
    monkeypatch.setattr(backend, "authenticate_operator", _raise_unavailable)

    response = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "Sup3rSecret!", "portal": "administrator"})
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()


def test_cloud_unavailable_does_not_affect_an_already_established_session(http_client, monkeypatch):
    # The offline-grace case: an existing session cookie is validated
    # locally (signed cookie + user_sessions row), no cloud round-trip
    # involved at all -- unaffected by the cloud being unreachable for
    # a *new* login.
    client, db_path = http_client
    _seed_appliance_and_operator(db_path)
    _configure_own_appliance(monkeypatch)

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "Sup3rSecret!", "portal": "administrator"}, follow_redirects=False)
    cookie_value = login.cookies[partner_portal.SESSION_COOKIE]

    backend = appliance_identity.get_cloud_identity_backend()

    def _raise_unavailable(**kwargs):
        raise appliance_identity.CloudIdentityUnavailable("network unreachable")

    monkeypatch.setattr(backend, "authenticate_operator", _raise_unavailable)

    response = client.get("/partner", cookies={partner_portal.SESSION_COOKIE: cookie_value})
    assert response.status_code == 200  # the existing session still works; no cloud call was made to view this page
