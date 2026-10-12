"""Commercial restructure confirmed 2026-09-21, corrected same day:
POST /api/customer/analytics/checkout -- the customer-facing checkout
that was missing for Advanced Analytics / Face Access add-ons.

analytics_entitlements.py already had a fully working, tested webhook
side (resolve_addon()/_sync_checkout_completed()/upsert_analytics_
subscription(), already wired into the live POST /api/payments/stripe/
webhook route) -- but exactly the same gap test_camera_slot_checkout.py
documents for camera-slot tiers, nothing ever CREATED a Checkout Session
for one of these analytics Price IDs. This is that endpoint.

Correction (same day, before this ever shipped): Advanced Analytics is
ONE recurring, customer-billed add-on ("advanced_analytics") that grants
smart_motion/people_counting/lpr/ppe TOGETHER from one Stripe Price --
not four separate customer purchases. Face Access (facial_recognition)
remains its own separate recurring add-on. These tests exercise that
corrected model directly; there is no per-analytic-key checkout anymore.

stripe_api_post() (the actual network call to Stripe) is monkeypatched
to capture the `fields` list instead of making a real HTTP request --
matching test_camera_slot_checkout.py's own established convention.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target


# Billing reads Stripe's current state and applies nothing without it (Codex
# audit of 3f5b9c4, finding 9): these tests get a Stripe that follows their
# own events (conftest.stripe_follows_events).
pytestmark = pytest.mark.usefixtures("stripe_follows_events")


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_analytics_addon_checkout.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch, fake_stripe_prices):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_test_advanced_analytics")
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_FACIAL_RECOGNITION", "price_test_facial_recognition")
        monkeypatch.delenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", raising=False)

        captured = {}

        def _fake_stripe_api_post(path, fields, idempotency_key=None):
            if path == "/v1/customers":  # the account's canonical Stripe customer, one per account (checkout_guard.py)
                return {"id": "cus_test_" + str(idempotency_key).replace("anyaicam-customer-", "")}
            captured["path"] = path
            captured["fields"] = fields
            return {"id": "cs_test_analytics_1", "url": "https://checkout.stripe.test/cs_test_analytics_1"}

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


def _configure_founder_lpr(monkeypatch, *, customer_ids="cust-founder", duration="repeating", months="2"):
    import main

    monkeypatch.setenv("ANYAICAM_FOUNDER_LPR_CUSTOMER_IDS", customer_ids)
    monkeypatch.setenv("ANYAICAM_STRIPE_COUPON_FOUNDER_LPR", "coupon_founder_lpr")
    monkeypatch.setenv("ANYAICAM_FOUNDER_LPR_COUPON_DURATION", duration)
    if months is None:
        monkeypatch.delenv("ANYAICAM_FOUNDER_LPR_COUPON_DURATION_MONTHS", raising=False)
        months_value = None
    else:
        monkeypatch.setenv("ANYAICAM_FOUNDER_LPR_COUPON_DURATION_MONTHS", months)
        months_value = int(months)
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_VEHICLE_INTELLIGENCE", "price_test_vehicle_intelligence")
    original_get = main.stripe_api_get

    def stripe_get(path):
        if path.startswith("/v1/coupons/coupon_founder_lpr"):
            return {
                "id": "coupon_founder_lpr", "valid": True, "percent_off": 100,
                "amount_off": None, "duration": duration,
                "duration_in_months": months_value,
                "applies_to": {"products": ["prod_vehicle_intelligence"]},
            }
        if path == "/v1/prices/price_test_vehicle_intelligence":
            price = original_get(path)
            return {**price, "product": "prod_vehicle_intelligence"}
        return original_get(path)

    monkeypatch.setattr(main, "stripe_api_get", stripe_get)


# ------------------------------------------------------------ happy path


def test_advanced_analytics_creates_one_recurring_session_for_the_bundle(client, db_path):
    """The core correction: one addon_key, one Checkout Session, one
    Stripe Price -- not four."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "advanced_analytics"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["checkout_url"] == "https://checkout.stripe.test/cs_test_analytics_1"
    assert body["addon_key"] == "advanced_analytics"
    # 2026-09-30: Smart Motion is included with every plan, not sold.
    assert set(body["analytic_keys"]) == {"people_counting", "lpr", "ppe"}
    assert body["billing_type"] == "recurring"

    fields = _fields_dict(captured["fields"])
    assert fields["mode"] == "subscription"
    assert fields["line_items[0][price]"] == "price_test_advanced_analytics"
    # Exactly one line item for the whole bundle -- not four.
    assert fields["line_items[0][quantity]"] == "1"
    assert fields["metadata[anyaicam_stripe_price_id]"] == "price_test_advanced_analytics"
    assert fields["metadata[anyaicam_customer_id]"] == "cust-1"
    assert fields["metadata[anyaicam_addon_key]"] == "advanced_analytics"
    assert fields["subscription_data[metadata][anyaicam_stripe_price_id]"] == "price_test_advanced_analytics"
    assert fields["subscription_data[metadata][anyaicam_customer_id]"] == "cust-1"
    assert fields["client_reference_id"] == "cust-1"
    # The account's one canonical Stripe customer, never only an email (Codex audit of 3f5b9c4, finding 2).
    assert fields["customer"] == "cus_test_cust-1" and "customer_email" not in fields


def test_old_single_price_face_access_sku_is_no_longer_sold(client, db_path):
    """2026-09-30: Face Access is sold per door by size (enrolled people).
    The old single-price SKU still resolves for existing subscriptions
    (see the webhook test below) but can't start a new checkout."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "facial_recognition"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 400
    assert "per door" in response.json()["detail"]
    assert "fields" not in captured


# --------------------------------------------------------- ownership gate


def test_no_session_is_rejected(client):
    test_client, captured = client
    response = test_client.post("/api/customer/analytics/checkout", json={"addon_key": "advanced_analytics"})
    assert response.status_code == 401
    assert "path" not in captured, "Stripe must never be called for an unauthenticated request"


def test_partner_owner_session_is_rejected_not_treated_as_a_customer(client):
    test_client, captured = client
    import partner_portal
    non_owner_cookie = partner_portal._token("partner@example.test", "partner_owner", "partner-1", None, None)
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "advanced_analytics"},
        cookies={partner_portal.SESSION_COOKIE: non_owner_cookie},
    )
    assert response.status_code == 403
    assert "path" not in captured


def test_customer_viewer_role_is_rejected(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="viewer@example.test")
    import partner_portal
    viewer_cookie = partner_portal._token("viewer@example.test", "customer_viewer", None, "cust-1", None)
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "advanced_analytics"},
        cookies={partner_portal.SESSION_COOKIE: viewer_cookie},
    )
    assert response.status_code == 403
    assert "path" not in captured


# ------------------------------------------------ catalog/price validation


def test_unknown_addon_key_is_rejected(client, db_path):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "smart_motion"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 400, "smart_motion is an internal analytic_key, not a purchasable addon_key anymore"
    assert "path" not in captured


def test_addon_with_no_configured_price_id_fails_closed(client, db_path):
    """ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN is deliberately left
    unset by this file's own fixture -- a real catalog entry that
    simply isn't purchasable yet must be refused, never silently
    substituted."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "talk_down"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 503
    assert "PRICE_ID_REQUIRED" in response.json()["detail"]
    assert "path" not in captured


def test_a_submitted_price_id_field_is_ignored_server_side_resolution_wins(client, db_path):
    """Tampering guard: the request model has no price_id field at all,
    so even a client that adds one to the raw JSON body can never
    influence which Stripe price is actually charged."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "advanced_analytics", "price_id": "price_attacker_supplied"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])
    assert fields["line_items[0][price]"] == "price_test_advanced_analytics"


# --------------------------------------------- webhook-to-entitlement path


def test_webhook_grants_every_advanced_analytics_key_from_one_purchase(client, db_path, monkeypatch):
    """Integration proof that this endpoint's single-Price-ID checkout
    fans out into four independent analytics_subscriptions rows via the
    existing, unmodified webhook handler -- not just visually similar."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "advanced_analytics"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])

    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        from partner_db import connection
        monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
        event = {
            "id": "evt_test_1",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_analytics_1", "mode": "subscription", "subscription": "sub_1",
                "customer": "cus_test_1",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {
                    "anyaicam_stripe_price_id": fields["metadata[anyaicam_stripe_price_id]"],
                    "anyaicam_customer_id": fields["metadata[anyaicam_customer_id]"],
                },
            }},
        }
        result = ae.sync_analytics_from_stripe_event(event)
        assert result["status"] == "analytics_subscription_updated"
        assert set(result["analytic_keys"]) == {"people_counting", "lpr", "ppe"}
        assert len(result["subscription_ids"]) == 3
        assert ae.get_active_analytics_for_customer("cust-1") == ["lpr", "people_counting", "ppe"]


def test_founder_allowlisted_owner_gets_verified_zero_dollar_lpr_checkout(client, db_path, monkeypatch):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-founder", email="founder@example.test")
    _configure_founder_lpr(monkeypatch)

    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "vehicle_intelligence"},
        cookies={"anyaicam_partner_session": _owner_cookie(customer_id="cust-founder", email="founder@example.test")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["analytic_keys"] == ["lpr"]
    assert response.json()["discount"] == {
        "program": "founder_lpr_testing", "percent_off": 100,
        "duration": "repeating", "duration_months": 2,
    }
    fields = _fields_dict(captured["fields"])
    assert fields["line_items[0][price]"] == "price_test_vehicle_intelligence"
    assert fields["discounts[0][coupon]"] == "coupon_founder_lpr"
    assert fields["allow_promotion_codes"] == "false"
    assert fields["metadata[anyaicam_discount_program]"] == "founder_lpr_testing"
    assert fields["subscription_data[metadata][anyaicam_discount_program]"] == "founder_lpr_testing"
    assert fields["metadata[anyaicam_customer_id]"] == "cust-founder"


def test_unallowlisted_customer_cannot_request_founder_pricing(client, db_path, monkeypatch):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-other", email="other@example.test")
    _configure_founder_lpr(monkeypatch, customer_ids="cust-founder")

    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "vehicle_intelligence", "founder": True, "coupon_id": "coupon_founder_lpr"},
        cookies={"anyaicam_partner_session": _owner_cookie(customer_id="cust-other", email="other@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])
    assert "discounts[0][coupon]" not in fields
    assert fields["allow_promotion_codes"] == "true"
    assert "metadata[anyaicam_discount_program]" not in fields


def test_allowlisted_founder_discount_does_not_extend_to_other_analytics(client, db_path, monkeypatch):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-founder", email="founder@example.test")
    _configure_founder_lpr(monkeypatch)

    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "advanced_analytics"},
        cookies={"anyaicam_partner_session": _owner_cookie(customer_id="cust-founder", email="founder@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])
    assert fields["line_items[0][price]"] == "price_test_advanced_analytics"
    assert "discounts[0][coupon]" not in fields
    assert fields["allow_promotion_codes"] == "true"
    assert "metadata[anyaicam_discount_program]" not in fields


def test_founder_coupon_must_match_lpr_product_percent_and_configured_duration(client, db_path, monkeypatch):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-founder", email="founder@example.test")
    _configure_founder_lpr(monkeypatch)
    import main
    original_get = main.stripe_api_get

    def wrong_product_coupon(path):
        if path.startswith("/v1/coupons/coupon_founder_lpr"):
            return {"id": "coupon_founder_lpr", "valid": True, "percent_off": 100,
                    "amount_off": None, "duration": "forever", "duration_in_months": None,
                    "applies_to": {"products": ["prod_unrelated"]}}
        if path == "/v1/prices/price_test_vehicle_intelligence":
            return {**original_get(path), "product": "prod_vehicle_intelligence"}
        return original_get(path)

    monkeypatch.setattr(main, "stripe_api_get", wrong_product_coupon)
    response = test_client.post(
        "/api/customer/analytics/checkout", json={"addon_key": "vehicle_intelligence"},
        cookies={"anyaicam_partner_session": _owner_cookie(customer_id="cust-founder", email="founder@example.test")},
    )
    assert response.status_code == 503
    assert "path" not in captured, "an unverified coupon must never create a Stripe session"


@pytest.mark.parametrize(("duration", "months"), [("once", None), ("repeating", "6"), ("forever", None)])
def test_founder_coupon_duration_is_explicit_and_configurable(duration, months, client, db_path, monkeypatch):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-founder", email="founder@example.test")
    _configure_founder_lpr(monkeypatch, duration=duration, months=months)
    response = test_client.post(
        "/api/customer/analytics/checkout", json={"addon_key": "vehicle_intelligence"},
        cookies={"anyaicam_partner_session": _owner_cookie(customer_id="cust-founder", email="founder@example.test")},
    )
    assert response.status_code == 200, response.text
    assert _fields_dict(captured["fields"])["discounts[0][coupon]"] == "coupon_founder_lpr"


def test_zero_dollar_founder_checkout_activates_idempotent_lpr_entitlement_and_renewal(client, db_path, monkeypatch):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-founder", email="founder@example.test")
    _configure_founder_lpr(monkeypatch, duration="once", months=None)
    response = test_client.post(
        "/api/customer/analytics/checkout", json={"addon_key": "vehicle_intelligence"},
        cookies={"anyaicam_partner_session": _owner_cookie(customer_id="cust-founder", email="founder@example.test")},
    )
    assert response.status_code == 200, response.text
    fields = _fields_dict(captured["fields"])
    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        from partner_db import connection
        monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
        with connection() as db:
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-founder','cust-founder','Test Site','2026-01-01')")
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) "
                       "VALUES('appl-founder','cust-founder','site-founder','AIC-FOUNDER','2026-01-01')")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
                       "VALUES('cam-founder','cust-founder','site-founder','appl-founder',1,'Test Camera','2026-01-01')")
        event = {"id": "evt_founder_zero", "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_founder_zero", "mode": "subscription", "subscription": "sub_founder_lpr",
            "payment_status": "no_payment_required", "amount_total": 0,
            "customer": "cus_test_cust-founder", "customer_details": {"email": "founder@example.test"},
            "metadata": {"anyaicam_stripe_price_id": fields["metadata[anyaicam_stripe_price_id]"],
                         "anyaicam_customer_id": "cust-founder", "anyaicam_addon_key": "vehicle_intelligence"},
        }}}
        granted = ae.sync_analytics_from_stripe_event(event)
        assert granted["status"] == "analytics_subscription_updated"
        assert granted["analytic_keys"] == ["lpr"]
        assert ae.get_active_analytics_for_customer("cust-founder") == ["lpr"]

        renewed = {"id": "sub_founder_lpr", "type": "customer.subscription.updated", "data": {"object": {
            "id": "sub_founder_lpr", "customer": "cus_test_cust-founder", "status": "active",
            "metadata": {"anyaicam_customer_id": "cust-founder"},
            "items": {"data": [{"price": {"id": fields["metadata[anyaicam_stripe_price_id]"]}, "quantity": 1}]},
        }}}
        renewal_result = ae.sync_analytics_from_stripe_event(renewed)
        duplicate = ae.sync_analytics_from_stripe_event(event)
        assert renewal_result["status"] == "analytics_subscription_updated"
        assert duplicate["subscription_ids"] == granted["subscription_ids"]
        assert ae.get_active_analytics_for_customer("cust-founder") == ["lpr"]
        with connection() as db:
            assert db.execute("SELECT COUNT(*) FROM analytics_subscriptions WHERE customer_id=? AND analytic_key='lpr'",
                              ("cust-founder",)).fetchone()[0] == 1

    assignment = test_client.post(
        "/api/customer/cameras/cam-founder/analytics/lpr",
        cookies={"anyaicam_partner_session": _owner_cookie(customer_id="cust-founder", email="founder@example.test")},
    )
    assert assignment.status_code == 200, assignment.text
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            camera_entitlement = db.execute(
                "SELECT status FROM camera_analytics_entitlements WHERE camera_id='cam-founder' AND analytic_key='lpr'"
            ).fetchone()
            enabled = db.execute("SELECT lpr_enabled FROM cameras WHERE id='cam-founder'").fetchone()
    assert camera_entitlement["status"] == "active"
    assert enabled["lpr_enabled"] == 1


def test_cancelling_advanced_analytics_cancels_all_its_keys_together(client, db_path, monkeypatch):
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    response = test_client.post(
        "/api/customer/analytics/checkout",
        json={"addon_key": "advanced_analytics"},
        cookies={"anyaicam_partner_session": _owner_cookie(email="owner@example.test")},
    )
    fields = _fields_dict(captured["fields"])

    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
        checkout_event = {
            "id": "evt_checkout", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_1", "mode": "subscription", "subscription": "sub_1", "customer": "cus_test_1",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {
                    "anyaicam_stripe_price_id": fields["metadata[anyaicam_stripe_price_id]"],
                    "anyaicam_customer_id": fields["metadata[anyaicam_customer_id]"],
                },
            }},
        }
        ae.sync_analytics_from_stripe_event(checkout_event)
        assert len(ae.get_active_analytics_for_customer("cust-1")) == 3

        cancel_event = {
            "id": "evt_cancel", "type": "customer.subscription.deleted",
            "data": {"object": {
                "id": "sub_1", "customer": "cus_test_1", "status": "canceled",
                "items": {"data": [{"price": {"id": fields["metadata[anyaicam_stripe_price_id]"]}}]},
                "metadata": {},
            }},
        }
        result = ae.sync_analytics_from_stripe_event(cancel_event)
        assert result["status"] == "analytics_subscription_updated"
        assert len(result["analytic_keys"]) == 3
        assert ae.get_active_analytics_for_customer("cust-1") == []


def test_face_access_purchase_does_not_touch_advanced_analytics_keys(client, db_path, monkeypatch):
    """An existing Face Access subscription (old single-price SKU) still
    resolves on its webhook and grants only facial_recognition."""
    test_client, captured = client
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    fields = {"metadata[anyaicam_stripe_price_id]": "price_test_facial_recognition",
              "metadata[anyaicam_customer_id]": "cust-1"}

    with override_target(sqlite_path=str(db_path)):
        import analytics_entitlements as ae
        monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
        event = {
            "id": "evt_face", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_face", "mode": "subscription", "subscription": "sub_face", "customer": "cus_face",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {
                    "anyaicam_stripe_price_id": fields["metadata[anyaicam_stripe_price_id]"],
                    "anyaicam_customer_id": fields["metadata[anyaicam_customer_id]"],
                },
            }},
        }
        result = ae.sync_analytics_from_stripe_event(event)
        assert result["analytic_keys"] == ["facial_recognition"]
        assert ae.get_active_analytics_for_customer("cust-1") == ["facial_recognition"]


def test_webhook_ignores_an_unverified_price_id_grants_nothing(client, db_path):
    _seed_tenant(db_path, customer_id="cust-1", email="owner@example.test")
    with override_target(sqlite_path=str(db_path)):
        from analytics_entitlements import sync_analytics_from_stripe_event, get_active_analytics_for_customer
        event = {
            "id": "evt_test_2",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_forged",
                "customer_details": {"email": "owner@example.test"},
                "metadata": {"anyaicam_stripe_price_id": "price_never_configured", "anyaicam_customer_id": "cust-1"},
            }},
        }
        result = sync_analytics_from_stripe_event(event)
        assert result["status"] == "ignored"
        assert get_active_analytics_for_customer("cust-1") == []


# Pending-link (checkout-before-registration) and multi-addon-coexist
# coverage lives in test_analytics_entitlements.py, which tests
# analytics_entitlements.py's sync/pending-link functions directly and
# in more depth than this endpoint-focused file needs to duplicate.
