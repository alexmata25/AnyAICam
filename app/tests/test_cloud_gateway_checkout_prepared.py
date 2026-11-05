"""Synthetic handler tests: no application startup or provider calls."""
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app import cloud_gateway_checkout as checkout
from app import cloud_gateway_subscription_evidence as evidence
from app import cloud_gateway_camera_entitlements as camera_service


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        class Error(Exception):
            def __init__(self,status_code,detail): self.status_code=status_code; self.detail=detail
        class Router:
            def post(self,path):
                def decorator(fn): self.endpoint=fn; return fn
                return decorator
        self.Error=Error; self.router=Router()
        self.identity={'role':'customer_owner','customer_id':'a','email':'a@example.test'}
        self.request=SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={'x-csrf-token':'signed'})
        self.appliance={'id':'box','customer_id':'a','gateway_edition':'cloud_gateway'}
        class Result:
            def __init__(self,value): self.value=value
            def fetchone(self): return self.value
        class Db:
            def execute(inner,sql,args):
                return Result(self.appliance if args == ('box','a') else None)
        @contextmanager
        def transaction(): yield Db()
        self.guard=SimpleNamespace(canonical_stripe_customer=Mock(return_value='cus_test'),
            create_session=Mock(return_value={'url':'https://checkout.example.test/session'}))
        self.main=SimpleNamespace(PUBLIC_BASE_URL='https://example.test',require_stripe_price_matches_catalog=Mock())
        self.mapping=Mock(side_effect=lambda db,customer,appliance,camera:camera in {'cam1','cam2'})
        modules={'fastapi':SimpleNamespace(HTTPException=Error,Request=type('Request',(),{})),
                 'partner_portal':SimpleNamespace(partner_identity=lambda request:self.identity),
                 'partner_db':SimpleNamespace(transaction=transaction),
                 'database_backend':SimpleNamespace(lock_customer_camera_capacity=lambda db,customer:customer=='a'),
                 'cloud_gateway_camera_entitlements':SimpleNamespace(_camera_mapping=self.mapping,
                     cloud_price_classification=camera_service.cloud_price_classification),
                 'cloud_gateway_subscription_evidence':evidence,'checkout_guard':self.guard,'main':self.main}
        modules['gateway_checkout_handoff']=SimpleNamespace(create_session=lambda actor,**kw:self.guard.create_session(
            kw['customer_id'],kw['purchase'],price_id=kw['price_id'],quantity=len(kw['camera_ids']),fields=kw['fields']))
        modules['gateway_checkout_attempts']=SimpleNamespace(ATTEMPTS_SERVICE_READY=True)
        modules['token_security']=SimpleNamespace(unsign=lambda value:'csrf' if value=='signed' else None)
        self.modules=patch.dict(sys.modules,modules); self.modules.start(); self.addCleanup(self.modules.stop)
        self.env=patch.dict('os.environ',{'ANYAICAM_CLOUD_GATEWAY_CHECKOUT_ENABLED':'true',
                                        'ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY':'price_test_cloud'}); self.env.start(); self.addCleanup(self.env.stop)
        checkout.register_routes(self.router)

    def test_disabled_handoff_and_invalid_ownership(self):
        payload={'appliance_id':'box','camera_ids':['cam1']}
        with self.assertRaises(self.Error) as error: self.router.endpoint(payload,self.request)
        self.assertEqual(error.exception.status_code,503); self.guard.create_session.assert_not_called()
        self.identity['role']='customer_viewer'
        with self.assertRaises(self.Error) as error: self.router.endpoint(payload,self.request)
        self.assertEqual(error.exception.status_code,403)
        self.identity.update(role='customer_owner',customer_id='other')
        with self.assertRaises(self.Error) as error: self.router.endpoint(payload,self.request)
        self.assertEqual(error.exception.status_code,403)

    def test_duplicate_unowned_or_unverified_cameras_are_rejected(self):
        for cameras,expected in [(['cam1','cam1'],400),(['cam1','unknown'],409),([],400)]:
            with self.subTest(cameras=cameras),self.assertRaises(self.Error) as error:
                self.router.endpoint({'appliance_id':'box','camera_ids':cameras},self.request)
            self.assertEqual(error.exception.status_code,expected)
        self.guard.create_session.assert_not_called()

    def test_prepared_checkout_uses_server_verified_camera_count(self):
        with patch.object(checkout,'CHECKOUT_HANDOFF_VERIFIED',True):
            result=self.router.endpoint({'appliance_id':'box','camera_ids':['cam1','cam2']},self.request)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(self.guard.create_session.call_args.kwargs['quantity'],2)
        self.main.require_stripe_price_matches_catalog.assert_called_once_with('price_test_cloud',999,'month')

    def test_missing_or_unsigned_csrf_never_reaches_checkout(self):
        payload={'appliance_id':'box','camera_ids':['cam1']}
        for cookie,header in [('', ''),('signed','other'),('invalid','invalid')]:
            request=SimpleNamespace(cookies={'anyaicam_csrf':cookie},headers={'x-csrf-token':header})
            with self.subTest(cookie=cookie,header=header),self.assertRaises(self.Error) as error:
                self.router.endpoint(payload,request)
            self.assertEqual(error.exception.status_code,403)
        self.guard.create_session.assert_not_called()


if __name__ == '__main__': unittest.main()
