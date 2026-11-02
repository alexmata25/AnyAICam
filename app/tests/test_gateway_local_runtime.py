"""Actual local licence, camera and playback functions; no camera/cloud processes."""
from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.tests.test_gateway_licensing import load_functions
import vms_capacity


class GatewayLocalRuntimeTests(unittest.TestCase):
    def test_remote_expiry_does_not_remove_independent_signed_local_licence(self):
        for edition in ('local_gateway', 'cloud_gateway', 'full_vms'):
            with self.subTest(edition=edition), patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': edition}):
                grant = {'camera_slot_quantity': 0, 'plan_camera_slots': 0, 'vms_license_capacity': 2}
                capacity = vms_capacity._record(grant, 'signed')
                self.assertEqual(capacity['camera_slot_quantity'], 0 if edition == 'full_vms' else 2)
                self.assertEqual(grant['camera_slot_quantity'], 0)  # signed document is never mutated
                with patch.object(vms_capacity, 'load_capacity', return_value=capacity):
                    self.assertEqual(vms_capacity.licensed_camera_numbers([3, 1, 2]), [] if edition == 'full_vms' else [1, 2])

    def test_no_unsigned_or_malformed_local_capacity_is_granted(self):
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'local_gateway'}):
            for value in (True, -1, 10001, '4', None):
                self.assertIsNone(vms_capacity._record({'camera_slot_quantity': 0, 'vms_license_capacity': value}, 'signed'))
            self.assertEqual(vms_capacity._record({'camera_slot_quantity': 0, 'vms_license_capacity': 4}, 'migrated')['camera_slot_quantity'], 0)
            self.assertEqual(vms_capacity._record({'camera_slot_quantity': 1}, 'signed')['camera_slot_quantity'], 1)
            self.assertEqual(vms_capacity._record({'camera_slot_quantity': 0, 'vms_license_capacity': 0}, 'signed')['camera_slot_quantity'], 0)

    def test_rejected_accepted_store_does_not_fall_back_to_local_licence(self):
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'local_gateway'}), \
             patch.dict(sys.modules, {'appliance_entitlements': SimpleNamespace(CLOCK_SKEW=timedelta(seconds=1))}), \
             patch.object(vms_capacity, '_accepted_signed_detail', return_value=(True, None, 'revoked', {})), \
             patch.object(vms_capacity, '_fallback_capacity') as fallback:
            self.assertIsNone(vms_capacity.load_capacity())
            fallback.assert_not_called()

    def test_provisioned_camera_starts_from_local_registry_after_remote_expiry(self):
        ns = {'get_camera_numbers': lambda: [1], '_provisioned_camera_stream': lambda number: {
            'rtsp_url': 'rtsp://test-camera/stream', 'username': '', 'password': ''},
            'credentialed_rtsp_url': lambda url, user, password: url,
            'CameraNotLicensedError': PermissionError, 'CameraNotConfiguredError': LookupError}
        load_functions('main.py', {'camera_url'}, ns)
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'local_gateway'}):
            capacity = vms_capacity._record({'camera_slot_quantity': 0, 'vms_license_capacity': 1}, 'signed')
            with patch.object(vms_capacity, 'load_capacity', return_value=capacity):
                self.assertEqual(ns['camera_url'](1), 'rtsp://test-camera/stream')
                with self.assertRaises(PermissionError):
                    ns['camera_url'](2)

    def test_camera_registry_resolves_credentials_without_cloud_requests(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("CREATE TABLE cameras(id,camera_number,onvif_endpoint,ip_address,device_key); CREATE TABLE camera_credentials(camera_id,encrypted_blob); INSERT INTO cameras VALUES('c1',1,'rtsp://test-camera/stream','192.0.2.1','device1'); INSERT INTO camera_credentials VALUES('c1','encrypted-test-fixture');")
        @contextmanager
        def connection():
            yield db
        recovery = Mock(side_effect=lambda camera, device, url, number: url)
        modules = {'partner_db': SimpleNamespace(connection=connection),
                   'appliance_protocol': SimpleNamespace(decrypt_camera_credentials=lambda blob: {'username':'test','password':'fixture'}),
                   'camera_address_recovery': SimpleNamespace(stream_endpoint=recovery)}
        with patch.dict(sys.modules, modules):
            ns = {}
            load_functions('main.py', {'_provisioned_camera_stream'}, ns)
            result = ns['_provisioned_camera_stream'](1)
            self.assertEqual(result, {'rtsp_url':'rtsp://test-camera/stream','username':'test','password':'fixture'})
            self.assertIsNone(ns['_provisioned_camera_stream'](2))
            recovery.assert_called_once_with('c1', 'device1', 'rtsp://test-camera/stream', 1)

    def test_gateway_playback_prefers_existing_local_file_without_cloud_signing(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("CREATE TABLE cameras(id,name,camera_number,former_camera_number); CREATE TABLE recordings(id,camera_id,s3_key,status); INSERT INTO cameras VALUES('c1','Test',1,NULL); INSERT INTO recordings VALUES('r1','c1','prefix/camera1_test.mkv','available');")
        @contextmanager
        def connection():
            yield db
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {'partner_db': SimpleNamespace(connection=connection)}):
            root = Path(folder); (root / 'camera1').mkdir(); (root / 'camera1/camera1_test.mkv').write_bytes(b'test')
            cloud = Mock(return_value='https://mock.invalid/recording')
            ns = {'Path': Path, 'RECORDINGS_FOLDER': root, '_presigned_recording_url': cloud}
            load_functions('main.py', {'_customer_recording_url', '_customer_local_recording_path'}, ns)
            for edition in ('local_gateway', 'cloud_gateway'):
                with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': edition}):
                    self.assertEqual(ns['_customer_recording_url']('c1', 'r1'), '/api/customer/recordings/c1/r1/local')
                    cloud.assert_not_called()
                    self.assertIsNone(ns['_customer_recording_url']('other', 'r1'))
            with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'full_vms'}):
                self.assertEqual(ns['_customer_recording_url']('c1', 'r1'), 'https://mock.invalid/recording')

    def test_local_notification_playback_link_does_not_require_cloud_event_media(self):
        href = Mock(return_value='/playback?camera=c1')
        ns = {'_customer_event_playback_href': href}
        load_functions('main.py', {'_customer_notification_playback_href'}, ns)
        event = {'camera_id': 'c1', 'timestamp': '2026-11-02T10:00:00', 'event_id': 'event1', 'has_event_clip': False}
        for edition in ('local_gateway', 'cloud_gateway'):
            with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': edition}):
                self.assertIsNotNone(ns['_customer_notification_playback_href'](event))
                href.assert_called_with('c1', event['timestamp'])
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'full_vms'}):
            self.assertIsNone(ns['_customer_notification_playback_href'](event))
            event['has_event_clip'] = True
            self.assertIsNotNone(ns['_customer_notification_playback_href'](event))
            href.assert_called_with('c1', event['timestamp'], 'event1', True)
