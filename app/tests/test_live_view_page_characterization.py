"""Phase 0 characterization tests (docs/AI_HANDOFF.md §8).

Locks down the current browser-side live-view assumptions rendered by
home() (app/main.py, the "/" route) -- a pure source-string
characterization of the generated HTML/JS. home() is called directly (it
takes no request/session argument) rather than going through FastAPI
routing or an authenticated HTTP client, so this test changes no frontend
behavior, needs no test server, and cannot itself alter what a browser
sees.

Named to sort alphabetically AFTER test_cloud_readiness.py on purpose:
test_cloud_readiness.py deletes and re-initializes its own temp sqlite
file at module import time, and partner_db.py's initialize_database()
(which applies schema migrations) only ever runs on the FIRST import of
partner_db in the process -- a file that imports `main` (and so
partner_db, transitively) earlier in discovery order would freeze that
first-import to a different temp path than the one test_cloud_readiness.py
just deleted, and its migrated-tables assertion would fail. This is a
pre-existing fragility in how the test suite shares process state across
files, not something this file's tests need to fix -- keeping this file's
name after "cloud_readiness" alphabetically avoids triggering it.

Run from the `app` directory, per docs/MODULARIZATION.md's existing
convention:
    python -m unittest tests.test_live_view_page_characterization -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# See test_live_stream_ffmpeg_characterization.py for why ANYAICAM_PARTNER_DB
# is set explicitly here rather than left unset: `main` freezes it for the
# rest of the process on first import, so whichever test file imports it
# first in a shared discovery run must not point it at a real project path.
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-phase0-characterization-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault("ANYAICAM_RUNTIME_ROLE", "edge")

import main  # noqa: E402  (path setup must happen first)


class BrowserLiveViewCharacterizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # main.CAMERA_COUNT defaults to 4 and the grid always renders camera 1
        # first, so "camera1" below is a safe, currently-true assumption.
        assert main.CAMERA_COUNT >= 1
        cls.page = main.home()

    def test_local_hls_source_pattern_is_static_hls_path(self):
        self.assertIn("/static/hls/camera${n}.m3u8", self.page)

    def test_hls_js_live_sync_settings_are_preserved(self):
        self.assertIn("liveSyncDurationCount:2", self.page)
        self.assertIn("liveMaxLatencyDurationCount:5", self.page)

    def test_primary_live_view_video_starts_muted(self):
        self.assertIn(
            '<video id="camera1" autoplay muted controls playsinline',
            self.page,
        )

    def test_unmute_control_toggles_video_muted(self):
        self.assertIn("toggleCameraAudio", self.page)
        self.assertIn("video.muted=!video.muted", self.page)


if __name__ == "__main__":
    unittest.main()
