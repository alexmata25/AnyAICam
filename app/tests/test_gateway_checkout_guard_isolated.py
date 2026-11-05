"""Exercise Gateway checkout guard mutations against disposable SQLite."""
import ast
import sqlite3
import time
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace


class GatewayCheckoutGuardTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row; self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE checkout_pending(customer_id TEXT,purchase TEXT,token TEXT,price_id TEXT,quantity INTEGER,fingerprint TEXT,status TEXT,expires_at INTEGER,created_at TEXT,updated_at TEXT,session_id TEXT,checkout_url TEXT,PRIMARY KEY(customer_id,purchase))')
        path=Path(__file__).resolve().parents[1]/'checkout_guard.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        names={'_gateway_purchase','_claim','_retake_stalled','_take_over'}
        selected=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
        self.env={'time':time,'uuid':uuid,'connection':lambda:nullcontext(self.db),'_now_iso':lambda:'2000-01-01T00:00:00',
                  'CHECKOUT_SESSION_SECONDS':1800,'CLOCK_SKEW_SECONDS':0,'IN_FLIGHT_SECONDS':1}
        self.env['_pending']=lambda customer,purchase: (dict(r) if (r:=self.db.execute('SELECT * FROM checkout_pending WHERE customer_id=? AND purchase=?',(customer,purchase)).fetchone()) else None)
        exec(compile(ast.fix_missing_locations(ast.Module(body=selected,type_ignores=[])),str(path),'exec'),self.env)

    def insert(self,purchase,status='creating',session=None):
        self.db.execute('INSERT INTO checkout_pending VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('a',purchase,'old','price_old',1,'old',status,1,'2000-01-01T00:00:00','2000-01-01T00:00:00',session,None))

    def test_unknown_outcomes_do_not_generate_new_gateway_keys(self):
        for purchase in ('cloud_gateway','local_gateway_remote:box'):
            self.insert(purchase)
            self.assertIsNone(self.env['_claim']('a',purchase,'price_new',2,'new'))
            old=self.env['_pending']('a',purchase)
            self.assertIsNone(self.env['_retake_stalled'](old,'price_new',2,'new'))
            self.assertEqual(self.env['_pending']('a',purchase)['token'],'old')

    def test_generic_vms_expired_claim_preserves_existing_behavior(self):
        self.insert('vms_license')
        result=self.env['_claim']('a','vms_license','price_new',1,'new')
        self.assertIsNotNone(result); self.assertNotEqual(result['token'],'old')

    def test_paid_webhook_during_expiry_cannot_be_overwritten(self):
        self.insert('cloud_gateway','open','cs-old')
        old=self.env['_pending']('a','cloud_gateway')
        def expire(*args,**kwargs):
            self.db.execute("UPDATE checkout_pending SET status='paid' WHERE purchase='cloud_gateway'")
            return {}
        self.env['_main']=lambda:SimpleNamespace(stripe_api_post=expire)
        self.assertIsNone(self.env['_take_over'](old,'price_new',2,'new'))
        state=self.env['_pending']('a','cloud_gateway')
        self.assertEqual((state['status'],state['token'],state['session_id']),('paid','old','cs-old'))


if __name__ == '__main__': unittest.main()
