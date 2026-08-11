"""Focused tests for the Stage 1 camera-status diagnostics work
(feature/camera-status-diagnostics): the additive last_exit_code/last_error/
last_error_at fields on /api/cameras/status, the stderr redaction/bounding
helpers used before that data is ever exposed, and the /camera-health page
that displays it.

app/main.py cannot be imported directly in this environment without
triggering its own runtime side effects (StaticFiles mounts against
directories that don't exist in a bare test sandbox, DB setup, etc.), so
every test here either reads app/main.py as plain source text -- the
existing convention in this repo, see tests/test_edge_streaming.py's
EdgeStreamingIntegrationSourceTests and app/tests/test_login_csrf.py's
test_login_page_wires_hidden_csrf_field_to_cookie -- or, for the one class
that needs to exercise real behavior rather than just check source text,
extracts the exact, self-contained helper-function source (verbatim, not
retyped) and execs it in an isolated namespace. No production code was
changed to make any of this possible.
"""

import re
import unittest
from pathlib import Path

MAIN_PY = (Path(__file__).resolve().parents[1] / "app" / "main.py")


class CameraStatusApiSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_status_endpoint_includes_diagnostic_fields(self):
        self.assertIn(
            '"last_exit_code": camera_process_state[camera_number].get("last_exit_code"),',
            self.source,
        )
        self.assertIn(
            '"last_error": camera_process_state[camera_number].get("last_error"),',
            self.source,
        )
        self.assertIn(
            '"last_error_at": camera_process_state[camera_number].get("last_error_at"),',
            self.source,
        )

    def test_status_endpoint_preserves_existing_fields(self):
        existing_fields = (
            '"camera": camera_number,',
            '"online": streaming,',
            '"stream": "online" if streaming else camera_process_state[camera_number]["live"],',
            '"recording": camera_process_state[camera_number]["recording"],',
            '"last_stream_update_seconds": manifest_age,',
            '"reconnects": camera_reconnect_counts[camera_number],',
        )
        for existing_field in existing_fields:
            with self.subTest(field=existing_field):
                self.assertIn(existing_field, self.source)


class CameraStreamErrorHelperTests(unittest.TestCase):
    """Extracts the real _redact_camera_stream_error/_bounded_camera_error
    source verbatim from app/main.py and execs it in isolation, so this
    exercises the actual shipped implementation rather than a hand-copied
    duplicate that could silently drift from it."""

    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")
        start_marker = "CAMERA_STDERR_MAX_LINES = 20"
        end_marker = "return _redact_camera_stream_error(joined)[:CAMERA_ERROR_MAX_CHARS]"
        start = source.index(start_marker)
        end = source.index(end_marker) + len(end_marker)
        snippet = source[start:end]

        import asyncio
        import subprocess

        namespace = {"re": re, "asyncio": asyncio, "subprocess": subprocess}
        exec(compile(snippet, "app/main.py (extracted camera-error helpers)", "exec"), namespace)

        cls.redact = staticmethod(namespace["_redact_camera_stream_error"])
        cls.bounded = staticmethod(namespace["_bounded_camera_error"])
        cls.max_chars = namespace["CAMERA_ERROR_MAX_CHARS"]

    def test_redact_strips_full_rtsp_url_including_userinfo(self):
        message = (
            "Connection refused: "
            "rtsp://admin:supersecret@192.168.1.5:554/Streaming/Channels/101 "
            "unreachable"
        )
        redacted = self.redact(message)
        self.assertNotIn("admin", redacted)
        self.assertNotIn("supersecret", redacted)
        self.assertNotIn("192.168.1.5", redacted)
        self.assertIn("rtsp://<redacted>", redacted)
        self.assertIn("Connection refused:", redacted)
        self.assertIn("unreachable", redacted)

    def test_bounded_error_truncates_to_max_length(self):
        long_line = "x" * (self.max_chars + 250)
        result = self.bounded([long_line])
        self.assertLessEqual(len(result), self.max_chars)
        self.assertEqual(result, long_line[: self.max_chars])

    def test_bounded_error_preserves_plain_nonsensitive_text(self):
        result = self.bounded(["Input/output error", "Connection timed out"])
        self.assertEqual(result, "Input/output error Connection timed out")

    def test_bounded_error_also_redacts_credentials_from_tail_lines(self):
        result = self.bounded([
            "opening input",
            "rtsp://viewer:hunter2@10.0.0.9:554/live failed authentication",
        ])
        self.assertNotIn("hunter2", result)
        self.assertNotIn("10.0.0.9", result)
        self.assertIn("rtsp://<redacted>", result)


class CameraHealthPageSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_page_includes_last_error_column_and_cell(self):
        self.assertIn("<th>Last error</th>", self.source)
        self.assertIn('id="camera-health-error-{camera_number}"', self.source)

    def test_javascript_reads_last_error_field(self):
        self.assertIn("camera.last_error", self.source)
        self.assertIn(
            "document.getElementById(`camera-health-error-${number}`)",
            self.source,
        )

    def test_existing_health_columns_and_ids_still_present(self):
        existing_markers = (
            "<th>Live</th>",
            "<th>Recording</th>",
            "<th>AI</th>",
            "<th>Reconnects</th>",
            'id="camera-health-live-{camera_number}"',
            'id="camera-health-recording-{camera_number}"',
            'id="camera-health-ai-{camera_number}"',
            'id="camera-health-age-{camera_number}"',
            'id="camera-health-reconnects-{camera_number}"',
        )
        for existing in existing_markers:
            with self.subTest(existing=existing):
                self.assertIn(existing, self.source)

class CameraDetailLiveViewSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = MAIN_PY.read_text(encoding="utf-8")
        cls.security_source = (
            MAIN_PY.parent / "cloud_security.py"
        ).read_text(encoding="utf-8")

    def test_camera_detail_uses_static_hls_path(self):
        self.assertIn(
            "source='/static/hls/camera{camera_number}.m3u8'",
            self.main_source,
        )
        self.assertNotIn(
            "source='/hls/camera{camera_number}.m3u8'",
            self.main_source,
        )

    def test_csp_allows_hls_js_cdn(self):
        self.assertIn(
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            self.security_source,
        )
if __name__ == "__main__":
    unittest.main()
