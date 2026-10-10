import json
from unittest.mock import patch

from anyaicam_agent import onvif_media


def test_samsung_advertised_h264_but_actual_ffprobe_h265_is_not_browser_compatible():
    profiles = [
        {'profile_token': 'MainStream', 'profile_name': 'MainStream', 'stream_role': 'main',
         'advertised_codec': 'h264', 'observed_codec': 'h265'},
        {'profile_token': 'SubStream', 'profile_name': 'SubStream', 'stream_role': 'substream',
         'advertised_codec': 'h264', 'observed_codec': 'h265'},
    ]
    assert onvif_media.select_browser_stream(profiles) is None
    assert all(item['advertised_codec'] == 'h264' and item['observed_codec'] == 'h265' for item in profiles)


def test_verified_h264_substream_is_preferred_over_h264_main():
    profiles = [
        {'profile_token': 'MainStream', 'stream_role': 'main', 'observed_codec': 'h264'},
        {'profile_token': 'SubStream', 'stream_role': 'substream', 'observed_codec': 'h264'},
    ]
    assert onvif_media.select_browser_stream(profiles)['profile_token'] == 'SubStream'


def test_h265_substream_falls_back_to_verified_h264_main():
    profiles = [
        {'profile_token': 'MainStream', 'stream_role': 'main', 'observed_codec': 'h264'},
        {'profile_token': 'SubStream', 'stream_role': 'substream', 'observed_codec': 'h265'},
    ]
    assert onvif_media.select_browser_stream(profiles)['profile_token'] == 'MainStream'


def test_unverified_advertised_h264_does_not_count_as_browser_compatible():
    profiles = [{'profile_token': 'SubStream', 'stream_role': 'substream',
                 'advertised_codec': 'h264', 'observed_codec': None}]
    assert onvif_media.select_browser_stream(profiles) is None


def test_ffprobe_uses_authorized_credentials_and_returns_only_codec():
    completed = type('Completed', (), {'returncode': 0, 'stdout': json.dumps({'streams': [{'codec_name': 'hevc'}]})})()
    with patch.object(onvif_media.subprocess, 'run', return_value=completed) as run:
        observed = onvif_media.probe_rtsp_codec('rtsp://192.0.2.8:554/sub', 'cam user', 'secret')
    args = run.call_args.args[0]
    assert observed == 'h265'
    assert args[-1] == 'rtsp://cam%20user:secret@192.0.2.8:554/sub'
    assert run.call_args.kwargs['timeout'] == 8


def test_ffprobe_timeout_fails_closed_without_exposing_credentials():
    with patch.object(onvif_media.subprocess, 'run', side_effect=onvif_media.subprocess.TimeoutExpired('ffprobe', 8)):
        assert onvif_media.probe_rtsp_codec('rtsp://192.0.2.8/sub', 'owner', 'secret') is None


def test_profile_parser_keeps_advertised_codec_separate_from_observed_codec():
    response = onvif_media.ElementTree.fromstring('''<root>
      <Profiles token=\"Profile_1\"><Name>MainStream</Name><VideoEncoderConfiguration>
        <Encoding>H264</Encoding><Resolution><Width>1920</Width><Height>1080</Height></Resolution>
      </VideoEncoderConfiguration></Profiles></root>''')
    with patch.object(onvif_media, '_soap_call', return_value=response):
        profiles = onvif_media.get_profiles('http://camera/onvif/media')
    assert profiles == [{'token': 'Profile_1', 'name': 'MainStream', 'advertised_codec': 'h264',
                         'width': 1920, 'height': 1080}]


def test_resolver_detects_samsung_onvif_h264_vs_ffprobe_h265_mismatch():
    profiles = [
        {'token': 'MainStream', 'name': 'MainStream', 'advertised_codec': 'h264', 'width': 1920, 'height': 1080},
        {'token': 'SubStream', 'name': 'SubStream', 'advertised_codec': 'h264', 'width': 640, 'height': 360},
    ]
    with patch.object(onvif_media, '_probe_xaddr', return_value=None), \
         patch.object(onvif_media, 'get_media_service_url', return_value='http://camera/media'), \
         patch.object(onvif_media, 'get_profiles', return_value=profiles), \
         patch.object(onvif_media, 'get_stream_uri', side_effect=[
             'rtsp://192.0.2.8/main', 'rtsp://192.0.2.8/sub',
             ]):
        result = onvif_media.resolve_media_streams('192.0.2.8', 'urn:uuid:samsung-camera',
                                                    ffprobe=lambda uri, username, password: 'h265')
    assert result['status'] == 'resolved'
    assert result['browser_profile_token'] is None
    assert result['compatibility_warning']
    assert [(item['advertised_codec'], item['observed_codec']) for item in result['streams']] == [
        ('h264', 'h265'), ('h264', 'h265'),
    ]
    assert [item['stream_role'] for item in result['streams']] == ['main', 'substream']
