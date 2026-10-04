"""Camera onboarding blockers found by the Samsung clean-machine acceptance
(2026-10-04), against an isolated simulator: ONVIF camera serving
rtsp://<camera>:8554/testcamera, reached over a layer-3 (WireGuard) link.

1. RTSP port: verify_device() always tested port 554 (and Hikvision's
   default path), so a camera on 8554 failed with "connection refused".
2. No MAC: binding required an ARP MAC; a camera reached over layer 3 has
   none, so it stayed camera_not_bound even though its ONVIF UUID
   (device_key) is a stable identity.
(The third blocker, credential-free cameras, is in the VMS:
app/tests/test_credential_free_camera_stream.py.)"""
import ipaddress
import json
import tempfile
import unittest
from pathlib import Path

from anyaicam_agent import discovery, provisioning
from anyaicam_agent.camera_binding import (CameraBindingStore, LocalVmsStatusReader, auto_bind_discovered_cameras,
                                           normalize_mac, reconcile_cloud_cameras)
from test_rtsp_authentication_classification import _FakeRtspCamera

UUID = 'urn:uuid:5a7e0c3f-0000-4000-8000-000000000001'


# ------------------------------------------------------------------ 1. RTSP port

class DiscoveryRecordsThePortTests(unittest.TestCase):
    def scan_with(self, open_ports):
        originals = (discovery.local_networks, discovery._onvif_probe, discovery.arp_table, discovery._port)
        self.addCleanup(lambda: (setattr(discovery, 'local_networks', originals[0]), setattr(discovery, '_onvif_probe', originals[1]),
                                 setattr(discovery, 'arp_table', originals[2]), setattr(discovery, '_port', originals[3])))
        discovery.local_networks = lambda networks=None: [ipaddress.ip_network('192.0.2.8/30')]
        discovery._onvif_probe = lambda timeout=2: {}
        discovery.arp_table = lambda: {}
        discovery._port = lambda ip, port, timeout=.25: (str(ip), port) in open_ports
        return {item['ip']: item for item in discovery.scan()}

    def test_a_camera_serving_only_8554_is_recorded_with_port_8554(self):
        found = self.scan_with({('192.0.2.9', 8554)})
        self.assertEqual((found['192.0.2.9']['rtsp_support'], found['192.0.2.9']['rtsp_port']), (True, 8554))

    def test_554_is_still_preferred_when_both_answer(self):
        found = self.scan_with({('192.0.2.9', 554), ('192.0.2.9', 8554)})
        self.assertEqual(found['192.0.2.9']['rtsp_port'], 554)

    def test_no_rtsp_port_means_no_rtsp_support(self):
        found = self.scan_with({('192.0.2.9', 80)})
        self.assertNotIn('192.0.2.9', found)  # neither RTSP nor ONVIF: not a camera candidate


class VerificationUsesTheCamerasPortTests(unittest.TestCase):
    def setUp(self):
        self._orig = (provisioning.locate_device, provisioning._resolve_stream_uri)
        self.addCleanup(lambda: (setattr(provisioning, 'locate_device', self._orig[0]),
                                 setattr(provisioning, '_resolve_stream_uri', self._orig[1])))

    def verify(self, device, credentials=None):
        provisioning.locate_device = lambda *a, **k: device
        return provisioning.verify_device(device.get('device_key', 'k'), credentials or {'username': 'viewer', 'password': 'right'})

    def test_the_discovered_non_default_port_is_used(self):
        with _FakeRtspCamera('basic', username='viewer', password='right') as camera:  # listens on a random port, not 554
            ok, message = self.verify({'ip': '127.0.0.1', 'rtsp_support': True, 'rtsp_port': camera.port, 'device_key': 'k'})
        self.assertTrue(ok, message)
        self.assertNotIn('right', message)

    def test_the_cameras_own_onvif_stream_uri_sets_port_and_path(self):
        with _FakeRtspCamera('digest', username='viewer', password='right') as camera:
            provisioning._resolve_stream_uri = lambda ip, key, username=None, password=None: f'rtsp://127.0.0.1:{camera.port}/testcamera'
            ok, message = self.verify({'ip': '127.0.0.1', 'rtsp_support': True, 'rtsp_port': 554, 'onvif_support': True,
                                       'device_key': UUID})
            uri = camera.last_describe_uri
        self.assertTrue(ok, message)
        self.assertTrue(uri.endswith(f':{camera.port}/testcamera'), uri)

    def test_a_wrong_password_is_still_refused_on_the_right_port(self):
        with _FakeRtspCamera('digest', username='viewer', password='right') as camera:
            ok, message = self.verify({'ip': '127.0.0.1', 'rtsp_support': True, 'rtsp_port': camera.port, 'device_key': 'k'},
                                      {'username': 'viewer', 'password': 'wrong'})
        self.assertFalse(ok)
        self.assertNotIn('wrong', message)

    def test_onvif_is_asked_without_credentials(self):
        seen = []
        import anyaicam_agent.onvif_media as onvif_media
        original = onvif_media.resolve_media_uri
        onvif_media.resolve_media_uri = lambda ip, key, **kwargs: seen.append(kwargs) or {'status': 'auth_required'}
        self.addCleanup(setattr, onvif_media, 'resolve_media_uri', original)
        provisioning._resolve_stream_uri = self._orig[1]
        with self.assertRaisesRegex(ValueError, 'requires ONVIF credentials'):
            provisioning.stream_target({'ip': '127.0.0.1', 'onvif_support': True, 'rtsp_port': 8554}, UUID)
        self.assertEqual(seen, [{'username': None, 'password': None}])

    def test_a_record_without_port_information_keeps_the_previous_default(self):
        self.assertEqual(provisioning.stream_target({'ip': '127.0.0.1'}, 'k')[:2], (554, provisioning.DEFAULT_RTSP_STREAM_PATH))

    def test_anonymous_camera_is_accepted_only_after_real_describe(self):
        with _FakeRtspCamera('open') as camera:
            ok, message = self.verify({'ip': '127.0.0.1', 'rtsp_support': True, 'rtsp_port': camera.port, 'device_key': 'k'},
                                      {'username': '', 'password': ''})
        self.assertTrue(ok, message)
        self.assertEqual(camera.requests_seen, 2)  # OPTIONS plus actual media DESCRIBE

    def test_protected_camera_cannot_be_provisioned_without_credentials(self):
        with _FakeRtspCamera('basic', username='viewer', password='secret') as camera:
            ok, message = self.verify({'ip': '127.0.0.1', 'rtsp_support': True, 'rtsp_port': camera.port, 'device_key': 'k'},
                                      {'username': '', 'password': ''})
        self.assertFalse(ok)
        self.assertIn('no credentials', message)
        self.assertEqual(camera.requests_seen, 2)

    def test_stream_endpoint_must_match_camera_and_cannot_embed_credentials(self):
        for uri in ('rtsp://192.0.2.2:8554/live', 'rtsp://user:secret@127.0.0.1/live',
                    'https://127.0.0.1/live', 'rtsp://127.0.0.1:0/live', 'rtsp://127.0.0.1/live#fragment'):
            with self.subTest(uri=uri):
                with self.assertRaisesRegex(ValueError, 'invalid'):
                    provisioning.stream_target({'ip': '127.0.0.1', 'rtsp_uri': uri}, 'key')


# ------------------------------------------------------------------ 2. binding without a MAC

def _cloud(cloud_id='cam-1', camera_number=1, device_key=UUID):
    return {'id': cloud_id, 'name': 'Camera', 'camera_number': camera_number, 'device_key': device_key, 'status': 'configured'}


def _found(device_key=UUID, mac='Unknown'):
    return {'device_key': device_key, 'mac_address': mac, 'ip': '192.168.253.10'}


class BindingWithoutMacTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = CameraBindingStore(self.root / 'bindings.json')

    def test_a_camera_without_an_arp_mac_is_bound_by_its_stable_identity(self):
        self.assertEqual(auto_bind_discovered_cameras([_cloud()], [_found()], self.store), ['cam-1'])
        [binding] = self.store.bindings()
        self.assertEqual((binding['camera_number'], binding.get('mac_address', ''), binding['device_key']), (1, '', UUID))
        # Idempotent: an unchanged binding is not rewritten.
        approved = binding['approved_at']
        self.assertEqual(auto_bind_discovered_cameras([_cloud()], [_found()], self.store), [])
        self.assertEqual(self.store.bindings()[0]['approved_at'], approved)

    def test_two_valid_cameras_can_never_claim_the_same_identity(self):
        auto_bind_discovered_cameras([_cloud()], [_found()], self.store)
        with self.assertRaisesRegex(ValueError, 'already bound to another cloud camera'):
            self.store.bind('cam-2', 2, '', device_key=UUID, valid_cloud_camera_ids={'cam-1', 'cam-2'})
        self.assertEqual([b['cloud_camera_id'] for b in self.store.bindings()], ['cam-1'])

    def test_an_orphaned_identity_binding_is_superseded_like_a_mac_one(self):
        auto_bind_discovered_cameras([_cloud('old-cam')], [_found()], self.store)
        bound = auto_bind_discovered_cameras([_cloud('new-cam')], [_found()], self.store)
        self.assertEqual(bound, ['new-cam'])
        self.assertEqual([b['cloud_camera_id'] for b in self.store.bindings()], ['new-cam'])

    def test_a_direct_bind_with_neither_mac_nor_identity_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, 'MAC'):
            self.store.bind('cam-1', 1, 'Unknown')

    def test_macless_binding_requires_a_canonical_non_nil_onvif_uuid(self):
        for key in ('ip-' + 'a' * 32, 'mac-' + 'a' * 32, 'urn:uuid:1111',
                    'urn:uuid:00000000-0000-0000-0000-000000000000'):
            with self.subTest(key=key):
                self.assertEqual(auto_bind_discovered_cameras([_cloud(device_key=key)], [_found(device_key=key)], self.store), [])
                self.assertEqual(self.store.bindings(), [])

    def test_a_camera_with_a_valid_mac_binds_by_mac_as_before(self):
        auto_bind_discovered_cameras([_cloud()], [_found(mac='14:2F:FD:A2:F6:AF')], self.store)
        [binding] = self.store.bindings()
        self.assertEqual(binding['mac_address'], normalize_mac('14:2F:FD:A2:F6:AF'))
        with self.assertRaisesRegex(ValueError, 'already bound'):
            self.store.bind('cam-2', 2, '14:2f:fd:a2:f6:af', valid_cloud_camera_ids={'cam-1', 'cam-2'})

    def test_reconcile_finds_an_identity_bound_camera_and_reports_its_vms_state(self):
        auto_bind_discovered_cameras([_cloud()], [_found()], self.store)
        hls = self.root / 'hls'
        hls.mkdir()
        (hls / 'camera1.m3u8').write_text('#EXTM3U')
        reader = LocalVmsStatusReader(hls, self.root / 'recordings')
        [camera] = reconcile_cloud_cameras([_cloud()], [_found()], self.store.bindings(), reader)
        self.assertIsNone(camera['last_error'])
        self.assertTrue(camera['online'])
        self.assertNotIn('mac_address', json.dumps(camera))

    def test_ambiguous_duplicate_uuid_is_not_bound_and_clears_after_unique_scan(self):
        discovered = __import__('anyaicam_agent.camera_binding', fromlist=['DiscoveredCameraStore']).DiscoveredCameraStore(
            self.root / 'discovered.json')
        first, second = _found(), _found()
        second['ip'] = '192.168.253.11'
        second['device_key'] = UUID.upper()
        self.assertEqual(auto_bind_discovered_cameras([_cloud()], [first, second], self.store), [])
        discovered.save_scan([first, second])
        self.assertTrue(discovered.cameras()[0]['identity_ambiguous'])
        self.assertEqual(auto_bind_discovered_cameras([_cloud()], discovered.cameras(), self.store), [])
        discovered.save_scan([first])
        self.assertFalse(discovered.cameras()[0]['identity_ambiguous'])
        self.assertEqual(auto_bind_discovered_cameras([_cloud()], discovered.cameras(), self.store), ['cam-1'])

    def test_existing_real_mac_cannot_be_overridden_by_matching_uuid(self):
        self.store.bind('cam-1', 1, '02:00:00:00:00:01', device_key=UUID)
        candidate = _found(mac='02:00:00:00:00:02')
        self.assertEqual(auto_bind_discovered_cameras([_cloud()], [candidate], self.store), [])
        self.assertEqual(self.store.bindings()[0]['mac_address'], normalize_mac('02:00:00:00:00:01'))


if __name__ == '__main__':
    unittest.main()
