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


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_slot_checkout.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
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

        def _fake_stripe_api_post(path, fields):
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


def test_local_checkout_is_a_one_time_payment_session_with_full_metadata(client, db_path):
    """Business decision confirmed 2026-09-21: Local is a one-time
    purchase -- mode="payment", and Stripe rejects subscription_data
    params outright in that mode, so they must be completely absent
    from the request, not just unused."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "1-8"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["checkout_url"] == "https://checkout.stripe.test/cs_test_camera_1"
    assert body["camera_slot_maximum"] == 8
    assert body["billing_type"] == "one_time"

    fields = _fields_dict(captured["fields"])
    assert fields["mode"] == "payment"
    assert fields["line_items[0][price]"] == "price_test_local_1_8"
    # Fixed-tier purchase: always exactly one line item, never a
    # customer-submitted multiplier -- same discipline as the existing
    # license-tier checkout.
    assert fields["line_items[0][quantity]"] == "1"
    assert fields["metadata[anyaicam_stripe_price_id]"] == "price_test_local_1_8"
    assert fields["metadata[anyaicam_customer_id]"] == "cust-1"
    assert not any(key.startswith("subscription_data") for key in fields), (
        "mode=payment must never carry subscription_data params -- Stripe rejects the whole session if it does"
    )
    assert fields["client_reference_id"] == "cust-1"
    assert fields["customer_email"] == "owner@example.test"
    assert "customer/setup" in fields["success_url"]
    assert "customer/setup" in fields["cancel_url"]


def test_hybrid_checkout_is_still_a_recurring_subscription_session(client, db_path):
    """Cross-tier tampering guard (extended): local and hybrid must never
    resolve to each other's price, AND hybrid must still get mode=
    subscription with subscription_data metadata -- only Local's mode
    changed on 2026-09-21, Hybrid did not."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "hybrid", "tier_label": "1-8"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["billing_type"] == "recurring"
    fields = _fields_dict(captured["fields"])
    assert fields["mode"] == "subscription"
    assert fields["line_items[0][price]"] == "price_test_hybrid_1_8"
    assert fields["metadata[anyaicam_camera_slot_plan_type]"] == "hybrid"
    assert fields["subscription_data[metadata][anyaicam_stripe_price_id]"] == "price_test_hybrid_1_8"
    assert fields["subscription_data[metadata][anyaicam_customer_id]"] == "cust-1"


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


def test_partner_owner_session_is_rejected_not_treated_as_a_customer(client):
    """A partner/administrator session must never be able to purchase
    camera-slot capacity on some customer's behalf through this endpoint."""
    test_client, captured = client
    import partner_portal
    non_owner_cookie = partner_portal._token("partner@example.test", "partner_owner", "partner-1", None, None)
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "1-8"},
        cookies={partner_portal.SESSION_COOKIE: non_owner_cookie},
    )
    assert response.status_code == 403
    assert "path" not in captured


def test_customer_viewer_role_is_rejected(client, db_path):
    """customer_viewer (read-only) must not be able to purchase capacity
    -- only customer_owner."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="viewer@example.test")
    import partner_portal
    viewer_cookie = partner_portal._token("viewer@example.test", "customer_viewer", None, "cust-1", None)
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "1-8"},
        cookies={partner_portal.SESSION_COOKIE: viewer_cookie},
    )
    assert response.status_code == 403
    assert "path" not in captured


# --------------------------------------------------- tier/price validation


def test_unknown_tier_combination_is_rejected(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "999-1000"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 400
    assert "path" not in captured


def test_unknown_plan_type_is_rejected(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "enterprise", "tier_label": "1-8"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 400
    assert "path" not in captured


def test_tier_with_no_configured_price_id_fails_closed(client, db_path):
    """ANYAICAM_STRIPE_PRICE_LOCAL_9_16 is deliberately left unset by this
    file's own fixture -- a real PLAN_TIERS entry that simply isn't
    purchasable yet must be refused, never silently substituted with a
    different tier's price."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "9-16"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 503
    assert "PRICE_ID_REQUIRED" in response.json()["detail"]
    assert "path" not in captured


def test_a_submitted_price_id_field_is_ignored_server_side_resolution_wins(client, db_path):
    """Tampering guard: the request model has no price_id field at all,
    so even a client that adds one to the raw JSON body can never
    influence which Stripe price is actually charged -- server-side
    PLAN_TIERS lookup, keyed only by plan_type+tier_label, is the sole
    source, matching every other checkout endpoint's own discipline."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "1-8", "price_id": "price_attacker_supplied", "camera_slot_maximum": 999999},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])
    assert fields["line_items[0][price]"] == "price_test_local_1_8"
    assert response.json()["camera_slot_maximum"] == 8


# --------------------------------------------- webhook-to-entitlement path


def test_webhook_processes_the_exact_metadata_this_endpoint_sends_into_a_real_entitlement(client, db_path, monkeypatch):
    """Integration proof that this endpoint's metadata contract is
    actually compatible with the existing, unmodified webhook handler --
    not just visually similar. Builds a synthetic checkout.session.
    completed event using exactly the field NAMES create_camera_slot_
    checkout() sends (anyaicam_stripe_price_id, anyaicam_customer_id),
    then runs it through the real, unmodified sync_entitlement_from_
    stripe_event(), and confirms the DB ends up with an entitlement that
    total_camera_slots() (the same function the camera-provisioning gate
    calls) now correctly sums to 8 -- exactly the block from the live
    incident this endpoint fixes."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/camera-slots/checkout",
        json={"plan_type": "local", "tier_label": "1-8"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])

    with override_target(sqlite_path=str(db_path)):
        import customer_entitlements
        # PRICE_ID_CAMERA_SLOT_MAP is built once at module-import time
        # from the env vars present then (see _load_price_tier_map()'s
        # own docstring) -- pre-existing, unrelated-to-this-endpoint
        # behavior. Rebuilding it here against this test's env vars is
        # test-only plumbing so resolve_tier() can see the same fake
        # price id this fixture configured; production reads the real
        # env at process startup exactly once, same as today.
        monkeypatch.setattr(customer_entitlements, "PRICE_ID_CAMERA_SLOT_MAP", customer_entitlements._load_price_tier_map())
        event = {
            "id": "evt_test_1",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_camera_1",
                "customer": "cus_test_1",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {
                    "anyaicam_stripe_price_id": fields["metadata[anyaicam_stripe_price_id]"],
                    "anyaicam_customer_id": fields["metadata[anyaicam_customer_id]"],
                },
            }},
        }
        result = customer_entitlements.sync_entitlement_from_stripe_event(event)
        assert result["status"] == "entitlement_updated"
        assert result["customer_id"] == "cust-1"
        assert customer_entitlements.total_camera_slots("cust-1") == 8


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
