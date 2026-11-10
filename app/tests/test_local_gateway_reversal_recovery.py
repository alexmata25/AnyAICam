"""Mocked shared billing reversal tests without importing or starting the VMS."""
import ast
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class LocalReversalRecoveryTests(unittest.TestCase):
    def test_duplicate_concurrent_reversal_and_late_invoice_are_idempotent(self):
        source = Path(__file__).resolve().parents[1] / 'billing_status.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        target = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_record_reversal')
        db = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(db.close)
        lock = threading.RLock()
        @contextmanager
        def connection():
            with lock, db:
                yield db
        ns = {'connection':connection, '_now':lambda:datetime(2026, 1, 1),
              'REVERSALS_DDL':'CREATE TABLE IF NOT EXISTS billing_reversals(charge_id TEXT,payment_intent TEXT,invoice_id TEXT,reason TEXT,created_at TEXT)'}
        exec(compile(ast.Module(body=[target], type_ignores=[]), str(source), 'exec'), ns)
        barrier = threading.Barrier(2, timeout=3)
        errors = []
        def deliver():
            try:
                barrier.wait()
                ns['_record_reversal']('ch-a','pi-a','','refunded')
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=deliver) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        ns['_record_reversal']('ch-a','pi-a','in-a','refunded')
        ns['_record_reversal']('ch-a','pi-a','in-a','refunded')
        self.assertEqual(db.execute('SELECT charge_id,payment_intent,invoice_id,reason FROM billing_reversals').fetchall(),
                         [('ch-a','pi-a','in-a','refunded')])

    def test_unrecorded_old_refund_or_dispute_does_not_suspend_new_paid_local_subscription(self):
        source = Path(__file__).resolve().parents[1] / 'billing_status.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        target = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_reverse_purchase_locked')
        for reason in ('refunded', 'disputed'):
          for case in ('new_paid', 'new_reversed', 'new_not_recorded', 'same_invoice'):
            with self.subTest(reason=reason, case=case):
                suspend = Mock(return_value=1)
                def row(sql, args):
                    if 'local_gateway_remote_subscriptions' in sql:
                        return {'appliance_id':'appliance-a'}
                    if 'stripe_subscription_id=?' in sql and 'subscription_payments' in sql:
                        return None if case == 'new_not_recorded' else {'id':'in-new','status':'paid','stripe_customer_id':'cus-a','stripe_payment_intent_id':'pi-new','stripe_charge_id':'ch-new'}
                    return None
                ns = {'_payment_for_charge':Mock(return_value=None), '_record_reversal':Mock(),
                      '_hardware_payment':Mock(return_value=False), '_stripe_invoice_for_payment':Mock(return_value={
                          'invoice_id':'in-old','subscription_id':'sub-a','stripe_customer_id':'cus-a'}),
                      '_lock_invoice':Mock(), 'row':row, '_reverse_initial_licence':Mock(return_value=0),
                      '_plan_rows':Mock(return_value=[]), '_addon_rows':Mock(return_value=[]), '_suspend':suspend,
                      '_is_reversed':Mock(return_value=case == 'new_reversed'),
                      'logger':Mock()}
                exec(compile(ast.Module(body=[target], type_ignores=[]), str(source), 'exec'), ns)
                local = SimpleNamespace(_table_available=lambda:True, _verified_unreversed_invoice=lambda invoice,sub:True)
                state = SimpleNamespace(stripe_customer_accounts=lambda cid:set(),
                    current_subscription=lambda sid:{'id':sid,'customer':'cus-a','status':'active','latest_invoice':'in-old' if case == 'same_invoice' else 'in-new'},
                    _id=lambda value:value,
                    stripe_get=lambda path,why:{'id':'in-new','status':'paid','amount_paid':999})
                with patch.dict(sys.modules, {'stripe_state':state,'local_gateway_remote_billing':local}):
                    result = ns['_reverse_purchase_locked']('ch-old','pi-old','in-old',reason)
                if case == 'new_paid':
                    self.assertEqual(result['status'], 'ignored')
                    suspend.assert_not_called()
                else:
                    self.assertEqual(result['status'], 'suspended')
                    suspend.assert_called_once_with('sub-a', reason, include_local=True)
