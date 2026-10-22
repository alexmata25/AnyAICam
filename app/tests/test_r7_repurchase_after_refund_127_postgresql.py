"""R7, PostgreSQL only: starting the new order after a refunded and ended own-PC order
(per_camera_billing.supersede_refunded_order) from concurrent requests on real connections under READ COMMITTED
supersedes the paid order exactly once -- one new pending order, one audit copy of the old order -- and never touches an
account whose order still grants its licence. Requires ANYAICAM_TEST_POSTGRES_URL (a disposable database); skipped
otherwise. The SQLite and route-level tests are in test_r7_repurchase_after_refund_127.py.
"""
import json
import os
import sys
import threading
import unittest
from pathlib import Path

TEST_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "")
if TEST_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"] = "postgresql"
    os.environ["ANYAICAM_DATABASE_URL"] = TEST_URL
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))

ROUNDS = 5
THREADS = 4


@unittest.skipUnless(TEST_URL, "set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class PostgreSQLRepurchaseTests(unittest.TestCase):
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
            for statement in ("DELETE FROM audit_logs WHERE entity_id LIKE 'cust-pg-r7-%'",
                              "DELETE FROM build_system_intents WHERE customer_id LIKE 'cust-pg-r7-%'",
                              "DELETE FROM camera_plan_entitlements_v2 WHERE customer_id LIKE 'cust-pg-r7-%'",
                              "DELETE FROM customer_entitlements WHERE customer_id LIKE 'cust-pg-r7-%'",
                              "DELETE FROM customers WHERE id LIKE 'cust-pg-r7-%'"):
                db.execute(statement)
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('house','House','approved',"
                       "'real','now') ON CONFLICT DO NOTHING")

    def _account(self, n, *, licence_status="suspended", reason="refunded", plan_status="cancelled"):
        customer = f"cust-pg-r7-{n}"
        with self.db() as db:
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(%s,'house','Owner',%s,"
                       "'active','real','now')", (customer, f"pg-r7-{n}@example.test"))
            db.execute("INSERT INTO build_system_intents(customer_id,plan_key,camera_quantity,own_pc,state,created_at,updated_at,"
                       "paid_at) VALUES(%s,'ai_local',5,1,'paid_setup_pending','2026-10-20T10:00:00','2026-10-20T10:05:00',"
                       "'2026-10-20T10:05:00')", (customer,))
            db.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_customer_id,"
                       "stripe_checkout_session_id,stripe_payment_intent_id,suspended_reason,created_at,updated_at) "
                       "VALUES(%s,%s,'vms_license',8,%s,%s,%s,%s,%s,'now','now')",
                       (f"lic-pg-r7-{n}", customer, licence_status, f"cus_pg_r7_{n}", f"cs_pg_r7_{n}", f"pi_pg_r7_{n}",
                        reason if licence_status == "suspended" else None))
            db.execute("INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,"
                       "stripe_subscription_id,stripe_subscription_item_id,stripe_price_id,status,stripe_status,suspended_reason,"
                       "local_retention_days,created_at,updated_at) VALUES(%s,%s,'ai_local',0,%s,%s,'si','price_ai',%s,'canceled',"
                       "%s,30,'now','now')", (f"plan-pg-r7-{n}", customer, f"cus_pg_r7_{n}", f"sub_pg_r7_{n}", plan_status,
                                              reason))
        return customer

    def _race(self, customer):
        import per_camera_billing as billing
        barrier, results = threading.Barrier(THREADS), []

        def run():
            barrier.wait()
            results.append(billing.supersede_refunded_order(customer))
        threads = [threading.Thread(target=run) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return results

    def _state(self, customer):
        with self.db() as db:
            intent = db.execute("SELECT * FROM build_system_intents WHERE customer_id=%s", (customer,)).fetchone()
            audits = db.execute("SELECT details_json FROM audit_logs WHERE action='build_order.superseded_after_refund' "
                                "AND entity_id=%s", (customer,)).fetchall()
            licence = db.execute("SELECT status,suspended_reason,stripe_payment_intent_id FROM customer_entitlements "
                                 "WHERE customer_id=%s", (customer,)).fetchone()
        return intent, [json.loads(item["details_json"]) for item in audits], licence

    def test_concurrent_new_order_requests_supersede_the_refunded_order_exactly_once(self):
        for n in range(ROUNDS):
            customer = self._account(n)
            results = self._race(customer)
            intent, audits, licence = self._state(customer)
            self.assertEqual(sorted(results), [False] * (THREADS - 1) + [True], n)
            self.assertEqual((intent["state"], intent["own_pc"], intent["paid_at"]), ("pending_checkout", 1, None))
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]["previous_order"]["state"], "paid_setup_pending")
            self.assertEqual(audits[0]["licence"]["stripe_payment_intent_id"], f"pi_pg_r7_{n}")
            self.assertEqual(audits[0]["plan"]["stripe_subscription_id"], f"sub_pg_r7_{n}")
            self.assertEqual(dict(licence), {"status": "suspended", "suspended_reason": "refunded",
                                             "stripe_payment_intent_id": f"pi_pg_r7_{n}"})

    def test_an_order_that_still_grants_its_licence_is_never_superseded(self):
        for n, kwargs in enumerate(({"licence_status": "active"}, {"reason": "disputed"},
                                    {"plan_status": "suspended"}), start=100):
            customer = self._account(n, **kwargs)
            self.assertEqual(self._race(customer), [False] * THREADS)
            intent, audits, _ = self._state(customer)
            self.assertEqual((intent["state"], audits), ("paid_setup_pending", []))


if __name__ == "__main__":
    unittest.main()
