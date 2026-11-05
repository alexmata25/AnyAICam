"""Claim route logic exercised in isolation with synthetic data and no database."""
from pathlib import Path
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import nullcontext
import sys


class Error(Exception):
    def __init__(self,status_code,detail): self.status_code,self.detail=status_code,detail


class ClaimRetryTests(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'appliance_claims.py').read_text(encoding='utf-8')
        begin = source[source.index('    def claim_begin('):source.index("    @app.post('/api/appliance/claim/status')")]
        complete = source[source.index('    def claim_complete('):source.index('    if shell is None:', source.index('    def claim_complete('))]
        self.ns = {'Request':object,'HTTPException':Error,'_client_ip':lambda request:'synthetic',
                   'claim_begin_limiter':SimpleNamespace(allow=lambda client:True),
                   'activation_limiter':SimpleNamespace(allow=lambda client:True),
                   '_valid_device_id':lambda value:True,'_normalize_device_id':lambda value:value,
                   '_device_secret':lambda payload:'synthetic-secret','_label_verifier_from':lambda payload:None,
                   '_clear_label_requested':lambda payload:False, '_possesses':lambda claim,secret:True,
                   'verify_password':lambda proof,hashed:True, 'connection':Mock(side_effect=AssertionError('unexpected write'))}
        exec(compile(textwrap.dedent(begin)+'\n'+textwrap.dedent(complete),'appliance_claims.py:routes','exec',
                     flags=__import__('__future__').annotations.compiler_flag), self.ns)

    def test_duplicate_begin_for_registered_device_is_rejected(self):
        self.ns['row'] = lambda sql,args:{'id':'existing-appliance'}
        with self.assertRaises(Error) as error:
            self.ns['claim_begin'](object(),{'device_id':'synthetic-id'})
        self.assertEqual(error.exception.status_code,409)
        self.ns['connection'].assert_not_called()

    def test_completed_claim_retry_returns_same_result_without_new_registration(self):
        claim = {'status':'completed','claim_proof_hash':'synthetic'}
        result = {'appliance_id':'same-appliance','credential':'synthetic-credential'}
        self.ns['row'] = lambda sql,args:claim
        self.ns['_recover_completed_result'] = Mock(return_value=result)
        payload = {'claim_session_id':'synthetic-session','claim_proof':'synthetic-proof'}
        self.assertEqual(self.ns['claim_complete'](object(),payload),result)
        self.assertEqual(self.ns['claim_complete'](object(),payload),result)
        self.ns['connection'].assert_not_called()

    def test_revoked_claim_cannot_complete_or_recover_credential(self):
        self.ns['row'] = lambda sql,args:{'status':'completed','claim_proof_hash':'synthetic','revoked_at':'revoked'}
        self.ns['_recover_completed_result'] = Mock()
        with self.assertRaises(Error) as error:
            self.ns['claim_complete'](object(),{'claim_session_id':'session','claim_proof':'proof'})
        self.assertEqual(error.exception.status_code,403)
        self.ns['_recover_completed_result'].assert_not_called()

    def test_claim_begin_fails_closed_when_session_index_unavailable(self):
        self.ns['row'] = lambda sql,args:None
        self.ns['connection'] = lambda:nullcontext(object())
        self.ns['_find_pending_claim_for_device'] = Mock()
        with patch.dict(sys.modules, {'gateway_schema_readiness':SimpleNamespace(claim_session_index_ready=lambda db:False)}):
            with self.assertRaises(Error) as error:
                self.ns['claim_begin'](object(),{'device_id':'synthetic-id'})
        self.assertEqual(error.exception.status_code,503)
        self.ns['_find_pending_claim_for_device'].assert_not_called()
