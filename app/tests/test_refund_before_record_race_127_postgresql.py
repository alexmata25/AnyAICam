"""RC10b, PostgreSQL only: an own-PC licence grant (build_orders._grant_vms_license) and a full refund of the same first
invoice (billing_status charge.refunded) racing on real concurrent connections under READ COMMITTED -- the licence ends
suspended in every interleaving, and the two never deadlock (billing_status.lock_payment takes charge, PaymentIntent and
invoice keys in one global order). Requires ANYAICAM_TEST_POSTGRES_URL (a disposable database); skipped otherwise.
The SQLite counterparts are in test_refund_before_record_race_127.py.
"""
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "")
if TEST_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"] = "postgresql"
    os.environ["ANYAICAM_DATABASE_URL"] = TEST_URL
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))

LICENCE_PRICE = "price_pg_licence_8"
AMOUNT = 52499
ROUNDS = 6


def _ids(n):
    return {"session": f"cs_pg_race{n}", "invoice": f"in_pg_race{n}", "intent": f"pi_pg_race{n}", "charge": f"ch_pg_race{n}",
            "sub": f"sub_pg_race{n}", "customer": f"cust-pg-race-{n}", "stripe_customer": f"cus_pg_race{n}"}


@unittest.skipUnless(TEST_URL, "set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class PostgreSQLRefundGrantRaceTests(unittest.TestCase):
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
            for statement in ("DELETE FROM customer_entitlements WHERE customer_id LIKE 'cust-pg-race-%'",
                              "DELETE FROM billing_reversals WHERE payment_intent LIKE 'pi_pg_race%'",
                              "DELETE FROM payment_locks WHERE key LIKE '%pg_race%'",
                              "DELETE FROM customers WHERE id LIKE 'cust-pg-race-%'"):
                try:
                    db.execute(statement)
                except Exception:
                    pass  # billing_reversals is created on first use
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('house','House','approved',"
                       "'real','now') ON CONFLICT DO NOTHING")
            for n in range(ROUNDS + 2):
                ids = _ids(n)
                db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(%s,'house','Owner',%s,"
                           "'active','real','now')", (ids["customer"], f"race{n}@example.test"))
        self.hooks = {}
        self.payment_at_grant = True
        self.patches = [patch.dict(os.environ, {"ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8": LICENCE_PRICE}),
                        patch("stripe_state._stripe_reader", return_value=self.reader)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def reader(self, path):
        for prefix, hook in list(self.hooks.items()):
            if path.startswith(prefix):
                hook(path)
        n = int("".join(ch for ch in path.split("pg_race", 1)[1][:4] if ch.isdigit())) if "pg_race" in path else -1
        ids = _ids(n)
        if path.startswith("/v1/invoice_payments?"):
            return {"object": "list", "data": [{"invoice": ids["invoice"], "status": "paid",
                                                "payment": {"type": "payment_intent", "payment_intent": ids["intent"]}}]}
        if path.startswith("/v1/invoices/"):
            if not self.payment_at_grant and threading.current_thread().name.startswith("grant"):
                raise OSError("Stripe unreachable")
            return {"id": ids["invoice"], "object": "invoice", "status": "paid", "amount_paid": AMOUNT,
                    "customer": ids["stripe_customer"], "subscription": ids["sub"], "payment_intent": ids["intent"],
                    "charge": ids["charge"]}
        if path.startswith("/v1/checkout/sessions/"):
            return {"id": ids["session"], "object": "checkout.session", "subscription": ids["sub"], "invoice": ids["invoice"]}
        raise AssertionError(f"unexpected Stripe read {path}")

    def grant(self, n):
        import build_orders
        ids = _ids(n)
        session = {"id": ids["session"], "customer": ids["stripe_customer"], "invoice": ids["invoice"],
                   "subscription": ids["sub"], "payment_intent": None}
        return build_orders._grant_vms_license(session, ids["customer"], {"anyaicam_vms_license_price_id": LICENCE_PRICE,
                                                                          "anyaicam_vms_license_capacity": "8"})

    def refund(self, n):
        import billing_status
        ids = _ids(n)
        return billing_status.sync_from_stripe_event({"id": f"evt_pg_refund_{n}", "type": "charge.refunded", "data": {"object": {
            "id": ids["charge"], "object": "charge", "payment_intent": ids["intent"], "amount": AMOUNT,
            "amount_refunded": AMOUNT, "refunded": True}}})

    def licence(self, n):
        with self.db() as db:
            return db.execute("SELECT status,suspended_reason FROM customer_entitlements WHERE customer_id=%s "
                              "AND product='vms_license'", (_ids(n)["customer"],)).fetchone()

    def run_threads(self, *targets):
        results, errors = {}, []

        def wrap(name, fn):
            def run():
                try:
                    results[name] = fn()
                except Exception as exc:  # reported below
                    errors.append(repr(exc))
            return threading.Thread(target=run, name=name)
        threads = [wrap(name, fn) for name, fn in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        return results

    def test_grant_and_refund_released_together_always_end_suspended(self):
        for n in range(ROUNDS):
            self.payment_at_grant = n % 2 == 0
            barrier = threading.Barrier(2)
            pair = [("grant", lambda n=n: (barrier.wait(10), self.grant(n))[1]),
                    ("refund", lambda n=n: (barrier.wait(10), self.refund(n))[1])]
            self.run_threads(*(pair if n % 4 < 2 else reversed(pair)))
            self.assertEqual(dict(self.licence(n)), {"status": "suspended", "suspended_reason": "refunded"}, n)

    def test_a_grant_started_while_the_refund_holds_the_lock_waits_and_lands_suspended(self):
        for n, at_grant in ((ROUNDS, True), (ROUNDS + 1, False)):
            self.payment_at_grant = at_grant
            in_lookup, grant_started = threading.Event(), threading.Event()

            def hold(path):
                if threading.current_thread().name == "refund":
                    in_lookup.set()
                    grant_started.wait(10)
                    time.sleep(0.5)
            self.hooks = {"/v1/invoice_payments?payment": hold}

            def grant_later(n=n):
                in_lookup.wait(10)
                grant_started.set()
                return self.grant(n)
            results = self.run_threads(("refund", lambda n=n: self.refund(n)), ("grant", grant_later))
            # With its PaymentIntent the grant waits for the refund (same keys) and is granted suspended; without it the
            # grant may commit first (only the invoice key, which the refund takes after its Stripe lookup) and the
            # refund then binds and suspends it. Either way the licence ends suspended.
            if at_grant:
                self.assertEqual(results["grant"]["status"], "vms_license_not_granted")
            self.assertEqual(dict(self.licence(n)), {"status": "suspended", "suspended_reason": "refunded"})
            self.hooks = {}


if __name__ == "__main__":
    unittest.main()
