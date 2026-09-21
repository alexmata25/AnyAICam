"""Entitlement-driven product mode (2026-09-21): customer_entitlements.
product_mode_for_customer() and its exposure through the edge sync
channel, GET /api/appliance/configuration's `product_mode` field -- see
appliance_cloud.appliance_configuration()'s own comment and product_
mode.py's module docstring for why this makes an "Upgrade Local ->
Hybrid" action genuinely billing-driven: the moment a Hybrid checkout
completes (create_camera_slot_checkout() -> the existing Stripe webhook
-> upsert_entitlement(product="camera_slots_hybrid")), this field starts
reporting "hybrid" for that customer with no separate code path.

Real HTTP throughout for the route-level tests (TestClient(main.app)),
mirroring test_rdm_cloud_policy.py's own established pattern for testing
this same GET /api/appliance/configuration route.
"""
import secrets
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_product_mode_cloud_config.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(db_path, customer_id, partner_id=None):
    partner_id = partner_id or f"partner-{customer_id}"
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", f"{customer_id}@example.test", "active", "2026-01-01"),
        )
        conn.commit()


def _seed_appliance(db_path, customer_id, appliance_id, cloud_id, credential):
    from partner_db import connection, password_hash
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Site", "2026-01-01"))
            db.execute(
                "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
                (appliance_id, customer_id, f"site-{customer_id}", cloud_id, "2026-01-01"),
            )
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-01-01"))


def _appliance_auth_headers(appliance_id, credential):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


# ------------------------------------------------------ product_mode_for_customer()


def test_no_camera_slot_entitlement_reports_no_mode(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer
        assert product_mode_for_customer("cust-1") == ""


def test_active_local_entitlement_reports_local(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "local"


def test_active_hybrid_entitlement_reports_hybrid(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "hybrid"


def test_upgrade_from_local_to_hybrid_reports_hybrid_even_with_the_old_local_row_still_active(client, db_path):
    """The exact moment right after a real Local -> Hybrid upgrade
    checkout completes: upsert_entitlement() is idempotent per
    (customer_id, product), so the original one-time Local purchase and
    the new Hybrid subscription are two independent, coexisting rows --
    "the customer just paid for Hybrid" must take effect immediately,
    with no separate step to first retire the Local row."""
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "local"
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "hybrid"


def test_a_cancelled_entitlement_is_not_counted(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="cancelled")
        assert product_mode_for_customer("cust-1") == ""


def test_downgrade_falls_back_to_local_when_a_local_entitlement_is_still_on_file(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="cancelled")
        assert product_mode_for_customer("cust-1") == "local"


# --------------------------------------------------- GET /api/appliance/configuration


def test_configuration_route_reports_no_mode_with_no_entitlement(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-MODE1", "test-credential")
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 200
    assert response.json()["product_mode"] == ""


def test_configuration_route_reflects_a_local_entitlement(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-MODE1", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "local"


def test_configuration_route_reflects_a_hybrid_upgrade_immediately(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-MODE1", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "local"

    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "hybrid"


def test_two_appliances_under_different_customers_see_their_own_mode_only(client, db_path):
    _seed_tenant(db_path, "cust-a", partner_id="partner-a")
    _seed_tenant(db_path, "cust-b", partner_id="partner-b")
    _seed_appliance(db_path, "cust-a", "appl-a", "AIC-MODEA", "cred-a")
    _seed_appliance(db_path, "cust-b", "appl-b", "AIC-MODEB", "cred-b")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-a", product="camera_slots_local", camera_slot_quantity=8)
        upsert_entitlement(customer_id="cust-b", product="camera_slots_hybrid", camera_slot_quantity=8)

    response_a = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-a", "cred-a"))
    response_b = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-b", "cred-b"))
    assert response_a.json()["product_mode"] == "local"
    assert response_b.json()["product_mode"] == "hybrid"
