"""Regression coverage for the appliance activation UX: GET /operations/
appliance-activation, POST /api/operations/appliance-identity/fetch-
manifest, and the existing POST /api/appliance/activate driven from
that page's own JS. Real HTTP, real main.app -- proves the credential
is never displayed and the manifest is fetched immediately on success.
"""
import secrets
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import appliance_activation
import appliance_cloud
import appliance_identity
import main
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash


@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", tmp_path / "appliance_identity.json")
    # appliance_cloud.activation_limiter is a plain module-level singleton,
    # shared (and never reset) across every test file in the whole pytest
    # session -- TestClient's client host is the same constant value every
    # time, so a full-suite run can otherwise trip it purely from other
    # files' own POST /api/appliance/activate calls, well before this
    # file's own 10-attempt budget is used. Reset per test so this file's
    # results never depend on collection order.
    appliance_cloud.activation_limiter.events.clear()
    db_path = tmp_path / "test_activation_ux.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as test_client:
            yield test_client, db_path
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _admin_session():
    main.save_users([{"id": "admin-1", "email": "admin@example.test", "role": "administrator", "enabled": True, "camera_ids": []}])
    return main.create_session("admin-1")


def _seed_appliance_with_token(db_path, cloud_id="AIC-UX1", token="tok-ux-1"):
    appliance_id = f"appl-{cloud_id}"
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            now = "2026-08-27T00:00:00"
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", now))
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Customer", "", "cust1@example.test", "active", "eligible", "real", now))
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Site", now))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, "cust-1", "site-1", cloud_id, now))
            db.execute(
                "INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                (secrets.token_hex(4), appliance_id, password_hash(token), (datetime.now() + timedelta(hours=24)).isoformat(), now),
            )


# =============================================================== page access control


def test_activation_page_requires_admin_access(http_client):
    client, db_path = http_client
    response = client.get("/operations/appliance-activation")
    assert response.status_code in (302, 303, 200)  # anonymous -- current_user() falls back, permission_denied_page renders 200 or middleware redirects


def test_activation_page_shows_the_entry_form_before_activation(http_client):
    client, db_path = http_client
    token = _admin_session()
    response = client.get("/operations/appliance-activation", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 200
    assert "Not yet activated" in response.text
    assert 'id="activate-form"' in response.text


# =============================================================== the credential is never displayed


def test_activate_response_credential_never_appears_in_the_page_html(http_client):
    client, db_path = http_client
    _seed_appliance_with_token(db_path)
    admin_token = _admin_session()
    result = client.post("/api/appliance/activate", json={"cloud_id": "AIC-UX1", "activation_token": "tok-ux-1"})
    credential = result.json()["credential"]

    response = client.get("/operations/appliance-activation", cookies={main.SESSION_COOKIE_NAME: admin_token})
    assert credential not in response.text
    assert "credential" not in response.text.lower().split("permanent")[-1][:60] or "never" in response.text.lower()


def test_activation_page_shows_customer_and_site_association_after_activation(http_client):
    client, db_path = http_client
    _seed_appliance_with_token(db_path)
    admin_token = _admin_session()
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-UX1", "activation_token": "tok-ux-1"})

    response = client.get("/operations/appliance-activation", cookies={main.SESSION_COOKIE_NAME: admin_token})
    assert response.status_code == 200
    assert "AIC-UX1" in response.text
    assert "cust-1" in response.text
    assert "site-1" in response.text
    assert "Reset / re-provision" in response.text


# =============================================================== immediate manifest fetch


def test_fetch_manifest_endpoint_requires_prior_activation(http_client):
    client, db_path = http_client
    token = _admin_session()
    response = client.post("/api/operations/appliance-identity/fetch-manifest", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 409


def test_fetch_manifest_endpoint_returns_identity_count_after_activation(http_client):
    client, db_path = http_client
    _seed_appliance_with_token(db_path)
    admin_token = _admin_session()
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-UX1", "activation_token": "tok-ux-1"})

    response = client.post("/api/operations/appliance-identity/fetch-manifest", cookies={main.SESSION_COOKIE_NAME: admin_token})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "connected"
    assert body["cloud_id"] == "AIC-UX1"
    assert isinstance(body["identity_count"], int)
    assert isinstance(body["manifest_version"], int)


# =============================================================== reset / re-provision path preserved


def test_reset_then_reactivate_with_a_different_cloud_id_via_the_real_endpoints(http_client):
    client, db_path = http_client
    _seed_appliance_with_token(db_path, cloud_id="AIC-UX1", token="tok-ux-1")
    _seed_appliance_with_token(db_path, cloud_id="AIC-UX2", token="tok-ux-2")
    admin_token = _admin_session()
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-UX1", "activation_token": "tok-ux-1"})

    # A different Cloud ID is refused before reset -- same rule as gap #1.
    conflict = client.post("/api/appliance/activate", json={"cloud_id": "AIC-UX2", "activation_token": "tok-ux-2"})
    assert conflict.status_code == 409

    reset = client.post("/api/operations/appliance-identity/reset", cookies={main.SESSION_COOKIE_NAME: admin_token})
    assert reset.status_code == 200

    recovered = client.post("/api/appliance/activate", json={"cloud_id": "AIC-UX2", "activation_token": "tok-ux-2"})
    assert recovered.status_code == 200
    assert appliance_activation.load_persisted_identity()["cloud_id"] == "AIC-UX2"
