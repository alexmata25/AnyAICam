"""Focused acceptance coverage for the additive per-camera billing v2 model."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "per_camera_billing_v2.db"


@pytest.fixture(autouse=True)
def initialized_db(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        yield


def seed_customer(db_path, *, customer_id="cust-v2", email="v2@example.test"):
    with override_target(sqlite_path=str(db_path)):
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-v2','Partner','2026-10-04')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                       (customer_id, "partner-v2", "V2 Customer", email, "active", "2026-10-04"))


def test_v2_catalog_is_customer_safe_and_keeps_premium_analytics_separate():
    import per_camera_billing as billing
    catalog = billing.public_catalog()
    assert catalog["catalog_version"] == 2
    assert catalog["quantity"] == {"minimum": 1, "maximum": 64}
    assert catalog["continuous_cloud_recording_included"] is False
    plans = {plan["plan_key"]: plan for plan in catalog["plans"]}
    assert [plans[key]["monthly_per_camera_amount"] for key in ("basic_local", "ai_local", "hybrid")] == [9.99, 14.99, 24.99]
    assert plans["basic_local"]["local_retention_days"] == 2
    assert plans["ai_local"]["local_retention_days"] == plans["hybrid"]["local_retention_days"] == 7
    assert plans["hybrid"]["cloud_event_retention_days"] == 14
    assert plans["hybrid"]["cloud_event_storage"] is True
    assert plans["ai_local"]["cloud_event_storage"] is False
    assert all(plan["resolution_classes"] == ["standard"] for plan in plans.values())
    assert "supported_talk_down" in {item["key"] for item in plans["ai_local"]["included_features"]}
    assert catalog["resolution_classes"] == [
        {"key": "standard", "display_name": "Standard", "maximum_megapixels": 4},
        {"key": "high_resolution", "display_name": "High Resolution", "minimum_megapixels": 5, "maximum_megapixels": 8},
    ]
    serialized = str(catalog).lower()
    assert "stripe_price_id" not in serialized and "commission" not in serialized


@pytest.mark.parametrize("quantity", [0, 65, True])
def test_v2_checkout_request_rejects_invalid_camera_quantity(quantity):
    from per_camera_billing import CheckoutRequest
    with pytest.raises(ValidationError):
        CheckoutRequest(plan_key="ai_local", camera_quantity=quantity)


def test_v2_checkout_request_rejects_client_supplied_price_id():
    from per_camera_billing import CheckoutRequest
    with pytest.raises(ValidationError):
        CheckoutRequest(plan_key="ai_local", camera_quantity=6, price_id="price_client_choice")


def test_v2_price_resolution_fails_closed_and_maps_only_configured_allowlist(monkeypatch):
    import per_camera_billing as billing
    for env in billing.PRICE_ENV.values():
        monkeypatch.delenv(env, raising=False)
    assert billing.resolve_price("price_not_configured") is None
    monkeypatch.setenv(billing.PRICE_ENV["ai_local"], "price_test_ai_local")
    resolved = billing.resolve_price("price_test_ai_local")
    assert resolved["plan_key"] == "ai_local"
    assert resolved["product_class"] == "base_v2_commissionable"
    assert billing.resolve_price("price_some_other_plan") is None


def test_current_stripe_item_creates_exact_versioned_entitlement_and_capacity(db_path, monkeypatch):
    import customer_entitlements as ce
    import per_camera_billing as billing
    import stripe_state
    seed_customer(db_path)
    monkeypatch.setenv(billing.PRICE_ENV["ai_local"], "price_test_ai_local")
    sub = {"id": "sub_v2", "customer": "cus_v2", "status": "active", "metadata": {"anyaicam_customer_id": "cust-v2"},
           "current_period_end": 1800000000, "items": {"data": [{"id": "si_v2", "quantity": 6,
               "price": {"id": "price_test_ai_local"}}]}}
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _subscription: None)
    result = billing._upsert_current(sub)
    assert result["status"] == "entitlement_updated"
    assert result["camera_quantity"] == 6
    entitlement = billing.entitlement_for_customer("cust-v2")
    assert entitlement["plan_key"] == "ai_local"
    assert entitlement["stripe_subscription_item_id"] == "si_v2"
    assert entitlement["local_retention_days"] == 7
    assert entitlement["cloud_event_storage"] == 0
    assert ce.total_camera_slots("cust-v2") == 6
    assert ce.vms_license_capacity("cust-v2") == 6
    assert ce.usable_camera_capacity("cust-v2") == 6
    assert ce.product_mode_for_customer("cust-v2") == "local"
    payload = billing.entitlement_payload(entitlement)
    assert payload["monthly_per_camera_amount"] == 14.99
    assert payload["monthly_base_total"] == 89.94


@pytest.mark.parametrize("plan_key,retention,cloud", [("basic_local", 2, False), ("ai_local", 7, False), ("hybrid", 7, True)])
def test_local_retention_baseline_is_used_only_when_no_customer_override(db_path, monkeypatch, plan_key, retention, cloud):
    import per_camera_billing as billing
    import stripe_state
    import local_storage_policy
    seed_customer(db_path)
    monkeypatch.setenv(billing.PRICE_ENV[plan_key], f"price_{plan_key}")
    sub = {"id": f"sub_{plan_key}", "customer": "cus_v2", "status": "active", "metadata": {"anyaicam_customer_id": "cust-v2"},
           "items": {"data": [{"id": "si", "quantity": 1, "price": {"id": f"price_{plan_key}"}}]}}
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _subscription: None)
    billing._upsert_current(sub)
    with override_target(sqlite_path=str(db_path)), __import__("partner_db").connection() as db:
        policy = local_storage_policy.local_storage_policy_for_customer(db, "cust-v2")
        assert policy["local_retention_days"] == retention
        from event_media_policy import motion_event_policy
        event_policy = motion_event_policy(db, "cust-v2")
        assert bool(event_policy) is cloud
        if cloud:
            assert event_policy == {"retention_days": 14}


def test_v2_suspension_and_recovery_preserve_the_paid_quantity(db_path, monkeypatch):
    import per_camera_billing as billing
    import stripe_state
    import billing_status
    seed_customer(db_path)
    monkeypatch.setenv(billing.PRICE_ENV["hybrid"], "price_hybrid")
    sub = {"id": "sub_recover", "customer": "cus_v2", "status": "active", "metadata": {"anyaicam_customer_id": "cust-v2"},
           "items": {"data": [{"id": "si", "quantity": 3, "price": {"id": "price_hybrid"}}]}}
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _subscription: None)
    billing._upsert_current(sub)
    assert billing_status._suspend("sub_recover", "payment_failed") == 1
    assert billing.active_quantity("cust-v2") == 0
    assert billing.entitlement_for_customer("cust-v2")["camera_quantity"] == 3
    assert billing_status.payment_recovered("sub_recover") == 1
    assert billing.active_quantity("cust-v2") == 3


def test_v2_my_subscription_shows_plan_and_does_not_offer_included_talk_down(db_path, monkeypatch):
    import per_camera_billing as billing
    import stripe_state
    seed_customer(db_path)
    monkeypatch.setenv(billing.PRICE_ENV["ai_local"], "price_ai")
    sub = {"id": "sub_ui", "customer": "cus_v2", "status": "active", "metadata": {"anyaicam_customer_id": "cust-v2"},
           "items": {"data": [{"id": "si", "quantity": 6, "price": {"id": "price_ai"}}]}}
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _subscription: None)
    billing._upsert_current(sub)
    import partner_portal
    html = billing.customer_portal_page({"customer_id": "cust-v2", "role": "customer_owner"}, billing.entitlement_for_customer("cust-v2"))
    assert "AI Local" in html and "$14.99 per camera/month" in html and "$89.94 monthly base total" in html
    assert "7-day local retention" in html
    assert "Talk Down / two-way audio on supported cameras" in html
    assert 'data-addon-key="talk_down"' not in html
    assert "Premium Add-ons" in html and "Advanced People Counting" in html
    assert 'id="friends-family-panel"' in html


@pytest.mark.parametrize("plan_key,unit_cents", [("basic_local", 999), ("ai_local", 1499), ("hybrid", 2499)])
def test_friends_family_base_coupon_applies_to_camera_quantity(plan_key, unit_cents, db_path, monkeypatch):
    import friends_family
    import pricing_catalog
    seed_customer(db_path)
    request, _ = friends_family.create_request(customer_id="cust-v2", email="v2@example.test")
    friends_family.decide(request["id"], approve=True, decided_by="admin@example.test")
    monkeypatch.setenv("ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_BASE", "coupon_test_base_50")

    quantity = 6
    fields = [("line_items[0][price]", f"price_{plan_key}"), ("line_items[0][quantity]", str(quantity))]
    discount = friends_family.apply_to_checkout_fields(fields, "cust-v2", "base")
    field_map = dict(fields)
    regular_total_cents = unit_cents * quantity

    assert discount["friends_family"] is True
    assert pricing_catalog.friends_family_percent("base") == 50
    assert field_map["line_items[0][quantity]"] == "6"
    assert field_map["discounts[0][coupon]"] == "coupon_test_base_50"
    assert regular_total_cents == unit_cents * 6
    if plan_key == "ai_local":
        assert regular_total_cents == 8994
        assert regular_total_cents // 2 == 4497


@pytest.mark.parametrize("discount_class,coupon_env,coupon", [
    ("analytics", "ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_ANALYTICS", "coupon_test_analytics_25"),
    ("hardware", None, None),
    ("vms_license", None, None),
    ("face_access", None, None),
])
def test_friends_family_v2_eligible_analytics_and_excluded_products(
        discount_class, coupon_env, coupon, db_path, monkeypatch):
    import friends_family
    import pricing_catalog
    seed_customer(db_path)
    request, _ = friends_family.create_request(customer_id="cust-v2", email="v2@example.test")
    friends_family.decide(request["id"], approve=True, decided_by="admin@example.test")
    if coupon_env:
        monkeypatch.setenv(coupon_env, coupon)

    discount = friends_family.checkout_discount("cust-v2", discount_class)
    assert discount["friends_family"] is True
    assert pricing_catalog.friends_family_percent(discount_class) == (25 if discount_class == "analytics" else 0)
    assert discount["coupon"] == coupon
    assert discount["allow_promotion_codes"] is (coupon is None)


@pytest.mark.parametrize("plan_key,has_separate_talk_down_add_action", [
    ("basic_local", True), ("ai_local", False), ("hybrid", False),
])
def test_included_talk_down_never_becomes_a_separate_discounted_addon(
        plan_key, has_separate_talk_down_add_action, db_path, monkeypatch):
    import per_camera_billing as billing
    import stripe_state
    seed_customer(db_path)
    monkeypatch.setenv(billing.PRICE_ENV[plan_key], f"price_{plan_key}")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", "price_test_talk_down")
    monkeypatch.setenv("ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_ANALYTICS", "coupon_test_analytics_25")
    sub = {"id": f"sub_{plan_key}_talk", "customer": "cus_v2", "status": "active",
           "metadata": {"anyaicam_customer_id": "cust-v2"},
           "items": {"data": [{"id": "si", "quantity": 2, "price": {"id": f"price_{plan_key}"}}]}}
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _subscription: None)
    billing._upsert_current(sub)
    html = billing.customer_portal_page(
        {"customer_id": "cust-v2", "role": "customer_owner"}, billing.entitlement_for_customer("cust-v2"))
    offered = 'data-addon-key="talk_down"' in html
    assert offered is has_separate_talk_down_add_action
    if plan_key in {"ai_local", "hybrid"}:
        assert "Talk Down / two-way audio on supported cameras" in html


def test_basic_local_does_not_start_or_consume_v2_commission_window(db_path, monkeypatch):
    import per_camera_billing as billing
    import sales_commissions
    seed_customer(db_path)
    monkeypatch.setenv(billing.PRICE_ENV["basic_local"], "price_basic")
    monkeypatch.setenv(billing.PRICE_ENV["ai_local"], "price_ai")
    invoice = {"lines": {"data": [
        {"amount": 5994, "price": {"id": "price_basic"}},
        {"amount": 1000, "price": {"id": "price_known_addon"}},
    ]}}
    monkeypatch.setattr(sales_commissions, "_classify", lambda price: ("base_v2_noncommissionable", {"commission_percent": 0}) if price == "price_basic" else ("addon", {}))
    assert sales_commissions._commissionable_invoice_fraction(invoice) == pytest.approx(1000 / 6994)
    with override_target(sqlite_path=str(db_path)), __import__("partner_db").connection() as db:
        db.execute("INSERT INTO subscription_payments(id,customer_id,product_class,amount_paid_cents,paid_at,status) VALUES(?,?,?,?,?,?)",
                   ("basic_invoice", "cust-v2", "base_v2_noncommissionable", 5994, "2026-10-04", "paid"))
        assert sales_commissions._per_camera_commission_paid_months("cust-v2") == 0
        db.execute("INSERT INTO subscription_payments(id,customer_id,product_class,amount_paid_cents,paid_at,status) VALUES(?,?,?,?,?,?)",
                   ("ai_invoice", "cust-v2", "base_v2_commissionable", 8994, "2026-11-04", "paid"))
        assert sales_commissions._per_camera_commission_paid_months("cust-v2") == 1


def test_activation_payout_remains_legacy_only(db_path, monkeypatch):
    import sales_commissions
    seed_customer(db_path, customer_id="legacy-activation")
    seed_customer(db_path, customer_id="basic-activation")
    seed_customer(db_path, customer_id="ai-activation")
    recorded = []
    monkeypatch.setattr(sales_commissions, "attribution_for", lambda _customer: {"salesperson_user_id": "sales-v2"})
    monkeypatch.setattr(sales_commissions, "_verified_invoice_owner",
                        lambda invoice, _subscription: (invoice["customer"], ""))
    monkeypatch.setattr(sales_commissions, "_ff_approved", lambda _customer: False)
    monkeypatch.setattr(sales_commissions, "_invoice_payment_intent", lambda _invoice: None)
    monkeypatch.setattr(sales_commissions, "_record", lambda **kwargs: recorded.append(kwargs) or {"id": "commission"})
    products = {
        "base": {"product": "camera_slots_local", "camera_slot_maximum": 8, "commission_percent": 20},
        "base_v2_noncommissionable": {"commission_percent": 0},
        "base_v2_commissionable": {"commission_percent": 20},
    }
    monkeypatch.setattr(sales_commissions, "_classify_invoice", lambda invoice:
                        (invoice["fixture_class"], products[invoice["fixture_class"]], "price_fixture", 1.0))
    monkeypatch.setattr(sales_commissions, "_commissionable_invoice_fraction", lambda invoice:
                        0.0 if invoice["fixture_class"] == "base_v2_noncommissionable" else 1.0)

    results = {}
    for customer_id, product_class in (
        ("legacy-activation", "base"),
        ("basic-activation", "base_v2_noncommissionable"),
        ("ai-activation", "base_v2_commissionable"),
    ):
        event = {"data": {"object": {
            "id": f"invoice-{customer_id}", "subscription": f"sub-{customer_id}",
            "customer": customer_id, "amount_paid": 5000, "currency": "usd",
            "fixture_class": product_class,
        }}}
        results[product_class] = sales_commissions._invoice_paid(event)

    legacy_kinds = [item["kind"] for item in recorded if item["customer_id"] == "legacy-activation"]
    basic_kinds = [item["kind"] for item in recorded if item["customer_id"] == "basic-activation"]
    ai_kinds = [item["kind"] for item in recorded if item["customer_id"] == "ai-activation"]
    assert legacy_kinds == ["activation", "recurring"]
    assert basic_kinds == []
    assert ai_kinds == ["recurring"]
    assert results["base_v2_noncommissionable"]["paid_month_index"] == 0
    assert results["base_v2_commissionable"]["paid_month_index"] == 1


def test_old_checkout_guard_does_not_require_v2_provisioning_for_legacy_sessions():
    import checkout_guard
    assert checkout_guard._provisioned("no_step_rows", {"metadata": {"anyaicam_billing_version": "1"}}) is False
    assert checkout_guard.PROVISIONING_STEPS == ("camera_slot_entitlements", "analytics_entitlements", "hardware_orders")
