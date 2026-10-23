"""Codex HW-REFUND-RACE, PostgreSQL only: the hardware order webhook (hardware_checkout.sync_from_stripe_event) and a full
refund / dispute of the same PaymentIntent (billing_status charge.refunded / charge.dispute.created) on real concurrent
connections under READ COMMITTED. The order always ends refunded / disputed -- never 'paid' -- and the two never
deadlock. Without the fix the window was: the refund's reverse_hardware_orders ran before the order row existed and its
reversal was not committed yet, so the webhook wrote 'paid' and read no reversal.
Requires ANYAICAM_TEST_POSTGRES_URL (a disposable database); skipped otherwise. SQLite: test_hardware_refund_race_127.py.
"""
import json
import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

TEST_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "")
if TEST_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"] = "postgresql"
    os.environ["ANYAICAM_DATABASE_URL"] = TEST_URL
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))

STARTER_PRICE = "price_pg_hw_starter"
ROUNDS = 6


@unittest.skipUnless(TEST_URL, "set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class PostgreSQLHardwareRefundRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from partner_db import initialize_database
        initialize_database()

    def db(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(TEST_URL, row_factory=dict_row, autocommit=True)

    def setUp(self):
        with self.db() as db:
            for statement in ("DELETE FROM hardware_orders WHERE stripe_payment_intent_id LIKE 'pi_pghw%'",
                              "DELETE FROM hardware_checkouts WHERE id LIKE 'hwc-pghw-%'",
                              "DELETE FROM billing_reversals WHERE payment_intent LIKE 'pi_pghw%'",
                              "DELETE FROM payment_locks WHERE key LIKE '%pghw%'",
                              "DELETE FROM customers WHERE id LIKE 'cust-pghw-%'"):
                try:
                    db.execute(statement)
                except Exception:
                    pass  # billing_reversals / payment_locks are created on first use
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('house','House','approved',"
                       "'real','now') ON CONFLICT DO NOTHING")
        self.patches = [
            patch.dict(os.environ, {"ANYAICAM_STRIPE_PRICE_RYZEN_STARTER": STARTER_PRICE}),
            patch("build_orders._line_amounts", return_value={}),
            patch("hardware_checkout.send_confirmation", return_value=None),
            patch("stripe_state.stripe_get", side_effect=self.stripe_get),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    @staticmethod
    def stripe_get(path, what):
        if path.startswith("/v1/invoice_payments?"):
            return {"object": "list", "data": []}  # a one-time payment: no invoice
        if path.startswith("/v1/checkout/sessions?payment_intent="):
            return {"object": "list", "data": []}  # reverse_hardware_orders' lookup for older rows without a PaymentIntent
        raise AssertionError(f"unexpected Stripe read {path}")

    def new_order(self, n):
        ids = {"customer": f"cust-pghw-{n}-{uuid.uuid4().hex[:6]}", "checkout": f"hwc-pghw-{n}-{uuid.uuid4().hex[:6]}",
               "session": f"cs_pghw{n}{uuid.uuid4().hex[:8]}", "intent": f"pi_pghw{n}{uuid.uuid4().hex[:8]}"}
        with self.db() as db:
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(%s,'house','Owner',%s,"
                       "'active','real','now')", (ids["customer"], f"{ids['customer']}@example.test"))
            db.execute("INSERT INTO hardware_checkouts(id,customer_id,email,items,status,created_at,updated_at) "
                       "VALUES(%s,%s,%s,%s,'open','now','now')",
                       (ids["checkout"], ids["customer"], f"{ids['customer']}@example.test",
                        json.dumps([{"sku": "AIC-APPLIANCE-RYZEN-STARTER", "quantity": 1}])))
        return ids

    def webhook(self, ids):
        import hardware_checkout
        metadata = {"anyaicam_build_order": "hardware_only", "anyaicam_hardware_checkout_id": ids["checkout"],
                    "anyaicam_hardware_items": "AIC-APPLIANCE-RYZEN-STARTER:1", "anyaicam_customer_id": ids["customer"]}
        return hardware_checkout.sync_from_stripe_event({"id": f"evt_{ids['session']}", "type": "checkout.session.completed",
                                                         "livemode": False, "data": {"object": {
            "id": ids["session"], "mode": "payment", "payment_status": "paid", "status": "complete", "customer": "cus_pghw",
            "payment_intent": ids["intent"], "customer_details": {"email": f"{ids['customer']}@example.test"},
            "metadata": metadata}}})

    def reversal(self, ids, kind):
        import billing_status
        if kind == "refund":
            event = {"id": f"evt_refund_{ids['intent']}", "type": "charge.refunded", "data": {"object": {
                "object": "charge", "id": f"ch_{ids['intent']}", "payment_intent": ids["intent"], "amount": 124999,
                "amount_refunded": 124999, "refunded": True}}}
        else:
            event = {"id": f"evt_dispute_{ids['intent']}", "type": "charge.dispute.created", "data": {"object": {
                "object": "dispute", "id": f"dp_{ids['intent']}", "charge": f"ch_{ids['intent']}",
                "payment_intent": ids["intent"], "status": "needs_response"}}}
        return billing_status.sync_from_stripe_event(event)

    def statuses(self, ids):
        with self.db() as db:
            return {r["status"] for r in db.execute("SELECT status FROM hardware_orders WHERE stripe_payment_intent_id=%s",
                                                    (ids["intent"],)).fetchall()}

    def run_threads(self, *targets):
        errors = []

        def wrap(name, fn):
            def run():
                try:
                    fn()
                except Exception as exc:  # reported below
                    errors.append(f"{name}: {exc!r}")
            return threading.Thread(target=run, name=name)
        threads = [wrap(name, fn) for name, fn in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "a thread is stuck (deadlock?)")
        self.assertEqual(errors, [])

    def test_the_webhook_waits_for_an_uncommitted_refund_and_records_the_order_reversed(self):
        import billing_status
        for n, kind, want in ((1, "refund", "refunded"), (2, "dispute", "disputed")):
            ids = self.new_order(n)
            holding, release, done = threading.Event(), threading.Event(), {}
            original = billing_status.reverse_hardware_orders

            def paused(intent, reason):
                changed = original(intent, reason)  # no order row yet: nothing to reverse
                if threading.current_thread().name == "reversal":
                    holding.set()                   # reversal recorded, payment lock held, NOT committed
                    release.wait(15)
                return changed
            with patch("billing_status.reverse_hardware_orders", side_effect=paused):
                reversal = threading.Thread(target=lambda: self.reversal(ids, kind), name="reversal")
                reversal.start()
                self.assertTrue(holding.wait(15))
                webhook = threading.Thread(target=lambda: (self.webhook(ids), done.setdefault("at", time.monotonic())),
                                           name="webhook")
                webhook.start()
                time.sleep(1.5)
                self.assertNotIn("at", done, "the webhook must wait for the reversal's payment lock")
                released = time.monotonic()
                release.set()
                reversal.join(30)
                webhook.join(30)
            self.assertGreaterEqual(done["at"], released)
            self.assertEqual(self.statuses(ids), {want}, kind)

    def test_webhook_and_refund_released_together_never_leave_a_paid_order(self):
        for n in range(ROUNDS):
            ids = self.new_order(10 + n)
            barrier = threading.Barrier(2)
            pair = [("webhook", lambda ids=ids: (barrier.wait(10), self.webhook(ids))),
                    ("refund", lambda ids=ids: (barrier.wait(10), self.reversal(ids, "refund")))]
            self.run_threads(*(pair if n % 2 == 0 else reversed(pair)))
            self.assertEqual(self.statuses(ids), {"refunded"}, n)

    def test_a_fulfilment_helper_waits_for_an_uncommitted_refund_and_refuses(self):
        """Codex review of 35801bfa: hardware_fulfillment.mark_shipped / advance_fulfillment_status take the payment lock
        and re-read the reversal in the same transaction as their update."""
        import billing_status
        import hardware_fulfillment
        for n, helper in ((40, "mark_shipped"), (41, "advance_fulfillment_status")):
            ids = self.new_order(n)
            self.webhook(ids)
            with self.db() as db:
                order_id = db.execute("SELECT id FROM hardware_orders WHERE stripe_payment_intent_id=%s",
                                      (ids["intent"],)).fetchone()["id"]
            holding, release, outcome = threading.Event(), threading.Event(), {}
            original = billing_status.reverse_hardware_orders

            def paused(intent, reason):
                changed = original(intent, reason)  # the paid row is reversed, NOT committed yet
                if threading.current_thread().name == "reversal":
                    holding.set()
                    release.wait(15)
                return changed

            def fulfil():
                try:
                    if helper == "mark_shipped":
                        hardware_fulfillment.mark_shipped(order_id, carrier="UPS", tracking_number="1Z999AA10123456784")
                    else:
                        hardware_fulfillment.advance_fulfillment_status(order_id, "preparing")
                    outcome["result"] = "applied"
                except ValueError as refused:
                    outcome["result"] = f"refused: {refused}"
            with patch("billing_status.reverse_hardware_orders", side_effect=paused),                     patch("purchase_notifications.notify_hardware_shipped", return_value=None):
                reversal = threading.Thread(target=lambda: self.reversal(ids, "refund"), name="reversal")
                reversal.start()
                self.assertTrue(holding.wait(15))
                worker = threading.Thread(target=fulfil, name="fulfil")
                worker.start()
                time.sleep(1.5)
                self.assertNotIn("result", outcome, "the helper must wait for the refund's payment lock")
                release.set()
                reversal.join(30)
                worker.join(30)
            self.assertTrue(outcome["result"].startswith("refused"), (helper, outcome))
            with self.db() as db:
                row = db.execute("SELECT status,fulfillment_status FROM hardware_orders WHERE id=%s", (order_id,)).fetchone()
            self.assertEqual(row["status"], "refunded", helper)
            self.assertNotIn(row["fulfillment_status"], ("shipped", "preparing"), helper)

    def test_reconciliation_applies_a_reversal_left_beside_a_paid_row(self):
        import billing_status
        ids = self.new_order(30)
        self.webhook(ids)
        self.assertEqual(self.statuses(ids), {"paid"})
        billing_status._record_reversal(f"ch_{ids['intent']}", ids["intent"], "", "refunded")  # reached no order row
        self.assertEqual(billing_status.reconcile_hardware_reversals(), 1)
        self.assertEqual(self.statuses(ids), {"refunded"})
        self.assertEqual(billing_status.reconcile_hardware_reversals(), 0)


if __name__ == "__main__":
    unittest.main()
