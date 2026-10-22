"""R7 (owner-approved isolated correction, 2026-10-21; Green's RC10b journey R7): an own-PC customer whose first Build
Your System order was fully refunded (licence suspended 'refunded') and whose subscription has ended can place a NEW
order -- before, the order summary offered "Proceed to Secure Checkout" and the checkout answered 409 "There is no order
to check out" because the account's build_system_intents row stayed paid.

* the old order is kept: an append-only audit_logs copy of the order, its licence, plan and payments with every Stripe
  reference, and its payment, reversal rows untouched; the new order is a separate pending order;
* replaying ANY event of the old order (checkout, invoice, subscription, refund; same and new event ids), before or
  after the new order is paid, never restores the old licence or plan and never touches the new order's;
* the new order, once paid, has its own active licence, plan, camera capacity, download and activation;
* an account whose order still grants something (active licence or plan, a refunded plan whose subscription is still
  running, a dispute that may still be won) keeps today's behaviour: no second order.

Real routes (/order-summary, /api/v2/customer/build-order/checkout, /api/payments/stripe/webhook) and the real
build_orders / billing_status / per_camera_billing code; Stripe is the fake of test_refund_before_record_race_127.py.
"""
import copy
import json
import sqlite3

import pytest

from database_backend import override_target
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hybrid_build_system_flow import (PRICES, _checkout, _checkouts, _paid_checkout_event, _order,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)
from test_refund_before_record_race_127 import (_access, _configure, _ids, _invoice_paid, _licence, _place, _post,
                                                _refund, _reversals, _stripe_subscription, _sub_event, stripe)  # noqa: F401

pytestmark = [pytest.mark.real_activation]

NO_ORDER = "There is no order to check out. Build your system first."
SECOND_ORDER_QUERY = "plan=ai_local&cameras=10&vms_licence=10"


# ------------------------------------------------------------------ helpers

def _all(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(item) for item in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _intent(db_path, customer_id):
    return _one(db_path, "SELECT * FROM build_system_intents WHERE customer_id=?", (customer_id,))


def _licence_row(db_path, customer_id):
    return _one(db_path, "SELECT * FROM customer_entitlements WHERE customer_id=? AND product='vms_license'", (customer_id,))


def _plan_row(db_path, customer_id):
    return _one(db_path, "SELECT * FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer_id,))


def _payments(db_path, subscription_id):
    return _all(db_path, "SELECT * FROM subscription_payments WHERE stripe_subscription_id=? ORDER BY id", (subscription_id,))


def _superseded(db_path, customer_id):
    return [json.loads(item["details_json"]) for item in _all(
        db_path, "SELECT details_json FROM audit_logs WHERE action='build_order.superseded_after_refund' AND entity_id=?",
        (customer_id,))]


def _progress(db_path, customer_id):
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        return billing.build_progress(customer_id)


def _capacity(db_path, customer_id):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(db_path)):
        return ce.capacity_breakdown(customer_id)


def _subscription(stripe, ids, customer_id, stripe_customer, *, quantity, status="active", latest_invoice=None):
    """Stripe's current state of an order's subscription (ai_local, `quantity` cameras)."""
    stripe["now"]["subscriptions"][ids["sub"]] = {
        "id": ids["sub"], "object": "subscription", "status": status, "customer": stripe_customer,
        "latest_invoice": latest_invoice or ids["invoice"], "current_period_end": 1800000000,
        "metadata": {"anyaicam_customer_id": customer_id, "anyaicam_billing_version": "2"},
        "items": {"data": [{"id": f"si_{ids['sub']}", "quantity": quantity, "price": {"id": PRICES["ai_local"]}}]}}


def _ok(client, event):
    response = _post(client, event)
    assert response.status_code == 200, (event["type"], response.text)
    return response


def _order1_paid(shop, db_path, stripe, email):
    """Order 1 (own PC, AI Local, 5 cameras) paid through the webhook route: checkout, first invoice, subscription."""
    client = shop[0]
    customer_id, event, ids = _place(shop, db_path, stripe, email=email, n=1)
    stripe_customer = event["data"]["object"]["customer"]
    _subscription(stripe, ids, customer_id, stripe_customer, quantity=5)
    events = {"checkout": event, "invoice": _invoice_paid(ids), "created": _sub_event(stripe, ids, "created")}
    for name in ("checkout", "invoice", "created"):
        _ok(client, events[name])
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["stripe_checkout_session_id"], licence["stripe_payment_intent_id"]) == (
        "active", ids["session"], ids["intent"])
    assert _plan_row(db_path, customer_id)["status"] == "active"
    assert _intent(db_path, customer_id)["state"] == "paid_setup_pending"
    return customer_id, ids, events, stripe_customer


def _refund_order1(shop, db_path, stripe, customer_id, ids, events):
    events["refund"] = _refund(ids, "evt_r7_refund1")
    _ok(shop[0], events["refund"])
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"]) == ("suspended", "refunded")
    assert (_plan_row(db_path, customer_id)["status"], _plan_row(db_path, customer_id)["suspended_reason"]) == ("suspended", "refunded")


def _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer):
    _subscription(stripe, ids, customer_id, stripe_customer, quantity=5, status="canceled")
    events["deleted"] = _sub_event(stripe, ids, "deleted")
    _ok(shop[0], events["deleted"])
    assert _plan_row(db_path, customer_id)["status"] == "cancelled"


def _r7_ready(shop, db_path, stripe, email="r7@example.test"):
    """Green's R7 state: order 1 paid, fully refunded, its subscription ended at Stripe."""
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, email)
    _refund_order1(shop, db_path, stripe, customer_id, ids, events)
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    return customer_id, ids, events, stripe_customer


def _order1_records(db_path, customer_id, ids):
    """Every record of order 1 that must never change: its payments and its reversal."""
    return {"payments": _payments(db_path, ids["sub"]), "reversals": _reversals(db_path, ids["intent"])}


def _order2_paid(shop, db_path, stripe, customer_id, stripe_customer):
    """Order 2 (the new pending order: AI Local, 10 cameras, licence for 16) paid through the webhook route."""
    client, captured = shop[0], shop[1]
    ids2 = _ids(2)
    fields = _checkouts(captured)[-1]
    assert fields["metadata[anyaicam_camera_quantity]"] == "10" and fields["metadata[anyaicam_vms_license_capacity]"] == "16"
    event = _paid_checkout_event(captured, customer_id, ids2["session"])
    obj = event["data"]["object"]
    obj["invoice"], obj["payment_intent"], obj["subscription"] = ids2["invoice"], None, ids2["sub"]
    obj["customer"] = stripe_customer
    _configure(stripe, ids2, stripe_customer)
    _subscription(stripe, ids2, customer_id, stripe_customer, quantity=10)
    events2 = {"checkout": event, "invoice": _invoice_paid(ids2), "created": _sub_event(stripe, ids2, "created")}
    for name in ("checkout", "invoice", "created"):
        _ok(client, events2[name])
    return ids2, events2


def _replays(stripe, ids, events, suffix):
    """Every order-1 event again: the same ids (Stripe's redelivery) and new ids (a resend), in a hostile order. The
    subscription events of a resend carry Stripe's current (ended) state of order 1's subscription."""
    replays = []
    for name in ("checkout", "invoice", "created", "refund", "deleted"):
        replays.append(copy.deepcopy(events[name]))
    for name in ("refund", "checkout", "invoice", "deleted", "refund"):
        again = copy.deepcopy(events[name])
        again["id"] = f"{events[name]['id']}_{suffix}_{len(replays)}"
        replays.append(again)
    for kind in ("updated", "created", "deleted"):
        replays.append(_sub_event(stripe, ids, kind, f"evt_r7_{suffix}_sub_{kind}"))
    return replays


def _replay_all(shop, stripe, ids, events, suffix):
    for event in _replays(stripe, ids, events, suffix):
        _ok(shop[0], event)
        assert stripe["now"]["subscriptions"][ids["sub"]]["status"] == "canceled"  # Stripe's truth never changed


def _new_order(shop, query=SECOND_ORDER_QUERY):
    client = shop[0]
    page = _order(client, query)
    assert page.status_code == 200 and 'id="order-checkout"' in page.text, page.text[:400]
    response = _checkout(client)
    assert response.status_code == 200, response.text
    return page, response.json()


# ------------------------------------------------------------------ the full R7 sequence

def test_r7_full_sequence_new_order_after_refund_and_ended_subscription(shop, db_path, stripe):
    client, captured = shop[0], shop[1]
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe)
    old_intent = _intent(db_path, customer_id)
    old_licence, old_plan = _licence_row(db_path, customer_id), _plan_row(db_path, customer_id)
    order1 = _order1_records(db_path, customer_id, ids)
    assert order1["reversals"] and _access(db_path, customer_id) == (True, False)  # (cancelled plan counts as activated)
    checkouts_before = len(_checkouts(captured))

    # The order summary offers a NEW order (licence + plan) and the checkout opens a NEW Stripe Checkout.
    page, opened = _new_order(shop)
    assert "VMS software license" in page.text and opened["status"] == "complete" and opened["checkout_url"]
    assert len(_checkouts(captured)) == checkouts_before + 1
    fields = _checkouts(captured)[-1]
    assert fields["mode"] == "subscription" and fields["metadata[anyaicam_build_order]"] == "own_pc"
    assert fields["line_items[0][quantity]"] == "10" and fields["line_items[1][price]"] == "price_vms_16"

    # A separate new pending order; the old one is preserved with every Stripe reference.
    intent = _intent(db_path, customer_id)
    assert (intent["state"], intent["plan_key"], intent["camera_quantity"], intent["own_pc"]) == ("pending_checkout", "ai_local", 10, 1)
    assert intent["paid_at"] is None and intent["created_at"] > old_intent["created_at"]
    [archived] = _superseded(db_path, customer_id)
    assert archived["previous_order"] == old_intent  # the paid order exactly as it was (state, paid_at, selection)
    assert archived["previous_order"]["state"] == "paid_setup_pending" and archived["previous_order"]["paid_at"]
    assert archived["licence"] == old_licence and archived["plan"] == old_plan
    assert (archived["licence"]["stripe_checkout_session_id"], archived["licence"]["stripe_payment_intent_id"],
            archived["licence"]["suspended_reason"]) == (ids["session"], ids["intent"], "refunded")
    assert (archived["plan"]["stripe_subscription_id"], archived["plan"]["status"]) == (ids["sub"], "cancelled")
    assert archived["payments"] == order1["payments"]
    # The refund itself is in the archive (charge, PaymentIntent, invoice, reason).
    assert [(r["charge_id"], r["payment_intent"], r["invoice_id"], r["reason"]) for r in archived["reversals"]] == order1["reversals"]
    # Nothing of order 1 was changed by starting order 2.
    assert _licence_row(db_path, customer_id) == old_licence and _plan_row(db_path, customer_id) == old_plan
    assert _order1_records(db_path, customer_id, ids) == order1

    # Order 2 is paid.
    ids2, _ = _order2_paid(shop, db_path, stripe, customer_id, stripe_customer)
    licence = _licence_row(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"], licence["stripe_checkout_session_id"],
            licence["stripe_payment_intent_id"], licence["camera_slot_quantity"]) == ("active", None, ids2["session"], ids2["intent"], 16)
    plan = _plan_row(db_path, customer_id)
    assert (plan["status"], plan["stripe_subscription_id"], plan["camera_quantity"], plan["plan_key"]) == ("active", ids2["sub"], 10, "ai_local")
    assert _access(db_path, customer_id) == (True, True)  # account activated, installer download allowed
    capacity = _capacity(db_path, customer_id)
    assert (capacity["camera_slot_quantity"], capacity["plan_camera_slots"], capacity["vms_license_capacity"]) == (10, 10, 10)
    assert _progress(db_path, customer_id)["state"] == "paid_setup_pending"
    assert _intent(db_path, customer_id)["paid_at"]
    assert _order1_records(db_path, customer_id, ids) == order1 and len(_superseded(db_path, customer_id)) == 1
    complete = client.get("/order-complete?session_id=" + ids2["session"])
    assert complete.status_code == 200 and 'id="order-checkout"' not in complete.text
    # The paid customer gets today's paid behaviour: no third order.
    assert _checkout(client).status_code == 409


def test_r7_order1_replays_before_and_after_order2_change_nothing(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe)
    order1 = _order1_records(db_path, customer_id, ids)
    old_licence, old_plan = _licence_row(db_path, customer_id), _plan_row(db_path, customer_id)

    # Before the new order exists: nothing of order 1 comes back.
    _replay_all(shop, stripe, ids, events, "pre")
    assert _licence_row(db_path, customer_id)["status"] == "suspended" and _plan_row(db_path, customer_id)["status"] == "cancelled"
    assert _access(db_path, customer_id)[1] is False

    _new_order(shop)
    pending = _intent(db_path, customer_id)
    # The new order is pending: replays neither pay it nor revive order 1.
    _replay_all(shop, stripe, ids, events, "pending")
    assert _intent(db_path, customer_id) == pending and _progress(db_path, customer_id)["state"] == "pending_checkout"
    licence, plan = _licence_row(db_path, customer_id), _plan_row(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"], licence["stripe_checkout_session_id"]) == ("suspended", "refunded", ids["session"])
    assert (plan["status"], plan["stripe_subscription_id"]) == ("cancelled", ids["sub"])
    assert _access(db_path, customer_id)[1] is False
    assert _order1_records(db_path, customer_id, ids) == order1
    assert {k: v for k, v in old_licence.items() if k != "updated_at"} == {k: v for k, v in licence.items() if k != "updated_at"}
    assert {k: v for k, v in old_plan.items() if k != "updated_at"} == {k: v for k, v in plan.items() if k != "updated_at"}

    ids2, _ = _order2_paid(shop, db_path, stripe, customer_id, stripe_customer)
    licence2, plan2 = _licence_row(db_path, customer_id), _plan_row(db_path, customer_id)
    assert (licence2["status"], licence2["stripe_payment_intent_id"]) == ("active", ids2["intent"])

    # After order 2 is paid: order 1's events never suspend, overwrite or replace order 2's licence or plan.
    _replay_all(shop, stripe, ids, events, "post")
    assert _licence_row(db_path, customer_id) == licence2 and _plan_row(db_path, customer_id) == plan2
    assert _access(db_path, customer_id) == (True, True) and _capacity(db_path, customer_id)["camera_slot_quantity"] == 10
    assert _order1_records(db_path, customer_id, ids) == order1 and len(_superseded(db_path, customer_id)) == 1

    # Refunding order 2 suspends order 2's licence (its own payment), as for any order.
    _ok(shop[0], _refund(ids2, "evt_r7_refund2"))
    licence = _licence_row(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"], licence["stripe_payment_intent_id"]) == ("suspended", "refunded", ids2["intent"])


def _order_n_paid(shop, db_path, stripe, customer_id, stripe_customer, n):
    """Order n (the account's current pending order) paid through the webhook route, like _order2_paid."""
    client, captured = shop[0], shop[1]
    ids_n = _ids(n)
    event = _paid_checkout_event(captured, customer_id, ids_n["session"])
    obj = event["data"]["object"]
    obj["invoice"], obj["payment_intent"], obj["subscription"], obj["customer"] = ids_n["invoice"], None, ids_n["sub"], stripe_customer
    _configure(stripe, ids_n, stripe_customer)
    _subscription(stripe, ids_n, customer_id, stripe_customer, quantity=10)
    events_n = {"checkout": event, "invoice": _invoice_paid(ids_n), "created": _sub_event(stripe, ids_n, "created")}
    for name in ("checkout", "invoice", "created"):
        _ok(client, events_n[name])
    return ids_n, events_n


def test_r7_repeated_repurchases_keep_every_orders_history_and_old_replays_never_touch_the_newest(shop, db_path, stripe):
    customer_id, ids1, events1, stripe_customer = _r7_ready(shop, db_path, stripe)
    order1 = _order1_records(db_path, customer_id, ids1)
    _new_order(shop)
    ids2, events2 = _order2_paid(shop, db_path, stripe, customer_id, stripe_customer)
    # Order 2 is also refunded and its subscription ends: a third order opens, and order 2 is archived in turn.
    events2["refund"] = _refund(ids2, "evt_r7_refund2")
    _ok(shop[0], events2["refund"])
    _subscription(stripe, ids2, customer_id, stripe_customer, quantity=10, status="canceled")
    events2["deleted"] = _sub_event(stripe, ids2, "deleted")
    _ok(shop[0], events2["deleted"])
    order2 = _order1_records(db_path, customer_id, ids2)
    assert order2["reversals"]
    _new_order(shop)
    ids3, _ = _order_n_paid(shop, db_path, stripe, customer_id, stripe_customer, 3)
    licence3, plan3 = _licence_row(db_path, customer_id), _plan_row(db_path, customer_id)
    assert (licence3["status"], licence3["stripe_checkout_session_id"], licence3["stripe_payment_intent_id"]) == (
        "active", ids3["session"], ids3["intent"])
    assert (plan3["status"], plan3["stripe_subscription_id"]) == ("active", ids3["sub"])

    # The complete history, oldest first: one archive per superseded order, each with its own references.
    first, second = _superseded(db_path, customer_id)
    for archived, ids, records in ((first, ids1, order1), (second, ids2, order2)):
        assert (archived["licence"]["stripe_checkout_session_id"], archived["licence"]["stripe_payment_intent_id"],
                archived["licence"]["suspended_reason"]) == (ids["session"], ids["intent"], "refunded")
        assert (archived["plan"]["stripe_subscription_id"], archived["plan"]["status"]) == (ids["sub"], "cancelled")
        assert archived["previous_order"]["paid_at"] and archived["payments"] == records["payments"]
        assert [(r["charge_id"], r["payment_intent"], r["invoice_id"], r["reason"]) for r in archived["reversals"]] == records["reversals"]
    assert _order1_records(db_path, customer_id, ids1) == order1 and _order1_records(db_path, customer_id, ids2) == order2

    # Replays of orders 1 and 2 never touch order 3.
    _replay_all(shop, stripe, ids1, events1, "o3a")
    _replay_all(shop, stripe, ids2, events2, "o3b")
    assert _licence_row(db_path, customer_id) == licence3 and _plan_row(db_path, customer_id) == plan3
    assert _access(db_path, customer_id) == (True, True) and len(_superseded(db_path, customer_id)) == 2


def test_r7_building_the_new_order_again_is_idempotent(shop, db_path, stripe):
    import per_camera_billing as billing
    client, captured = shop[0], shop[1]
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe)
    before = len(_checkouts(captured))
    first_page, first = _new_order(shop)
    second_page = _order(client, SECOND_ORDER_QUERY)
    assert second_page.status_code == 200 and 'id="order-checkout"' in second_page.text
    assert _order(client).status_code == 200
    second = _checkout(client)
    assert second.status_code == 200 and second.json()["session_id"] == first["session_id"]  # the same open Checkout
    with override_target(sqlite_path=str(db_path)):
        assert billing.supersede_refunded_order(customer_id) is False
    assert len(_checkouts(captured)) == before + 1 and len(_superseded(db_path, customer_id)) == 1
    assert _intent(db_path, customer_id)["state"] == "pending_checkout"


def test_r7_order_summary_without_a_selection_resumes_the_old_selection_as_a_new_order(shop, db_path, stripe):
    client = shop[0]
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe)
    page = _order(client)
    assert page.status_code == 200 and 'id="order-checkout"' in page.text and "VMS software license" in page.text
    intent = _intent(db_path, customer_id)
    assert (intent["state"], intent["plan_key"], intent["camera_quantity"], intent["own_pc"]) == ("pending_checkout", "ai_local", 5, 1)
    assert _checkout(client).status_code == 200


def test_r7_a_checkout_posted_from_an_order_page_opened_earlier_opens_the_new_order(shop, db_path, stripe):
    """Green's exact R7 click: the page was rendered before; the checkout itself starts the new order."""
    client = shop[0]
    customer_id, ids, events, stripe_customer = _r7_ready(shop, db_path, stripe)
    response = _checkout(client)
    assert response.status_code == 200, response.text
    assert _intent(db_path, customer_id)["state"] == "pending_checkout" and len(_superseded(db_path, customer_id)) == 1


def test_r7_setup_completed_before_the_refund_is_also_a_new_order(shop, db_path, stripe):
    from test_hybrid_build_system_flow import _activate_appliance
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "r7done@example.test")
    _activate_appliance(db_path, customer_id)
    assert _progress(db_path, customer_id) is None and _intent(db_path, customer_id)["state"] == "setup_complete"
    _refund_order1(shop, db_path, stripe, customer_id, ids, events)
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    _new_order(shop)
    [archived] = _superseded(db_path, customer_id)
    assert archived["previous_order"]["state"] == "setup_complete" and archived["previous_order"]["completed_at"]


# ------------------------------------------------------------------ accounts that keep today's behaviour

def _blocked(shop, db_path, customer_id, *, detail):
    client, captured = shop[0], shop[1]
    intent, before = _intent(db_path, customer_id), len(_checkouts(captured))
    page = _order(client, SECOND_ORDER_QUERY)
    assert 'id="order-checkout"' not in page.text or detail == NO_ORDER
    response = _checkout(client)
    assert response.status_code == 409 and response.json()["detail"] == detail
    assert len(_checkouts(captured)) == before and _intent(db_path, customer_id) == intent
    assert _superseded(db_path, customer_id) == []
    return page


def test_a_paid_order_still_active_never_gets_a_second_order(shop, db_path, stripe):
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "active@example.test")
    page = _blocked(shop, db_path, customer_id, detail="This account already has a camera plan or a paid order.")
    assert (page.status_code, page.headers.get("location")) == (303, "/order-complete")
    assert _licence(db_path, customer_id)["status"] == "active"


def test_a_refunded_order_whose_subscription_is_still_running_stays_blocked_until_it_ends(shop, db_path, stripe):
    """Intended: the refund suspends the plan but Stripe still runs its subscription; a new order would start a second
    subscription beside it. The customer (or Stripe) ends it first -- then the new order opens."""
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "running@example.test")
    _refund_order1(shop, db_path, stripe, customer_id, ids, events)
    page = _blocked(shop, db_path, customer_id, detail="This account already has a camera plan or a paid order.")
    assert (page.status_code, page.headers.get("location")) == (303, "/order-complete")
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    _new_order(shop)


def test_an_active_licence_whose_plan_ended_is_never_charged_a_second_licence(shop, db_path, stripe):
    """No double-buy of the one-time licence: the plan ended without a refund, so the order still grants the licence;
    nothing is superseded and no Checkout opens (today's behaviour)."""
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "ended@example.test")
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    _blocked(shop, db_path, customer_id, detail=NO_ORDER)
    assert _licence(db_path, customer_id)["status"] == "active"


def test_a_disputed_order_whose_subscription_ended_keeps_todays_behaviour(shop, db_path, stripe):
    """A dispute may still be won, which restores that licence: no new order beside it (owner decision for later)."""
    from test_refund_before_record_race_127 import _dispute
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "disputed@example.test")
    _ok(shop[0], _dispute(ids, "created"))
    assert _licence(db_path, customer_id)["suspended_reason"] == "disputed"
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    _blocked(shop, db_path, customer_id, detail=NO_ORDER)
