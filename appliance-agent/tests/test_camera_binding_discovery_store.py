"""Regression coverage for the discovered-camera local-cache data-loss
fix: DiscoveredCameraStore.save_scan() previously keyed every record by
MAC address and silently `continue`d (dropped) any candidate whose MAC
was 'Unknown' -- the normal state for a camera that answered ONVIF
WS-Discovery before the OS had ARP-resolved its IP. Confirmed live
against Samsung's appliance: a real scan found 5 ONVIF-capable cameras
and all 5 were correctly reported to the cloud (redact_discovery_for_
cloud()), but only 2 survived into discovered_cameras.json.

device_key (the ONVIF endpoint UUID, or discovery.py's own MAC-hash/
IP-hash fallback -- always present on real scan() output) is now the
primary, sufficient identity for a discovery record. MAC/IP are
optional metadata: absent or 'Unknown' no longer drops a candidate,
and a later scan that resolves them enriches the existing record in
place rather than creating a duplicate or erasing what's already
known. Same style as test_stage2_appliance.py (unittest + a real
temp-file store, no mocking needed -- this is pure local JSON I/O)."""

import json
import tempfile
import unittest
from pathlib import Path

from anyaicam_agent.camera_binding import DiscoveredCameraStore


def _camera(device_key='urn:uuid:aaaa', ip='192.168.1.10', mac='Unknown', **overrides):
    label = ip if ip else device_key
    base = {
        'id': 'camera-' + label.replace('.', '-').replace(':', '-'), 'device_key': device_key, 'name': 'Camera ' + label,
        'ip': ip, 'manufacturer': 'Unknown', 'model': 'Unknown', 'mac_address': mac,
        'onvif_support': True, 'rtsp_support': True, 'connection_status': 'reachable',
        'online': True, 'recording': False, 'analytics': False, 'last_recording_at': None, 'last_error': None,
    }
    base.update(overrides)
    return base


class DiscoveredCameraStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.store = DiscoveredCameraStore(Path(self._tmpdir.name) / 'discovered_cameras.json')

    # ---- the three identity shapes a real candidate can arrive in

    def test_device_key_with_resolved_ip_and_valid_mac_is_saved(self):
        self.store.save_scan([_camera(device_key='urn:uuid:1111', ip='192.168.1.10', mac='14:2f:fd:a2:f6:af')])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['device_key'], 'urn:uuid:1111')
        self.assertEqual(cameras[0]['ip'], '192.168.1.10')
        self.assertEqual(cameras[0]['mac_address'], '14:2f:fd:a2:f6:af')

    def test_device_key_with_resolved_ip_and_unknown_mac_is_not_dropped(self):
        """The exact bug: this candidate has a real device_key and IP,
        but the ARP table hadn't caught up yet. Must survive."""
        self.store.save_scan([_camera(device_key='urn:uuid:2222', ip='192.168.1.20', mac='Unknown')])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['device_key'], 'urn:uuid:2222')
        self.assertEqual(cameras[0]['ip'], '192.168.1.20')
        self.assertEqual(cameras[0]['mac_address'], 'Unknown')

    def test_device_key_with_unresolved_ip_and_unknown_mac_is_not_dropped(self):
        """Even with no usable IP at all, a real device_key alone is
        sufficient to preserve the candidate."""
        camera = _camera(device_key='urn:uuid:3333', mac='Unknown')
        camera['ip'] = None
        self.store.save_scan([camera])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['device_key'], 'urn:uuid:3333')
        self.assertIsNone(cameras[0]['ip'])
        self.assertEqual(cameras[0]['mac_address'], 'Unknown')

    # ---- enrichment and dedup across repeated scans

    def test_later_scan_enriches_the_same_record_with_mac_and_ip(self):
        self.store.save_scan([_camera(device_key='urn:uuid:4444', ip=None, mac='Unknown')])
        self.store.save_scan([_camera(device_key='urn:uuid:4444', ip='192.168.1.40', mac='14:2f:fd:60:6e:23')])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 1, 'enrichment must update the existing record, not add a second one')
        self.assertEqual(cameras[0]['ip'], '192.168.1.40')
        self.assertEqual(cameras[0]['mac_address'], '14:2f:fd:60:6e:23')

    def test_enrichment_never_erases_a_previously_resolved_value(self):
        """A later scan that (transiently) fails to resolve MAC again
        must not blank out a MAC a previous scan already established."""
        self.store.save_scan([_camera(device_key='urn:uuid:5555', ip='192.168.1.50', mac='14:2f:fd:60:6e:23')])
        self.store.save_scan([_camera(device_key='urn:uuid:5555', ip='192.168.1.50', mac='Unknown')])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['mac_address'], '14:2f:fd:60:6e:23', 'must keep the earlier known MAC, not regress to Unknown')

    def test_repeated_discovery_of_the_same_device_key_does_not_duplicate(self):
        for _ in range(3):
            self.store.save_scan([_camera(device_key='urn:uuid:6666', ip='192.168.1.60', mac='14:2f:fd:00:00:60')])
        self.assertEqual(len(self.store.cameras()), 1)

    def test_two_different_device_keys_are_two_separate_records(self):
        self.store.save_scan([
            _camera(device_key='urn:uuid:7777', ip='192.168.1.70'),
            _camera(device_key='urn:uuid:8888', ip='192.168.1.71'),
        ])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 2)
        self.assertEqual({c['device_key'] for c in cameras}, {'urn:uuid:7777', 'urn:uuid:8888'})

    # ---- backward compatibility with pre-fix / pre-device_key data on disk

    def test_legacy_mac_keyed_entries_already_on_disk_remain_readable(self):
        """Simulates a discovered_cameras.json written by the OLD
        save_scan() (or any record predating device_key entirely) --
        must still load, and a fresh scan for a *different* physical
        camera must not disturb it."""
        legacy_payload = {
            'version': 1, 'updated_at': '2026-08-01T00:00:00+00:00',
            'cameras': [{'id': 'camera-192-168-1-99', 'name': 'Camera 192.168.1.99', 'ip': '192.168.1.99',
                         'manufacturer': 'Unknown', 'model': 'Unknown', 'mac_address': '14:2f:fd:99:99:99',
                         'onvif_support': True, 'rtsp_support': True, 'connection_status': 'reachable',
                         'online': True, 'recording': False, 'analytics': False,
                         'last_recording_at': None, 'last_error': None}],  # no device_key field at all
        }
        self.store.path.write_text(json.dumps(legacy_payload), encoding='utf-8')
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['mac_address'], '14:2f:fd:99:99:99')

        self.store.save_scan([_camera(device_key='urn:uuid:9999', ip='192.168.1.10', mac='14:2f:fd:a2:f6:af')])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 2, 'the legacy MAC-only record must be preserved alongside the new device_key record')
        macs = {c['mac_address'] for c in cameras}
        self.assertIn('14:2f:fd:99:99:99', macs)
        self.assertIn('14:2f:fd:a2:f6:af', macs)

    def test_legacy_mac_keyed_entry_is_migrated_in_place_once_its_device_key_is_learned(self):
        """Once a scan reports a device_key for what was previously a
        MAC-only legacy record of the SAME physical camera (matched by
        MAC), it must enrich that record in place, not create a
        duplicate."""
        legacy_payload = {
            'version': 1, 'updated_at': '2026-08-01T00:00:00+00:00',
            'cameras': [{'id': 'camera-192-168-1-99', 'name': 'Camera 192.168.1.99', 'ip': '192.168.1.99',
                         'manufacturer': 'Unknown', 'model': 'Unknown', 'mac_address': '14:2f:fd:99:99:99',
                         'onvif_support': True, 'rtsp_support': True, 'connection_status': 'reachable',
                         'online': True, 'recording': False, 'analytics': False,
                         'last_recording_at': None, 'last_error': None}],
        }
        self.store.path.write_text(json.dumps(legacy_payload), encoding='utf-8')
        # A device_key-only candidate for a *different* camera must coexist, not collide.
        self.store.save_scan([_camera(device_key='urn:uuid:aaaa1111', ip='192.168.1.20', mac='Unknown')])
        cameras = self.store.cameras()
        self.assertEqual(len(cameras), 2)

    def test_only_truly_unidentifiable_candidates_are_dropped(self):
        """No device_key at all AND no valid MAC: genuinely nothing
        safe to key it by -- this is the only case still dropped."""
        camera = _camera(device_key='', ip='192.168.1.30', mac='Unknown')
        self.store.save_scan([camera])
        self.assertEqual(self.store.cameras(), [])


if __name__ == '__main__':
    unittest.main()
