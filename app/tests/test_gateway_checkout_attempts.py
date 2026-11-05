"""Synthetic attempt lifecycle; no migrations, provider or application startup."""
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
from app import gateway_checkout_attempts as service
from app import gateway_checkout_handoff as handoff
from app import gateway_checkout_webhook as webhook
from app import gateway_checkout_review as review
from app import cloud_gateway_subscription_evidence as evidence


SCHEMA='''
CREATE TABLE stripe_customer_bindings(customer_id TEXT, stripe_customer_id TEXT);
CREATE TABLE checkout_pending(customer_id TEXT,purchase TEXT,session_id TEXT,status TEXT);
CREATE TABLE appliance_edition_provisioning_decisions(id TEXT,customer_id TEXT,appliance_id TEXT,claim_id TEXT,edition TEXT,evidence_reference TEXT,evidence_version TEXT,revoked_at TEXT);
CREATE TABLE gateway_checkout_attempts(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,claim_id TEXT,edition TEXT,decision_id TEXT,authorization_reference TEXT,authorization_version TEXT,stripe_customer_id TEXT,stripe_price_id TEXT,quantity INTEGER,camera_ids_json TEXT,status TEXT,authorization_invalidated INTEGER NOT NULL DEFAULT 0,session_id TEXT UNIQUE,stripe_subscription_id TEXT,stripe_invoice_id TEXT UNIQUE,payment_event_id TEXT UNIQUE,reversal_state TEXT NOT NULL DEFAULT 'none',refund_charge_id TEXT,refund_event_id TEXT UNIQUE,created_at TEXT,updated_at TEXT);
CREATE TABLE gateway_checkout_review_actions(request_id TEXT PRIMARY KEY,attempt_id TEXT,actor_email TEXT,action TEXT,created_at TEXT);
CREATE TABLE cloud_gateway_webhook_correlation(event_id TEXT PRIMARY KEY,event_type TEXT,object_id TEXT,stripe_customer_id TEXT,event_created INTEGER,status TEXT,subscription_id TEXT,created_at TEXT);
CREATE UNIQUE INDEX one_unresolved ON gateway_checkout_attempts(appliance_id) WHERE status IN ('reserved','creating','open','review','paid_review');
CREATE TABLE audit_logs(actor TEXT,action TEXT,entity TEXT,details TEXT);
INSERT INTO stripe_customer_bindings VALUES('customer-a','cus-a');
INSERT INTO appliance_edition_provisioning_decisions VALUES('decision-a','customer-a','box-a','claim-a','local_gateway','auth-a','1',NULL);
'''


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row; self.db.executescript(SCHEMA); self.addCleanup(self.db.close)
        self.actor={'role':'customer_owner','customer_id':'customer-a','email':'owner@example.test'}
        self.authorized=True; self.cameras={'cam-a','cam-b'}
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        modules={'partner_db':SimpleNamespace(audit=audit),
                 'billing_status':SimpleNamespace(reversal_reason=lambda *args:None),
                 'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:self.authorized),
                 'cloud_gateway_camera_entitlements':SimpleNamespace(_camera_mapping=lambda db,c,a,cam:cam in self.cameras,
                                                                       cloud_price_classification=lambda pid:pid=='price_cloud')}
        for context in (patch.dict(sys.modules,modules), patch.dict('os.environ',{'ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE':'price_local','ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY':'price_cloud'}),patch.object(service,'ATTEMPTS_SERVICE_READY',True)):
            context.start(); self.addCleanup(context.stop)
        self.args={'attempt_id':'attempt-a','customer_id':'customer-a','appliance_id':'box-a','edition':'local_gateway',
                   'stripe_customer_id':'cus-a','price_id':'price_local','camera_ids':[]}

    def reserve(self, **changes): return service.reserve(self.db,self.actor,**{**self.args,**changes})
    def session(self,**changes):
        return {**{'id':'cs-test','customer':'cus-a','mode':'subscription','status':'open','payment_status':'unpaid',
                   'subscription':'sub-test','invoice':'in-test','metadata':{'anyaicam_gateway_attempt':'attempt-a'}},**changes}

    def test_default_off_and_idempotent_unknown_outcome(self):
        with patch.object(service,'ATTEMPTS_SERVICE_READY',False):
            self.assertEqual(self.reserve()['status'],'unavailable')
        self.assertEqual(self.reserve()['status'],'reserved')
        self.assertEqual(self.reserve()['status'],'existing')
        first=service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.assertEqual(first,service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a'))
        self.assertEqual(first['idempotency_key'],'anyaicam-gateway-attempt-attempt-a')
        with self.assertRaises(service.AttemptConflict): self.reserve(attempt_id='attempt-b')

    def test_revocation_before_exposure_and_late_payment_remain_reviewable(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.authorized=False
        with self.assertRaises(service.AttemptConflict): service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.assertEqual(service.observe_session(self.db,attempt_id='attempt-a',session=self.session())['status'],'review')
        self.authorized=True
        self.assertEqual(service.observe_session(self.db,attempt_id='attempt-a',session=self.session())['status'],'review')
        paid=self.session(status='complete',payment_status='paid')
        self.assertEqual(service.observe_session(self.db,attempt_id='attempt-a',session=paid,event_id='evt-1')['status'],'paid_review')
        self.assertTrue(service.observe_session(self.db,attempt_id='attempt-a',session=paid,event_id='evt-1')['duplicate'])
        record=self.db.execute('SELECT status,session_id,payment_event_id FROM gateway_checkout_attempts').fetchone()
        self.assertEqual(tuple(record),('paid_review','cs-test','evt-1'))
        self.assertEqual(self.db.execute('SELECT authorization_invalidated FROM gateway_checkout_attempts').fetchone()[0],1)
        subscription={'id':'sub-test','customer':'cus-a','status':'active','items':{'data':[{'quantity':1,'price':{'id':'price_local'}}]}}
        self.assertFalse(service.exact_paid_attempt_authorized(self.db,attempt_id='attempt-a',customer_id='customer-a',
            appliance_id='box-a',edition='local_gateway',price_id='price_local',quantity=1,session=paid,subscription=subscription))

    def test_exact_activation_evidence_checks_subscription_owner_and_authorization(self):
        self.reserve()
        session=self.session(status='complete',payment_status='paid')
        service.observe_session(self.db,attempt_id='attempt-a',session=session,event_id='evt-1')
        subscription={'id':'sub-test','customer':'cus-a','status':'active','items':{'data':[{'quantity':1,'price':{'id':'price_local'}}]}}
        args={'attempt_id':'attempt-a','customer_id':'customer-a','appliance_id':'box-a','edition':'local_gateway',
              'price_id':'price_local','quantity':1,'session':session,'subscription':subscription}
        self.assertTrue(service.exact_paid_attempt_authorized(self.db,**args))
        self.assertFalse(service.PAID_ACTIVATION_READY)
        for field,value in [('customer_id','customer-b'),('appliance_id','other'),('edition','cloud_gateway'),('price_id','price_other'),('quantity',2)]:
            with self.subTest(field=field): self.assertFalse(service.exact_paid_attempt_authorized(self.db,**{**args,field:value}))
        bad={**subscription,'id':'sub-other'}
        self.assertFalse(service.exact_paid_attempt_authorized(self.db,**{**args,'subscription':bad}))
        self.db.execute("UPDATE appliance_edition_provisioning_decisions SET evidence_version='2'")
        self.assertFalse(service.exact_paid_attempt_authorized(self.db,**args))
        self.db.execute("UPDATE appliance_edition_provisioning_decisions SET evidence_version='1'")
        self.authorized=False
        self.assertFalse(service.exact_paid_attempt_authorized(self.db,**args))

    def test_exact_cloud_activation_checks_current_camera_mapping_and_quantity(self):
        self.db.execute("UPDATE appliance_edition_provisioning_decisions SET edition='cloud_gateway'")
        self.reserve(edition='cloud_gateway',price_id='price_cloud',camera_ids=['cam-a','cam-b'])
        session=self.session(status='complete',payment_status='paid')
        service.observe_session(self.db,attempt_id='attempt-a',session=session,event_id='evt-1')
        subscription={'id':'sub-test','customer':'cus-a','status':'active','items':{'data':[{'quantity':2,'price':{'id':'price_cloud'}}]}}
        args={'attempt_id':'attempt-a','customer_id':'customer-a','appliance_id':'box-a','edition':'cloud_gateway',
              'price_id':'price_cloud','quantity':2,'session':session,'subscription':subscription}
        self.assertTrue(service.exact_paid_attempt_authorized(self.db,**args))
        subscription['items']['data'][0]['quantity']=1
        self.assertFalse(service.exact_paid_attempt_authorized(self.db,**args))
        subscription['items']['data'][0]['quantity']=2; self.cameras.remove('cam-a')
        self.assertFalse(service.exact_paid_attempt_authorized(self.db,**args))

    def test_canonical_full_refund_is_audited_idempotent_and_never_activates(self):
        self.reserve(); paid=self.session(status='complete',payment_status='paid')
        service.observe_session(self.db,attempt_id='attempt-a',session=paid,event_id='evt-paid'); self.db.commit()
        charge={'id':'ch-test','invoice':'in-test','customer':'cus-a','payment_intent':'pi-test','status':'succeeded',
                'currency':'usd','amount':999,'amount_refunded':999,'refunded':True,'livemode':False}
        invoice={'id':'in-test','customer':'cus-a','subscription':'sub-test','payment_intent':'pi-test',
                 'currency':'usd','amount_paid':999,'livemode':False}
        event={'id':'evt-refund','type':'charge.refunded','data':{'object':{'id':'ch-test','invoice':'in-test'}}}
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            result=self.db.execute(sql,args).fetchone()
            return dict(result) if result else None
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        locks=[]; ledger=SimpleNamespace(payment_keys=lambda *ids:ids,lock_payment=lambda db,ids:locks.append(ids),reversal_reason=lambda *args:'refunded')
        def fetch(path,description): return dict(charge) if '/charges/' in path else dict(invoice)
        modules={'gateway_checkout_attempts':service,'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=audit),
                 'billing_status':ledger,'stripe_state':SimpleNamespace(stripe_get=fetch)}
        with patch.dict(sys.modules,modules):
            self.assertEqual(webhook.sync_from_verified_webhook(event,fetch=fetch)['status'],'refunded')
            self.assertTrue(webhook.sync_from_verified_webhook({**event,'id':'evt-replay'},fetch=fetch)['duplicate'])
            subscription={'id':'sub-test','customer':'cus-a','status':'active','items':{'data':[{'quantity':1,'price':{'id':'price_local'}}]}}
            self.assertFalse(service.exact_paid_attempt_authorized(self.db,attempt_id='attempt-a',customer_id='customer-a',
                appliance_id='box-a',edition='local_gateway',price_id='price_local',quantity=1,session=paid,subscription=subscription))
        record=self.db.execute('SELECT status,reversal_state,refund_charge_id,refund_event_id FROM gateway_checkout_attempts').fetchone()
        self.assertEqual(tuple(record),('paid_review','refunded','ch-test','evt-refund'))
        self.assertEqual(locks[0],('ch-test','pi-test','in-test'))
        self.assertEqual(self.db.execute("SELECT count(*) FROM audit_logs WHERE action='gateway.checkout_refund_observed'").fetchone()[0],1)

    def test_partial_and_unmatched_refunds_do_not_change_attempt(self):
        self.reserve(); service.observe_session(self.db,attempt_id='attempt-a',session=self.session(status='complete',payment_status='paid'),event_id='evt-paid'); self.db.commit()
        charge={'id':'ch-test','invoice':'in-test','customer':'cus-a','payment_intent':'pi-test','amount':999,'amount_refunded':100}
        event={'id':'evt-partial','type':'charge.refunded','data':{'object':{'id':'ch-test','invoice':'in-test'}}}
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            result=self.db.execute(sql,args).fetchone()
            return dict(result) if result else None
        def fetch(path,description): return charge if '/charges/' in path else {'id':'in-test'}
        modules={'gateway_checkout_attempts':service,'partner_db':SimpleNamespace(transaction=transaction,row=row),
                 'billing_status':SimpleNamespace(payment_keys=lambda *ids:ids,lock_payment=lambda *args:None),
                 'stripe_state':SimpleNamespace(stripe_get=fetch)}
        with patch.dict(sys.modules,modules):
            self.assertEqual(webhook.sync_from_verified_webhook(event,fetch=fetch)['status'],'partial_refund')
            unknown={**event,'data':{'object':{'id':'ch-other','invoice':'in-other'}}}
            self.assertEqual(webhook.sync_from_verified_webhook(unknown,fetch=lambda *args:self.fail('unmatched must not fetch'))['status'],'ignored')
        self.assertEqual(self.db.execute('SELECT reversal_state FROM gateway_checkout_attempts').fetchone()[0],'none')

    def test_wrong_owner_version_customer_and_session_rejected(self):
        with self.assertRaises(service.AttemptConflict): service.reserve(self.db,{**self.actor,'customer_id':'customer-b'},**self.args)
        with self.assertRaises(service.AttemptConflict): self.reserve(stripe_customer_id='cus-other')
        self.reserve(); self.db.execute("UPDATE appliance_edition_provisioning_decisions SET evidence_version='2'")
        with self.assertRaises(service.AttemptConflict): service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        with self.assertRaises(service.AttemptConflict): service.observe_session(self.db,attempt_id='attempt-a',session=self.session(customer='cus-other'))

    def test_cloud_camera_selection_and_mapping_changes(self):
        self.db.execute("UPDATE appliance_edition_provisioning_decisions SET edition='cloud_gateway'")
        with self.assertRaises(service.AttemptConflict): self.reserve(edition='cloud_gateway',price_id='price_cloud',camera_ids=['cam-a','cam-a'])
        args={'edition':'cloud_gateway','price_id':'price_cloud','camera_ids':['cam-b','cam-a']}
        self.assertEqual(self.reserve(**args)['status'],'reserved')
        self.assertEqual(tuple(self.db.execute('SELECT quantity,camera_ids_json FROM gateway_checkout_attempts').fetchone()),(2,'["cam-a","cam-b"]'))
        self.cameras.remove('cam-a')
        with self.assertRaises(service.AttemptConflict): service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.assertEqual(service.observe_session(self.db,attempt_id='attempt-a',session=self.session())['status'],'review')

    def test_failed_audit_rolls_back_reservation(self):
        def fail(*args): raise RuntimeError('audit unavailable')
        self.db.commit()
        with patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=fail)}),self.assertRaises(RuntimeError):
            with self.db: self.reserve()
        self.assertEqual(self.db.execute('SELECT count(*) FROM gateway_checkout_attempts').fetchone()[0],0)

    def test_expired_session_cannot_erase_late_payment(self):
        self.reserve()
        self.assertEqual(service.observe_session(self.db,attempt_id='attempt-a',session=self.session(status='expired'))['status'],'expired')
        self.reserve(attempt_id='attempt-b')
        self.assertEqual(service.observe_session(self.db,attempt_id='attempt-a',session=self.session(status='complete',payment_status='paid'),event_id='evt-late')['status'],'late_paid_review')
        with self.assertRaises(service.AttemptConflict): self.reserve(attempt_id='attempt-c')

    def test_simultaneous_reservations_leave_one_unresolved_attempt(self):
        local=threading.local(); barrier=threading.Barrier(2,timeout=5); outcomes=[]; errors=[]
        def audit(actor,action,entity_type,entity_id,details):
            local.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=audit)}):
            path=str(Path(directory)/'synthetic.db'); target=sqlite3.connect(path); self.db.backup(target); target.close()
            def worker(attempt_id):
                db=sqlite3.connect(path,timeout=5); db.row_factory=sqlite3.Row; local.db=db
                try:
                    barrier.wait(); db.execute('BEGIN IMMEDIATE')
                    try:
                        outcomes.append(service.reserve(db,self.actor,**{**self.args,'attempt_id':attempt_id})['status'])
                        db.commit()
                    except service.AttemptConflict:
                        db.rollback(); outcomes.append('conflict')
                except Exception as error: errors.append(str(error)); db.rollback()
                finally: db.close()
            threads=[threading.Thread(target=worker,args=(name,)) for name in ('attempt-a','attempt-b')]
            for thread in threads: thread.start()
            for thread in threads: thread.join(8)
            self.assertFalse(any(thread.is_alive() for thread in threads)); self.assertEqual(errors,[])
            self.assertEqual(sorted(outcomes),['conflict','reserved'])

    def test_checkout_handoff_reuses_attempt_and_fences_revocation(self):
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        calls=[]; state={'revoked':False,'attempt':None}
        def create(customer,purchase,**kwargs):
            tagged=dict(kwargs['fields']); state['attempt']=tagged['metadata[anyaicam_gateway_attempt]']; calls.append(state['attempt'])
            return {'id':'cs-test'}
        def get(path,description):
            if path.endswith('/line_items'):
                return {'data':[{'quantity':1,'price':{'id':'price_local'}}],'has_more':False}
            if state['revoked']: self.authorized=False
            return {'id':'cs-test','customer':'cus-a','mode':'subscription','status':'open','payment_status':'unpaid',
                    'metadata':{'anyaicam_gateway_attempt':state['attempt']},'url':'https://checkout.stripe.com/session'}
        modules={'gateway_checkout_attempts':service,'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
                 'database_backend':SimpleNamespace(lock_customer_camera_capacity=lambda db,customer:True),
                 'checkout_guard':SimpleNamespace(create_session=create),'stripe_state':SimpleNamespace(stripe_get=get)}
        args={'customer_id':'customer-a','appliance_id':'box-a','edition':'local_gateway','stripe_customer_id':'cus-a',
              'price_id':'price_local','camera_ids':[],'purchase':'local_gateway_remote:box-a','fields':[]}
        with patch.dict(sys.modules,modules):
            self.assertEqual(handoff.create_session(self.actor,**args)['id'],'cs-test')
            self.assertEqual(handoff.create_session(self.actor,**args)['id'],'cs-test')
            self.assertEqual(calls[0],calls[1])
            state['revoked']=True
            with self.assertRaises(service.AttemptConflict): handoff.create_session(self.actor,**args)
            self.assertEqual(self.db.execute('SELECT status FROM gateway_checkout_attempts').fetchone()[0],'review')

    def test_verified_dispatch_records_late_payment_without_activation(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.authorized=False; self.db.commit()
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        calls=[]
        ledger=SimpleNamespace(payment_keys=lambda *ids:ids,lock_payment=lambda db,ids:calls.append(ids))
        event={'id':'evt-paid','type':'checkout.session.completed','data':{'object':{'id':'cs-test',
               'metadata':{'anyaicam_gateway_attempt':'attempt-a'}}}}
        canonical=self.session(status='complete',payment_status='paid',invoice='in-test')
        def fetch(path,description):
            return {'data':[{'quantity':1,'price':{'id':'price_local'}}],'has_more':False} if path.endswith('/line_items') else canonical
        def row(sql,args):
            found=self.db.execute(sql,args).fetchone()
            return dict(found) if found else None
        modules={'gateway_checkout_attempts':service,'partner_db':SimpleNamespace(transaction=transaction,audit=audit,row=row),
                 'billing_status':ledger,'stripe_state':SimpleNamespace(stripe_get=fetch)}
        with patch.dict(sys.modules,modules):
            self.assertEqual(webhook.sync_from_verified_webhook(event,fetch=fetch)['status'],'paid_review')
            self.assertTrue(webhook.sync_from_verified_webhook(event,fetch=fetch)['duplicate'])
            self.assertTrue(webhook.sync_from_verified_webhook({**event,'id':'evt-second'},fetch=fetch)['duplicate'])
            without_meta={'id':'evt-third','type':'checkout.session.completed','data':{'object':{'id':'cs-test'}}}
            self.assertTrue(webhook.sync_from_verified_webhook(without_meta,fetch=fetch)['duplicate'])
            unknown={'id':'evt-other','type':'checkout.session.completed','data':{'object':{'id':'cs-other'}}}
            self.assertEqual(webhook.sync_from_verified_webhook(unknown,fetch=fetch)['status'],'ignored')
            self.assertEqual(webhook.sync_from_verified_webhook({'type':'checkout.session.completed','data':{'object':{'metadata':{}}}},fetch=fetch)['status'],'ignored')
        self.assertEqual(tuple(self.db.execute('SELECT status,payment_event_id FROM gateway_checkout_attempts').fetchone()),('paid_review','evt-paid'))
        self.assertEqual(calls[0],('','','in-test'))

    def test_attempt_webhook_fails_closed_on_disabled_or_wrong_price(self):
        event={'id':'evt','type':'checkout.session.completed','data':{'object':{'id':'cs-test','metadata':{'anyaicam_gateway_attempt':'attempt-a'}}}}
        with patch.dict(sys.modules,{'gateway_checkout_attempts':service}),patch.object(service,'ATTEMPTS_SERVICE_READY',False):
            with self.assertRaises(service.AttemptConflict): webhook.sync_from_verified_webhook(event)
        self.reserve(); self.db.commit()
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def fetch(path,description):
            return {'data':[{'quantity':1,'price':{'id':'wrong'}}],'has_more':False} if path.endswith('/line_items') else self.session(invoice='in-test')
        modules={'gateway_checkout_attempts':service,'partner_db':SimpleNamespace(transaction=transaction),
                 'billing_status':SimpleNamespace(payment_keys=lambda *ids:ids,lock_payment=lambda *args:None),
                 'stripe_state':SimpleNamespace(stripe_get=fetch)}
        with patch.dict(sys.modules,modules),self.assertRaises(service.AttemptConflict): webhook.sync_from_verified_webhook(event,fetch=fetch)
        self.assertEqual(self.db.execute('SELECT status FROM gateway_checkout_attempts').fetchone()[0],'reserved')

    def test_unidentified_event_quarantined_without_tenant_then_exact_anchor_resolves(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a'); self.db.commit()
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            value=self.db.execute(sql,args).fetchone()
            return dict(value) if value else None
        def rows(sql,args): return [dict(value) for value in self.db.execute(sql,args).fetchall()]
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        event={'id':'evt-unknown','type':'checkout.session.completed','created':20,
               'data':{'object':{'id':'cs-test','customer':'cus-a'}}}
        unrelated={'id':'evt-unrelated','type':'checkout.session.completed','created':21,
                   'data':{'object':{'id':'cs-other','customer':'cus-other'}}}
        canonical=self.session(status='complete',payment_status='paid',invoice='in-test')
        def fetch(path,description):
            if path=='/v1/events/evt-unknown': return event
            if path.endswith('/line_items'): return {'data':[{'quantity':1,'price':{'id':'price_local'}}],'has_more':False}
            return canonical
        modules={'gateway_checkout_attempts':service,'gateway_checkout_webhook':webhook,
                 'partner_db':SimpleNamespace(transaction=transaction,row=row,rows=rows,audit=audit),
                 'billing_status':SimpleNamespace(payment_keys=lambda *ids:ids,lock_payment=lambda *args:None),
                 'stripe_state':SimpleNamespace(stripe_get=fetch,RetryableStripeEventError=RuntimeError)}
        with patch.dict(sys.modules,modules),patch.object(evidence,'WEBHOOK_PRODUCER_READY',True):
            self.assertEqual(webhook.sync_from_verified_webhook(event,fetch=fetch)['status'],'quarantined')
            conflict={**event,'data':{'object':{'id':'cs-other','customer':'cus-a'}}}
            self.assertEqual(webhook.sync_from_verified_webhook(conflict,fetch=fetch)['status'],'unavailable')
            self.assertEqual(webhook.sync_from_verified_webhook(unrelated,fetch=fetch)['status'],'quarantined')
            self.assertEqual(evidence.retry_pending_correlations(fetch=fetch)['selected'],0)
            self.assertEqual(self.db.execute('SELECT status FROM gateway_checkout_attempts').fetchone()[0],'creating')
            service.observe_session(self.db,attempt_id='attempt-a',session=self.session())
            outcome=evidence.retry_pending_correlations(fetch=fetch)
            self.assertEqual((outcome['selected'],outcome['reconciled'],outcome['failed']),(1,1,0))
            self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_webhook_correlation WHERE event_id='evt-unrelated'").fetchone()[0],'unidentified')
            self.assertEqual(self.db.execute('SELECT status FROM gateway_checkout_attempts').fetchone()[0],'paid_review')

    def test_unavailable_gateway_lookup_does_not_break_unrelated_checkout(self):
        def fail(*args): raise RuntimeError('optional schema unavailable')
        event={'id':'evt-unrelated','type':'checkout.session.completed','created':20,'data':{'object':{'id':'cs-other'}}}
        modules={'gateway_checkout_attempts':service,'partner_db':SimpleNamespace(row=fail)}
        with patch.dict(sys.modules,modules):
            self.assertEqual(webhook.sync_from_verified_webhook(event,fetch=lambda *args:self.fail('must not fetch'))['status'],'unavailable')

    def test_review_route_requires_current_global_grant(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        class HTTPError(Exception):
            def __init__(self,status_code,detail): self.status_code=status_code
        class Router:
            def __init__(self): self.posts={}
            def get(self,path):
                def decorate(fn): self.fn=fn; return fn
                return decorate
            def post(self,path):
                def decorate(fn): self.posts[path]=fn; return fn
                return decorate
        @contextmanager
        def transaction(): yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        state={'grant':True,'enabled':True,'actor':{'role':'administrator','email':'admin@example.test'}}
        modules={'fastapi':SimpleNamespace(HTTPException=HTTPError,Request=type('Request',(),{})),
                 'partner_portal':SimpleNamespace(partner_identity=lambda req:state['actor']),
                 'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
                 'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:state['grant']),
                 'gateway_internal_provisioning':SimpleNamespace(internal_test_enabled=lambda:state['enabled']),
                 'token_security':SimpleNamespace(unsign=lambda value:'csrf' if value=='signed' else None)}
        router=Router()
        with patch.dict(sys.modules,modules):
            review.register_unmounted_review_routes(router)
            action_route=router.posts['/api/operations/internal-gateway/checkout-review/{attempt_id}/actions']
            self.assertEqual(router.fn(object())['attempts'][0]['status'],'creating')
            request=SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={'x-csrf-token':'signed'})
            bad=SimpleNamespace(cookies={},headers={})
            with self.assertRaises(HTTPError) as error: action_route('attempt-a',{'request_id':'r1','action':'refund_review'},bad)
            self.assertEqual(error.exception.status_code,403)
            self.assertEqual(action_route('attempt-a',{'request_id':'r1','action':'refund_review'},request)['status'],'recorded')
            self.assertEqual(action_route('attempt-a',{'request_id':'r1','action':'refund_review'},request)['status'],'already_recorded')
            with self.assertRaises(HTTPError) as error:
                action_route('attempt-a',{'request_id':'r1','action':'investigate'},request)
            self.assertEqual(error.exception.status_code,409)
            self.assertEqual(self.db.execute('SELECT count(*) FROM gateway_checkout_review_actions').fetchone()[0],1)
            self.assertEqual(self.db.execute('SELECT status FROM gateway_checkout_attempts').fetchone()[0],'creating')
            state['grant']=False
            with self.assertRaises(HTTPError) as error: router.fn(object())
            self.assertEqual(error.exception.status_code,403)
            with self.assertRaises(HTTPError) as error: action_route('attempt-a',{'request_id':'r2','action':'refund_review'},request)
            self.assertEqual(error.exception.status_code,403)
            state['enabled']=False
            with self.assertRaises(HTTPError) as error: router.fn(object())
            self.assertEqual(error.exception.status_code,404)

    def test_admin_can_reconcile_only_canonical_expired_unpaid_test_session(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        service.observe_session(self.db,attempt_id='attempt-a',session=self.session())
        admin={'role':'administrator','email':'admin@example.test'}
        modules={'gateway_internal_provisioning':SimpleNamespace(internal_test_enabled=lambda:True),
                 'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:True)}
        session=self.session(status='expired',livemode=False)
        with patch.dict(sys.modules,modules):
            with self.assertRaises(review.ReviewConflict):
                review.resolve_verified_expired(self.db,admin,attempt_id='attempt-a',request_id='resolve-1',session={**session,'livemode':True})
            with self.assertRaises(review.ReviewConflict):
                review.resolve_verified_expired(self.db,admin,attempt_id='attempt-a',request_id='resolve-1',session={**session,'payment_status':'paid'})
            self.assertEqual(review.resolve_verified_expired(self.db,admin,attempt_id='attempt-a',request_id='resolve-1',session=session)['status'],'expired')
            self.assertEqual(review.resolve_verified_expired(self.db,admin,attempt_id='attempt-a',request_id='resolve-1',session=session)['status'],'already_recorded')
        self.assertEqual(self.db.execute('SELECT status FROM gateway_checkout_attempts').fetchone()[0],'expired')
        self.assertEqual(self.db.execute("SELECT action FROM gateway_checkout_review_actions").fetchone()[0],'resolved_expired')

    def test_admin_links_only_server_anchored_session_for_review(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.db.execute("INSERT INTO checkout_pending VALUES('customer-a','local_gateway_remote:box-a','cs-test','open')")
        admin={'role':'administrator','email':'admin@example.test'}
        session=self.session(status='complete',payment_status='paid',livemode=False)
        items={'data':[{'quantity':1,'price':{'id':'price_local'}}],'has_more':False}
        modules={'gateway_internal_provisioning':SimpleNamespace(internal_test_enabled=lambda:True),
                 'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:True)}
        with patch.dict(sys.modules,modules):
            wrong={'data':[{'quantity':2,'price':{'id':'price_local'}}],'has_more':False}
            with self.assertRaises(review.ReviewConflict):
                review.link_verified_session(self.db,admin,attempt_id='attempt-a',request_id='link-1',session=session,items=wrong)
            self.assertEqual(review.link_verified_session(self.db,admin,attempt_id='attempt-a',request_id='link-1',session=session,items=items)['status'],'review')
            self.assertEqual(review.link_verified_session(self.db,admin,attempt_id='attempt-a',request_id='link-1',session=session,items=items)['status'],'already_recorded')
        record=self.db.execute('SELECT status,session_id,authorization_invalidated,payment_event_id FROM gateway_checkout_attempts').fetchone()
        self.assertEqual(tuple(record),('review','cs-test',1,None))
        self.assertEqual(self.db.execute("SELECT action FROM gateway_checkout_review_actions").fetchone()[0],'linked_session')

    def test_admin_cannot_link_other_tenant_or_unanchored_session(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.db.execute("INSERT INTO checkout_pending VALUES('customer-b','local_gateway_remote:box-a','cs-test','open')")
        modules={'gateway_internal_provisioning':SimpleNamespace(internal_test_enabled=lambda:True),
                 'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:True)}
        items={'data':[{'quantity':1,'price':{'id':'price_local'}}],'has_more':False}
        with patch.dict(sys.modules,modules),self.assertRaises(review.ReviewConflict):
            review.link_verified_session(self.db,{'role':'administrator','email':'admin@example.test'},attempt_id='attempt-a',
                request_id='link-1',session=self.session(livemode=False),items=items)
        self.assertEqual(self.db.execute('SELECT status FROM gateway_checkout_attempts').fetchone()[0],'creating')

    def test_link_session_route_enforces_admin_csrf_and_test_mode(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a')
        self.db.execute("INSERT INTO checkout_pending VALUES('customer-a','local_gateway_remote:box-a','cs-test','open')"); self.db.commit()
        class HTTPError(Exception):
            def __init__(self,status_code,detail): self.status_code=status_code
        class Router:
            def __init__(self): self.posts={}
            def get(self,path): return lambda fn:fn
            def post(self,path): return lambda fn:self.posts.setdefault(path,fn)
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(actor['email'],action,entity_id,json.dumps(details)))
        state={'test':False,'grant':True,'calls':0}
        def fetch(path,description):
            state['calls']+=1
            if path.endswith('/line_items'): return {'data':[{'quantity':1,'price':{'id':'price_local'}}],'has_more':False}
            return self.session(status='complete',payment_status='paid',livemode=False)
        modules={'fastapi':SimpleNamespace(HTTPException=HTTPError,Request=type('Request',(),{})),
                 'partner_portal':SimpleNamespace(partner_identity=lambda req:{'role':'administrator','email':'admin@example.test'}),
                 'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
                 'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:state['grant']),
                 'gateway_internal_provisioning':SimpleNamespace(internal_test_enabled=lambda:True),
                 'token_security':SimpleNamespace(unsign=lambda value:'csrf' if value=='signed' else None),
                 'stripe_mode':SimpleNamespace(server_key_is_test=lambda:state['test']),
                 'stripe_state':SimpleNamespace(stripe_get=fetch)}
        request=SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={'x-csrf-token':'signed'}); router=Router()
        with patch.dict(sys.modules,modules):
            review.register_unmounted_review_routes(router)
            route=router.posts['/api/operations/internal-gateway/checkout-review/{attempt_id}/link-session']
            with self.assertRaises(HTTPError) as error: route('attempt-a',{'request_id':'r1'},request)
            self.assertEqual(error.exception.status_code,503); self.assertEqual(state['calls'],0)
            state['test']=True; state['grant']=False
            with self.assertRaises(HTTPError) as error: route('attempt-a',{'request_id':'r1'},request)
            self.assertEqual(error.exception.status_code,403); self.assertEqual(state['calls'],0)
            state['grant']=True
            with self.assertRaises(HTTPError) as error: route('attempt-a',{'request_id':'r1','session_id':'cs-other'},request)
            self.assertEqual(error.exception.status_code,400); self.assertEqual(state['calls'],0)
            self.assertEqual(route('attempt-a',{'request_id':'r1'},request)['status'],'review')
            self.assertEqual(state['calls'],2)

    def test_review_audit_failure_rolls_back_action(self):
        self.reserve(); service.begin(self.db,self.actor,customer_id='customer-a',attempt_id='attempt-a'); self.db.commit()
        def fail(*args): raise RuntimeError('audit unavailable')
        modules={'gateway_internal_provisioning':SimpleNamespace(internal_test_enabled=lambda:True),
                 'appliance_identity':SimpleNamespace(has_global_administrator_grant=lambda db,email:True),
                 'partner_db':SimpleNamespace(audit=fail)}
        with patch.dict(sys.modules,modules),self.assertRaises(RuntimeError):
            with self.db: review.record_review_action(self.db,{'role':'administrator','email':'admin@example.test'},
                attempt_id='attempt-a',request_id='review-1',action='refund_review')
        self.assertEqual(self.db.execute('SELECT count(*) FROM gateway_checkout_review_actions').fetchone()[0],0)


if __name__ == '__main__': unittest.main()
