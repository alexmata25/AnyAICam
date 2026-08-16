"""RDM-2 Group 2H (Tier 1): cloud-side schema-continuity check -- confirms
the manifest envelope flowing publish_update.publish() -> updates_storage
-> appliance_cloud.updates_latest() is exactly what the device's REAL
ManifestSource.check_for_manifest() parsing code (and PackageVerifier
signature verification) independently accepts, with no hand-authored
intermediate dict on either side of the seam.

Complements appliance-agent/tests/test_rdm2_e2e_pipeline.py (the primary,
full publish/install/restart/resume/health/report pipeline test, anchored
on the device side). This file is anchored on the CLOUD side and stays
narrowly scoped to the publish -> serve -> device-parse/verify seam,
reusing the SAME cross-package sys.path convention tools/publish_update.py
and its own tests already established.

Route function is pulled directly off a freshly-registered FastAPI app
and called as a plain Python function, matching the approach already
established in test_appliance_updates_latest.py / test_appliance_update_history.py
(the approved Group 2H open-decision resolution: no Starlette
TestClient/httpx).

No real AWS, no real S3, no real network -- a hand-rolled FakeS3Client is
shared between the publisher call and the cloud endpoint's own reads.
"""

import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
APPLIANCE_AGENT_DIR = REPO_ROOT / "appliance-agent"
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2h-cloud-schema-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2h-cloud-schema-live-manifest-import-guard.json"),
)

from fastapi import FastAPI  # noqa: E402

import appliance_cloud  # noqa: E402
import updates_storage  # noqa: E402

import publish_update  # noqa: E402

from anyaicam_agent.updater.s3_source import ManifestSource  # noqa: E402
from anyaicam_agent.updater.verify import PackageVerifier  # noqa: E402

DEVICE_TARGET = "anyaicam-appliance"
DEVICE_CHANNEL = "stable"


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
appliance_cloud.register_appliance_cloud_routes(_ROUTE_APP, lambda *_args, **_kwargs: "")
updates_latest_route = _endpoint(_ROUTE_APP, "/api/appliance/updates/latest")


class FakeRequest:
    def __init__(self):
        self.headers = {}
        self.client = None


FAKE_APPLIANCE = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1"}


class _FakeNoSuchKey(Exception):
    pass


class _FakeExceptions:
    NoSuchKey = _FakeNoSuchKey


class FakeS3Client:
    """Local copy matching the shape already established in
    tools/tests/test_publish_update.py and
    appliance-agent/tests/test_rdm2_e2e_pipeline.py -- kept per-file per
    the approved Group 2H scope (no test-helper consolidation in this
    group)."""

    def __init__(self):
        self.objects = {}
        self.exceptions = _FakeExceptions()

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("not found")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("not found")
        return {}

    def put_object(self, Bucket, Key, Body, ContentType=None, CacheControl=None):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode("utf-8")

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://fake-s3.example/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"


class FakeManifestClient:
    """Stands in for PortalClient in ManifestSource(client) -- .request()
    returns whatever dict it is configured with, so
    ManifestSource.check_for_manifest()'s OWN real parsing/base64-decode
    code runs against the REAL cloud response, with no HTTP transport
    involved (that seam is covered by the primary device-side pipeline
    file; this file's job is the schema, not the transport)."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        return self.response


class Rdm2CloudSchemaTestCase(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.fake_s3 = FakeS3Client()
        self.bucket = "anyaicam-updates-e2e-schema-test"

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.private_key_path = self.root / "operator-key.pem"
        self.private_key_path.write_bytes(private_pem)
        self.trusted_key_path = self.root / "trusted_signing_key.pem"
        self.trusted_key_path.write_bytes(public_pem)

        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "UPDATES_SOURCE_ENABLED", True),
            patch.object(updates_storage, "_client", return_value=self.fake_s3),
            patch.object(updates_storage, "UPDATES_S3_BUCKET", self.bucket),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _publish(self, version="1.1.0", update_id="upd-schema-1", target=DEVICE_TARGET, channel=DEVICE_CHANNEL):
        package_path = self.root / f"package-{version}.tar"
        package_path.write_bytes(f"fake package bytes for version {version}".encode())
        return publish_update.publish(
            s3_client=self.fake_s3, bucket=self.bucket, target=target, channel=channel,
            version=version, update_id=update_id, package_path=package_path,
            platform="linux", architecture="x86_64", private_key_path=self.private_key_path,
        )

    def _real_cloud_response(self, target=DEVICE_TARGET, channel=DEVICE_CHANNEL):
        return updates_latest_route(FakeRequest(), target, channel)


class ManifestVerifiesAgainstTheRealDeviceVerifierTests(Rdm2CloudSchemaTestCase):
    def test_the_real_cloud_response_verifies_against_the_real_package_verifier(self):
        self._publish(version="1.1.0", update_id="upd-schema-1")
        response = self._real_cloud_response()

        verifier = PackageVerifier(self.trusted_key_path)
        signature = base64.b64decode(response["signature"])
        manifest = verifier.verify_manifest(response["manifest"], signature)  # raises on failure

        self.assertEqual(manifest.update_id, "upd-schema-1")
        self.assertEqual(manifest.version, "1.1.0")
        self.assertIn("package_url", response)
        self.assertIn("expires_at", response)


class ManifestSourceParsesTheRealCloudResponseTests(Rdm2CloudSchemaTestCase):
    def test_check_for_manifest_accepts_the_real_cloud_response_unmodified(self):
        self._publish(version="1.2.0", update_id="upd-schema-2")
        response = self._real_cloud_response()

        client = FakeManifestClient(response)
        source = ManifestSource(client)
        result = source.check_for_manifest(current_version="1.0.0", target=DEVICE_TARGET, channel=DEVICE_CHANNEL)

        self.assertIsNotNone(result)
        manifest_dict, signature = result
        self.assertEqual(manifest_dict, response["manifest"])
        self.assertEqual(signature, base64.b64decode(response["signature"]))
        self.assertEqual(source._last_package_url, response["package_url"])
        self.assertEqual(client.calls, [("GET", f"/api/appliance/updates/latest?target={DEVICE_TARGET}&channel={DEVICE_CHANNEL}")])

    def test_a_different_channel_reports_no_update_available_end_to_end(self):
        self._publish(version="1.3.0", update_id="upd-schema-3", channel="stable")
        response = updates_latest_route(FakeRequest(), DEVICE_TARGET, "beta")
        self.assertEqual(response, {"status": "no_update_available"})

        client = FakeManifestClient(response)
        source = ManifestSource(client)
        result = source.check_for_manifest(current_version="1.0.0", target=DEVICE_TARGET, channel="beta")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
