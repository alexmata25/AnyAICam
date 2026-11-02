"""Gateway uses existing camera capacity and fresh server authorization; no live services."""
import ast
import __future__
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
import gateway_edition as edition
import vms_capacity


def load_functions(source, names, namespace):
    tree = ast.parse((ROOT / 'app' / source).read_text(encoding='utf-8'))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(functions) == len(names)
    for function in functions:
        function.decorator_list = []  # call route logic without registering a web app
    exec(compile(ast.Module(body=functions, type_ignores=[]), source, 'exec', flags=__future__.annotations.compiler_flag), namespace)


class GatewayLicensingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            edition.EDITION_ENV: 'cloud_gateway', 'ANYAICAM_RUNTIME_ROLE': 'edge',
            'ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED': 'true',
        })
        env.start()
        self.addCleanup(env.stop)

    def test_gateway_requires_accepted_camera_capacity(self):
        identity = {'cloud_recording_mode': 'motion'}
        for capacity, allowed in [(None, False), ({'camera_slot_quantity': 0}, False), ({'camera_slot_quantity': 1}, True)]:
            with self.subTest(capacity=capacity), patch.object(vms_capacity, 'load_capacity', return_value=capacity):
                self.assertEqual(edition.event_camera_authorized(1, identity, [2, 1]), allowed)
                self.assertFalse(edition.event_camera_authorized(2, identity, [2, 1]))
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': 'cloud'}):
            self.assertFalse(edition.event_camera_authorized(1, identity, [1]))

    def test_revoked_or_broken_capacity_stops_gateway_only(self):
        with patch.object(vms_capacity, 'licensed_camera_numbers', side_effect=ValueError('revoked')):
            self.assertFalse(edition.event_camera_authorized(1, {'cloud_recording_mode': 'motion'}, [1]))
            with patch.dict(os.environ, {edition.EDITION_ENV: 'full_vms'}):
                self.assertTrue(edition.event_camera_authorized(1, {}, []))

    def test_gateway_licence_does_not_imply_cloud_entitlement(self):
        with patch.object(vms_capacity, 'licensed_camera_numbers', return_value=[1]):
            self.assertFalse(edition.event_camera_authorized(1, {'cloud_recording_mode': 'disabled'}, [1]))
            with patch.dict(os.environ, {edition.EDITION_ENV: 'local_gateway'}):
                self.assertFalse(edition.event_camera_authorized(1, {'cloud_recording_mode': 'motion'}, [1]))

    def test_gateway_rejects_bulk_recordings_or_other_event_references(self):
        self.assertTrue(edition.event_clip_reference_allowed('event-1', '/recordings/clips/motion/motion_event-1.mp4'))
        for url in ('/recordings/camera1/segment.mkv', '/recordings/camera1/segment.mp4',
                    '/recordings/clips/motion/motion_other.mp4', '/recordings/clips/motion/../segment.mp4'):
            self.assertFalse(edition.event_clip_reference_allowed('event-1', url))
        self.assertFalse(edition.event_clip_reference_allowed('../event', '/recordings/clips/motion/motion_../event.mp4'))
        with patch.dict(os.environ, {edition.EDITION_ENV: 'full_vms'}):
            self.assertTrue(edition.event_clip_reference_allowed('event-1', '/recordings/legacy.mp4'))

    def test_notice_shows_licence_and_disabled_upload_without_enabling_anything(self):
        with patch.object(edition, '_capacity_status', return_value={'state': 'licensed', 'camera_slot_quantity': 4}):
            with patch.dict(os.environ, {'ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED': 'false'}):
                html = edition.edition_notice_html()
                self.assertIn('4 camera slots', html)
                self.assertIn('currently disabled', html)
                self.assertEqual(os.environ['ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED'], 'false')
        with patch.object(edition, '_capacity_status', return_value={'state': 'fail_closed', 'reason': '<private>'}):
            html = edition.edition_notice_html()
            self.assertIn('Camera licence required', html)
            self.assertNotIn('<private>', html)

    def session_namespace(self):
        ns = {'_sessions': {}, '_clients': {}, '_control_plane_post': Mock(), 'logger': Mock(),
              'datetime': datetime, 'timezone': timezone, 'SESSION_RENEW_MARGIN_SECONDS': 120}
        load_functions('recording_uploader.py', {'_ensure_session', '_session_expires_soon'}, ns)
        return ns

    def test_fresh_gateway_session_cannot_fall_back_to_cached_grant_after_refusal(self):
        ns = self.session_namespace()
        old = {'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        ns['_sessions'][1] = old
        ns['_clients'][1] = Mock()
        ns['_control_plane_post'].return_value = None  # denied/unreachable
        self.assertIs(ns['_ensure_session'](1, 'camera1'), old)  # Full VMS cache unchanged
        ns['_control_plane_post'].assert_not_called()
        self.assertIsNone(ns['_ensure_session'](1, 'camera1', force_refresh=True))
        ns['_control_plane_post'].assert_called_once_with('/api/appliance/recordings/camera1/credentials', {})
        self.assertNotIn(1, ns['_sessions'])
        self.assertNotIn(1, ns['_clients'])

    def test_gateway_rejects_malformed_expired_or_incomplete_session(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        for expiration, token, accepted in [
            ('invalid', 'test', False), ('2020-01-01T00:00:00+00:00', 'test', False),
            ('2030-01-01T00:00:00', 'test', False), (future, '', False), (future, 'test', True),
        ]:
            with self.subTest(expiration=expiration, accepted=accepted):
                ns = self.session_namespace()
                ns['_control_plane_post'].return_value = {
                    'credentials': {'access_key_id': 'fake', 'secret_access_key': 'fake', 'session_token': token, 'expiration': expiration},
                    'bucket': 'fake', 'key_prefix': 'recordings/test/', 'region': 'test-region',
                }
                session = ns['_ensure_session'](1, 'camera1', force_refresh=True)
                self.assertEqual(session is not None, accepted)

    def test_only_gateway_requests_fresh_session(self):
        recording = Mock()
        ns = {'recording_upload': recording}
        load_functions('event_media_uploader.py', {'_event_upload_session'}, ns)
        ns['_event_upload_session'](1, 'c1')
        recording._ensure_session.assert_called_once_with(1, 'c1', force_refresh=True)
        recording.reset_mock()
        with patch.dict(os.environ, {edition.EDITION_ENV: 'full_vms'}):
            ns['_event_upload_session'](1, 'c1')
            recording._ensure_session.assert_called_once_with(1, 'c1')

    def test_authorized_gateway_clip_uses_only_event_objects_with_mock_transport(self):
        # Execute the actual uploader/session functions. Every transport is a
        # mock; the only media is a tiny disposable file, never a real camera.
        with tempfile.TemporaryDirectory() as temp:
            clip = Path(temp) / 'event.mp4'
            clip.write_bytes(b'test clip')
            client = Mock()
            recording_ns = self.session_namespace()
            recording_ns.update({
                'AWS_REGION': '',
                'boto3': SimpleNamespace(client=Mock(return_value=client)),
                '_refresh_camera_map': Mock(), '_known_camera_numbers': lambda: [1],
                '_camera_identity': lambda number: {'camera_id': 'c1', 'cloud_recording_mode': 'motion'},
            })
            response = {
                'credentials': {'access_key_id': 'fake', 'secret_access_key': 'fake', 'session_token': 'fake',
                                'expiration': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                'bucket': 'fake-bucket', 'key_prefix': 'recordings/customer/site/appliance/c1/', 'region': 'test-region',
            }
            recording_ns['_control_plane_post'].side_effect = lambda url, payload: response if url.endswith('/credentials') else {'status': 'accepted'}
            load_functions('recording_uploader.py', {'session_region'}, recording_ns)
            recording = SimpleNamespace(**recording_ns)
            outbox = Mock()
            start = datetime(2026, 11, 1)
            end = start + timedelta(seconds=10)
            ns = {
                'recording_upload': recording, 'event_media_outbox': outbox, 'logger': Mock(),
                'EVENT_MEDIA_CAPTURE_ENABLED': True, 'EVENT_MEDIA_UPLOAD_ENABLED': True,
                '_safe_recording_url': lambda url: url,
                '_local_path_from_recording_url': lambda url: clip if url else None,
                '_ensure_detection_event_synced': lambda event, camera: True,
                'compute_clip_window': lambda a, b: SimpleNamespace(start=a, end=b),
                '_duration_seconds': lambda path: 10,
                'EVENT_MEDIA_CACHE_CONTROL': 'test-cache',
            }
            load_functions('event_media_uploader.py', {'_edition_upload_allowed', '_event_camera_authorized',
                '_event_upload_session', 'upload_motion_event_media'}, ns)
            with patch.object(vms_capacity, 'load_capacity', return_value={'camera_slot_quantity': 1}):
                result = ns['upload_motion_event_media'](event_id='e1', camera_number=1,
                    event_start=start, event_end=end, clip_url='/recordings/clips/motion/motion_e1.mp4',
                    thumbnail_url=None, already_classified=True)
            self.assertTrue(result)
            client.upload_file.assert_called_once()
            self.assertEqual(client.upload_file.call_args.args[1], 'fake-bucket')
            self.assertTrue(client.upload_file.call_args.args[2].endswith('/2026/11/01/events/motion_e1.mp4'))
            self.assertEqual(recording._control_plane_post.call_args_list[0].args[0], '/api/appliance/recordings/c1/credentials')
            self.assertEqual(recording._control_plane_post.call_args_list[1].args[0], '/api/appliance/analytics/c1/events/e1/media')
            outbox.remove.assert_called_once_with('e1')


if __name__ == '__main__':
    unittest.main()
