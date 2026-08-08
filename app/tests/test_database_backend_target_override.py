import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('ANYAICAM_DATABASE_BACKEND', 'sqlite')

import database_backend


class DatabaseBackendTargetOverrideTests(unittest.TestCase):
    """Regression coverage for the env-var collision this override replaces:
    os.environ['ANYAICAM_PARTNER_DB'] is process-global, and pytest imports
    every test module before running any test, so whichever module's import
    ran last left its path in os.environ for the entire execution phase -
    every test in the session shared one database regardless of which file
    "intended" its own. override_target() must be immune to that: its
    contextvars-backed value must win over os.environ for as long as it is
    entered, and os.environ must be consulted again once it exits."""

    def setUp(self):
        self._original_env = os.environ.get('ANYAICAM_PARTNER_DB')
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._original_env is None:
            os.environ.pop('ANYAICAM_PARTNER_DB', None)
        else:
            os.environ['ANYAICAM_PARTNER_DB'] = self._original_env

    def test_override_wins_over_a_later_os_environ_write(self):
        # Simulate the exact failure mode: an override is active (as it would
        # be for the duration of one test's setUp/tearDown window), and then
        # some *other* code - like another test module's import-time
        # os.environ['ANYAICAM_PARTNER_DB'] = ... - mutates the environment
        # variable in between. The override must still win.
        os.environ['ANYAICAM_PARTNER_DB'] = '/tmp/should-not-be-used-a.db'
        with database_backend.override_target(sqlite_path='/tmp/override-wins.db'):
            os.environ['ANYAICAM_PARTNER_DB'] = '/tmp/should-not-be-used-b.db'
            self.assertEqual(database_backend.sqlite_target_path(), Path('/tmp/override-wins.db'))
            self.assertEqual(database_backend.target_key(), ('sqlite', '/tmp/override-wins.db'))

    def test_os_environ_is_consulted_again_after_override_exits(self):
        with database_backend.override_target(sqlite_path='/tmp/temporary.db'):
            self.assertEqual(database_backend.sqlite_target_path(), Path('/tmp/temporary.db'))
        os.environ['ANYAICAM_PARTNER_DB'] = '/tmp/after-override.db'
        self.assertEqual(database_backend.sqlite_target_path(), Path('/tmp/after-override.db'))

    def test_sequential_overrides_do_not_leak_into_each_other(self):
        with database_backend.override_target(sqlite_path='/tmp/first.db'):
            self.assertEqual(database_backend.sqlite_target_path(), Path('/tmp/first.db'))
        with database_backend.override_target(sqlite_path='/tmp/second.db'):
            self.assertEqual(database_backend.sqlite_target_path(), Path('/tmp/second.db'))

    def test_nested_overrides_restore_the_outer_value_on_exit(self):
        with database_backend.override_target(sqlite_path='/tmp/outer.db'):
            with database_backend.override_target(sqlite_path='/tmp/inner.db'):
                self.assertEqual(database_backend.sqlite_target_path(), Path('/tmp/inner.db'))
            self.assertEqual(database_backend.sqlite_target_path(), Path('/tmp/outer.db'))

    def test_two_test_style_windows_never_see_each_others_target(self):
        """End-to-end proof using real connections: two overlapping-in-time
        (but not actually concurrent) "test windows" that each set up their
        own sqlite target via override_target must never read or write
        through to the other's file, however os.environ looks at any given
        moment."""
        with tempfile.TemporaryDirectory() as tmp:
            path_a = Path(tmp) / 'a.db'
            path_b = Path(tmp) / 'b.db'

            os.environ['ANYAICAM_PARTNER_DB'] = str(path_b)  # simulates a later module's import already having run
            with database_backend.override_target(sqlite_path=path_a):
                with database_backend.connect() as db:
                    db.execute('CREATE TABLE marker(owner TEXT)')
                    db.execute("INSERT INTO marker(owner) VALUES('a')")

            with database_backend.override_target(sqlite_path=path_b):
                with database_backend.connect() as db:
                    db.execute('CREATE TABLE marker(owner TEXT)')
                    db.execute("INSERT INTO marker(owner) VALUES('b')")

            with database_backend.override_target(sqlite_path=path_a):
                with database_backend.connect() as db:
                    owners = {row['owner'] for row in db.execute('SELECT owner FROM marker').fetchall()}
            self.assertEqual(owners, {'a'})

            with database_backend.override_target(sqlite_path=path_b):
                with database_backend.connect() as db:
                    owners = {row['owner'] for row in db.execute('SELECT owner FROM marker').fetchall()}
            self.assertEqual(owners, {'b'})


if __name__ == '__main__':
    unittest.main()
