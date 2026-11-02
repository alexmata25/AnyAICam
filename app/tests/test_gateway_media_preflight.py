"""Gateway media rejection occurs before credentials or AWS client creation."""
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.tests.test_gateway_licensing import load_functions
import gateway_edition as edition
from event_clips import compute_clip_window


class GatewayMediaPreflightTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {edition.EDITION_ENV: 'cloud_gateway', 'ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED': 'true'})
        env.start()
        self.addCleanup(env.stop)

    def test_duration_reuses_existing_sanity_ceiling_without_daily_quota(self):
        from event_media_policy import _MAX_SINGLE_CLIP_SECONDS
        for duration in (None, True, -1, 0, float('nan'), float('inf'), _MAX_SINGLE_CLIP_SECONDS + 1):
            self.assertFalse(edition.event_clip_duration_allowed(duration), repr(duration))
        for duration in (0.1, 10, _MAX_SINGLE_CLIP_SECONDS):
            self.assertTrue(edition.event_clip_duration_allowed(duration))
        with patch.dict(os.environ, {edition.EDITION_ENV: 'full_vms'}):
            self.assertTrue(edition.event_clip_duration_allowed(None))

    def test_optional_thumbnail_cannot_be_a_video_or_unreadable_file(self):
        self.assertTrue(edition.event_thumbnail_allowed(None))
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'event.jpg'
            image.write_bytes(b'not an image')
            self.assertFalse(edition.event_thumbnail_allowed(image))
            image.write_bytes(b'\xff\xd8\xfftest')
            self.assertTrue(edition.event_thumbnail_allowed(image))
            video = Path(folder) / 'event.mp4'
            video.write_bytes(b'\xff\xd8\xfftest')
            self.assertFalse(edition.event_thumbnail_allowed(video))
            self.assertFalse(edition.event_thumbnail_allowed(Path(folder) / 'missing.jpg'))
            with patch.dict(os.environ, {edition.EDITION_ENV: 'full_vms'}):
                self.assertTrue(edition.event_thumbnail_allowed(video))

    def test_invalid_gateway_media_never_requests_credentials_or_uploads(self):
        for duration, thumbnail in [(None, None), (0, None), (3601, None), (float('nan'), None), (10, Path('video.mp4'))]:
            with self.subTest(duration=duration, thumbnail=thumbnail):
                recording = Mock()
                recording._camera_identity.return_value = {'cloud_recording_mode': 'motion', 'camera_id': 'c1'}
                ns = {
                    'EVENT_MEDIA_CAPTURE_ENABLED': True, 'EVENT_MEDIA_UPLOAD_ENABLED': True,
                    '_edition_upload_allowed': lambda: True,
                    '_event_camera_authorized': lambda number, identity: True,
                    '_event_upload_session': recording._ensure_session,
                    '_safe_recording_url': lambda url: url,
                    '_local_path_from_recording_url': lambda url: thumbnail if url == 'thumbnail' else Path('clip.mp4'),
                    '_duration_seconds': Mock(return_value=duration),
                    'compute_clip_window': compute_clip_window,
                    'recording_upload': recording, 'logger': Mock(), 'event_media_outbox': Mock(),
                }
                load_functions('event_media_uploader.py', {'upload_motion_event_media'}, ns)
                result = ns['upload_motion_event_media'](event_id='e1', camera_number=1,
                    event_start=datetime(2026, 11, 1), event_end=datetime(2026, 11, 1, 0, 0, 10),
                    clip_url='/recordings/clips/motion/motion_e1.mp4', thumbnail_url='thumbnail', already_classified=True)
                self.assertFalse(result)
                ns['event_media_outbox'].put.assert_called_once()
                ns['event_media_outbox'].remove.assert_not_called()
                recording._ensure_session.assert_not_called()
                recording.boto3.client.assert_not_called()

    def test_invalid_event_windows_are_rejected_before_transport(self):
        for start, end in [(datetime(2026, 11, 2), datetime(2026, 11, 1)),
                           (datetime(2026, 11, 1), datetime(2026, 11, 1, tzinfo=timezone.utc)),
                           (datetime.min, datetime.min)]:
            with self.subTest(start=start, end=end):
                recording = Mock()
                recording._camera_identity.return_value = {'cloud_recording_mode': 'motion', 'camera_id': 'c1'}
                ns = {'EVENT_MEDIA_CAPTURE_ENABLED': True, 'EVENT_MEDIA_UPLOAD_ENABLED': True,
                      '_edition_upload_allowed': lambda: True, '_event_camera_authorized': lambda *args: True,
                      '_event_upload_session': recording._ensure_session, '_safe_recording_url': lambda url: url,
                      '_local_path_from_recording_url': lambda url: Path('clip.mp4') if url else None,
                      '_duration_seconds': lambda path: 10, 'compute_clip_window': compute_clip_window,
                      'recording_upload': recording, 'logger': Mock(), 'event_media_outbox': Mock()}
                load_functions('event_media_uploader.py', {'upload_motion_event_media'}, ns)
                self.assertFalse(ns['upload_motion_event_media'](event_id='e1', camera_number=1,
                    event_start=start, event_end=end, clip_url='/recordings/clips/motion/motion_e1.mp4',
                    thumbnail_url=None, already_classified=True))
                recording._ensure_session.assert_not_called()
                recording.boto3.client.assert_not_called()

    def test_recording_backup_routes_and_queue_fail_before_state_changes(self):
        class Refused(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
        ns = {'_edition_video_transport_allowed': edition.video_transport_allowed, 'HTTPException': Refused}
        load_functions('main.py', {'enqueue_cloud_upload', 'cloud_recording_scan', 'cloud_recording_queue_file', 'cloud_recording_retry'}, ns)
        for selected in ('local_gateway', 'cloud_gateway'):
            with patch.dict(os.environ, {edition.EDITION_ENV: selected}):
                with self.assertRaises(PermissionError):
                    ns['enqueue_cloud_upload'](None)
                for name, args in [('cloud_recording_scan', (None,)), ('cloud_recording_queue_file', (None, None)), ('cloud_recording_retry', (None, None))]:
                    with self.assertRaises(Refused) as error:
                        ns[name](*args)
                    self.assertEqual(error.exception.status_code, 403)

    def test_gateway_page_has_no_bulk_controls_or_polling_and_keeps_permissions(self):
        ns = {'current_user': lambda request: {}, 'has_permission': lambda user, permission: True,
              '_edition_video_transport_allowed': edition.video_transport_allowed,
              'page_shell': lambda title, active, content, scripts='': content + scripts,
              'permission_denied_page': lambda *args: 'permission denied'}
        load_functions('main.py', {'cloud_recording_page'}, ns)
        for selected, phrase in [('local_gateway', 'Local Gateway'), ('cloud_gateway', 'authorized event clips')]:
            with patch.dict(os.environ, {edition.EDITION_ENV: selected}):
                html = ns['cloud_recording_page'](None)
                self.assertIn(phrase, html)
                self.assertNotIn('cloud-recording-scan', html)
                self.assertNotIn('setInterval', html)
                self.assertIn('/analytics', html)
                with patch.dict(ns, {'has_permission': lambda *args: False}):
                    self.assertEqual(ns['cloud_recording_page'](None), 'permission denied')
        with patch.dict(os.environ, {edition.EDITION_ENV: 'full_vms'}):
            html = ns['cloud_recording_page'](None)
            self.assertIn('cloud-recording-scan', html)
            self.assertIn('setInterval', html)


if __name__ == '__main__':
    unittest.main()
