"""Gateway authorization for cloud media credentials (recording and live relay).

Route-level: a bare FastAPI app with the real appliance_cloud routes, a
throwaway SQLite database built by the real migrations and a synthetic STS
client that records every AssumeRole call. Nothing contacts AWS or Stripe.

- Local Gateway never receives cloud media credentials.
- Cloud Gateway needs its server-recorded edition, current verified paid
  subscription evidence and an active verified binding for that camera.
- Missing, expired, revoked, mismatched or unverifiable evidence fails
  closed before STS is called.
- An appliance without any Gateway record keeps the Full VMS behaviour.
"""
import json
import secrets
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

import appliance_cloud
import cloud_gateway_camera_entitlements as entitlements
import recording_retention_sweep
from partner_db import connection, initialize_database, password_hash, transaction
from recording_credentials import recording_s3_prefix

# The real check, kept before setUp patches it to "a healthy worker exists".
REAL_OPERATIONAL_READINESS = recording_retention_sweep.operational_readiness
CONFIGURED = {"ready": True, "missing": []}

NOW = "2026-11-04T00:00:00"
PRICE = "price_synthetic_cloud_gateway"
ACTOR = {"email": "admin@example.test", "role": "administrator"}


class _FakeSTS:
    calls = []

    def assume_role(self, **kwargs):
        _FakeSTS.calls.append(kwargs)
        return {"Credentials": {"AccessKeyId": "FAKE", "SecretAccessKey": "fake", "SessionToken": "fake",
                                "Expiration": datetime.now(timezone.utc)}}


def _subscription(customer_id, **changes):
    record = {"customer_id": customer_id, "subscription_id": f"sub-{customer_id}", "version": "v1",
              "product": "cloud_gateway", "status": "active", "stripe_status": "active", "verified_paid": True,
              "payment_failed_at": None, "reversal": False, "quantity": 2,
              "current_period_end": int(time.time()) + 86400, "price_id": PRICE, "amount_cents": 999,
              "currency": "usd", "interval": "month", "interval_count": 1}
    record.update(changes)
    return record


class GatewayMediaCredentialAuthorizationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = override_target(sqlite_path=str(Path(directory.name) / "media.db"))
        self.target.__enter__()
        self.addCleanup(self.target.__exit__, None, None, None)
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','P','approved','real',?)", (NOW,))
            for customer in ("cust-1", "cust-2"):
                db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,'partner-1','C',?,'active','real',?)",
                           (customer, f"{customer}@example.test", NOW))
                db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,'S',?)", (f"site-{customer}", customer, NOW))
                self._grant_hybrid(db, customer)
            self._appliance(db, "appl-1", "cust-1")
            self._appliance(db, "appl-2", "cust-2")
            self._camera(db, "cam-1", "cust-1", "appl-1", 1)
            self._camera(db, "cam-2", "cust-2", "appl-2", 1)
        self.subscriptions = {"cust-1": _subscription("cust-1"), "cust-2": _subscription("cust-2")}
        _FakeSTS.calls = []
        fake_boto3 = type("_Boto3", (), {"client": staticmethod(lambda service, region_name=None: _FakeSTS())})
        patches = [
            patch.object(appliance_cloud, "RECORDING_UPLOAD_ENABLED", False),
            patch.object(appliance_cloud, "EVENT_MEDIA_UPLOAD_ENABLED", True),
            patch.object(appliance_cloud, "RECORDING_UPLOAD_PILOT_CAMERAS", frozenset()),
            patch.object(appliance_cloud, "RECORDING_UPLOAD_ROLE_ARN", "arn:aws:iam::000000000000:role/test"),
            patch.object(appliance_cloud, "RECORDING_S3_BUCKET", "test-bucket"),
            patch.object(appliance_cloud, "RECORDING_AWS_REGION", "us-east-1"),
            patch.object(appliance_cloud, "RECORDING_SESSION_TAGS_ENABLED", False),
            patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", True),
            patch.object(appliance_cloud, "LIVE_UPLOAD_ROLE_ARN", "arn:aws:iam::000000000000:role/live"),
            patch.object(appliance_cloud, "LIVE_RELAY_S3_BUCKET", "live-bucket"),
            patch.object(appliance_cloud, "LIVE_RELAY_AWS_REGION", "us-east-1"),
            patch.object(appliance_cloud, "boto3", fake_boto3),
            patch.dict("os.environ", {entitlements.CLOUD_GATEWAY_PRICE_ENV: PRICE}),
            # A retention worker is proven to delete on this (synthetic) host.
            patch("recording_retention_sweep.operational_readiness", return_value={"ready": True, "missing": [], "worker_id": "test"}),
            # The verified-evidence store is synthetic here; every other check is real.
            patch.object(entitlements, "_current_verified_cloud_subscription",
                         side_effect=lambda db, customer, reference: (
                             dict(self.subscriptions[customer]) if customer in self.subscriptions
                             and self.subscriptions[customer]["subscription_id"] == reference else None)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ---------------------------------------------------------------- fixtures

    @staticmethod
    def _grant_hybrid(db, customer):
        db.execute(
            "INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
            "stripe_subscription_item_id,stripe_price_id,status,stripe_status,local_retention_days,cloud_event_storage,"
            "cloud_event_retention_days,created_at,updated_at) VALUES(?,?,'hybrid',4,?,?,?,'price_t','active','active',7,1,14,?,?)",
            (f"v2-{customer}", customer, f"cus_{customer}", f"sub_v2_{customer}", f"si_{customer}", NOW, NOW))

    @staticmethod
    def _appliance(db, appliance_id, customer):
        # Explicit, trusted Full VMS identity unless a test sets another edition.
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,live_relay_pilot,gateway_edition,created_at) "
                   "VALUES(?,?,?,?,1,'full_vms',?)",
                   (appliance_id, customer, f"site-{customer}", f"AIC-{appliance_id.upper()}", NOW))
        db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
                   (f"cred-{appliance_id}", appliance_id, password_hash(f"secret-{appliance_id}"), NOW))

    @staticmethod
    def _camera(db, camera_id, customer, appliance_id, number):
        db.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,device_key,status,local_recording_mode,"
            "cloud_recording_mode,created_at) VALUES(?,?,?,?,?,?,?,'configured','event','motion',?)",
            (camera_id, customer, f"site-{customer}", appliance_id, f"Camera {number}", number, f"urn:uuid:{camera_id}", NOW))

    def _edition(self, appliance_id, edition):
        with connection() as db:
            db.execute("UPDATE appliances SET gateway_edition=? WHERE id=?", (edition, appliance_id))

    def _bind(self, customer="cust-1", appliance_id="appl-1", camera_id="cam-1", binding="binding-1"):
        """Produce a real verified binding through the production assignment path."""
        with patch.object(entitlements, "CLOUD_GATEWAY_BILLING_SOURCE_READY", True), \
                patch("gateway_internal_provisioning.edition_authorization_effective", return_value=True):
            with transaction() as db:
                result = entitlements.produce_paid_binding(
                    db, source_reference=f"sub-{customer}", source_version="v1", customer_id=customer,
                    appliance_id=appliance_id, camera_id=camera_id, actor=ACTOR, request_id=binding)
        self.assertEqual(result["status"], "assigned")

    def _headers(self, appliance_id="appl-1"):
        return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())),
                "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer secret-{appliance_id}"}

    def _request(self, camera_id="cam-1", appliance_id="appl-1"):
        return self.client.post(f"/api/appliance/recordings/{camera_id}/credentials", headers=self._headers(appliance_id))

    def _live(self, camera_id="cam-1", appliance_id="appl-1"):
        return self.client.post(f"/api/appliance/live/{camera_id}/session", headers=self._headers(appliance_id))

    def _paid_ready(self):
        """Billing source on and the internal edition chain accepted; nothing else is patched."""
        return (patch.object(entitlements, "CLOUD_GATEWAY_BILLING_SOURCE_READY", True),
                patch("gateway_internal_provisioning.edition_authorization_effective", return_value=True))

    def assertRefusedBeforeSts(self, response, status=403):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(_FakeSTS.calls, [], "STS must not be called for a refused request")

    # ---------------------------------------------------------------- Full VMS

    def test_full_vms_appliance_keeps_existing_event_media_credentials(self):
        response = self._request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(_FakeSTS.calls), 1)
        self.assertIn("/events/", json.dumps(json.loads(_FakeSTS.calls[0]["Policy"])))

    def test_full_vms_appliance_keeps_existing_live_relay_session(self):
        response = self._live()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(_FakeSTS.calls), 1)

    def test_unverified_edition_identity_is_refused_before_sts(self):
        for marker in (None, "garbage", "FULL_VMS", ""):
            self._edition("appl-1", marker)
            with self.subTest(marker=marker):
                self.assertRefusedBeforeSts(self._request())
                self.assertRefusedBeforeSts(self._live())

    def test_full_vms_marker_with_gateway_evidence_is_refused(self):
        with connection() as db:
            db.execute("INSERT INTO gateway_stable_installation_claim_ids(claim_id,appliance_id,original_claim_id,created_at) "
                       "VALUES('stable-1','appl-1','orig-1',?)", (NOW,))
        self.assertRefusedBeforeSts(self._request())

    def test_missing_edition_column_or_evidence_table_fails_closed(self):
        import server_gateway_edition
        original = server_gateway_edition._table_exists
        with patch.object(server_gateway_edition, "_table_exists",
                          side_effect=lambda db, name: name != "gateway_checkout_attempts" and original(db, name)):
            self.assertRefusedBeforeSts(self._request(), status=503)
        with patch("server_gateway_edition.edition_records",
                   side_effect=server_gateway_edition.EditionUnverifiable("Appliance edition column is missing.")):
            self.assertRefusedBeforeSts(self._request(), status=503)

    def test_appliance_table_without_edition_column_fails_closed(self):
        import server_gateway_edition

        class _Row(dict):
            pass

        class _Db:
            def execute(self, sql, args=()):
                if "FROM appliances" in sql:
                    return type("_C", (), {"fetchone": lambda self: _Row(id="appl-1", customer_id="cust-1")})()
                raise AssertionError("evidence must not be read without the column")

        with self.assertRaises(server_gateway_edition.EditionUnverifiable):
            server_gateway_edition.edition_records(_Db(), "appl-1")

    def test_full_vms_still_requires_cloud_event_protection(self):
        with connection() as db:
            db.execute("UPDATE camera_plan_entitlements_v2 SET status='cancelled' WHERE customer_id='cust-1'")
        self.assertRefusedBeforeSts(self._request())

    # ---------------------------------------------------------------- Local Gateway

    def test_local_gateway_is_never_issued_credentials(self):
        self._edition("appl-1", "local_gateway")
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._request())
            self.assertRefusedBeforeSts(self._live())

    def test_local_gateway_recorded_only_by_remote_subscription_is_refused(self):
        with connection() as db:
            db.execute("INSERT INTO local_gateway_remote_subscriptions(appliance_id,customer_id,stripe_customer_id,stripe_subscription_id,"
                       "stripe_subscription_item_id,stripe_price_id,status,stripe_status,created_at,updated_at) "
                       "VALUES('appl-1','cust-1','cus_1','sub_remote_1','si_r','price_r','active','active',?,?)", (NOW, NOW))
        self.assertRefusedBeforeSts(self._request())

    def test_local_gateway_with_a_cloud_binding_is_still_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        self._edition("appl-1", "local_gateway")
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._request())

    # ---------------------------------------------------------------- Cloud Gateway

    def test_unpaid_cloud_gateway_camera_is_refused(self):
        self._edition("appl-1", "cloud_gateway")
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._request())

    def test_paid_cloud_gateway_camera_is_accepted(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        with a, b:
            response = self._request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(_FakeSTS.calls), 1)
        self.assertIn("/events/", json.dumps(json.loads(_FakeSTS.calls[0]["Policy"])))

    def _without_hybrid(self, customer="cust-1"):
        with connection() as db:
            db.execute("DELETE FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer,))

    def _retention(self, days, customer="cust-1"):
        with connection() as db:
            db.execute("INSERT INTO customer_cloud_policy(customer_id,daily_cloud_seconds,retention_days,updated_at,updated_by) "
                       "VALUES(?,NULL,?,?,'admin@example.test') ON CONFLICT(customer_id) DO UPDATE SET retention_days=excluded.retention_days",
                       (customer, days, NOW))

    def test_paid_cloud_gateway_needs_no_full_vms_hybrid_plan(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        self._without_hybrid()
        self._retention(30)
        a, b = self._paid_ready()
        with a, b:
            response = self._request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("/events/", json.dumps(json.loads(_FakeSTS.calls[0]["Policy"])))

    def _gateway_retention_row(self, customer="cust-1"):
        with connection() as db:
            row = db.execute("SELECT retention_days,source,version FROM cloud_gateway_retention_settings WHERE customer_id=?",
                             (customer,)).fetchone()
        return dict(row) if row else None

    def test_new_cloud_gateway_customer_receives_the_14_day_default(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        self._without_hybrid()  # no Full VMS retention source at all
        self.assertIsNone(self._gateway_retention_row())
        a, b = self._paid_ready()
        with a, b:
            response = self._request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._gateway_retention_row(), {"retention_days": 14, "source": "default", "version": 1})
        with connection() as db:
            audit = db.execute("SELECT actor_email,entity_id FROM audit_logs WHERE action='cloud_gateway.retention.default_applied'").fetchall()
        self.assertEqual([tuple(row) for row in audit], [("system:cloud_gateway_default", "cust-1")])
        with a, b:  # a second upload never re-applies or audits the default again
            self.assertEqual(self._request().status_code, 200)
        self.assertEqual(self._gateway_retention_row()["version"], 1)

    def test_cloud_gateway_retention_ignores_the_full_vms_override(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        self._without_hybrid()
        self._retention(30)  # a Full VMS (RDM) override is not a Cloud Gateway choice
        a, b = self._paid_ready()
        with a, b:
            self.assertEqual(self._request().status_code, 200)
        self.assertEqual(self._gateway_retention_row()["retention_days"], 14)

    def test_cloud_gateway_upload_is_refused_when_the_sweep_does_not_enforce(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        with a, b, patch("recording_retention_sweep.operational_readiness",
                                return_value={"ready": False, "missing": ["ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED=true"]}):
            response = self._request()
        self.assertRefusedBeforeSts(response, status=503)
        self.assertIn("retention", response.json()["detail"])
        self.assertIsNone(self._gateway_retention_row())

    # ------------------------------------------------ operational readiness
    def _health(self, *, probe_age_hours, tick_age_minutes):
        from datetime import timedelta
        now = datetime.now()
        with connection() as db:
            db.execute("INSERT INTO recording_retention_sweep_health(worker_id,started_at,last_probe_ok_at,last_tick_ok_at,updated_at) "
                       "VALUES('host-1',?,?,?,?)", (now.isoformat(), (now - timedelta(hours=probe_age_hours)).isoformat(),
                                                    (now - timedelta(minutes=tick_age_minutes)).isoformat(), now.isoformat()))

    def test_configuration_alone_does_not_authorize_cloud_gateway_uploads(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        with a, b, patch("recording_retention_sweep.operational_readiness", new=REAL_OPERATIONAL_READINESS), \
                patch("recording_retention_sweep.readiness", return_value=CONFIGURED):
            response = self._request()
        self.assertRefusedBeforeSts(response, status=503)
        self.assertIsNone(self._gateway_retention_row())

    def test_stale_deletion_probe_or_tick_refuses_cloud_gateway_uploads(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        for probe_age, tick_age in ((30, 5), (1, 200)):  # probe older than 26 h; tick older than 2 intervals
            with connection() as db:
                db.execute("DELETE FROM recording_retention_sweep_health")
            self._health(probe_age_hours=probe_age, tick_age_minutes=tick_age)
            with a, b, patch("recording_retention_sweep.operational_readiness", new=REAL_OPERATIONAL_READINESS), \
                    patch("recording_retention_sweep.readiness", return_value=CONFIGURED):
                self.assertRefusedBeforeSts(self._request(), status=503)

    def test_a_proven_retention_worker_authorizes_cloud_gateway_uploads(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        self._health(probe_age_hours=1, tick_age_minutes=5)
        a, b = self._paid_ready()
        with a, b, patch("recording_retention_sweep.operational_readiness", new=REAL_OPERATIONAL_READINESS), \
                patch("recording_retention_sweep.readiness", return_value=CONFIGURED):
            response = self._request()
        self.assertEqual(response.status_code, 200, response.text)

    # ------------------------------------------------ capture-time attribution
    def _event(self, customer, appliance_id, camera_id, local_event_id):
        with connection() as db:
            db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
                       "VALUES(?,?,?,?,?,?,'motion',?,?)",
                       (f"ev-{local_event_id}", customer, f"site-{customer}", appliance_id, camera_id, local_event_id, NOW, NOW))
        return (recording_s3_prefix(customer, f"site-{customer}", appliance_id, camera_id)
                + f"2026/11/04/events/motion_{local_event_id}.mp4")

    def _catalog(self, camera_id, appliance_id, local_event_id, key):
        return self.client.post(f"/api/appliance/analytics/{camera_id}/events/{local_event_id}/media", headers=self._headers(appliance_id),
                                json={"s3_key": key, "started_at": NOW, "ended_at": NOW, "duration_seconds": 5})

    def _attribution(self, local_event_id):
        with connection() as db:
            row = db.execute("SELECT retention_policy,retention_days,retention_basis_ref,retention_recorded_at FROM detection_event_media "
                             "WHERE detection_event_id=?", (f"ev-{local_event_id}",)).fetchone()
        return dict(row) if row else None

    def test_cloud_gateway_clip_freezes_its_retention_and_binding_at_capture(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        key = self._event("cust-1", "appl-1", "cam-1", "g-1")
        a, b = self._paid_ready()
        with a, b, patch.object(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True):
            response = self._catalog("cam-1", "appl-1", "g-1", key)
        self.assertEqual(response.status_code, 200, response.text)
        with connection() as db:
            binding_id = db.execute("SELECT id FROM cloud_gateway_camera_entitlements WHERE camera_id='cam-1'").fetchone()["id"]
            details = json.loads(db.execute("SELECT details_json FROM audit_logs WHERE action='appliance.analytics_event_media_available'")
                                 .fetchone()["details_json"])
        frozen = self._attribution("g-1")
        self.assertEqual((frozen["retention_policy"], frozen["retention_days"], frozen["retention_basis_ref"]),
                         ("cloud_gateway", 14, binding_id))
        self.assertTrue(frozen["retention_recorded_at"])
        self.assertEqual((details["retention_policy"], details["retention_days"], details["retention_basis_ref"]),
                         ("cloud_gateway", 14, binding_id))
        # A later retention change and a re-registration never rewrite it.
        import cloud_gateway_retention
        with transaction() as db:
            cloud_gateway_retention.set_retention(db, "cust-1", 30, actor_email="o@example.test", actor_role="customer_owner",
                                                  expected_version=1)
        with a, b, patch.object(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True):
            self.assertEqual(self._catalog("cam-1", "appl-1", "g-1", key).json()["status"], "duplicate")
        self.assertEqual(self._attribution("g-1"), frozen)

    def test_full_vms_clip_records_the_full_vms_plan_policy(self):
        key = self._event("cust-2", "appl-2", "cam-2", "f-1")
        with patch.object(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True):
            response = self._catalog("cam-2", "appl-2", "f-1", key)
        self.assertEqual(response.status_code, 200, response.text)
        frozen = self._attribution("f-1")
        self.assertEqual((frozen["retention_policy"], frozen["retention_days"], frozen["retention_basis_ref"]),
                         ("full_vms_plan", None, None))

    def test_capture_retention_refuses_an_unverified_gateway_basis(self):
        from fastapi import HTTPException
        self.assertEqual(appliance_cloud._capture_retention("full_vms", {}), ("full_vms_plan", None, None))
        self.assertEqual(appliance_cloud._capture_retention(7, {"binding_id": "b-1"}), ("cloud_gateway", 7, "b-1"))
        for basis, details in ((14, {}), (21, {"binding_id": "b"}), (True, {"binding_id": "b"}), ("14", {"binding_id": "b"}), (None, {})):
            with self.assertRaises(HTTPException) as caught:
                appliance_cloud._capture_retention(basis, details)
            self.assertEqual(caught.exception.status_code, 503)

    def test_invalid_cloud_gateway_retention_fails_closed(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        import cloud_gateway_retention
        with a, b, patch.object(cloud_gateway_retention, "effective_retention_days", side_effect=RuntimeError("corrupt")):
            self.assertRefusedBeforeSts(self._request(), status=503)

    def test_paid_cloud_gateway_camera_explicitly_disabled_is_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        with connection() as db:
            db.execute("UPDATE cameras SET cloud_recording_mode='disabled' WHERE id='cam-1'")
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._request())

    def test_cloud_gateway_configuration_advertises_motion_only_for_bound_cameras(self):
        self._edition("appl-1", "cloud_gateway")
        with connection() as db:
            self._camera(db, "cam-3", "cust-1", "appl-1", 2)
            self._camera(db, "cam-4", "cust-1", "appl-1", 3)
            db.execute("UPDATE cameras SET cloud_recording_mode='disabled' WHERE id='cam-4'")
        self._bind()
        self._without_hybrid()
        response = self.client.get("/api/appliance/configuration", headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        modes = {item["id"]: item["recording_mode"] for item in response.json()["cameras"]}
        self.assertEqual(modes, {"cam-1": "motion", "cam-3": "disabled", "cam-4": "disabled"})

    def test_cloud_gateway_catalog_is_not_blocked_by_a_missing_full_vms_plan(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        self._without_hybrid()
        self._retention(14)
        key = "recordings/cust-1/site-cust-1/appl-1/cam-1/events/e-1.mp4"
        a, b = self._paid_ready()
        with a, b, patch.object(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True):
            response = self.client.post("/api/appliance/analytics/cam-1/events/e-1/media", headers=self._headers(),
                                        json={"s3_key": key, "started_at": NOW, "ended_at": NOW, "duration_seconds": 5})
        self.assertNotIn(response.json().get("detail"), {"Camera is not entitled to motion event recording.",
                                                         "An active 7, 14, or 30-day motion recording plan is required.",
                                                         "This Gateway camera is not entitled to cloud media."})

    def test_full_vms_never_takes_the_write_transaction(self):
        import partner_db
        with patch.object(partner_db, "transaction", side_effect=AssertionError("Full VMS must not lock")):
            response = self._request()
        self.assertEqual(response.status_code, 200, response.text)

    def test_paid_cloud_gateway_is_refused_while_billing_source_is_disabled(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        with patch("gateway_internal_provisioning.edition_authorization_effective", return_value=True):
            self.assertRefusedBeforeSts(self._request())

    def test_paid_cloud_gateway_without_edition_evidence_is_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        with patch.object(entitlements, "CLOUD_GATEWAY_BILLING_SOURCE_READY", True):
            self.assertRefusedBeforeSts(self._request())  # real edition chain: no internal decision exists

    def test_cloud_gateway_never_gets_a_live_relay_session(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._live())

    def test_revoked_binding_is_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        with transaction() as db:
            entitlements.retire_binding(db, ACTOR, binding_id="binding-1", customer_id="cust-1", appliance_id="appl-1",
                                        camera_id="cam-1", version=1, reason="entitlement_revoked")
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._request())

    def test_expired_subscription_is_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        self.subscriptions["cust-1"]["current_period_end"] = int(time.time()) - 1
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._request())

    def test_unverified_or_failed_payment_is_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        for change in ({"verified_paid": False}, {"payment_failed_at": NOW}, {"reversal": True}, {"status": "past_due"}):
            self.subscriptions["cust-1"] = _subscription("cust-1", **change)
            with self.subTest(change=change), a, b:
                self.assertRefusedBeforeSts(self._request())

    def test_mismatched_edition_records_are_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        with connection() as db:
            db.execute("INSERT INTO appliance_claims(id,device_id,claim_session_id,claim_code_hash,status,customer_id,appliance_id,expires_at,created_at) "
                       "VALUES('claim-x','device-x','session-x','x','completed','cust-1','appl-1',?,?)", (NOW, NOW))
            db.execute("INSERT INTO appliance_edition_provisioning_decisions(id,customer_id,appliance_id,claim_id,edition,evidence_source,"
                       "evidence_reference,evidence_version,verified_by,verified_at,revoked_at) "
                       "VALUES('d-1','cust-1','appl-1','claim-x','local_gateway','x','x','1','x',?,NULL)", (NOW,))
        a, b = self._paid_ready()
        with a, b:
            self.assertRefusedBeforeSts(self._request())

    def test_cross_customer_camera_and_binding_are_refused(self):
        self._edition("appl-1", "cloud_gateway")
        self._edition("appl-2", "cloud_gateway")
        self._bind(customer="cust-2", appliance_id="appl-2", camera_id="cam-2", binding="binding-2")
        a, b = self._paid_ready()
        with a, b:
            # Another customer's appliance cannot use this customer's camera.
            self.assertRefusedBeforeSts(self._request(camera_id="cam-1", appliance_id="appl-2"))
            # This customer's camera has no binding; another customer's paid binding grants nothing.
            self.assertRefusedBeforeSts(self._request(camera_id="cam-1", appliance_id="appl-1"))
            # A binding row recorded for the wrong customer is not this camera's evidence.
            with connection() as db:
                db.execute("UPDATE cloud_gateway_camera_entitlements SET camera_id='cam-1',appliance_id='appl-1' WHERE id='binding-2'")
            self.assertRefusedBeforeSts(self._request(camera_id="cam-1", appliance_id="appl-1"))

    def test_gateway_cannot_catalog_cloud_event_media(self):
        self._edition("appl-1", "local_gateway")
        key = "recordings/cust-1/site-cust-1/appl-1/cam-1/events/e-1.mp4"
        with patch.object(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True):
            for path, body in (("media", {"s3_key": key, "started_at": NOW, "ended_at": NOW}),
                               ("media/shared", {"parent_local_event_id": "e-0"})):
                with self.subTest(path=path):
                    response = self.client.post(f"/api/appliance/analytics/cam-1/events/e-1/{path}",
                                                headers=self._headers(), json=body)
                    self.assertEqual(response.status_code, 403, response.text)
        with connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action LIKE '%media%'").fetchone()["n"], 0)

    def test_evidence_lookup_failure_fails_closed(self):
        self._edition("appl-1", "cloud_gateway")
        self._bind()
        a, b = self._paid_ready()
        with a, b, patch.object(entitlements, "verify_paid_binding", side_effect=RuntimeError("database unavailable")):
            self.assertRefusedBeforeSts(self._request(), status=503)


if __name__ == "__main__":
    unittest.main()
