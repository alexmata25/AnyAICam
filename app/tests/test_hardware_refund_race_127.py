"""Codex HW-REFUND-RACE (candidate 4e11e5c9 review, 2026-10-23): a refund or dispute of an appliance payment can never
leave its hardware order 'paid' -- whatever the order of the Stripe events -- and a reversed order can never be
prepared, shipped or delivered, nor count as including the VMS licence.

The hardware webhook (hardware_checkout.sync_from_stripe_event) writes the order rows, re-checks the reversal and saves
the plan in one transaction under the payment lock a refund / dispute takes; billing_status.reconcile_hardware_reversals
applies any reversal on record to a still-'paid' row; fulfilment re-checks under the same lock.
SQLite here (real routes and webhook steps); the PostgreSQL interleaving is test_hardware_refund_race_127_postgresql.py.
"""
import sqlite3
import threading
import time

import pytest
from fastapi import HTTPException

from database_backend import override_target
from test_activation_billing_127 import (_activate_appliance, _billing_event, _call, _dispute, _hardware_order,  # noqa: F401
                                         _plan_row, _refund, _signed_in_customer, one_time_payments)
from test_build_order_billing import stripe_subscriptions  # noqa: F401
from test_hybrid_build_system_flow import db_path, license_portal, package, portal, shop, site, storage  # noqa: F401

pytestmark = pytest.mark.usefixtures("stripe_follows_events")
ADMIN = {"email": "ops@example.test", "role": "global_admin"}


def _orders(db_path, intent):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT id,status,fulfillment_status FROM hardware_orders "
                                              "WHERE stripe_payment_intent_id=? AND sku LIKE 'AIC-APPLIANCE-%'", (intent,))]


def _step(db_path, order_id, status, **extra):
    import fulfillment_admin
    with override_target(sqlite_path=str(db_path)):
        try:
            return fulfillment_admin.apply_step(order_id, fulfillment_admin.FulfillmentRequest(status=status, **extra), ADMIN)
        except HTTPException as error:
            return error


def _includes_licence(db_path, customer_id):
    import customer_entitlements
    return _call(db_path, customer_entitlements.appliance_includes_vms_license, customer_id)


def _stale_reversal(db_path, intent, reason="refunded"):
    """A reversal on record that never reached the order row (what the unserialized race could leave behind)."""
    import billing_status
    with override_target(sqlite_path=str(db_path)):
        with sqlite3.connect(db_path) as conn:
            conn.execute(billing_status.REVERSALS_DDL)
        billing_status._record_reversal(f"ch_{intent}", intent, "", reason)


def test_a_refund_handled_before_the_order_exists_records_the_order_refunded(shop, db_path, stripe_subscriptions,
                                                                             one_time_payments, monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race1@example.test")
    _billing_event(db_path, _refund("pi_race1"))  # charge.refunded first: no hardware order row yet
    _hardware_order(db_path, captured, customer_id, "race1@example.test", monkeypatch, intent="pi_race1")
    orders = _orders(db_path, "pi_race1")
    assert orders and all(o["status"] == "refunded" for o in orders)
    assert _plan_row(db_path, customer_id) is None
    assert not _includes_licence(db_path, customer_id)
    refused = _step(db_path, orders[0]["id"], "preparing")
    assert isinstance(refused, HTTPException) and refused.status_code == 409


def test_a_dispute_before_the_order_is_recorded_then_won_makes_the_order_fulfillable(shop, db_path, stripe_subscriptions,
                                                                                     one_time_payments, monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race2@example.test")
    _billing_event(db_path, _dispute("pi_race2", "created"))
    _hardware_order(db_path, captured, customer_id, "race2@example.test", monkeypatch, intent="pi_race2")
    assert {o["status"] for o in _orders(db_path, "pi_race2")} == {"disputed"}
    assert isinstance(_step(db_path, _orders(db_path, "pi_race2")[0]["id"], "preparing"), HTTPException)
    _billing_event(db_path, _dispute("pi_race2", "closed", status="won", event_id="evt_won_race2"))
    assert {o["status"] for o in _orders(db_path, "pi_race2")} == {"paid"}
    result = _step(db_path, _orders(db_path, "pi_race2")[0]["id"], "preparing")
    assert not isinstance(result, HTTPException) and result["status"] == "applied"
    assert _call(db_path, __import__("billing_status").reconcile_hardware_reversals) == 0  # a won dispute is never applied


def test_the_interleaving_a_webhook_arriving_while_the_refund_holds_the_payment_lock(shop, db_path, stripe_subscriptions,
                                                                                    one_time_payments, monkeypatch):
    """The refund is paused INSIDE its locked transaction (reversal recorded, not committed); the hardware webhook starts
    and must wait, then see the refund and record the order refunded with no plan."""
    import billing_status
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race3@example.test")
    holding, release = threading.Event(), threading.Event()
    original = billing_status.reverse_hardware_orders

    def paused(intent, reason):
        changed = original(intent, reason)  # the refund's own order update ran (no row yet) ...
        if threading.current_thread().name == "refund":
            holding.set()                   # ... and its transaction, holding the payment lock, is not committed yet
            release.wait(10)
        return changed
    monkeypatch.setattr(billing_status, "reverse_hardware_orders", paused)
    errors, done = [], {}

    def refund():
        try:
            with override_target(sqlite_path=str(db_path)):
                billing_status.sync_from_stripe_event(_refund("pi_race3", event_id="evt_refund_race3"))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def webhook():
        try:
            _hardware_order(db_path, captured, customer_id, "race3@example.test", monkeypatch, intent="pi_race3")
            done["webhook"] = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    refund_thread = threading.Thread(target=refund, name="refund")
    refund_thread.start()
    assert holding.wait(10)
    webhook_thread = threading.Thread(target=webhook, name="webhook")
    webhook_thread.start()
    time.sleep(1.5)
    assert "webhook" not in done  # it waits for the refund's transaction
    released = time.monotonic()
    release.set()
    refund_thread.join(30)
    webhook_thread.join(30)
    assert errors == [] and done["webhook"] >= released
    assert {o["status"] for o in _orders(db_path, "pi_race3")} == {"refunded"}
    assert _plan_row(db_path, customer_id) is None
    assert not _includes_licence(db_path, customer_id)


def test_a_refund_after_the_order_reverses_it_and_fulfilment_refuses(shop, db_path, stripe_subscriptions, one_time_payments,
                                                                     monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race4@example.test")
    _hardware_order(db_path, captured, customer_id, "race4@example.test", monkeypatch, intent="pi_race4")
    order_id = _orders(db_path, "pi_race4")[0]["id"]
    assert _step(db_path, order_id, "preparing")["status"] == "applied"
    _billing_event(db_path, _refund("pi_race4", event_id="evt_refund_race4"))
    assert {o["status"] for o in _orders(db_path, "pi_race4")} == {"refunded"}
    refused = _step(db_path, order_id, "shipped", carrier="UPS", tracking_number="1Z999AA10123456784")
    assert isinstance(refused, HTTPException) and refused.status_code == 409
    assert _orders(db_path, "pi_race4")[0]["fulfillment_status"] != "shipped"


@pytest.mark.parametrize("reason", ["refunded", "disputed"])
def test_a_stale_paid_row_with_a_reversal_on_record_is_never_shipped(shop, db_path, stripe_subscriptions, reason, monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, f"race5{reason}@example.test")
    intent = f"pi_race5_{reason}"
    _hardware_order(db_path, captured, customer_id, f"race5{reason}@example.test", monkeypatch, intent=intent)
    _stale_reversal(db_path, intent, reason)  # the row stays 'paid': what the old unserialized race could leave
    order_id = _orders(db_path, intent)[0]["id"]
    refused = _step(db_path, order_id, "shipped", carrier="UPS", tracking_number="1Z999AA10123456784")
    assert isinstance(refused, HTTPException) and refused.status_code == 409
    assert {o["status"] for o in _orders(db_path, intent)} == {reason}  # reconciled
    assert _orders(db_path, intent)[0]["fulfillment_status"] != "shipped"


def test_library_fulfilment_helpers_refuse_a_reversed_payment(shop, db_path, stripe_subscriptions, monkeypatch):
    import hardware_fulfillment
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race6@example.test")
    _hardware_order(db_path, captured, customer_id, "race6@example.test", monkeypatch, intent="pi_race6")
    order_id = _orders(db_path, "pi_race6")[0]["id"]
    _stale_reversal(db_path, "pi_race6")
    with pytest.raises(ValueError):
        _call(db_path, hardware_fulfillment.mark_shipped, order_id, carrier="UPS", tracking_number="1Z999AA10123456784")
    with pytest.raises(ValueError):
        _call(db_path, hardware_fulfillment.advance_fulfillment_status, order_id, "preparing")
    assert {o["status"] for o in _orders(db_path, "pi_race6")} == {"refunded"}
    assert _orders(db_path, "pi_race6")[0]["fulfillment_status"] not in ("shipped", "preparing")


def test_reconciliation_sweep_applies_recorded_reversals_once(shop, db_path, stripe_subscriptions, monkeypatch):
    import billing_status
    client, captured, mail, _, _ = shop
    for n in (7, 8):
        customer_id = _signed_in_customer(client, mail, db_path, f"race{n}@example.test")
        _hardware_order(db_path, captured, customer_id, f"race{n}@example.test", monkeypatch, intent=f"pi_race{n}")
        _stale_reversal(db_path, f"pi_race{n}")
        assert _includes_licence(db_path, customer_id) is False  # a recorded reversal never counts as the included licence
    customer_id = _signed_in_customer(client, mail, db_path, "race9@example.test")
    _hardware_order(db_path, captured, customer_id, "race9@example.test", monkeypatch, intent="pi_race9")
    _stale_reversal(db_path, "pi_race9", "dispute_won")  # a won dispute: not a reversal
    first = _call(db_path, billing_status.reconcile_hardware_reversals)
    assert first == 0  # the licence check above already reconciled 7 and 8
    for n in (7, 8):
        assert {o["status"] for o in _orders(db_path, f"pi_race{n}")} == {"refunded"}
    assert {o["status"] for o in _orders(db_path, "pi_race9")} == {"paid"}
    assert _includes_licence(db_path, customer_id) is True


def test_reconciliation_sweep_finds_rows_nothing_else_touched(shop, db_path, stripe_subscriptions, monkeypatch):
    import billing_status
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race10@example.test")
    _hardware_order(db_path, captured, customer_id, "race10@example.test", monkeypatch, intent="pi_race10")
    _stale_reversal(db_path, "pi_race10")
    assert _call(db_path, billing_status.reconcile_hardware_reversals) == len(_orders(db_path, "pi_race10")) >= 1
    assert {o["status"] for o in _orders(db_path, "pi_race10")} == {"refunded"}
    assert _call(db_path, billing_status.reconcile_hardware_reversals) == 0  # idempotent


def test_a_paid_order_without_any_reversal_is_unchanged(shop, db_path, stripe_subscriptions, monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race11@example.test")
    _hardware_order(db_path, captured, customer_id, "race11@example.test", monkeypatch, intent="pi_race11")
    assert {o["status"] for o in _orders(db_path, "pi_race11")} == {"paid"}
    assert _plan_row(db_path, customer_id)["state"] == "awaiting_activation"
    assert _includes_licence(db_path, customer_id) is True
    order_id = _orders(db_path, "pi_race11")[0]["id"]
    for step, extra in (("preparing", {}), ("shipped", {"carrier": "UPS", "tracking_number": "1Z999AA10123456784"}),
                        ("delivered", {})):
        assert _step(db_path, order_id, step, **extra)["status"] == "applied"


# ------------------------------------------------------------------ Codex HW-REFUND-RACE-FULFILLMENT (review of 35801bfa)

@pytest.mark.parametrize("helper", ["mark_shipped", "advance_fulfillment_status"])
def test_a_fulfilment_helper_waits_for_a_refund_holding_the_lock_and_then_refuses(shop, db_path, stripe_subscriptions,
                                                                                one_time_payments, monkeypatch, helper):
    """The refund of an already-recorded appliance payment is paused INSIDE its locked transaction (orders reversed, not
    committed). The library fulfilment helper started meanwhile must not ship / advance the order: it waits for the
    payment lock, re-reads the reversal in the same transaction as its update, and refuses."""
    import billing_status
    import hardware_fulfillment
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, f"race12{helper}@example.test")
    intent = f"pi_race12_{helper}"
    _hardware_order(db_path, captured, customer_id, f"race12{helper}@example.test", monkeypatch, intent=intent)
    order_id = _orders(db_path, intent)[0]["id"]
    holding, release = threading.Event(), threading.Event()
    original = billing_status.reverse_hardware_orders

    def paused(pi, reason):
        changed = original(pi, reason)
        if threading.current_thread().name == "refund":
            holding.set()
            release.wait(10)
        return changed
    monkeypatch.setattr(billing_status, "reverse_hardware_orders", paused)
    outcome, errors = {}, []

    def refund():
        try:
            with override_target(sqlite_path=str(db_path)):
                billing_status.sync_from_stripe_event(_refund(intent, event_id=f"evt_refund_{intent}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def fulfil():
        try:
            with override_target(sqlite_path=str(db_path)):
                if helper == "mark_shipped":
                    hardware_fulfillment.mark_shipped(order_id, carrier="UPS", tracking_number="1Z999AA10123456784")
                else:
                    hardware_fulfillment.advance_fulfillment_status(order_id, "preparing")
            outcome["result"] = "applied"
        except ValueError as refused:
            outcome["result"] = f"refused: {refused}"
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    refund_thread = threading.Thread(target=refund, name="refund")
    refund_thread.start()
    assert holding.wait(10)
    fulfil_thread = threading.Thread(target=fulfil, name="fulfil")
    fulfil_thread.start()
    time.sleep(1.0)
    release.set()
    refund_thread.join(30)
    fulfil_thread.join(30)
    assert errors == []
    assert outcome["result"].startswith("refused"), outcome
    order = _orders(db_path, intent)[0]
    assert order["status"] == "refunded" and order["fulfillment_status"] not in ("shipped", "preparing"), order


def test_library_helpers_still_fulfil_and_cancel_normal_paid_orders(shop, db_path, stripe_subscriptions, monkeypatch):
    """The locked helpers change nothing for a paid, unreversed order; cancellation stays possible (also when refunded)."""
    import hardware_fulfillment
    import purchase_notifications
    monkeypatch.setattr(purchase_notifications, "notify_hardware_shipped", lambda order_id: None)
    monkeypatch.setattr(purchase_notifications, "notify_hardware_cancellation", lambda order_id: None)
    client, captured, mail, _, _ = shop
    customer_id = _signed_in_customer(client, mail, db_path, "race13@example.test")
    _hardware_order(db_path, captured, customer_id, "race13@example.test", monkeypatch, intent="pi_race13")
    order_id = _orders(db_path, "pi_race13")[0]["id"]
    assert _call(db_path, hardware_fulfillment.advance_fulfillment_status, order_id, "preparing")["fulfillment_status"] == "preparing"
    shipped = _call(db_path, hardware_fulfillment.mark_shipped, order_id, carrier="UPS", tracking_number="1Z999AA10123456784")
    assert shipped["fulfillment_status"] == "shipped" and shipped["tracking_number"] == "1Z999AA10123456784"
    assert _call(db_path, hardware_fulfillment.advance_fulfillment_status, order_id, "delivered")["fulfillment_status"] == "delivered"
    customer_id = _signed_in_customer(client, mail, db_path, "race14@example.test")
    _hardware_order(db_path, captured, customer_id, "race14@example.test", monkeypatch, intent="pi_race14")
    paid_order = _orders(db_path, "pi_race14")[0]["id"]
    assert _call(db_path, hardware_fulfillment.mark_cancelled, paid_order)["fulfillment_status"] == "cancelled"
    customer_id = _signed_in_customer(client, mail, db_path, "race15@example.test")
    _hardware_order(db_path, captured, customer_id, "race15@example.test", monkeypatch, intent="pi_race15")
    refunded_order = _orders(db_path, "pi_race15")[0]["id"]
    _stale_reversal(db_path, "pi_race15")
    assert _call(db_path, hardware_fulfillment.mark_cancelled, refunded_order)["fulfillment_status"] == "cancelled"
