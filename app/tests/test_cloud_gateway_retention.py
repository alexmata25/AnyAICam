"""Cloud Gateway event-clip retention (owner approval 2026-11-05):
- 14-day default, 7/30 options, validated and audited on the server;
- the cleanup sweep enforces the policy frozen on each clip at capture
  time (Codex review of 9ba528b), never the camera's current binding;
- historical clips without a recorded policy are held unless provably
  Full VMS; Gateway lookup failures hold only those, with diagnostics;
- the worker's readiness needs a real deletion probe and tick;
- Full VMS retention and local recordings stay independent.

No AWS, Stripe or network: the sweep's S3 delete is patched."""
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target
import cloud_gateway_retention as cgr
import recording_retention_sweep as sweep
from partner_db import connection, initialize_database, transaction

NOW = datetime(2026, 11, 5, 12, 0, 0)
T0 = "2026-11-01T00:00:00"


class CloudGatewayRetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = override_target(sqlite_path=str(Path(directory.name) / "retention.db"))
        self.target.__enter__()
        self.addCleanup(self.target.__exit__, None, None, None)
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','P','approved','real',?)", (T0,))
            for customer in ("cust-1", "cust-2", "cust-fvms"):
                db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,'partner-1','C',?,'active','real',?)",
                           (customer, f"{customer}@example.test", T0))
                db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,'S',?)", (f"site-{customer}", customer, T0))
            self._appliance(db, "appl-g", "cust-1", "cloud_gateway")
            self._appliance(db, "appl-f", "cust-1", "full_vms")
            self._appliance(db, "appl-g2", "cust-2", "cloud_gateway")
            self._appliance(db, "appl-v", "cust-fvms", "full_vms")
            self._camera(db, "cam-g", "cust-1", "appl-g", 1)
            self._camera(db, "cam-f", "cust-1", "appl-f", 2)
            self._camera(db, "cam-g2", "cust-2", "appl-g2", 1)
            self._camera(db, "cam-v", "cust-fvms", "appl-v", 1)
            self._binding(db, "cust-1", "appl-g", "cam-g", 1, "active")
            self._binding(db, "cust-2", "appl-g2", "cam-g2", 1, "cancelled")  # ended: media must still expire
            for user_id, role, customer in (("owner-1", "customer_owner", "cust-1"), ("owner-2", "customer_owner", "cust-2"),
                                            ("owner-f", "customer_owner", "cust-fvms"), ("partner-o", "partner_owner", None),
                                            ("admin-g", "administrator", None), ("admin-p", "administrator", None)):
                db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (user_id, "partner-1", f"{user_id}@example.test", "U", role, "x", 1, customer, T0))
            db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) "
                       "VALUES('grant-g','admin-g','administrator','global',NULL,?,'system:test',NULL)", (T0,))
            db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) "
                       "VALUES('grant-p','admin-p','administrator','partner','partner-1',?,'system:test',NULL)", (T0,))

    # ------------------------------------------------------------ seeding
    @staticmethod
    def _appliance(db, appliance_id, customer, edition):
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,gateway_edition,created_at) VALUES(?,?,?,?,?,?)",
                   (appliance_id, customer, f"site-{customer}", f"AIC-{appliance_id.upper()}", edition, T0))

    @staticmethod
    def _camera(db, camera_id, customer, appliance_id, number):
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,device_key,status,created_at) "
                   "VALUES(?,?,?,?,?,?,?,'active',?)",
                   (camera_id, customer, f"site-{customer}", appliance_id, camera_id, number, f"urn:uuid:{camera_id}", T0))

    @staticmethod
    def _binding(db, customer, appliance_id, camera_id, number, status):
        db.execute("INSERT INTO cloud_gateway_camera_entitlements(id,customer_id,appliance_id,camera_id,camera_number,device_key,edition,"
                   "billing_source,billing_reference,billing_version,status,version,audit_reference,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,'cloud_gateway','stripe',?,'v1',?,1,'audit-test',?,?)",
                   (f"binding-{camera_id}", customer, appliance_id, camera_id, number, f"urn:uuid:{camera_id}", f"sub-{customer}", status, T0, T0))

    _DEFAULT = object()

    def _media(self, media_id, customer, camera_id, age_days, policy=_DEFAULT, days=_DEFAULT, created_at=None):
        """A root clip. By default it carries the policy its camera's edition
        records at capture: Cloud Gateway 14 days, or the Full VMS plan.
        policy=None models a historical row cataloged before attribution."""
        started = (NOW - timedelta(days=age_days)).isoformat()
        appliance = {"cam-g": "appl-g", "cam-f": "appl-f", "cam-g2": "appl-g2", "cam-v": "appl-v"}[camera_id]
        gateway = camera_id in ("cam-g", "cam-g2")
        if policy is self._DEFAULT:
            policy = "cloud_gateway" if gateway else "full_vms_plan"
        if days is self._DEFAULT:
            days = 14 if policy == "cloud_gateway" else None
        with connection() as db:
            db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
                       "VALUES(?,?,?,?,?,?,'motion',?,?)", (f"ev-{media_id}", customer, f"site-{customer}", appliance, camera_id, media_id, started, started))
            db.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,started_at,ended_at,created_at,"
                       "retention_policy,retention_days,retention_basis_ref,retention_recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (media_id, f"ev-{media_id}", customer, camera_id, f"recordings/{customer}/{media_id}.mp4",
                        started, started, created_at or started, policy, days,
                        f"binding-{camera_id}" if policy == "cloud_gateway" else None, None if policy is None else started))

    def _candidates(self, diagnostics=None):
        with connection() as db:
            return sorted(row["id"] for row in sweep._expired_candidates(db, NOW, diagnostics) if row["kind"] == "event_media")

    def _full_vms_override(self, customer, days):
        with connection() as db:
            db.execute("INSERT INTO customer_cloud_policy(customer_id,daily_cloud_seconds,retention_days,updated_at,updated_by) "
                       "VALUES(?,NULL,?,?,'admin') ON CONFLICT(customer_id) DO UPDATE SET retention_days=excluded.retention_days",
                       (customer, days, T0))

    def _set(self, customer, days, *, version, confirm=False, actor=("owner-1@example.test", "customer_owner")):
        with transaction() as db:
            return cgr.set_retention(db, customer, days, actor_email=actor[0], actor_role=actor[1],
                                     expected_version=version, confirm_reduction=confirm)

    # ------------------------------------------------------------ policy
    def test_default_is_14_days_with_7_and_30_options(self):
        self.assertEqual(cgr.DEFAULT_RETENTION_DAYS, 14)
        self.assertEqual(cgr.RETENTION_OPTIONS, (7, 14, 30))
        with connection() as db:
            self.assertEqual(cgr.effective_retention_days(db, "cust-1"), 14)
            self.assertEqual(cgr.describe(db, "cust-1")["source"], "default")

    def test_retention_values_are_validated_strictly(self):
        for value in (7, 14, 30):
            self.assertEqual(cgr.parse_retention_days(value), value)
        for value in (True, False, "14", 14.0, 0, -7, 1, 13, 21, 31, 60, 365, None, [14], {"days": 14}):
            with self.assertRaises(cgr.RetentionRefused) as caught:
                cgr.parse_retention_days(value)
            self.assertEqual(caught.exception.status, 422, value)

    def test_database_rejects_an_out_of_range_value(self):
        with self.assertRaises(Exception):
            with connection() as db:
                db.execute("INSERT INTO cloud_gateway_retention_settings(customer_id,retention_days,source,version,updated_at,updated_by) "
                           "VALUES('cust-1',21,'customer_owner',1,?,'x')", (T0,))

    def test_ensure_default_records_and_audits_once(self):
        with transaction() as db:
            self.assertEqual(cgr.ensure_default(db, "cust-1"), 14)
        with transaction() as db:
            self.assertEqual(cgr.ensure_default(db, "cust-1"), 14)
        with connection() as db:
            row = dict(db.execute("SELECT retention_days,source,version FROM cloud_gateway_retention_settings WHERE customer_id='cust-1'").fetchone())
            audits = db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='cloud_gateway.retention.default_applied'").fetchone()["n"]
        self.assertEqual(row, {"retention_days": 14, "source": "default", "version": 1})
        self.assertEqual(audits, 1)

    def test_change_is_versioned_audited_and_needs_confirmation_to_lower(self):
        self.assertEqual(self._set("cust-1", 30, version=0)["retention_days"], 30)
        with self.assertRaises(cgr.RetentionRefused) as stale:
            self._set("cust-1", 14, version=0, confirm=True)
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(cgr.RetentionRefused) as unconfirmed:
            self._set("cust-1", 7, version=1)
        self.assertEqual(unconfirmed.exception.status, 409)
        self.assertIn("confirm", unconfirmed.exception.message)
        result = self._set("cust-1", 7, version=1, confirm=True)
        self.assertEqual((result["retention_days"], result["version"], result["source"]), (7, 2, "customer_owner"))
        with connection() as db:
            audits = [row["details_json"] for row in db.execute(
                "SELECT details_json FROM audit_logs WHERE action='cloud_gateway.retention.changed' ORDER BY id").fetchall()]
        self.assertEqual(len(audits), 2)
        self.assertIn('"from_days": 30', audits[1])
        self.assertIn('"to_days": 7', audits[1])

    def test_raising_needs_no_confirmation(self):
        self.assertEqual(self._set("cust-1", 30, version=0)["retention_days"], 30)

    def test_customer_without_cloud_gateway_cannot_set_it(self):
        with self.assertRaises(cgr.RetentionRefused) as caught:
            self._set("cust-fvms", 30, version=0, actor=("owner-f@example.test", "customer_owner"))
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(cgr.RetentionRefused) as missing:
            self._set("no-such-customer", 30, version=0)
        self.assertEqual(missing.exception.status, 404)

    def test_invalid_value_never_writes(self):
        with self.assertRaises(cgr.RetentionRefused):
            self._set("cust-1", 21, version=0)
        with connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM cloud_gateway_retention_settings WHERE customer_id='cust-1'").fetchone())

    def test_full_vms_retention_sources_are_never_touched(self):
        with connection() as db:
            db.execute("INSERT INTO customer_cloud_policy(customer_id,daily_cloud_seconds,retention_days,updated_at,updated_by) "
                       "VALUES('cust-1',NULL,30,?,'admin')", (T0,))
        self._set("cust-1", 7, version=0, confirm=True)
        with connection() as db:
            self.assertEqual(db.execute("SELECT retention_days FROM customer_cloud_policy WHERE customer_id='cust-1'").fetchone()[0], 30)

    # ------------------------------------------------------------ actor authorization
    def test_actor_authorization(self):
        with connection() as db:
            self.assertEqual(cgr.authorize_actor(db, {"role": "customer_owner", "customer_id": "cust-1", "email": "o@x"}, None)[0], "cust-1")
            self.assertEqual(cgr.authorize_actor(db, {"role": "customer_owner", "customer_id": "cust-1", "email": "o@x"}, "cust-1")[0], "cust-1")
            refusals = [
                (None, None, 401),
                ({"role": "customer_owner", "customer_id": "cust-1", "email": "o@x"}, "cust-2", 403),
                ({"role": "customer_owner", "customer_id": None, "email": "o@x"}, None, 403),
                ({"role": "partner_owner", "partner_id": "partner-1", "email": "partner-o@example.test"}, "cust-1", 403),
                ({"role": "technician", "email": "t@x"}, "cust-1", 403),
                ({"role": "administrator", "email": "admin-p@example.test"}, "cust-1", 403),  # partner-scoped only
                ({"role": "administrator", "email": "admin-g@example.test"}, None, 422),
            ]
            for identity, requested, status in refusals:
                with self.assertRaises(cgr.RetentionRefused) as caught:
                    cgr.authorize_actor(db, identity, requested)
                self.assertEqual(caught.exception.status, status, (identity, requested))
            self.assertEqual(cgr.authorize_actor(db, {"role": "administrator", "email": "admin-g@example.test"}, "cust-2"),
                             ("cust-2", "admin-g@example.test", "administrator"))

    # ------------------------------------------------------------ HTTP routes
    def _client(self):
        app = FastAPI()
        cgr.register_cloud_gateway_retention_routes(app)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    @staticmethod
    def _cookie(email, role):
        import partner_portal
        from authz_helpers import partner_session_cookie
        return {partner_portal.SESSION_COOKIE: partner_session_cookie(email, role)}

    def test_routes_end_to_end(self):
        client = self._client()
        owner = self._cookie("owner-1@example.test", "customer_owner")
        got = client.get("/api/cloud-gateway/retention", cookies=owner)
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual((got.json()["retention_days"], got.json()["version"], got.json()["options"]), (14, 0, [7, 14, 30]))
        bad = client.put("/api/cloud-gateway/retention", cookies=owner, json={"retention_days": "30", "expected_version": 0})
        self.assertEqual(bad.status_code, 422, bad.text)
        extra = client.put("/api/cloud-gateway/retention", cookies=owner, json={"retention_days": 30, "expected_version": 0, "source": "x"})
        self.assertEqual(extra.status_code, 422, extra.text)
        cross = client.put("/api/cloud-gateway/retention", cookies=owner, json={"customer_id": "cust-2", "retention_days": 30, "expected_version": 0})
        self.assertEqual(cross.status_code, 403, cross.text)
        ok = client.put("/api/cloud-gateway/retention", cookies=owner, json={"retention_days": 30, "expected_version": 0})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["retention_days"], 30)
        lower = client.put("/api/cloud-gateway/retention", cookies=owner, json={"retention_days": 7, "expected_version": 1})
        self.assertEqual(lower.status_code, 409, lower.text)
        confirmed = client.put("/api/cloud-gateway/retention", cookies=owner,
                               json={"retention_days": 7, "expected_version": 1, "confirm_reduction": True})
        self.assertEqual(confirmed.json()["retention_days"], 7)
        admin = self._cookie("admin-g@example.test", "administrator")
        other = client.put("/api/cloud-gateway/retention", cookies=admin, json={"customer_id": "cust-2", "retention_days": 30, "expected_version": 0})
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["source"], "administrator")
        fvms = client.get("/api/cloud-gateway/retention", cookies=self._cookie("owner-f@example.test", "customer_owner"))
        self.assertEqual(fvms.status_code, 404, fvms.text)
        self.assertEqual(client.get("/api/cloud-gateway/retention").status_code, 401)
        partner = client.put("/api/cloud-gateway/retention", cookies=self._cookie("partner-o@example.test", "partner_owner"),
                             json={"customer_id": "cust-1", "retention_days": 30, "expected_version": 2})
        self.assertEqual(partner.status_code, 403, partner.text)

    # ------------------------------------------------------------ cleanup sweep
    def test_sweep_enforces_the_14_day_default_frozen_at_capture(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("g-13", "cust-1", "cam-g", 13)
        self.assertEqual(self._candidates(), ["g-15"])

    def test_each_clip_keeps_the_7_or_30_days_it_was_captured_with(self):
        self._media("g7-8", "cust-1", "cam-g", 8, days=7)
        self._media("g7-6", "cust-1", "cam-g", 6, days=7)
        self._media("g30-20", "cust-1", "cam-g", 20, days=30)
        self._media("g30-31", "cust-1", "cam-g", 31, days=30)
        self.assertEqual(self._candidates(), ["g30-31", "g7-8"])

    def test_a_shorter_setting_never_deletes_clips_captured_under_a_longer_one(self):
        self._media("g30-20", "cust-1", "cam-g", 20, days=30)
        self._set("cust-1", 7, version=0, confirm=True)
        self.assertEqual(self._candidates(), [])  # 20 days old, captured with 30: kept

    def test_a_longer_setting_applies_to_new_clips_only(self):
        self._set("cust-1", 7, version=0, confirm=True)
        self._media("g7-8", "cust-1", "cam-g", 8, days=7)
        self._set("cust-1", 30, version=1)
        self._media("g30-8", "cust-1", "cam-g", 8, days=30)
        self.assertEqual(self._candidates(), ["g7-8"])  # its own 7-day promise, not extended retroactively

    def test_full_vms_to_gateway_transition_never_applies_gateway_retention_to_older_clips(self):
        # cam-f was Full VMS: one attributed clip and one historical clip
        # cataloged before the camera's first Cloud Gateway binding.
        self._media("f-20", "cust-1", "cam-f", 20)
        self._media("h-20", "cust-1", "cam-f", 20, policy=None, days=None)
        with connection() as db:
            db.execute("UPDATE appliances SET gateway_edition='cloud_gateway' WHERE id='appl-f'")
            db.execute("INSERT INTO cloud_gateway_camera_entitlements(id,customer_id,appliance_id,camera_id,camera_number,device_key,edition,"
                       "billing_source,billing_reference,billing_version,status,version,audit_reference,created_at,updated_at) "
                       "VALUES('binding-cam-f','cust-1','appl-f','cam-f',2,'urn:uuid:cam-f','cloud_gateway','stripe','sub-cust-1','v1','active',1,"
                       "'audit-test',?,?)", (NOW.isoformat(), NOW.isoformat()))
        self._set("cust-1", 7, version=0, confirm=True)
        # The Gateway 7 days never reaches them; no Full VMS retention: kept.
        self.assertEqual(self._candidates(), [])
        # They follow the Full VMS chain only.
        self._full_vms_override("cust-1", 30)
        self.assertEqual(self._candidates(), [])
        self._full_vms_override("cust-1", 14)
        self.assertEqual(self._candidates(), ["f-20", "h-20"])
        # A clip cataloged on cam-f after the binding carries its own Gateway policy.
        self._media("g-on-f", "cust-1", "cam-f", 8, policy="cloud_gateway", days=7)
        self.assertIn("g-on-f", self._candidates())

    def test_ended_binding_keeps_the_frozen_retention(self):
        self._media("g2-15", "cust-2", "cam-g2", 15)  # cam-g2's binding is cancelled
        self._media("g2-10", "cust-2", "cam-g2", 10)
        self.assertEqual(self._candidates(), ["g2-15"])

    def test_reassignment_or_binding_removal_keeps_the_frozen_retention(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("g30-20", "cust-1", "cam-g", 20, days=30)
        with connection() as db:
            db.execute("UPDATE cameras SET customer_id='cust-2', site_id='site-cust-2' WHERE id='cam-g'")
            db.execute("UPDATE cloud_gateway_camera_entitlements SET status='revoked', ended_reason='transfer' WHERE camera_id='cam-g'")
        self._set("cust-2", 7, version=0, confirm=True)  # the new owner's choice never applies to old clips
        self.assertEqual(self._candidates(), ["g-15"])
        with connection() as db:
            db.execute("DELETE FROM cloud_gateway_camera_entitlements WHERE camera_id='cam-g'")
        self.assertEqual(self._candidates(), ["g-15"])

    def test_ambiguous_historical_clips_are_held(self):
        # Cataloged on/after cam-g's first binding (T0) with no recorded policy.
        self._media("amb-after", "cust-1", "cam-g", 40, policy=None, days=None, created_at="2026-11-02T00:00:00")
        self._media("amb-equal", "cust-1", "cam-g", 40, policy=None, days=None, created_at=T0)
        self._media("amb-badtime", "cust-1", "cam-g", 40, policy=None, days=None, created_at="not-a-time")
        self._full_vms_override("cust-1", 7)
        self._set("cust-1", 7, version=0, confirm=True)
        diagnostics = sweep._new_diagnostics()
        self.assertEqual(self._candidates(diagnostics), [])
        self.assertEqual(diagnostics["held_historical_ambiguous"], 3)

    def test_historical_clip_before_the_first_binding_follows_full_vms(self):
        self._media("pre", "cust-1", "cam-g", 40, policy=None, days=None, created_at="2026-10-01T00:00:00")
        self._media("never-bound", "cust-fvms", "cam-v", 40, policy=None, days=None)
        diagnostics = sweep._new_diagnostics()
        self.assertEqual(self._candidates(diagnostics), [])  # no Full VMS retention: kept, as before
        self.assertEqual(diagnostics["held_no_retention"], 2)
        self._full_vms_override("cust-1", 30)
        self._full_vms_override("cust-fvms", 30)
        self.assertEqual(self._candidates(), ["never-bound", "pre"])

    def test_inconsistent_attribution_is_held_and_out_of_range_is_refused(self):
        self._media("gw-nodays", "cust-1", "cam-g", 40, policy="cloud_gateway", days=None)
        self._media("fv-days", "cust-1", "cam-f", 40, policy="full_vms_plan", days=7)
        self._media("orphan-days", "cust-1", "cam-f", 40, policy=None, days=7)
        self._full_vms_override("cust-1", 7)
        diagnostics = sweep._new_diagnostics()
        self.assertEqual(self._candidates(diagnostics), [])
        self.assertEqual(diagnostics["held_invalid_attribution"], 3)
        for policy, days in (("cloud_gateway", 21), ("other", None)):
            with self.assertRaises(Exception):
                self._media(f"bad-{policy}", "cust-1", "cam-g", 1, policy=policy, days=days)

    def test_gateway_lookup_failure_holds_only_historical_clips(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("f-20", "cust-1", "cam-f", 20)
        self._media("hist", "cust-1", "cam-f", 20, policy=None, days=None)
        self._full_vms_override("cust-1", 14)
        diagnostics = sweep._new_diagnostics()
        with patch.object(sweep, "_first_gateway_binding_times", side_effect=RuntimeError("missing table")):
            self.assertEqual(self._candidates(diagnostics), ["f-20", "g-15"])
        self.assertEqual(diagnostics["held_historical_lookup_unavailable"], 1)
        self.assertIn("missing table", diagnostics["gateway_lookup_error"])

    def test_attributed_clips_need_no_gateway_lookup(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("f-20", "cust-1", "cam-f", 20)
        self._full_vms_override("cust-1", 14)
        with patch.object(sweep, "_first_gateway_binding_times", side_effect=AssertionError("not needed")):
            self.assertEqual(self._candidates(), ["f-20", "g-15"])

    def test_repeated_lookup_failures_are_reported_and_escalated(self):
        self._media("hist", "cust-1", "cam-f", 20, policy=None, days=None)
        sweep.recording_retention_sweep_state["gateway_lookup_failures_consecutive"] = 0
        self.addCleanup(sweep.recording_retention_sweep_state.pop, "gateway_lookup_failures_consecutive", None)
        with patch.object(sweep, "_first_gateway_binding_times", side_effect=RuntimeError("down")), \
                patch.object(sweep, "_delete_recording_object", return_value=True):
            with self.assertLogs(sweep.logger, level="WARNING") as first:
                result = sweep.run_retention_sweep_tick(now=NOW)
            self.assertEqual(sweep.recording_retention_sweep_state["last_diagnostics"]["held_historical_lookup_unavailable"], 1)
            self.assertTrue(any("media_held" in line for line in first.output))
            for _ in range(sweep.LOOKUP_FAILURE_ALERT_TICKS - 2):
                sweep.run_retention_sweep_tick(now=NOW)
            with self.assertLogs(sweep.logger, level="CRITICAL") as escalated:
                sweep.run_retention_sweep_tick(now=NOW)
        self.assertTrue(any("historical_media_expiry_blocked" in line for line in escalated.output))
        self.assertEqual(sweep.recording_retention_sweep_state["gateway_lookup_failures_consecutive"], sweep.LOOKUP_FAILURE_ALERT_TICKS)
        sweep.run_retention_sweep_tick(now=NOW)  # lookup works again: counter resets
        self.assertEqual(sweep.recording_retention_sweep_state["gateway_lookup_failures_consecutive"], 0)

    def test_sweep_tick_deletes_expired_gateway_cloud_media(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("g-1", "cust-1", "cam-g", 1)
        deleted_keys = []
        with patch.object(sweep, "_delete_recording_object", side_effect=lambda key: deleted_keys.append(key) or True):
            result = sweep.run_retention_sweep_tick(now=NOW)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(sweep.recording_retention_sweep_state["last_diagnostics"]["attributed_cloud_gateway"], 2)
        self.assertEqual(deleted_keys, ["recordings/cust-1/g-15.mp4"])
        with connection() as db:
            self.assertEqual([r["id"] for r in db.execute("SELECT id FROM detection_event_media").fetchall()], ["g-1"])

    # ------------------------------------------------------------ worker readiness
    def _ready_config(self):
        return patch.object(sweep, "readiness", return_value={"ready": True, "missing": []})

    def test_configuration_alone_is_not_operational_readiness(self):
        with self._ready_config(), connection() as db:
            result = sweep.operational_readiness(db, NOW)
        self.assertFalse(result["ready"])
        self.assertTrue(any("deletion probe" in item for item in result["missing"]))

    def test_worker_probe_then_tick_makes_the_host_operational(self):
        calls = []

        class FakeS3:
            def delete_object(self, Bucket, Key):
                calls.append(Key)

        with patch.object(sweep, "_lifecycle_s3", return_value=(FakeS3(), "bucket")):
            probe_at = sweep.worker_iteration("host-1", None, now=NOW)
        self.assertEqual(probe_at, NOW)
        self.assertEqual(calls, ["recordings/_retention_probe/host-1.probe"])  # never a customer key
        with self._ready_config(), connection() as db:
            self.assertEqual(sweep.operational_readiness(db, NOW + timedelta(minutes=5)),
                             {"ready": True, "missing": [], "worker_id": "host-1"})
            # Stale tick or stale probe: not ready.
            self.assertFalse(sweep.operational_readiness(db, NOW + sweep.tick_max_age() + timedelta(minutes=1))["ready"])
            row = db.execute("SELECT diagnostics_json FROM recording_retention_sweep_health WHERE worker_id='host-1'").fetchone()
        self.assertIn("attributed_cloud_gateway", row["diagnostics_json"])
        with patch.object(sweep, "_lifecycle_s3", return_value=(FakeS3(), "bucket")):  # not due: no new probe
            self.assertEqual(sweep.worker_iteration("host-1", probe_at, now=NOW + timedelta(hours=1)), probe_at)
        self.assertEqual(len(calls), 1)

    def test_failed_probe_never_sweeps_and_clears_readiness(self):
        self._media("g-15", "cust-1", "cam-g", 15)

        class DeniedS3:
            def delete_object(self, Bucket, Key):
                raise PermissionError("AccessDenied")

        with patch.object(sweep, "_lifecycle_s3", return_value=(DeniedS3(), "bucket")), \
                patch.object(sweep, "run_retention_sweep_tick", side_effect=AssertionError("must not sweep")), \
                self.assertLogs(sweep.logger, level="ERROR"):
            self.assertIsNone(sweep.worker_iteration("host-1", None, now=NOW))
        with self._ready_config(), connection() as db:
            self.assertFalse(sweep.operational_readiness(db, NOW)["ready"])
            row = dict(db.execute("SELECT last_probe_ok_at,last_tick_ok_at,last_error FROM recording_retention_sweep_health").fetchone())
        self.assertEqual((row["last_probe_ok_at"], row["last_tick_ok_at"]), (None, None))
        self.assertIn("PermissionError", row["last_error"])
        with patch.object(sweep, "_lifecycle_s3", return_value=None), self.assertLogs(sweep.logger, level="ERROR"):
            self.assertIsNone(sweep.worker_iteration("host-1", None, now=NOW))

    def test_missing_health_table_is_not_ready(self):
        with connection() as db:
            db.execute("DROP TABLE recording_retention_sweep_health")
        with self._ready_config(), connection() as db, self.assertLogs(sweep.logger, level="ERROR"):
            self.assertFalse(sweep.operational_readiness(db, NOW)["ready"])

    def test_diagnostics_report_is_read_only(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("amb", "cust-1", "cam-g", 40, policy=None, days=None, created_at="2026-11-02T00:00:00")
        with patch.object(sweep, "_delete_recording_object", side_effect=AssertionError("no deletion")):
            report = sweep.diagnostics_report(NOW)
        self.assertEqual(report["would_delete"], 1)
        self.assertEqual(report["classification"]["held_historical_ambiguous"], 1)
        self.assertFalse(report["operational"]["ready"])
        with connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM detection_event_media").fetchone()["n"], 2)

    # ------------------------------------------------------------ local recordings
    def test_local_recordings_are_independent(self):
        with connection() as db:
            db.execute("INSERT INTO local_storage_policy(customer_id,reserved_free_percent,warning_free_percent,local_retention_days,updated_at,updated_by) "
                       "VALUES('cust-1',10,20,90,?,'owner')", (T0,))
        self._set("cust-1", 7, version=0, confirm=True)
        with connection() as db:
            self.assertEqual(db.execute("SELECT local_retention_days FROM local_storage_policy WHERE customer_id='cust-1'").fetchone()[0], 90)
        # The module never names local storage, paths or the filesystem.
        source = Path(cgr.__file__).read_text(encoding="utf-8")
        for forbidden in ("local_storage_policy", "local_relative_path", "os.remove", "unlink", "shutil", "rmtree"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
