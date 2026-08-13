"""Phase 2 (docs/AI_HANDOFF.md §8) tests for the two new live-relay control-plane
routes in app/appliance_cloud.py: POST .../live/{camera_id}/session (STS credential
issuance) and POST .../live/{camera_id}/segment-available (manifest bookkeeping).

No real network/AWS call is ever made -- boto3's STS client is mocked. No real
appliance authentication is exercised here (authenticate_appliance is mocked to
return a fixed appliance row) -- that mechanism is already covered elsewhere;
these tests focus on what Phase 2 actually adds: camera-ownership authorization,
STS AssumeRole call shape, and the segment_key prefix check.

Route functions are pulled directly off a freshly-registered FastAPI app and
called as plain Python functions (no TestClient/ASGI needed), matching the
"call the function directly, mock the boundary" approach used throughout the
existing Phase 0 tests.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# See test_live_stream_ffmpeg_characterization.py for why ANYAICAM_PARTNER_DB is
# set explicitly rather than left unset: importing appliance_cloud pulls in
# partner_db, which freezes its DB path on first import for the rest of the
# process -- point it at a safe temp path, not a real project path.
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-phase0-characterization-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
# Importing appliance_cloud constructs a module-level LiveManifestStore. Its
# loading is lazy (see live_manifest.py), so this mainly guards against any
# future regression of that laziness rather than a present-day requirement.
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-phase2-live-manifest-import-guard.json"),
)

from fastapi import FastAPI, HTTPException  # noqa: E402

import appliance_cloud  # noqa: E402
from live_manifest import LiveManifestStore  # noqa: E402


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
appliance_cloud.register_appliance_cloud_routes(_ROUTE_APP, lambda *_args, **_kwargs: "")
live_relay_session = _endpoint(_ROUTE_APP, "/api/appliance/live/{camera_id}/session")
live_relay_segment_available = _endpoint(_ROUTE_APP, "/api/appliance/live/{camera_id}/segment-available")


class FakeRequest:
    """authenticate_appliance() is mocked in every test below, so the header
    values here are never actually read -- this only exists because the route
    functions take a `request` parameter."""

    def __init__(self):
        self.headers = {}
        self.client = None


FAKE_APPLIANCE = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1"}
FAKE_CAMERA = {"id": "camera-1", "customer_id": "customer-1", "site_id": "site-1", "appliance_id": "appliance-1"}


class LiveRelaySessionEndpointTests(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "audit"),
            patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", True),
            patch.object(appliance_cloud, "LIVE_UPLOAD_ROLE_ARN", "arn:aws:iam::880690594006:role/anyaicam-live-relay-upload"),
            patch.object(appliance_cloud, "LIVE_RELAY_S3_BUCKET", "anyaicam2026"),
            patch.object(appliance_cloud, "LIVE_RELAY_AWS_REGION", "us-east-1"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def test_disabled_flag_returns_404_before_touching_boto3(self):
        with patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", False), \
                patch.object(appliance_cloud, "boto3") as mock_boto3:
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(ctx.exception.status_code, 404)
        mock_boto3.client.assert_not_called()

    def test_camera_not_owned_by_appliance_returns_403(self):
        with patch.object(appliance_cloud, "row", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-999")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_missing_configuration_returns_503(self):
        with patch.object(appliance_cloud, "row", return_value=FAKE_CAMERA), \
                patch.object(appliance_cloud, "LIVE_UPLOAD_ROLE_ARN", ""):
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_issues_a_scoped_credential_via_sts_assume_role(self):
        expiration = MagicMock()
        expiration.isoformat.return_value = "2026-01-01T00:15:00"
        mock_sts_client = MagicMock()
        mock_sts_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "secretexample",
                "SessionToken": "tokenexample",
                "Expiration": expiration,
            }
        }
        with patch.object(appliance_cloud, "row", return_value=FAKE_CAMERA), \
                patch.object(appliance_cloud.boto3, "client", return_value=mock_sts_client) as client_factory:
            result = live_relay_session(FakeRequest(), "camera-1")

        client_factory.assert_called_once_with("sts", region_name="us-east-1")
        kwargs = mock_sts_client.assume_role.call_args.kwargs
        self.assertEqual(kwargs["RoleArn"], "arn:aws:iam::880690594006:role/anyaicam-live-relay-upload")
        self.assertEqual(kwargs["DurationSeconds"], 900)

        policy = json.loads(kwargs["Policy"])
        self.assertEqual(
            policy["Statement"][0]["Resource"],
            "arn:aws:s3:::anyaicam2026/live/customer-1/site-1/appliance-1/camera-1/*",
        )
        self.assertEqual(policy["Statement"][0]["Action"], "s3:PutObject")

        self.assertEqual(result["bucket"], "anyaicam2026")
        self.assertEqual(result["key_prefix"], "live/customer-1/site-1/appliance-1/camera-1/")
        self.assertEqual(result["credentials"]["access_key_id"], "AKIAEXAMPLE")
        self.assertEqual(result["credentials"]["session_token"], "tokenexample")

    def test_sts_failure_is_reported_as_502_not_a_raw_exception(self):
        with patch.object(appliance_cloud, "row", return_value=FAKE_CAMERA), \
                patch.object(appliance_cloud.boto3, "client", side_effect=RuntimeError("boom")):
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(ctx.exception.status_code, 502)


class LiveRelaySegmentAvailableEndpointTests(unittest.TestCase):
    """Covers the prefix-authorization fix: a segment_key must fall under the
    exact live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/ prefix the
    calling appliance is authorized for -- not just any prefix at all."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-live-manifest-endpoint-")
        self._store = LiveManifestStore(Path(self._tempdir.name) / "live_manifest.json")
        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "audit"),
            patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", True),
            patch.object(appliance_cloud, "live_manifest_store", self._store),
            patch.object(appliance_cloud, "row", return_value=FAKE_CAMERA),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tempdir.cleanup()

    def test_disabled_flag_returns_404(self):
        with patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", False):
            with self.assertRaises(HTTPException) as ctx:
                live_relay_segment_available(FakeRequest(), "camera-1", {"segment_key": "irrelevant"})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_missing_segment_key_returns_400(self):
        with self.assertRaises(HTTPException) as ctx:
            live_relay_segment_available(FakeRequest(), "camera-1", {})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_exact_camera_prefix_is_accepted(self):
        prefix = appliance_cloud.live_relay_s3_prefix("customer-1", "site-1", "appliance-1", "camera-1")
        result = live_relay_segment_available(
            FakeRequest(), "camera-1", {"segment_key": prefix + "segment_1.ts", "sequence": 1}
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["segment_count"], 1)
        self.assertEqual(self._store.manifest_for("camera-1")["segments"][-1]["key"], prefix + "segment_1.ts")

    def test_another_camera_on_the_same_appliance_is_rejected(self):
        other_camera_prefix = appliance_cloud.live_relay_s3_prefix("customer-1", "site-1", "appliance-1", "camera-2")
        with self.assertRaises(HTTPException) as ctx:
            live_relay_segment_available(
                FakeRequest(), "camera-1", {"segment_key": other_camera_prefix + "segment_1.ts"}
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self._store.manifest_for("camera-1")["segments"], [])

    def test_another_appliance_and_customer_prefix_is_rejected(self):
        forged_prefix = appliance_cloud.live_relay_s3_prefix(
            "other-customer", "other-site", "other-appliance", "camera-1"
        )
        with self.assertRaises(HTTPException) as ctx:
            live_relay_segment_available(FakeRequest(), "camera-1", {"segment_key": forged_prefix + "segment_1.ts"})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self._store.manifest_for("camera-1")["segments"], [])

    def test_camera_not_owned_by_appliance_returns_403_before_prefix_check(self):
        prefix = appliance_cloud.live_relay_s3_prefix("customer-1", "site-1", "appliance-1", "camera-9")
        with patch.object(appliance_cloud, "row", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                live_relay_segment_available(FakeRequest(), "camera-9", {"segment_key": prefix + "segment_1.ts"})
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
