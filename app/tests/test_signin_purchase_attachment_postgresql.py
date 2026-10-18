"""PostgreSQL-only race and atomicity tests for the sign-in purchase attachment protocol (Green 2026-10-18, post-RC5;
requested by Codex before integration review). Requires ANYAICAM_TEST_POSTGRES_URL (a disposable database).

Real concurrent workers (threads released together by a barrier) against PostgreSQL 16 under its default READ
COMMITTED isolation: a competing conditional UPDATE blocks on the row lock, re-evaluates its WHERE clause after the
winner commits, and changes no row.
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

TEST_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "")
if TEST_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"] = "postgresql"
    os.environ["ANYAICAM_DATABASE_URL"] = TEST_URL
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))

import psycopg
from psycopg.rows import dict_row

EMAIL = "owner@example.test"
CUSTOMER = "cust-pg-owner"
WORKERS = 8


@unittest.skipUnless(TEST_URL, "set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class PostgreSQLSigninAttachmentRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from partner_db import initialize_database
        initialize_database()

    def db(self):
        return psycopg.connect(TEST_URL, row_factory=dict_row, autocommit=True)

    def setUp(self):
        with self.db() as db:
            db.execute("TRUNCATE pending_customer_links, purchase_attachments, customer_entitlements, direct_signups, "
                       "audit_logs, provisioning_notifications RESTART IDENTITY CASCADE")
            db.execute("DELETE FROM partner_users WHERE customer_id=%s", (CUSTOMER,))
            db.execute("DELETE FROM customers WHERE id=%s", (CUSTOMER,))
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('house','House','approved',"
                       "'real','now') ON CONFLICT DO NOTHING")
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(%s,'house','Owner',%s,"
                       "'active','real','now')", (CUSTOMER, EMAIL))
            db.execute("INSERT INTO direct_signups(id,email,name,password_hash,token_hash,expires_at,created_at,verified_at,"
                       "customer_id) VALUES('ds-1',%s,'Owner','x','tok-hash-1','2999-01-01',%s,%s,%s)",
                       (EMAIL, datetime.now().isoformat(), datetime.now().isoformat(), CUSTOMER))
        import customer_entitlements as ce
        ce.create_pending_link(email=EMAIL, product="professional", camera_slot_quantity=10, raw_event={"id": "evt_pg"})
        with self.db() as db:
            self.link_id = db.execute("SELECT id FROM pending_customer_links").fetchone()["id"]
        self.mail = patch("purchase_notifications.get_email_service",
                          return_value=type("M", (), {"send": lambda self, *a, **k: {"status": "sent"}})())
        self.mail.start()

    def tearDown(self):
        self.mail.stop()

    def counts(self):
        with self.db() as db:
            return {
                "entitlements": db.execute("SELECT COUNT(*) AS n FROM customer_entitlements WHERE customer_id=%s",
                                           (CUSTOMER,)).fetchone()["n"],
                "attachments": db.execute("SELECT COUNT(*) AS n FROM purchase_attachments").fetchone()["n"],
                "audits": db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='signin.purchase_attached'").fetchone()["n"],
                "link": db.execute("SELECT status FROM pending_customer_links").fetchone()["status"],
            }

    def run_together(self, target):
        barrier, results, errors = threading.Barrier(WORKERS), [], []

        def worker():
            try:
                barrier.wait(timeout=10)
                results.append(target())
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))
        threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        return results

    def user(self):
        return {"role": "customer_owner", "customer_id": CUSTOMER, "email": EMAIL, "approved": 1, "account_status": "active"}

    # ---------------------------------------------------------------- competing workers

    def test_parallel_claims_exactly_one_worker_wins(self):
        import signin_purchase_attachment as spa
        tokens = self.run_together(lambda: spa.claim("pending_customer_links", self.link_id, EMAIL))
        self.assertEqual(len([t for t in tokens if t]), 1)

    def test_parallel_sign_ins_attach_exactly_once(self):
        import signin_purchase_attachment as spa
        results = self.run_together(lambda: spa.attach_after_signin(self.user()))
        self.assertEqual(sum(len(r.get("attached", [])) for r in results), 1)
        self.assertEqual(self.counts(), {"entitlements": 1, "attachments": 1, "audits": 1, "link": "resolved"})

    # ---------------------------------------------------------------- expired lease, fencing, replay

    def test_lease_takeover_fences_out_the_stalled_worker(self):
        import signin_purchase_attachment as spa
        stale = datetime.now() - timedelta(seconds=spa.CLAIM_LEASE_SECONDS + 30)
        token_a = spa.claim("pending_customer_links", self.link_id, EMAIL, now=stale)
        token_b = spa.claim("pending_customer_links", self.link_id, EMAIL)
        self.assertTrue(token_a and token_b and token_a != token_b)
        with self.assertRaises(spa.FenceLost):
            spa.finalize("plan", "pending_customer_links", self.link_id, token_a, customer_id=CUSTOMER, email=EMAIL,
                         label="camera plan")
        self.assertEqual(self.counts()["attachments"], 0)
        spa.finalize("plan", "pending_customer_links", self.link_id, token_b, customer_id=CUSTOMER, email=EMAIL,
                     label="camera plan")
        with self.assertRaises(spa.FenceLost):  # replay of the winner
            spa.finalize("plan", "pending_customer_links", self.link_id, token_b, customer_id=CUSTOMER, email=EMAIL,
                         label="camera plan")
        self.assertEqual(self.counts(), {"entitlements": 1, "attachments": 1, "audits": 1, "link": "resolved"})

    def test_parallel_finalize_with_the_same_token_grants_once(self):
        import signin_purchase_attachment as spa
        token = spa.claim("pending_customer_links", self.link_id, EMAIL)

        def finalize():
            try:
                return spa.finalize("plan", "pending_customer_links", self.link_id, token, customer_id=CUSTOMER,
                                    email=EMAIL, label="camera plan")
            except spa.FenceLost:
                return None
        results = self.run_together(finalize)
        self.assertEqual(len([r for r in results if r]), 1)
        self.assertEqual(self.counts(), {"entitlements": 1, "attachments": 1, "audits": 1, "link": "resolved"})

    # ---------------------------------------------------------------- atomic transition + grant + audit

    def test_an_audit_failure_rolls_back_the_transition_and_the_grant(self):
        import signin_purchase_attachment as spa
        token = spa.claim("pending_customer_links", self.link_id, EMAIL)
        with patch.object(spa, "audit", side_effect=RuntimeError("audit store unavailable")):
            with self.assertRaises(RuntimeError):
                spa.finalize("plan", "pending_customer_links", self.link_id, token, customer_id=CUSTOMER, email=EMAIL,
                             label="camera plan")
        self.assertEqual(self.counts(), {"entitlements": 0, "attachments": 0, "audits": 0, "link": "attaching"})
        spa.finalize("plan", "pending_customer_links", self.link_id, token, customer_id=CUSTOMER, email=EMAIL,
                     label="camera plan")  # same live claim completes once the audit store is back
        self.assertEqual(self.counts(), {"entitlements": 1, "attachments": 1, "audits": 1, "link": "resolved"})

    # ---------------------------------------------------------------- unauthorized attachment

    def test_an_unconfirmed_account_and_another_address_attach_nothing(self):
        import signin_purchase_attachment as spa
        with self.db() as db:
            db.execute("DELETE FROM direct_signups")  # the address was never confirmed
        self.assertEqual(spa.attach_after_signin(self.user())["reason"], "the account's address was never confirmed")
        other = dict(self.user(), email="intruder@example.test")
        self.assertEqual(spa.attach_after_signin(other)["status"], "skipped")
        self.assertIsNone(spa.claim("pending_customer_links", self.link_id, "intruder@example.test"))
        self.assertEqual(self.counts(), {"entitlements": 0, "attachments": 0, "audits": 0, "link": "pending"})


if __name__ == "__main__":
    unittest.main()
