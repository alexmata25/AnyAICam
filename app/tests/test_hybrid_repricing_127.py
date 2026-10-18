"""Hybrid repricing (owner approval 2026-10-17).

Basic Local $9.99, AI Local $14.99 and Hybrid $19.99 (was $24.99) per camera per month. Stripe Prices are immutable:
new checkouts need the new $19.99 Price (the price guard refuses the old one), while subscriptions still on an
earlier Hybrid Price stay recognized and are shown at what THAT Price bills: ANYAICAM_STRIPE_PRICE_HYBRID_PREVIOUS
entries carry their own amount ("price_id:cents"), so no swap of values can show a subscriber another Price's amount
(Codex 2026-10-18). What existing subscribers pay is an open owner decision, so nothing here moves them.
The unapproved remote-phone-support feature line is NOT part of the pricing-only hotfix (Codex 2026-10-18).
"""
import pytest
from fastapi import HTTPException

import per_camera_billing as billing

OLD, NEW = "price_hybrid_2499_previous", "price_hybrid_1999"


@pytest.fixture()
def prices(monkeypatch):
    monkeypatch.setenv(billing.PRICE_ENV["hybrid"], NEW)
    monkeypatch.setenv(billing.PREVIOUS_PRICE_ENV["hybrid"], f"{OLD}:2499")
    monkeypatch.setenv(billing.PRICE_ENV["ai_local"], "price_ai_1499")
    monkeypatch.setenv(billing.PRICE_ENV["basic_local"], "price_basic_999")


BASE = {"plan_key": "hybrid", "camera_quantity": 4, "status": "active", "cloud_event_storage": 1,
        "cloud_event_retention_days": 14, "local_retention_days": 7}


def test_the_three_per_camera_prices():
    assert {key: plan["monthly_cents_per_camera"] for key, plan in billing.PLANS.items()} == {
        "basic_local": 999, "ai_local": 1499, "hybrid": 1999}


@pytest.mark.parametrize("cameras,monthly_cents", [(1, 1999), (4, 7996), (10, 19990)])
def test_hybrid_monthly_totals_for_1_4_and_10_cameras(cameras, monthly_cents):
    import build_orders
    assert build_orders.order_quote({"plan": "hybrid", "cameras": cameras})["monthly_cents"] == monthly_cents


def test_no_plan_gains_a_feature_in_the_pricing_only_hotfix():
    assert "remote_phone_support" not in billing.FEATURE_LABELS
    assert not hasattr(billing, "REMOTE_PHONE_SUPPORT")
    for key, plan in billing.PLANS.items():
        assert "remote_phone_support" not in plan["features"], key


def test_hybrid_keeps_every_cloud_feature_and_retention():
    hybrid = billing.PLANS["hybrid"]
    assert hybrid["cloud_event_storage"] is True and hybrid["cloud_event_retention_days"] == 14
    assert hybrid["local_retention_days"] == 7 and hybrid["mode"] == "hybrid" and hybrid["commission_percent"] == 20
    for feature in ("hybrid_cloud_services", "remote_cloud_services", "cloud_event_protection", "smart_motion",
                    "core_aaco_retrieval", "supported_talk_down"):
        assert feature in hybrid["features"], feature


def test_a_checkout_on_a_stripe_price_still_charging_24_99_is_refused(monkeypatch):
    import main
    monkeypatch.setattr(main, "_VERIFIED_STRIPE_PRICES", {})
    monkeypatch.setattr(main, "stripe_api_get", lambda path: {"id": OLD, "unit_amount": 2499, "currency": "usd",
                                                              "active": True, "recurring": {"interval": "month"}})
    with pytest.raises(HTTPException) as refused:
        main.require_stripe_price_matches_catalog(OLD, billing.PLANS["hybrid"]["monthly_cents_per_camera"], "month")
    assert refused.value.status_code == 503 and "PRICE_MISMATCH" in str(refused.value.detail)
    monkeypatch.setattr(main, "stripe_api_get", lambda path: {"id": NEW, "unit_amount": 1999, "currency": "usd",
                                                              "active": True, "recurring": {"interval": "month"}})
    main.require_stripe_price_matches_catalog(NEW, 1999, "month")  # the new $19.99 Price passes


def test_a_subscription_on_the_previous_hybrid_price_is_still_recognized(prices):
    previous = billing.resolve_price(OLD)
    assert previous["plan_key"] == "hybrid" and previous["monthly_cents_per_camera"] == 2499 and previous["previous_price"]
    assert previous["product_class"] == "base_v2_commissionable"  # same 20% commission as the current Price
    current = billing.resolve_price(NEW)
    assert current["monthly_cents_per_camera"] == 1999 and not current.get("previous_price")
    sub = {"items": {"data": [{"id": "si_1", "quantity": 4, "price": {"id": OLD}}]}}
    assert billing._stripe_item(sub)["plan"]["plan_key"] == "hybrid"  # renewals and changes keep working


def test_a_previous_price_is_never_offered_for_a_new_purchase(prices):
    assert billing.price_id_for("hybrid") == NEW
    assert OLD not in {billing.price_id_for(key) for key in billing.PLANS}


def test_the_same_price_id_as_current_and_previous_is_refused(monkeypatch):
    monkeypatch.setenv(billing.PRICE_ENV["hybrid"], NEW)
    monkeypatch.setenv(billing.PREVIOUS_PRICE_ENV["hybrid"], f"{NEW}:1999")
    assert billing.resolve_price(NEW) is None  # ambiguous configuration fails closed


def test_my_subscription_shows_what_each_subscriber_is_really_billed(prices):
    previous = billing.entitlement_payload({**BASE, "stripe_price_id": OLD})
    assert (previous["monthly_per_camera_amount"], previous["monthly_base_total"], previous["previous_price"]) == (24.99, 99.96, True)
    current = billing.entitlement_payload({**BASE, "stripe_price_id": NEW})
    assert (current["monthly_per_camera_amount"], current["monthly_base_total"], current["previous_price"]) == (19.99, 79.96, False)


def test_each_previous_price_carries_its_own_amount(monkeypatch):
    """Several earlier Prices with different amounts: each subscriber sees exactly what their own Price bills."""
    monkeypatch.setenv(billing.PRICE_ENV["hybrid"], NEW)
    monkeypatch.setenv(billing.PREVIOUS_PRICE_ENV["hybrid"], f"{OLD}:2499, price_hybrid_2999_older:2999")
    assert billing.billed_cents_per_camera({**BASE, "stripe_price_id": OLD}) == 2499
    assert billing.billed_cents_per_camera({**BASE, "stripe_price_id": "price_hybrid_2999_older"}) == 2999
    assert billing.billed_cents_per_camera({**BASE, "stripe_price_id": NEW}) == 1999


def test_previous_entries_never_swap_meaning(monkeypatch):
    """Even if the env values were swapped (NEW listed as previous), a NEW subscriber is shown $19.99 -- the amount
    recorded with that ID -- never the old $24.99 (Codex 2026-10-18: rollback must not swap the two meanings)."""
    monkeypatch.setenv(billing.PRICE_ENV["hybrid"], OLD)
    monkeypatch.setenv(billing.PREVIOUS_PRICE_ENV["hybrid"], f"{NEW}:1999")
    assert billing.billed_cents_per_camera({**BASE, "stripe_price_id": NEW}) == 1999
    assert billing.resolve_price(NEW)["monthly_cents_per_camera"] == 1999


@pytest.mark.parametrize("entry", ["price_hybrid_2499_previous", "price_x:", "price_x:abc", "price_x:0", "notaprice:2499",
                                   "price_x:99999999"])
def test_a_previous_entry_without_a_valid_amount_is_ignored_never_guessed(monkeypatch, entry):
    monkeypatch.setenv(billing.PRICE_ENV["hybrid"], NEW)
    monkeypatch.setenv(billing.PREVIOUS_PRICE_ENV["hybrid"], entry)
    assert billing.previous_prices("hybrid") == {}
    assert billing.resolve_price(entry.split(":")[0]) is None


def test_my_subscription_comparison_shows_the_three_prices():
    import subscription_offers
    table = subscription_offers.plan_comparison_html()
    for price in ("$9.99/camera/mo", "$14.99/camera/mo", "$19.99/camera/mo"):  # the live build's (9bf3c20) format
        assert price in table, price
    assert "$24.99" not in table
