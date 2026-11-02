"""Explicit sequential Gateway regression set. No discovery, builds or service operations."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_MODULES = (
    'app.tests.test_gateway_edition',
    'app.tests.test_gateway_licensing',
    'app.tests.test_gateway_event_retention_contract',
    'app.tests.test_gateway_media_preflight',
    'app.tests.test_gateway_local_runtime',
    'app.tests.test_gateway_local_notifications',
    'app.tests.test_gateway_outbox_persistence',
    'installer.tests.test_gateway_edition_integration',
    'installer.tests.test_gateway_configuration_contract',
    'installer.tests.test_gateway_prerequisite_audit',
    'installer.tests.test_gateway_cache_audit',
    'installer.tests.test_gateway_build_tools',
    'installer.tests.test_windows_1341_acceptance_static',
    'installer.tests.test_windows_local_activation_wording_132',
    'installer.tests.test_windows_hybrid_state_dir_132',
    'installer.tests.test_windows_premium_defaults_132',
)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
