"""GET /api/appliance/recordings/status: focused tests proving the new
route returns the current ANYAICAM_RECORDING_UPLOAD_ENABLED state with
the same authenticate_appliance() auth as the existing recording
routes, and that it's a pure env-var read -- no S3/STS call, no
per-camera authorization.

Imports appliance_cloud (which imports partner_db, triggering its
import-time schema init) -- per this project's own documented
constraint, this file redirects to a throwaway sqlite file via
override_target() before that import happens, so nothing here ever
touches the real production database.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_cloud_recording_status.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed_appliance(db, appliance_id: str, cloud_id: str, credential: str):
    now = "2026-08-21T00:00:00"
    db.execute(
        "INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",
        ("partner-1", "Test Partner", "approved", "real", now),
    )
    db.execute(
        "INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
        ("cust-1", "partner-1", "Test Customer", "test@example.test", "active", "real", now),
    )
    db.execute(
        "INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
        ("site-1", "cust-1", "Test Site", now),
    )
    db.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (appliance_id, "cust-1", "site-1", cloud_id, now),
    )
    db.execute(
        "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
        ("cred-1", appliance_id, password_hash(credential), now),
    )


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_appliance_status.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _seeded(db_path, appliance_id="appl-1", cloud_id="AIC-TEST0001", credential="test-credential"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, appliance_id, cloud_id, credential)


# ------------------------------------------------------------------- tests


def test_authenticated_request_returns_enabled_false_when_flag_is_false(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ENABLED", False)
    _seeded(db_path)

    response = client.get("/api/appliance/recordings/status", headers=_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_authenticated_request_returns_enabled_true_when_flag_is_true(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ENABLED", True)
    _seeded(db_path)

    response = client.get("/api/appliance/recordings/status", headers=_auth_headers("appl-1", "test-credential"))

    assert response.status_code == 200
    assert response.json() == {"enabled": True}


def test_unauthenticated_request_is_rejected(client, db_path):
    _seeded(db_path)

    response = client.get("/api/appliance/recordings/status")  # no auth headers at all

    assert response.status_code == 401


def test_wrong_credential_is_rejected_consistently_with_existing_routes(client, db_path):
    _seeded(db_path)

    response = client.get(
        "/api/appliance/recordings/status",
        headers=_auth_headers("appl-1", "totally-wrong-credential"),
    )

    assert response.status_code == 403  # matches authenticate_appliance()'s own "Invalid appliance credential."


def test_unknown_appliance_id_is_rejected(client, db_path):
    _seeded(db_path)

    response = client.get(
        "/api/appliance/recordings/status",
        headers=_auth_headers("appl-does-not-exist", "test-credential"),
    )

    assert response.status_code == 403


# --------------------------------------------------- existing route regression


def test_existing_credentials_route_still_returns_404_when_disabled(client, db_path, monkeypatch):
    """Regression guard: adding the new route must not have disturbed
    the existing /credentials route's own fail-closed behavior."""
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ENABLED", False)
    _seeded(db_path)

    response = client.post(
        "/api/appliance/recordings/appl-1/credentials",
        headers=_auth_headers("appl-1", "test-credential"),
        json={},
    )

    assert response.status_code == 404


def test_existing_available_route_still_returns_404_when_disabled(client, db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ENABLED", False)
    _seeded(db_path)

    response = client.post(
        "/api/appliance/recordings/appl-1/available",
        headers=_auth_headers("appl-1", "test-credential"),
        json={"s3_key": "recordings/x/y/z/w/file.mp4", "started_at": "2026-08-21T00:00:00", "ended_at": "2026-08-21T00:05:00"},
    )

    assert response.status_code == 404
