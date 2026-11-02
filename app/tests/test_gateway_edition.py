"""Small isolated tests: no VMS import, workers, database or network."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gateway_edition as edition
import product_mode as pm


class GatewayEditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        state = patch.object(pm, 'STATE_FILE', Path(self.temp.name) / 'mode.json')
        state.start()
        self.addCleanup(state.stop)
        capacity = patch.object(edition, '_capacity_status', return_value={'state': 'fail_closed'})
        capacity.start()
        self.addCleanup(capacity.stop)

    def test_full_vms_preserves_legacy_default(self):
        self.assertEqual(edition.current_edition(), 'full_vms')
        self.assertEqual(pm.current_mode(), '')
        self.assertFalse(pm._resolve('TEST_CLOUD_FLAG', False))
        self.assertTrue(pm._resolve('TEST_CLOUD_FLAG', True))

    def test_gateway_defaults_and_explicit_flag_override(self):
        for name, mode, enabled in [('local_gateway', 'local', False), ('cloud_gateway', 'hybrid', True)]:
            with self.subTest(edition=name):
                os.environ[edition.EDITION_ENV] = name
                self.assertEqual(pm.current_mode(), mode)
                self.assertEqual(pm._resolve('TEST_CLOUD_FLAG', False), enabled)
                with patch.dict(os.environ, {'TEST_CLOUD_FLAG': str(not enabled).lower()}):
                    self.assertEqual(pm._resolve('TEST_CLOUD_FLAG', False), not enabled)

    def test_existing_configuration_precedence(self):
        os.environ[edition.EDITION_ENV] = 'cloud_gateway'
        os.environ[pm.BOOTSTRAP_MODE_ENV] = 'local'
        self.assertEqual(pm.current_mode(), 'local')
        pm.persist_mode('hybrid')
        self.assertEqual(pm.current_mode(), 'hybrid')
        os.environ[pm.PRODUCT_MODE_ENV] = 'local'
        self.assertEqual(pm.current_mode(), 'local')

    def test_invalid_selection_is_rejected(self):
        os.environ[edition.EDITION_ENV] = 'cluod_gateway'
        with self.assertRaises(ValueError):
            pm.current_mode()

    def test_local_gateway_blocks_video_even_with_hybrid_and_explicit_flags(self):
        os.environ[edition.EDITION_ENV] = 'local_gateway'
        os.environ[pm.PRODUCT_MODE_ENV] = 'hybrid'
        for flag, kind in edition.VIDEO_FLAGS.items():
            with patch.dict(os.environ, {flag: 'true'}):
                self.assertFalse(pm._resolve(flag, True))
                self.assertFalse(edition.video_transport_allowed(kind))
        # Non-video functions retain the existing mode/override rules.
        self.assertTrue(pm._resolve('ANYAICAM_ANALYTICS_SYNC_ENABLED', False))

    def test_cloud_gateway_requires_explicit_event_opt_in(self):
        os.environ[edition.EDITION_ENV] = 'cloud_gateway'
        self.assertFalse(pm._resolve('ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED', True))
        with patch.dict(os.environ, {'ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED': 'true'}):
            self.assertTrue(pm._resolve('ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED', False))
            for kind in ('recording', 'live', 'backup'):
                self.assertFalse(edition.video_transport_allowed(kind))

    def test_full_vms_preserves_explicit_video_flags(self):
        for flag in edition.VIDEO_FLAGS:
            for enabled in (True, False):
                with patch.dict(os.environ, {flag: str(enabled).lower()}):
                    self.assertEqual(pm._resolve(flag, not enabled), enabled)

    def test_gateway_notice_and_full_vms_ui(self):
        self.assertEqual(edition.edition_notice_html(), '')
        os.environ[edition.EDITION_ENV] = 'local_gateway'
        self.assertIn('Local Gateway', edition.edition_notice_html())
        self.assertIn('uploads are disabled', edition.edition_notice_html())
        os.environ[edition.EDITION_ENV] = 'cloud_gateway'
        self.assertIn('eligible plan', edition.edition_notice_html())


if __name__ == '__main__':
    unittest.main()
