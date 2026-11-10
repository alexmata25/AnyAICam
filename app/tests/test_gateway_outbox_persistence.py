"""Tiny deterministic persistence tests, not a stress test or parallel suite."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.tests.test_gateway_licensing import load_functions
import event_media_outbox as outbox


class GatewayOutboxPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'event_media_outbox.json'
        target = patch.object(outbox, 'OUTBOX_FILE', self.path)
        target.start(); self.addCleanup(target.stop)

    def test_two_clip_workers_do_not_lose_another_pending_event(self):
        # Two bounded writers reproduce the former load/modify/save race.
        # No parallel test runner, camera worker, large media or service runs.
        entered, release, second_started, second_done = (threading.Event() for _ in range(4))
        original_save = outbox.save
        errors = []
        def paused_save(jobs):
            if threading.current_thread().name == 'outbox-first':
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test writer not released')
            original_save(jobs)
        def writer(event_id, done=None):
            try:
                if event_id == 'second':
                    second_started.set()
                outbox.put({'event_id': event_id})
            except Exception as error:
                errors.append(error)
            finally:
                if done:
                    done.set()
        first = threading.Thread(target=writer, args=('first',), name='outbox-first')
        second = threading.Thread(target=writer, args=('second', second_done))
        with patch.object(outbox, 'save', side_effect=paused_save):
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
                self.assertTrue(second_started.wait(1))
                self.assertFalse(second_done.wait(0.1))
            finally:
                release.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)
        self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual({job['event_id'] for job in outbox.load()}, {'first', 'second'})

    def test_failed_atomic_replace_keeps_previous_outbox_and_cleans_tempfile(self):
        outbox.put({'event_id': 'kept'})
        before = self.path.read_bytes()
        with patch.object(outbox.os, 'replace', side_effect=PermissionError('mock locked destination')):
            with self.assertRaises(PermissionError):
                outbox.put({'event_id': 'failed'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_retry_updates_survive_reload_and_do_not_modify_other_events(self):
        outbox.put({'event_id': 'a'})
        outbox.put({'event_id': 'b'})
        outbox.replace('b', {'event_id':'b', 'attempts':4, 'next_attempt_at':'2030-01-01T00:00:00+00:00'})
        outbox.remove('a')
        self.assertEqual(outbox.load(), [{'event_id':'b', 'attempts':4, 'next_attempt_at':'2030-01-01T00:00:00+00:00'}])
        self.assertEqual(outbox.due(now=datetime(2026, 11, 2, tzinfo=timezone.utc)), [])

    def test_cloud_retry_defers_denied_event_and_disabled_transport_preserves_it(self):
        outbox.put({'event_id':'e1', 'camera_number':1, 'event_start':'2026-11-02T10:00:00',
                    'event_end':'2026-11-02T10:00:01', 'clip_url':'/recordings/clips/motion/motion_e1.mp4', 'attempts':3})
        uploader = Mock(return_value=False)
        ns = {'EVENT_MEDIA_UPLOAD_ENABLED':True, '_edition_upload_allowed':lambda: True,
              'event_media_outbox':outbox, '_job_defect':lambda job: None,
              'upload_motion_event_media':uploader, 'RETRY_MAX_JOBS':10, 'RETRY_SECONDS':120,
              'RETRY_MAX_SECONDS':3600, 'datetime':datetime, 'timedelta':timedelta, 'timezone':timezone, 'logger':Mock()}
        load_functions('event_media_uploader.py', {'retry_pending_event_media'}, ns)
        self.assertEqual(ns['retry_pending_event_media'](), {'attempted':1, 'completed':0, 'pending':1})
        self.assertEqual(outbox.load()[0]['attempts'], 4)
        self.assertIsNotNone(outbox.load()[0]['next_attempt_at'])
        before = self.path.read_bytes()
        ns['_edition_upload_allowed'] = lambda: False
        self.assertEqual(ns['retry_pending_event_media'](), {'attempted':0, 'completed':0, 'pending':1})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(uploader.call_count, 1)
        # Simulate the next due tick after authorization recovers. The actual
        # uploader removes intent only after accepted registration (covered
        # separately by the mock-backed real uploader test).
        def accepted_upload(**job):
            outbox.remove(job['event_id'])
            return True
        uploader.side_effect = accepted_upload
        ns['_edition_upload_allowed'] = lambda: True
        with patch.object(outbox, 'due', return_value=outbox.load()):
            self.assertEqual(ns['retry_pending_event_media'](), {'attempted':1, 'completed':1, 'pending':0})
        self.assertEqual(outbox.load(), [])


if __name__ == '__main__':
    unittest.main()
