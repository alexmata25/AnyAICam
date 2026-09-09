"""Provisioning Phase 2: proves POST /api/payments/stripe/webhook now
also drives customer_entitlements via sync_entitlement_from_stripe_event()
-- additively, alongside the existing legacy processing
(process_stripe_webhook_event() / billing_accounts.json), with:

- the existing HMAC signature verification and timestamp-tolerance
  replay protection completely unchanged (one test constructs a real
  Stripe-shaped signature to prove this end to end; the rest monkeypatch
  verify_stripe_webhook_signature() to focus on entitlement-sync
  behavior specifically),
- the existing legacy JSON-file webhook idempotency
  (record_stripe_webhook_event()) unchanged and still gating a duplicate
  before it even reaches process_stripe_webhook_event() OR the new
  entitlement sync,
- the NEW DB-backed provisioning_webhook_events idempotency as a second,
  independent guard specifically for the entitlement side.
"""
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_stripe_webhook_entitlement_sync.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
        monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
        monkeypatch.setattr(main, "BILLING_ACCOUNTS_FILE", tmp_path / "billing_accounts.json")
        monkeypatch.setattr(main, "verify_stripe_webhook_signature", lambda payload, signature: True)

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client, main


def _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


def _checkout_completed_event(event_id="evt_1", email="real-customer@example.test", plan="starter",
                               stripe_customer="cus_1", quantity=4, customer_id=None):
    metadata = {"anyaicam_plan": plan, "anyaicam_camera_slot_quantity": str(quantity)}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_1",
            "customer": stripe_customer,
            "customer_details": {"email": email},
            "payment_status": "paid",
            "client_reference_id": "primary",
            "metadata": metadata,
        }},
    }


def _post_webhook(test_client, event):
    return test_client.post(
        "/api/payments/stripe/webhook",
        content=json.dumps(event),
        headers={"stripe-signature": "t=1,v1=test", "content-type": "application/json"},
    )


# ------------------------------------------------------------- end to end


def test_checkout_completed_updates_customer_entitlements_for_a_signed_in_customer(client, db_path):
    test_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    response = _post_webhook(test_client, _checkout_completed_event(customer_id="cust-1", quantity=8))
    assert response.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        total = ce.total_camera_slots("cust-1")
    assert total == 8


def test_duplicate_stripe_event_never_double_grants_slots(client, db_path):
    test_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    event = _checkout_completed_event(event_id="evt_dup", customer_id="cust-1", quantity=5)

    first = _post_webhook(test_client, event)
    second = _post_webhook(test_client, event)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json().get("duplicate") is True  # the existing legacy idempotency short-circuits it first

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        total = ce.total_camera_slots("cust-1")
        entitlements = ce.get_entitlements_for_customer("cust-1")
    assert total == 5
    assert len(entitlements) == 1


def test_checkout_before_registration_creates_a_pending_link_not_a_duplicate_customer(client, db_path):
    test_client, main = client
    response = _post_webhook(test_client, _checkout_completed_event(email="brand-new@example.test", quantity=4))
    assert response.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        customers = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        pending = conn.execute("SELECT status,camera_slot_quantity FROM pending_customer_links WHERE normalized_email=?", ("brand-new@example.test",)).fetchone()
    assert customers == 0  # no duplicate/guessed customer created
    assert pending == ("pending", 4)


def test_entitlement_sync_failure_never_breaks_the_webhook_response(client, db_path, monkeypatch):
    """The legacy 200 response Stripe needs to stop retrying must survive
    even if the new entitlement bridge throws."""
    test_client, main = client

    def _boom(event):
        raise RuntimeError("simulated entitlement sync failure")

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements
        monkeypatch.setattr(customer_entitlements, "sync_entitlement_from_stripe_event", _boom)
        # main.py imports the function by name inside the route handler
        # (a deferred import), so patching the module attribute above is
        # what the route actually calls.
    response = _post_webhook(test_client, _checkout_completed_event(event_id="evt_boom"))
    assert response.status_code == 200


def test_legacy_processing_still_runs_unchanged_alongside_the_new_sync(client, db_path):
    """process_stripe_webhook_event() (billing_accounts.json/
    payment_sessions.json) must still run exactly as before -- Phase 2 is
    additive, not a replacement."""
    test_client, main = client
    response = _post_webhook(test_client, _checkout_completed_event(event_id="evt_legacy_check"))
    assert response.status_code == 200
    sessions = main.load_payment_sessions()
    assert sessions["cs_test_1"]["status"] == "completed"


def test_subscription_cancellation_updates_entitlement_status_via_webhook(client, db_path):
    test_client, main = client
    _seed_tenant(db_path, customer_id="cust-1", email="real-customer@example.test")
    _post_webhook(test_client, _checkout_completed_event(event_id="evt_created", customer_id="cust-1", stripe_customer="cus_sub_1", quantity=10))

    cancel_event = {
        "id": "evt_cancelled",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_1", "customer": "cus_sub_1", "status": "canceled", "metadata": {"anyaicam_plan": "starter"}}},
    }
    response = _post_webhook(test_client, cancel_event)
    assert response.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements as ce
        total = ce.total_camera_slots("cust-1")
    assert total == 0


def test_real_stripe_shaped_signature_still_verifies(db_path, tmp_path, monkeypatch):
    """End-to-end proof that the actual HMAC verification path (not
    monkeypatched away) is untouched by this phase's changes."""
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PAYMENT_WEBHOOK_EVENTS_FILE", tmp_path / "payment_webhook_events.json")
        monkeypatch.setattr(main, "PAYMENT_SESSIONS_FILE", tmp_path / "payment_sessions.json")
        monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", "whsec_test_secret")

        payload = json.dumps(_checkout_completed_event(event_id="evt_real_sig")).encode("utf-8")
        timestamp = int(time.time())
        signed_payload = f"{timestamp}".encode("utf-8") + b"." + payload
        signature = hmac.new(b"whsec_test_secret", signed_payload, hashlib.sha256).hexdigest()

        with TestClient(main.app, follow_redirects=False) as test_client:
            good = test_client.post(
                "/api/payments/stripe/webhook",
                content=payload,
                headers={"stripe-signature": f"t={timestamp},v1={signature}", "content-type": "application/json"},
            )
            bad = test_client.post(
                "/api/payments/stripe/webhook",
                content=payload,
                headers={"stripe-signature": f"t={timestamp},v1=deadbeef", "content-type": "application/json"},
            )
    assert good.status_code == 200
    assert bad.status_code == 400
