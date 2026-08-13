"""Phase 2 (docs/AI_HANDOFF.md §8) unit tests for app/live_manifest.py.

Pure module -- no FastAPI, no DB, no network. Confirms the rolling-window
manifest behavior the segment-available endpoint relies on.
"""

import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from live_manifest import MAX_SEGMENTS_PER_CAMERA, LiveManifestStore  # noqa: E402


class LiveManifestStoreTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-live-manifest-")
        self.store = LiveManifestStore(Path(self._tempdir.name) / "live_manifest.json")

    def tearDown(self):
        self._tempdir.cleanup()

    def test_constructing_the_store_touches_no_filesystem(self):
        # The backing file/directory must not exist merely from construction --
        # only real use (below) should ever create it.
        never_used = LiveManifestStore(Path(self._tempdir.name) / "unused" / "manifest.json")
        self.assertFalse(never_used.path.exists())
        self.assertFalse(never_used.path.parent.exists())

    def test_unknown_camera_returns_empty_manifest(self):
        manifest = self.store.manifest_for("camera-missing")
        self.assertEqual(manifest["segments"], [])
        self.assertIsNone(manifest["updated_at"])

    def test_record_segment_appends_and_is_visible_via_manifest_for(self):
        self.store.record_segment("camera-1", "live/c/s/a/camera-1/segment_1.ts", sequence=1)
        manifest = self.store.manifest_for("camera-1")
        self.assertEqual(len(manifest["segments"]), 1)
        self.assertEqual(manifest["segments"][0]["key"], "live/c/s/a/camera-1/segment_1.ts")
        self.assertEqual(manifest["segments"][0]["sequence"], 1)

    def test_recording_the_same_key_again_does_not_duplicate_it(self):
        self.store.record_segment("camera-1", "segment_1.ts", sequence=1)
        self.store.record_segment("camera-1", "segment_1.ts", sequence=1)
        self.assertEqual(len(self.store.manifest_for("camera-1")["segments"]), 1)

    def test_rolling_window_keeps_only_the_last_n_segments(self):
        for index in range(MAX_SEGMENTS_PER_CAMERA + 3):
            self.store.record_segment("camera-1", f"segment_{index}.ts", sequence=index)
        segments = self.store.manifest_for("camera-1")["segments"]
        self.assertEqual(len(segments), MAX_SEGMENTS_PER_CAMERA)
        self.assertEqual(segments[-1]["key"], f"segment_{MAX_SEGMENTS_PER_CAMERA + 2}.ts")

    def test_cameras_are_isolated_from_each_other(self):
        self.store.record_segment("camera-1", "camera1-segment.ts")
        self.store.record_segment("camera-2", "camera2-segment.ts")
        self.assertEqual(len(self.store.manifest_for("camera-1")["segments"]), 1)
        self.assertEqual(len(self.store.manifest_for("camera-2")["segments"]), 1)
        self.assertNotEqual(
            self.store.manifest_for("camera-1")["segments"][0]["key"],
            self.store.manifest_for("camera-2")["segments"][0]["key"],
        )

    def test_state_persists_across_store_instances(self):
        self.store.record_segment("camera-1", "segment_1.ts")
        reloaded = LiveManifestStore(self.store.path)
        self.assertEqual(reloaded.manifest_for("camera-1")["segments"][0]["key"], "segment_1.ts")


if __name__ == "__main__":
    unittest.main()
