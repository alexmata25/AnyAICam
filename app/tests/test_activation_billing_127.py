"""Owner billing rule (2026-10-22, "COMPLETE OWNER BILLING DIRECTIVE"): software (own PC) and mini-PC (appliance)
customers start monthly subscription billing ONLY at successful service activation.

1 purchase charges only the one-time licence / hardware;  2 the plan, camera count and approved monthly Price are
saved at purchase;  3 nothing monthly is charged at purchase, download, install, shipping or delivery;  4 the first
monthly charge happens at activation;  5 activation checks the purchase is still valid (not refunded / disputed);
6 retries never create a second subscription or charge;  7 existing plans and grandfathered prices are preserved;
8 'Pending Activation' before, active only once Stripe confirms the first payment;  9 a failed activation payment keeps
the licence / hardware but grants no plan;  10 publishing an installer starts nothing.
Real routes and webhook steps with the Stripe doubles of test_build_order_billing / test_hybrid_build_system_flow.
"""
import sqlite3

import pytest

from database_backend import override_target
from test_build_order_billing import _Refused, stripe_subscriptions  # noqa: F401
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hybrid_build_system_flow import (PRICES, _activate_appliance, _checkout, _checkouts, _customer_id,  # noqa: F401
                                           _new_customer, _paid_checkout_event, _record_build_order,
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

OWN_PC = "plan=ai_local&cameras=5&vms_licence=5"


def _plan_row(db_path, customer_id):
    return _one(db_path, "SELECT * FROM deferred_storage_plans WHERE customer_id=?", (customer_id,))


def _licence(db_path, customer_id):
    return _one(db_path, "SELECT * FROM customer_entitlements WHERE customer_id=? AND product='vms_license'", (customer_id,))


def _own_pc_order(client, mail, db_path, captured, email, session_id=None):
    _new_customer(client, mail, email, OWN_PC)
    customer_id = _customer_id(db_path, email)
    assert _checkout(client).status_code == 200
    event = _paid_checkout_event(captured, customer_id, session_id or f"cs_test_{customer_id[:12]}")
    result = _record_build_order(db_path, event)
    return customer_id, event, result


def _call(db_path, fn, *args, **kwargs):
    with override_target(sqlite_path=str(db_path)):
        return fn(*args, **kwargs)


def _reverse(db_path, intent, reason):
    import billing_status
    with override_target(sqlite_path=str(db_path)):
        with sqlite3.connect(db_path) as conn:
            conn.execute(billing_status.REVERSALS_DDL)
        billing_status._record_reversal(f"ch_{intent}", intent, "", reason)
        for licence in billing_status.rows("SELECT id FROM customer_entitlements WHERE stripe_payment_intent_id=? AND status='active'",
                                           (intent,)):
            billing_status._set_plan(licence["id"], "suspended", reason)


# ------------------------------------------------------------------ 1-3: purchase charges only the licence

def test_own_pc_checkout_charges_only_the_licence_and_saves_the_card(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "rule1@example.test", OWN_PC)
    page = client.get("/order-summary").text
    assert "Only your one-time VMS software license is charged today." in page
    assert 'id="order-today"' in page and "$24.99" in page.split('id="order-today"', 1)[1][:200]  # due today = licence only
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    assert fields["mode"] == "payment"
    assert fields["line_items[0][price]"].startswith("price_") and "line_items[1][price]" not in fields  # licence only
    assert fields["payment_intent_data[setup_future_usage]"] == "off_session"
    assert fields["metadata[anyaicam_plan_price_id]"] == PRICES["ai_local"]
    assert fields["metadata[anyaicam_plan_monthly_cents]"] == "1499"
    assert fields.get("customer")  # a Stripe customer holds the saved card
    assert not any(key.startswith("subscription_data[") for key in fields)


def test_the_paid_order_grants_the_licence_and_saves_the_plan_pending_activation(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id, event, result = _own_pc_order(client, mail, db_path, captured, "rule2@example.test")
    assert result["status"] == "vms_license_granted" and result["deferred_plan"] == "recorded"
    licence = _licence(db_path, customer_id)
    assert licence["status"] == "active" and licence["stripe_payment_intent_id"] == event["data"]["object"]["payment_intent"]
    plan = _plan_row(db_path, customer_id)
    assert (plan["plan_key"], plan["camera_quantity"], plan["stripe_price_id"], plan["monthly_cents_per_camera"],
            plan["order_kind"], plan["state"]) == ("ai_local", 5, PRICES["ai_local"], 1499, "own_pc", "awaiting_activation")
    assert stripe_subscriptions["created"] == []  # 3: nothing monthly charged at purchase
    assert result["plan_start"] == "ignored"
    import per_camera_billing as billing
    assert not _call(db_path, billing.has_camera_plan, customer_id)
    portal_page = client.get("/subscription-portal").text
    assert "Pending Activation" in portal_page and "$14.99 per camera/month ($74.95/month)" in portal_page
    assert "Pending Activation" in client.get("/order-complete").text


def test_a_redelivered_order_event_changes_nothing(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, "rule6a@example.test")
    before = _plan_row(db_path, customer_id)
    again = _record_build_order(db_path, event)
    assert again["deferred_plan"] == "unchanged" and _plan_row(db_path, customer_id) == before
    assert stripe_subscriptions["created"] == []


def test_the_confirmation_email_says_the_plan_starts_at_activation(shop, db_path):
    import order_funnel
    kind, subject, text, _ = order_funnel._order_email("Bea", {"own_pc": 1}, None, "AnyAiCam AI Local subscription for 5 licensed cameras",
                                                       plan_pending=True)
    assert subject == "Your AnyAiCam order is confirmed" and "is active" not in text
    assert "starts, with its first monthly charge, when your AnyAiCam system is activated" in text


# ------------------------------------------------------------------ 4, 6, 10: first charge at activation, exactly once

def test_activation_starts_the_plan_once_at_the_saved_price(shop, db_path, stripe_subscriptions, monkeypatch):
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id, _, _ = _own_pc_order(client, mail, db_path, captured, "rule4@example.test")
    _call(db_path, build_orders.on_appliance_activated, customer_id)  # 10: no activated PC yet -> nothing
    assert stripe_subscriptions["created"] == []
    _activate_appliance(db_path, customer_id)
    first = _call(db_path, build_orders.on_appliance_activated, customer_id)
    assert first["status"] == "started"
    created = stripe_subscriptions["created"]
    assert len(created) == 1 and created[0]["items[0][price]"] == PRICES["ai_local"] and created[0]["items[0][quantity]"] == "5"
    assert created[0]["payment_behavior"] == "error_if_incomplete" and created[0]["off_session"] == "true"
    for _ in range(3):  # 6: activation retried, worker, duplicate claim
        _call(db_path, build_orders.on_appliance_activated, customer_id)
        _call(db_path, build_orders.retry_storage_starts)
    assert len(stripe_subscriptions["created"]) == 1
    assert _plan_row(db_path, customer_id)["state"] == "started"


def test_grandfathered_saved_price_is_charged_after_a_catalog_change(shop, db_path, stripe_subscriptions, monkeypatch):
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id, _, _ = _own_pc_order(client, mail, db_path, captured, "rule7@example.test")
    # The plan's Price moves from A (saved) to B; A stays recognised as the plan's previous Price at its amount.
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_AI_LOCAL", "price_srv_ai_new")
    monkeypatch.setitem(PRICES, "ai_local", "price_srv_ai_new")
    import per_camera_billing as billing
    monkeypatch.setitem(billing.PREVIOUS_PRICE_ENV, "ai_local", "ANYAICAM_STRIPE_PRICE_AI_LOCAL_PREVIOUS")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_AI_LOCAL_PREVIOUS", "price_srv_ai:1499")
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "started"
    assert stripe_subscriptions["created"][-1]["items[0][price]"] == "price_srv_ai"  # the price saved at purchase


def test_a_saved_price_that_is_no_longer_configured_is_held_never_repriced(shop, db_path, stripe_subscriptions, monkeypatch):
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id, _, _ = _own_pc_order(client, mail, db_path, captured, "rule7b@example.test")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_AI_LOCAL", "price_srv_ai_other")  # A neither current nor previous
    _activate_appliance(db_path, customer_id)
    result = _call(db_path, build_orders.on_appliance_activated, customer_id)
    assert result["status"] == "owner_review" and stripe_subscriptions["created"] == []
    assert _plan_row(db_path, customer_id)["state"] == "owner_review"


# ------------------------------------------------------------------ 5: refunded / disputed purchases never start

@pytest.mark.parametrize("reason", ["refunded", "disputed"])
def test_a_reversed_licence_never_starts_the_plan(shop, db_path, stripe_subscriptions, reason):
    import build_orders
    import per_camera_billing as billing
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, f"rule5-{reason}@example.test")
    _reverse(db_path, event["data"]["object"]["payment_intent"], reason)
    _activate_appliance(db_path, customer_id)
    result = _call(db_path, build_orders.on_appliance_activated, customer_id)
    assert result["status"] == "ignored" and stripe_subscriptions["created"] == []
    assert not _call(db_path, billing.order_paid, customer_id)
    assert _call(db_path, build_orders.live_storage_plan, customer_id) is None


def test_after_a_refund_a_new_order_replaces_the_never_started_plan(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, "rule5r7@example.test")
    _reverse(db_path, event["data"]["object"]["payment_intent"], "refunded")
    page = client.get("/order-summary").text
    assert 'id="order-checkout"' in page  # R7: the customer can order again
    assert _checkout(client).status_code == 200
    second = _paid_checkout_event(captured, customer_id, "cs_test_second_order")
    result = _record_build_order(db_path, second)
    assert result["status"] == "vms_license_granted" and result["deferred_plan"] == "replaced"
    plan = _plan_row(db_path, customer_id)
    assert plan["checkout_session_id"] == "cs_test_second_order" and plan["state"] == "awaiting_activation"
    assert _one(db_path, "SELECT COUNT(*) AS n FROM audit_logs WHERE action='deferred_plan.replaced_after_reversal' AND entity_id=?",
                (customer_id,))["n"] == 1


# ------------------------------------------------------------------ 8, 9: payment failure at activation

def test_a_declined_first_payment_keeps_the_licence_and_grants_no_plan(shop, db_path, stripe_subscriptions):
    import build_orders
    import per_camera_billing as billing
    client, captured, mail, _, _ = shop
    customer_id, _, _ = _own_pc_order(client, mail, db_path, captured, "rule9@example.test")
    stripe_subscriptions["fail"].append(_Refused("card_declined"))
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "payment_failed"
    assert _licence(db_path, customer_id)["status"] == "active"
    assert not _call(db_path, billing.has_camera_plan, customer_id)
    assert "payment not completed" in client.get("/subscription-portal").text


def test_an_already_activated_pc_starts_the_plan_with_the_order(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "rule4b@example.test", OWN_PC)
    customer_id = _customer_id(db_path, "rule4b@example.test")
    _activate_appliance(db_path, customer_id)  # installed and linked before buying (Activate Your System)
    assert _checkout(client).status_code == 200
    result = _record_build_order(db_path, _paid_checkout_event(captured, customer_id, "cs_test_activated_first"))
    assert result["plan_start"] == "started" and len(stripe_subscriptions["created"]) == 1


# ------------------------------------------------------------------ My subscription before activation

def test_my_subscription_sells_no_plan_before_service_activation(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id, _, _ = _own_pc_order(client, mail, db_path, captured, "gate@example.test")
    response = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 5})
    assert response.status_code == 409  # a saved plan exists: it starts at activation
    _one(db_path, "SELECT 1", ())
    with sqlite3.connect(db_path) as conn:  # no saved plan, licence only (e.g. a superseded row)
        conn.execute("UPDATE deferred_storage_plans SET state='superseded' WHERE customer_id=?", (customer_id,))
    response = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 5})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "activate_service_first"


def test_existing_running_plans_are_untouched(shop, db_path):
    import build_orders
    from test_hybrid_build_system_flow import _pay
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "running@example.test", OWN_PC)
    customer_id = _customer_id(db_path, "running@example.test")
    _pay(db_path, customer_id, "hybrid", 8, "sub_running")
    assert _call(db_path, build_orders.record_deferred_plan, customer_id, order_kind="own_pc", plan_key="ai_local", cameras=5,
                 stripe_customer_id="cus_x", payment_method_id=None, checkout_session_id="cs_x") == "skipped"
    assert _plan_row(db_path, customer_id) is None


# ------------------------------------------------------------------ appliance (mini PC) orders

def test_hardware_checkout_saves_the_card_and_locks_the_plan_price(shop, db_path, monkeypatch):
    import hardware_checkout
    client, captured, mail, _, _ = shop
    with override_target(sqlite_path=str(db_path)):
        result = hardware_checkout.create_checkout({"appliance": "AIC-APPLIANCE-RYZEN-STARTER", "plan": "hybrid", "cameras": 8},
                                                   email="mini@example.test", identity=None)
    fields = dict(captured[-1])
    assert fields["mode"] == "payment" and fields["payment_intent_data[setup_future_usage]"] == "off_session"
    record = _one(db_path, "SELECT * FROM hardware_checkouts WHERE stripe_checkout_session_id=?", (result["session_id"],))
    assert (record["intended_plan"], record["intended_cameras"], record["intended_price_id"], record["intended_monthly_cents"]) == \
        ("hybrid", 8, PRICES["hybrid"], 1999)


# ------------------------------------------------------------------ Red's review list for the licence-only order shape
# Refunds and disputes go through billing_status.sync_from_stripe_event (the real webhook step). For a one-time payment
# Stripe answers that it paid no invoice (invoice_payments: empty list).

@pytest.fixture()
def one_time_payments(monkeypatch):
    import stripe_state
    original = stripe_state.stripe_get

    def get(path, what):
        if path.startswith("/v1/invoice_payments?"):
            return {"data": []}
        return original(path, what)
    monkeypatch.setattr(stripe_state, "stripe_get", get)


def _billing_event(db_path, event):
    import billing_status
    with override_target(sqlite_path=str(db_path)):
        return billing_status.sync_from_stripe_event(event)


def _refund(intent, event_id="evt_refund"):
    return {"id": event_id, "type": "charge.refunded", "data": {"object": {
        "object": "charge", "id": f"ch_{intent}", "payment_intent": intent, "amount": 2499, "amount_refunded": 2499,
        "refunded": True}}}


def _dispute(intent, kind, status="needs_response", event_id=None):
    return {"id": event_id or f"evt_dispute_{kind}_{status}", "type": f"charge.dispute.{kind}", "data": {"object": {
        "object": "dispute", "id": f"dp_{intent}", "charge": f"ch_{intent}", "payment_intent": intent, "status": status}}}


def test_a_refund_before_the_order_is_recorded_grants_a_suspended_licence_and_no_plan(shop, db_path, stripe_subscriptions,
                                                                                     one_time_payments):
    import build_orders
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "reda@example.test", OWN_PC)
    customer_id = _customer_id(db_path, "reda@example.test")
    assert _checkout(client).status_code == 200
    event = _paid_checkout_event(captured, customer_id, "cs_test_reda")
    intent = event["data"]["object"]["payment_intent"]
    _billing_event(db_path, _refund(intent))  # the refund arrives first
    result = _record_build_order(db_path, event)
    assert result["status"] == "vms_license_not_granted" and "deferred_plan" not in result
    assert _licence(db_path, customer_id)["status"] == "suspended" and _plan_row(db_path, customer_id) is None
    again = _record_build_order(db_path, event)  # a replay changes nothing
    assert again["status"] == "vms_license_not_granted" and _plan_row(db_path, customer_id) is None
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "ignored"
    assert stripe_subscriptions["created"] == []


def test_a_dispute_before_activation_blocks_the_start_until_won(shop, db_path, stripe_subscriptions, one_time_payments):
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, "redb@example.test")
    intent = event["data"]["object"]["payment_intent"]
    _billing_event(db_path, _dispute(intent, "created"))
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "ignored"
    assert stripe_subscriptions["created"] == []
    _billing_event(db_path, _dispute(intent, "closed", "won"))
    assert _licence(db_path, customer_id)["status"] == "active"
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "started"
    assert len(stripe_subscriptions["created"]) == 1


def test_a_lost_dispute_never_starts_the_plan(shop, db_path, stripe_subscriptions, one_time_payments):
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, "redb2@example.test")
    intent = event["data"]["object"]["payment_intent"]
    _billing_event(db_path, _dispute(intent, "created"))
    _billing_event(db_path, _dispute(intent, "closed", "lost"))
    _activate_appliance(db_path, customer_id)
    for _ in range(2):
        assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "ignored"
        _call(db_path, build_orders.retry_storage_starts)
    assert stripe_subscriptions["created"] == [] and _licence(db_path, customer_id)["status"] == "suspended"


def test_a_refund_after_the_plan_started_suspends_the_licence_and_its_capacity(shop, db_path, stripe_subscriptions,
                                                                            one_time_payments):
    import build_orders
    import customer_entitlements as ce
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, "redc@example.test")
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "started"
    result = _billing_event(db_path, _refund(event["data"]["object"]["payment_intent"]))
    assert result["status"] == "suspended"
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"]) == ("suspended", "refunded")
    assert _call(db_path, ce.usable_camera_capacity, customer_id) == 0  # C5: no licence, no cameras
    assert len(stripe_subscriptions["created"]) == 1  # nothing re-created


def test_c5_gates_refuse_purchases_for_a_reversed_new_shape_licence(shop, db_path, stripe_subscriptions, one_time_payments):
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, "redd@example.test")
    _billing_event(db_path, _refund(event["data"]["object"]["payment_intent"]))
    addon = client.post("/api/customer/analytics/checkout", json={"addon_key": "advanced_analytics", "quantity": 1})
    # Refused either way: a reversed licence with no live order is no activated account (activate_first, C5), and
    # an activated one is refused as reversed (licence_reversed).
    assert addon.status_code == 409 and addon.json()["detail"]["code"] in ("activate_first", "licence_reversed")
    plan = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 5})
    assert plan.status_code == 409
    licence = client.post("/api/customer/vms-license/checkout", json={"capacity": 8})
    assert licence.status_code == 409
    assert stripe_subscriptions["created"] == []


def test_r7_old_order_replays_never_touch_the_new_order(shop, db_path, stripe_subscriptions, one_time_payments):
    import copy
    client, captured, mail, _, _ = shop
    customer_id, first, _ = _own_pc_order(client, mail, db_path, captured, "rede@example.test", session_id="cs_test_rede_1")
    _billing_event(db_path, _refund(first["data"]["object"]["payment_intent"], "evt_rede_refund"))
    assert _checkout(client).status_code == 200
    second = _paid_checkout_event(captured, customer_id, "cs_test_rede_2")
    assert _record_build_order(db_path, second)["deferred_plan"] == "replaced"
    new_plan, new_licence = _plan_row(db_path, customer_id), _licence(db_path, customer_id)
    assert new_licence["status"] == "active" and new_licence["stripe_checkout_session_id"] == "cs_test_rede_2"
    resent = copy.deepcopy(first)
    resent["id"] = "evt_rede_first_again"
    for event in (first, resent):  # Stripe's redelivery (same id) and a resend (new id)
        _record_build_order(db_path, event)
    _billing_event(db_path, _refund(first["data"]["object"]["payment_intent"], "evt_rede_refund"))
    _billing_event(db_path, _refund(first["data"]["object"]["payment_intent"], "evt_rede_refund_new"))
    assert _plan_row(db_path, customer_id) == new_plan and _licence(db_path, customer_id)["status"] == "active"
    assert _licence(db_path, customer_id)["stripe_checkout_session_id"] == "cs_test_rede_2"


# ------------------------------------------------------------------ Red review of 0ba37427 (H1, H2, M1, M2, L1, L2)

APPLIANCE_SELECTION = {"appliance": "AIC-APPLIANCE-RYZEN-STARTER", "plan": "hybrid", "cameras": 8}


def _hardware_order(db_path, captured, customer_id, email, monkeypatch, *, intent="pi_hw_review", pre_change=False):
    """A signed-in buyer's appliance checkout and its verified webhook (hardware_checkout.py)."""
    import build_orders
    import hardware_checkout
    monkeypatch.setattr(build_orders, "_line_amounts", lambda session: {})
    with override_target(sqlite_path=str(db_path)):
        opened = hardware_checkout.create_checkout(APPLIANCE_SELECTION, email=email, identity={"customer_id": customer_id})
    if pre_change:  # a checkout opened before 2026-10-22: no price lock, no saved card
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE hardware_checkouts SET intended_price_id=NULL,intended_monthly_cents=NULL "
                         "WHERE stripe_checkout_session_id=?", (opened["session_id"],))
    fields = dict(captured[-1])
    metadata = {key[len("metadata["):-1]: value for key, value in fields.items() if key.startswith("metadata[")}
    event = {"id": f"evt_{opened['session_id']}", "type": "checkout.session.completed", "livemode": False,
             "data": {"object": {"id": opened["session_id"], "mode": "payment", "payment_status": "paid", "status": "complete",
                                 "customer": "cus_hw_review", "payment_intent": intent, "customer_email": email,
                                 "customer_details": {"email": email}, "metadata": metadata}}}
    with override_target(sqlite_path=str(db_path)):
        return hardware_checkout.sync_from_stripe_event(event)


def _signed_in_customer(client, mail, db_path, email):
    _new_customer(client, mail, email, OWN_PC)
    return _customer_id(db_path, email)


def test_h1_hardware_refunded_before_its_order_is_recorded_saves_no_plan(shop, db_path, stripe_subscriptions, monkeypatch):
    import billing_status
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "h1a@example.test")
    with override_target(sqlite_path=str(db_path)):
        with sqlite3.connect(db_path) as conn:
            conn.execute(billing_status.REVERSALS_DDL)
        billing_status._record_reversal("ch_h1a", "pi_h1a", "", "refunded")  # the refund arrived first
    _hardware_order(db_path, captured, customer_id, "h1a@example.test", monkeypatch, intent="pi_h1a")
    assert _plan_row(db_path, customer_id) is None
    assert _one(db_path, "SELECT status FROM hardware_orders WHERE stripe_payment_intent_id='pi_h1a' AND sku LIKE 'AIC-APPLIANCE-%'",
                ())["status"] == "refunded"
    _activate_appliance(db_path, customer_id)
    _call(db_path, build_orders.on_appliance_activated, customer_id)
    assert stripe_subscriptions["created"] == []


def test_h1_a_reversal_recorded_beside_a_paid_hardware_order_blocks_the_start(shop, db_path, stripe_subscriptions, monkeypatch):
    import billing_status
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "h1b@example.test")
    _hardware_order(db_path, captured, customer_id, "h1b@example.test", monkeypatch, intent="pi_h1b")
    assert _plan_row(db_path, customer_id)["state"] == "awaiting_activation"
    with override_target(sqlite_path=str(db_path)):  # recorded without touching the order row (the race Red found)
        billing_status._record_reversal("ch_h1b", "pi_h1b", "", "disputed")
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "ignored"
    assert stripe_subscriptions["created"] == []


def test_h1_control_a_paid_hardware_order_starts_its_saved_plan_at_activation(shop, db_path, stripe_subscriptions, monkeypatch):
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "h1c@example.test")
    _hardware_order(db_path, captured, customer_id, "h1c@example.test", monkeypatch, intent="pi_h1c")
    plan = _plan_row(db_path, customer_id)
    assert (plan["plan_key"], plan["camera_quantity"], plan["stripe_price_id"], plan["order_kind"]) == ("hybrid", 8, PRICES["hybrid"], "appliance")
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "started"
    assert stripe_subscriptions["created"][-1]["items[0][price]"] == PRICES["hybrid"]


def test_h2_a_hardware_checkout_opened_before_the_change_never_gets_an_automatic_plan(shop, db_path, stripe_subscriptions,
                                                                                     monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "h2@example.test")
    _hardware_order(db_path, captured, customer_id, "h2@example.test", monkeypatch, intent="pi_h2", pre_change=True)
    assert _plan_row(db_path, customer_id) is None  # the customer starts the plan themselves, as they were told


def test_m1_a_manual_retry_uses_the_card_the_customer_chose_since(shop, db_path, stripe_subscriptions, monkeypatch):
    import build_orders
    import main
    client, captured, mail, _, _ = shop
    customer_id, _, _ = _own_pc_order(client, mail, db_path, captured, "m1@example.test")
    stripe_subscriptions["fail"].append(_Refused("card_declined"))
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "payment_failed"
    get = main.stripe_api_get
    monkeypatch.setattr(main, "stripe_api_get", lambda path: {"invoice_settings": {"default_payment_method": "pm_updated_card"}}
                        if path.startswith("/v1/customers/") else get(path))
    assert _call(db_path, build_orders.start_storage_plan, customer_id, manual=True)["status"] == "started"
    assert stripe_subscriptions["created"][-1]["default_payment_method"] == "pm_updated_card"


def test_m2_my_subscription_accepts_an_account_activated_before_the_claim_marker(shop, db_path, stripe_subscriptions, monkeypatch):
    import appliance_readiness
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "m2@example.test")
    monkeypatch.setattr(build_orders, "_appliance_activated", lambda cid: False)  # no 2026-10-11 activation marker
    monkeypatch.setattr(appliance_readiness, "customer_has_activated_appliance", lambda cid: cid == customer_id)
    import per_camera_billing as billing
    monkeypatch.setattr(billing, "account_activated", lambda cid: True)
    response = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 5})
    assert response.status_code == 200, response.text


def test_l2_a_saved_price_refused_by_the_catalog_check_is_held(shop, db_path, stripe_subscriptions, monkeypatch):
    import build_orders
    import main
    from fastapi import HTTPException
    client, captured, mail, _, _ = shop
    customer_id, _, _ = _own_pc_order(client, mail, db_path, captured, "l2@example.test")

    def archived(*args):
        raise HTTPException(status_code=503, detail="PRICE_INACTIVE")
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", archived)
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "owner_review"
    assert _call(db_path, build_orders.retry_storage_starts) == 0 and stripe_subscriptions["created"] == []


def test_l1_a_reversal_during_the_start_is_flagged_for_owner_review(shop, db_path, stripe_subscriptions, monkeypatch):
    import billing_status
    import build_orders
    client, captured, mail, _, _ = shop
    customer_id, event, _ = _own_pc_order(client, mail, db_path, captured, "l1@example.test")
    intent = event["data"]["object"]["payment_intent"]
    post = stripe_subscriptions  # noqa: F841
    import main
    original = main.stripe_api_post

    def post_then_refund(path, fields, idempotency_key=None):
        answer = original(path, fields, idempotency_key=idempotency_key)
        if path == "/v1/subscriptions":  # the refund lands while Stripe answers
            billing_status._record_reversal(f"ch_{intent}", intent, "", "refunded")
        return answer
    monkeypatch.setattr(main, "stripe_api_post", post_then_refund)
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "started"
    assert "owner review" in (_plan_row(db_path, customer_id)["hold_reason"] or "")
    assert _one(db_path, "SELECT COUNT(*) AS n FROM audit_logs WHERE action='deferred_plan.started_after_reversal' AND entity_id=?",
                (customer_id,))["n"] == 1


# ------------------------------------------------------------------ owner 2026-10-22: Trial Active apart from Paid Active

HYBRID_OWN_PC = "plan=hybrid&cameras=5&vms_licence=5"
ADMIN = {"email": "admin@anyaicam.test", "role": "global_admin"}


def _hybrid_order_with_trial(client, mail, db_path, captured, email):
    import storage_trial
    _new_customer(client, mail, email, HYBRID_OWN_PC)
    customer_id = _customer_id(db_path, email)
    _call(db_path, storage_trial.grant, customer_id, admin=ADMIN, reason="evaluating cloud storage")
    assert _checkout(client).status_code == 200
    _record_build_order(db_path, _paid_checkout_event(captured, customer_id, f"cs_test_{customer_id[:12]}"))
    return customer_id


def _label(db_path, customer_id):
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        return billing.plan_status_label(customer_id, billing.entitlement_for_customer(customer_id))


def _paid_invoice(db_path, customer_id, subscription_id, refunded=False):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO subscription_payments(id,customer_id,stripe_customer_id,stripe_subscription_id,stripe_price_id,"
                     "product_class,amount_paid_cents,currency,period_start,paid_at,status,refunded_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"in_{subscription_id}", customer_id, "cus_x", subscription_id, PRICES["hybrid"], "base_v2_commissionable",
                      9995, "usd", "1790000000", "2026-10-22T00:00:00", "paid", "2026-10-23T00:00:00" if refunded else None))


def test_a_trial_started_at_activation_is_trial_active_and_charges_nothing(shop, db_path, stripe_subscriptions):
    import build_orders
    from test_hybrid_build_system_flow import _pay
    client, captured, mail, _, _ = shop
    customer_id = _hybrid_order_with_trial(client, mail, db_path, captured, "trial-active@example.test")
    _activate_appliance(db_path, customer_id)
    assert _call(db_path, build_orders.on_appliance_activated, customer_id)["status"] == "started"
    created = stripe_subscriptions["created"][-1]
    # No recurring charge because the trial started: no card attached, ends at the trial end unless the owner continues.
    assert "trial_end" in created and created["cancel_at_period_end"] == "true" and "default_payment_method" not in created
    _pay(db_path, customer_id, "hybrid", 5, "sub_storage_1", status="trialing")
    assert _label(db_path, customer_id) == "Trial Active"
    portal_page = client.get("/subscription-portal").text
    status_row = portal_page[portal_page.index('id="v2-plan-status"'):][:200]
    assert "Trial Active" in status_row and "Paid Active" not in portal_page
    panel = client.get("/order-complete").text
    assert "Trial Active · free trial until" in panel and "not charged unless you choose to continue" in panel
    assert "Paid Active" not in panel


def test_trial_active_needs_verified_activation(shop, db_path, stripe_subscriptions):
    import storage_trial
    from test_hybrid_build_system_flow import _pay
    client, captured, mail, _, _ = shop
    customer_id = _hybrid_order_with_trial(client, mail, db_path, captured, "trial-unactivated@example.test")
    trial = _call(db_path, storage_trial.latest, customer_id)
    _call(db_path, storage_trial.link_subscription, trial["id"], customer_id, "sub_trial_early")
    _pay(db_path, customer_id, "hybrid", 5, "sub_trial_early", status="trialing")
    assert _label(db_path, customer_id) == "Trial · Pending Activation"  # no activated system on the account
    _activate_appliance(db_path, customer_id)
    assert _label(db_path, customer_id) == "Trial Active"


def test_trial_active_needs_a_valid_trial_entitlement(shop, db_path, stripe_subscriptions, monkeypatch):
    import storage_trial
    from test_hybrid_build_system_flow import _pay
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "trial-none@example.test", HYBRID_OWN_PC)
    customer_id = _customer_id(db_path, "trial-none@example.test")
    _activate_appliance(db_path, customer_id)
    _pay(db_path, customer_id, "hybrid", 5, "sub_untracked_trial", status="trialing")
    assert _label(db_path, customer_id) == "Trial · under review by AnyAiCam"  # Stripe trialing, no AnyAiCam trial bound
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO storage_trials(id,customer_id,status,starts_at,expires_at,granted_by,granted_at,reason,"
                     "stripe_subscription_id,updated_at) VALUES('t-old',?,?,?,?,?,?,?,?,?)",
                     (customer_id, "active", "2026-08-01T00:00:00+00:00", "2026-08-31T00:00:00+00:00", "admin",
                      "2026-08-01T00:00:00+00:00", "test", "sub_untracked_trial", "2026-08-01T00:00:00+00:00"))
    assert _label(db_path, customer_id) == "Trial ended"  # its 30 days are over
    assert _call(db_path, storage_trial.active_trial, customer_id) is None


def test_paid_active_needs_a_confirmed_payment(shop, db_path, stripe_subscriptions):
    from test_hybrid_build_system_flow import _pay
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "paid-active@example.test", HYBRID_OWN_PC)
    customer_id = _customer_id(db_path, "paid-active@example.test")
    _activate_appliance(db_path, customer_id)
    _pay(db_path, customer_id, "hybrid", 5, "sub_paid", status="active")
    assert _label(db_path, customer_id) == "Active · awaiting payment confirmation"  # Stripe 'active' alone is not enough
    _paid_invoice(db_path, customer_id, "sub_paid")
    assert _label(db_path, customer_id) == "Paid Active"
    assert "Paid Active · renews on" in client.get("/order-complete").text
    assert "Paid Active" in client.get("/subscription-portal").text
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE subscription_payments SET refunded_at='2026-10-23T00:00:00' WHERE stripe_subscription_id='sub_paid'")
    assert _label(db_path, customer_id) != "Paid Active"


def test_a_converted_trial_is_paid_active_only_after_its_first_payment(shop, db_path, stripe_subscriptions):
    import storage_trial
    from test_hybrid_build_system_flow import _pay
    client, captured, mail, _, _ = shop
    customer_id = _hybrid_order_with_trial(client, mail, db_path, captured, "trial-converted@example.test")
    _activate_appliance(db_path, customer_id)
    trial = _call(db_path, storage_trial.latest, customer_id)
    _call(db_path, storage_trial.link_subscription, trial["id"], customer_id, "sub_converting")
    _pay(db_path, customer_id, "hybrid", 5, "sub_converting", status="trialing")
    assert _label(db_path, customer_id) == "Trial Active"
    _pay(db_path, customer_id, "hybrid", 5, "sub_converting", status="active")  # Stripe ends the trial, invoice not yet paid
    assert _label(db_path, customer_id) == "Active · awaiting payment confirmation"
    _paid_invoice(db_path, customer_id, "sub_converting")
    assert _label(db_path, customer_id) == "Paid Active"


# ------------------------------------------------------------------ owner 2026-10-22 verification: what a purchase charges

def test_a_hardware_purchase_charges_only_the_hardware(shop, db_path):
    import os
    import hardware_checkout
    import per_camera_billing as billing
    client, captured, mail, _, _ = shop
    with override_target(sqlite_path=str(db_path)):
        hardware_checkout.create_checkout({"appliance": "AIC-APPLIANCE-RYZEN-STARTER", "plan": "hybrid", "cameras": 8, "relays": 2},
                                          email="hw-only@example.test", identity=None)
    fields = dict(captured[-1])
    hardware_prices = {os.environ.get(entry["env"], "") for entry in hardware_checkout._catalog().values()} - {""}
    lines = [value for key, value in fields.items() if key.startswith("line_items[") and key.endswith("[price]")]
    assert fields["mode"] == "payment" and lines and set(lines) <= hardware_prices
    plan_prices = {billing.price_id_for(key) for key in billing.PLANS} - {None, ""}
    assert not set(lines) & plan_prices  # the monthly plan is saved, never charged at purchase
    assert not any(key.startswith("subscription_data[") for key in fields)


def test_an_own_pc_purchase_charges_only_the_approved_licence_price(shop, db_path):
    import os
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "licence-price@example.test", OWN_PC)
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    lines = [value for key, value in fields.items() if key.startswith("line_items[") and key.endswith("[price]")]
    assert lines == [os.environ["ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8"]] and fields["line_items[0][quantity]"] == "1"


def test_a_redelivered_hardware_order_records_one_order_one_plan_and_charges_nothing(shop, db_path, stripe_subscriptions,
                                                                                    monkeypatch):
    import hardware_checkout
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "hw-replay@example.test")
    first = _hardware_order(db_path, captured, customer_id, "hw-replay@example.test", monkeypatch, intent="pi_hw_replay")
    fields = dict(captured[-1])
    metadata = {key[len("metadata["):-1]: value for key, value in fields.items() if key.startswith("metadata[")}
    session_id = _one(db_path, "SELECT stripe_checkout_session_id AS s FROM hardware_checkouts WHERE customer_id=?", (customer_id,))["s"]
    replay = {"id": f"evt_{session_id}", "type": "checkout.session.completed", "livemode": False,
              "data": {"object": {"id": session_id, "mode": "payment", "payment_status": "paid", "status": "complete",
                                  "customer": "cus_hw_review", "payment_intent": "pi_hw_replay",
                                  "customer_email": "hw-replay@example.test", "customer_details": {"email": "hw-replay@example.test"},
                                  "metadata": metadata}}}
    with override_target(sqlite_path=str(db_path)):
        again = hardware_checkout.sync_from_stripe_event(replay)
    assert again["order_ids"] == first["order_ids"]
    assert _one(db_path, "SELECT COUNT(*) AS n FROM hardware_orders WHERE stripe_payment_intent_id='pi_hw_replay'", ())["n"] == \
        len(first["order_ids"])
    assert _one(db_path, "SELECT COUNT(*) AS n FROM deferred_storage_plans WHERE customer_id=?", (customer_id,))["n"] == 1
    assert stripe_subscriptions["created"] == []  # nothing monthly before activation, however often Stripe resends
