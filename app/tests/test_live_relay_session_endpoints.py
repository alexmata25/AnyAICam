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
import sqlite3
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
set_live_relay_pilot = _endpoint(_ROUTE_APP, "/api/admin/appliances/{appliance_id}/live-relay-pilot")


class FakeRequest:
    """authenticate_appliance() is mocked in every test below, so the header
    values here are never actually read -- this only exists because the route
    functions take a `request` parameter."""

    def __init__(self):
        self.headers = {}
        self.client = None


class _ConnectionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *_args):
        return False


# Phase 6f (docs/AI_HANDOFF.md §8): live_relay_pilot=1 here so every existing
# positive-path test below (written before Phase 6f) continues to reach the
# same code path it always has -- pilot-gating itself is exercised by its own
# dedicated test class (LiveRelayPilotGatingTests) using locally-constructed
# appliance dicts, not by varying this shared fixture.
FAKE_APPLIANCE = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1", "live_relay_pilot": 1}
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


class LiveRelayPilotGatingTests(unittest.TestCase):
    """Phase 6f (docs/AI_HANDOFF.md §8): live_relay_session() is the single
    authoritative capability gate -- effective enablement requires BOTH the
    global ANYAICAM_LIVE_RELAY_ENABLED flag AND this appliance's own
    live_relay_pilot column. Neither alone is sufficient, and the fail-closed
    default (column absent/0) must never be treated as eligible."""

    def setUp(self):
        self._patches = [
            patch.object(appliance_cloud, "audit"),
            patch.object(appliance_cloud, "row", return_value=FAKE_CAMERA),
            patch.object(appliance_cloud, "LIVE_UPLOAD_ROLE_ARN", "arn:aws:iam::880690594006:role/anyaicam-live-relay-upload"),
            patch.object(appliance_cloud, "LIVE_RELAY_S3_BUCKET", "anyaicam2026"),
            patch.object(appliance_cloud, "LIVE_RELAY_AWS_REGION", "us-east-1"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @staticmethod
    def _appliance(**overrides):
        return {**FAKE_APPLIANCE, **overrides}

    def test_global_on_pilot_on_succeeds(self):
        expiration = MagicMock()
        expiration.isoformat.return_value = "2026-01-01T00:15:00"
        mock_sts_client = MagicMock()
        mock_sts_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE", "SecretAccessKey": "secretexample",
                "SessionToken": "tokenexample", "Expiration": expiration,
            }
        }
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=self._appliance(live_relay_pilot=1)), \
                patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", True), \
                patch.object(appliance_cloud.boto3, "client", return_value=mock_sts_client):
            result = live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(result["status"], "accepted")

    def test_global_on_pilot_off_returns_404(self):
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=self._appliance(live_relay_pilot=0)), \
                patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", True), \
                patch.object(appliance_cloud, "boto3") as mock_boto3:
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "Live relay is not enabled.")
        mock_boto3.client.assert_not_called()

    def test_global_on_pilot_key_missing_defaults_to_ineligible(self):
        # Simulates a not-yet-migrated-in-this-process-image row shape or any
        # caller that omits the key entirely -- .get() must fail closed.
        appliance_without_pilot_key = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1"}
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=appliance_without_pilot_key), \
                patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", True):
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_global_off_pilot_on_still_returns_404(self):
        # The global flag remains the master kill switch regardless of
        # per-appliance pilot eligibility.
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=self._appliance(live_relay_pilot=1)), \
                patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", False), \
                patch.object(appliance_cloud, "boto3") as mock_boto3:
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(ctx.exception.status_code, 404)
        mock_boto3.client.assert_not_called()

    def test_global_off_pilot_off_returns_404(self):
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=self._appliance(live_relay_pilot=0)), \
                patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", False):
            with self.assertRaises(HTTPException) as ctx:
                live_relay_session(FakeRequest(), "camera-1")
        self.assertEqual(ctx.exception.status_code, 404)


class SetLiveRelayPilotAdminRouteTests(unittest.TestCase):
    """Phase 6f: POST /api/admin/appliances/{appliance_id}/live-relay-pilot --
    the mechanism, not any real activation (never invoked against a real
    appliance by this test suite, only an in-memory fixture row)."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE appliances(id TEXT PRIMARY KEY, live_relay_pilot INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("INSERT INTO appliances(id, live_relay_pilot) VALUES(?,?)", ("appliance-1", 0))
        self.db.commit()
        self._patches = [
            patch.object(appliance_cloud, "require_partner_access", return_value={"email": "admin@example.com", "role": "administrator"}),
            patch.object(appliance_cloud, "audit"),
            patch.object(appliance_cloud, "connection", return_value=_ConnectionContext(self.db)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.db.close()

    def _pilot_value(self, appliance_id="appliance-1"):
        row = self.db.execute("SELECT live_relay_pilot FROM appliances WHERE id=?", (appliance_id,)).fetchone()
        return row["live_relay_pilot"] if row else None

    def test_administrator_can_enable_pilot(self):
        result = set_live_relay_pilot(FakeRequest(), "appliance-1", {"enabled": True})
        self.assertEqual(result, {"appliance_id": "appliance-1", "live_relay_pilot": True})
        self.assertEqual(self._pilot_value(), 1)
        appliance_cloud.audit.assert_called_once()

    def test_administrator_can_disable_pilot(self):
        self.db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id='appliance-1'")
        self.db.commit()
        result = set_live_relay_pilot(FakeRequest(), "appliance-1", {"enabled": False})
        self.assertEqual(result, {"appliance_id": "appliance-1", "live_relay_pilot": False})
        self.assertEqual(self._pilot_value(), 0)

    def test_toggling_twice_is_idempotent(self):
        set_live_relay_pilot(FakeRequest(), "appliance-1", {"enabled": True})
        result = set_live_relay_pilot(FakeRequest(), "appliance-1", {"enabled": True})
        self.assertEqual(result["live_relay_pilot"], True)
        self.assertEqual(self._pilot_value(), 1)

    def test_non_administrator_role_is_rejected(self):
        with patch.object(
            appliance_cloud, "require_partner_access",
            side_effect=HTTPException(status_code=403, detail="Administrator access required."),
        ):
            with self.assertRaises(HTTPException) as ctx:
                set_live_relay_pilot(FakeRequest(), "appliance-1", {"enabled": True})
        self.assertEqual(ctx.exception.status_code, 403)
        appliance_cloud.audit.assert_not_called()

    def test_unknown_appliance_id_returns_404_and_does_not_audit(self):
        # Deliberately does NOT inherit revoke_appliance()'s silent-no-op
        # shape -- an unknown appliance_id must 404, and the audit call must
        # never fire for a change that didn't happen.
        with self.assertRaises(HTTPException) as ctx:
            set_live_relay_pilot(FakeRequest(), "appliance-does-not-exist", {"enabled": True})
        self.assertEqual(ctx.exception.status_code, 404)
        appliance_cloud.audit.assert_not_called()
        self.assertEqual(self._pilot_value("appliance-1"), 0)  # untouched


if __name__ == "__main__":
    unittest.main()
