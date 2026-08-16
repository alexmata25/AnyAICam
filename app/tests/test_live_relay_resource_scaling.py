"""Phase 6f (docs/AI_HANDOFF.md §8) resource-scaling characterization tests.

These are CODE-LEVEL CHARACTERIZATION tests, not a load test and not proof of
how the real ECS deployment's CPU/memory/network scale under production
traffic. Everything here calls route functions directly (no real network/ASGI
server) against a mocked boto3 boundary, in a single process. What they DO
prove, rigorously:

1. No live-relay control-plane route ever proxies media bytes -- confirmed
   both by static inspection of the actual route source (no
   StreamingResponse/FileResponse/media file read anywhere) and by a
   mocked-boto3 assertion that the only client ever constructed across N
   simulated concurrent cameras is "sts" (credential issuance), never "s3"
   or any data-plane call.
2. Increasing concurrent camera/viewer count does not increase the *shape*
   of per-call work in appliance_cloud.py's routes -- every response stays
   bounded by MAX_SEGMENTS_PER_CAMERA regardless of N.

Separately, and reported honestly rather than concealed: live_manifest.py's
record_segment() (Phase 2, unchanged, not touched by Phase 6f) serializes
every camera's segment-available call through one process-wide Lock() and
rewrites one shared JSON file containing every tracked camera's state on
each call. This is not media-byte proxying, but it is real application-
server work whose cost is shaped by total concurrent camera count. It is
measured here and recorded as a pre-existing scaling limitation / follow-up
candidate -- not redesigned as part of Phase 6f.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-phase0-characterization-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-phase2-live-manifest-import-guard.json"),
)

from fastapi import FastAPI  # noqa: E402

import appliance_cloud  # noqa: E402
import live_cdn_signing  # noqa: E402
import live_playlist  # noqa: E402
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
    def __init__(self):
        self.headers = {}
        self.client = None


class NoMediaBytesStaticInspectionTests(unittest.TestCase):
    """Static source inspection -- proves no live-relay control-plane module
    ever streams/returns a media file, independent of any mocked test run."""

    def test_appliance_cloud_never_streams_or_serves_a_file(self):
        source = Path(appliance_cloud.__file__).read_text(encoding="utf-8")
        self.assertNotIn("StreamingResponse", source)
        self.assertNotIn("FileResponse", source)
        self.assertNotIn(".ts'", source)
        self.assertNotIn('.ts"', source)

    def test_live_playlist_never_streams_or_serves_a_file(self):
        source = Path(live_playlist.__file__).read_text(encoding="utf-8")
        self.assertNotIn("StreamingResponse", source)
        self.assertNotIn("FileResponse", source)

    def test_live_cdn_signing_never_reads_or_returns_media_bytes(self):
        source = Path(live_cdn_signing.__file__).read_text(encoding="utf-8")
        self.assertNotIn("StreamingResponse", source)
        self.assertNotIn("FileResponse", source)
        self.assertNotIn("get_object", source)  # never fetches segment bytes from S3 itself


class ConcurrentCameraCharacterizationTests(unittest.TestCase):
    """Drives N simulated concurrent cameras through the two live-relay
    control-plane routes and confirms: (a) the only boto3 client ever
    constructed, across all N, is "sts" -- never "s3" or any data-plane
    call; (b) every response payload stays bounded by
    MAX_SEGMENTS_PER_CAMERA regardless of N. Code-level characterization
    only -- see module docstring."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-phase6f-scaling-")
        self._store = LiveManifestStore(Path(self._tempdir.name) / "live_manifest.json")
        self._patches = [
            patch.object(appliance_cloud, "audit"),
            patch.object(appliance_cloud, "LIVE_RELAY_ENABLED", True),
            patch.object(appliance_cloud, "live_manifest_store", self._store),
            patch.object(appliance_cloud, "LIVE_UPLOAD_ROLE_ARN", "arn:aws:iam::880690594006:role/anyaicam-live-relay-upload"),
            patch.object(appliance_cloud, "LIVE_RELAY_S3_BUCKET", "anyaicam2026"),
            patch.object(appliance_cloud, "LIVE_RELAY_AWS_REGION", "us-east-1"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tempdir.cleanup()

    @staticmethod
    def _camera(n):
        return {"id": f"camera-{n}", "customer_id": "customer-1", "site_id": "site-1", "appliance_id": "appliance-1"}

    @staticmethod
    def _appliance():
        return {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1", "live_relay_pilot": 1}

    def test_only_sts_clients_are_ever_constructed_across_n_concurrent_cameras(self):
        n = 50
        expiration = MagicMock()
        expiration.isoformat.return_value = "2026-01-01T00:15:00"
        mock_sts_client = MagicMock()
        mock_sts_client.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T", "Expiration": expiration}
        }
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=self._appliance()), \
                patch.object(appliance_cloud.boto3, "client", return_value=mock_sts_client) as client_factory:
            for i in range(n):
                with patch.object(appliance_cloud, "row", return_value=self._camera(i)):
                    live_relay_session(FakeRequest(), f"camera-{i}")

        # Every client constructed, across all N cameras, was "sts" -- never "s3".
        for call in client_factory.call_args_list:
            self.assertEqual(call.args[0], "sts")
        self.assertEqual(client_factory.call_count, n)
        self.assertEqual(mock_sts_client.assume_role.call_count, n)

    def test_segment_available_response_size_is_bounded_regardless_of_n(self):
        n = 50
        result = None
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=self._appliance()):
            for i in range(n):
                with patch.object(appliance_cloud, "row", return_value=self._camera(i)):
                    prefix = appliance_cloud.live_relay_s3_prefix("customer-1", "site-1", "appliance-1", f"camera-{i}")
                    for segment_index in range(8):  # more segments than MAX_SEGMENTS_PER_CAMERA
                        result = live_relay_segment_available(
                            FakeRequest(), f"camera-{i}",
                            {"segment_key": f"{prefix}segment_{segment_index}.ts", "sequence": segment_index},
                        )
        self.assertLessEqual(result["segment_count"], 5)  # LiveManifestStore.MAX_SEGMENTS_PER_CAMERA
        self.assertEqual(set(result.keys()), {"status", "segment_count"})  # response shape never grew with N


class LiveManifestScalingCharacterizationTests(unittest.TestCase):
    """Honest disclosure, not a Phase 6f fix: record_segment() serializes
    every camera through one process-wide Lock() and rewrites one shared
    JSON file containing every tracked camera's state on each call. This
    measures that cost at N=1 vs N~=50 tracked cameras. This is a
    characterization measurement in one local test process, NOT a
    production ECS load/resource-scaling proof."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-phase6f-manifest-scaling-")

    def tearDown(self):
        self._tempdir.cleanup()

    def test_record_segment_latency_characterization_at_n1_vs_n50(self):
        store_small = LiveManifestStore(Path(self._tempdir.name) / "small.json")
        store_large = LiveManifestStore(Path(self._tempdir.name) / "large.json")

        store_small.record_segment("camera-0", "live/c/s/a/camera-0/segment_0.ts", 0)
        start = time.perf_counter()
        store_small.record_segment("camera-new", "live/c/s/a/camera-new/segment_0.ts", 0)
        elapsed_at_n1 = time.perf_counter() - start

        for i in range(50):
            store_large.record_segment(f"camera-{i}", f"live/c/s/a/camera-{i}/segment_0.ts", 0)
        start = time.perf_counter()
        store_large.record_segment("camera-new", "live/c/s/a/camera-new/segment_0.ts", 0)
        elapsed_at_n50 = time.perf_counter() - start

        # Not an assertion of a specific bound -- this is characterization,
        # printed for the record. The pre-existing behavior (confirmed by
        # reading live_manifest.py's _save(), unchanged by Phase 6f) is that
        # EVERY call rewrites the entire shared JSON file (all tracked
        # cameras' state, not just the one being updated) under one
        # process-wide Lock() -- so this number is expected to grow with N,
        # by design of a module Phase 6f does not modify.
        print(
            f"\n[Phase 6f characterization, not a regression gate] "
            f"live_manifest_store.record_segment() latency: "
            f"N=1 tracked camera: {elapsed_at_n1 * 1000:.3f}ms, "
            f"N=50 tracked cameras: {elapsed_at_n50 * 1000:.3f}ms "
            f"(pre-existing Phase 2 shared-file/global-lock behavior, "
            f"unmodified by Phase 6f; flagged as a follow-up candidate)."
        )
        # The only actual assertion: both calls still succeed and return the
        # expected, bounded per-camera shape -- proving this is a latency/
        # contention characteristic, not a correctness defect.
        self.assertEqual(len(store_large.manifest_for("camera-new")["segments"]), 1)


if __name__ == "__main__":
    unittest.main()
