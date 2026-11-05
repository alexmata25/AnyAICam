"""Prepared reducer tests with synthetic snapshots; no Stripe or migrations."""
import ast
import copy
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
from app import cloud_gateway_subscription_evidence as producer
from app import cloud_gateway_camera_entitlements as assignments
from app import cloud_gateway_subscription_refresh as refresh


class SubscriptionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row; self.addCleanup(self.db.close)
        self.db.executescript('''
          CREATE TABLE stripe_customer_bindings(customer_id TEXT,stripe_customer_id TEXT);
          CREATE TABLE checkout_pending(customer_id TEXT,purchase TEXT,session_id TEXT,status TEXT,price_id TEXT,quantity INTEGER);
          CREATE TABLE cloud_gateway_subscription_evidence(stripe_subscription_id TEXT PRIMARY KEY,customer_id TEXT,stripe_customer_id TEXT,
            stripe_price_id TEXT,checkout_session_id TEXT,product TEXT,quantity INTEGER,amount_cents INTEGER,currency TEXT,billing_interval TEXT,interval_count INTEGER,
            status TEXT,stripe_status TEXT,current_period_end INTEGER,invoice_id TEXT,charge_id TEXT,payment_intent_id TEXT,payment_verified INTEGER,reversal TEXT,
            source_version TEXT,event_created INTEGER,event_id TEXT,updated_at TEXT);
          CREATE TABLE cloud_gateway_camera_entitlements(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,camera_id TEXT,edition TEXT,
            billing_source TEXT,billing_reference TEXT,status TEXT,version INTEGER,updated_at TEXT,ended_reason TEXT);
          CREATE TABLE cloud_gateway_evidence_holds(stripe_subscription_id TEXT,customer_id TEXT,event_id TEXT,event_created INTEGER,created_at TEXT,PRIMARY KEY(stripe_subscription_id,event_id));
          CREATE TABLE cloud_gateway_webhook_correlation(event_id TEXT PRIMARY KEY,event_type TEXT,object_id TEXT,stripe_customer_id TEXT,event_created INTEGER,status TEXT,subscription_id TEXT,created_at TEXT);
          CREATE TABLE cloud_gateway_refresh_leases(stripe_subscription_id TEXT PRIMARY KEY,customer_id TEXT,token TEXT,lease_until INTEGER);
          CREATE TABLE gateway_stripe_payment_links(kind TEXT,provider_id TEXT,customer_id TEXT,stripe_customer_id TEXT,stripe_subscription_id TEXT,created_at TEXT,PRIMARY KEY(kind,provider_id));
          CREATE TABLE gateway_checkout_attempts(id TEXT,customer_id TEXT,appliance_id TEXT,session_id TEXT);
          CREATE TABLE audit_logs(actor_email TEXT,actor_role TEXT,action TEXT,entity_type TEXT,entity_id TEXT,details_json TEXT);
          INSERT INTO stripe_customer_bindings VALUES('customer-a','cus-test');
          INSERT INTO checkout_pending VALUES('customer-a','cloud_gateway','session-test','open','price_cloud_test',2);
        ''')
        self.sub={'id':'sub-test','customer':'cus-test','status':'active','current_period_end':4102444800,'latest_invoice':'in-test',
          'metadata':{'anyaicam_product':'cloud_gateway','anyaicam_customer_id':'customer-a'},
          'items':{'data':[{'quantity':2,'price':{'id':'price_cloud_test','unit_amount':999,'currency':'usd',
                                                  'recurring':{'interval':'month','interval_count':1}}}]}}
        self.invoice={'id':'in-test','subscription':'sub-test','customer':'cus-test','status':'paid','currency':'usd','amount_paid':1998,'charge':'ch-test','payment_intent':'pi-test'}
        self.checkout={'id':'session-test','mode':'subscription','payment_status':'paid','customer':'cus-test','subscription':'sub-test',
                       'metadata':{'anyaicam_product':'cloud_gateway','anyaicam_customer_id':'customer-a'}}
        self.charge={'id':'ch-test','status':'succeeded','currency':'usd','amount':1998,'amount_refunded':0,'refunded':False,'disputed':False,'payment_intent':'pi-test','invoice':'in-test'}
        self.fail_audit=False
        def audit(actor,action,entity_type,entity_id,details):
            if self.fail_audit: raise RuntimeError('audit unavailable')
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        patches=[patch.dict(sys.modules,{'database_backend':SimpleNamespace(lock_customer_camera_capacity=lambda db,customer:customer=='customer-a'),
                    'partner_db':SimpleNamespace(audit=audit),'cloud_gateway_camera_entitlements':assignments}),
                 patch.dict('os.environ',{'ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY':'price_cloud_test'}),
                 patch.object(producer,'WEBHOOK_PRODUCER_READY',True)]
        for p in patches: p.start(); self.addCleanup(p.stop)

    def reconcile(self, event='evt-1', created=10, **changes):
        args=dict(event_id=event,event_created=created,customer_id='customer-a',session_id='session-test',
                  subscription=self.sub,invoice=self.invoice,charge=self.charge,checkout=self.checkout)
        return producer.reconcile_verified_snapshot(self.db,**{**args,**changes})

    def test_paid_activation_duplicate_and_audited_source(self):
        self.assertEqual(self.reconcile()['status'],'active')
        self.assertEqual(self.reconcile()['status'],'already_processed')
        result=assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test')
        self.assertEqual(result['quantity'],2); self.assertTrue(result['verified_paid'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0],1)

    def test_attempt_tagged_payment_does_not_activate_while_gate_is_off(self):
        self.db.execute("INSERT INTO gateway_checkout_attempts VALUES('attempt-a','customer-a','box-a',NULL)")
        self.checkout['metadata']['anyaicam_gateway_attempt']='attempt-a'
        checker=__import__('unittest.mock',fromlist=['Mock']).Mock(return_value=True)
        module=SimpleNamespace(exact_paid_attempt_authorized=checker,PAID_ACTIVATION_READY=False)
        with patch.dict(sys.modules,{'gateway_checkout_attempts':module}),self.assertRaises(producer.EvidenceConflict):
            self.reconcile()
        checker.assert_called_once()
        self.assertEqual(self.db.execute('SELECT count(*) FROM cloud_gateway_subscription_evidence').fetchone()[0],0)

    def test_pending_and_partial_refund_do_not_invent_payment(self):
        self.sub['status']='past_due'
        self.assertEqual(self.reconcile()['status'],'pending')
        self.sub['status']='active'; self.charge['amount_refunded']=100
        self.assertEqual(self.reconcile('evt-2',11)['status'],'active')
        self.assertEqual(self.db.execute('SELECT reversal FROM cloud_gateway_subscription_evidence').fetchone()[0],None)

    def test_partial_refund_and_harmless_refresh_keep_assignment_source_version(self):
        self.assertEqual(self.reconcile()['version'],'evt-1')
        self.charge['amount_refunded']=100
        self.assertEqual(self.reconcile('evt-partial',11)['version'],'evt-1')
        self.sub['metadata']['unrelated']='changed'
        self.assertEqual(self.reconcile('evt-refresh',12)['version'],'evt-1')
        source=assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test')
        self.assertEqual(source['version'],'evt-1'); self.assertTrue(source['verified_paid'])

    def test_partial_refund_keeps_existing_camera_binding_verified(self):
        self.reconcile()
        self.db.executescript('''
          CREATE TABLE appliances(id TEXT,customer_id TEXT,gateway_edition TEXT);
          CREATE TABLE cameras(id TEXT,customer_id TEXT,appliance_id TEXT,camera_number INTEGER,device_key TEXT,status TEXT);
          INSERT INTO appliances VALUES('a','customer-a','cloud_gateway');
          INSERT INTO cameras VALUES('c','customer-a','a',1,'key','active');
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN camera_number INTEGER;
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN device_key TEXT;
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN billing_version TEXT;
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN audit_reference TEXT;
          INSERT INTO cloud_gateway_camera_entitlements(id,customer_id,appliance_id,camera_id,edition,billing_source,billing_reference,status,version,camera_number,device_key,billing_version,audit_reference)
          VALUES('b','customer-a','a','c','cloud_gateway','verified_subscription','sub-test','active',1,1,'key','evt-1','b');
        ''')
        detail=assignments._assignment_details('customer-a','a','c',1,'key','sub-test','evt-1')
        self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',('admin','administrator','cloud_gateway.camera_binding_assigned','cloud_gateway_camera_entitlement','b',json.dumps(detail)))
        modules={'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}
        with patch.dict(sys.modules,modules),patch.object(assignments,'CLOUD_GATEWAY_BILLING_SOURCE_READY',True):
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b',customer_id='customer-a',appliance_id='a',camera_id='c')['status'],'verified')
            self.charge['amount_refunded']=100
            self.reconcile('evt-partial',11)
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b',customer_id='customer-a',appliance_id='a',camera_id='c')['status'],'verified')
            self.db.execute("INSERT INTO cloud_gateway_evidence_holds VALUES('sub-test','customer-a','evt-pending',12,'now')")
            self.assertNotEqual(assignments.verify_paid_binding(self.db,binding_id='b',customer_id='customer-a',appliance_id='a',camera_id='c')['status'],'verified')
            self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='b'").fetchone()[0],'active')
            self.db.execute("DELETE FROM cloud_gateway_evidence_holds WHERE event_id='evt-pending'")
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b',customer_id='customer-a',appliance_id='a',camera_id='c')['status'],'verified')

    def seed_quantity_bindings(self):
        self.reconcile()
        self.db.executescript('''
          CREATE TABLE appliances(id TEXT,customer_id TEXT,gateway_edition TEXT);
          CREATE TABLE cameras(id TEXT,customer_id TEXT,appliance_id TEXT,camera_number INTEGER,device_key TEXT,status TEXT);
          INSERT INTO appliances VALUES('a','customer-a','cloud_gateway');
          INSERT INTO cameras VALUES('c1','customer-a','a',1,'key1','active');
          INSERT INTO cameras VALUES('c2','customer-a','a',2,'key2','active');
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN camera_number INTEGER;
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN device_key TEXT;
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN billing_version TEXT;
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN audit_reference TEXT;
          ALTER TABLE cloud_gateway_camera_entitlements ADD COLUMN created_at TEXT;
          INSERT INTO cloud_gateway_camera_entitlements(id,customer_id,appliance_id,camera_id,edition,billing_source,billing_reference,status,version,camera_number,device_key,billing_version,audit_reference,created_at)
          VALUES('b1','customer-a','a','c1','cloud_gateway','verified_subscription','sub-test','active',1,1,'key1','evt-1','b1','2026-01-01T00:00:00+00:00');
          INSERT INTO cloud_gateway_camera_entitlements(id,customer_id,appliance_id,camera_id,edition,billing_source,billing_reference,status,version,camera_number,device_key,billing_version,audit_reference,created_at)
          VALUES('b2','customer-a','a','c2','cloud_gateway','verified_subscription','sub-test','active',1,2,'key2','evt-1','b2','2026-01-02T00:00:00+00:00');
        ''')
        for binding,camera,number,key in [('b1','c1',1,'key1'),('b2','c2',2,'key2')]:
            detail=assignments._assignment_details('customer-a','a',camera,number,key,'sub-test','evt-1')
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',('admin','administrator','cloud_gateway.camera_binding_assigned','cloud_gateway_camera_entitlement',binding,json.dumps(detail)))

    def test_paid_quantity_increase_preserves_existing_verified_assignments(self):
        self.seed_quantity_bindings()
        self.sub['items']['data'][0]['quantity']=3
        with patch.dict(sys.modules,{'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}), \
             patch.object(assignments,'CLOUD_GATEWAY_BILLING_SOURCE_READY',True):
            self.reconcile('evt-increase',11)
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b1',customer_id='customer-a',appliance_id='a',camera_id='c1')['status'],'verified')

    def test_quantity_reduction_retires_newest_and_keeps_oldest_verified(self):
        self.seed_quantity_bindings()
        self.sub['items']['data'][0]['quantity']=1
        with patch.dict(sys.modules,{'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}), \
             patch.object(assignments,'CLOUD_GATEWAY_BILLING_SOURCE_READY',True):
            self.assertEqual(self.reconcile('evt-reduce',11)['status'],'active')
            states=self.db.execute('SELECT id,status,billing_version FROM cloud_gateway_camera_entitlements ORDER BY id').fetchall()
            self.assertEqual([tuple(row) for row in states],[('b1','active','evt-reduce'),('b2','revoked','evt-1')])
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b1',customer_id='customer-a',appliance_id='a',camera_id='c1')['status'],'verified')
            self.assertEqual(self.reconcile('evt-reduce',11)['status'],'already_processed')
            self.assertEqual(self.reconcile('evt-older',9)['status'],'stale')
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b1',customer_id='customer-a',appliance_id='a',camera_id='c1')['status'],'verified')
            self.sub['items']['data'][0]['quantity']=2
            self.reconcile('evt-increase',12)
            self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='b2'").fetchone()[0],'revoked')
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b1',customer_id='customer-a',appliance_id='a',camera_id='c1')['status'],'verified')
            self.db.execute("DELETE FROM audit_logs WHERE action='cloud_gateway.camera_binding_reconciled' AND entity_id='b1'")
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='b1',customer_id='customer-a',appliance_id='a',camera_id='c1')['status'],'unavailable')

    def test_reduction_prefers_active_over_older_suspended_binding(self):
        self.seed_quantity_bindings()
        self.db.execute("UPDATE cloud_gateway_camera_entitlements SET status='suspended' WHERE id='b1'")
        self.sub['items']['data'][0]['quantity']=1
        with patch.dict(sys.modules,{'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}):
            self.reconcile('evt-reduce',11)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT id,status FROM cloud_gateway_camera_entitlements ORDER BY id')],
                         [('b1','revoked'),('b2','active')])

    def test_quantity_reduction_rejects_stale_or_foreign_bindings_atomically(self):
        self.seed_quantity_bindings(); self.db.commit()
        self.sub['items']['data'][0]['quantity']=1
        modules={'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}
        with patch.dict(sys.modules,modules):
            for field,value in (('billing_version','old-unrelated'),('customer_id','customer-b'),
                                ('billing_source','other-subscription-source'),('device_key','wrong-device')):
                self.db.execute('UPDATE cloud_gateway_camera_entitlements SET '+field+'=? WHERE id=?',(value,'b2')); self.db.commit()
                with self.subTest(field=field),self.assertRaises(assignments.BindingConflict):
                    with self.db: self.reconcile('evt-reduce',11)
                source=self.db.execute('SELECT event_id,quantity FROM cloud_gateway_subscription_evidence').fetchone()
                self.assertEqual(tuple(source),('evt-1',2))
                self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='b1'").fetchone()[0],'active')
                original={'billing_version':'evt-1','customer_id':'customer-a','billing_source':'verified_subscription','device_key':'key2'}[field]
                self.db.execute('UPDATE cloud_gateway_camera_entitlements SET '+field+'=? WHERE id=?',(original,'b2')); self.db.commit()

    def test_quantity_reduction_audit_failure_rolls_back_retirements(self):
        self.seed_quantity_bindings(); self.db.commit()
        self.sub['items']['data'][0]['quantity']=1
        def audit(actor,action,entity_type,entity_id,details):
            if action=='cloud_gateway.camera_binding_reconciled': raise RuntimeError('audit unavailable')
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        modules={'partner_db':SimpleNamespace(audit=audit),'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}
        with patch.dict(sys.modules,modules),self.assertRaisesRegex(RuntimeError,'audit unavailable'):
            with self.db: self.reconcile('evt-reduce',11)
        self.assertEqual(tuple(self.db.execute('SELECT event_id,quantity FROM cloud_gateway_subscription_evidence').fetchone()),('evt-1',2))
        self.assertEqual([tuple(item) for item in self.db.execute('SELECT status,billing_version FROM cloud_gateway_camera_entitlements ORDER BY id')],
                         [('active','evt-1'),('active','evt-1')])

    def test_quantity_reduction_rejects_duplicate_camera_binding(self):
        self.seed_quantity_bindings()
        self.db.execute("UPDATE cloud_gateway_camera_entitlements SET camera_id='c1',camera_number=1,device_key='key1' WHERE id='b2'")
        detail=assignments._assignment_details('customer-a','a','c1',1,'key1','sub-test','evt-1')
        self.db.execute("UPDATE audit_logs SET details_json=? WHERE entity_id='b2'",(json.dumps(detail),)); self.db.commit()
        self.sub['items']['data'][0]['quantity']=1
        modules={'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}
        with patch.dict(sys.modules,modules),self.assertRaises(assignments.BindingConflict):
            with self.db: self.reconcile('evt-reduce',11)
        self.assertEqual(tuple(self.db.execute('SELECT event_id,quantity FROM cloud_gateway_subscription_evidence').fetchone()),('evt-1',2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM cloud_gateway_camera_entitlements WHERE status='active'").fetchone()[0],2)

    def test_checkout_anchor_cannot_bind_a_different_subscription(self):
        self.reconcile(); self.db.commit()
        self.sub.update(id='sub-other',latest_invoice='in-other')
        self.checkout['subscription']='sub-other'
        self.invoice.update(id='in-other',subscription='sub-other',charge='ch-other',payment_intent='pi-other')
        self.charge.update(id='ch-other',payment_intent='pi-other')
        with self.assertRaises(producer.EvidenceConflict):
            with self.db: self.reconcile('evt-other',11)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_subscription_evidence').fetchone()[0],1)

    def test_repeated_concurrent_reduction_is_idempotent_in_sqlite(self):
        self.seed_quantity_bindings(); self.db.commit()
        subscription=copy.deepcopy(self.sub); subscription['items']['data'][0]['quantity']=1
        local=threading.local(); barrier=threading.Barrier(2); results=[]; errors=[]
        def audit(actor,action,entity_type,entity_id,details):
            local.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'disposable-synthetic.sqlite')
            target=sqlite3.connect(path); self.db.backup(target); target.close()
            def worker():
                db=sqlite3.connect(path,timeout=5); db.row_factory=sqlite3.Row; local.db=db
                try:
                    barrier.wait(timeout=5); db.execute('BEGIN IMMEDIATE')
                    result=producer.reconcile_verified_snapshot(db,event_id='evt-reduce',event_created=11,customer_id='customer-a',
                        session_id='session-test',subscription=subscription,invoice=self.invoice,charge=self.charge,checkout=self.checkout)
                    db.commit(); results.append(result['status'])
                except Exception as error:
                    db.rollback(); errors.append(repr(error))
                finally: db.close()
            modules={'partner_db':SimpleNamespace(audit=audit),'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)}
            with patch.dict(sys.modules,modules):
                threads=[threading.Thread(target=worker),threading.Thread(target=worker)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors,[]); self.assertCountEqual(results,['active','already_processed'])
            db=sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM cloud_gateway_camera_entitlements WHERE status='active'").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='cloud_gateway.camera_binding_retired'").fetchone()[0],1)
            finally: db.close()

    def test_cancellation_refund_stale_and_same_second_never_restore(self):
        self.reconcile()
        self.db.execute("INSERT INTO cloud_gateway_camera_entitlements VALUES('b','customer-a','a','c','cloud_gateway','verified_subscription','sub-test','active',1,'now',NULL)")
        self.sub['status']='canceled'
        self.assertEqual(self.reconcile('evt-2',20)['status'],'cancelled')
        self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_camera_entitlements').fetchone()[0],'cancelled')
        self.sub['status']='active'
        self.assertEqual(self.reconcile('evt-old',9)['status'],'stale')
        self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_subscription_evidence').fetchone()[0],'cancelled')
        self.assertEqual(self.reconcile('evt-same-second',20)['status'],'suspended')
        self.assertEqual(self.db.execute('SELECT payment_verified FROM cloud_gateway_subscription_evidence').fetchone()[0],0)

    def test_full_refund_and_dispute_suspend(self):
        self.reconcile()
        self.charge['amount_refunded']=1998
        self.assertEqual(self.reconcile('evt-refund',11)['status'],'suspended')
        self.charge['amount_refunded']=0; self.charge['disputed']=True
        self.assertEqual(self.reconcile('evt-dispute',12)['status'],'suspended')

    def test_old_subscription_cancellation_cannot_retire_new_subscription(self):
        self.reconcile(); old=copy.deepcopy(self.sub)
        self.db.execute("UPDATE checkout_pending SET session_id='session-new'")
        self.sub['id']='sub-new'; self.sub['latest_invoice']='in-new'
        self.invoice.update(id='in-new',subscription='sub-new',charge='ch-new',payment_intent='pi-new')
        self.charge.update(id='ch-new',payment_intent='pi-new')
        self.checkout.update(id='session-new',subscription='sub-new')
        self.assertEqual(self.reconcile('evt-new',11,session_id='session-new')['status'],'active')
        self.db.execute("INSERT INTO cloud_gateway_camera_entitlements VALUES('old-binding','customer-a','a','c-old','cloud_gateway','verified_subscription','sub-test','active',1,'now',NULL)")
        self.db.execute("INSERT INTO cloud_gateway_camera_entitlements VALUES('new-binding','customer-a','a','c-new','cloud_gateway','verified_subscription','sub-new','active',1,'now',NULL)")
        old['status']='canceled'
        self.assertEqual(self.reconcile('evt-old-cancel',12,session_id='session-test',subscription=old)['status'],'cancelled')
        states={r['id']:r['status'] for r in self.db.execute('SELECT id,status FROM cloud_gateway_camera_entitlements')}
        self.assertEqual(states,{'old-binding':'cancelled','new-binding':'active'})
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_subscription_evidence WHERE stripe_subscription_id='sub-new'").fetchone()[0],'active')

    def test_foreign_customer_price_and_checkout_anchor_rejected(self):
        with self.assertRaises(producer.EvidenceConflict): self.reconcile(customer_id='customer-b')
        self.sub['metadata']['anyaicam_customer_id']='customer-b'
        with self.assertRaises(producer.EvidenceConflict): self.reconcile()
        self.sub['metadata']['anyaicam_customer_id']='customer-a'
        self.sub['items']['data'][0]['price']['unit_amount']=1000
        with self.assertRaises(producer.EvidenceConflict): self.reconcile()
        self.sub['items']['data'][0]['price']['unit_amount']=999
        with self.assertRaises(producer.EvidenceConflict): self.reconcile(session_id='unknown')
        with self.assertRaises(producer.EvidenceConflict): self.reconcile(checkout=None)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_subscription_evidence').fetchone()[0],0)

    def test_unpaid_or_mismatched_charge_cannot_create_paid_evidence(self):
        for obj,key,value in [(self.checkout,'payment_status','unpaid'),
                              (self.charge,'status','failed'),(self.charge,'amount',1),
                              (self.charge,'currency','eur')]:
            previous=obj[key]; obj[key]=value
            with self.subTest(key=key,value=value), self.assertRaises(producer.EvidenceConflict): self.reconcile()
            obj[key]=previous
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_subscription_evidence').fetchone()[0],0)

    def test_unpaid_current_invoice_is_pending_not_verified(self):
        self.invoice['status']='open'
        self.assertEqual(self.reconcile()['status'],'pending')
        self.assertEqual(self.db.execute('SELECT payment_verified FROM cloud_gateway_subscription_evidence').fetchone()[0],0)

    def test_proposed_migration_is_source_only(self):
        source=(Path(__file__).resolve().parents[1]/'db_migrations.py').read_text(encoding='utf-8')
        versions=[n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Tuple) and len(n.elts)==2
                  and isinstance(n.elts[0],ast.Constant) and n.elts[0].value=='20261109_cloud_gateway_subscription_evidence']
        self.assertEqual(len(versions),1)
        sql=versions[0].elts[1].value
        for key in ('stripe_subscription_id','checkout_session_id','quantity','payment_verified','reversal','source_version'):
            self.assertIn(key,sql)
        self.assertNotIn('INSERT INTO',sql.upper())

    def test_quantity_change_audit_rollback_and_hard_stop(self):
        self.reconcile(); self.db.commit()
        self.sub['items']['data'][0]['quantity']=1; self.fail_audit=True
        with self.assertRaises(RuntimeError):
            with self.db: self.reconcile('evt-2',11)
        self.assertEqual(self.db.execute('SELECT quantity FROM cloud_gateway_subscription_evidence').fetchone()[0],2)
        self.fail_audit=False
        self.assertEqual(self.reconcile('evt-2',11)['status'],'active')
        self.assertEqual(self.db.execute('SELECT quantity FROM cloud_gateway_subscription_evidence').fetchone()[0],1)
        with patch.object(producer,'WEBHOOK_PRODUCER_READY',False):
            self.assertEqual(self.reconcile('evt-3',12)['status'],'unavailable')

    def test_prepared_webhook_entry_never_acknowledges_cloud_event_as_provisioned(self):
        class Retryable(Exception): pass
        event={'type':'checkout.session.completed','data':{'object':{'metadata':{'anyaicam_product':'cloud_gateway'}}}}
        with patch.object(producer,'WEBHOOK_PRODUCER_READY',False), patch.dict(sys.modules,{'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable)}):
            with self.assertRaises(Retryable): producer.sync_from_verified_webhook(event)
            self.assertEqual(producer.sync_from_verified_webhook({'type':'other'}),{'status':'ignored'})

    def test_checkout_anchor_fields_are_server_classified(self):
        purchase,fields=producer.checkout_fields(customer_id='customer-a',stripe_customer_id='cus-test',
            price_id='price_cloud_test',quantity=2,success_url='https://example.test/success',cancel_url='https://example.test/cancel')
        self.assertEqual(purchase,'cloud_gateway')
        values=dict(fields)
        self.assertEqual(values['line_items[0][quantity]'],'2')
        self.assertEqual(values['subscription_data[metadata][anyaicam_product]'],'cloud_gateway')
        with self.assertRaises(ValueError):
            producer.checkout_fields(customer_id='customer-a',stripe_customer_id='cus-test',price_id='price_other',quantity=2,success_url='s',cancel_url='c')

    def test_canonical_dispatch_uses_payment_locks_and_reversal_ledger(self):
        class Retryable(Exception): pass
        lock_calls=[]; ledger={'reason':None}; reads=[]
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            found=self.db.execute(sql,args).fetchone()
            return dict(found) if found else None
        def fetch(path,description):
            reads.append(path)
            if path == '/v1/checkout/sessions/session-test': return copy.deepcopy(self.checkout)
            if path == '/v1/subscriptions/sub-test': return copy.deepcopy(self.sub)
            if path == '/v1/invoices/in-test': return copy.deepcopy(self.invoice)
            if path == '/v1/charges/ch-test': return copy.deepcopy(self.charge)
            raise AssertionError(path)
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:lock_calls.append(tuple(keys)),
                                reversal_reason=lambda *ids:ledger['reason'])
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda stripe,customer:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=audit)}
        event={'id':'evt-1','created':10,'type':'checkout.session.completed',
               'data':{'object':{'id':'session-test','metadata':{'anyaicam_product':'cloud_gateway'}}}}
        with patch.dict(sys.modules,modules):
            self.assertEqual(producer.sync_from_verified_webhook(event,fetch=fetch)['status'],'active')
            self.assertEqual(lock_calls,[('ch-test','pi-test','in-test')])
            self.assertGreaterEqual(reads.count('/v1/subscriptions/sub-test'),2)
            self.assertEqual(producer.sync_from_verified_webhook(event,fetch=fetch)['status'],'already_processed')
            self.charge['amount_refunded']=100
            partial={'id':'evt-partial','created':11,'type':'charge.refunded','data':{'object':{'id':'ch-test'}}}
            self.assertEqual(producer.sync_from_verified_webhook(partial,fetch=fetch)['version'],'evt-1')
            ledger['reason']='disputed'
            dispute={'id':'evt-dispute','created':12,'type':'charge.dispute.created','data':{'object':{'charge':'ch-test'}}}
            self.assertEqual(producer.sync_from_verified_webhook(dispute,fetch=fetch)['status'],'suspended')

    def test_canonical_checkout_rejects_client_paid_claim(self):
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def fetch(path,description):
            return copy.deepcopy({'/v1/checkout/sessions/session-test':self.checkout,
                '/v1/subscriptions/sub-test':self.sub,'/v1/invoices/in-test':self.invoice,
                '/v1/charges/ch-test':self.charge}[path])
        self.checkout['payment_status']='unpaid'
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda stripe,customer:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,row=lambda sql,args:None,audit=lambda *args:None)}
        event={'id':'evt-1','created':10,'type':'checkout.session.completed',
               'data':{'object':{'id':'session-test','payment_status':'paid','metadata':{'anyaicam_product':'cloud_gateway'}}}}
        with patch.dict(sys.modules,modules):
            with self.assertRaises(Retryable): producer.sync_from_verified_webhook(event,fetch=fetch)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_subscription_evidence').fetchone()[0],0)

    def test_provider_failure_retries_and_assignment_gate_remains_closed(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            found=self.db.execute(sql,args).fetchone()
            return dict(found) if found else None
        def failed_fetch(path,description): raise Retryable('provider unavailable')
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':SimpleNamespace(), 'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=lambda *args:None)}
        event={'id':'evt-later','created':20,'type':'customer.subscription.updated',
               'data':{'object':{'id':'sub-test','metadata':{'anyaicam_product':'cloud_gateway'}}}}
        with patch.dict(sys.modules,modules),patch.object(assignments,'CLOUD_GATEWAY_BILLING_SOURCE_READY',False):
            with self.assertRaises(Retryable): producer.sync_from_verified_webhook(event,fetch=failed_fetch)
            self.assertEqual(assignments.verify_paid_binding(self.db,binding_id='x',customer_id='customer-a',appliance_id='a',camera_id='c')['status'],'unavailable')
        self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_subscription_evidence').fetchone()[0],'active')
        self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM cloud_gateway_evidence_holds').fetchone()[0],1)

    def test_failure_holds_retry_recovery_and_unrelated_event_fencing(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            result=self.db.execute(sql,args).fetchone()
            return dict(result) if result else None
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        stripe=SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False)
        modules={'stripe_state':stripe,'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=audit)}
        def event(ident,created):
            return {'id':ident,'created':created,'type':'customer.subscription.updated','data':{'object':{'id':'sub-test'}}}
        responses={'/v1/subscriptions/sub-test':self.sub,'/v1/invoices/in-test':self.invoice,'/v1/charges/ch-test':self.charge}
        def fetch(path,description): return copy.deepcopy(responses[path])
        def timeout(path,description): raise TimeoutError('timeout')
        with patch.dict(sys.modules,modules):
            for e in (event('evt-failed',20),event('evt-failed',20),event('evt-other',21)):
                with self.assertRaises(TimeoutError): producer.sync_from_verified_webhook(e,fetch=timeout)
            self.assertEqual(self.db.execute('SELECT count(*) FROM cloud_gateway_evidence_holds').fetchone()[0],2)
            self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
            self.assertEqual(producer.sync_from_verified_webhook(event('evt-other',21),fetch=fetch)['status'],'active')
            self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
            self.assertEqual(producer.sync_from_verified_webhook(event('evt-failed',20),fetch=fetch)['status'],'stale')
            self.assertEqual(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test')['version'],'evt-1')
            self.assertEqual(self.db.execute("SELECT count(*) FROM audit_logs WHERE action='cloud_gateway.subscription_verification_held'").fetchone()[0],2)
            self.assertEqual(self.db.execute("SELECT count(*) FROM audit_logs WHERE action='cloud_gateway.subscription_verification_released'").fetchone()[0],2)

    def test_hold_scopes_to_subscription_and_cancellation_retires_its_bindings(self):
        self.reconcile(); self.db.commit()
        self.db.execute("INSERT INTO cloud_gateway_camera_entitlements VALUES('a-binding','customer-a','a','a-camera','cloud_gateway','verified_subscription','sub-test','active',1,'now',NULL)")
        self.db.execute("INSERT INTO cloud_gateway_camera_entitlements VALUES('b-binding','customer-b','b','b-camera','cloud_gateway','verified_subscription','sub-other','active',1,'now',NULL)")
        self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            found=self.db.execute(sql,args).fetchone()
            return dict(found) if found else None
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=audit)}
        event={'id':'evt-cancel','created':20,'type':'customer.subscription.deleted','data':{'object':{'id':'sub-test'}}}
        self.sub['status']='canceled'
        with patch.dict(sys.modules,modules):
            self.assertEqual(producer.sync_from_verified_webhook(event,fetch=lambda *args:copy.deepcopy(self.sub))['status'],'cancelled')
        states={r['id']:r['status'] for r in self.db.execute('SELECT id,status FROM cloud_gateway_camera_entitlements')}
        self.assertEqual(states,{'a-binding':'cancelled','b-binding':'active'})
        self.assertEqual(self.db.execute('SELECT count(*) FROM cloud_gateway_evidence_holds').fetchone()[0],0)

    def test_malformed_or_mismatched_provider_data_stays_on_hold(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            found=self.db.execute(sql,args).fetchone()
            return dict(found) if found else None
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=audit)}
        event={'id':'evt-invalid','created':20,'type':'customer.subscription.updated','data':{'object':{'id':'sub-test'}}}
        responses={'/v1/subscriptions/sub-test':self.sub,'/v1/invoices/in-test':self.invoice,'/v1/charges/ch-test':self.charge}
        with patch.dict(sys.modules,modules):
            with self.assertRaises(Retryable): producer.sync_from_verified_webhook(event,fetch=lambda *args:None)
            self.sub['metadata']['anyaicam_customer_id']='customer-b'
            with self.assertRaises(Retryable): producer.sync_from_verified_webhook(event,fetch=lambda path,description:copy.deepcopy(responses[path]))
            self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
            self.assertEqual(self.db.execute('SELECT count(*) FROM cloud_gateway_evidence_holds').fetchone()[0],1)

    def test_stale_future_and_missing_hold_schema_fail_closed(self):
        self.reconcile()
        self.db.execute("UPDATE cloud_gateway_subscription_evidence SET updated_at='2000-01-01T00:00:00+00:00'")
        self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))

    def test_refresh_retry_preserves_versions_and_webhook_holds(self):
        self.reconcile(); self.db.commit()
        self.db.execute("UPDATE cloud_gateway_subscription_evidence SET updated_at='2000-01-01T00:00:00+00:00'"); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
                 'cloud_gateway_subscription_evidence':producer}
        responses={'/v1/subscriptions/sub-test':self.sub,'/v1/invoices/in-test':self.invoice,'/v1/charges/ch-test':self.charge}
        def fetch(path,description): return copy.deepcopy(responses[path])
        def fail(*args): raise TimeoutError('outage')
        with patch.dict(sys.modules,modules):
            with self.assertRaises(TimeoutError): refresh.refresh_one('customer-a','sub-test',fetch=fail)
            self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
            self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_subscription_evidence').fetchone()[0],'active')
            self.assertEqual(refresh.refresh_one('customer-a','sub-test',fetch=fetch)['status'],'refreshed')
            self.assertEqual(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test')['version'],'evt-1')
            self.db.execute("INSERT INTO cloud_gateway_evidence_holds VALUES('sub-test','customer-a','evt-webhook',11,'now')")
            self.db.commit()
            self.assertEqual(refresh.refresh_one('customer-a','sub-test',fetch=fetch)['status'],'refreshed')
            self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
            self.assertEqual(self.db.execute('SELECT event_id FROM cloud_gateway_subscription_evidence').fetchone()[0],'evt-1')

    def test_refresh_batch_is_disabled_and_retries_individual_failures(self):
        with patch.object(refresh,'REFRESH_WORKER_READY',False):
            self.assertEqual(refresh.refresh_batch(),{'status':'unavailable'})
        records=[{'customer_id':'a','stripe_subscription_id':'sub-a'}, {'customer_id':'b','stripe_subscription_id':'sub-b'}]
        with patch.object(refresh,'REFRESH_WORKER_READY',True), patch.dict(sys.modules,{'partner_db':SimpleNamespace(rows=lambda *args:records)}), \
             patch.object(refresh,'refresh_one',side_effect=[TimeoutError('offline'),{'status':'refreshed'}]):
            self.assertEqual(refresh.refresh_batch(fetch=lambda *args:None),{'status':'complete','selected':2,'refreshed':1,'failed':1,'next_cursor':'sub-b'})

    def test_refresh_lease_blocks_and_fences_replaced_workers(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
                 'cloud_gateway_subscription_evidence':producer}
        responses={'/v1/subscriptions/sub-test':self.sub,'/v1/invoices/in-test':self.invoice,'/v1/charges/ch-test':self.charge}
        self.db.execute("INSERT INTO cloud_gateway_refresh_leases VALUES('sub-test','customer-a','other',4102444800)"); self.db.commit()
        with patch.dict(sys.modules,modules):
            self.assertEqual(refresh.refresh_one('customer-a','sub-test',fetch=lambda *args:self.fail('busy must not fetch'))['status'],'busy')
            self.db.execute('DELETE FROM cloud_gateway_refresh_leases'); self.db.commit()
            def replace(path,description):
                if path == '/v1/subscriptions/sub-test':
                    self.db.execute("UPDATE cloud_gateway_refresh_leases SET token='replacement'"); self.db.commit()
                return copy.deepcopy(responses[path])
            with self.assertRaises(Retryable): refresh.refresh_one('customer-a','sub-test',fetch=replace)
            self.assertEqual(self.db.execute('SELECT token FROM cloud_gateway_refresh_leases').fetchone()[0],'replacement')
            self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))

    def test_unknown_charge_correlation_is_durable_and_customer_scoped(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable),
                 'billing_status':SimpleNamespace(),'partner_db':SimpleNamespace(transaction=transaction,row=lambda *args:None,audit=audit)}
        def failed(*args): raise TimeoutError('offline')
        event={'id':'evt-risk','created':20,'type':'charge.refunded',
               'data':{'object':{'id':'ch-old','customer':'cus-test'}}}
        unknown={'id':'evt-unknown','created':21,'type':'charge.refunded',
                 'data':{'object':{'id':'ch-unknown'}}}
        with patch.dict(sys.modules,modules):
            for item in (event,event,unknown):
                with self.assertRaises(TimeoutError): producer.sync_from_verified_webhook(item,fetch=failed)
        records=self.db.execute('SELECT event_id,status FROM cloud_gateway_webhook_correlation ORDER BY event_id').fetchall()
        self.assertEqual([(r['event_id'],r['status']) for r in records],[('evt-risk','pending'),('evt-unknown','pending')])
        holds=self.db.execute('SELECT customer_id,stripe_subscription_id,event_id FROM cloud_gateway_evidence_holds').fetchall()
        self.assertEqual([tuple(r) for r in holds],[('customer-a','sub-test','evt-risk')])
        self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))

    def test_queued_event_retries_canonical_identity_and_recovers(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            value=self.db.execute(sql,args).fetchone()
            return dict(value) if value else None
        def rows(sql,args): return [dict(value) for value in self.db.execute(sql,args).fetchall()]
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,row=row,rows=rows,audit=audit)}
        event={'id':'evt-queued','created':20,'type':'invoice.paid',
               'data':{'object':{'id':'in-test','subscription':'sub-test','customer':'cus-test'}}}
        def timeout(*args): raise TimeoutError('offline')
        responses={'/v1/events/evt-queued':event,'/v1/subscriptions/sub-test':self.sub,
                   '/v1/invoices/in-test':self.invoice,'/v1/charges/ch-test':self.charge}
        def fetch(path,description): return copy.deepcopy(responses[path])
        with patch.dict(sys.modules,modules):
            with self.assertRaises(TimeoutError): producer.sync_from_verified_webhook(event,fetch=timeout)
            bad=copy.deepcopy(event); bad['created']=19
            with patch.dict(responses,{'/v1/events/evt-queued':bad}):
                self.assertEqual(producer.retry_pending_correlations(fetch=fetch)['failed'],1)
            self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_webhook_correlation').fetchone()[0],'pending')
            result=producer.retry_pending_correlations(fetch=fetch)
            self.assertEqual((result['selected'],result['reconciled'],result['failed']),(1,1,0))
            self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_webhook_correlation').fetchone()[0],'resolved')
            self.assertEqual(producer.retry_pending_correlations(fetch=fetch)['selected'],0)

    def test_resolved_old_charge_is_held_before_subscription_fetch(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            result=self.db.execute(sql,args).fetchone()
            return dict(result) if result else None
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable),'billing_status':SimpleNamespace(),
                 'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=audit)}
        event={'id':'evt-old-charge','created':20,'type':'charge.refunded','data':{'object':{'id':'ch-old'}}}
        def fetch(path,description):
            if path == '/v1/charges/ch-old': return {'id':'ch-old','invoice':'in-old','customer':'cus-test'}
            if path == '/v1/invoices/in-old': return {'id':'in-old','customer':'cus-test','subscription':'sub-test'}
            raise TimeoutError('provider outage')
        with patch.dict(sys.modules,modules):
            with self.assertRaises(TimeoutError): producer.sync_from_verified_webhook(event,fetch=fetch)
        self.assertEqual(self.db.execute('SELECT event_id FROM cloud_gateway_evidence_holds').fetchone()[0],'evt-old-charge')
        self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))

    def test_historical_charge_fences_only_its_subscription_before_provider_fetch(self):
        self.reconcile(); self.db.commit()
        self.sub['latest_invoice']='in-next'
        self.invoice.update(id='in-next',charge='ch-next',payment_intent='pi-next')
        self.charge.update(id='ch-next',payment_intent='pi-next')
        self.reconcile('evt-next',11); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable),'billing_status':SimpleNamespace(),
                 'partner_db':SimpleNamespace(transaction=transaction,row=lambda *args:None,audit=audit)}
        event={'id':'evt-old-refund','created':20,'type':'charge.refunded','data':{'object':{'id':'ch-test'}}}
        def timeout(*args): raise TimeoutError('offline')
        with patch.dict(sys.modules,modules),self.assertRaises(TimeoutError):
            producer.sync_from_verified_webhook(event,fetch=timeout)
        hold=self.db.execute('SELECT customer_id,stripe_subscription_id,event_id FROM cloud_gateway_evidence_holds').fetchone()
        self.assertEqual(tuple(hold),('customer-a','sub-test','evt-old-refund'))

    def test_payment_identity_cannot_move_to_a_different_subscription(self):
        self.reconcile(); self.db.commit()
        self.db.execute("UPDATE checkout_pending SET session_id='session-new'")
        self.sub['id']='sub-new'; self.checkout.update(id='session-new',subscription='sub-new')
        self.invoice['subscription']='sub-new'
        self.db.commit()
        with self.assertRaises(producer.EvidenceConflict):
            with self.db: self.reconcile('evt-new',11,session_id='session-new')
        self.assertEqual(self.db.execute("SELECT stripe_subscription_id FROM gateway_stripe_payment_links WHERE kind='charge' AND provider_id='ch-test'").fetchone()[0],'sub-test')
        self.assertEqual(self.db.execute("SELECT count(*) FROM cloud_gateway_subscription_evidence WHERE stripe_subscription_id='sub-new'").fetchone()[0],0)

    def test_resolved_charge_releases_only_same_customer_candidate_holds(self):
        self.reconcile(); self.db.commit()
        source=dict(self.db.execute('SELECT * FROM cloud_gateway_subscription_evidence').fetchone())
        for subscription,customer,stripe_customer in [('sub-other','customer-a','cus-test'),('sub-b','customer-b','cus-b')]:
            item={**source,'stripe_subscription_id':subscription,'customer_id':customer,'stripe_customer_id':stripe_customer,
                  'event_id':'older-'+subscription,'checkout_session_id':'session-'+subscription}
            columns=list(item)
            self.db.execute('INSERT INTO cloud_gateway_subscription_evidence('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(item[c] for c in columns))
        self.db.execute("INSERT INTO cloud_gateway_evidence_holds VALUES('sub-b','customer-b','evt-charge',20,'now')")
        self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            result=self.db.execute(sql,args).fetchone()
            return dict(result) if result else None
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,row=row,audit=audit)}
        event={'id':'evt-charge','created':20,'type':'charge.refunded','data':{'object':{'id':'ch-old','customer':'cus-test'}}}
        responses={'/v1/charges/ch-old':{'id':'ch-old','customer':'cus-test','invoice':'in-old'},
                   '/v1/invoices/in-old':{'id':'in-old','customer':'cus-test','subscription':'sub-test'},
                   '/v1/subscriptions/sub-test':self.sub,'/v1/invoices/in-test':self.invoice,'/v1/charges/ch-test':self.charge}
        with patch.dict(sys.modules,modules):
            self.assertEqual(producer.sync_from_verified_webhook(event,fetch=lambda path,description:copy.deepcopy(responses[path]))['status'],'active')
        holds=[tuple(r) for r in self.db.execute('SELECT customer_id,stripe_subscription_id FROM cloud_gateway_evidence_holds')]
        self.assertEqual(holds,[('customer-b','sub-b')])
        self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_webhook_correlation').fetchone()[0],'pending')

    def test_refresh_rejects_quantity_cancellation_and_other_customer(self):
        self.reconcile(); self.db.commit()
        class Retryable(Exception): pass
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        billing=SimpleNamespace(payment_keys=lambda *ids:list(ids),lock_payment=lambda db,keys:None,reversal_reason=lambda *ids:None)
        modules={'stripe_state':SimpleNamespace(RetryableStripeEventError=Retryable,bound_elsewhere=lambda *args:False),
                 'billing_status':billing,'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
                 'cloud_gateway_subscription_evidence':producer}
        responses={'/v1/subscriptions/sub-test':self.sub,'/v1/invoices/in-test':self.invoice,'/v1/charges/ch-test':self.charge}
        def fetch(path,description): return copy.deepcopy(responses[path])
        with patch.dict(sys.modules,modules):
            with self.assertRaises(Retryable): refresh.refresh_one('customer-b','sub-test',fetch=fetch)
            self.sub['items']['data'][0]['quantity']=1
            with self.assertRaises(Retryable): refresh.refresh_one('customer-a','sub-test',fetch=fetch)
            self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
            self.assertEqual(self.reconcile('evt-downgrade',11)['status'],'active')
            self.assertEqual(refresh.refresh_one('customer-a','sub-test',fetch=fetch)['status'],'refreshed')
            self.assertEqual(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test')['quantity'],1)
            self.sub['status']='canceled'
            with self.assertRaises(Retryable): refresh.refresh_one('customer-a','sub-test',fetch=fetch)
            self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_subscription_evidence').fetchone()[0],'active')
            self.assertEqual(self.reconcile('evt-cancel',12)['status'],'cancelled')
        self.db.execute("UPDATE cloud_gateway_subscription_evidence SET updated_at='2999-01-01T00:00:00+00:00'")
        self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))
        self.db.execute('DROP TABLE cloud_gateway_evidence_holds')
        self.assertIsNone(assignments._current_verified_cloud_subscription(self.db,'customer-a','sub-test'))

    def test_concurrent_snapshot_reconciliation_keeps_latest_event_in_sqlite(self):
        local=threading.local(); barrier=threading.Barrier(2); results=[]; errors=[]
        def audit(actor,action,entity_type,entity_id,details):
            local.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',(actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=audit)}):
            path=str(Path(directory)/'synthetic.db')
            target=sqlite3.connect(path); self.db.backup(target); target.close()
            def worker(event_id,created):
                db=sqlite3.connect(path,timeout=5); db.row_factory=sqlite3.Row; local.db=db
                try:
                    barrier.wait(timeout=5); db.execute('BEGIN IMMEDIATE')
                    result=producer.reconcile_verified_snapshot(db,event_id=event_id,event_created=created,
                        customer_id='customer-a',session_id='session-test',subscription=self.sub,
                        invoice=self.invoice,charge=self.charge,checkout=self.checkout)
                    db.commit(); results.append(result['status'])
                except Exception as error:
                    db.rollback(); errors.append(str(error))
                finally: db.close()
            threads=[threading.Thread(target=worker,args=('evt-early',10)),threading.Thread(target=worker,args=('evt-late',20))]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads)); self.assertEqual(errors,[])
            db=sqlite3.connect(path)
            try: self.assertEqual(db.execute('SELECT event_id FROM cloud_gateway_subscription_evidence').fetchone()[0],'evt-late')
            finally: db.close()


if __name__ == '__main__': unittest.main()
