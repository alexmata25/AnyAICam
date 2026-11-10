"""Bounded extraction of the real verified-event dispatcher; no VMS import."""
from pathlib import Path
import ast
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')
        anchor = source.index('# Provisioning Phase 7: record_stripe_webhook_event() below')
        start = source.index('    event_id = str(event.get("id") or "")', anchor)
        end = source.index('\n\n# A step left', start)
        self.seen, self.steps = set(), {}
        self.attempts = {}
        self.lock = threading.RLock()
        self.effects = []
        self.handler = lambda event: self.effects.append(event['id'])
        self.ns = {'HTTPException':HttpError, 'record_stripe_webhook_event':self.record,
                   'load_payment_webhook_events':lambda:dict.fromkeys(self.seen),
                   '_stripe_webhook_has_step_rows':self.has_rows, '_stripe_webhook_steps':lambda:[('local_gateway_remote', self.handler)],
                   '_claim_stripe_webhook_step':self.claim, '_finish_stripe_webhook_step':self.finish,
                   'structured_log':lambda *args,**kwargs:None}
        exec(compile('def dispatch(event):\n' + source[start:end] + '\n', 'main.py:verified_dispatch', 'exec'), self.ns)
        self.notifications = patch.dict(sys.modules, {'purchase_notifications':SimpleNamespace(notify_from_stripe_event=lambda event:None)})
        self.notifications.start(); self.addCleanup(self.notifications.stop)

    def record(self, event):
        with self.lock:
            new = event['id'] not in self.seen
            self.seen.add(event['id'])
            return new

    def has_rows(self, event_id):
        with self.lock:
            return any(key[0] == event_id for key in self.steps)

    def claim(self, event_id, step, *, return_attempt=False):
        with self.lock:
            key = (event_id, step)
            state = self.steps.get(key)
            if state == 'completed': return ('already_completed', None) if return_attempt else 'already_completed'
            if state == 'running': return ('busy', None) if return_attempt else 'busy'
            self.steps[key] = 'running'
            self.attempts[key] = self.attempts.get(key, 0) + 1
            return ('claimed', self.attempts[key]) if return_attempt else 'claimed'

    def finish(self, event_id, step, status, *, error=None, attempt=None):
        with self.lock:
            if attempt is not None and (self.steps.get((event_id, step)) != 'running' or self.attempts.get((event_id, step)) != attempt):
                return False
            self.steps[(event_id, step)] = status
            return True

    def dispatch(self, event_id, event_type='invoice.paid'):
        return self.ns['dispatch']({'id':event_id, 'type':event_type})

    def test_duplicate_delivery_runs_completed_step_once(self):
        self.assertEqual(self.dispatch('evt-1')['status'], 'complete')
        self.assertTrue(self.dispatch('evt-1')['duplicate'])
        self.assertEqual(self.effects, ['evt-1'])

    def test_failed_step_is_retried_and_completed_once(self):
        attempts = []
        def handler(event):
            attempts.append(event['id'])
            if len(attempts) == 1: raise RuntimeError('synthetic write failure')
            self.effects.append(event['id'])
        self.handler = handler
        with self.assertRaises(HttpError) as error:
            self.dispatch('evt-retry')
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.dispatch('evt-retry')['status'], 'complete')
        self.assertTrue(self.dispatch('evt-retry')['duplicate'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.effects, ['evt-retry'])

    def test_overlapping_duplicate_reports_busy_then_replay_is_duplicate(self):
        entered, release = threading.Event(), threading.Event()
        def handler(event):
            entered.set()
            if not release.wait(3): raise TimeoutError('synthetic wait')
            self.effects.append(event['id'])
        self.handler = handler
        errors = []
        def worker():
            try: self.dispatch('evt-busy')
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(HttpError) as error: self.dispatch('evt-busy')
            self.assertEqual(error.exception.status_code, 503)
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.dispatch('evt-busy')['duplicate'])
        self.assertEqual(self.effects, ['evt-busy'])

    def test_recorded_event_without_step_rows_has_legacy_gap(self):
        # The log write and step claim are separate: a crash in between is
        # indistinguishable from an event handled before step tracking existed.
        self.seen.add('evt-gap')
        self.assertTrue(self.dispatch('evt-gap')['duplicate'])
        self.assertEqual(self.effects, [])

    def test_log_write_then_crash_keeps_marker_and_retries_step(self):
        def record_then_crash(event):
            self.record(event)
            raise RuntimeError('synthetic crash after log write')
        self.ns['record_stripe_webhook_event'] = record_then_crash
        with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
            self.dispatch('evt-crash')
        self.assertTrue(self.has_rows('evt-crash'))
        self.assertEqual(self.effects, [])
        self.ns['record_stripe_webhook_event'] = self.record
        self.assertEqual(self.dispatch('evt-crash')['status'], 'complete')
        self.assertEqual(self.effects, ['evt-crash'])
        self.assertTrue(self.dispatch('evt-crash')['duplicate'])

    def test_out_of_order_invoice_events_do_not_undo_new_paid_recovery(self):
        source = Path(__file__).resolve().parents[1] / 'billing_status.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        names = {'_invoice_subscription', '_invoice_payment_failed', '_restore_reasons', 'sync_from_stripe_event'}
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual(len(funcs), len(names))
        invoice_states = {'in-new':'paid'}
        remote = {'reason':'refunded', 'status':'suspended'}
        transitions = []
        def set_invoice(invoice_id, subscription, state):
            if invoice_states.get(invoice_id) in ('paid', 'void'): return False
            invoice_states[invoice_id] = state
            return True
        def recovered(subscription_id, reasons):
            if remote['status'] == 'suspended' and remote['reason'] in reasons:
                remote.update(status='active', reason=None)
                transitions.append('restored')
                return 1
            return 0
        provider = SimpleNamespace(current_subscription=lambda sid:{'id':sid,'status':'active','latest_invoice':'in-new'},
            _id=lambda value:value, stripe_get=lambda path,why:{'id':'in-new','status':'paid'},
            subscription_payment_reversal=lambda sub:None, RetryableStripeEventError=RuntimeError)
        ns = {'RESTORED_BY_PAYMENT':('payment_failed','refunded'), 'SETTLED_INVOICE_STATES':('paid','void'),
              'invoice_state':invoice_states.get, '_set_invoice_state':set_invoice,
              '_is_reversed':lambda *args:False, '_outstanding_failure':lambda *args,**kw:False,
              '_has_refunded_rows':lambda sid:remote['reason'] == 'refunded', 'payment_recovered':recovered,
              'mark_payment_failed':lambda *args,**kw:transitions.append('failed')}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(source), 'exec',
                     flags=__import__('__future__').annotations.compiler_flag), ns)
        self.handler = ns['sync_from_stripe_event']
        old = {'id':'evt-old', 'type':'invoice.paid', 'data':{'object':{'id':'in-old','subscription':'sub-a'}}}
        new = {'id':'evt-new', 'type':'invoice.paid', 'data':{'object':{'id':'in-new','subscription':'sub-a'}}}
        late_failure = {'id':'evt-late-failure', 'type':'invoice.payment_failed', 'data':{'object':{'id':'in-new','subscription':'sub-a'}}}
        with patch.dict(sys.modules, {'stripe_state':provider,'sales_commissions':SimpleNamespace(_invoice_payment_intent=lambda obj:'pi-test')}):
            self.assertEqual(self.ns['dispatch'](old)['status'], 'complete')
            self.assertEqual(remote['status'], 'suspended')
            self.assertEqual(self.ns['dispatch'](new)['status'], 'complete')
            self.assertEqual(remote['status'], 'active')
            self.assertEqual(self.ns['dispatch'](late_failure)['status'], 'complete')
            self.assertTrue(self.ns['dispatch'](new)['duplicate'])
        self.assertEqual(invoice_states['in-new'], 'paid')
        self.assertEqual(transitions, ['restored'])

    def test_dispatch_old_refund_after_new_paid_recovery_preserves_local_access(self):
        source = Path(__file__).resolve().parents[1] / 'billing_status.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        names = {'_invoice_subscription', '_restore_reasons', '_reverse_purchase_locked', 'sync_from_stripe_event'}
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(len(funcs), len(names))
        state = {'remote':'suspended', 'reason':'refunded', 'license':'active'}
        transitions, recorded = [], []
        def row(sql, args):
            if 'local_gateway_remote_subscriptions' in sql: return {'appliance_id':'a'}
            if 'subscription_payments' in sql and 'stripe_subscription_id=?' in sql:
                return {'id':'in-new','status':'paid','stripe_customer_id':'cus-a',
                        'stripe_charge_id':'ch-new','stripe_payment_intent_id':'pi-new'}
            return None
        def recovered(subscription_id, reasons):
            if state['remote'] == 'suspended' and state['reason'] in reasons:
                state.update(remote='active',reason=None); transitions.append('restored'); return 1
            return 0
        def suspend(subscription_id, reason, **kwargs):
            state.update(remote='suspended',reason=reason); transitions.append('suspended'); return 1
        provider = SimpleNamespace(current_subscription=lambda sid:{'id':sid,'customer':'cus-a','status':'active','latest_invoice':'in-new'},
            stripe_customer_accounts=lambda customer:set(), _id=lambda value:value,
            stripe_get=lambda path,why:{'id':'in-new','status':'paid'},
            subscription_payment_reversal=lambda sub:None, RetryableStripeEventError=RuntimeError)
        ns = {'RESTORED_BY_PAYMENT':('payment_failed','refunded'), '_set_invoice_state':lambda *args:True,
              '_is_reversed':lambda *args:False, '_outstanding_failure':lambda *args,**kwargs:False,
              '_has_refunded_rows':lambda sid:state['reason']=='refunded', 'payment_recovered':recovered,
              '_payment_for_charge':lambda *args:None, '_record_reversal':lambda *args:recorded.append(args),
              '_hardware_payment':lambda intent:False,
              '_stripe_invoice_for_payment':lambda *args:{'invoice_id':'in-old','subscription_id':'sub-a','stripe_customer_id':'cus-a'},
              '_lock_invoice':lambda invoice:None, 'row':row, '_reverse_initial_licence':lambda *args,**kwargs:0,
              '_plan_rows':lambda sid:[], '_addon_rows':lambda sid:[], '_suspend':suspend,
              'logger':SimpleNamespace(warning=lambda *args,**kwargs:None)}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(source), 'exec',
                     flags=__import__('__future__').annotations.compiler_flag), ns)
        ns['_reverse_purchase'] = lambda event,reason:ns['_reverse_purchase_locked']('ch-old','pi-old','in-old',reason)
        self.handler = ns['sync_from_stripe_event']
        paid = {'id':'evt-paid','type':'invoice.paid','data':{'object':{'id':'in-new','subscription':'sub-a'}}}
        refund = {'id':'evt-refund','type':'charge.refunded','data':{'object':{'id':'ch-old'}}}
        with patch.dict(sys.modules, {'stripe_state':provider,'sales_commissions':SimpleNamespace(_invoice_payment_intent=lambda obj:'pi-new'),
                                      'local_gateway_remote_billing':SimpleNamespace(_table_available=lambda:True)}):
            self.ns['dispatch'](paid)
            self.ns['dispatch'](refund)
            self.assertTrue(self.ns['dispatch'](refund)['duplicate'])
        self.assertEqual(transitions, ['restored'])
        self.assertEqual(state, {'remote':'active','reason':None,'license':'active'})
        self.assertEqual(len(recorded), 2)  # same reversal enriched with its invoice by the handler
