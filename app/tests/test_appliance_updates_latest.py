"""RDM-2 Group 2G: tests for the new authenticated manifest endpoint in
app/appliance_cloud.py: GET /api/appliance/updates/latest

Route function is pulled directly off a freshly-registered FastAPI app
and called as a plain Python function, matching the approach already
established in test_live_relay_session_endpoints.py /
test_appliance_update_history.py.

No real AWS, no real S3, no real network -- updates_storage.py's own
internal _client() is patched to return a small hand-rolled FakeS3Client
(matching this project's established preference for real-collaborator
testing wherever feasible: get_latest_manifest()/presign_package_url()
themselves run for REAL against the fake client, only the actual boto3
call is faked).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2g-updates-latest-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2g-live-manifest-import-guard.json"),
)

from fastapi import FastAPI, HTTPException  # noqa: E402

import appliance_cloud  # noqa: E402
import updates_storage  # noqa: E402


class _FakeNoSuchKey(Exception):
    pass


class _FakeExceptions:
    NoSuchKey = _FakeNoSuchKey


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.exceptions = _FakeExceptions()

    def put(self, bucket, key, data):
        self.objects[(bucket, key)] = data if isinstance(data, bytes) else data.encode("utf-8")

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


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
appliance_cloud.register_appliance_cloud_routes(_ROUTE_APP, lambda *_args, **_kwargs: "")
updates_latest = _endpoint(_ROUTE_APP, "/api/appliance/updates/latest")


class FakeRequest:
    def __init__(self):
        self.headers = {}
        self.client = None


FAKE_APPLIANCE = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1"}

GOOD_MANIFEST = {
    "update_id": "upd-1", "version": "1.1.0", "sha256": "a" * 64,
    "target": "anyaicam-appliance", "platform": "linux", "architecture": "x86_64",
    "channel": "stable", "issued_at": "2026-08-20T00:00:00Z", "package_size_bytes": 100,
}
GOOD_ENVELOPE = {"manifest": GOOD_MANIFEST, "signature": "ZmFrZS1zaWduYXR1cmU="}


class UpdatesLatestEndpointTestCase(unittest.TestCase):
    def setUp(self):
        self.fake_s3 = FakeS3Client()
        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "UPDATES_SOURCE_ENABLED", True),
            patch.object(updates_storage, "_client", return_value=self.fake_s3),
            patch.object(updates_storage, "UPDATES_S3_BUCKET", "anyaicam-updates-test"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _publish(self, target="anyaicam-appliance", channel="stable", envelope=None):
        envelope = envelope if envelope is not None else GOOD_ENVELOPE
        self.fake_s3.put("anyaicam-updates-test", f"manifests/{target}/{channel}/latest.json", json.dumps(envelope))


class AuthAndFeatureFlagTests(UpdatesLatestEndpointTestCase):
    def test_auth_failure_propagates_before_anything_else(self):
        def _raise(request):
            raise HTTPException(status_code=401, detail="Appliance authentication headers are required.")
        with patch.object(appliance_cloud, "authenticate_appliance", side_effect=_raise):
            with self.assertRaises(HTTPException) as ctx:
                updates_latest(FakeRequest(), "anyaicam-appliance", "stable")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_disabled_flag_returns_404_before_touching_s3(self):
        self._publish()
        with patch.object(appliance_cloud, "UPDATES_SOURCE_ENABLED", False):
            with self.assertRaises(HTTPException) as ctx:
                updates_latest(FakeRequest(), "anyaicam-appliance", "stable")
        self.assertEqual(ctx.exception.status_code, 404)


class ValidResponseTests(UpdatesLatestEndpointTestCase):
    def test_no_update_available(self):
        result = updates_latest(FakeRequest(), "anyaicam-appliance", "stable")
        self.assertEqual(result, {"status": "no_update_available"})

    def test_valid_manifest_envelope_with_presigned_package_url(self):
        self._publish()
        result = updates_latest(FakeRequest(), "anyaicam-appliance", "stable")
        self.assertEqual(result["manifest"], GOOD_MANIFEST)
        self.assertEqual(result["signature"], GOOD_ENVELOPE["signature"])
        self.assertIn("packages/anyaicam-appliance/stable/1.1.0.tar", result["package_url"])
        self.assertIn("expires_at", result)

    def test_different_target_channel_do_not_see_each_others_publication(self):
        self._publish(target="anyaicam-appliance", channel="stable")
        result = updates_latest(FakeRequest(), "anyaicam-appliance", "beta")
        self.assertEqual(result, {"status": "no_update_available"})


class MalformedInputTests(UpdatesLatestEndpointTestCase):
    def test_invalid_target_grammar_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            updates_latest(FakeRequest(), "../escape", "stable")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_invalid_channel_grammar_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            updates_latest(FakeRequest(), "anyaicam-appliance", "has/slash")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_malformed_envelope_missing_manifest_is_502(self):
        self._publish(envelope={"signature": "abc"})
        with self.assertRaises(HTTPException) as ctx:
            updates_latest(FakeRequest(), "anyaicam-appliance", "stable")
        self.assertEqual(ctx.exception.status_code, 502)

    def test_manifest_missing_version_is_502(self):
        bad_manifest = dict(GOOD_MANIFEST)
        del bad_manifest["version"]
        self._publish(envelope={"manifest": bad_manifest, "signature": "abc"})
        with self.assertRaises(HTTPException) as ctx:
            updates_latest(FakeRequest(), "anyaicam-appliance", "stable")
        self.assertEqual(ctx.exception.status_code, 502)


class NotConfiguredTests(unittest.TestCase):
    """Deliberately does NOT patch updates_storage._client() -- the real
    _client() itself must raise NotConfigured (checked before boto3 is
    ever touched) when the bucket env var is empty."""

    def setUp(self):
        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "UPDATES_SOURCE_ENABLED", True),
            patch.object(updates_storage, "UPDATES_S3_BUCKET", ""),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def test_missing_bucket_configuration_is_503(self):
        with self.assertRaises(HTTPException) as ctx:
            updates_latest(FakeRequest(), "anyaicam-appliance", "stable")
        self.assertEqual(ctx.exception.status_code, 503)


class RegionAndPresignedTtlConfigurationTests(unittest.TestCase):
    """RDM-2 Group 2G: proves ANYAICAM_UPDATES_S3_REGION and
    ANYAICAM_UPDATES_PRESIGNED_TTL_SECONDS are actually threaded through
    by updates_storage.py's own functions -- not merely present as
    strings in the module source. Added in response to a review question
    about whether these two env-var-backed constants are genuinely
    honored; re-verified byte-for-byte (grep + md5sum + cat -A for
    hidden characters) that the module source itself has no typo in
    either name before adding this coverage."""

    def test_region_constant_is_passed_to_the_boto3_client_constructor(self):
        seen_kwargs = {}

        def fake_client(service, **kwargs):
            seen_kwargs.update(kwargs)
            return FakeS3Client()

        with patch.object(updates_storage, "UPDATES_S3_BUCKET", "anyaicam-updates-test"), \
                patch.object(updates_storage, "UPDATES_S3_REGION", "eu-west-2"), \
                patch("boto3.client", side_effect=fake_client):
            updates_storage._client()

        self.assertEqual(seen_kwargs.get("region_name"), "eu-west-2")

    def test_default_presigned_ttl_is_1800_seconds(self):
        with patch.object(updates_storage, "UPDATES_S3_BUCKET", "anyaicam-updates-test"), \
                patch.object(updates_storage, "UPDATES_PRESIGNED_TTL_SECONDS", 1800), \
                patch.object(updates_storage, "_client", return_value=FakeS3Client()):
            url = updates_storage.presign_package_url("anyaicam-appliance", "stable", "1.0.0")

        self.assertIn("ttl=1800", url)

    def test_explicit_ttl_override_is_honored_over_the_default(self):
        with patch.object(updates_storage, "UPDATES_S3_BUCKET", "anyaicam-updates-test"), \
                patch.object(updates_storage, "UPDATES_PRESIGNED_TTL_SECONDS", 1800), \
                patch.object(updates_storage, "_client", return_value=FakeS3Client()):
            url = updates_storage.presign_package_url("anyaicam-appliance", "stable", "1.0.0", ttl_seconds=60)

        self.assertIn("ttl=60", url)
        self.assertNotIn("ttl=1800", url)


if __name__ == "__main__":
    unittest.main()
