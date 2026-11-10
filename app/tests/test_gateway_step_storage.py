"""Execute the actual step SQL in synthetic SQLite; no VMS or migrations."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class StepStorageTests(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')
        start = source.index('def _stripe_webhook_has_step_rows(')
        end = source.index('\n\n@app.get("/payment-setup"', start)
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE stripe_webhook_steps(event_id TEXT,step TEXT,status TEXT,attempts INTEGER,updated_at TEXT,last_error TEXT,PRIMARY KEY(event_id,step))')
        self.lock = threading.RLock()
        @contextmanager
        def connection():
            with self.lock, self.db:
                yield self.db
        self.ns = {'datetime':datetime, 'timedelta':timedelta, 'STRIPE_WEBHOOK_STEP_STALE_SECONDS':300}
        exec(compile(source[start:end], 'main.py:step_storage', 'exec',
                     flags=__import__('__future__').annotations.compiler_flag), self.ns)
        modules = patch.dict(sys.modules, {'partner_db':SimpleNamespace(connection=connection)})
        modules.start(); self.addCleanup(modules.stop)

    def test_concurrent_duplicate_claims_and_completed_replay(self):
        barrier = threading.Barrier(2, timeout=3)
        results, errors = [], []
        def worker():
            try:
                barrier.wait()
                results.append(self.ns['_claim_stripe_webhook_step']('evt','local'))
            except Exception as exc: errors.append(exc)
        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ['busy','claimed'])
        self.ns['_finish_stripe_webhook_step']('evt','local','completed')
        self.assertEqual(self.ns['_claim_stripe_webhook_step']('evt','local'), 'already_completed')
        self.assertEqual(self.db.execute('SELECT attempts FROM stripe_webhook_steps').fetchone()[0], 1)

    def test_interrupted_finish_stays_running_then_stale_claim_can_retry(self):
        claim, finish = self.ns['_claim_stripe_webhook_step'], self.ns['_finish_stripe_webhook_step']
        self.assertEqual(claim('evt','local'), 'claimed')
        self.db.execute("CREATE TRIGGER reject_finish BEFORE UPDATE OF status ON stripe_webhook_steps BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError): finish('evt','local','completed')
        self.assertEqual(claim('evt','local'), 'busy')
        self.db.execute('DROP TRIGGER reject_finish')
        self.db.execute("UPDATE stripe_webhook_steps SET updated_at='2000-01-01T00:00:00'")
        self.db.commit()
        self.assertEqual(claim('evt','local'), 'claimed')
        finish('evt','local','completed')
        self.assertEqual(claim('evt','local'), 'already_completed')
        self.assertEqual(self.db.execute('SELECT attempts FROM stripe_webhook_steps').fetchone()[0], 2)

    def test_stale_worker_cannot_finish_a_newer_attempt(self):
        claim, finish = self.ns['_claim_stripe_webhook_step'], self.ns['_finish_stripe_webhook_step']
        state, first = claim('evt','local',return_attempt=True)
        self.assertEqual(state, 'claimed')
        self.db.execute("UPDATE stripe_webhook_steps SET updated_at='2000-01-01T00:00:00'")
        self.db.commit()
        state, second = claim('evt','local',return_attempt=True)
        self.assertEqual(state, 'claimed')
        self.assertNotEqual(first, second)
        self.assertFalse(finish('evt','local','completed',attempt=first))
        self.assertEqual(claim('evt','local'), 'busy')
        self.assertTrue(finish('evt','local','completed',attempt=second))
        self.assertFalse(finish('evt','local','failed',error='late worker',attempt=first))
        self.assertEqual(claim('evt','local'), 'already_completed')
