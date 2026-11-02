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


if __name__ == '__main__':
    unittest.main()
