import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from app.edge_streaming import (
    StreamStatus,
    canonical_hls_url,
    classify_stream,
    playlist_snapshot,
    remove_startup_manifests,
    rtsp_endpoint_ready,
)


class EdgeStreamingTests(unittest.TestCase):
    def test_canonical_hls_url(self):
        self.assertEqual(canonical_hls_url(2), "/static/hls/camera2.m3u8")

    def test_fresh_playlist_is_live_and_old_playlist_is_stale(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            manifest = folder / "camera1.m3u8"
            segment = folder / "camera1_001.ts"
            manifest.write_text("#EXTM3U\n#EXTINF:2,\ncamera1_001.ts\n", encoding="utf-8")
            segment.write_bytes(b"segment")
            os.utime(manifest, (100, 100))
            os.utime(segment, (100, 100))

            fresh = playlist_snapshot(folder, 1, freshness_seconds=15, now=110)
            stale = playlist_snapshot(folder, 1, freshness_seconds=15, now=116)

            self.assertTrue(fresh["fresh"])
            self.assertEqual(classify_stream(fresh, "running"), StreamStatus.LIVE)
            self.assertFalse(stale["fresh"])
            self.assertEqual(classify_stream(stale, "running"), StreamStatus.STALE)

    def test_missing_playlist_preserves_connecting_offline_and_error_states(self):
        missing = {"exists": False, "fresh": False}
        self.assertEqual(classify_stream(missing, "connecting"), StreamStatus.CONNECTING)
        self.assertEqual(classify_stream(missing, "offline"), StreamStatus.OFFLINE)
        self.assertEqual(classify_stream(missing, "error"), StreamStatus.ERROR)

    def test_manifest_without_a_referenced_segment_is_not_live(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / "camera1.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            snapshot = playlist_snapshot(folder, 1, freshness_seconds=15)
            self.assertFalse(snapshot["fresh"])
            self.assertEqual(classify_stream(snapshot, "running"), StreamStatus.STALE)

    def test_startup_cleanup_removes_only_camera_manifests(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            manifest = folder / "camera1.m3u8"
            segment = folder / "camera1_001.ts"
            other = folder / "index.m3u8"
            manifest.write_text("stale", encoding="utf-8")
            segment.write_bytes(b"segment")
            other.write_text("other", encoding="utf-8")

            removed = remove_startup_manifests(folder)

            self.assertEqual(removed, ["camera1.m3u8"])
            self.assertFalse(manifest.exists())
            self.assertTrue(segment.exists())
            self.assertTrue(other.exists())

    def test_rtsp_readiness_detects_reachable_tcp_endpoint(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        accepted = threading.Event()

        def accept_once():
            connection, _ = listener.accept()
            connection.close()
            accepted.set()

        thread = threading.Thread(target=accept_once, daemon=True)
        thread.start()
        try:
            ready, detail = rtsp_endpoint_ready("127.0.0.1", port, timeout=1)
            self.assertTrue(ready, detail)
            self.assertTrue(accepted.wait(1))
        finally:
            listener.close()
            thread.join(timeout=1)


class EdgeStreamingIntegrationSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )

    def test_cloud_runtime_does_not_launch_rtsp_ingestion(self):
        self.assertIn('if RUNTIME_ROLE in {"edge", "combined"}:', self.source)
        self.assertIn("process_supervisor(camera_number, \"live\")", self.source)
        self.assertIn(
            '"rtsp ingestion is disabled outside the edge runtime"', self.source
        )

    def test_live_ui_uses_server_status_and_canonical_hls_url(self):
        self.assertIn("camera.status==='live'", self.source)
        self.assertIn("connectCamera(n,camera.hls_url)", self.source)
        self.assertNotIn("source='/hls/camera", self.source)
        self.assertNotIn("source=`/hls/camera", self.source)


if __name__ == "__main__":
    unittest.main()
