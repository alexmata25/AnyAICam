import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Match the convention every other partner_db-touching test file uses: point
# ANYAICAM_PARTNER_DB at a writable temp path before the first `import
# partner_db`, since that import itself triggers schema initialization against
# whatever target is configured at the time.
_IMPORT_TIME_DB = Path(tempfile.gettempdir()) / 'anyaicam-partner-db-initialization-test.db'
_IMPORT_TIME_DB.unlink(missing_ok=True)
os.environ.setdefault('ANYAICAM_DATABASE_BACKEND', 'sqlite')
os.environ['ANYAICAM_PARTNER_DB'] = str(_IMPORT_TIME_DB)

import database_backend
import partner_db


class PartnerDbInitializationTests(unittest.TestCase):
    """Regression coverage for Cluster A: partner_db.ensure_database_initialized()
    must be safely re-runnable for whichever ANYAICAM_PARTNER_DB path is currently
    configured, instead of depending on a one-time import side effect."""

    def setUp(self):
        self._original_env = os.environ.get('ANYAICAM_PARTNER_DB')
        self._original_targets = set(partner_db._initialized_targets)
        self._original_initialize_database = partner_db.initialize_database
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self._restore_state)

    def _restore_state(self):
        if self._original_env is None:
            os.environ.pop('ANYAICAM_PARTNER_DB', None)
        else:
            os.environ['ANYAICAM_PARTNER_DB'] = self._original_env
        partner_db._initialized_targets.clear()
        partner_db._initialized_targets.update(self._original_targets)
        partner_db.initialize_database = self._original_initialize_database

    def _use_path(self, name: str) -> Path:
        path = Path(self.temp_dir.name) / name
        os.environ['ANYAICAM_PARTNER_DB'] = str(path)
        return path

    def _table_names(self, db) -> set:
        return {item['name'] for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def test_two_isolated_targets_each_get_their_own_schema_and_data(self):
        path_a = self._use_path('target-a.db')
        with partner_db.connection() as db:
            self.assertIn('partners', self._table_names(db))
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('only-in-a','A','approved','real','now')")
        self.assertTrue(path_a.exists())

        path_b = self._use_path('target-b.db')
        # Switching targets must re-run initialization against B specifically,
        # not assume B already has A's schema/data just because *a* target was
        # already marked initialized.
        with partner_db.connection() as db:
            self.assertIn('partners', self._table_names(db))
            count = db.execute('SELECT COUNT(*) AS count FROM partners').fetchone()['count']
        self.assertEqual(count, 0, "target B must not see target A's rows")
        self.assertTrue(path_b.exists())

        with partner_db.connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('only-in-b','B','approved','real','now')")

        # Switching back to A must not reuse B's schema/data either.
        os.environ['ANYAICAM_PARTNER_DB'] = str(path_a)
        with partner_db.connection() as db:
            ids = {row['id'] for row in db.execute('SELECT id FROM partners').fetchall()}
        self.assertEqual(ids, {'only-in-a'})

    def test_each_target_is_initialized_only_once(self):
        self._use_path('once.db')
        calls = []
        real_initialize = partner_db.initialize_database
        def counting_wrapper():
            calls.append(1)
            real_initialize()
        partner_db.initialize_database = counting_wrapper

        partner_db.ensure_database_initialized()
        partner_db.ensure_database_initialized()
        partner_db.ensure_database_initialized()

        self.assertEqual(len(calls), 1, "repeat calls for the same target must not re-run schema setup")

    def test_failed_initialization_is_not_cached_as_done(self):
        self._use_path('broken.db')
        def failing():
            raise RuntimeError('simulated schema failure')
        partner_db.initialize_database = failing

        with self.assertRaises(RuntimeError):
            partner_db.ensure_database_initialized()
        self.assertNotIn(database_backend.target_key(), partner_db._initialized_targets)

        # A subsequent call using the real initializer must retry, not skip,
        # because the earlier failure was never recorded as a success.
        partner_db.initialize_database = self._original_initialize_database
        partner_db.ensure_database_initialized()
        self.assertIn(database_backend.target_key(), partner_db._initialized_targets)
        with partner_db.connection() as db:
            self.assertIn('partners', self._table_names(db))

    def test_concurrent_callers_for_a_new_target_initialize_only_once(self):
        self._use_path('concurrent.db')
        calls = []
        counting_lock = threading.Lock()
        real_initialize = partner_db.initialize_database
        def counting_wrapper():
            with counting_lock:
                calls.append(1)
            real_initialize()
        partner_db.initialize_database = counting_wrapper

        start = threading.Barrier(8)
        def worker():
            start.wait()
            partner_db.ensure_database_initialized()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()

        self.assertEqual(len(calls), 1, "concurrent callers for one target must not race to initialize twice")
        with partner_db.connection() as db:
            self.assertIn('partners', self._table_names(db))


if __name__ == '__main__':
    unittest.main()
