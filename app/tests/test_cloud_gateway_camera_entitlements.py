"""Synthetic binding retirement checks; no migrations or application startup."""
import ast
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app import cloud_gateway_camera_entitlements as service


class CameraBindingTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
          CREATE TABLE cloud_gateway_camera_entitlements(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,camera_id TEXT,
             camera_number INTEGER,device_key TEXT,edition TEXT,billing_source TEXT,billing_reference TEXT,billing_version TEXT,
             status TEXT,version INTEGER,audit_reference TEXT,created_at TEXT,updated_at TEXT,ended_reason TEXT);
          CREATE UNIQUE INDEX one_current ON cloud_gateway_camera_entitlements(camera_id) WHERE status IN ('pending','active','suspended');
          CREATE TABLE audit_logs(action TEXT,entity_type TEXT,entity_id TEXT,details_json TEXT);
          INSERT INTO cloud_gateway_camera_entitlements VALUES('binding','customer-a','appliance-a','camera-a',1,'key-a','cloud_gateway',
             'synthetic','synthetic-ref','1','active',1,'audit-id','now','now',NULL);
        ''')
        def audit(actor,action,entity_type,entity_id,details):
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?)',(action,entity_type,entity_id,json.dumps(details)))
        patcher=patch.dict(sys.modules,{'partner_db':SimpleNamespace(audit=audit)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.actor={'role':'administrator','email':'admin@example.test'}
        self.args=dict(binding_id='binding',customer_id='customer-a',appliance_id='appliance-a',camera_id='camera-a',version=1)

    def test_paid_producer_and_verifier_remain_unavailable_even_with_a_row(self):
        self.assertEqual(service.produce_paid_binding(self.db,source_reference='synthetic',source_version='1',customer_id='customer-a',appliance_id='appliance-a',camera_id='camera-a')['status'],'unavailable')
        self.assertEqual(service.verify_paid_binding(self.db,binding_id='binding',customer_id='customer-a',appliance_id='appliance-a',camera_id='camera-a')['status'],'unavailable')
        # Even a caller's "paid" assertion cannot substitute for the missing
        # server-verified edition-specific, camera-bound billing source.
        self.assertEqual(service.produce_paid_binding(self.db,source_reference={'paid':True},source_version='1',customer_id='customer-b',appliance_id='appliance-a',camera_id='camera-a')['status'],'unavailable')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_camera_entitlements').fetchone()[0],1)

    def test_replacement_retires_old_binding_without_creating_or_granting_new(self):
        self.assertEqual(service.retire_binding(self.db,self.actor,**self.args,reason='camera_replaced')['status'],'revoked')
        self.assertEqual(service.retire_binding(self.db,self.actor,**self.args,reason='camera_replaced')['status'],'already_retired')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cloud_gateway_camera_entitlements').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0],1)

    def test_foreign_customer_stale_version_and_conflicting_reason_fail(self):
        for changes in ({'customer_id':'customer-b'},{'appliance_id':'other'},{'camera_id':'other'},{'version':2}):
            with self.subTest(changes=changes), self.assertRaises(service.BindingConflict):
                service.retire_binding(self.db,self.actor,**{**self.args,**changes},reason='camera_removed')
        service.retire_binding(self.db,self.actor,**self.args,reason='camera_removed')
        with self.assertRaises(service.BindingConflict):
            service.retire_binding(self.db,self.actor,**self.args,reason='entitlement_revoked')

    def test_cancellation_and_transfer_retire_recorded_binding(self):
        for reason, expected in [('subscription_cancelled','cancelled'),('ownership_transferred','revoked')]:
            with self.subTest(reason=reason):
                self.db.execute("UPDATE cloud_gateway_camera_entitlements SET status='active',version=1,ended_reason=NULL")
                self.assertEqual(service.retire_binding(self.db,self.actor,**self.args,reason=reason)['status'],expected)
                self.assertEqual(service.verify_paid_binding(self.db,binding_id='binding',customer_id='customer-a',appliance_id='appliance-a',camera_id='camera-a')['status'],'unavailable')

    def test_duplicate_current_camera_and_audit_rollback(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO cloud_gateway_camera_entitlements SELECT 'duplicate',customer_id,appliance_id,camera_id,camera_number,device_key,edition,billing_source,billing_reference,billing_version,status,version,audit_reference,created_at,updated_at,ended_reason FROM cloud_gateway_camera_entitlements WHERE id='binding'")
        self.db.commit()
        broken=SimpleNamespace(audit=lambda *args: (_ for _ in ()).throw(RuntimeError('audit failure')))
        with patch.dict(sys.modules,{'partner_db':broken}):
            with self.assertRaises(RuntimeError):
                with self.db: service.retire_binding(self.db,self.actor,**self.args,reason='camera_removed')
        self.assertEqual(tuple(self.db.execute("SELECT status,version FROM cloud_gateway_camera_entitlements").fetchone()),('active',1))

    def test_migration_is_source_only_and_binds_required_identities(self):
        source=(Path(__file__).resolve().parents[1]/'db_migrations.py').read_text(encoding='utf-8')
        tree=ast.parse(source)
        matches=[n.elts[1].value for n in ast.walk(tree) if isinstance(n,ast.Tuple) and len(n.elts)==2
                 and isinstance(n.elts[0],ast.Constant) and n.elts[0].value=='20261108_cloud_gateway_camera_entitlements']
        self.assertEqual(len(matches),1)
        for field in ('customer_id','appliance_id','camera_id','billing_reference','edition','status','audit_reference'):
            self.assertIn(field,matches[0])
        self.assertNotIn('INSERT INTO',matches[0].upper())


if __name__ == '__main__': unittest.main()
