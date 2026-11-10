"""Exercise the actual JSON event-log functions using disposable files."""
import ast
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class WebhookJsonLogTests(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')
        names = ('load_json_file', 'save_json_file', 'load_payment_webhook_events',
                 'save_payment_webhook_events', 'record_stripe_webhook_event')
        functions = []
        for name in names:
            start = source.index('\ndef ' + name + '(') + 1
            end = source.find('\ndef ', start + 1)
            functions.append(source[start:end])
        self.temp = tempfile.TemporaryDirectory(prefix='aac-webhook-log-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'events.json'
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE payment_locks(key TEXT PRIMARY KEY,created_at TEXT NOT NULL)')
        self.lock = threading.RLock()
        @contextmanager
        def transaction():
            with self.lock, self.db:
                yield self.db
        self.ns = {'Path':Path, 'json':json, 'datetime':datetime, 'PAYMENT_WEBHOOK_EVENTS_FILE':self.path}
        exec(compile('\n'.join(functions), 'main.py:webhook_log', 'exec',
                     flags=__import__('__future__').annotations.compiler_flag), self.ns)
        billing_source = (Path(__file__).resolve().parents[1] / 'billing_status.py').read_text(encoding='utf-8')
        billing_tree = ast.parse(billing_source)
        lock_node = next(n for n in billing_tree.body if isinstance(n, ast.FunctionDef) and n.name == 'lock_payment')
        lock_ns = {'_now':datetime.now, '_LOCK_ORDER':{'ch':0,'pi':1,'in':2}}
        exec(compile(ast.Module(body=[lock_node], type_ignores=[]), 'billing_status.py:lock_payment', 'exec'), lock_ns)
        modules = patch.dict(sys.modules, {'partner_db':SimpleNamespace(transaction=transaction),
                                         'database_backend':SimpleNamespace(backend=lambda:'sqlite'),
                                         'billing_status':SimpleNamespace(lock_payment=lock_ns['lock_payment'])})
        modules.start(); self.addCleanup(modules.stop)

    def event(self, name):
        return {'id':name, 'type':'invoice.paid', 'created':1, 'livemode':False}

    def test_concurrent_distinct_events_do_not_lose_each_other(self):
        barrier = threading.Barrier(2, timeout=3)
        errors = []
        def record(name):
            try:
                barrier.wait()
                self.ns['record_stripe_webhook_event'](self.event(name))
            except Exception as exc: errors.append(exc)
        workers = [threading.Thread(target=record,args=(name,)) for name in ('evt-a','evt-b')]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(set(json.loads(self.path.read_text(encoding='utf-8'))), {'evt-a','evt-b'})
        self.assertFalse(self.ns['record_stripe_webhook_event'](self.event('evt-a')))

    def test_interrupted_replace_preserves_old_log_and_retry_succeeds(self):
        self.assertTrue(self.ns['record_stripe_webhook_event'](self.event('evt-a')))
        with patch.object(Path, 'replace', side_effect=OSError('synthetic replacement failure')):
            with self.assertRaises(OSError): self.ns['record_stripe_webhook_event'](self.event('evt-b'))
        self.assertEqual(set(json.loads(self.path.read_text(encoding='utf-8'))), {'evt-a'})
        self.assertTrue(self.ns['record_stripe_webhook_event'](self.event('evt-b')))
        self.assertEqual(set(json.loads(self.path.read_text(encoding='utf-8'))), {'evt-a','evt-b'})

    def test_corrupt_log_is_not_overwritten(self):
        self.path.write_text('{invalid', encoding='utf-8')
        with self.assertRaises((ValueError, OSError)):
            self.ns['record_stripe_webhook_event'](self.event('evt-a'))
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{invalid')
