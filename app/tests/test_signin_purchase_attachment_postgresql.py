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


    # ---------------------------------------------------------------- refund / dispute before attachment (Codex 7080e193)

    def _reversed_link(self, reason, suffix):
        import billing_status
        import customer_entitlements as ce
        with self.db() as db:
            db.execute("DELETE FROM pending_customer_links")
        event = {"id": f"evt-{suffix}", "data": {"object": {"payment_intent": f"pi_{suffix}", "charge": f"ch_{suffix}"}}}
        link = ce.create_pending_link(email=EMAIL, product="professional", camera_slot_quantity=10, raw_event=event)
        billing_status.record_reversal(f"ch_{suffix}", f"pi_{suffix}", "", reason)
        return link["id"]

    def test_a_refund_or_dispute_recorded_before_attachment_grants_nothing(self):
        import signin_purchase_attachment as spa
        for reason in ("refunded", "disputed"):
            with self.subTest(reason=reason):
                link_id = self._reversed_link(reason, f"{reason}-pg")
                token = spa.claim("pending_customer_links", link_id, EMAIL)
                self.assertTrue(token)
                self.assertIsNone(spa.finalize("plan", "pending_customer_links", link_id, token, customer_id=CUSTOMER,
                                               email=EMAIL, label="camera plan"))
                c = self.counts()
                self.assertEqual((c["entitlements"], c["attachments"], c["audits"], c["link"]), (0, 0, 0, "reversed"))

    def test_parallel_sign_ins_never_attach_a_refunded_purchase(self):
        import signin_purchase_attachment as spa
        self._reversed_link("refunded", "refunded-race")
        results = self.run_together(lambda: spa.attach_after_signin(self.user()))
        self.assertEqual(sum(len(r.get("attached", [])) for r in results), 0)
        c = self.counts()
        self.assertEqual((c["entitlements"], c["attachments"], c["audits"], c["link"]), (0, 0, 0, "reversed"))

    def test_an_attached_purchase_keeps_its_stripe_references_for_a_later_refund(self):
        import customer_entitlements as ce
        import signin_purchase_attachment as spa
        with self.db() as db:
            db.execute("DELETE FROM pending_customer_links")
        event = {"id": "evt-refs-pg", "data": {"object": {"payment_intent": "pi_refs_pg", "subscription": "sub_refs_pg"}}}
        link = ce.create_pending_link(email=EMAIL, product="professional", camera_slot_quantity=10, raw_event=event)
        token = spa.claim("pending_customer_links", link["id"], EMAIL)
        self.assertTrue(spa.finalize("plan", "pending_customer_links", link["id"], token, customer_id=CUSTOMER,
                                     email=EMAIL, label="camera plan"))
        with self.db() as db:
            row = db.execute("SELECT stripe_payment_intent_id,stripe_subscription_id FROM customer_entitlements "
                             "WHERE customer_id=%s AND product='professional'", (CUSTOMER,)).fetchone()
        self.assertEqual((row["stripe_payment_intent_id"], row["stripe_subscription_id"]), ("pi_refs_pg", "sub_refs_pg"))

    # ---------------------------------------------------------------- refund vs attachment interleavings (Codex, P2)

    def _paid_link(self, suffix):
        import customer_entitlements as ce
        with self.db() as db:
            db.execute("DELETE FROM pending_customer_links")
        event = {"id": f"evt-{suffix}", "data": {"object": {"payment_intent": f"pi_{suffix}", "charge": f"ch_{suffix}"}}}
        return ce.create_pending_link(email=EMAIL, product="professional", camera_slot_quantity=10, raw_event=event)["id"]

    @staticmethod
    def _charge_event(kind, suffix, **extra):
        obj = {"id": f"ch_{suffix}", "object": "charge", "payment_intent": f"pi_{suffix}", "amount": 1000,
               "amount_refunded": 1000, "refunded": True}
        if kind != "charge.refunded":
            obj = {"id": f"dp_{suffix}", "object": "dispute", "charge": f"ch_{suffix}", "payment_intent": f"pi_{suffix}", **extra}
        return {"id": f"evt-{kind}-{suffix}", "type": kind, "data": {"object": obj}}

    def _no_stripe(self):
        import billing_status
        import sales_commissions
        return (patch.object(billing_status, "_stripe_invoice_subscription", return_value=None),
                patch.object(sales_commissions, "_invoice_for_payment_intent", return_value=None))

    def _entitlement_status(self):
        with self.db() as db:
            found = db.execute("SELECT status FROM customer_entitlements WHERE customer_id=%s AND product='professional'",
                               (CUSTOMER,)).fetchone()
        return found["status"] if found else None

    def test_a_refund_arriving_while_attachment_is_granting_never_leaves_an_active_entitlement(self):
        """Forced ordering from the Codex review: finalize has passed its reversal check and is granting; the refund
        webhook runs at that moment. With the payment lock the refund waits, then finds and suspends the entitlement."""
        import billing_status
        import signin_purchase_attachment as spa
        link_id = self._paid_link("race-grant")
        token = spa.claim("pending_customer_links", link_id, EMAIL)
        granting, release, outcome = threading.Event(), threading.Event(), {}
        real_grant = spa._grant

        def slow_grant(*args, **kwargs):
            real_grant(*args, **kwargs)
            granting.set()
            release.wait(timeout=20)
        a, b = self._no_stripe()
        with a, b, patch.object(spa, "_grant", side_effect=slow_grant):
            attach = threading.Thread(target=lambda: outcome.setdefault("attached", spa.finalize(
                "plan", "pending_customer_links", link_id, token, customer_id=CUSTOMER, email=EMAIL, label="camera plan")))
            attach.start()
            self.assertTrue(granting.wait(timeout=20))
            refund = threading.Thread(target=lambda: outcome.setdefault(
                "refund", billing_status.sync_from_stripe_event(self._charge_event("charge.refunded", "race-grant"))))
            refund.start()
            refund.join(timeout=2)
            self.assertTrue(refund.is_alive(), "the refund must wait for the attachment's payment lock")
            release.set()
            attach.join(timeout=30)
            refund.join(timeout=30)
        self.assertTrue(outcome["attached"])
        self.assertEqual(outcome["refund"]["status"], "suspended")
        self.assertEqual(self._entitlement_status(), "suspended")

    def test_a_refund_being_recorded_when_attachment_starts_makes_attachment_grant_nothing(self):
        """The other ordering: the refund holds the payment lock; attachment waits, then sees the refund."""
        import billing_status
        import signin_purchase_attachment as spa
        link_id = self._paid_link("race-refund")
        token = spa.claim("pending_customer_links", link_id, EMAIL)
        recording, release, outcome = threading.Event(), threading.Event(), {}
        real_record = billing_status._record_reversal

        def slow_record(*args, **kwargs):
            real_record(*args, **kwargs)
            recording.set()
            release.wait(timeout=20)
        a, b = self._no_stripe()
        with a, b, patch.object(billing_status, "_record_reversal", side_effect=slow_record):
            refund = threading.Thread(target=lambda: outcome.setdefault(
                "refund", billing_status.sync_from_stripe_event(self._charge_event("charge.refunded", "race-refund"))))
            refund.start()
            self.assertTrue(recording.wait(timeout=20))
            attach = threading.Thread(target=lambda: outcome.setdefault("attached", spa.finalize(
                "plan", "pending_customer_links", link_id, token, customer_id=CUSTOMER, email=EMAIL, label="camera plan")))
            attach.start()
            attach.join(timeout=2)
            self.assertTrue(attach.is_alive(), "attachment must wait for the refund's payment lock")
            release.set()
            refund.join(timeout=30)
            attach.join(timeout=30)
        self.assertIsNone(outcome["attached"])
        self.assertIsNone(self._entitlement_status())
        c = self.counts()
        self.assertEqual((c["attachments"], c["audits"], c["link"]), (0, 0, "reversed"))

    # ---------------------------------------------------------------- dispute won after attachment set it aside

    def test_a_won_dispute_reinstates_the_pending_purchase_and_the_next_sign_in_attaches_it(self):
        import billing_status
        import signin_purchase_attachment as spa
        link_id = self._paid_link("dispute-won")
        a, b = self._no_stripe()
        with a, b:
            billing_status.sync_from_stripe_event(self._charge_event("charge.dispute.created", "dispute-won"))
            self.assertEqual(spa.attach_after_signin(self.user())["status"], "nothing to attach")
            self.assertEqual(self.counts()["link"], "reversed")
            closed = billing_status.sync_from_stripe_event(
                self._charge_event("charge.dispute.closed", "dispute-won", status="won"))
        self.assertEqual(closed["pending_reinstated"], 1)
        self.assertEqual(self.counts()["link"], "pending")
        self.assertEqual(len(spa.attach_after_signin(self.user())["attached"]), 1)
        self.assertEqual(self.counts(), {"entitlements": 1, "attachments": 1, "audits": 1, "link": "resolved"})
        self.assertEqual(self._entitlement_status(), "active")

    def test_a_lost_dispute_and_a_refund_stay_terminal(self):
        import billing_status
        import signin_purchase_attachment as spa
        for first, suffix in (("charge.dispute.created", "dispute-lost"), ("charge.refunded", "refund-terminal")):
            with self.subTest(first=first):
                self._paid_link(suffix)
                a, b = self._no_stripe()
                with a, b:
                    billing_status.sync_from_stripe_event(self._charge_event(first, suffix))
                    spa.attach_after_signin(self.user())
                    status = "lost" if first == "charge.dispute.created" else "won"
                    billing_status.sync_from_stripe_event(self._charge_event("charge.dispute.closed", suffix, status=status))
                self.assertEqual(self.counts()["link"], "reversed")
                self.assertIsNone(self._entitlement_status())


if __name__ == "__main__":
    unittest.main()
