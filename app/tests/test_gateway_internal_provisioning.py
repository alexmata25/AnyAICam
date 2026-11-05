"""Isolated synthetic SQLite fixtures; never imports the application or migrations."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app import gateway_internal_provisioning as service
from app import gateway_edition_evidence as evidence
from app import gateway_internal_test_routes as prepared_routes
from app import local_gateway_remote_billing as billing


SCHEMA = '''
CREATE TABLE appliances(id TEXT PRIMARY KEY, customer_id TEXT, activated_via TEXT, gateway_edition TEXT, state TEXT, activation_status TEXT);
CREATE TABLE appliance_claims(id TEXT PRIMARY KEY, appliance_id TEXT, customer_id TEXT, status TEXT, revoked_at TEXT);
CREATE TABLE internal_gateway_provisioning_authorizations(id TEXT PRIMARY KEY, customer_id TEXT, appliance_id TEXT, claim_id TEXT, edition TEXT, basis TEXT, authorized_by TEXT, created_at TEXT, status TEXT, version INTEGER, audit_reference TEXT);
CREATE UNIQUE INDEX auth_current ON internal_gateway_provisioning_authorizations(appliance_id) WHERE status='active';
CREATE TABLE appliance_edition_provisioning_decisions(id TEXT PRIMARY KEY, customer_id TEXT, appliance_id TEXT, claim_id TEXT, edition TEXT, evidence_source TEXT, evidence_reference TEXT, evidence_version TEXT, verified_by TEXT, verified_at TEXT, revoked_at TEXT);
CREATE UNIQUE INDEX decision_current ON appliance_edition_provisioning_decisions(appliance_id) WHERE revoked_at IS NULL;
CREATE TABLE audit_logs(actor_email TEXT, actor_role TEXT, action TEXT, entity_type TEXT, entity_id TEXT, details_json TEXT);
INSERT INTO appliances VALUES('device-a','customer-a','claim',NULL,'online','pending');
INSERT INTO appliance_claims VALUES('claim-a','device-a','customer-a','completed',NULL);
'''


class InternalProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA); self.addCleanup(self.db.close)
        self.actor = {'role':'administrator','email':'admin@example.test'}
        self.granted = True
        def audit(actor, action, entity_type, entity_id, details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',
                            (actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        @contextmanager
        def transaction():
            with self.db: yield self.db
        self.patcher = patch.dict(sys.modules, {'partner_db':SimpleNamespace(audit=audit,transaction=transaction),
            'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:self.granted),
            'gateway_internal_provisioning':service,
            'cloud_config':SimpleNamespace(settings=SimpleNamespace(environment='test'))})
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        env = patch.dict('os.environ', {'ANYAICAM_ENV':'test','ANYAICAM_INTERNAL_GATEWAY_PROVISIONING_ENABLED':'true'})
        env.start(); self.addCleanup(env.stop)
        self.args = dict(request_id='request-a',customer_id='customer-a',appliance_id='device-a',claim_id='claim-a',edition='local_gateway')

    def authorize(self):
        return service.authorize_internal_test(self.db,self.actor,**self.args)

    def confirm(self, **changes):
        values = dict(authorization_id='request-a',customer_id='customer-a',appliance_id='device-a',claim_id='claim-a',edition='local_gateway',version=1)
        return service.confirm_internal_test(self.db,self.actor,**{**values,**changes})

    def test_authorization_verification_confirmation_and_retry(self):
        self.assertEqual(self.authorize()['status'],'authorized')
        self.assertEqual(self.authorize()['status'],'already_authorized')
        result = evidence.verify_edition_evidence(self.db,source=evidence.AUTHORIZED_INTERNAL_PROVISIONING,
            reference='request-a',customer_id='customer-a',appliance_id='device-a',claim_id='claim-a',edition='local_gateway',version=1)
        self.assertTrue(result); self.assertIn('not purchase evidence',result.reason)
        self.assertEqual(self.confirm()['status'],'confirmed')
        self.assertEqual(self.confirm()['status'],'already_confirmed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT gateway_edition FROM appliances').fetchone()[0],'local_gateway')

    def test_missing_revoked_stale_and_wrong_customer_are_rejected(self):
        with self.assertRaises(service.ProvisioningError): self.confirm()
        self.authorize()
        for changes in ({'version':2},{'customer_id':'customer-b'},{'claim_id':'other'},{'edition':'cloud_gateway'}):
            with self.subTest(changes=changes), self.assertRaises(service.ProvisioningError): self.confirm(**changes)
        self.db.execute("UPDATE internal_gateway_provisioning_authorizations SET status='revoked'")
        with self.assertRaises(service.ProvisioningError): self.confirm()

    def test_ambiguous_claim_and_ownership_change_are_rejected(self):
        self.authorize()
        self.db.execute("INSERT INTO appliance_claims VALUES('claim-b','device-a','customer-a','completed',NULL)")
        with self.assertRaises(service.ProvisioningError): self.confirm()
        self.db.execute("DELETE FROM appliance_claims WHERE id='claim-b'")
        self.db.execute("UPDATE appliances SET customer_id='customer-b'")
        with self.assertRaises(service.ProvisioningError): self.confirm()

    def test_revoke_after_transfer_or_invalid_claim_only_with_recorded_identity(self):
        self.authorize()
        self.db.execute("UPDATE appliances SET customer_id='customer-b'")
        self.db.execute("UPDATE appliance_claims SET revoked_at='now'")
        params = dict(authorization_id='request-a',customer_id='customer-a',appliance_id='device-a',claim_id='claim-a',edition='local_gateway',version=1)
        with self.assertRaises(service.ProvisioningError): service.change_authorization(self.db,self.actor,**params,action='advance')
        with self.assertRaises(service.ProvisioningError): service.change_authorization(self.db,self.actor,**{**params,'customer_id':'customer-b'},action='revoke')
        with self.assertRaises(service.ProvisioningError): service.change_authorization(self.db,self.actor,**{**params,'version':2},action='revoke')
        self.assertEqual(service.change_authorization(self.db,self.actor,**params,action='revoke')['status'],'revoked')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='gateway.internal_test_revoked'").fetchone()[0],1)

    def test_revoked_decision_denies_remote_until_fresh_explicit_confirmation(self):
        self.authorize(); self.confirm()
        def row(sql,args):
            if 'FROM appliances' in sql:
                record = self.db.execute('SELECT * FROM appliances WHERE id=?',args).fetchone()
                return dict(record) if record else None
            if 'local_gateway_remote_subscriptions' in sql:
                return {'customer_id':'customer-a','status':'active','payment_failed_at':None}
            raise AssertionError(sql)
        @contextmanager
        def transaction():
            with self.db: yield self.db
        with patch.dict(sys.modules, {'partner_db':SimpleNamespace(row=row,transaction=transaction),
                                      'appliance_readiness':SimpleNamespace(appliance_activated=lambda a: True)}):
            self.assertTrue(billing.remote_access_for_appliance('customer-a','device-a'))
        params = dict(authorization_id='request-a',customer_id='customer-a',appliance_id='device-a',claim_id='claim-a',edition='local_gateway',version=1)
        service.change_authorization(self.db,self.actor,**params,action='revoke')
        with patch.dict(sys.modules, {'partner_db':SimpleNamespace(row=row,transaction=transaction),
                                      'appliance_readiness':SimpleNamespace(appliance_activated=lambda a: True)}):
            self.assertFalse(billing.remote_access_for_appliance('customer-a','device-a'))
        self.assertEqual(self.db.execute('SELECT gateway_edition FROM appliances').fetchone()[0],'local_gateway')
        service.authorize_internal_test(self.db,self.actor,**{**self.args,'request_id':'request-b'})
        self.assertFalse(service.edition_authorization_effective(self.db,'customer-a','device-a','local_gateway'))
        self.assertEqual(self.confirm(authorization_id='request-b')['status'],'confirmed')
        self.assertTrue(service.edition_authorization_effective(self.db,'customer-a','device-a','local_gateway'))
        self.assertEqual(self.confirm(authorization_id='request-b')['status'],'already_confirmed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM appliance_edition_provisioning_decisions').fetchone()[0],2)

    def test_only_historical_decisions_are_not_treated_as_legacy(self):
        self.authorize(); self.confirm()
        self.db.execute("UPDATE appliance_edition_provisioning_decisions SET revoked_at='now'")
        self.assertFalse(service.edition_authorization_effective(self.db,'customer-a','device-a','local_gateway'))

    def test_gateway_without_decision_fails_closed(self):
        self.assertFalse(service.edition_authorization_effective(self.db,'customer-a','device-a','local_gateway'))

    def test_conflicts_and_missing_audit_fail_closed(self):
        self.authorize()
        with self.assertRaises(service.ProvisioningError):
            service.authorize_internal_test(self.db,self.actor,**{**self.args,'request_id':'other','edition':'cloud_gateway'})
        self.db.execute('DELETE FROM audit_logs')
        with self.assertRaises(service.ProvisioningError): self.confirm()

    def test_live_global_grant_required(self):
        self.granted = False
        with self.assertRaises(service.ProvisioningError): self.authorize()
        self.granted = True; self.authorize(); self.granted = False
        with self.assertRaises(service.ProvisioningError): self.confirm()

    def test_versions_and_revocation_are_audited_and_stale_versions_fail(self):
        self.authorize()
        params = dict(authorization_id='request-a',customer_id='customer-a',appliance_id='device-a',claim_id='claim-a',edition='local_gateway')
        self.assertEqual(service.change_authorization(self.db,self.actor,**params,version=1,action='advance')['version'],2)
        with self.assertRaises(service.ProvisioningError): self.confirm(version=1)
        self.assertTrue(service.verify_internal_test(self.db,'request-a','customer-a','device-a','claim-a','local_gateway',2))
        self.granted = False
        with self.assertRaises(service.AuthorizationDenied): service.change_authorization(self.db,self.actor,**params,version=2,action='revoke')
        self.granted = True
        self.assertEqual(service.change_authorization(self.db,self.actor,**params,version=2,action='revoke')['status'],'revoked')
        self.assertFalse(service.verify_internal_test(self.db,'request-a','customer-a','device-a','claim-a','local_gateway',3))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0],3)

    def test_feature_gate_requires_explicit_consistent_test_environment(self):
        for env in ({'ANYAICAM_ENV':'production','ANYAICAM_INTERNAL_GATEWAY_PROVISIONING_ENABLED':'true'},
                    {'ANYAICAM_ENV':'test','ANYAICAM_INTERNAL_GATEWAY_PROVISIONING_ENABLED':'false'},
                    {'ANYAICAM_ENV':'','ANYAICAM_INTERNAL_GATEWAY_PROVISIONING_ENABLED':'true'},
                    {'ANYAICAM_ENV':'development','ANYAICAM_INTERNAL_GATEWAY_PROVISIONING_ENABLED':'true'}):
            with self.subTest(env=env), patch.dict('os.environ',env):
                self.assertFalse(service.internal_test_enabled())
                with self.assertRaises(service.AuthorizationDenied): self.authorize()

    def test_cloud_authorization_is_internal_only(self):
        service.authorize_internal_test(self.db,self.actor,**{**self.args,'edition':'cloud_gateway'})
        self.assertEqual(self.confirm(edition='cloud_gateway')['status'],'confirmed')
        self.assertEqual(self.db.execute('SELECT gateway_edition FROM appliances').fetchone()[0], 'cloud_gateway')
        self.assertEqual(self.db.execute('SELECT evidence_source FROM appliance_edition_provisioning_decisions').fetchone()[0], 'authorized_internal_provisioning')

    def test_authorization_audit_failure_rolls_back(self):
        self.db.commit()
        broken = SimpleNamespace(audit=lambda *args: (_ for _ in ()).throw(RuntimeError('audit unavailable')))
        with patch.dict(sys.modules, {'partner_db':broken}):
            with self.assertRaises(RuntimeError):
                with self.db: self.authorize()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM internal_gateway_provisioning_authorizations').fetchone()[0],0)

    def test_revocation_audit_failure_rolls_back(self):
        self.authorize(); self.db.commit()
        broken = SimpleNamespace(audit=lambda *args: (_ for _ in ()).throw(RuntimeError('audit unavailable')))
        with patch.dict(sys.modules, {'partner_db':broken}):
            with self.assertRaises(RuntimeError):
                with self.db:
                    service.change_authorization(self.db,self.actor,authorization_id='request-a',customer_id='customer-a',
                        appliance_id='device-a',claim_id='claim-a',edition='local_gateway',version=1,action='revoke')
        current = self.db.execute('SELECT status,version FROM internal_gateway_provisioning_authorizations').fetchone()
        self.assertEqual(tuple(current),('active',1))

    def test_unmounted_route_authorization_csrf_and_conflicts(self):
        class HttpError(Exception):
            def __init__(self, status_code, detail): self.status_code, self.detail = status_code, detail
        class Router:
            def __init__(self): self.handlers = {}
            def post(self, path): return lambda fn: self.handlers.setdefault(path,fn)
        identity = {'value':self.actor}
        mocks = {'fastapi':SimpleNamespace(HTTPException=HttpError,Request=object),
                 'partner_portal':SimpleNamespace(partner_identity=lambda request:identity['value']),
                 'token_security':SimpleNamespace(unsign=lambda token:'csrf' if token == 'signed' else None)}
        request = SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={'x-csrf-token':'signed'})
        common = dict(customer_id='customer-a',appliance_id='device-a',claim_id='claim-a',edition='local_gateway')
        with patch.dict(sys.modules, mocks):
            router = Router(); prepared_routes.register_unmounted_test_routes(router)
            prefix = '/api/operations/internal-gateway/authorizations'
            authorize = router.handlers[prefix]; confirm = router.handlers[prefix+'/{authorization_id}/confirm']
            revoke = router.handlers[prefix+'/{authorization_id}/revoke']
            advance = router.handlers[prefix+'/{authorization_id}/version']
            identity['value'] = {'role':'customer_owner','email':'owner@example.test'}
            with self.assertRaises(HttpError) as error: authorize({**common,'request_id':'request-a'},request)
            self.assertEqual(error.exception.status_code,403)
            identity['value'] = self.actor
            bad_csrf = SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={})
            with self.assertRaises(HttpError) as error: authorize({**common,'request_id':'request-a'},bad_csrf)
            self.assertEqual(error.exception.status_code,403)
            self.granted = False
            with self.assertRaises(HttpError) as error: authorize({**common,'request_id':'request-a'},request)
            self.assertEqual(error.exception.status_code,403)
            self.granted = True
            with self.assertRaises(HttpError) as error: authorize({**common,'customer_id':'customer-b','request_id':'request-a'},request)
            self.assertEqual(error.exception.status_code,409)
            with self.assertRaises(HttpError) as error: confirm('request-a',{**common,'version':1},request)
            self.assertEqual(error.exception.status_code,409)
            self.assertEqual(authorize({**common,'request_id':'request-a'},request)['status'],'authorized')
            self.assertEqual(advance('request-a',{**common,'version':1},request)['version'],2)
            with self.assertRaises(HttpError) as error: confirm('request-a',{**common,'version':1},request)
            self.assertEqual(error.exception.status_code,409)
            self.assertEqual(confirm('request-a',{**common,'version':2},request)['status'],'confirmed')
            self.assertEqual(confirm('request-a',{**common,'version':2},request)['status'],'already_confirmed')
            with self.assertRaises(HttpError) as error: confirm('request-a',{**common,'edition':'cloud_gateway','version':2},request)
            self.assertEqual(error.exception.status_code,409)
            self.assertEqual(revoke('request-a',{**common,'version':2},request)['status'],'revoked')
            with self.assertRaises(HttpError) as error: confirm('request-a',{**common,'version':2},request)
            self.assertEqual(error.exception.status_code,409)
            with patch.dict('os.environ', {'ANYAICAM_INTERNAL_GATEWAY_PROVISIONING_ENABLED':'false'}):
                with self.assertRaises(HttpError) as error: confirm('request-a',{**common,'version':1},request)
                self.assertEqual(error.exception.status_code,404)

    def test_failed_audit_rolls_back_confirmation(self):
        self.authorize(); self.db.commit()
        broken = SimpleNamespace(audit=lambda *args: (_ for _ in ()).throw(RuntimeError('audit unavailable')))
        with patch.dict(sys.modules, {'partner_db':broken}):
            with self.assertRaises(RuntimeError):
                with self.db: self.confirm()
        self.assertIsNone(self.db.execute('SELECT gateway_edition FROM appliances').fetchone()[0])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM appliance_edition_provisioning_decisions').fetchone()[0],0)

    def test_concurrent_confirmations_produce_one_decision_and_audit(self):
        local = threading.local()
        def audit(actor, action, entity_type, entity_id, details):
            local.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',
                (actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'partner_db':SimpleNamespace(audit=audit)}):
            path = str(Path(directory) / 'synthetic.db')
            initial = sqlite3.connect(path); initial.row_factory = sqlite3.Row
            initial.executescript(SCHEMA); local.db = initial
            with initial: service.authorize_internal_test(initial, self.actor, **self.args)
            initial.close()
            barrier = threading.Barrier(2); results = []; errors = []
            def worker():
                db = sqlite3.connect(path, timeout=5); db.row_factory = sqlite3.Row; local.db = db
                try:
                    barrier.wait(timeout=5)
                    db.execute('BEGIN IMMEDIATE')
                    value = service.confirm_internal_test(db, self.actor, authorization_id='request-a', customer_id='customer-a',
                        appliance_id='device-a', claim_id='claim-a', edition='local_gateway', version=1)
                    db.commit(); results.append(value['status'])
                except Exception as error:
                    db.rollback(); errors.append(str(error))
                finally:
                    db.close()
            workers = [threading.Thread(target=worker) for _ in range(2)]
            for thread in workers: thread.start()
            for thread in workers: thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in workers))
            self.assertEqual(errors, [])
            self.assertCountEqual(results, ['confirmed','already_confirmed'])
            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute('SELECT COUNT(*) FROM appliance_edition_provisioning_decisions').fetchone()[0],1)
                self.assertEqual(check.execute("SELECT COUNT(*) FROM audit_logs WHERE action='gateway.internal_test_edition_confirmed'").fetchone()[0],1)
            finally: check.close()


if __name__ == '__main__': unittest.main()
