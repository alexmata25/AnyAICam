"""Audited Full VMS edition backfill: only verified legacy appliances, never Gateways.

A throwaway SQLite database built by the real migrations; nothing else is
contacted. The backfill is never run against any real database here.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
for path in (APP, APP / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from database_backend import override_target

import full_vms_edition_backfill as backfill
from partner_db import connection, initialize_database, transaction

CUTOFF = "2026-11-01T00:00:00"
ADMIN = {"email": "admin@example.test", "role": "administrator"}
STAFF = {"email": "staff@example.test", "role": "administrator"}


class FullVmsBackfillTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.target = override_target(sqlite_path=str(self.directory / "backfill.db"))
        self.target.__enter__()
        self.addCleanup(self.target.__exit__, None, None, None)
        initialize_database()
        from authz_helpers import make_partner_user
        make_partner_user(ADMIN["email"], "administrator", global_admin=True)
        make_partner_user(STAFF["email"], "administrator", partner_admin_grant=True)
        with connection() as db:
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) "
                       "VALUES('cust-1','anyaicam-primary','C','c@example.test','active','real',?)", (CUTOFF,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','S',?)", (CUTOFF,))
            for appliance_id, created, via, marker in (
                    ("legacy-1", "2026-10-01T09:00:00", None, None),
                    ("legacy-2", "2026-09-01T09:00:00", "token", None),
                    ("after-cutoff", "2026-11-10T09:00:00", None, None),
                    ("claimed", "2026-10-15T09:00:00", "claim", None),
                    ("remote-local", "2026-10-01T09:00:00", None, None),
                    ("bad-date", "not a date", None, None),
                    ("marked-local", "2026-10-01T09:00:00", None, "local_gateway"),
                    ("conflict", "2026-10-01T09:00:00", None, "full_vms")):
                db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,activated_via,gateway_edition) "
                           "VALUES(?,?,?,?,?,?,?)", (appliance_id, "cust-1", "site-1", f"AIC-{appliance_id}", created, via, marker))
            db.execute("INSERT INTO local_gateway_remote_subscriptions(appliance_id,customer_id,stripe_customer_id,stripe_subscription_id,"
                       "stripe_subscription_item_id,stripe_price_id,status,stripe_status,created_at,updated_at) "
                       "VALUES('remote-local','cust-1','cus_1','sub_1','si_1','price_1','active','active',?,?)", (CUTOFF, CUTOFF))
            db.execute("INSERT INTO gateway_stable_installation_claim_ids(claim_id,appliance_id,original_claim_id,created_at) "
                       "VALUES('stable-1','conflict','orig-1',?)", (CUTOFF,))

    def markers(self):
        with connection() as db:
            return {item["id"]: item["gateway_edition"] for item in db.execute("SELECT id,gateway_edition FROM appliances")}

    def plan(self):
        with connection() as db:
            return backfill.build_plan(db, created_before=CUTOFF)

    def apply(self, plan, digest, actor=ADMIN):
        with transaction() as db:
            return backfill.apply_plan(db, plan, digest, actor=actor)

    def test_plan_is_read_only_and_only_verified_legacy_appliances_are_eligible(self):
        before = self.markers()
        plan, digest = self.plan()
        self.assertEqual(self.markers(), before)
        self.assertEqual(sorted(item["appliance_id"] for item in plan["eligible"]), ["legacy-1", "legacy-2"])
        review = {item["appliance_id"]: " ".join(item["reasons"]) for item in plan["review"]}
        self.assertEqual(sorted(review), ["after-cutoff", "bad-date", "claimed", "conflict", "remote-local"])
        self.assertIn("after the cutoff", review["after-cutoff"])
        self.assertIn("claim flow", review["claimed"])
        self.assertIn("Gateway evidence", review["remote-local"])
        self.assertIn("CONFLICT", review["conflict"])
        self.assertEqual(plan["already_recorded"], {"local_gateway": 1})
        self.assertEqual(digest, backfill.plan_sha256(json.loads(json.dumps(plan))))
        self.assertNotIn("sqlite", json.dumps(plan))  # only a fingerprint of the database target

    def test_apply_designates_only_eligible_appliances_with_audit(self):
        plan, digest = self.plan()
        self.assertEqual(self.apply(plan, digest), 2)
        markers = self.markers()
        self.assertEqual((markers["legacy-1"], markers["legacy-2"]), ("full_vms", "full_vms"))
        for untouched in ("after-cutoff", "claimed", "remote-local", "bad-date"):
            self.assertIsNone(markers[untouched])
        self.assertEqual((markers["marked-local"], markers["conflict"]), ("local_gateway", "full_vms"))
        with connection() as db:
            audits = db.execute("SELECT entity_id,details_json FROM audit_logs WHERE action='appliance.edition_designated' ORDER BY entity_id").fetchall()
        self.assertEqual([item["entity_id"] for item in audits], ["legacy-1", "legacy-2"])
        self.assertEqual(json.loads(audits[0]["details_json"])["plan_sha256"], digest)
        with connection() as db:
            self.assertEqual(backfill.readiness(db)["unmarked"], 4)

    def test_apply_refuses_unreviewed_foreign_or_unauthorized_plans_without_changes(self):
        plan, digest = self.plan()
        before = self.markers()
        tampered = json.loads(json.dumps(plan))
        tampered["eligible"].append({"appliance_id": "claimed", "customer_id": "cust-1",
                                     "created_at": "2026-10-15T09:00:00", "activated_via": "claim"})
        for args in ((tampered, digest, ADMIN), (plan, "0" * 64, ADMIN), (plan, digest, STAFF),
                     (plan, digest, {"email": "nobody@example.test", "role": "administrator"})):
            with self.subTest(actor=args[2]["email"]), self.assertRaises(backfill.BackfillRefused):
                self.apply(*args)
        with patch.object(backfill, "database_identity", return_value="another-database"):
            with self.assertRaises(backfill.BackfillRefused):
                self.apply(plan, digest)
        self.assertEqual(self.markers(), before)

    def test_stale_plan_rolls_back_every_designation(self):
        plan, digest = self.plan()
        with connection() as db:  # legacy-2 gains Gateway evidence after review
            db.execute("INSERT INTO gateway_claim_recovery_requests(request_id,appliance_id,customer_id,requested_by,status,created_at) "
                       "VALUES('r-1','legacy-2','cust-1','x','pending_manual_verification',?)", (CUTOFF,))
        with self.assertRaises(backfill.BackfillRefused):
            self.apply(plan, digest)
        self.assertIsNone(self.markers()["legacy-1"], "an aborted apply must not keep earlier designations")
        with connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='appliance.edition_designated'").fetchone()["n"], 0)

    def test_reviewed_single_designation_never_converts_gateways(self):
        with transaction() as db:
            backfill.designate_full_vms(db, ADMIN, appliance_id="claimed", customer_id="cust-1",
                                        evidence_reference="order 1234 shipped as Full VMS")
        self.assertEqual(self.markers()["claimed"], "full_vms")
        for appliance_id, evidence, actor in (("remote-local", "x", ADMIN), ("marked-local", "x", ADMIN),
                                              ("after-cutoff", " ", ADMIN), ("after-cutoff", "x", STAFF)):
            with self.subTest(appliance=appliance_id), self.assertRaises(backfill.BackfillRefused):
                with transaction() as db:
                    backfill.designate_full_vms(db, actor, appliance_id=appliance_id, customer_id="cust-1",
                                                evidence_reference=evidence)
        markers = self.markers()
        self.assertEqual((markers["remote-local"], markers["marked-local"], markers["after-cutoff"]),
                         (None, "local_gateway", None))

    def test_cli_apply_requires_explicit_authorization_flag(self):
        from importlib import util
        spec = util.spec_from_file_location("backfill_cli", APP.parent / "tools" / "full_vms_edition_backfill.py")
        cli = util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        plan_file = self.directory / "plan.json"
        self.assertEqual(cli.main(["plan", "--created-before", CUTOFF, "--out", str(plan_file)]), 0)
        self.assertEqual(cli.main(["plan", "--created-before", CUTOFF, "--out", str(plan_file)]), 2)  # never overwrites
        digest = backfill.plan_sha256(json.loads(plan_file.read_text(encoding="utf-8")))
        before = self.markers()
        self.assertEqual(cli.main(["apply", "--plan", str(plan_file), "--sha256", digest, "--actor-email", ADMIN["email"]]), 2)
        self.assertEqual(self.markers(), before)


if __name__ == "__main__":
    unittest.main()
