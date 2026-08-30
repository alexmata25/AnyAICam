"""Onboarding Stage 2, appliance side: device_key stability/privacy
(discovery.scan()) and provisioning verification (provisioning.py).
No real network/camera involved -- discovery's own network-facing
helpers (local_networks/_onvif_probe/arp_table/_port) and
provisioning's RTSP socket call are mocked; same style as the existing
test_agent.py (unittest + unittest.mock)."""

import hashlib
import ipaddress
import unittest
from unittest.mock import patch

from anyaicam_agent import discovery, provisioning


class DeviceKeyTests(unittest.TestCase):
    def _scan_with(self, onvif, arp, rtsp_ips=frozenset(), onvif_port_ips=frozenset()):
        network = [ipaddress.ip_network('192.168.1.0/30')]  # hosts: .1, .2
        with patch.object(discovery, 'local_networks', return_value=network), \
             patch.object(discovery, '_onvif_probe', return_value=onvif), \
             patch.object(discovery, 'arp_table', return_value=arp), \
             patch.object(discovery, '_port', side_effect=lambda ip, port, timeout=.25: (port in (554, 8554) and ip in rtsp_ips) or (port in (80, 8000) and ip in onvif_port_ips)):
            return discovery.scan()

    def test_onvif_endpoint_preferred_when_present(self):
        results = self._scan_with(
            onvif={'192.168.1.1': {'scopes': '', 'endpoint': 'urn:uuid:abc-123'}},
            arp={'192.168.1.1': 'AA:BB:CC:DD:EE:FF'},
            rtsp_ips={'192.168.1.1'},  # a real ONVIF device also has RTSP open; scan()'s own filter needs rtsp or non-empty scopes
        )
        self.assertEqual(results[0]['device_key'], 'urn:uuid:abc-123')

    def test_mac_hash_used_when_no_onvif_endpoint(self):
        results = self._scan_with(
            onvif={}, arp={'192.168.1.1': 'AA:BB:CC:DD:EE:FF'}, rtsp_ips={'192.168.1.1'},
        )
        expected = 'mac-' + hashlib.sha256('aa:bb:cc:dd:ee:ff'.encode()).hexdigest()[:32]
        self.assertEqual(results[0]['device_key'], expected)

    def test_raw_mac_never_appears_in_device_key(self):
        results = self._scan_with(
            onvif={}, arp={'192.168.1.1': 'AA:BB:CC:DD:EE:FF'}, rtsp_ips={'192.168.1.1'},
        )
        self.assertNotIn('AA:BB:CC:DD:EE:FF', results[0]['device_key'])
        self.assertNotIn('aa:bb:cc:dd:ee:ff', results[0]['device_key'])

    def test_ip_hash_used_as_last_resort(self):
        results = self._scan_with(onvif={}, arp={}, rtsp_ips={'192.168.1.1'})
        expected = 'ip-' + hashlib.sha256('192.168.1.1'.encode()).hexdigest()[:32]
        self.assertEqual(results[0]['device_key'], expected)
        self.assertNotIn('192.168.1.1', results[0]['device_key'])

    def test_device_key_is_stable_across_rescans_of_the_same_device(self):
        first = self._scan_with(onvif={}, arp={'192.168.1.1': 'AA:BB:CC:DD:EE:FF'}, rtsp_ips={'192.168.1.1'})
        second = self._scan_with(onvif={}, arp={'192.168.1.1': 'AA:BB:CC:DD:EE:FF'}, rtsp_ips={'192.168.1.1'})
        self.assertEqual(first[0]['device_key'], second[0]['device_key'])

    def test_different_devices_get_different_device_keys(self):
        results = self._scan_with(
            onvif={}, arp={'192.168.1.1': 'AA:AA:AA:AA:AA:AA', '192.168.1.2': 'BB:BB:BB:BB:BB:BB'},
            rtsp_ips={'192.168.1.1', '192.168.1.2'},
        )
        keys = {item['device_key'] for item in results}
        self.assertEqual(len(keys), 2)


class VerifyDeviceTests(unittest.TestCase):
    def test_device_not_reachable_fails_without_touching_credentials(self):
        with patch.object(provisioning, 'locate_device', return_value=None), \
             patch.object(provisioning, 'verify_rtsp_credentials') as verify:
            success, message = provisioning.verify_device('some-key', {'username': 'a', 'password': 'b'})
        self.assertFalse(success)
        verify.assert_not_called()

    def test_onvif_only_device_succeeds_without_rtsp_check(self):
        device = {'ip': '192.168.1.5', 'rtsp_support': False}
        with patch.object(provisioning, 'locate_device', return_value=device), \
             patch.object(provisioning, 'verify_rtsp_credentials') as verify:
            success, message = provisioning.verify_device('k', {'username': 'a', 'password': 'b'})
        self.assertTrue(success)
        verify.assert_not_called()

    def test_no_credentials_supplied_succeeds_on_reachability_alone(self):
        device = {'ip': '192.168.1.5', 'rtsp_support': True}
        with patch.object(provisioning, 'locate_device', return_value=device), \
             patch.object(provisioning, 'verify_rtsp_credentials') as verify:
            success, message = provisioning.verify_device('k', None)
        self.assertTrue(success)
        verify.assert_not_called()

    def test_credentials_supplied_delegates_to_rtsp_check(self):
        # path is now DEFAULT_RTSP_STREAM_PATH, not omitted (which used
        # to silently default to verify_rtsp_credentials()'s own bare-
        # root '/') -- see test_rtsp_authentication_classification.py's
        # own dedicated coverage of this fix for the live bug this
        # closes (Samsung, camera device_key ...f6af: every real-camera
        # check was authenticating against '/' instead of the actual
        # stream resource).
        device = {'ip': '192.168.1.5', 'rtsp_support': True}
        with patch.object(provisioning, 'locate_device', return_value=device), \
             patch.object(provisioning, 'verify_rtsp_credentials', return_value=(True, 'ok')) as verify:
            success, message = provisioning.verify_device('k', {'username': 'admin', 'password': 'hunter2'})
        self.assertTrue(success)
        verify.assert_called_once_with('192.168.1.5', 554, 'admin', 'hunter2', path=provisioning.DEFAULT_RTSP_STREAM_PATH)

    def test_a_per_device_discovered_path_is_used_instead_of_the_default(self):
        device = {'ip': '192.168.1.5', 'rtsp_support': True, 'rtsp_path': '/live/ch00_1'}
        with patch.object(provisioning, 'locate_device', return_value=device), \
             patch.object(provisioning, 'verify_rtsp_credentials', return_value=(True, 'ok')) as verify:
            provisioning.verify_device('k', {'username': 'admin', 'password': 'hunter2'})
        verify.assert_called_once_with('192.168.1.5', 554, 'admin', 'hunter2', path='/live/ch00_1')

    def test_message_never_contains_the_credential_values(self):
        # verify_rtsp_credentials() now issues a real RTSP DESCRIBE (see
        # classify_rtsp_authentication()) rather than OPTIONS, so the
        # patch target moved from _rtsp_options to the shared low-level
        # _rtsp_request() helper both now share -- same interception
        # point in spirit, same assertion.
        device = {'ip': '192.168.1.5', 'rtsp_support': True}
        with patch.object(provisioning, 'locate_device', return_value=device), \
             patch.object(provisioning, '_rtsp_request', return_value=(401, 'Basic realm="cam"')):
            success, message = provisioning.verify_device('k', {'username': 'admin', 'password': 'hunter2-secret'})
        self.assertNotIn('hunter2-secret', message)
        self.assertNotIn('admin', message)


class DigestHeaderTests(unittest.TestCase):
    def test_matches_rfc2617_worked_example(self):
        # RFC 2617 section 3.5's own worked example.
        header = provisioning._digest_header(
            'Mufasa', 'Circle Of Life', 'testrealm@host.com', 'dcd98b7102dd2f0e8b11d0f600bfb0c093',
            'GET', '/dir/index.html',
        )
        self.assertIn('username="Mufasa"', header)
        self.assertIn('realm="testrealm@host.com"', header)
        # Not asserting the exact RFC response hash here since that example
        # also includes cnonce/qop which this minimal OPTIONS-only client
        # doesn't send (most ONVIF cameras accept the simpler exchange
        # below) -- this test instead pins the algorithm itself.
        ha1 = hashlib.md5(b'Mufasa:testrealm@host.com:Circle Of Life').hexdigest()
        ha2 = hashlib.md5(b'GET:/dir/index.html').hexdigest()
        expected_response = hashlib.md5(f'{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:{ha2}'.encode()).hexdigest()
        self.assertIn(f'response="{expected_response}"', header)

    def test_credentials_never_appear_in_plaintext_alongside_hash(self):
        header = provisioning._digest_header('admin', 'hunter2', 'realm', 'nonce123', 'OPTIONS', '/')
        self.assertNotIn('hunter2', header)


if __name__ == '__main__':
    unittest.main()
