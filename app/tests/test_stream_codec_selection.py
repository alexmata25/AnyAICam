import pytest

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
