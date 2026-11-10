"""Extracted authenticated camera-removal route against synthetic SQLite."""
import ast
import json
import sqlite3
import sys
import textwrap
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app import cloud_gateway_camera_entitlements as bindings


class HttpError(Exception):
    def __init__(self,status_code,detail): self.status_code,self.detail=status_code,detail


class CameraRemovalTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
          CREATE TABLE cameras(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,camera_number INTEGER,former_camera_number INTEGER,device_key TEXT,status TEXT,name TEXT,site_id TEXT,local_recording_mode TEXT);
          CREATE TABLE camera_credentials(camera_id TEXT);
          CREATE TABLE camera_analytics_entitlements(camera_id TEXT,analytic_key TEXT,status TEXT);
          CREATE TABLE recordings(id TEXT,camera_id TEXT);
          CREATE TABLE audit_logs(actor_email TEXT,actor_role TEXT,action TEXT,entity_type TEXT,entity_id TEXT,details_json TEXT);
          CREATE TABLE cloud_gateway_camera_entitlements(id TEXT PRIMARY KEY,customer_id TEXT,appliance_id TEXT,camera_id TEXT,
             camera_number INTEGER,device_key TEXT,edition TEXT,billing_source TEXT,billing_reference TEXT,billing_version TEXT,
             status TEXT,version INTEGER,audit_reference TEXT,created_at TEXT,updated_at TEXT,ended_reason TEXT);
          INSERT INTO cameras VALUES('camera-a','customer-a','appliance-a',1,NULL,'device-a','active','Camera','site-a','event');
          INSERT INTO camera_credentials VALUES('camera-a');
          INSERT INTO recordings VALUES('recording-a','camera-a');
          INSERT INTO cloud_gateway_camera_entitlements VALUES('binding','customer-a','appliance-a','camera-a',1,'device-a',
              'cloud_gateway','synthetic','synthetic-ref','1','active',1,'audit-id','now','now',NULL);
        ''')
        self.identity={'role':'customer_owner','email':'owner@example.test','customer_id':'customer-a'}
        self.fail_audit=False
        @contextmanager
        def transaction():
            with self.db: yield self.db
        def row(sql,args):
            result=self.db.execute(sql,args).fetchone()
            return dict(result) if result else None
        def audit(actor,action,entity_type,entity_id,details=None):
            if self.fail_audit: raise RuntimeError('synthetic audit failure')
            self.db.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)',
                            (actor['email'],actor['role'],action,entity_type,entity_id,json.dumps(details)))
        def assign(db,camera_id,number,*,appliance_id,customer_id):
            db.execute('UPDATE cameras SET camera_number=? WHERE id=? AND appliance_id=? AND customer_id=?',
                       (number,camera_id,appliance_id,customer_id))
        self.modules={'partner_db':SimpleNamespace(transaction=transaction,audit=audit),
            'database_backend':SimpleNamespace(backend=lambda:'sqlite',lock_customer_camera_capacity=lambda db,customer:True),
            'customer_analytics_panel':SimpleNamespace(remove_entitlement=lambda *args,**kwargs:None),
            'camera_mapping':SimpleNamespace(assign_camera_number=assign),
            'customer_entitlements':SimpleNamespace(camera_capacity_status=lambda customer,can_manage: {'licensed':4}),
            'cloud_gateway_camera_entitlements':bindings}
        module_patch=patch.dict(sys.modules,self.modules); module_patch.start(); self.addCleanup(module_patch.stop)
        source=(Path(__file__).resolve().parents[1]/'partner_workspace.py').read_text(encoding='utf-8')
        start=source.index('    def remove_customer_camera(')
        end=source.index("    @app.put('/api/customer/cameras')",start)
        ns={'Request':object,'HTTPException':HttpError,'cloud_account_only':lambda:None,
            'customer_owner':lambda request:self.identity,'require_permission':lambda identity,permission:None,
            'row':row,'audit':audit,'datetime':datetime}
        exec(compile(textwrap.dedent(source[start:end]),'remove_customer_camera','exec',
                     flags=__import__('__future__').annotations.compiler_flag),ns)
        self.remove=ns['remove_customer_camera']

    def test_removal_retires_binding_and_preserves_recording(self):
        result=self.remove(object(),'camera-a')
        self.assertEqual(result['camera_capacity'],{'licensed':4})
        self.assertEqual(tuple(self.db.execute('SELECT status,camera_number,former_camera_number FROM cameras').fetchone()),('removed',None,1))
        self.assertEqual(tuple(self.db.execute('SELECT status,version,ended_reason FROM cloud_gateway_camera_entitlements').fetchone()),('revoked',2,'camera_removed'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0],2)
        self.assertEqual(bindings.retire_for_camera_removal(self.db,self.identity,customer_id='customer-a',appliance_id='appliance-a',camera_id='camera-a'),0)

    def test_foreign_camera_and_conflicting_binding_fail_without_removal(self):
        self.identity['customer_id']='customer-b'
        with self.assertRaises(HttpError) as error: self.remove(object(),'camera-a')
        self.assertEqual(error.exception.status_code,404)
        self.identity['customer_id']='customer-a'
        self.db.execute("UPDATE cloud_gateway_camera_entitlements SET customer_id='customer-b'")
        with self.assertRaises(bindings.BindingConflict): self.remove(object(),'camera-a')
        self.assertEqual(self.db.execute('SELECT status FROM cameras').fetchone()[0],'active')

    def test_retirement_audit_failure_rolls_back_removal(self):
        self.db.commit(); self.fail_audit=True
        with self.assertRaises(RuntimeError): self.remove(object(),'camera-a')
        self.assertEqual(self.db.execute('SELECT status FROM cameras').fetchone()[0],'active')
        self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_camera_entitlements').fetchone()[0],'active')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM camera_credentials').fetchone()[0],1)

    def test_legacy_schema_without_binding_table_still_allows_removal(self):
        self.db.execute('DROP TABLE cloud_gateway_camera_entitlements')
        self.remove(object(),'camera-a')
        self.assertEqual(self.db.execute('SELECT status FROM cameras').fetchone()[0],'removed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0],1)

    def test_authenticated_reprovision_branch_retires_old_appliance_binding(self):
        source=(Path(__file__).resolve().parents[1]/'appliance_cloud.py').read_text(encoding='utf-8')
        tree=ast.parse(source)
        route=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='appliance_submit_provisioning')
        branch=next(n for n in ast.walk(route) if isinstance(n,ast.If) and isinstance(n.test,ast.Name)
                    and n.test.id=='existing' and 'retire_for_camera_transition' in ast.get_source_segment(source,n))
        # Exercise the actual existing-camera branch in the enclosing transaction.
        ns={'existing':{'id':'camera-a','appliance_id':'appliance-a'},'db':self.db,
            'appliance':{'id':'appliance-b','cloud_id':'AIC-TEST'},
            'job':{'customer_id':'customer-a','camera_name':'Camera','site_id':'site-b'}}
        self.db.commit()
        with self.db:
            exec(compile(ast.fix_missing_locations(ast.Module(body=branch.body,type_ignores=[])),
                         'camera_reprovision_branch','exec'),ns)
        self.assertEqual(self.db.execute('SELECT appliance_id FROM cameras').fetchone()[0],'appliance-b')
        self.assertEqual(tuple(self.db.execute('SELECT status,ended_reason FROM cloud_gateway_camera_entitlements').fetchone()),('revoked','appliance_reassigned'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0],1)

    def test_reprovision_audit_failure_leaves_camera_and_binding_unchanged(self):
        source=(Path(__file__).resolve().parents[1]/'appliance_cloud.py').read_text(encoding='utf-8')
        route=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.FunctionDef) and n.name=='appliance_submit_provisioning')
        branch=next(n for n in ast.walk(route) if isinstance(n,ast.If) and isinstance(n.test,ast.Name)
                    and n.test.id=='existing' and 'retire_for_camera_transition' in ast.get_source_segment(source,n))
        ns={'existing':{'id':'camera-a','appliance_id':'appliance-a'},'db':self.db,
            'appliance':{'id':'appliance-b','cloud_id':'AIC-TEST'},
            'job':{'customer_id':'customer-a','camera_name':'Camera','site_id':'site-b'}}
        self.db.commit(); self.fail_audit=True
        with self.assertRaises(RuntimeError):
            with self.db:
                exec(compile(ast.fix_missing_locations(ast.Module(body=branch.body,type_ignores=[])),
                             'camera_reprovision_branch','exec'),ns)
        self.assertEqual(self.db.execute('SELECT appliance_id FROM cameras').fetchone()[0],'appliance-a')
        self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_camera_entitlements').fetchone()[0],'active')

    def test_rediscovery_on_same_appliance_does_not_retire_binding(self):
        source=(Path(__file__).resolve().parents[1]/'appliance_cloud.py').read_text(encoding='utf-8')
        route=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.FunctionDef) and n.name=='appliance_submit_provisioning')
        branch=next(n for n in ast.walk(route) if isinstance(n,ast.If) and isinstance(n.test,ast.Name)
                    and n.test.id=='existing' and 'retire_for_camera_transition' in ast.get_source_segment(source,n))
        ns={'existing':{'id':'camera-a','appliance_id':'appliance-a'},'db':self.db,
            'appliance':{'id':'appliance-a','cloud_id':'AIC-TEST'},
            'job':{'customer_id':'customer-a','camera_name':'Camera','site_id':'site-a'}}
        with self.db:
            exec(compile(ast.fix_missing_locations(ast.Module(body=branch.body,type_ignores=[])),
                         'camera_reprovision_branch','exec'),ns)
        self.assertEqual(self.db.execute('SELECT status FROM cloud_gateway_camera_entitlements').fetchone()[0],'active')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0],0)


if __name__ == '__main__': unittest.main()
