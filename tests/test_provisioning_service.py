"""AWS-authoritative onboarding rework, Phase 1.

app/provisioning_service.py has no FastAPI/DB dependency, so it is tested
directly and fully behaviorally here. app/partner_workspace.py does depend
on FastAPI (not installed in every dev environment this suite runs in --
see the other test_*_characterization.py-style files in this directory for
the same constraint), so its wiring to provisioning_service.py is verified
by reading its source, the same established pattern
test_sidebar_navigation.py and test_camera_provisioning_health.py's
CameraProvisioningIntegrationTests already use in this suite.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from provisioning_service import (  # noqa: E402
    AwsProvisioningBackend,
    MockProvisioningBackend,
    ProvisioningBackendUnavailable,
    UnavailableProvisioningBackend,
)

PARTNER_WORKSPACE_SOURCE = (ROOT / "app" / "partner_workspace.py").read_text(encoding="utf-8")

SAMPLE_ORDER = {
    "customer_id": "cust-1",
    "site_id": "site-1",
    "customer_name": "Jane Doe",
    "company": "Doe Storage",
    "email": "jane@example.com",
    "phone": "555-0100",
    "status": "active",
    "site_name": "Main Warehouse",
    "appliance_type": "AnyAiCam mini PC",
    "camera_count": 5,
    "resolution": "4mp",
    "recording_mode": "motion",
    "retention_days": 14,
    "analytics_addons": ["smart_motion"],
    "deployment_mode": "hybrid",
    "order_reference": "quote-1",
}


class MockBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = MockProvisioningBackend(Path(self.tmp.name) / "mock-aws.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_provision_returns_cloud_id_and_activation_token_from_the_backend(self):
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="key-1")
        self.assertTrue(result["cloud_id"].startswith("AIC-"))
        self.assertTrue(result["activation_token"])
        self.assertEqual(result["provisioning_status"], "provisioned")
        self.assertEqual(result["customer_id"], "cust-1")
        self.assertEqual(result["site_id"], "site-1")

    def test_duplicate_request_with_same_idempotency_key_returns_same_appliance(self):
        first = self.backend.provision(SAMPLE_ORDER, idempotency_key="same-key")
        second = self.backend.provision(SAMPLE_ORDER, idempotency_key="same-key")
        self.assertEqual(first["cloud_id"], second["cloud_id"])
        self.assertEqual(first["appliance_id"], second["appliance_id"])
        self.assertEqual(first["activation_token"], second["activation_token"])

    def test_different_idempotency_keys_produce_different_appliances(self):
        first = self.backend.provision(SAMPLE_ORDER, idempotency_key="key-a")
        second = self.backend.provision(SAMPLE_ORDER, idempotency_key="key-b")
        self.assertNotEqual(first["cloud_id"], second["cloud_id"])

    def test_provision_requires_an_idempotency_key(self):
        with self.assertRaises(ValueError):
            self.backend.provision(SAMPLE_ORDER, idempotency_key="")

    def test_qr_payload_round_trips_to_cloud_id_and_token(self):
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="qr-key")
        cloud_id, token = result["provisioning_qr_payload"].split("|")
        self.assertEqual(cloud_id, result["cloud_id"])
        self.assertEqual(token, result["activation_token"])
        # The same round trip Wizard B's BarcodeDetector JS performs client
        # side -- verify the split values actually authenticate.
        verified = self.backend.verify_link(cloud_id, token)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["cloud_id"], result["cloud_id"])

    def test_entitlement_state_is_present_and_reflects_camera_count(self):
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="ent-key")
        self.assertEqual(result["entitlement"]["camera_limit"], 5)
        self.assertEqual(result["entitlement"]["status"], "active")

    def test_get_status_returns_none_for_unknown_cloud_id(self):
        self.assertIsNone(self.backend.get_status("AIC-DOESNOTEXIST"))

    def test_get_status_never_includes_the_activation_token(self):
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="status-key")
        status = self.backend.get_status(result["cloud_id"])
        self.assertNotIn("activation_token", status)
        self.assertNotIn("_activation_token", status)
        self.assertNotIn(result["activation_token"], str(status))

    def test_verify_link_never_includes_the_activation_token(self):
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="verify-key")
        verified = self.backend.verify_link(result["cloud_id"], result["activation_token"])
        self.assertNotIn("activation_token", verified)
        self.assertNotIn(result["activation_token"], str(verified))

    def test_verify_link_fails_closed_on_wrong_token(self):
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="wrong-token-key")
        self.assertIsNone(self.backend.verify_link(result["cloud_id"], "not-the-real-token"))

    def test_verify_link_fails_closed_on_unknown_cloud_id(self):
        self.assertIsNone(self.backend.verify_link("AIC-UNKNOWN00", "anything"))

    def test_backend_persists_across_new_instances_same_path(self):
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="persist-key")
        reopened = MockProvisioningBackend(self.backend.path)
        status = reopened.get_status(result["cloud_id"])
        self.assertIsNotNone(status)
        self.assertEqual(status["cloud_id"], result["cloud_id"])

    def test_stored_file_never_contains_the_plaintext_token_under_a_public_key(self):
        # The mock's own on-disk store simulates AWS's database (which is
        # allowed to know the token); the only requirement here is that the
        # bytes on disk don't ALSO put the plaintext under any of the public
        # response keys this module promises never to leak it through.
        import json
        result = self.backend.provision(SAMPLE_ORDER, idempotency_key="disk-key")
        raw = json.loads(self.backend.path.read_text(encoding="utf-8"))
        record = raw["appliances_by_cloud_id"][result["cloud_id"]]
        self.assertIn("_activation_token", record)  # the one sanctioned internal field
        for public_key in ("cloud_id", "appliance_id", "customer_id", "site_id",
                            "provisioning_status", "online_status", "last_check_in",
                            "software_version", "entitlement"):
            self.assertNotEqual(record.get(public_key), result["activation_token"])


class UnavailableBackendTests(unittest.TestCase):
    def test_provision_raises_unavailable(self):
        with self.assertRaises(ProvisioningBackendUnavailable):
            UnavailableProvisioningBackend().provision(SAMPLE_ORDER, idempotency_key="x")

    def test_get_status_raises_unavailable(self):
        with self.assertRaises(ProvisioningBackendUnavailable):
            UnavailableProvisioningBackend().get_status("AIC-X")

    def test_verify_link_raises_unavailable(self):
        with self.assertRaises(ProvisioningBackendUnavailable):
            UnavailableProvisioningBackend().verify_link("AIC-X", "token")


class AwsBackendStubTests(unittest.TestCase):
    """Phase 1 does not touch production AWS -- confirm the stub fails
    closed instead of silently doing nothing or falling back to local
    generation."""

    def test_aws_backend_is_not_implemented_and_fails_closed(self):
        backend = AwsProvisioningBackend()
        for call in (
            lambda: backend.provision(SAMPLE_ORDER, idempotency_key="x"),
            lambda: backend.get_status("AIC-X"),
            lambda: backend.verify_link("AIC-X", "token"),
        ):
            with self.assertRaises(ProvisioningBackendUnavailable):
                call()


class PartnerWorkspaceWiringTests(unittest.TestCase):
    """Static source checks: partner_workspace.py depends on FastAPI, which
    is not installed in this dev environment (see module docstring)."""

    def test_local_cloud_id_and_token_generation_is_gone_from_onboarding(self):
        self.assertNotIn("cloud_id='AIC-'+secrets.token_hex(4).upper()", PARTNER_WORKSPACE_SOURCE)
        self.assertNotIn("activation=secrets.token_urlsafe(24)", PARTNER_WORKSPACE_SOURCE)

    def test_onboard_customer_calls_the_provisioning_backend(self):
        self.assertIn("from provisioning_service import get_provisioning_backend, ProvisioningBackendUnavailable", PARTNER_WORKSPACE_SOURCE)
        self.assertIn("get_provisioning_backend().provision(order,idempotency_key=", PARTNER_WORKSPACE_SOURCE)
        self.assertIn("except ProvisioningBackendUnavailable as error: raise HTTPException(status_code=503", PARTNER_WORKSPACE_SOURCE)

    def test_link_endpoint_verifies_against_the_backend_not_only_the_local_hash(self):
        link_start = PARTNER_WORKSPACE_SOURCE.index("def link_customer_appliance")
        link_body = PARTNER_WORKSPACE_SOURCE[link_start:link_start + 2500]
        self.assertIn("get_provisioning_backend().verify_link(cloud_id,token)", link_body)
        self.assertIn("if not verification: raise HTTPException(status_code=403", link_body)

    def test_status_endpoint_refreshes_from_the_backend_and_treats_local_row_as_cache(self):
        status_start = PARTNER_WORKSPACE_SOURCE.index("def customer_setup_status")
        status_body = PARTNER_WORKSPACE_SOURCE[status_start:status_start + 1500]
        self.assertIn("backend.get_status(appliance['cloud_id'])", status_body)
        self.assertIn("UPDATE appliances SET online_status=?,software_version=?,last_check_in=?", status_body)

    def test_order_payload_covers_the_required_provisioning_contract_fields(self):
        onboard_start = PARTNER_WORKSPACE_SOURCE.index("def onboard_customer")
        onboard_body = PARTNER_WORKSPACE_SOURCE[onboard_start:onboard_start + 4000]
        for field in ("customer_name", "site_name", "appliance_type", "camera_count",
                      "resolution", "recording_mode", "retention_days",
                      "analytics_addons", "deployment_mode", "order_reference"):
            self.assertIn(f"'{field}'", onboard_body, f"order payload is missing {field!r}")


if __name__ == "__main__":
    unittest.main()
