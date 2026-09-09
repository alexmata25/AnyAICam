"""HTTP-level coverage for provisioning_api.py's new Phase 1 routes:
GET /api/customer/entitlements, GET /api/customer/installations,
POST /api/provisioning/release, POST /api/provisioning/refresh.

Mirrors the auth patterns already established elsewhere in this test
suite: browser-facing routes are exercised through partner_portal's real
session-token format (test_customer_setup_wizard_rehydration.py), and
the appliance-facing refresh route reuses appliance_cloud.
authenticate_appliance() exactly like test_appliance_cloud_recording_
status.py already does -- proving this module did not invent a second
appliance-identity/auth mechanism.
"""
import secrets
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_provisioning_api.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import appliance_cloud
        from provisioning_api import register_provisioning_api_routes

        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        register_provisioning_api_routes(app)
        with TestClient(app) as test_client:
            yield test_client


def _seed_tenant(db_path, customer_id="cust-1", partner_id="partner-1", email="customer@example.test"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _seed_appliance(db_path, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-1", activation_status="linked", credential=None):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import password_hash
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Site", "2026-01-01"))
        conn.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
            (appliance_id, customer_id, "site-1", cloud_id, activation_status, "2026-01-01"),
        )
        if credential:
            conn.execute(
                "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
                (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-01-01"),
            )
        conn.commit()


def _seed_entitlement(db_path, customer_id="cust-1", product="starter", camera_slot_quantity=4):
    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        ce.upsert_entitlement(customer_id=customer_id, product=product, camera_slot_quantity=camera_slot_quantity)


def _owner_cookie(customer_id="cust-1", email="customer@example.test"):
    import partner_portal
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _appliance_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _cookie():
    import partner_portal
    return partner_portal.SESSION_COOKIE


# --------------------------------------------------------- GET entitlements


def test_entitlements_endpoint_returns_the_callers_own_entitlements(client, db_path):
    _seed_tenant(db_path)
    _seed_entitlement(db_path, product="starter", camera_slot_quantity=4)
    response = client.get("/api/customer/entitlements", cookies={_cookie(): _owner_cookie()})
    assert response.status_code == 200
    body = response.json()
    assert body["total_camera_slots"] == 4
    assert body["entitlements"][0]["product"] == "starter"


def test_entitlements_endpoint_is_scoped_to_the_caller_customer(client, db_path):
    _seed_tenant(db_path, customer_id="cust-1", email="a@example.test")
    _seed_tenant(db_path, customer_id="cust-2", email="b@example.test")
    _seed_entitlement(db_path, customer_id="cust-2", product="enterprise", camera_slot_quantity=25)
    response = client.get("/api/customer/entitlements", cookies={_cookie(): _owner_cookie(customer_id="cust-1", email="a@example.test")})
    assert response.status_code == 200
    body = response.json()
    assert body["total_camera_slots"] == 0
    assert body["entitlements"] == []


def test_entitlements_endpoint_requires_a_customer_owner_session(client, db_path):
    response = client.get("/api/customer/entitlements")
    assert response.status_code == 403


# --------------------------------------------------------- GET installations


def test_installations_endpoint_lists_appliances_and_purchased_slots(client, db_path):
    _seed_tenant(db_path)
    _seed_appliance(db_path, appliance_id="appl-1", cloud_id="AIC-1", activation_status="linked")
    _seed_entitlement(db_path, product="starter", camera_slot_quantity=4)
    response = client.get("/api/customer/installations", cookies={_cookie(): _owner_cookie()})
    assert response.status_code == 200
    body = response.json()
    assert body["camera_slots_purchased"] == 4
    assert body["installations_claimed"] == 1
    assert body["installations"][0]["cloud_id"] == "AIC-1"


# --------------------------------------------------------------- release


def test_release_marks_the_installation_released_and_revokes_its_credential(client, db_path):
    _seed_tenant(db_path)
    _seed_appliance(db_path, appliance_id="appl-1", cloud_id="AIC-1", activation_status="linked", credential="cred-secret")
    response = client.post(
        "/api/provisioning/release", json={"appliance_id": "appl-1"}, cookies={_cookie(): _owner_cookie()}
    )
    assert response.status_code == 200

    # The revoked credential must no longer authenticate.
    refresh = client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", "cred-secret"))
    assert refresh.status_code == 403


def test_release_is_scoped_to_the_caller_customer(client, db_path):
    _seed_tenant(db_path, customer_id="cust-1", email="a@example.test")
    _seed_tenant(db_path, customer_id="cust-2", email="b@example.test")
    _seed_appliance(db_path, appliance_id="appl-1", customer_id="cust-2", cloud_id="AIC-1")
    response = client.post(
        "/api/provisioning/release",
        json={"appliance_id": "appl-1"},
        cookies={_cookie(): _owner_cookie(customer_id="cust-1", email="a@example.test")},
    )
    assert response.status_code == 404


def test_release_requires_an_appliance_id(client, db_path):
    _seed_tenant(db_path)
    response = client.post("/api/provisioning/release", json={}, cookies={_cookie(): _owner_cookie()})
    assert response.status_code == 400


# --------------------------------------------------------------- refresh


def test_refresh_returns_the_installations_current_entitlement(client, db_path):
    _seed_tenant(db_path)
    _seed_appliance(db_path, appliance_id="appl-1", cloud_id="AIC-1", credential="cred-1")
    _seed_entitlement(db_path, product="professional", camera_slot_quantity=10)
    response = client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    body = response.json()
    assert body["cloud_id"] == "AIC-1"
    assert body["camera_slot_quantity"] == 10


def test_refresh_rejects_an_invalid_credential(client, db_path):
    _seed_tenant(db_path)
    _seed_appliance(db_path, appliance_id="appl-1", cloud_id="AIC-1", credential="cred-1")
    response = client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", "wrong-credential"))
    assert response.status_code == 403


def test_refresh_never_accepts_a_customer_browser_session_in_place_of_appliance_auth(client, db_path):
    """The refresh endpoint is appliance-facing only -- a customer's own
    browser session cookie must not substitute for a signed appliance
    request, even for their own installation."""
    _seed_tenant(db_path)
    _seed_appliance(db_path, appliance_id="appl-1", cloud_id="AIC-1", credential="cred-1")
    response = client.post("/api/provisioning/refresh", cookies={_cookie(): _owner_cookie()})
    assert response.status_code == 401
