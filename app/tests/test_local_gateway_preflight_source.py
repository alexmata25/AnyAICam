"""Source-only checks: do not import the VMS app or open a database."""
import ast
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import run_local_gateway_acceptance as preflight


class PreflightSourceTests(unittest.TestCase):
    def test_integration_plan_never_reports_ready_or_prints_secrets(self):
        output = io.StringIO()
        values = {'ANYAICAM_GATEWAY_ACCEPTANCE_POSTGRES_URL': 'postgresql://private:secret@invalid/db',
                  'ANYAICAM_STRIPE_SECRET_KEY': 'sk_test_private_secret'}
        with patch.dict('os.environ', values), contextlib.redirect_stdout(output):
            self.assertEqual(preflight.integration_plan_preflight(), 2)
        self.assertIn('STOP: plan only', output.getvalue())
        self.assertIn('not connected or independently verified', output.getvalue())
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn('secret', output.getvalue())

    def setUp(self):
        self.fixture = preflight.FIXTURE.read_text(encoding="utf-8")
        self.guards = preflight.GUARDS.read_text(encoding="utf-8")

    def check_with_sources(self, check, fixture=None, guards=None):
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == preflight.FIXTURE:
                return self.fixture if fixture is None else fixture
            if path == preflight.GUARDS:
                return self.guards if guards is None else guards
            return real_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            return check()

    def test_current_source_passes_both_checks(self):
        self.check_with_sources(preflight.database_check)
        self.check_with_sources(preflight.stripe_check)

    def test_independent_confinement_remains_a_blocker(self):
        with self.assertRaisesRegex(RuntimeError, "independent OS-enforced filesystem isolation"):
            preflight.confinement_check()

    def test_network_isolation_remains_a_separate_blocker(self):
        with self.assertRaisesRegex(RuntimeError, "independent OS-enforced outbound-denial"):
            preflight.isolation_check(("synthetic-sid", True, 0, "synthetic-appcontainer-sid"))

    def test_stripe_patch_after_main_fails(self):
        source = self.fixture.replace('    import main\n', '    import main\n', 1)
        marker = '    stack.enter_context(patch.object(stripe_state, "stripe_get", side_effect=fixtures.stripe.get))\n'
        source = source.replace(marker, '').replace('    import main\n', '    import main\n' + marker)
        with self.assertRaises(RuntimeError):
            self.check_with_sources(preflight.stripe_check, fixture=source)

    def test_sqlite_guard_removal_fails(self):
        for marker in ('patch.object(module, "connect", confined_connect)',
                       'patch.object(module, "Connection", confined_class)'):
            with self.subTest(marker=marker), self.assertRaises(RuntimeError):
                self.check_with_sources(preflight.database_check, fixture=self.fixture.replace(marker, 'None'))
        with self.assertRaises(RuntimeError):
            self.check_with_sources(preflight.database_check,
                                    guards=self.guards.replace('sqlite3.SQLITE_ATTACH', '0'))

    def test_sources_parse_without_importing_or_executing_them(self):
        for path in (preflight.FIXTURE, preflight.GUARDS, Path(preflight.__file__)):
            with self.subTest(path=path):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
    unittest.main()
