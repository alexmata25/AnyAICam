"""Regression coverage for the read-only ONVIF media-URI resolution
path (GetCapabilities -> GetProfiles -> GetStreamUri) that closes the
confirmed-live Samsung gap: cameras.onvif_endpoint (what camera_url()'s
_provisioned_camera_stream() actually reads -- app/main.py) was never
populated by anything, so camera_url() always fell through to an unset
legacy CAMERA{n}_HOST env var and FFmpeg was never attempted.

Network I/O is mocked throughout -- urllib.request.urlopen for the SOAP
calls, and the raw UDP socket for the targeted WS-Discovery probe --
so these tests run with zero real network activity, same style as
test_rtsp_authentication_classification.py (a loopback fake, not a
real device)."""

import io
import socket
import unittest
import urllib.error
from unittest.mock import patch

from anyaicam_agent import onvif_media


def _soap(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'<SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>'
    ).encode('utf-8')


CAPABILITIES_OK = _soap(
    '<tds:GetCapabilitiesResponse><tds:Capabilities>'
    '<tt:Media><tt:XAddr>http://192.0.2.10/onvif/media_service</tt:XAddr></tt:Media>'
    '</tds:Capabilities></tds:GetCapabilitiesResponse>'
)

PROFILES_MULTI = _soap(
    '<trt:GetProfilesResponse>'
    '<trt:Profiles token="Profile_2" fixed="true"><tt:Name>SubStream</tt:Name></trt:Profiles>'
    '<trt:Profiles token="Profile_1" fixed="true"><tt:Name>MainStream</tt:Name></trt:Profiles>'
    '</trt:GetProfilesResponse>'
)

PROFILES_SINGLE_UNNAMED = _soap(
    '<trt:GetProfilesResponse>'
    '<trt:Profiles token="Profile_1"><tt:Name></tt:Name></trt:Profiles>'
    '</trt:GetProfilesResponse>'
)

PROFILES_EMPTY = _soap('<trt:GetProfilesResponse></trt:GetProfilesResponse>')


def _stream_uri(uri: str) -> bytes:
    return _soap(f'<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>')


STREAM_URI_CLEAN = _stream_uri('rtsp://192.0.2.10:554/Streaming/Channels/101')
STREAM_URI_WITH_CREDENTIALS = _stream_uri('rtsp://admin:hunter2@192.0.2.10:554/Streaming/Channels/101')
STREAM_URI_NON_RTSP = _stream_uri('http://192.0.2.10/not-a-stream')
STREAM_URI_EMPTY = _soap('<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri></tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>')

AUTH_FAULT_BODY = _soap(
    '<SOAP-ENV:Fault><SOAP-ENV:Reason><SOAP-ENV:Text>Sender not Authorized</SOAP-ENV:Text></SOAP-ENV:Reason></SOAP-ENV:Fault>'
)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code, body=b''):
    error = urllib.error.HTTPError('http://device', code, 'error', {}, io.BytesIO(body))
    return error


class ResolveMediaUriTests(unittest.TestCase):
    """End-to-end resolve_media_uri() behavior, with _probe_xaddr always
    mocked out (its own device_key-matching logic is covered separately
    below) so these focus purely on the SOAP GetCapabilities/GetProfiles/
    GetStreamUri chain and its outcomes."""

    def setUp(self):
        patcher = patch.object(onvif_media, '_probe_xaddr', return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _resolve(self, responses):
        with patch('urllib.request.urlopen', side_effect=responses):
            return onvif_media.resolve_media_uri('192.0.2.10', 'urn:uuid:test-device')

    # ---- success

    def test_get_profiles_and_stream_uri_success(self):
        result = self._resolve([_FakeResponse(CAPABILITIES_OK), _FakeResponse(PROFILES_SINGLE_UNNAMED), _FakeResponse(STREAM_URI_CLEAN)])
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['rtsp_uri'], 'rtsp://192.0.2.10:554/Streaming/Channels/101')
        self.assertEqual(result['profile_token'], 'Profile_1')
        self.assertFalse(result['had_embedded_credentials'])

    def test_multiple_profiles_deterministically_selects_the_main_one(self):
        """SubStream is listed FIRST, MainStream second -- the 'main'
        naming signal must still win over list order."""
        result = self._resolve([_FakeResponse(CAPABILITIES_OK), _FakeResponse(PROFILES_MULTI), _FakeResponse(STREAM_URI_CLEAN)])
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['profile_token'], 'Profile_1')
        self.assertEqual(result['profile_name'], 'MainStream')
        self.assertEqual(result['profile_count'], 2)

    def test_result_is_deterministic_and_side_effect_free_across_repeated_calls(self):
        """resolve_media_uri() itself performs no persistence -- calling
        it twice against identical responses must yield an identical
        result both times (the idempotency/no-duplication guarantee for
        the actual persisted write lives in the cloud-side endpoint,
        covered in app/tests/test_camera_discovery_provisioning.py)."""
        responses = [_FakeResponse(CAPABILITIES_OK), _FakeResponse(PROFILES_SINGLE_UNNAMED), _FakeResponse(STREAM_URI_CLEAN)]
        first = self._resolve(list(responses))
        second = self._resolve(list(responses))
        self.assertEqual(first, second)

    # ---- no usable URI

    def test_no_uri_returned(self):
        result = self._resolve([_FakeResponse(CAPABILITIES_OK), _FakeResponse(PROFILES_SINGLE_UNNAMED), _FakeResponse(STREAM_URI_EMPTY)])
        self.assertEqual(result['status'], 'no_uri')
        self.assertIsNone(result['rtsp_uri'])

    def test_no_profiles_returned(self):
        result = self._resolve([_FakeResponse(CAPABILITIES_OK), _FakeResponse(PROFILES_EMPTY)])
        self.assertEqual(result['status'], 'no_profiles')
        self.assertIsNone(result['rtsp_uri'])

    # ---- malformed / non-RTSP

    def test_malformed_non_rtsp_uri_is_never_persisted_as_is(self):
        result = self._resolve([_FakeResponse(CAPABILITIES_OK), _FakeResponse(PROFILES_SINGLE_UNNAMED), _FakeResponse(STREAM_URI_NON_RTSP)])
        self.assertEqual(result['status'], 'invalid_uri')
        self.assertIsNone(result['rtsp_uri'])

    # ---- authentication required

    def test_authentication_challenge_on_capabilities_stops_with_auth_required(self):
        result = self._resolve([_http_error(401)])
        self.assertEqual(result['status'], 'auth_required')
        self.assertIsNone(result['rtsp_uri'])

    def test_authentication_soap_fault_on_profiles_stops_with_auth_required(self):
        result = self._resolve([_FakeResponse(CAPABILITIES_OK), _http_error(500, AUTH_FAULT_BODY)])
        self.assertEqual(result['status'], 'auth_required')
        self.assertIsNone(result['rtsp_uri'])

    def test_unreachable_device_is_not_misreported_as_auth_required(self):
        result = self._resolve([urllib.error.URLError('timed out')])
        self.assertEqual(result['status'], 'unreachable')

    # ---- credential-bearing URI sanitization

    def test_credential_bearing_uri_is_sanitized_before_it_ever_leaves_this_module(self):
        result = self._resolve([_FakeResponse(CAPABILITIES_OK), _FakeResponse(PROFILES_SINGLE_UNNAMED), _FakeResponse(STREAM_URI_WITH_CREDENTIALS)])
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['rtsp_uri'], 'rtsp://192.0.2.10:554/Streaming/Channels/101')
        self.assertTrue(result['had_embedded_credentials'])
        serialized = repr(result)
        self.assertNotIn('admin', serialized)
        self.assertNotIn('hunter2', serialized)


class SanitizeRtspUriTests(unittest.TestCase):
    def test_strips_username_and_password(self):
        clean, had_credentials = onvif_media.sanitize_rtsp_uri('rtsp://admin:hunter2@192.0.2.10:554/ch1')
        self.assertEqual(clean, 'rtsp://192.0.2.10:554/ch1')
        self.assertTrue(had_credentials)
        self.assertNotIn('admin', clean)
        self.assertNotIn('hunter2', clean)

    def test_username_only_is_also_stripped(self):
        clean, had_credentials = onvif_media.sanitize_rtsp_uri('rtsp://viewer@192.0.2.10/ch1')
        self.assertEqual(clean, 'rtsp://192.0.2.10/ch1')
        self.assertTrue(had_credentials)

    def test_uri_without_credentials_is_unchanged(self):
        clean, had_credentials = onvif_media.sanitize_rtsp_uri('rtsp://192.0.2.10:554/ch1')
        self.assertEqual(clean, 'rtsp://192.0.2.10:554/ch1')
        self.assertFalse(had_credentials)

    def test_non_rtsp_scheme_is_rejected(self):
        self.assertEqual(onvif_media.sanitize_rtsp_uri('http://192.0.2.10/ch1'), (None, False))

    def test_empty_or_none_is_rejected(self):
        self.assertEqual(onvif_media.sanitize_rtsp_uri(''), (None, False))
        self.assertEqual(onvif_media.sanitize_rtsp_uri(None), (None, False))

    def test_malformed_uri_with_no_host_is_rejected(self):
        self.assertEqual(onvif_media.sanitize_rtsp_uri('rtsp:///ch1'), (None, False))


class SelectProfileTests(unittest.TestCase):
    def test_empty_list_returns_none(self):
        self.assertIsNone(onvif_media.select_profile([]))

    def test_single_profile_is_selected(self):
        profile = {'token': 'Profile_1', 'name': 'Stream'}
        self.assertEqual(onvif_media.select_profile([profile]), profile)

    def test_main_named_profile_wins_regardless_of_order(self):
        sub = {'token': 'Profile_2', 'name': 'SubStream'}
        main = {'token': 'Profile_1', 'name': 'MainStream'}
        self.assertEqual(onvif_media.select_profile([sub, main]), main)

    def test_falls_back_to_first_listed_when_no_profile_is_named_main(self):
        first = {'token': 'Profile_1', 'name': 'High'}
        second = {'token': 'Profile_2', 'name': 'Low'}
        self.assertEqual(onvif_media.select_profile([first, second]), first)


class DeviceServiceUrlTests(unittest.TestCase):
    def test_prefers_a_valid_discovered_xaddr(self):
        url = onvif_media.device_service_url('192.0.2.10', xaddr='http://192.0.2.10:8080/onvif/device_service')
        self.assertEqual(url, 'http://192.0.2.10:8080/onvif/device_service')

    def test_falls_back_to_the_onvif_standard_path_when_no_xaddr(self):
        url = onvif_media.device_service_url('192.0.2.10', xaddr=None)
        self.assertEqual(url, 'http://192.0.2.10/onvif/device_service')

    def test_falls_back_when_xaddr_is_not_a_url(self):
        url = onvif_media.device_service_url('192.0.2.10', xaddr='not-a-url')
        self.assertEqual(url, 'http://192.0.2.10/onvif/device_service')


class ProbeXaddrDeviceKeyAssociationTests(unittest.TestCase):
    """_probe_xaddr() must only ever attribute an XAddr to the physical
    device whose own advertised endpoint reference equals the target
    device_key -- never to some other device that merely replied from
    the same IP (spoofed or stale ARP/IP reuse), and never to the right
    IP with the wrong device_key. Network I/O is faked via a stand-in
    socket object -- no real UDP traffic."""

    class _FakeDiscoverySocket:
        def __init__(self, datagrams):
            self._datagrams = list(datagrams)

        def settimeout(self, value):
            pass

        def sendto(self, data, address):
            pass

        def recvfrom(self, bufsize):
            if not self._datagrams:
                raise socket.timeout()
            return self._datagrams.pop(0)

        def close(self):
            pass

    def _probe_match(self, endpoint_uuid, xaddr):
        text = (
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
            f'<e:Header><w:MessageID>uuid:x</w:MessageID><w:Action>ProbeMatches</w:Action></e:Header>'
            f'<e:Body><d:ProbeMatches><d:ProbeMatch>'
            f'<w:EndpointReference><w:Address>{endpoint_uuid}</w:Address></w:EndpointReference>'
            f'<d:XAddrs>{xaddr}</d:XAddrs>'
            '</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>'
        ).encode()
        return text

    def test_matching_device_key_yields_its_xaddr(self):
        target = 'urn:uuid:aaaa-1111'
        datagram = (self._probe_match(target, 'http://192.0.2.10/onvif/device_service'), ('192.0.2.10', 3702))
        with patch('socket.socket', return_value=self._FakeDiscoverySocket([datagram])):
            xaddr = onvif_media._probe_xaddr('192.0.2.10', target, timeout=0.1)
        self.assertEqual(xaddr, 'http://192.0.2.10/onvif/device_service')

    def test_reply_from_a_different_device_key_is_ignored(self):
        """Same IP, but a different physical device's endpoint reference
        -- must never be attributed to our target device_key."""
        target = 'urn:uuid:aaaa-1111'
        other_device = self._probe_match('urn:uuid:bbbb-2222', 'http://192.0.2.10/onvif/device_service')
        with patch('socket.socket', return_value=self._FakeDiscoverySocket([(other_device, ('192.0.2.10', 3702))])):
            xaddr = onvif_media._probe_xaddr('192.0.2.10', target, timeout=0.1)
        self.assertIsNone(xaddr)

    def test_reply_from_a_different_ip_is_ignored_even_with_matching_device_key(self):
        target = 'urn:uuid:aaaa-1111'
        datagram = (self._probe_match(target, 'http://198.51.100.5/onvif/device_service'), ('198.51.100.5', 3702))
        with patch('socket.socket', return_value=self._FakeDiscoverySocket([datagram])):
            xaddr = onvif_media._probe_xaddr('192.0.2.10', target, timeout=0.1)
        self.assertIsNone(xaddr)

    def test_no_reply_at_all_returns_none(self):
        with patch('socket.socket', return_value=self._FakeDiscoverySocket([])):
            xaddr = onvif_media._probe_xaddr('192.0.2.10', 'urn:uuid:aaaa-1111', timeout=0.1)
        self.assertIsNone(xaddr)


if __name__ == '__main__':
    unittest.main()
