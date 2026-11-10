"""Level 2 (offline) paid Cloud Gateway upload verification.

See docs/cloud-gateway-paid-binding-verification-strategy.md. Unlike the
route tests in test_gateway_media_credential_authorization.py, the paid
evidence here is NOT synthetic: it is written by the real reducer
(cloud_gateway_subscription_evidence.reconcile_verified_snapshot) from
canonical-shaped provider snapshots, read back by the real evidence reader
(_current_verified_cloud_subscription: single matching webhook audit row,
300 s freshness, hold table), bound by the real produce_paid_binding and
revalidated by verify_paid_binding on every request.

Patched, and only these:
- the disabled gates WEBHOOK_PRODUCER_READY and
  CLOUD_GATEWAY_BILLING_SOURCE_READY (both stay False in the product);
- gateway_internal_provisioning.edition_authorization_effective (the
  internal edition chain, covered by its own suites);
- the upload price env var, upload flags and a synthetic STS client;
- recording_retention_sweep.readiness (configuration only). The worker's
  operational evidence is a real health row read by the real check.

No Stripe, AWS, network, VM or deployment. SQLite only.
"""
import json
import secrets
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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
import cloud_gateway_retention
import cloud_gateway_subscription_evidence as producer
import recording_retention_sweep as sweep
from partner_db import connection, initialize_database, password_hash, transaction
from recording_credentials import recording_s3_prefix

PRICE = "price_offline_cloud_gateway"
T0 = "2026-11-01T00:00:00"
ACTOR = {"email": "admin@example.test", "role": "administrator"}


class _FakeSTS:
    calls = []

    def assume_role(self, **kwargs):
        _FakeSTS.calls.append(kwargs)
        return {"Credentials": {"AccessKeyId": "FAKE", "SecretAccessKey": "fake", "SessionToken": "fake",
                                "Expiration": datetime.now(timezone.utc)}}


class PaidCloudGatewayUploadOfflineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = override_target(sqlite_path=str(Path(directory.name) / "paid.db"))
        self.target.__enter__()
        self.addCleanup(self.target.__exit__, None, None, None)
        initialize_database()
        now = datetime.now().isoformat()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','P','approved','real',?)", (T0,))
            for customer in ("cust-1", "cust-2"):
                db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,'partner-1','C',?,'active','real',?)",
                           (customer, f"{customer}@example.test", T0))
                db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,'S',?)", (f"site-{customer}", customer, T0))
                db.execute("INSERT INTO stripe_customer_bindings(customer_id,stripe_customer_id,created_at) VALUES(?,?,?)",
                           (customer, f"cus_{customer}", T0))
            for appliance_id, customer in (("appl-1", "cust-1"), ("appl-2", "cust-2")):
                db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,gateway_edition,created_at) VALUES(?,?,?,?,'cloud_gateway',?)",
                           (appliance_id, customer, f"site-{customer}", f"AIC-{appliance_id.upper()}", T0))
                db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
                           (f"cred-{appliance_id}", appliance_id, password_hash(f"secret-{appliance_id}"), T0))
            for camera_id, customer, appliance_id, number in (("cam-1", "cust-1", "appl-1", 1), ("cam-2", "cust-1", "appl-1", 2),
                                                              ("cam-x", "cust-2", "appl-2", 1)):
                db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,device_key,status,local_recording_mode,"
                           "cloud_recording_mode,created_at) VALUES(?,?,?,?,?,?,?,'configured','event','motion',?)",
                           (camera_id, customer, f"site-{customer}", appliance_id, camera_id, number, f"urn:uuid:{camera_id}", T0))
            db.execute("INSERT INTO checkout_pending(customer_id,purchase,token,price_id,quantity,fingerprint,session_id,status,expires_at,created_at,updated_at) "
                       "VALUES('cust-1','cloud_gateway','tok',?,1,'fp','cs_offline_1','paid',?,?,?)", (PRICE, int(time.time()) + 3600, now, now))
            # Operational evidence of a retention worker, read by the real check.
            db.execute("INSERT INTO recording_retention_sweep_health(worker_id,started_at,last_probe_ok_at,last_tick_ok_at,updated_at) "
                       "VALUES('offline-host',?,?,?,?)", (now, now, now, now))
        _FakeSTS.calls = []
        fake_boto3 = type("_Boto3", (), {"client": staticmethod(lambda service, region_name=None: _FakeSTS())})
        patches = [
            patch.object(producer, "WEBHOOK_PRODUCER_READY", True),
            patch.object(entitlements, "CLOUD_GATEWAY_BILLING_SOURCE_READY", True),
            patch("gateway_internal_provisioning.edition_authorization_effective", return_value=True),
            patch.dict("os.environ", {entitlements.CLOUD_GATEWAY_PRICE_ENV: PRICE}),
            patch.object(appliance_cloud, "RECORDING_UPLOAD_ENABLED", False),
            patch.object(appliance_cloud, "EVENT_MEDIA_UPLOAD_ENABLED", True),
            patch.object(appliance_cloud, "RECORDING_UPLOAD_PILOT_CAMERAS", frozenset()),
            patch.object(appliance_cloud, "RECORDING_UPLOAD_ROLE_ARN", "arn:aws:iam::000000000000:role/test"),
            patch.object(appliance_cloud, "RECORDING_S3_BUCKET", "test-bucket"),
            patch.object(appliance_cloud, "RECORDING_AWS_REGION", "us-east-1"),
            patch.object(appliance_cloud, "RECORDING_SESSION_TAGS_ENABLED", False),
            patch.object(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True),
            patch.object(appliance_cloud, "boto3", fake_boto3),
            patch.object(sweep, "readiness", return_value={"ready": True, "missing": []}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self._snapshots()

    # ------------------------------------------------------------ provider snapshots
    def _snapshots(self):
        self.sub = {"id": "sub_offline_1", "customer": "cus_cust-1", "status": "active",
                    "current_period_end": int(time.time()) + 30 * 86400, "latest_invoice": "in_1",
                    "metadata": {"anyaicam_product": "cloud_gateway", "anyaicam_customer_id": "cust-1"},
                    "items": {"data": [{"quantity": 1, "price": {"id": PRICE, "unit_amount": 999, "currency": "usd",
                                                                 "recurring": {"interval": "month", "interval_count": 1}}}]}}
        self.invoice = {"id": "in_1", "subscription": "sub_offline_1", "customer": "cus_cust-1", "status": "paid", "currency": "usd",
                        "amount_paid": 999, "charge": "ch_1", "payment_intent": "pi_1"}
        self.charge = {"id": "ch_1", "status": "succeeded", "currency": "usd", "amount": 999, "amount_refunded": 0,
                       "refunded": False, "disputed": False, "payment_intent": "pi_1", "invoice": "in_1"}
        self.checkout = {"id": "cs_offline_1", "mode": "subscription", "payment_status": "paid", "customer": "cus_cust-1",
                         "subscription": "sub_offline_1",
                         "metadata": {"anyaicam_product": "cloud_gateway", "anyaicam_customer_id": "cust-1"}}

    def _reconcile(self, event="evt_1", created=100):
        with transaction() as db:
            return producer.reconcile_verified_snapshot(db, event_id=event, event_created=created, customer_id="cust-1",
                                                        session_id="cs_offline_1", subscription=self.sub, invoice=self.invoice,
                                                        charge=self.charge, checkout=self.checkout)

    def _bind(self, camera_id="cam-1", version="evt_1", customer="cust-1", appliance_id="appl-1", reference="sub_offline_1"):
        with transaction() as db:
            return entitlements.produce_paid_binding(db, source_reference=reference, source_version=version, customer_id=customer,
                                                     appliance_id=appliance_id, camera_id=camera_id, actor=ACTOR,
                                                     request_id=f"req-{camera_id}-{secrets.token_hex(3)}")

    def _paid_and_bound(self):
        self.assertEqual(self._reconcile()["status"], "active")
        self.assertEqual(self._bind()["status"], "assigned")

    # ------------------------------------------------------------ appliance requests
    def _headers(self, appliance_id="appl-1"):
        return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())),
                "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer secret-{appliance_id}"}

    def _credentials(self, camera_id="cam-1", appliance_id="appl-1"):
        return self.client.post(f"/api/appliance/recordings/{camera_id}/credentials", headers=self._headers(appliance_id))

    def _catalog(self, local_event_id, camera_id="cam-1", appliance_id="appl-1", customer="cust-1"):
        stamp = datetime.now().replace(microsecond=0).isoformat()
        with connection() as db:
            db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
                       "VALUES(?,?,?,?,?,?,'motion',?,?)",
                       (f"ev-{local_event_id}", customer, f"site-{customer}", appliance_id, camera_id, local_event_id, stamp, stamp))
        key = (recording_s3_prefix(customer, f"site-{customer}", appliance_id, camera_id)
               + datetime.fromisoformat(stamp).strftime("%Y/%m/%d") + f"/events/motion_{local_event_id}.mp4")
        return self.client.post(f"/api/appliance/analytics/{camera_id}/events/{local_event_id}/media", headers=self._headers(appliance_id),
                                json={"s3_key": key, "started_at": stamp, "ended_at": stamp, "duration_seconds": 5})

    def _clip(self, local_event_id):
        with connection() as db:
            row = db.execute("SELECT id,retention_policy,retention_days,retention_basis_ref FROM detection_event_media WHERE detection_event_id=?",
                             (f"ev-{local_event_id}",)).fetchone()
        return dict(row) if row else None

    def assertRefusedBeforeSts(self, response, status=403):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(_FakeSTS.calls, [], "STS must not be called for a refused request")

    # ------------------------------------------------------------ positive path
    def test_real_evidence_binding_credential_catalog_and_expiry(self):
        self._paid_and_bound()
        with connection() as db:
            evidence = db.execute("SELECT status,payment_verified,quantity,source_version FROM cloud_gateway_subscription_evidence").fetchone()
            audits = db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='cloud_gateway.subscription_evidence_updated'").fetchone()["n"]
            binding_id = db.execute("SELECT id FROM cloud_gateway_camera_entitlements WHERE camera_id='cam-1'").fetchone()["id"]
        self.assertEqual(tuple(evidence), ("active", 1, 1, "evt_1"))
        self.assertEqual(audits, 1)

        response = self._credentials()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(_FakeSTS.calls), 1)
        policy = json.dumps(json.loads(_FakeSTS.calls[0]["Policy"]))
        self.assertIn("/events/", policy)
        self.assertIn(recording_s3_prefix("cust-1", "site-cust-1", "appl-1", "cam-1"), policy)

        self.assertEqual(self._catalog("e-14").status_code, 200)
        self.assertEqual(self._clip("e-14")["retention_policy"], "cloud_gateway")
        self.assertEqual((self._clip("e-14")["retention_days"], self._clip("e-14")["retention_basis_ref"]), (14, binding_id))
        with transaction() as db:
            cloud_gateway_retention.set_retention(db, "cust-1", 7, actor_email="owner@example.test", actor_role="customer_owner",
                                                  expected_version=1, confirm_reduction=True)
        self.assertEqual(self._catalog("e-7").status_code, 200)
        self.assertEqual(self._clip("e-7")["retention_days"], 7)
        self.assertEqual(self._clip("e-14")["retention_days"], 14)  # frozen

        deleted = []
        with patch.object(sweep, "_delete_recording_object", side_effect=lambda key: deleted.append(key) or True):
            sweep.run_retention_sweep_tick(now=datetime.now() + timedelta(days=8))
            self.assertEqual(len(deleted), 1)
            self.assertIsNone(self._clip("e-7"))
            self.assertIsNotNone(self._clip("e-14"))
            sweep.run_retention_sweep_tick(now=datetime.now() + timedelta(days=15))
        self.assertEqual(len(deleted), 2)
        self.assertIsNone(self._clip("e-14"))

    # ------------------------------------------------------------ evidence negatives
    def test_without_evidence_nothing_binds_or_uploads(self):
        self.assertEqual(self._bind()["status"], "unavailable")
        self.assertRefusedBeforeSts(self._credentials())

    def test_wrong_price_amount_currency_or_interval_never_writes_evidence(self):
        for mutate in (lambda: self.sub["items"]["data"][0]["price"].update(unit_amount=499),
                       lambda: self.sub["items"]["data"][0]["price"].update(currency="eur"),
                       lambda: self.sub["items"]["data"][0]["price"]["recurring"].update(interval="year"),
                       lambda: self.sub["items"]["data"][0]["price"].update(id="price_other"),
                       lambda: self.charge.update(amount=1)):
            self._snapshots()
            mutate()
            with self.assertRaises(producer.EvidenceConflict):
                self._reconcile()
            with connection() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM cloud_gateway_subscription_evidence").fetchone())

    def _refused_after(self, change):
        self._paid_and_bound()
        with connection() as db:
            change(db)
        self.assertRefusedBeforeSts(self._credentials())

    def test_stale_evidence_is_refused(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=entitlements.CLOUD_GATEWAY_EVIDENCE_MAX_AGE_SECONDS + 60)).isoformat()
        self._refused_after(lambda db: db.execute("UPDATE cloud_gateway_subscription_evidence SET updated_at=?", (old,)))

    def test_evidence_hold_is_refused(self):
        self._refused_after(lambda db: db.execute(
            "INSERT INTO cloud_gateway_evidence_holds(stripe_subscription_id,customer_id,event_id,event_created,created_at) "
            "VALUES('sub_offline_1','cust-1','evt_later',200,?)", (datetime.now(timezone.utc).isoformat(),)))

    def test_missing_webhook_audit_is_refused(self):
        self._refused_after(lambda db: db.execute("DELETE FROM audit_logs WHERE action='cloud_gateway.subscription_evidence_updated'"))

    def test_duplicated_webhook_audit_is_refused(self):
        def duplicate(db):
            row = db.execute("SELECT actor_email,actor_role,action,entity_type,entity_id,details_json FROM audit_logs "
                             "WHERE action='cloud_gateway.subscription_evidence_updated'").fetchone()
            db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                       tuple(row) + (datetime.now().isoformat(),))
        self._refused_after(duplicate)

    def test_unverified_payment_is_refused(self):
        self._refused_after(lambda db: db.execute("UPDATE cloud_gateway_subscription_evidence SET payment_verified=0"))

    def test_past_period_end_is_refused(self):
        self._refused_after(lambda db: db.execute("UPDATE cloud_gateway_subscription_evidence SET current_period_end=?",
                                                  (int(time.time()) - 60,)))

    def test_camera_identity_change_is_refused(self):
        self._refused_after(lambda db: db.execute("UPDATE cameras SET device_key='urn:uuid:replaced' WHERE id='cam-1'"))

    def _bind_refused(self, **kwargs):
        try:
            result = self._bind(**kwargs)
        except entitlements.BindingConflict:
            return True
        return result.get("status") != "assigned"

    def test_seat_count_and_other_customer_subscription_are_enforced(self):
        self._paid_and_bound()
        self.assertTrue(self._bind_refused(camera_id="cam-2"))  # quantity 1: one camera
        self.assertTrue(self._bind_refused(camera_id="cam-x", customer="cust-2", appliance_id="appl-2"))
        with connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM cloud_gateway_camera_entitlements").fetchone()["n"], 1)
        self.assertRefusedBeforeSts(self._credentials("cam-2"))
        self.assertRefusedBeforeSts(self._credentials("cam-x", "appl-2"))

    def test_operational_readiness_is_required_with_real_evidence(self):
        self._paid_and_bound()
        with connection() as db:
            db.execute("DELETE FROM recording_retention_sweep_health")
        self.assertRefusedBeforeSts(self._credentials(), status=503)

    # ------------------------------------------------------------ reversal
    def test_refund_retires_the_binding_and_stored_clips_keep_their_retention(self):
        self._paid_and_bound()
        self.assertEqual(self._catalog("e-before").status_code, 200)
        self.assertEqual(self._clip("e-before")["retention_days"], 14)
        self.charge.update(refunded=True, amount_refunded=999)
        self.assertEqual(self._reconcile("evt_refund", 200)["status"], "suspended")
        with connection() as db:
            status = db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE camera_id='cam-1'").fetchone()["status"]
        self.assertNotEqual(status, "active")
        _FakeSTS.calls = []
        self.assertRefusedBeforeSts(self._credentials())
        self.assertEqual(self._catalog("e-after").status_code, 403)
        self.assertIsNone(self._clip("e-after"))
        deleted = []
        with patch.object(sweep, "_delete_recording_object", side_effect=lambda key: deleted.append(key) or True):
            sweep.run_retention_sweep_tick(now=datetime.now() + timedelta(days=13))
            self.assertEqual(deleted, [])
            sweep.run_retention_sweep_tick(now=datetime.now() + timedelta(days=15))
        self.assertEqual(len(deleted), 1)
        self.assertIsNone(self._clip("e-before"))


if __name__ == "__main__":
    unittest.main()
