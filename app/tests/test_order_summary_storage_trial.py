"""The order summary tells the truth about a granted 30-day storage trial
(Green, 2026-10-16).

build_orders' plan-only Checkout starts the granted trial (no charge today),
but the summary used to show the first month as "Due today" and "Your monthly
subscription starts today". Display only: prices and the Checkout are unchanged.
"""
import pytest  # noqa: F401

from test_pricing_ff_commission import _make_global_admin, db_path, portal  # noqa: F401
from test_storage_trial_30day import CUSTOMER, TRIAL_KEYS, _grant, shop  # noqa: F401


def _summary(selection):
    import order_funnel
    lines, notes = order_funnel._order_lines(selection, CUSTOMER)
    return lines + notes


def test_a_plan_only_storage_order_with_a_granted_trial_shows_nothing_due_today(shop, db_path):
    import build_orders
    client, _, _ = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    html = _summary({"plan": "hybrid", "cameras": 4})
    assert '<span>Due today</span><span>$0.00</span>' in html
    assert "30-day free cloud storage trial starts today" in html and "unless you choose to continue, then at $79.96/month" in html
    assert "Your monthly subscription starts today." not in html
    # The Checkout that page leads to really carries the trial (the claim above is not a promise of its own).
    fields, _, _ = build_orders._checkout_fields(CUSTOMER, "owner@example.test", {"plan": "hybrid", "cameras": 4})
    assert "subscription_data[trial_end]" in dict(fields)


def test_without_a_trial_or_for_orders_that_never_carry_one_the_full_amount_is_shown(shop, db_path):
    html = _summary({"plan": "hybrid", "cameras": 4})  # no grant
    assert '<span>Due today</span><span>$79.96</span>' in html and "Your monthly subscription starts today." in html
    client, _, _ = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    local = _summary({"plan": "ai_local", "cameras": 4})  # a local-only plan has no storage trial
    assert '<span>Due today</span><span>$59.96</span>' in local and "free cloud storage trial" not in local
    own_pc = _summary({"plan": "hybrid", "cameras": 4, "own_pc": True})  # license order: never the trial
    assert "free cloud storage trial" not in own_pc and "Due today</span><span>$0.00" not in own_pc


def test_the_reserved_trial_is_not_promised_to_an_account_already_paying_for_a_plan(shop, db_path, monkeypatch):
    import per_camera_billing as billing
    import storage_trial
    client, _, _ = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    assert "trial is reserved" in storage_trial.customer_note_html(CUSTOMER, True)
    monkeypatch.setattr(billing, "has_camera_plan", lambda customer_id, **_: customer_id == CUSTOMER)
    assert storage_trial.customer_note_html(CUSTOMER, True) == ""
