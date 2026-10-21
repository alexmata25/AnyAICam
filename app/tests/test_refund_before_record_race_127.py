"""RC10b (owner-authorized, 2026-10-21; Codex read-only review of RC10 99a19591): a full refund or a dispute of an own-PC
order's first invoice that arrives BEFORE AnyAiCam has recorded that payment (subscription_payments) or the plan
(camera_plan_entitlements_v2) must still revoke the one-time VMS licence -- whichever of the refund and the licence grant
comes first, on a replay, during a Stripe outage, and when the two race. Unrelated refunds touch nothing; partial
refunds change nothing; won disputes restore; later renewals never restore a refunded licence.

Real build_orders, billing_status and per_camera_billing code and the real webhook route; Stripe is a fake that answers
what the test configures (stripe_follows_events plus Checkout Sessions, invoices and invoice payments).
"""
import copy
import sqlite3
import threading
import time

import pytest

import stripe_state
from database_backend import override_target
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hardware_delivery_flow import OWN_PC
from test_hybrid_build_system_flow import (PRICES, _checkout, _customer_id, _new_customer, _paid_checkout_event,  # noqa: F401
                                           _record_build_order, db_path, license_portal, package, portal, shop, site,
                                           storage)

pytestmark = [pytest.mark.real_activation]

# test_hybrid_build_system_flow._pay replaces this module attribute for the rest of the session; these tests need
# Stripe's real reversal check when a plan is created.
_REAL_SUBSCRIPTION_PAYMENT_REVERSAL = stripe_state.subscription_payment_reversal

AMOUNT = 5 * 2000 + 49999


def _ids(n=1):
    return {"session": f"cs_test_race{n:04d}", "invoice": f"in_race{n}", "intent": f"pi_race{n}", "charge": f"ch_race{n}",
            "sub": f"sub_cs_test_race{n:04d}"}


@pytest.fixture()
def stripe(stripe_follows_events, monkeypatch):
    """Stripe as these tests configure it. Records every read; prefixes in state['down'] are unreachable."""
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", _REAL_SUBSCRIPTION_PAYMENT_REVERSAL)
    now = stripe_follows_events
    inner = stripe_state._stripe_reader()
    state = {"now": now, "sessions": {}, "down": set(), "reads": [], "hooks": {}}

    def reader(path):
        state["reads"].append(path)
        for prefix, hook in list(state["hooks"].items()):
            if path.startswith(prefix):
                hook(path)
        if any(path.startswith(prefix) for prefix in state["down"]):
            raise OSError("Stripe unreachable")
        if path.startswith("/v1/checkout/sessions/"):
            session_id = path.split("/")[4].split("?")[0]
            subscription, invoice = state["sessions"].get(session_id, (f"sub_{session_id}", f"in_{session_id}"))
            return {"id": session_id, "object": "checkout.session", "subscription": subscription, "invoice": invoice}
        return inner(path)
    monkeypatch.setattr(stripe_state, "_stripe_reader", lambda: reader)
    return state


@pytest.fixture()
def grant_cannot_read_payment(monkeypatch):
    """Stripe unreadable while the licence is granted: the row keeps no PaymentIntent (as every row granted by RC10
    99a19591 does), so a later refund must find it through the invoice's Stripe customer and the Checkout Session."""
    import billing_status

    def down(invoice_id):
        raise stripe_state.RetryableStripeEventError("Stripe unreachable")
    monkeypatch.setattr(billing_status, "invoice_payment", down)


def _configure(stripe, ids, stripe_customer, *, linked=True):
    now = stripe["now"]
    stripe["sessions"][ids["session"]] = (ids["sub"], ids["invoice"])
    now["invoice_payments"][ids["intent"]] = ([{"invoice": ids["invoice"], "status": "paid",
                                                "payment": {"type": "payment_intent", "payment_intent": ids["intent"]}}]
                                              if linked else [])
    now["invoices"][ids["invoice"]] = {"id": ids["invoice"], "object": "invoice", "status": "paid", "amount_paid": AMOUNT,
                                       "customer": stripe_customer, "subscription": ids["sub"],
                                       "payment_intent": ids["intent"], "charge": ids["charge"]}


def _place(shop, db_path, stripe, email="race@example.test", n=1, *, linked=True):
    """An own-PC Build Your System checkout, paid; Stripe knows its subscription, first invoice and payment. Nothing is
    recorded locally yet (no licence, no subscription_payments row, no plan)."""
    client, captured, mail, _, _ = shop
    client.cookies.clear()
    _new_customer(client, mail, email, OWN_PC)
    customer_id = _customer_id(db_path, email)
    assert _checkout(client).status_code == 200
    ids = _ids(n)
    event = _paid_checkout_event(captured, customer_id, ids["session"])
    obj = event["data"]["object"]
    obj["invoice"], obj["payment_intent"], obj["subscription"] = ids["invoice"], None, ids["sub"]
    obj["customer"] = obj.get("customer") or f"cus_test_{customer_id}"
    _configure(stripe, ids, obj["customer"], linked=linked)
    return customer_id, event, ids


def _second_order(stripe, event, n):
    """A later own-PC order of the same account (a new Checkout, a new first invoice and payment)."""
    ids = _ids(n)
    again = copy.deepcopy(event)
    again["id"] = f"evt_{ids['session']}"
    obj = again["data"]["object"]
    obj["id"], obj["invoice"], obj["subscription"] = ids["session"], ids["invoice"], ids["sub"]
    _configure(stripe, ids, obj["customer"])
    return again, ids


def _billing(db_path, event):
    import billing_status
    with override_target(sqlite_path=str(db_path)):
        return billing_status.sync_from_stripe_event(event)


def _refund(ids, event_id="evt_refund", *, refunded=None):
    refunded = AMOUNT if refunded is None else refunded
    return {"id": event_id, "type": "charge.refunded", "data": {"object": {
        "id": ids["charge"], "object": "charge", "payment_intent": ids["intent"], "amount": AMOUNT,
        "amount_refunded": refunded, "refunded": refunded >= AMOUNT}}}


def _dispute(ids, kind, status="needs_response", event_id=None):
    return {"id": event_id or f"evt_dp_{kind}_{status}", "type": f"charge.dispute.{kind}", "data": {"object": {
        "id": f"dp_{ids['charge']}", "object": "dispute", "charge": ids["charge"], "payment_intent": ids["intent"],
        "status": status}}}


def _invoice_paid(ids, invoice=None, intent=None):
    return {"id": f"evt_paid_{invoice or ids['invoice']}", "type": "invoice.paid", "data": {"object": {
        "id": invoice or ids["invoice"], "object": "invoice", "subscription": ids["sub"], "status": "paid",
        "payment_intent": intent or ids["intent"], "amount_paid": AMOUNT}}}


def _plan_arrives(db_path, customer_id, event, ids, latest_invoice=None):
    """The per-camera plan as the camera_plan_v2 step applies Stripe's subscription (its latest invoice is the first)."""
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        return billing._upsert_current({
            "id": ids["sub"], "customer": event["data"]["object"]["customer"], "status": "active",
            "metadata": {"anyaicam_customer_id": customer_id, "anyaicam_billing_version": "2"},
            "current_period_end": 1800000000, "latest_invoice": latest_invoice or ids["invoice"],
            "items": {"data": [{"id": "si_race", "quantity": 5, "price": {"id": PRICES["ai_local"]}}]}})


def _licence(db_path, customer_id):
    return _one(db_path, "SELECT status,suspended_reason,stripe_payment_intent_id,stripe_checkout_session_id "
                         "FROM customer_entitlements WHERE customer_id=? AND product='vms_license'", (customer_id,))


def _plan(db_path, customer_id):
    return _one(db_path, "SELECT status,suspended_reason FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer_id,))


def _reversals(db_path, intent):
    conn = sqlite3.connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='billing_reversals'").fetchone():
            return []
        return conn.execute("SELECT charge_id,payment_intent,invoice_id,reason FROM billing_reversals WHERE payment_intent=?",
                            (intent,)).fetchall()
    finally:
        conn.close()


def _access(db_path, customer_id):
    """(account activated, installer download allowed)"""
    import customer_downloads
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        allowed, _ = customer_downloads.download_eligibility({"role": "customer_owner", "customer_id": customer_id})
        return billing.account_activated(customer_id), allowed


def _session_reads(stripe):
    return [path for path in stripe["reads"] if path.startswith("/v1/checkout/sessions/")]


# ------------------------------------------------------------------ licence granted, then refund with no local records

@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_refund_before_payment_and_plan_records_suspends_an_already_granted_licence(shop, db_path, stripe, monkeypatch,
                                                                                     payment_at_grant):
    if not payment_at_grant:
        monkeypatch.setattr("billing_status.invoice_payment",
                            lambda invoice_id: (_ for _ in ()).throw(stripe_state.RetryableStripeEventError("down")))
    customer_id, event, ids = _place(shop, db_path, stripe)
    assert _record_build_order(db_path, event)["status"] == "vms_license_granted"
    assert _licence(db_path, customer_id)["stripe_payment_intent_id"] == (ids["intent"] if payment_at_grant else None)
    assert _access(db_path, customer_id) == (True, True)

    result = _billing(db_path, _refund(ids))
    assert (result["status"], result["rows"]) == ("suspended", 1)
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"], licence["stripe_payment_intent_id"]) == ("suspended", "refunded", ids["intent"])
    assert _access(db_path, customer_id) == (False, False)
    assert _reversals(db_path, ids["intent"]) == [(ids["charge"], ids["intent"], ids["invoice"], "refunded")]
    # The licence row already named its payment: no Checkout Session lookup was needed.
    assert len(_session_reads(stripe)) == (0 if payment_at_grant else 1)

    # A replay changes nothing and is acknowledged.
    replay = _billing(db_path, _refund(ids))
    assert replay["status"] == "ignored"
    assert _licence(db_path, customer_id)["status"] == "suspended" and len(_reversals(db_path, ids["intent"])) == 1


def test_plan_invoice_paid_and_checkout_replay_after_the_early_refund_keep_everything_suspended(shop, db_path, stripe,
                                                                                               grant_cannot_read_payment):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    assert _billing(db_path, _refund(ids))["status"] == "suspended"
    # camera_plan_v2 (retried by Stripe) creates the plan: Stripe's latest invoice is the refunded first one.
    _plan_arrives(db_path, customer_id, event, ids)
    assert _plan(db_path, customer_id) == {"status": "suspended", "suspended_reason": "refunded"}
    # invoice.paid for the refunded first invoice never restores anything.
    assert _billing(db_path, _invoice_paid(ids))["status"] == "ignored"
    # A redelivered checkout event never re-activates the licence.
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
    assert _licence(db_path, customer_id)["status"] == "suspended"
    assert _plan(db_path, customer_id)["status"] == "suspended"
    assert _access(db_path, customer_id) == (False, False)


# ------------------------------------------------------------------ refund BEFORE the licence grant (reversed ordering)

@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_refund_arriving_before_the_checkout_grant_makes_the_grant_land_suspended(shop, db_path, stripe, monkeypatch,
                                                                                 payment_at_grant):
    if not payment_at_grant:
        monkeypatch.setattr("billing_status.invoice_payment",
                            lambda invoice_id: (_ for _ in ()).throw(stripe_state.RetryableStripeEventError("down")))
    customer_id, event, ids = _place(shop, db_path, stripe)
    result = _billing(db_path, _refund(ids))
    assert result["status"] == "recorded"  # nothing granted yet: acknowledged (2xx), applied when it is granted
    assert _reversals(db_path, ids["intent"]) == [(ids["charge"], ids["intent"], ids["invoice"], "refunded")]
    for _ in range(2):  # the checkout event, then its redelivery
        assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
        licence = _licence(db_path, customer_id)
        assert (licence["status"], licence["suspended_reason"], licence["stripe_payment_intent_id"]) == ("suspended", "refunded", ids["intent"])
    assert _billing(db_path, _refund(ids))["status"] == "ignored"  # refund replay
    assert _access(db_path, customer_id) == (False, False)


def test_a_refund_stripe_never_linked_to_an_invoice_is_still_honoured_at_grant_by_payment_intent(shop, db_path, stripe):
    """Missing association: Stripe names no invoice for the refunded PaymentIntent (e.g. a reversal recorded by RC10 during
    a Stripe outage, without its invoice). The grant reads its invoice's payment and finds the reversal by PaymentIntent."""
    customer_id, event, ids = _place(shop, db_path, stripe, linked=False)
    assert _billing(db_path, _refund(ids))["status"] == "ignored"  # Stripe: this payment paid no invoice
    assert _reversals(db_path, ids["intent"]) == [(ids["charge"], ids["intent"], None, "refunded")]
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
    assert _licence(db_path, customer_id)["status"] == "suspended" and _access(db_path, customer_id) == (False, False)


# ------------------------------------------------------------------ disputes before the records

def test_a_dispute_before_the_records_suspends_the_licence_and_a_won_dispute_restores_licence_and_plan(shop, db_path, stripe,
                                                                                                    grant_cannot_read_payment):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    _plan_arrives(db_path, customer_id, event, ids)
    assert _plan(db_path, customer_id)["status"] == "active"
    assert _billing(db_path, _dispute(ids, "created"))["status"] == "suspended"
    assert _licence(db_path, customer_id)["suspended_reason"] == "disputed"
    assert _plan(db_path, customer_id) == {"status": "suspended", "suspended_reason": "disputed"}
    assert _access(db_path, customer_id) == (False, False)
    assert _billing(db_path, _dispute(ids, "closed", "won"))["status"] == "restored"
    assert _licence(db_path, customer_id)["status"] == "active" and _plan(db_path, customer_id)["status"] == "active"
    assert _access(db_path, customer_id) == (True, True)
    # A checkout replay after the won dispute keeps the licence active.
    assert _record_build_order(db_path, event)["status"] == "vms_license_granted"


def test_a_dispute_before_the_grant_then_won_grants_nothing_suspended_for_ever(shop, db_path, stripe):
    customer_id, event, ids = _place(shop, db_path, stripe)
    assert _billing(db_path, _dispute(ids, "created"))["status"] == "recorded"
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
    assert _licence(db_path, customer_id)["suspended_reason"] == "disputed"
    assert _billing(db_path, _dispute(ids, "closed", "won"))["status"] == "restored"
    assert _licence(db_path, customer_id)["status"] == "active"


def test_a_lost_dispute_before_the_records_keeps_the_licence_suspended(shop, db_path, stripe, grant_cannot_read_payment):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    _billing(db_path, _dispute(ids, "created"))
    assert _billing(db_path, _dispute(ids, "closed", "lost"))["status"] == "ignored"
    assert _licence(db_path, customer_id)["status"] == "suspended"
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
    assert _access(db_path, customer_id) == (False, False)


def test_a_partial_refund_before_the_records_changes_nothing_and_reads_nothing(shop, db_path, stripe):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    stripe["reads"].clear()
    assert _billing(db_path, _refund(ids, refunded=10000))["status"] == "ignored"
    assert stripe["reads"] == [] and _reversals(db_path, ids["intent"]) == []
    assert _licence(db_path, customer_id)["status"] == "active" and _access(db_path, customer_id) == (True, True)


# ------------------------------------------------------------------ Stripe outage: retried, nothing half-written

@pytest.mark.parametrize("down", ["/v1/invoice_payments", "/v1/invoices/", "/v1/checkout/sessions/"])
def test_a_stripe_outage_during_the_refund_lookup_is_retried_and_writes_nothing(shop, db_path, stripe, down,
                                                                                grant_cannot_read_payment):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    stripe["down"].add(down)
    with pytest.raises(stripe_state.RetryableStripeEventError):
        _billing(db_path, _refund(ids))
    assert _reversals(db_path, ids["intent"]) == []  # the whole transaction rolled back
    assert _licence(db_path, customer_id)["status"] == "active"
    stripe["down"].clear()
    assert _billing(db_path, _refund(ids))["status"] == "suspended"  # Stripe's redelivery
    assert _licence(db_path, customer_id)["suspended_reason"] == "refunded"


def test_an_unexpected_stripe_answer_is_retried_not_read_as_no_invoice(shop, db_path, stripe, monkeypatch):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    inner = stripe_state._stripe_reader()
    monkeypatch.setattr(stripe_state, "_stripe_reader",
                        lambda: (lambda path: {"error": "odd"} if path.startswith("/v1/invoice_payments") else inner(path)))
    with pytest.raises(stripe_state.RetryableStripeEventError):
        _billing(db_path, _refund(ids))
    assert _reversals(db_path, ids["intent"]) == [] and _licence(db_path, customer_id)["status"] == "active"


# ------------------------------------------------------------------ unrelated refunds and other accounts

def test_an_unrelated_refund_is_ignored_and_touches_nothing(shop, db_path, stripe, grant_cannot_read_payment):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    stripe["reads"].clear()
    other = {"intent": "pi_not_ours", "charge": "ch_not_ours"}
    assert _billing(db_path, _refund(other, "evt_unrelated"))["status"] == "ignored"  # Stripe: it paid no invoice
    # A refund of another business's subscription invoice (unknown Stripe customer, unknown subscription).
    foreign = {"intent": "pi_foreign", "charge": "ch_foreign"}
    stripe["now"]["invoice_payments"]["pi_foreign"] = [{"invoice": "in_foreign", "payment": {"payment_intent": "pi_foreign"}}]
    stripe["now"]["invoices"]["in_foreign"] = {"id": "in_foreign", "customer": "cus_foreign", "subscription": "sub_foreign",
                                               "amount_paid": 100}
    assert _billing(db_path, _refund(foreign, "evt_foreign"))["status"] == "recorded"
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["stripe_payment_intent_id"]) == ("active", None)
    assert _session_reads(stripe) == []  # nothing of this account was even looked at
    assert _access(db_path, customer_id) == (True, True)


def test_a_refund_never_binds_another_accounts_licence(shop, db_path, stripe, grant_cannot_read_payment):
    customer_a, event_a, ids_a = _place(shop, db_path, stripe, "race-a@example.test", 1)
    customer_b, event_b, ids_b = _place(shop, db_path, stripe, "race-b@example.test", 2)
    _record_build_order(db_path, event_a)
    _record_build_order(db_path, event_b)
    assert _billing(db_path, _refund(ids_a, "evt_refund_a"))["status"] == "suspended"
    assert _licence(db_path, customer_a)["status"] == "suspended"
    licence_b = _licence(db_path, customer_b)
    assert (licence_b["status"], licence_b["stripe_payment_intent_id"]) == ("active", None)


def test_a_wrong_stripe_customer_on_the_invoice_binds_nothing_and_the_late_grant_still_lands_suspended(shop, db_path, stripe,
                                                                                                    grant_cannot_read_payment):
    """Missing / wrong association: the refunded invoice names account B's Stripe customer. B's licence was bought with
    another Checkout (another subscription and invoice), so it is never bound; A's licence, granted later, is suspended."""
    customer_a, event_a, ids_a = _place(shop, db_path, stripe, "race-a@example.test", 1)
    customer_b, event_b, ids_b = _place(shop, db_path, stripe, "race-b@example.test", 2)
    _record_build_order(db_path, event_b)
    stripe["now"]["invoices"][ids_a["invoice"]]["customer"] = event_b["data"]["object"]["customer"]
    assert _billing(db_path, _refund(ids_a, "evt_refund_a"))["status"] == "recorded"
    licence_b = _licence(db_path, customer_b)
    assert (licence_b["status"], licence_b["stripe_payment_intent_id"]) == ("active", None)
    assert _record_build_order(db_path, event_a)["status"] == "vms_license_not_granted"
    assert _licence(db_path, customer_a)["status"] == "suspended"
    assert _licence(db_path, customer_b)["status"] == "active"


# ------------------------------------------------------------------ a second own-PC order

@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_a_refund_of_a_second_own_pc_order_finds_that_orders_licence(shop, db_path, stripe, monkeypatch, payment_at_grant):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    assert _billing(db_path, _refund(ids))["status"] == "suspended"
    if not payment_at_grant:
        monkeypatch.setattr("billing_status.invoice_payment",
                            lambda invoice_id: (_ for _ in ()).throw(stripe_state.RetryableStripeEventError("down")))
    second, ids2 = _second_order(stripe, event, 2)
    assert _record_build_order(db_path, second)["status"] == "vms_license_granted"
    licence = _licence(db_path, customer_id)
    # The row is the second order's now: never the first order's (refunded) PaymentIntent.
    assert (licence["status"], licence["suspended_reason"], licence["stripe_checkout_session_id"]) == ("active", None, ids2["session"])
    assert licence["stripe_payment_intent_id"] == (ids2["intent"] if payment_at_grant else None)
    # A replay of the FIRST (refunded) order's checkout does not suspend the second order's licence.
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
    assert _licence(db_path, customer_id)["status"] == "active"
    assert _billing(db_path, _refund(ids2, "evt_refund_second"))["status"] == "suspended"
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"], licence["stripe_payment_intent_id"]) == ("suspended", "refunded", ids2["intent"])


# ------------------------------------------------------------------ renewals never restore a refunded licence (item 2)

def test_renewal_invoice_paid_and_subscription_updated_after_the_early_refund_keep_the_licence_suspended(shop, db_path, stripe,
                                                                                                      grant_cannot_read_payment):
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    _plan_arrives(db_path, customer_id, event, ids)
    assert _billing(db_path, _refund(ids))["status"] == "suspended"
    assert _plan(db_path, customer_id)["suspended_reason"] == "refunded"
    # The next month is paid: the plan follows the subscription rules (restored), the refunded licence never does.
    stripe["now"]["invoices"]["in_race_month2"] = {"id": "in_race_month2", "status": "paid", "amount_paid": 10000,
                                                   "subscription": ids["sub"], "payment_intent": "pi_race_month2"}
    assert _billing(db_path, _invoice_paid(ids, "in_race_month2", "pi_race_month2"))["status"] == "recovered"
    assert _plan(db_path, customer_id)["status"] == "active"
    assert _licence(db_path, customer_id)["suspended_reason"] == "refunded"
    _billing(db_path, {"id": "evt_sub_upd", "type": "customer.subscription.updated", "data": {"object": {
        "id": ids["sub"], "object": "subscription", "status": "active", "customer": event["data"]["object"]["customer"],
        "latest_invoice": "in_race_month2"}}})
    _plan_arrives(db_path, customer_id, event, ids, latest_invoice="in_race_month2")
    assert _licence(db_path, customer_id)["status"] == "suspended"
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"


# ------------------------------------------------------------------ concurrency (SQLite: two connections, two threads)

def _in_thread(db_path, fn, results, key):
    def run():
        try:
            with override_target(sqlite_path=str(db_path)):
                results[key] = fn()
        except Exception as error:  # surfaced by the test
            results[key] = error
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _grant_fn(event):
    import build_orders
    return lambda: build_orders.sync_from_stripe_event(event)


def _refund_fn(ids, event_id):
    import billing_status
    return lambda: billing_status.sync_from_stripe_event(_refund(ids, event_id))


@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_grant_and_refund_started_together_always_end_suspended(shop, db_path, stripe, monkeypatch, payment_at_grant):
    if not payment_at_grant:
        monkeypatch.setattr("billing_status.invoice_payment",
                            lambda invoice_id: (_ for _ in ()).throw(stripe_state.RetryableStripeEventError("down")))
    orders = [_place(shop, db_path, stripe, f"race-{n}@example.test", n) for n in range(1, 5)]
    for n, (customer_id, event, ids) in enumerate(orders):
        results, start = {}, threading.Barrier(2)
        grant, refund = _grant_fn(event), _refund_fn(ids, f"evt_refund_{n}")
        first, second = (grant, refund) if n % 2 == 0 else (refund, grant)
        threads = [_in_thread(db_path, lambda f=first: (start.wait(5), f())[1], results, "a"),
                   _in_thread(db_path, lambda f=second: (start.wait(5), f())[1], results, "b")]
        for thread in threads:
            thread.join(30)
        assert not any(isinstance(value, Exception) for value in results.values()), results
        licence = _licence(db_path, customer_id)
        assert (licence["status"], licence["suspended_reason"]) == ("suspended", "refunded"), (n, results)


@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_forced_interleaving_refund_commits_between_the_grants_stripe_read_and_its_lock(shop, db_path, stripe, monkeypatch,
                                                                                       payment_at_grant):
    import billing_status
    customer_id, event, ids = _place(shop, db_path, stripe)
    real = billing_status.invoice_payment
    grant_read, refund_done = threading.Event(), threading.Event()

    def slow_read(invoice_id):
        try:
            if not payment_at_grant:
                raise stripe_state.RetryableStripeEventError("down")
            return real(invoice_id)
        finally:
            grant_read.set()
            assert refund_done.wait(10)
    monkeypatch.setattr(billing_status, "invoice_payment", slow_read)
    results = {}
    grant = _in_thread(db_path, _grant_fn(event), results, "grant")
    assert grant_read.wait(10)
    refund = _in_thread(db_path, _refund_fn(ids, "evt_refund_mid"), results, "refund")
    refund.join(30)
    refund_done.set()
    grant.join(30)
    assert results["refund"]["status"] == "recorded" and results["grant"]["status"] == "vms_license_not_granted"
    assert _licence(db_path, customer_id)["status"] == "suspended"


@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_forced_interleaving_grant_waits_while_the_refund_holds_the_lock(shop, db_path, stripe, monkeypatch, payment_at_grant):
    """The refund is inside its transaction (asking Stripe) when the grant starts: the grant waits for it and then sees it."""
    if not payment_at_grant:
        monkeypatch.setattr("billing_status.invoice_payment",
                            lambda invoice_id: (_ for _ in ()).throw(stripe_state.RetryableStripeEventError("down")))
    customer_id, event, ids = _place(shop, db_path, stripe)
    in_lookup, grant_started = threading.Event(), threading.Event()

    def hold(path):
        in_lookup.set()
        assert grant_started.wait(10)
        time.sleep(0.5)  # the grant is now blocked on the write lock
    stripe["hooks"]["/v1/invoice_payments?payment"] = hold
    results = {}
    refund = _in_thread(db_path, _refund_fn(ids, "evt_refund_hold"), results, "refund")
    assert in_lookup.wait(10)

    def grant_now():
        grant_started.set()
        return _grant_fn(event)()
    grant = _in_thread(db_path, grant_now, results, "grant")
    refund.join(30)
    grant.join(30)
    assert results["refund"]["status"] == "recorded", results
    assert results["grant"]["status"] == "vms_license_not_granted", results
    assert _licence(db_path, customer_id)["status"] == "suspended"


@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_forced_interleaving_refund_waits_while_the_grant_holds_the_lock(shop, db_path, stripe, monkeypatch, payment_at_grant):
    """The grant is inside its transaction when the refund starts: the refund waits for it and then suspends the licence."""
    import customer_entitlements
    if not payment_at_grant:
        monkeypatch.setattr("billing_status.invoice_payment",
                            lambda invoice_id: (_ for _ in ()).throw(stripe_state.RetryableStripeEventError("down")))
    customer_id, event, ids = _place(shop, db_path, stripe)
    in_grant, refund_started = threading.Event(), threading.Event()
    real_upsert = customer_entitlements.upsert_entitlement

    def slow_upsert(**kwargs):
        in_grant.set()
        assert refund_started.wait(10)
        time.sleep(0.5)  # the refund is now blocked on the write lock
        return real_upsert(**kwargs)
    monkeypatch.setattr(customer_entitlements, "upsert_entitlement", slow_upsert)
    results = {}
    grant = _in_thread(db_path, _grant_fn(event), results, "grant")
    assert in_grant.wait(10)

    def refund_now():
        refund_started.set()
        return _refund_fn(ids, "evt_refund_after")()
    refund = _in_thread(db_path, refund_now, results, "refund")
    grant.join(30)
    refund.join(30)
    assert results["grant"]["status"] == "vms_license_granted", results
    assert results["refund"]["status"] == "suspended", results
    assert _licence(db_path, customer_id)["status"] == "suspended"


# ------------------------------------------------------------------ the real webhook route

def _post(client, event):
    import json
    return client.post("/api/payments/stripe/webhook", content=json.dumps(event),
                       headers={"stripe-signature": "t=1,v1=test", "content-type": "application/json"})


def _steps(db_path, event_id):
    conn = sqlite3.connect(db_path)
    try:
        return {step: (status, attempts) for step, status, attempts in conn.execute(
            "SELECT step,status,attempts FROM stripe_webhook_steps WHERE event_id=?", (event_id,))}
    finally:
        conn.close()


@pytest.mark.parametrize("payment_at_grant", [True, False], ids=["payment-read-at-grant", "grant-without-payment"])
def test_webhook_route_licence_granted_plan_step_failed_then_refund_then_redelivery(shop, db_path, stripe, monkeypatch,
                                                                                   payment_at_grant):
    import per_camera_billing
    if not payment_at_grant:
        monkeypatch.setattr("billing_status.invoice_payment",
                            lambda invoice_id: (_ for _ in ()).throw(stripe_state.RetryableStripeEventError("down")))
    client = shop[0]
    customer_id, event, ids = _place(shop, db_path, stripe)
    real_plan_step = per_camera_billing.sync_from_stripe_event
    plan_step_down = {"on": True}

    def plan_step(evt):
        if plan_step_down["on"] and str(evt.get("type")).startswith("checkout."):
            raise stripe_state.RetryableStripeEventError("Stripe unreachable")
        return real_plan_step(evt)
    monkeypatch.setattr(per_camera_billing, "sync_from_stripe_event", plan_step)

    first = _post(client, event)
    assert first.status_code == 503 and "camera_plan_v2_entitlements" in first.json()["detail"]
    assert _steps(db_path, event["id"])["build_orders"][0] == "completed"
    assert _licence(db_path, customer_id)["status"] == "active" and _plan(db_path, customer_id) is None

    refund = _refund(ids, "evt_route_refund")
    assert _post(client, refund).status_code == 200
    assert _steps(db_path, "evt_route_refund")["billing_status"] == ("completed", 1)
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"]) == ("suspended", "refunded")

    plan_step_down["on"] = False
    # Stripe's subscription now: 5 cameras on the plan's Price, its latest invoice the refunded first one.
    stripe["now"]["subscriptions"][ids["sub"]].update({
        "latest_invoice": ids["invoice"], "current_period_end": 1800000000,
        "metadata": {"anyaicam_customer_id": customer_id, "anyaicam_billing_version": "2"},
        "items": {"data": [{"id": "si_race", "quantity": 5, "price": {"id": PRICES["ai_local"]}}]}})
    assert _post(client, event).status_code == 200  # Stripe's redelivery runs the plan step only
    assert _steps(db_path, event["id"])["build_orders"] == ("completed", 1)
    assert _plan(db_path, customer_id) == {"status": "suspended", "suspended_reason": "refunded"}
    assert _licence(db_path, customer_id)["status"] == "suspended"
    assert _access(db_path, customer_id) == (False, False)
    assert _post(client, refund).status_code == 200  # a refund redelivery is a duplicate


def test_webhook_route_answers_503_while_stripe_cannot_name_the_refunds_invoice(shop, db_path, stripe, grant_cannot_read_payment):
    client = shop[0]
    customer_id, event, ids = _place(shop, db_path, stripe)
    _record_build_order(db_path, event)
    stripe["down"].add("/v1/invoice_payments")
    refund = _refund(ids, "evt_route_outage")
    assert _post(client, refund).status_code == 503
    assert _steps(db_path, "evt_route_outage")["billing_status"][0] == "failed"
    assert _licence(db_path, customer_id)["status"] == "active" and _reversals(db_path, ids["intent"]) == []
    stripe["down"].clear()
    assert _post(client, refund).status_code == 200
    assert _steps(db_path, "evt_route_outage")["billing_status"] == ("completed", 2)
    assert _licence(db_path, customer_id)["status"] == "suspended"
