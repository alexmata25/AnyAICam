"""Synthetic route and policy checks without a database or provider."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import gateway_stable_claim_id as claim
from app import gateway_checkout_attempts as attempts
from app import gateway_checkout_webhook as webhook


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code


class Router:
    def __init__(self): self.routes = {}
    def get(self, path): return lambda fn: self.routes.setdefault(('GET',path),fn)
    def post(self, path): return lambda fn: self.routes.setdefault(('POST',path),fn)


class NoIoGatewayTests(unittest.TestCase):
    def test_disabled_checkout_and_stable_claim_never_query_database(self):
        db=SimpleNamespace(execute=lambda *a,**k:self.fail('database reached'))
        with patch.object(attempts,'ATTEMPTS_SERVICE_READY',False), patch.object(claim,'STABLE_CLAIM_ID_READY',False):
            self.assertEqual(attempts.reserve(db,None,attempt_id='a',customer_id='a',appliance_id='a',
                                             edition='local_gateway',stripe_customer_id='c',price_id='p',camera_ids=[]),
                             {'status':'unavailable'})
            self.assertIsNone(claim.read_for_owner(db,{'role':'customer_owner','customer_id':'a'},appliance_id='a'))

    def test_unmounted_claim_recovery_rejects_csrf_and_wrong_tenant_before_db(self):
        state={'actor':{'role':'customer_owner','customer_id':'a'}, 'device':{'id':'box','customer_id':'a'}}
        never=lambda *a,**k:self.fail('database or audit reached')
        modules={'fastapi':SimpleNamespace(HTTPException=HttpError,Request=type('Request',(),{})),
                 'partner_portal':SimpleNamespace(partner_identity=lambda req:state['actor']),
                 'partner_db':SimpleNamespace(transaction=never,audit=never),
                 'appliance_cloud':SimpleNamespace(authenticate_appliance=lambda req:state['device']),
                 'token_security':SimpleNamespace(unsign=lambda token:'csrf' if token=='signed' else None)}
        router=Router()
        with patch.dict(sys.modules,modules), patch.object(claim,'STABLE_CLAIM_ID_READY',True):
            claim.register_unmounted_routes(router)
            recover=router.routes[('POST','/api/v2/customer/appliances/{appliance_id}/gateway-claim-id/recover')]
            with self.assertRaises(HttpError) as error:
                recover('box',SimpleNamespace(cookies={},headers={}))
            self.assertEqual(error.exception.status_code,403)
            state['actor']={'role':'customer_owner','customer_id':'other'}
            with self.assertRaises(HttpError) as error:
                recover('box',SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={'x-csrf-token':'signed'}))
            self.assertEqual(error.exception.status_code,403)

    def test_disabled_webhook_step_refuses_gateway_event_without_provider(self):
        event={'type':'charge.refunded','data':{'object':{'id':'ch_synthetic','invoice':'in_synthetic'}}}
        with patch.dict(sys.modules,{'gateway_checkout_attempts':attempts}), patch.object(attempts,'ATTEMPTS_SERVICE_READY',False):
            self.assertEqual(webhook.sync_from_verified_webhook(event,fetch=lambda *a:self.fail('provider reached')),
                             {'status':'unavailable'})


if __name__ == '__main__': unittest.main()
