"""Cloud Gateway event-clip retention (owner approval 2026-11-05):
- 14-day default, 7/30 options, validated and audited on the server;
- the cleanup sweep enforces it on Cloud Gateway camera media only;
- Full VMS retention and local recordings stay independent.

No AWS, Stripe or network: the sweep's S3 delete is patched."""
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

    def _media(self, media_id, customer, camera_id, age_days):
        started = (NOW - timedelta(days=age_days)).isoformat()
        appliance = {"cam-g": "appl-g", "cam-f": "appl-f", "cam-g2": "appl-g2", "cam-v": "appl-v"}[camera_id]
        with connection() as db:
            db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
                       "VALUES(?,?,?,?,?,?,'motion',?,?)", (f"ev-{media_id}", customer, f"site-{customer}", appliance, camera_id, media_id, started, started))
            db.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,started_at,ended_at,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (media_id, f"ev-{media_id}", customer, camera_id, f"recordings/{customer}/{media_id}.mp4",
                                                    started, started, started))

    def _candidates(self):
        with connection() as db:
            return sorted(row["id"] for row in sweep._expired_candidates(db, NOW) if row["kind"] == "event_media")

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
    def test_sweep_enforces_the_14_day_default_without_any_setting(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("g-13", "cust-1", "cam-g", 13)
        self.assertEqual(self._candidates(), ["g-15"])

    def test_sweep_enforces_the_selected_7_and_30_day_periods(self):
        for age in (8, 20, 31):
            self._media(f"g-{age}", "cust-1", "cam-g", age)
        self._set("cust-1", 7, version=0, confirm=True)
        self.assertEqual(self._candidates(), ["g-20", "g-31", "g-8"])
        self._set("cust-1", 30, version=1)
        self.assertEqual(self._candidates(), ["g-31"])

    def test_media_of_an_ended_binding_still_expires(self):
        self._media("g2-15", "cust-2", "cam-g2", 15)
        self.assertEqual(self._candidates(), ["g2-15"])

    def test_full_vms_media_keeps_its_own_retention(self):
        self._media("f-20", "cust-1", "cam-f", 20)  # same customer, Full VMS camera
        self._media("v-20", "cust-fvms", "cam-v", 20)
        self._set("cust-1", 7, version=0, confirm=True)
        # No Full VMS retention source: never expired, exactly as before.
        self.assertEqual(self._candidates(), [])
        with connection() as db:
            db.execute("INSERT INTO customer_cloud_policy(customer_id,daily_cloud_seconds,retention_days,updated_at,updated_by) "
                       "VALUES('cust-1',NULL,30,?,'admin')", (T0,))
        self._media("g-20", "cust-1", "cam-g", 20)
        # The Full VMS override governs cam-f only; cam-g follows its 7 days.
        self.assertEqual(self._candidates(), ["g-20"])

    def test_unavailable_gateway_retention_never_deletes(self):
        self._media("g-40", "cust-1", "cam-g", 40)
        with patch.object(cgr, "effective_retention_days", side_effect=RuntimeError("corrupt")):
            self.assertEqual(self._candidates(), [])
        with patch.object(cgr, "cloud_gateway_camera_ids", side_effect=RuntimeError("missing table")):
            self.assertEqual(self._candidates(), [])

    def test_sweep_tick_deletes_expired_gateway_cloud_media(self):
        self._media("g-15", "cust-1", "cam-g", 15)
        self._media("g-1", "cust-1", "cam-g", 1)
        deleted_keys = []
        with patch.object(sweep, "_delete_recording_object", side_effect=lambda key: deleted_keys.append(key) or True):
            result = sweep.run_retention_sweep_tick(now=NOW)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(deleted_keys, ["recordings/cust-1/g-15.mp4"])
        with connection() as db:
            self.assertEqual([r["id"] for r in db.execute("SELECT id FROM detection_event_media").fetchall()], ["g-1"])

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
