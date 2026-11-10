"""Assignment algorithm under mocked future billing evidence; no live producer."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app import cloud_gateway_camera_entitlements as service


SCHEMA = '''
CREATE TABLE appliances(id TEXT PRIMARY KEY,customer_id TEXT,gateway_edition TEXT);
CREATE TABLE cameras(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,camera_number INTEGER,device_key TEXT,status TEXT);
CREATE TABLE cloud_gateway_camera_entitlements(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,camera_id TEXT,camera_number INTEGER,device_key TEXT,edition TEXT,billing_source TEXT,billing_reference TEXT,billing_version TEXT,status TEXT,version INTEGER,audit_reference TEXT,created_at TEXT,updated_at TEXT,ended_reason TEXT);
CREATE UNIQUE INDEX one_current ON cloud_gateway_camera_entitlements(camera_id) WHERE status IN ('pending','active','suspended');
CREATE TABLE audit_logs(action TEXT,entity_type TEXT,entity_id TEXT,details_json TEXT);
INSERT INTO appliances VALUES('a','customer-a','cloud_gateway');
INSERT INTO cameras VALUES('c1','customer-a','a',1,'key-1','active');
INSERT INTO cameras VALUES('c2','customer-a','a',2,'key-2','active');
INSERT INTO cameras VALUES('c3','customer-a','a',3,'key-3','active');
'''


class PaidAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row; self.db.executescript(SCHEMA)
        self.addCleanup(self.db.close)
        self.subscription={'customer_id':'customer-a','subscription_id':'sub-test','version':'v1','product':'cloud_gateway',
            'status':'active','stripe_status':'active','verified_paid':True,'payment_failed_at':None,'reversal':False,
            'quantity':2,'current_period_end':4102444800,'price_id':'price_synthetic_cloud',
            'amount_cents':999,'currency':'usd','interval':'month','interval_count':1}
        self.actor={'email':'admin@example.test','role':'administrator'}
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(action,entity_type,entity_id,json.dumps(details)))
        modules={'partner_db':SimpleNamespace(audit=audit),
                 'database_backend':SimpleNamespace(lock_customer_camera_capacity=lambda db,customer:customer=='customer-a'),
                 'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda db,customer,appliance,edition:True)}
        self.patches=[patch.dict(sys.modules,modules),patch.dict('os.environ',{'ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY':'price_synthetic_cloud'}),
            patch.object(service,'CLOUD_GATEWAY_BILLING_SOURCE_READY',True),
            patch.object(service,'CLOUD_GATEWAY_REPLACEMENT_INTENT_READY',True),
            patch.object(service,'_current_verified_cloud_subscription',side_effect=lambda db,customer,reference:dict(self.subscription))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def assign(self,camera='c1', request='binding-1', **changes):
        args=dict(source_reference='sub-test',source_version='v1',customer_id='customer-a',appliance_id='a',camera_id=camera,actor=self.actor,request_id=request)
        return service.produce_paid_binding(self.db,**{**args,**changes})

    def verify(self,binding='binding-1',camera='c1'):
        return service.verify_paid_binding(self.db,binding_id=binding,customer_id='customer-a',appliance_id='a',camera_id=camera)['status']

    def replace(self, **changes):
        args=dict(actor={'email':'owner@example.test','role':'customer_owner','customer_id':'customer-a'},
                  customer_id='customer-a',old_binding_id='binding-1',old_version=1,
                  source_reference='sub-test',source_version='v1',new_appliance_id='a',
                  new_camera_id='c2',request_id='replacement-1')
        return service.replace_paid_binding(self.db,**{**args,**changes})

    def test_atomic_same_customer_replacement_and_retry(self):
        self.subscription['quantity']=1; self.assign()
        self.assertEqual(self.replace()['status'],'replaced')
        self.assertEqual(self.replace()['status'],'already_replaced')
        self.assertEqual(self.verify('binding-1','c1'),'unavailable')
        self.assertEqual(self.verify('replacement-1','c2'),'verified')
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,status FROM cloud_gateway_camera_entitlements ORDER BY id')],
                         [('binding-1','revoked'),('replacement-1','active')])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='cloud_gateway.camera_binding_replaced'").fetchone()[0],1)

    def test_same_customer_appliance_move(self):
        self.assign(); self.db.execute("INSERT INTO appliances VALUES('b','customer-a','cloud_gateway')")
        self.db.execute("UPDATE cameras SET appliance_id='b' WHERE id='c1'")
        result=self.replace(new_appliance_id='b',new_camera_id='c1')
        self.assertEqual(result['status'],'replaced')
        self.assertEqual(service.verify_paid_binding(self.db,binding_id='replacement-1',customer_id='customer-a',appliance_id='b',camera_id='c1')['status'],'verified')

    def setup_move_adapter(self):
        self.db.execute('ALTER TABLE appliances ADD COLUMN site_id TEXT')
        self.db.execute('ALTER TABLE cameras ADD COLUMN site_id TEXT')
        self.db.execute("UPDATE appliances SET site_id='site-a' WHERE id='a'")
        self.db.execute("UPDATE cameras SET site_id='site-a'")
        self.db.execute("INSERT INTO appliances(id,customer_id,gateway_edition,site_id) VALUES('b','customer-a','cloud_gateway','site-b')")
        self.subscription['quantity']=1; self.assign(); self.db.commit()

    def move_adapter(self, **changes):
        args=dict(actor={'email':'owner@example.test','role':'customer_owner','customer_id':'customer-a'},customer_id='customer-a',
                  old_binding_id='binding-1',old_version=1,source_reference='sub-test',source_version='v1',
                  target_appliance_id='b',request_id='move-1')
        return service.move_paid_camera_binding(self.db,**{**args,**changes})

    def test_move_adapter_commits_mapping_and_binding_once(self):
        self.setup_move_adapter()
        with self.db: self.assertEqual(self.move_adapter()['status'],'replaced')
        with self.db: self.assertEqual(self.move_adapter()['status'],'already_replaced')
        self.assertEqual(tuple(self.db.execute("SELECT appliance_id,site_id FROM cameras WHERE id='c1'").fetchone()),('b','site-b'))
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,status FROM cloud_gateway_camera_entitlements ORDER BY id')],
                         [('binding-1','revoked'),('move-1','active')])
        self.db.execute("UPDATE cameras SET site_id='wrong-site' WHERE id='c1'")
        with self.assertRaises(service.BindingConflict): self.move_adapter()

    def test_move_adapter_rolls_back_on_mapping_failure(self):
        self.setup_move_adapter()
        self.db.execute("CREATE TRIGGER reject_move BEFORE UPDATE OF appliance_id ON cameras BEGIN SELECT RAISE(ABORT,'mapping failed'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db: self.move_adapter()
        self.assertEqual(tuple(self.db.execute("SELECT appliance_id,site_id FROM cameras WHERE id='c1'").fetchone()),('a','site-a'))
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'active')

    def test_move_adapter_rejects_stale_duplicate_and_wrong_owner(self):
        self.setup_move_adapter()
        with self.assertRaises(service.BindingConflict):
            with self.db: self.move_adapter(source_version='stale')
        self.db.execute("UPDATE appliances SET customer_id='customer-b' WHERE id='b'"); self.db.commit()
        with self.assertRaises(service.BindingConflict):
            with self.db: self.move_adapter()
        self.db.execute("UPDATE appliances SET customer_id='customer-a' WHERE id='b'")
        self.db.execute("INSERT INTO cameras VALUES('duplicate','customer-a','b',7,'key-1','active','site-b')"); self.db.commit()
        with self.assertRaises(service.BindingConflict):
            with self.db: self.move_adapter()
        self.assertEqual(tuple(self.db.execute("SELECT appliance_id,site_id FROM cameras WHERE id='c1'").fetchone()),('a','site-a'))
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'active')

    def test_move_adapter_rolls_back_mapping_if_replacement_audit_fails(self):
        self.setup_move_adapter()
        def fail_final(actor,action,entity_type,entity_id,details):
            if action=='cloud_gateway.camera_binding_replaced': raise RuntimeError('audit failure')
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(action,entity_type,entity_id,json.dumps(details)))
        with patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=fail_final)}):
            with self.assertRaises(RuntimeError):
                with self.db: self.move_adapter()
        self.assertEqual(tuple(self.db.execute("SELECT appliance_id,site_id FROM cameras WHERE id='c1'").fetchone()),('a','site-a'))
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,status FROM cloud_gateway_camera_entitlements')],[('binding-1','active')])

    def test_move_adapter_rejects_destination_number_collision_and_other_owner(self):
        self.setup_move_adapter()
        self.db.execute("INSERT INTO cameras VALUES('target-slot','customer-a','b',1,'other-key','active','site-b')"); self.db.commit()
        with self.assertRaises(service.BindingConflict):
            with self.db: self.move_adapter()
        self.assertEqual(self.db.execute("SELECT appliance_id FROM cameras WHERE id='c1'").fetchone()[0],'a')
        with self.assertRaises(service.BindingConflict):
            with self.db: self.move_adapter(actor={'email':'foreign@example.test','role':'customer_owner','customer_id':'customer-b'})
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'active')

    def test_appliance_callback_or_viewer_is_not_owner_replacement_authority(self):
        self.setup_move_adapter()
        for actor in ({'email':'appliance-test','role':'appliance','customer_id':'customer-a'},
                      {'email':'viewer@example.test','role':'customer_viewer','customer_id':'customer-a'},
                      {'email':'owner@example.test','role':'customer_owner','customer_id':'customer-b'}):
            with self.subTest(actor=actor), self.assertRaises(service.BindingConflict):
                with self.db: self.move_adapter(actor=actor)
        self.assertEqual(tuple(self.db.execute("SELECT appliance_id,site_id FROM cameras WHERE id='c1'").fetchone()),('a','site-a'))
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'active')

    def test_replacement_requires_independent_owner_intent_gate(self):
        self.setup_move_adapter()
        with patch.object(service,'CLOUD_GATEWAY_REPLACEMENT_INTENT_READY',False,create=True):
            with self.assertRaises(service.BindingConflict):
                with self.db: self.move_adapter()
            with self.assertRaises(service.BindingConflict):
                with self.db: self.replace()
        self.assertEqual(tuple(self.db.execute("SELECT appliance_id,site_id FROM cameras WHERE id='c1'").fetchone()),('a','site-a'))
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'active')

    def test_replacement_rejects_duplicate_stale_and_cross_customer(self):
        self.assign(); self.db.commit()
        self.db.execute("INSERT INTO appliances VALUES('b','customer-b','cloud_gateway')")
        self.db.execute("INSERT INTO cameras VALUES('foreign','customer-b','b',9,'foreign-key','active')")
        for changes in ({'source_version':'old'}, {'old_version':2}, {'new_appliance_id':'b','new_camera_id':'foreign'},
                        {'actor':{'email':'other@example.test','role':'customer_owner','customer_id':'customer-b'}}):
            with self.subTest(changes=changes), self.assertRaises(service.BindingConflict):
                with self.db: self.replace(**changes)
            self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'active')
        self.db.execute("UPDATE cameras SET device_key='key-2' WHERE id='c3'"); self.db.commit()
        with self.assertRaises(service.BindingConflict):
            with self.db: self.replace()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM cloud_gateway_camera_entitlements WHERE status='active'").fetchone()[0],1)

    def test_replacement_audit_failure_rolls_back_and_retired_never_reactivates(self):
        self.assign(); self.db.commit()
        def fail_final(actor,action,entity_type,entity_id,details):
            if action=='cloud_gateway.camera_binding_replaced': raise RuntimeError('audit failure')
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(action,entity_type,entity_id,json.dumps(details)))
        with patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=fail_final)}):
            with self.assertRaises(RuntimeError):
                with self.db: self.replace()
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,status FROM cloud_gateway_camera_entitlements')],[('binding-1','active')])
        service.retire_binding(self.db,self.actor,binding_id='binding-1',customer_id='customer-a',appliance_id='a',camera_id='c1',version=1,reason='entitlement_revoked')
        with self.assertRaises(service.BindingConflict): self.replace()
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'revoked')

    def test_replacement_rejects_duplicate_original_binding(self):
        self.assign(); self.db.execute('DROP INDEX one_current')
        self.db.execute("INSERT INTO cloud_gateway_camera_entitlements SELECT 'duplicate',customer_id,appliance_id,camera_id,camera_number,device_key,edition,billing_source,billing_reference,billing_version,status,version,audit_reference,created_at,updated_at,ended_reason FROM cloud_gateway_camera_entitlements WHERE id='binding-1'")
        with self.assertRaises(service.BindingConflict): self.replace()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM cloud_gateway_camera_entitlements WHERE status='active'").fetchone()[0],2)

    def test_replacement_denies_changed_old_owner_and_disabled_source(self):
        self.assign(); self.db.commit()
        self.db.execute("UPDATE cameras SET customer_id='customer-b' WHERE id='c1'")
        with self.assertRaises(service.BindingConflict): self.replace()
        self.db.execute("UPDATE cameras SET customer_id='customer-a' WHERE id='c1'")
        with patch.object(service,'CLOUD_GATEWAY_BILLING_SOURCE_READY',False):
            with self.assertRaises(service.BindingConflict): self.replace()
        self.assertEqual(self.db.execute("SELECT status FROM cloud_gateway_camera_entitlements WHERE id='binding-1'").fetchone()[0],'active')

    def test_paid_quantity_idempotency_and_overallocation(self):
        self.assertEqual(self.assign()['status'],'assigned')
        self.assertEqual(self.assign()['status'],'already_assigned')
        self.assertEqual(self.assign('c2','binding-2')['status'],'assigned')
        with self.assertRaises(service.BindingConflict): self.assign('c3','binding-3')
        self.assertEqual(self.verify(),'verified')
        self.subscription['quantity']=1
        self.assertEqual(self.verify(),'unavailable')
        with self.assertRaises(service.BindingConflict): self.assign()

    def test_unpaid_cancel_refund_stale_version_and_price_conflict_deny(self):
        self.assertEqual(self.assign()['status'],'assigned')
        for change in ({'status':'cancelled'},{'verified_paid':False},{'reversal':True},
                       {'payment_failed_at':'failed'},{'version':'v2'},{'current_period_end':1},
                       {'amount_cents':1000},{'currency':'eur'},{'interval':'year'}):
            previous=dict(self.subscription); self.subscription.update(change)
            with self.subTest(change=change): self.assertEqual(self.verify(),'unavailable')
            self.subscription=previous
        with patch.dict('os.environ', {'ANYAICAM_STRIPE_PRICE_BASIC_LOCAL':'price_synthetic_cloud'}):
            self.assertEqual(self.verify(),'unavailable')
        with patch.dict('os.environ', {'ANYAICAM_STRIPE_PRICE_HYBRID_PREVIOUS':'price_synthetic_cloud:2499'}):
            self.assertEqual(self.verify(),'unavailable')

    def test_customer_isolation_stale_mapping_and_replacement(self):
        self.assign()
        with self.assertRaises(service.BindingConflict):
            service.verify_paid_binding(self.db,binding_id='binding-1',customer_id='customer-b',appliance_id='a',camera_id='c1')
        with self.assertRaises(service.BindingConflict): self.assign('c2','other',customer_id='customer-b')
        self.db.execute("UPDATE cameras SET device_key='new-key' WHERE id='c1'")
        self.assertEqual(self.verify(),'unavailable')
        with self.assertRaises(service.BindingConflict): self.assign()
        service.retire_binding(self.db,self.actor,binding_id='binding-1',customer_id='customer-a',appliance_id='a',camera_id='c1',version=1,reason='camera_replaced')
        self.assertEqual(self.assign('c1','replacement')['status'],'assigned')
        self.assertEqual(self.verify('replacement'),'verified')

    def test_duplicate_camera_mapping_and_audit_failure_fail_closed(self):
        self.db.execute("UPDATE cameras SET camera_number=1 WHERE id='c2'")
        with self.assertRaises(service.BindingConflict): self.assign()
        self.db.execute("UPDATE cameras SET camera_number=2 WHERE id='c2'"); self.db.commit()
        broken=SimpleNamespace(audit=lambda *args: (_ for _ in ()).throw(RuntimeError('audit failure')))
        with patch.dict(sys.modules,{'partner_db':broken}):
            with self.assertRaises(RuntimeError):
                with self.db: self.assign()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_camera_entitlements').fetchone()[0],0)

    def test_duplicate_device_key_across_appliances_and_unready_camera_are_denied(self):
        self.db.execute("INSERT INTO appliances VALUES('other','customer-a','cloud_gateway')")
        self.db.execute("INSERT INTO cameras VALUES('foreign-map','customer-a','other',7,'key-1','active')")
        with self.assertRaises(service.BindingConflict): self.assign()
        self.db.execute("DELETE FROM cameras WHERE id='foreign-map'")
        self.db.execute("UPDATE cameras SET status='pending_installation' WHERE id='c1'")
        with self.assertRaises(service.BindingConflict): self.assign()

    def test_retry_and_verifier_reject_tampered_binding_identity_or_mapping_audit(self):
        self.assign()
        for field,value in [('edition','local_gateway'),('billing_source','generic_plan'),('audit_reference','other')]:
            with self.subTest(field=field):
                original=self.db.execute('SELECT '+field+' FROM cloud_gateway_camera_entitlements WHERE id=?',('binding-1',)).fetchone()[0]
                self.db.execute('UPDATE cloud_gateway_camera_entitlements SET '+field+'=? WHERE id=?',(value,'binding-1'))
                with self.assertRaises(service.BindingConflict): self.assign()
                self.assertEqual(self.verify(),'unavailable')
                self.db.execute('UPDATE cloud_gateway_camera_entitlements SET '+field+'=? WHERE id=?',(original,'binding-1'))
        # Changing both the live mapping and the binding snapshot does not
        # rewrite the independent assignment audit.
        self.db.execute("UPDATE cameras SET device_key='changed' WHERE id='c1'")
        self.db.execute("UPDATE cloud_gateway_camera_entitlements SET device_key='changed' WHERE id='binding-1'")
        with self.assertRaises(service.BindingConflict): self.assign()
        self.assertEqual(self.verify(),'unavailable')

    def test_camera_already_assigned_to_another_subscription_is_conflict(self):
        self.assign()
        self.db.execute("UPDATE cloud_gateway_camera_entitlements SET billing_reference='old-sub' WHERE id='binding-1'")
        with self.assertRaises(service.BindingConflict): self.assign(request='new-binding')

    def test_verification_rejects_duplicate_current_bindings_even_when_quantity_allows_them(self):
        self.assign()
        self.db.execute('DROP INDEX one_current')
        self.db.execute('''INSERT INTO cloud_gateway_camera_entitlements
            SELECT 'duplicate',customer_id,appliance_id,camera_id,camera_number,device_key,edition,
                   billing_source,billing_reference,billing_version,status,version,audit_reference,
                   created_at,updated_at,ended_reason
            FROM cloud_gateway_camera_entitlements WHERE id='binding-1' ''')
        self.assertEqual(self.verify(),'unavailable')
        with self.assertRaises(service.BindingConflict): self.assign()
        self.db.execute("UPDATE cloud_gateway_camera_entitlements SET customer_id='customer-b' WHERE id='duplicate'")
        self.assertEqual(self.verify(),'unavailable')

    def test_hard_stop_denies_even_when_mocked_upstream_looks_paid(self):
        with patch.object(service,'CLOUD_GATEWAY_BILLING_SOURCE_READY',False):
            self.assertEqual(self.assign()['status'],'unavailable')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_camera_entitlements').fetchone()[0],0)

    def test_concurrent_assignments_do_not_exceed_one_seat_in_sqlite(self):
        self.subscription['quantity']=1
        local=threading.local()
        def audit(actor,action,entity_type,entity_id,details):
            local.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(action,entity_type,entity_id,json.dumps(details)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=audit)}):
            path=str(Path(directory)/'synthetic.db')
            db=sqlite3.connect(path); db.executescript(SCHEMA); db.close()
            barrier=threading.Barrier(2); results=[]; errors=[]
            def worker(camera,request):
                con=sqlite3.connect(path,timeout=5); con.row_factory=sqlite3.Row; local.db=con
                try:
                    barrier.wait(timeout=5); con.execute('BEGIN IMMEDIATE')
                    result=service.produce_paid_binding(con,source_reference='sub-test',source_version='v1',customer_id='customer-a',appliance_id='a',camera_id=camera,actor=self.actor,request_id=request)
                    con.commit(); results.append(result['status'])
                except service.BindingConflict:
                    con.rollback(); results.append('conflict')
                except Exception as error:
                    con.rollback(); errors.append(str(error))
                finally: con.close()
            threads=[threading.Thread(target=worker,args=('c1','b1')),threading.Thread(target=worker,args=('c2','b2'))]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors,[]); self.assertCountEqual(results,['assigned','conflict'])


if __name__ == '__main__': unittest.main()
