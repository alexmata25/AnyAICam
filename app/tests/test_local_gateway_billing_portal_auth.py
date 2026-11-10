"""Existing billing portal route with synthetic identities and provider doubles."""
import ast
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class Error(Exception):
    def __init__(self, status_code, detail): self.status_code, self.detail = status_code, detail


class PortalAuthTests(unittest.TestCase):
    def test_owner_portal_uses_only_authenticated_customer_viewer_denied(self):
        source = Path(__file__).resolve().parents[1] / 'customer_billing.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'register_customer_billing_routes')
        identity = {'role':'customer_owner','customer_id':'customer-a'}
        provider = SimpleNamespace(PUBLIC_BASE_URL='https://example.test',
            stripe_api_post=Mock(return_value={'url':'https://billing.example.test'}))
        ns = {'FastAPI':object,'HTTPException':Error,'Request':object,'sys':sys,
              'portal_enabled':lambda:True,
              'stripe_customer_ids_for_customer':lambda customer_id,lookup:{'customer-a':{'cus-a'},'customer-b':{'cus-b'}}[customer_id],
              '_portal_configuration':lambda main:{'id':'config'}, 'configuration_problems':lambda config:[], 'audit':Mock()}
        exec(compile(ast.Module(body=[fn],type_ignores=[]), str(source), 'exec',
                     flags=__import__('__future__').annotations.compiler_flag), ns)
        class Router:
            def post(self,path):
                def register(fn): self.handler=fn; return fn
                return register
        router=Router()
        modules={'partner_portal':SimpleNamespace(partner_identity=lambda request:identity), 'main':provider,
                 'stripe_customer_mode':SimpleNamespace(in_current_mode=lambda cid,lookup:True,current_livemode=lambda:False)}
        with patch.dict(sys.modules,modules):
            ns['register_customer_billing_routes'](router)
            self.assertEqual(router.handler(object())['url'], 'https://billing.example.test')
            fields=provider.stripe_api_post.call_args.args[1]
            self.assertIn(('customer','cus-a'),fields)
            self.assertNotIn(('customer','cus-b'),fields)
            identity['role']='customer_viewer'
            with self.assertRaises(Error) as denied: router.handler(object())
            self.assertEqual(denied.exception.status_code,403)
            self.assertEqual(provider.stripe_api_post.call_count,1)
            identity.update(role='customer_owner',customer_id='customer-b')
            router.handler(object())
            self.assertIn(('customer','cus-b'),provider.stripe_api_post.call_args.args[1])
