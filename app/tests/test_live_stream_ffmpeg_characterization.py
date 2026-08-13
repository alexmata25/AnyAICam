"""Phase 0 characterization tests (docs/AI_HANDOFF.md §8).

Locks down the CURRENT, working local camera pipeline in app/main.py before
any appliance-to-AWS live-media transport work begins, so future changes to
that transport cannot silently drift the existing FFmpeg command or the
LAN-only RTSP URL construction without a test consciously being updated.

These tests do not spawn a real ffmpeg process, do not contact a real
camera, and do not modify any production code. subprocess.Popen is mocked
so no external process is ever started.

Run from the `app` directory, per docs/MODULARIZATION.md's existing
convention:
    python -m unittest tests.test_live_stream_ffmpeg_characterization -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Importing `main` freezes cloud_config.settings (and partner_db's sqlite
# path) for the rest of THIS PROCESS on first import -- other test files
# that import it later cannot change it. Using setdefault() with an
# explicit, writable temp path here (instead of leaving it unset) means
# whichever test file happens to import `main` first in a shared discovery
# run does so safely, without touching a real project/deployment path.
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-phase0-characterization-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault("ANYAICAM_RUNTIME_ROLE", "edge")

import main  # noqa: E402  (path setup must happen first)


class CameraUrlCharacterizationTests(unittest.TestCase):
    """camera_url() (app/main.py) builds an rtsp:// URL exclusively from
    CAMERA{n}_HOST/USERNAME/PASSWORD/PATH environment variables local to the
    process running it. It has no awareness of RUNTIME_ROLE -- the function
    itself will happily build a LAN rtsp:// URL regardless of where it runs.
    The only thing that currently prevents this from being invoked on a
    "cloud" role box is that lifespan() never schedules
    start_live_stream()/start_recording() when RUNTIME_ROLE == "cloud" (see
    test_runtime_role_lifespan_characterization.py). This construction is
    only appropriate for edge/combined runtime roles, where the process is
    physically on the customer LAN.
    """

    CAMERA_NUMBER = 97  # unused by the app; avoids clobbering real CAMERA1-4 env vars

    def _keys(self):
        return [
            f"CAMERA{self.CAMERA_NUMBER}_HOST",
            f"CAMERA{self.CAMERA_NUMBER}_USERNAME",
            f"CAMERA{self.CAMERA_NUMBER}_PASSWORD",
            f"CAMERA{self.CAMERA_NUMBER}_PATH",
        ]

    def setUp(self):
        self._env_backup = {key: os.environ.get(key) for key in self._keys()}

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_builds_rtsp_url_from_local_camera_env_vars_only(self):
        host, user, password, path = "192.168.0.157", "admin", "p@ss w/ord", "/Streaming/Channels/101"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_HOST"] = host
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_USERNAME"] = user
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_PASSWORD"] = password
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_PATH"] = path

        url = main.camera_url(self.CAMERA_NUMBER)

        expected = f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{host}:554{path}"
        self.assertEqual(url, expected)
        # Characterizes today's LAN-only assumption: the host segment is
        # whatever CAMERA{n}_HOST says, unvalidated, unrouted -- exactly the
        # behavior that breaks when this function runs somewhere that is
        # not the customer LAN (docs/AI_HANDOFF.md §3).
        self.assertIn(host, url)

    def test_path_defaults_when_camera_path_not_configured(self):
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_HOST"] = "10.0.0.5"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_USERNAME"] = "user"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_PASSWORD"] = "pass"
        os.environ.pop(f"CAMERA{self.CAMERA_NUMBER}_PATH", None)

        url = main.camera_url(self.CAMERA_NUMBER)

        self.assertTrue(url.endswith("/Streaming/Channels/101"))

    def test_missing_required_host_raises_keyerror(self):
        os.environ.pop(f"CAMERA{self.CAMERA_NUMBER}_HOST", None)
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_USERNAME"] = "user"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_PASSWORD"] = "pass"

        with self.assertRaises(KeyError):
            main.camera_url(self.CAMERA_NUMBER)


class StartLiveStreamFfmpegCharacterizationTests(unittest.TestCase):
    """Locks down the exact FFmpeg invocation start_live_stream() (app/main.py)
    uses today, so a future live-relay transport change must consciously
    touch this test rather than silently drift the working local pipeline.
    subprocess.Popen is mocked -- no real ffmpeg process is spawned and no
    real camera is contacted.
    """

    CAMERA_NUMBER = 98

    def setUp(self):
        self._env_backup = {
            key: os.environ.get(key)
            for key in (
                f"CAMERA{self.CAMERA_NUMBER}_HOST",
                f"CAMERA{self.CAMERA_NUMBER}_USERNAME",
                f"CAMERA{self.CAMERA_NUMBER}_PASSWORD",
                f"CAMERA{self.CAMERA_NUMBER}_PATH",
            )
        }
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_HOST"] = "10.20.30.40"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_USERNAME"] = "svc"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_PASSWORD"] = "secret"
        os.environ.pop(f"CAMERA{self.CAMERA_NUMBER}_PATH", None)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _captured_command(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            main.start_live_stream(self.CAMERA_NUMBER)
            self.assertEqual(mock_popen.call_count, 1)
            (command,), _kwargs = mock_popen.call_args
            return command

    def test_command_starts_with_ffmpeg_and_the_camera_rtsp_url(self):
        command = self._captured_command()
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn(main.camera_url(self.CAMERA_NUMBER), command)

    def test_video_and_optional_audio_are_mapped(self):
        command = self._captured_command()
        self.assertIn("0:v:0", command)
        self.assertIn("0:a:0?", command)
        self.assertEqual(command[command.index("0:v:0") - 1], "-map")
        self.assertEqual(command[command.index("0:a:0?") - 1], "-map")

    def test_audio_codec_is_aac_96k_mono_48khz(self):
        command = self._captured_command()
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], "96k")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ar") + 1], "48000")

    def test_output_is_hls_with_2s_segments_and_list_size_5(self):
        command = self._captured_command()
        self.assertEqual(command[command.index("-f") + 1], "hls")
        self.assertEqual(command[command.index("-hls_time") + 1], "2")
        self.assertEqual(command[command.index("-hls_list_size") + 1], "5")

    def test_segment_container_is_current_mpegts_default_not_fmp4(self):
        # Validated against code (docs/AI_HANDOFF.md §8, AUDIO VALIDATION /
        # ACTUAL HLS FORMAT): there is no -hls_segment_type override anywhere
        # in this command, so FFmpeg's default HLS container (MPEG-TS, .ts
        # segments) is what's actually produced today -- not CMAF/fMP4/.m4s.
        # This test fails the moment that changes, which is deliberate: V1
        # of the live-relay work must not silently change this container.
        command = self._captured_command()
        self.assertNotIn("-hls_segment_type", command)
        self.assertNotIn("fmp4", command)

    def test_current_hls_flags_are_delete_segments_and_append_list(self):
        command = self._captured_command()
        self.assertEqual(
            command[command.index("-hls_flags") + 1],
            "delete_segments+append_list",
        )

    def test_output_target_is_the_local_per_camera_manifest(self):
        command = self._captured_command()
        self.assertTrue(command[-1].endswith(f"camera{self.CAMERA_NUMBER}.m3u8"))


if __name__ == "__main__":
    unittest.main()
