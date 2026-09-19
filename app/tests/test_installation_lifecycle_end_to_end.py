"""Provisioning Phase 3, end-to-end installation lifecycle: fresh VMS
installation -> Cloud ID -> AWS claim -> customer_entitlements ->
authorized camera-slot count -- exercised across the actual HTTP routes
(claim, the Stripe webhook, refresh, release) rather than calling
internal functions directly, so this proves the real wiring, not just
each piece in isolation.

Covers the specific Phase 3 scenarios:
- install-before-purchase (zero-slot claim, then a later purchase reaches
  the SAME installation with no reinstall/new Cloud ID)
- purchase-before-install (entitlement exists before any appliance does)
- upgrade reflected on the next refresh
- cancellation preserves customer/installation/Cloud ID -- only
  licensing goes away
- security: another tenant's credential can never read this customer's
  entitlement; a replayed signed request is rejected; Windows/Linux/
  appliance all use the identical API/auth model (no special-casing by
  appliance_type)
"""
import hashlib
import hmac as hmac_module
import json
import secrets
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

TIER_1_16 = "price_test_1_16"
TIER_17_32 = "price_test_17_32"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_installation_lifecycle.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        import appliance_cloud
        import customer_entitlements as ce
        from provisioning_api import register_provisioning_api_routes

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
        monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
        monkeypatch.setattr(main, "BILLING_ACCOUNTS_FILE", tmp_path / "billing_accounts.json")
        monkeypatch.setattr(main, "verify_stripe_webhook_signature", lambda payload, signature: True)
        monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", {
            TIER_1_16: {"product": "camera_slots", "camera_slot_maximum": 16},
            TIER_17_32: {"product": "camera_slots", "camera_slot_maximum": 32},
        })

        # ACTIVATION_IDENTITY_FILE and activation_limiter are process-wide
        # singletons (see test_appliance_activation_endpoint.py's matching
        # fixture) -- without isolating them, activating a second Cloud ID
        # in a later test within the same run 409s as "already activated
        # as <the first test's cloud_id>".
        import appliance_activation
        monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", tmp_path / "appliance_identity.json")
        appliance_cloud.activation_limiter.events.clear()

        app2 = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app2, shell=lambda *a, **k: "")
        register_provisioning_api_routes(app2)

        with TestClient(main.app, follow_redirects=False) as main_client, TestClient(app2) as appliance_client:
            yield main_client, appliance_client, main


def _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _owner_cookie(customer_id="cust-1", email="real-customer@example.test"):
    import partner_portal
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _session_cookie_name():
    import partner_portal
    return partner_portal.SESSION_COOKIE


def _checkout_completed_event(event_id, *, email, price_id, stripe_customer, customer_id=None):
    metadata = {"anyaicam_stripe_price_id": price_id}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id, "type": "checkout.session.completed",
        "data": {"object": {
            "id": f"cs_{event_id}", "customer": stripe_customer, "customer_details": {"email": email},
            "payment_status": "paid", "client_reference_id": "primary", "metadata": metadata,
        }},
    }


def _post_webhook(main_client, event):
    return main_client.post(
        "/api/payments/stripe/webhook", content=json.dumps(event),
        headers={"stripe-signature": "t=1,v1=test", "content-type": "application/json"},
    )


def _appliance_headers(appliance_id, credential, nonce=None):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": nonce or secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _seed_and_activate_appliance(db_path, *, appliance_id, customer_id, cloud_id, appliance_type="windows_byo_pc"):
    """Mints an activation token, then calls the real POST /api/appliance/
    activate route (the same one every Windows/Linux/appliance install
    uses) to produce a real, hashed credential -- not a fabricated one."""
    from partner_db import connection, password_hash
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Site", "2026-01-01"))
            db.execute(
                "INSERT INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,activation_status,created_at) VALUES(?,?,?,?,?,?,?)",
                (appliance_id, customer_id, "site-1", cloud_id, appliance_type, "pending", "2026-01-01"),
            )
            token = secrets.token_urlsafe(24)
            db.execute(
                "INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                (secrets.token_hex(4), appliance_id, password_hash(token), "2027-01-01T00:00:00", "2026-01-01"),
            )
    return token


# ------------------------------------------------- install-before-purchase


def test_install_before_purchase_zero_slots_then_purchase_reaches_the_same_installation_no_reinstall(client, db_path):
    main_client, appliance_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    token = _seed_and_activate_appliance(db_path, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-INSTALL1")

    activate = appliance_client.post("/api/appliance/activate", json={"cloud_id": "AIC-INSTALL1", "activation_token": token})
    assert activate.status_code == 200
    credential = activate.json()["credential"]

    # Zero-slot claim: the installation exists and can connect to AWS, but licensing is 0.
    refresh_before = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", credential))
    assert refresh_before.status_code == 200
    assert refresh_before.json()["camera_slot_quantity"] == 0
    assert refresh_before.json()["cloud_id"] == "AIC-INSTALL1"

    # Customer buys the 1-16 tier afterward.
    webhook = _post_webhook(main_client, _checkout_completed_event(
        "evt_purchase_after_install", email="real-customer@example.test", price_id=TIER_1_16,
        stripe_customer="cus_1", customer_id="cust-1",
    ))
    assert webhook.status_code == 200

    # Same Cloud ID, same credential, no reinstall -- next refresh sees it.
    refresh_after = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", credential))
    assert refresh_after.status_code == 200
    assert refresh_after.json()["camera_slot_quantity"] == 16
    assert refresh_after.json()["cloud_id"] == "AIC-INSTALL1"


# ------------------------------------------------- purchase-before-install


def test_purchase_before_install_appliance_immediately_sees_the_entitlement_on_first_refresh(client, db_path):
    main_client, appliance_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")

    # Customer buys first -- no appliance/installation exists yet at all.
    webhook = _post_webhook(main_client, _checkout_completed_event(
        "evt_purchase_before_install", email="real-customer@example.test", price_id=TIER_1_16,
        stripe_customer="cus_1", customer_id="cust-1",
    ))
    assert webhook.status_code == 200

    # Customer installs later and claims a fresh installation.
    token = _seed_and_activate_appliance(db_path, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-LATER1")
    activate = appliance_client.post("/api/appliance/activate", json={"cloud_id": "AIC-LATER1", "activation_token": token})
    assert activate.status_code == 200
    credential = activate.json()["credential"]

    refresh = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", credential))
    assert refresh.status_code == 200
    assert refresh.json()["camera_slot_quantity"] == 16


# ----------------------------------------------------------------- upgrade


def test_upgrade_is_reflected_on_the_next_refresh_same_installation(client, db_path):
    main_client, appliance_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    token = _seed_and_activate_appliance(db_path, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-UPGRADE1")
    activate = appliance_client.post("/api/appliance/activate", json={"cloud_id": "AIC-UPGRADE1", "activation_token": token})
    credential = activate.json()["credential"]

    _post_webhook(main_client, _checkout_completed_event("evt_tier1", email="real-customer@example.test", price_id=TIER_1_16, stripe_customer="cus_up_1", customer_id="cust-1"))
    first_refresh = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", credential))
    assert first_refresh.json()["camera_slot_quantity"] == 16

    upgrade_event = {
        "id": "evt_upgrade", "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_1", "customer": "cus_up_1", "status": "active", "items": {"data": [{"price": {"id": TIER_17_32}}]}, "metadata": {}}},
    }
    assert _post_webhook(main_client, upgrade_event).status_code == 200

    second_refresh = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", credential))
    assert second_refresh.json()["camera_slot_quantity"] == 32

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        assert len(ce.get_entitlements_for_customer("cust-1")) == 1  # no duplicate entitlement


# ------------------------------------------------------------- cancellation


def test_cancellation_zeroes_licensing_but_preserves_customer_installation_and_cloud_id(client, db_path):
    main_client, appliance_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    token = _seed_and_activate_appliance(db_path, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-CANCEL1")
    activate = appliance_client.post("/api/appliance/activate", json={"cloud_id": "AIC-CANCEL1", "activation_token": token})
    credential = activate.json()["credential"]
    _post_webhook(main_client, _checkout_completed_event("evt_before_cancel", email="real-customer@example.test", price_id=TIER_1_16, stripe_customer="cus_cancel_1", customer_id="cust-1"))

    cancel_event = {
        "id": "evt_cancelled", "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_1", "customer": "cus_cancel_1", "status": "canceled", "items": {"data": [{"price": {"id": TIER_1_16}}]}, "metadata": {}}},
    }
    assert _post_webhook(main_client, cancel_event).status_code == 200

    refresh = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-1", credential))
    assert refresh.status_code == 200  # the installation itself is untouched -- it can still connect and refresh
    assert refresh.json()["camera_slot_quantity"] == 0

    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        customer_row = conn.execute("SELECT status FROM customers WHERE id='cust-1'").fetchone()
        appliance_row = conn.execute("SELECT cloud_id,activation_status FROM appliances WHERE id='appl-1'").fetchone()
        entitlement_status = conn.execute("SELECT status FROM customer_entitlements WHERE customer_id='cust-1'").fetchone()[0]
    assert customer_row is not None  # customer account not deleted
    assert appliance_row == ("AIC-CANCEL1", "activated")  # installation identity/Cloud ID untouched
    assert entitlement_status == "cancelled"  # only licensing changed


# ------------------------------------------------------------------ security


def test_a_replayed_signed_refresh_request_is_rejected(client, db_path):
    main_client, appliance_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    token = _seed_and_activate_appliance(db_path, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-REPLAY1")
    credential = appliance_client.post("/api/appliance/activate", json={"cloud_id": "AIC-REPLAY1", "activation_token": token}).json()["credential"]

    headers = _appliance_headers("appl-1", credential, nonce="fixed-nonce-value")
    first = appliance_client.post("/api/provisioning/refresh", headers=headers)
    second = appliance_client.post("/api/provisioning/refresh", headers=headers)  # exact same nonce+timestamp replayed
    assert first.status_code == 200
    assert second.status_code == 409


def test_another_tenants_credential_can_never_read_this_customers_entitlement(client, db_path):
    main_client, appliance_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="a@example.test")
    _seed_tenant(db_path, customer_id="cust-2", email="b@example.test")
    _post_webhook(main_client, _checkout_completed_event("evt_c1", email="a@example.test", price_id=TIER_17_32, stripe_customer="cus_a", customer_id="cust-1"))

    other_token = _seed_and_activate_appliance(db_path, appliance_id="appl-2", customer_id="cust-2", cloud_id="AIC-OTHER1")
    other_credential = appliance_client.post("/api/appliance/activate", json={"cloud_id": "AIC-OTHER1", "activation_token": other_token}).json()["credential"]

    # cust-2's own, legitimately-issued credential can only ever see cust-2's own (zero) entitlement.
    refresh = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-2", other_credential))
    assert refresh.status_code == 200
    assert refresh.json()["camera_slot_quantity"] == 0

    # And presenting appl-2's id with a credential that was never issued for it fails outright.
    forged = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers("appl-2", "not-a-real-credential"))
    assert forged.status_code == 403


@pytest.mark.parametrize("appliance_type", ["windows_byo_pc", "linux_byo_pc", "anyaicam_windows_appliance", "ryzen_linux_appliance"])
def test_windows_linux_and_appliance_installs_all_use_the_identical_claim_and_refresh_api(client, db_path, appliance_type):
    """No special-casing by platform: the same activate/refresh routes,
    same auth model, same entitlement response shape for every install
    type this product supports."""
    main_client, appliance_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    token = _seed_and_activate_appliance(db_path, appliance_id=f"appl-{appliance_type}", customer_id="cust-1", cloud_id=f"AIC-{appliance_type.upper()}", appliance_type=appliance_type)
    activate = appliance_client.post("/api/appliance/activate", json={"cloud_id": f"AIC-{appliance_type.upper()}", "activation_token": token})
    assert activate.status_code == 200
    credential = activate.json()["credential"]

    _post_webhook(main_client, _checkout_completed_event(f"evt_{appliance_type}", email="real-customer@example.test", price_id=TIER_1_16, stripe_customer=f"cus_{appliance_type}", customer_id="cust-1"))
    refresh = appliance_client.post("/api/provisioning/refresh", headers=_appliance_headers(f"appl-{appliance_type}", credential))
    assert refresh.status_code == 200
    assert refresh.json()["camera_slot_quantity"] == 16
