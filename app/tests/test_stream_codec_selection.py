import pytest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from stream_codec import select_stream_profile
import main


def test_browser_prefers_verified_h264_substream():
    profiles = [
        {'profile_token': 'main', 'stream_role': 'main', 'observed_codec': 'h264'},
        {'profile_token': 'sub', 'stream_role': 'substream', 'observed_codec': 'h264'},
    ]
    assert select_stream_profile(profiles, 'browser_webrtc')['profile_token'] == 'sub'


def test_browser_falls_back_to_h264_main_when_substream_is_h265():
    profiles = [
        {'profile_token': 'main', 'stream_role': 'main', 'observed_codec': 'h264'},
        {'profile_token': 'sub', 'stream_role': 'substream', 'observed_codec': 'h265'},
    ]
    assert select_stream_profile(profiles, 'remote_webrtc')['profile_token'] == 'main'


def test_unverified_advertised_h264_is_not_selected_for_browser():
    assert select_stream_profile(
        [{'profile_token': 'sub', 'stream_role': 'substream',
          'advertised_codec': 'h264', 'observed_codec': 'h265'}],
        'browser_webrtc',
    ) is None


def test_recording_selects_main_even_when_browser_prefers_substream():
    profiles = [
        {'profile_token': 'sub', 'stream_role': 'substream', 'observed_codec': 'h264'},
        {'profile_token': 'main', 'stream_role': 'main', 'observed_codec': 'h265'},
    ]
    assert select_stream_profile(profiles, 'recording')['profile_token'] == 'main'


def test_unknown_stream_purpose_fails_closed():
    with pytest.raises(ValueError):
        select_stream_profile([], 'arbitrary')


def test_codec_related_ffmpeg_failure_requests_a_fresh_probe():
    assert main._is_codec_negotiation_failure(['Decoder (codec hevc) not supported'])
    assert main._is_codec_negotiation_failure(['video codec negotiation failed'])


def test_network_failure_does_not_trigger_codec_revalidation():
    assert not main._is_codec_negotiation_failure(['Connection timed out while opening RTSP input'])


def test_codec_revalidation_after_negotiation_failure_reprobes_profiles_without_logging_credentials(caplog):
    class FakeRows:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class FakeDatabase:
        def __init__(self):
            self.updates = []

        def execute(self, sql, params=()):
            if sql.startswith('SELECT id FROM cameras'):
                return FakeRows([{'id': 'camera-1'}])
            if sql.startswith('SELECT encrypted_blob FROM camera_credentials'):
                return FakeRows([{'encrypted_blob': 'encrypted-camera-credentials'}])
            if sql.startswith('SELECT profile_token,rtsp_uri FROM camera_stream_profiles'):
                return FakeRows([{'profile_token': 'main', 'rtsp_uri': 'rtsp://192.0.2.10/main'}])
            if sql.startswith('UPDATE camera_stream_profiles'):
                self.updates.append(params)
                return FakeRows([])
            raise AssertionError(f'unexpected SQL: {sql}')

    database = FakeDatabase()

    @contextmanager
    def connection():
        yield database

    probe_result = SimpleNamespace(returncode=0, stdout='{"streams":[{"codec_name":"h264"}]}')
    assert main._is_codec_negotiation_failure(['Decoder (codec hevc) not supported'])
    with patch('partner_db.connection', connection), \
         patch('appliance_protocol.decrypt_camera_credentials', return_value={'username': 'cam-user', 'password': 'top-secret'}), \
         patch.object(main.subprocess, 'run', return_value=probe_result) as ffprobe:
        main._revalidate_camera_stream_codecs(1)

    ffprobe.assert_called_once()
    assert ffprobe.call_args.args[0][-1] == 'rtsp://cam-user:top-secret@192.0.2.10/main'
    assert database.updates and database.updates[0][0] == 'h264'
    assert 'top-secret' not in caplog.text
