"""Regression coverage for browser-facing live HLS delivery.

The application module has substantial import-time runtime setup, so these
checks follow the repository's existing camera diagnostics tests and inspect
the shipped source without importing the full server.
"""

import unittest
from pathlib import Path


MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"


class HlsDeliverySourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")
        live_player_start = cls.source.index("function connectCamera(n)")
        live_player_end_marker = "for(let n=1;n<=4;n++)connectCamera(n);"
        live_player_end = cls.source.index(live_player_end_marker, live_player_start)
        cls.live_player_source = cls.source[
            live_player_start : live_player_end + len(live_player_end_marker)
        ]

    def test_canonical_hls_path_matches_ffmpeg_and_live_players(self):
        self.assertIn('HLS_URL_PREFIX = "/static/hls"', self.source)
        self.assertIn('HLS_FOLDER = Path("/app/static/hls")', self.source)
        self.assertIn('source=`/static/hls/camera${n}.m3u8`', self.source)
        self.assertIn(
            "source='/static/hls/camera{camera_number}.m3u8'",
            self.source,
        )

    def test_all_hls_responses_disable_browser_and_cdn_caching(self):
        self.assertIn(
            'if request.url.path.startswith(f"{HLS_URL_PREFIX}/"):',
            self.source,
        )
        self.assertIn(
            'HLS_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"',
            self.source,
        )
        self.assertIn(
            'response.headers["Cache-Control"] = HLS_CACHE_CONTROL',
            self.source,
        )
        self.assertIn(
            'response.headers["CDN-Cache-Control"] = "no-store"',
            self.source,
        )

    def test_camera_page_navigation_and_day_events_remain_present(self):
        self.assertIn(
            'class="camera-view" style="cursor:pointer" onclick="openCameraPage({n})"',
            self.source,
        )
        self.assertIn(
            "function openCameraPage(n){location.href=`/camera/${n}`}",
            self.source,
        )
        self.assertIn('id="camera-events-list"', self.source)

    def test_live_page_uses_three_segment_window_sync_and_starts_playback(self):
        self.assertIn(
            "new Hls({liveSyncDurationCount:1,liveMaxLatencyDurationCount:2})",
            self.live_player_source,
        )
        self.assertIn(
            "hls.on(Hls.Events.MANIFEST_PARSED,()=>{video.play().catch(()=>"
            "setState(n,'Click play to start'))})",
            self.live_player_source,
        )
        self.assertNotIn("currentTime", self.live_player_source)


if __name__ == "__main__":
    unittest.main()
