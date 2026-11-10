"""Synthetic stable identifiers, ownership changes and possession boundary."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from app import gateway_stable_claim_id as service


class StableClaimTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row; self.addCleanup(self.db.close)
        self.db.executescript('''
        CREATE TABLE appliances(id TEXT PRIMARY KEY,customer_id TEXT,activated_via TEXT,gateway_edition TEXT);
        CREATE TABLE appliance_claims(id TEXT PRIMARY KEY,appliance_id TEXT,customer_id TEXT,status TEXT,revoked_at TEXT);
        CREATE TABLE gateway_stable_installation_claim_ids(claim_id TEXT PRIMARY KEY,appliance_id TEXT UNIQUE,original_claim_id TEXT UNIQUE,created_at TEXT);
        CREATE TABLE gateway_claim_recovery_requests(request_id TEXT PRIMARY KEY,appliance_id TEXT,customer_id TEXT,requested_by TEXT,status TEXT,created_at TEXT);
        CREATE UNIQUE INDEX one_recovery ON gateway_claim_recovery_requests(appliance_id) WHERE status='pending_manual_verification';
        CREATE TABLE audit_logs(action TEXT,entity TEXT);
        INSERT INTO appliances VALUES('a','customer-a','claim',NULL);
        INSERT INTO appliance_claims VALUES('challenge-1','a','customer-a','completed',NULL);
        ''')
        def audit(actor,action,entity_type,entity_id,details): self.db.execute('INSERT INTO audit_logs VALUES(?,?)',(action,entity_id))
        for p in (patch.object(service,'STABLE_CLAIM_ID_READY',True),patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=audit)})):
            p.start(); self.addCleanup(p.stop)
        self.owner={'role':'customer_owner','customer_id':'customer-a'}
    def issue(self): return service.issue_after_verified_claim(self.db,appliance_id='a',customer_id='customer-a',completed_claim_id='challenge-1')

    def test_stable_across_retries_and_owner_reads(self):
        claim=self.issue(); self.assertTrue(claim.startswith('AAC-'))
        self.assertEqual(self.issue(),claim)
        self.assertEqual(service.read_for_owner(self.db,self.owner,appliance_id='a'),claim)
        self.assertIsNone(service.read_for_owner(self.db,{'role':'customer_viewer','customer_id':'customer-a'},appliance_id='a'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM audit_logs').fetchone()[0],1)

    def test_database_reopen_preserves_identifier(self):
        claim=self.issue(); self.db.commit()
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'synthetic.db')
            backup=sqlite3.connect(path); self.db.backup(backup); backup.close()
            reopened=sqlite3.connect(path); reopened.row_factory=sqlite3.Row
            try: self.assertEqual(service.read_for_owner(reopened,self.owner,appliance_id='a'),claim)
            finally: reopened.close()

    def test_identifier_alone_cannot_transfer_or_read_other_customer(self):
        claim=self.issue()
        self.assertIsNone(service.read_for_owner(self.db,{'role':'customer_owner','customer_id':'customer-b'},appliance_id='a'))
        with self.assertRaises(service.StableClaimConflict):
            service.issue_after_verified_claim(self.db,appliance_id='a',customer_id='customer-b',completed_claim_id=claim)
        self.assertEqual(self.db.execute("SELECT customer_id FROM appliances WHERE id='a'").fetchone()[0],'customer-a')

    def test_transfer_requires_existing_claim_protocol_and_preserves_identifier(self):
        claim=self.issue()
        self.db.execute("UPDATE appliances SET customer_id='customer-b' WHERE id='a'")
        self.db.execute("UPDATE appliance_claims SET revoked_at='now' WHERE id='challenge-1'")
        new_owner={'role':'customer_owner','customer_id':'customer-b'}
        self.assertIsNone(service.read_for_owner(self.db,self.owner,appliance_id='a'))
        self.assertIsNone(service.read_for_owner(self.db,new_owner,appliance_id='a'))
        self.db.execute("INSERT INTO appliance_claims VALUES('challenge-2','a','customer-b','completed',NULL)")
        self.assertEqual(service.read_for_owner(self.db,new_owner,appliance_id='a'),claim)
        self.assertEqual(service.issue_after_verified_claim(self.db,appliance_id='a',customer_id='customer-b',completed_claim_id='challenge-2'),claim)

    def test_revoked_ambiguous_or_uncompleted_claim_fails(self):
        self.db.execute("UPDATE appliance_claims SET status='claimed'")
        with self.assertRaises(service.StableClaimConflict): self.issue()
        self.db.execute("UPDATE appliance_claims SET status='completed'")
        self.db.execute("INSERT INTO appliance_claims VALUES('other','a','customer-a','completed',NULL)")
        with self.assertRaises(service.StableClaimConflict): self.issue()
        self.assertEqual(self.db.execute('SELECT count(*) FROM gateway_stable_installation_claim_ids').fetchone()[0],0)

    def test_default_off(self):
        with patch.object(service,'STABLE_CLAIM_ID_READY',False): self.assertIsNone(self.issue())
        self.assertEqual(self.db.execute('SELECT count(*) FROM gateway_stable_installation_claim_ids').fetchone()[0],0)

    def test_source_issues_only_after_possession_and_claim_completion(self):
        source=(Path(__file__).resolve().parents[1]/'appliance_claims.py').read_text(encoding='utf-8')
        start=source.index('    def claim_complete('); end=source.index("    @app.get('/customer/claim-appliance'",start)
        complete=source[start:end]
        self.assertLess(complete.index('_possesses(claim, device_secret)'),complete.index('issue_after_verified_claim'))
        self.assertLess(complete.index("UPDATE appliance_claims SET appliance_id=?"),complete.index('issue_after_verified_claim'))
        self.assertIn('if gateway_stable_claim_id.STABLE_CLAIM_ID_READY:',complete)

    def test_unmounted_recovery_requires_owner_csrf_and_current_device_proof(self):
        claim=self.issue()
        class HTTPError(Exception):
            def __init__(self,status_code,detail): self.status_code=status_code
        class Router:
            def __init__(self): self.routes={}
            def get(self,path): return lambda fn:self.routes.setdefault(('GET',path),fn)
            def post(self,path): return lambda fn:self.routes.setdefault(('POST',path),fn)
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details): self.db.execute('INSERT INTO audit_logs VALUES(?,?)',(action,entity_id))
        state={'actor':{**self.owner,'email':'owner@example.test'},'device':{'id':'a','customer_id':'customer-a'},'grant':False}
        modules={'fastapi':SimpleNamespace(HTTPException=HTTPError,Request=type('Request',(),{})),
                 'partner_portal':SimpleNamespace(partner_identity=lambda request:state['actor']),
                 'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
                 'appliance_cloud':SimpleNamespace(authenticate_appliance=lambda request:state['device']),
                 'gateway_internal_provisioning':SimpleNamespace(internal_test_enabled=lambda:True),
                 'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:state['grant']),
                 'token_security':SimpleNamespace(unsign=lambda value:'csrf' if value=='signed' else None)}
        router=Router(); request=SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={'x-csrf-token':'signed'})
        with patch.dict(sys.modules,modules):
            service.register_unmounted_routes(router)
            recover=router.routes[('POST','/api/v2/customer/appliances/{appliance_id}/gateway-claim-id/recover')]
            request_lost=router.routes[('POST','/api/v2/customer/appliances/{appliance_id}/gateway-claim-id/recovery-request')]
            self.assertEqual(recover('a',request)['claim_id'],claim)
            with self.assertRaises(HTTPError) as error:
                request_lost('a',{'request_id':'r1'},SimpleNamespace(cookies={},headers={}))
            self.assertEqual(error.exception.status_code,403)
            self.assertEqual(request_lost('a',{'request_id':'r1'},request)['status'],'pending_manual_verification')
            queue=router.routes[('GET','/api/operations/internal-gateway/claim-recovery')]
            with self.assertRaises(HTTPError) as error: queue(request)
            self.assertEqual(error.exception.status_code,403)
            state['actor']={'role':'administrator','email':'admin@example.test'}
            with self.assertRaises(HTTPError) as error: queue(request)
            self.assertEqual(error.exception.status_code,403)
            state['grant']=True
            self.assertEqual(queue(request)['requests'][0]['status'],'pending_manual_verification')
            state['actor']={**self.owner,'email':'owner@example.test'}
            state['device']={'id':'other','customer_id':'customer-a'}
            with self.assertRaises(HTTPError) as error: recover('a',request)
            self.assertEqual(error.exception.status_code,403)
            state['device']={'id':'a','customer_id':'customer-a'}
            with self.assertRaises(HTTPError) as error: recover('a',SimpleNamespace(cookies={},headers={}))
            self.assertEqual(error.exception.status_code,403)
            state['actor']={'role':'customer_owner','customer_id':'customer-b'}
            with self.assertRaises(HTTPError) as error: recover('a',request)
            self.assertEqual(error.exception.status_code,403)
        self.assertEqual(self.db.execute("SELECT count(*) FROM audit_logs WHERE action='gateway.stable_claim_id_recovered'").fetchone()[0],1)

    def test_lost_credential_request_is_owner_scoped_and_never_transfers(self):
        claim=self.issue(); owner={**self.owner,'email':'owner@example.test'}
        with self.assertRaises(service.StableClaimConflict):
            service.request_lost_credential_review(self.db,{'role':'customer_owner','customer_id':'customer-b','email':'b@example.test'},appliance_id='a',request_id='request-b')
        result=service.request_lost_credential_review(self.db,owner,appliance_id='a',request_id='request-a')
        self.assertEqual(result['status'],'pending_manual_verification')
        self.assertTrue(service.request_lost_credential_review(self.db,owner,appliance_id='a',request_id='request-a')['duplicate'])
        with self.assertRaises(service.StableClaimConflict):
            service.request_lost_credential_review(self.db,owner,appliance_id='a',request_id='request-other')
        self.assertEqual(self.db.execute("SELECT customer_id FROM appliances WHERE id='a'").fetchone()[0],'customer-a')
        self.assertEqual(service.read_for_owner(self.db,owner,appliance_id='a'),claim)
        self.assertEqual(self.db.execute('SELECT count(*) FROM gateway_claim_recovery_requests').fetchone()[0],1)

    def test_lost_credential_request_requires_live_owner_and_claim(self):
        self.issue(); owner={**self.owner,'email':'owner@example.test'}
        self.db.execute("UPDATE appliance_claims SET revoked_at='now' WHERE id='challenge-1'")
        with self.assertRaises(service.StableClaimConflict):
            service.request_lost_credential_review(self.db,owner,appliance_id='a',request_id='request-a')
        self.assertEqual(self.db.execute('SELECT count(*) FROM gateway_claim_recovery_requests').fetchone()[0],0)

    def test_pending_recovery_does_not_follow_ownership_transfer(self):
        self.issue(); old={**self.owner,'email':'owner-a@example.test'}
        service.request_lost_credential_review(self.db,old,appliance_id='a',request_id='old-request')
        self.db.execute("UPDATE appliances SET customer_id='customer-b' WHERE id='a'")
        self.db.execute("UPDATE appliance_claims SET revoked_at='now' WHERE id='challenge-1'")
        self.db.execute("INSERT INTO appliance_claims VALUES('challenge-2','a','customer-b','completed',NULL)")
        with self.assertRaises(service.StableClaimConflict):
            service.request_lost_credential_review(self.db,old,appliance_id='a',request_id='old-request')
        new={'role':'customer_owner','customer_id':'customer-b','email':'owner-b@example.test'}
        with self.assertRaises(service.StableClaimConflict):
            service.request_lost_credential_review(self.db,new,appliance_id='a',request_id='new-request')
        self.assertEqual(tuple(self.db.execute('SELECT customer_id,status FROM gateway_claim_recovery_requests').fetchone()),
                         ('customer-a','pending_manual_verification'))


if __name__ == '__main__': unittest.main()
