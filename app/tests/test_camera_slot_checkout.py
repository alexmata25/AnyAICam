"""Provisioning Phase 8: POST /api/customer/camera-slots/checkout --
the customer-facing checkout that was missing for camera-slot capacity.

Confirmed live on anyaicam-staging (2026-09-12): a real, newly-activated
appliance with zero configured cameras was refused at its very first
camera-provisioning attempt with "Camera limit reached... licensed for
0 camera(s)". customer_entitlements.PLAN_TIERS already had real,
configured Stripe test-mode Price IDs, and the webhook side (resolve_
tier()/_sync_checkout_completed()/upsert_entitlement()) already
correctly turned a completed session into a real entitlement -- but
nothing in the app ever CREATED a Checkout Session for one of those
prices. The only existing purchase button, POST /api/payments/checkout,
is for a completely different product line (starter/professional/
enterprise software license tiers).

stripe_api_post() (the actual network call to Stripe) is monkeypatched
to capture the `fields` list instead of making a real HTTP request --
this file never talks to Stripe, matching test_stripe_checkout_
authoritative_identity.py's own established convention exactly.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


@pytest.fixture(autouse=True)
def _legacy_new_purchases_switched_on(monkeypatch):
    """These tests drive the legacy fixed-capacity checkout to exercise the
    machinery it shares with grandfathered accounts (one payable session,
    canonical Stripe customer, price guard, F&F coupons, webhook grant).
    In production new legacy purchases are off (billing v2, 2026-10-05);
    that default is covered by test_legacy_checkout_guard.py."""
    import main
    pass  # fixed camera-slot tiers removed 2026-10-08


# Billing reads Stripe's current state and applies nothing without it (Codex
# audit of 3f5b9c4, finding 9): these tests get a Stripe that follows their
# own events (conftest.stripe_follows_events).
pytestmark = pytest.mark.usefixtures("stripe_follows_events")


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_slot_checkout.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch, fake_stripe_prices):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_LOCAL_1_8", "price_test_local_1_8")
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", "price_test_hybrid_1_8")
        monkeypatch.delenv("ANYAICAM_STRIPE_PRICE_LOCAL_9_16", raising=False)

        captured = {}

        def _fake_stripe_api_post(path, fields, idempotency_key=None):
            if path == "/v1/customers":  # the account's canonical Stripe customer, one per account (checkout_guard.py)
                return {"id": "cus_test_" + str(idempotency_key).replace("anyaicam-customer-", "")}
            captured["path"] = path
            captured["fields"] = fields
            return {"id": "cs_test_camera_1", "url": "https://checkout.stripe.test/cs_test_camera_1"}

        monkeypatch.setattr(main, "stripe_api_post", _fake_stripe_api_post)

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client, captured


def _fields_dict(fields):
    return dict(fields)


def _owner_cookie(customer_id="cust-1", email="signedin@example.test"):
    import partner_portal
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _seed_tenant(db_path, customer_id="cust-1", email="signedin@example.test", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", email, "active", "2026-01-01"),
        )
        conn.commit()


# ------------------------------------------------------------ happy path


# --------------------------------------------------------- ownership gate


def test_no_session_is_rejected(client):
    """No session at all never reaches this endpoint's own role check --
    authentication_middleware's own /api/* gate (401) catches it first,
    same as every other /api/customer/* route in this app."""
    test_client, captured = client
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "1-8"},
    )
    assert response.status_code == 401
    assert "path" not in captured, "Stripe must never be called for an unauthenticated request"


# --------------------------------------------------- tier/price validation


# --------------------------------------------- webhook-to-entitlement path


def test_webhook_ignores_an_unverified_price_id_grants_nothing(client, db_path):
    """A price id the server never configured (e.g. a forged/stale
    metadata value) must never grant slots -- resolve_tier()'s own
    fail-closed, Price-ID-keyed-only design."""
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import sync_entitlement_from_stripe_event, total_camera_slots
        event = {
            "id": "evt_test_2",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_forged",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {"anyaicam_stripe_price_id": "price_never_configured", "anyaicam_customer_id": "cust-1"},
            }},
        }
        result = sync_entitlement_from_stripe_event(event)
        assert result["status"] == "ignored"
        assert total_camera_slots("cust-1") == 0
