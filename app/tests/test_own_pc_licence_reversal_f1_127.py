"""F1 (Red Stripe/VMS inspection, 2026-10-21): an own-PC Build Your System order is one subscription Checkout -- the
per-camera plan plus the one-time VMS software licence, both charged on the subscription's FIRST invoice. A full refund
or a dispute of that payment must not leave the licence active: the licence is suspended with the plan, so the account
is no longer activated and the installer is no longer downloadable. A won dispute restores both; a partial refund and
refunds/disputes of later periods behave as before. Real build_orders and billing_status code; Stripe is a fake.
"""
import sqlite3

import pytest

from database_backend import override_target
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hardware_delivery_flow import OWN_PC
from test_hybrid_build_system_flow import (_checkout, _customer_id, _new_customer, _paid_checkout_event,  # noqa: F401
                                           _pay, _record_build_order, db_path, license_portal, package, portal, shop, site,
                                           storage)

pytestmark = [pytest.mark.usefixtures("stripe_follows_events"), pytest.mark.real_activation]

SESSION = "cs_test_ownpc0001"
SUB = f"sub_{SESSION}"
FIRST_INVOICE, FIRST_INTENT, FIRST_CHARGE = "in_ownpc_first", "pi_ownpc_first", "ch_ownpc_first"
FIRST_AMOUNT = 5 * 2000 + 49999


@pytest.fixture()
def stripe_sessions(monkeypatch):
    """Stripe's Checkout Session for the own-PC order: it names its subscription and first invoice."""
    import stripe_state
    inner = stripe_state._stripe_reader()
    reads = []

    def reader(path):
        if path.startswith("/v1/checkout/sessions/"):
            reads.append(path)
            session_id = path.split("/")[4].split("?")[0]
            return {"id": session_id, "object": "checkout.session", "subscription": f"sub_{session_id}", "invoice": FIRST_INVOICE}
        return inner(path)
    monkeypatch.setattr(stripe_state, "_stripe_reader", lambda: reader)
    return reads


def _exec(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _invoice_paid(db_path, customer_id, invoice_id, intent, charge, period_start, amount=FIRST_AMOUNT):
    """The subscription payment as sales_commissions records it from invoice.paid."""
    _exec(db_path, "INSERT INTO subscription_payments(id,customer_id,stripe_customer_id,stripe_subscription_id,stripe_price_id,"
                   "product_class,amount_paid_cents,currency,period_start,paid_at,stripe_charge_id,stripe_payment_intent_id,status) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'paid')",
          (invoice_id, customer_id, f"cus_test_{customer_id}", SUB, "price_srv_ai", "ai_local", amount, "usd", period_start,
           period_start, charge, intent))


def _order(shop, db_path, email="ownpc@example.test", *, grant=True, invoice_paid=True, with_invoice=True):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, email, OWN_PC)
    customer_id = _customer_id(db_path, email)
    assert _checkout(client).status_code == 200
    event = _paid_checkout_event(captured, customer_id, SESSION)
    if with_invoice:
        event["data"]["object"]["invoice"] = FIRST_INVOICE
    event["data"]["object"]["payment_intent"] = None  # a subscription Checkout has no PaymentIntent of its own
    if invoice_paid:
        _invoice_paid(db_path, customer_id, FIRST_INVOICE, FIRST_INTENT, FIRST_CHARGE, "2026-10-01T00:00:00")
    if grant:
        assert _record_build_order(db_path, event)["status"] == "vms_license_granted"
        _pay(db_path, customer_id, "ai_local", 5, SUB)
    return customer_id, event


def _billing(db_path, event):
    import billing_status
    with override_target(sqlite_path=str(db_path)):
        return billing_status.sync_from_stripe_event(event)


def _refund(event_id, *, intent=FIRST_INTENT, charge=FIRST_CHARGE, amount=FIRST_AMOUNT, refunded=None):
    refunded = amount if refunded is None else refunded
    return {"id": event_id, "type": "charge.refunded", "data": {"object": {
        "id": charge, "object": "charge", "payment_intent": intent, "amount": amount, "amount_refunded": refunded,
        "refunded": refunded >= amount}}}


def _dispute(event_id, kind, status="needs_response", *, intent=FIRST_INTENT, charge=FIRST_CHARGE):
    return {"id": event_id, "type": f"charge.dispute.{kind}", "data": {"object": {
        "id": f"dp_{charge}", "object": "dispute", "charge": charge, "payment_intent": intent, "status": status}}}


def _licence(db_path, customer_id):
    return _one(db_path, "SELECT status,suspended_reason,stripe_payment_intent_id FROM customer_entitlements "
                         "WHERE customer_id=? AND product='vms_license'", (customer_id,))


def _plan(db_path, customer_id):
    return _one(db_path, "SELECT status,suspended_reason FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer_id,))


def _access(db_path, customer_id):
    """(account activated, installer download allowed)"""
    import customer_downloads
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        allowed, _ = customer_downloads.download_eligibility({"role": "customer_owner", "customer_id": customer_id})
        return billing.account_activated(customer_id), allowed


# ------------------------------------------------------------------ normal paid flow

def test_a_paid_own_pc_order_keeps_the_licence_activation_and_download(shop, db_path, stripe_sessions):
    customer_id, _ = _order(shop, db_path)
    assert _licence(db_path, customer_id)["status"] == "active" and _plan(db_path, customer_id)["status"] == "active"
    assert _access(db_path, customer_id) == (True, True)
    # An unrelated refund changes nothing and reads nothing for this account.
    assert _billing(db_path, _refund("evt_other", intent="pi_other", charge="ch_other"))["status"] == "ignored"
    assert _licence(db_path, customer_id)["status"] == "active" and _access(db_path, customer_id) == (True, True)
    assert stripe_sessions == []


# ------------------------------------------------------------------ first invoice refunded / disputed

def test_a_full_refund_of_the_first_invoice_revokes_licence_activation_and_download(shop, db_path, stripe_sessions):
    customer_id, event = _order(shop, db_path)
    for _ in range(2):  # a replay changes nothing further
        result = _billing(db_path, _refund("evt_refund_first"))
        assert result["status"] in ("suspended",)
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"], licence["stripe_payment_intent_id"]) == ("suspended", "refunded", FIRST_INTENT)
    assert _plan(db_path, customer_id) == {"status": "suspended", "suspended_reason": "refunded"}
    assert _access(db_path, customer_id) == (False, False)
    # A redelivered checkout event never re-activates the refunded licence.
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
    assert _licence(db_path, customer_id)["status"] == "suspended" and _access(db_path, customer_id) == (False, False)


def test_a_dispute_of_the_first_invoice_revokes_access_and_a_won_dispute_restores_it(shop, db_path, stripe_sessions):
    customer_id, _ = _order(shop, db_path)
    assert _billing(db_path, _dispute("evt_dp_open", "created"))["status"] == "suspended"
    assert _licence(db_path, customer_id)["suspended_reason"] == "disputed" and _access(db_path, customer_id) == (False, False)
    assert _billing(db_path, _dispute("evt_dp_won", "closed", status="won"))["status"] == "restored"
    assert _licence(db_path, customer_id)["status"] == "active" and _plan(db_path, customer_id)["status"] == "active"
    assert _access(db_path, customer_id) == (True, True)


def test_a_lost_dispute_keeps_access_revoked(shop, db_path, stripe_sessions):
    customer_id, _ = _order(shop, db_path)
    _billing(db_path, _dispute("evt_dp_open", "created"))
    assert _billing(db_path, _dispute("evt_dp_lost", "closed", status="lost"))["status"] == "ignored"
    assert _licence(db_path, customer_id)["status"] == "suspended" and _access(db_path, customer_id) == (False, False)


def test_a_partial_refund_changes_nothing(shop, db_path, stripe_sessions):
    customer_id, _ = _order(shop, db_path)
    assert _billing(db_path, _refund("evt_partial", refunded=10000))["status"] == "ignored"
    assert _licence(db_path, customer_id)["status"] == "active" and _plan(db_path, customer_id)["status"] == "active"
    assert _access(db_path, customer_id) == (True, True) and stripe_sessions == []


def test_a_refund_recorded_before_the_licence_is_granted_is_honoured(shop, db_path, stripe_sessions):
    customer_id, event = _order(shop, db_path, grant=False)
    _billing(db_path, _refund("evt_refund_early"))  # Stripe delivered the refund before the checkout event
    assert _record_build_order(db_path, event)["status"] == "vms_license_not_granted"
    licence = _licence(db_path, customer_id)
    assert (licence["status"], licence["suspended_reason"], licence["stripe_payment_intent_id"]) == ("suspended", "refunded", FIRST_INTENT)
    assert _access(db_path, customer_id) == (False, False)


def test_after_a_refund_a_later_paid_renewal_restores_the_plan_as_before_but_not_the_refunded_licence(shop, db_path, stripe_sessions):
    import billing_status
    customer_id, _ = _order(shop, db_path)
    _billing(db_path, _refund("evt_refund_first"))
    with override_target(sqlite_path=str(db_path)):
        billing_status.payment_recovered(SUB)  # a later invoice of the subscription was paid
    assert _plan(db_path, customer_id)["status"] == "active"
    assert _licence(db_path, customer_id)["suspended_reason"] == "refunded"  # the licence money was returned: terminal
    assert _access(db_path, customer_id) == (True, True)  # the paid plan carries the software use (billing v2), as today


# ------------------------------------------------------------------ later periods

def test_a_refund_of_a_later_period_leaves_the_licence_and_behaves_as_before(shop, db_path, stripe_sessions):
    customer_id, _ = _order(shop, db_path)
    _invoice_paid(db_path, customer_id, "in_ownpc_second", "pi_ownpc_second", "ch_ownpc_second", "2026-11-01T00:00:00", amount=10000)
    result = _billing(db_path, _refund("evt_refund_second", intent="pi_ownpc_second", charge="ch_ownpc_second", amount=10000))
    assert result["status"] == "suspended"  # the current period was refunded: the plan is suspended, as before F1
    assert _plan(db_path, customer_id) == {"status": "suspended", "suspended_reason": "refunded"}
    assert _licence(db_path, customer_id)["status"] == "active"
    assert stripe_sessions == []  # not the first payment: no lookup


def test_a_later_dispute_leaves_the_licence(shop, db_path, stripe_sessions):
    customer_id, _ = _order(shop, db_path)
    _invoice_paid(db_path, customer_id, "in_ownpc_second", "pi_ownpc_second", "ch_ownpc_second", "2026-11-01T00:00:00", amount=10000)
    _billing(db_path, _dispute("evt_dp_second", "created", intent="pi_ownpc_second", charge="ch_ownpc_second"))
    assert _plan(db_path, customer_id)["suspended_reason"] == "disputed" and _licence(db_path, customer_id)["status"] == "active"
    _billing(db_path, _dispute("evt_dp_second_won", "closed", status="won", intent="pi_ownpc_second", charge="ch_ownpc_second"))
    assert _plan(db_path, customer_id)["status"] == "active" and _licence(db_path, customer_id)["status"] == "active"


def test_refunding_the_first_invoice_after_a_later_period_suspends_only_its_licence(shop, db_path, stripe_sessions):
    customer_id, _ = _order(shop, db_path)
    _invoice_paid(db_path, customer_id, "in_ownpc_second", "pi_ownpc_second", "ch_ownpc_second", "2026-11-01T00:00:00", amount=10000)
    result = _billing(db_path, _refund("evt_refund_first_late"))
    assert result["status"] == "suspended" and result["rows"] == 1
    assert _licence(db_path, customer_id)["suspended_reason"] == "refunded"
    assert _plan(db_path, customer_id)["status"] == "active"  # the current period is paid: the plan is unchanged


def test_a_stripe_outage_while_binding_the_licence_retries_the_event(shop, db_path, monkeypatch):
    import stripe_state
    customer_id, _ = _order(shop, db_path)
    inner = stripe_state._stripe_reader()

    def down(path):
        if path.startswith("/v1/checkout/sessions/"):
            raise OSError("Stripe unreachable")
        return inner(path)
    monkeypatch.setattr(stripe_state, "_stripe_reader", lambda: down)
    with pytest.raises(stripe_state.RetryableStripeEventError):
        _billing(db_path, _refund("evt_refund_outage"))
    # Nothing was half-applied: the whole reversal rolled back and Stripe redelivers the event.
    assert _licence(db_path, customer_id)["status"] == "active" and _plan(db_path, customer_id)["status"] == "active"
