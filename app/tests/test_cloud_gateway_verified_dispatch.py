"""Source-extracted verified-dispatch/checkout-guard wiring; no VMS startup."""
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class VerifiedDispatchWiringTests(unittest.TestCase):
    def test_cloud_step_in_existing_verified_dispatcher(self):
        source=(Path(__file__).resolve().parents[1]/'main.py').read_text(encoding='utf-8')
        start=source.index('def _stripe_webhook_steps() -> list:')
        end=source.index('def _stripe_webhook_has_step_rows',start)
        ns={'process_stripe_webhook_event':lambda event:None}
        exec(compile(source[start:end],'verified_steps','exec'),ns)
        steps=ns['_stripe_webhook_steps'](); names=[name for name,fn in steps]
        self.assertLess(names.index('billing_status'),names.index('cloud_gateway_billing'))
        self.assertLess(names.index('billing_status'),names.index('gateway_checkout_attempts'))
        self.assertLess(names.index('gateway_checkout_attempts'),names.index('cloud_gateway_billing'))
        self.assertLess(names.index('cloud_gateway_billing'),names.index('checkout_sessions'))
        self.assertLess(source.index('if not verify_stripe_webhook_signature(payload, signature):'),source.index('for step_name, run_step in _stripe_webhook_steps():'))
        seen=[]
        with patch.dict(sys.modules,{'cloud_gateway_subscription_evidence':SimpleNamespace(sync_from_verified_webhook=lambda event:seen.append(event))}):
            dict(steps)['cloud_gateway_billing']({'id':'synthetic'})
        self.assertEqual(seen,[{'id':'synthetic'}])

    def test_cloud_checkout_claim_waits_for_cloud_step(self):
        source=(Path(__file__).resolve().parents[1]/'checkout_guard.py').read_text(encoding='utf-8')
        start=source.index('def _provisioned('); end=source.index('def sync_from_stripe_event',start)
        states={'done':['camera_slot_entitlements','analytics_entitlements','hardware_orders']}
        class DB:
            def execute(self,sql,args): return SimpleNamespace(fetchall=lambda:[{'step':x} for x in states['done']])
        @contextmanager
        def connection(): yield DB()
        ns={'PROVISIONING_STEPS':('camera_slot_entitlements','analytics_entitlements','hardware_orders'),'connection':connection}
        exec(compile(source[start:end],'checkout_guard','exec',flags=__import__('__future__').annotations.compiler_flag),ns)
        with patch.dict(sys.modules,{'partner_db':SimpleNamespace(connection=connection)}):
            self.assertTrue(ns['_provisioned']('event',{'metadata':{}}))
            self.assertFalse(ns['_provisioned']('event',{'metadata':{'anyaicam_product':'cloud_gateway'}}))
            states['done'].append('cloud_gateway_billing')
            self.assertTrue(ns['_provisioned']('event',{'metadata':{'anyaicam_product':'cloud_gateway'}}))
            tagged={'metadata':{'anyaicam_product':'cloud_gateway','anyaicam_gateway_attempt':'attempt'}}
            self.assertFalse(ns['_provisioned']('event',tagged))
            states['done'].append('gateway_checkout_attempts')
            self.assertTrue(ns['_provisioned']('event',tagged))


if __name__ == '__main__': unittest.main()
