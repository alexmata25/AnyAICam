"""PostgreSQL-only lifecycle tests. Requires ANYAICAM_TEST_POSTGRES_URL."""
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_URL=os.environ.get("ANYAICAM_TEST_POSTGRES_URL","")
if TEST_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"]="postgresql"
    os.environ["ANYAICAM_DATABASE_URL"]=TEST_URL
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from customer_registration import approve_registration, assign_registration, create_pending_registration, reject_registration
from db_migrations import apply_migrations
from partner_db import authenticate_detailed, initialize_database, password_hash


@unittest.skipUnless(TEST_URL,"set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class PostgreSQLCustomerRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        initialize_database()
    def db(self):
        return psycopg.connect(TEST_URL,row_factory=dict_row)
    def setUp(self):
        with self.db() as db:
            for table in ("customers","partner_users","audit_logs"):
                db.execute(f"DROP TRIGGER IF EXISTS fail_{table}_trigger ON {table}")
                db.execute(f"DROP FUNCTION IF EXISTS fail_{table}()")
            db.execute("TRUNCATE customer_registration_requests,identity_grants,user_sessions,partner_users,customers,partners,audit_logs RESTART IDENTITY CASCADE")
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','Partner A','approved','real','now'),('partner-b','Partner B','approved','real','now')")
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES('master','partner-a','master@example.test','Master','administrator',%s,1,'now')",(password_hash("master-password-123"),))
            db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by) VALUES('master-grant','master','administrator','global',NULL,'now','test')")
        self.master={"master":True,"email":"master@example.test","partner_id":None}
    def rows(self,sql,args=()):
        with self.db() as db:return list(db.execute(sql,args).fetchall())
    def pending(self,email="new@example.test"):
        create_pending_registration("New Customer",email,"correct-horse-battery-staple")
        return self.rows("SELECT * FROM customer_registration_requests WHERE email=%s",(email,))[0]
    def test_schema_columns_index_and_unique_constraint(self):
        columns={x["column_name"] for x in self.rows("SELECT column_name FROM information_schema.columns WHERE table_name='customer_registration_requests'")}
        self.assertTrue({"id","display_name","email","password_hash","status","partner_id","customer_id","user_id","requested_at","decided_at","decided_by","rejection_reason"}<=columns)
        indexes={x["indexname"] for x in self.rows("SELECT indexname FROM pg_indexes WHERE tablename='customer_registration_requests'")}
        self.assertIn("idx_customer_registration_status_partner",indexes)
        create_pending_registration("A","case@example.test","correct-horse-battery-staple")
        self.assertEqual(create_pending_registration("B","CASE@example.test","correct-horse-battery-staple"),("pending",False))
        self.assertEqual(len(self.rows("SELECT * FROM customer_registration_requests WHERE email='case@example.test'")),1)
    def test_migration_rerun_preserves_existing_data(self):
        with self.db() as db:
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('preserved','partner-a','Preserved','preserved@example.test','active','real','now')")
            db.execute("DROP TABLE customer_registration_requests")
            db.execute("DELETE FROM schema_migrations WHERE version='20260907_customer_registration_requests'")
        # This now represents an already-populated Partner VMS database
        # immediately before this migration is applied.
        apply_migrations(); apply_migrations()
        self.assertEqual(self.rows("SELECT id FROM customers WHERE id='preserved'")[0]["id"],"preserved")
        self.assertEqual(self.rows("SELECT to_regclass('public.customer_registration_requests') AS table_name")[0]["table_name"],"customer_registration_requests")
    def test_pending_registration_and_authoritative_password(self):
        status,created=create_pending_registration("New Customer","NEW@Example.Test","correct-horse-battery-staple")
        item=self.rows("SELECT * FROM customer_registration_requests")[0]
        self.assertEqual((status,created,item["email"],item["status"],item["partner_id"]),("pending",True,"new@example.test","pending",None))
        self.assertTrue(item["password_hash"].startswith("pbkdf2_sha256$310000$"))
        self.assertEqual(authenticate_detailed("new@example.test","correct-horse-battery-staple")[1],"invalid")
    def test_assignment_and_tenant_isolation(self):
        item=self.pending(); assign_registration(item["id"],"partner-a",self.master)
        a={"master":False,"email":"a@example.test","partner_id":"partner-a"}; b={"master":False,"email":"b@example.test","partner_id":"partner-b"}
        self.assertEqual(len(self.rows("SELECT * FROM customer_registration_requests WHERE partner_id='partner-a'")),1)
        with self.assertRaises(HTTPException):approve_registration(item["id"],"partner-b",b)
        self.assertEqual(approve_registration(item["id"],None,a)["status"],"complete")
    def test_approval_idempotency_relationship_grant_and_login(self):
        item=self.pending(); first=approve_registration(item["id"],"partner-a",self.master); second=approve_registration(item["id"],"partner-a",self.master)
        self.assertEqual(first["user_id"],second["user_id"])
        self.assertEqual(len(self.rows("SELECT * FROM customers WHERE email='new@example.test'")),1)
        users=self.rows("SELECT * FROM partner_users WHERE email='new@example.test'"); self.assertEqual(len(users),1)
        grants=self.rows("SELECT * FROM identity_grants WHERE user_id=%s",(users[0]["id"],)); self.assertEqual(len(grants),1)
        self.assertEqual((grants[0]["scope_type"],grants[0]["scope_id"]),("customer",users[0]["customer_id"]))
        user,reason=authenticate_detailed("new@example.test","correct-horse-battery-staple")
        self.assertEqual(reason,"ok"); self.assertEqual((user["partner_id"],user["customer_id"]),("partner-a",users[0]["customer_id"]))
    def test_rejection_before_and_after_approval(self):
        item=self.pending(); reject_registration(item["id"],"denied",self.master)
        self.assertEqual(self.rows("SELECT status FROM customer_registration_requests")[0]["status"],"rejected")
        self.assertEqual(self.rows("SELECT * FROM identity_grants WHERE role='customer_owner'"),[])
        item=self.pending("second@example.test"); approve_registration(item["id"],"partner-a",self.master); reject_registration(item["id"],"revoked",self.master)
        self.assertEqual(authenticate_detailed("second@example.test","correct-horse-battery-staple")[1],"revoked")
        self.assertIsNotNone(self.rows("SELECT revoked_at FROM identity_grants WHERE role='customer_owner'")[0]["revoked_at"])
    def test_concurrent_duplicate_registration(self):
        outcomes=[]
        def run():
            try:outcomes.append(create_pending_registration("Race","race@example.test","correct-horse-battery-staple"))
            except HTTPException as error:outcomes.append(error.status_code)
        threads=[threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(len(outcomes),2); self.assertEqual(len(self.rows("SELECT * FROM customer_registration_requests WHERE email='race@example.test'")),1)
    def test_concurrent_approval_creates_one_identity_graph(self):
        item=self.pending(); outcomes=[]
        def run():
            try:outcomes.append(approve_registration(item["id"],"partner-a",self.master)["status"])
            except Exception as error:outcomes.append(type(error).__name__)
        threads=[threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(outcomes,["complete","complete"])
        self.assertEqual(len(self.rows("SELECT * FROM customers WHERE email='new@example.test'")),1,outcomes)
        users=self.rows("SELECT * FROM partner_users WHERE email='new@example.test'"); self.assertEqual(len(users),1)
        self.assertEqual(len(self.rows("SELECT * FROM identity_grants WHERE user_id=%s",(users[0]["id"],))),1)
    def install_failure_trigger(self,table,action):
        name=f"fail_{table}"
        with self.db() as db:
            db.execute(f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced failure'; END $$")
            db.execute(f"CREATE TRIGGER {name}_trigger BEFORE INSERT ON {table} FOR EACH ROW WHEN (NEW.action='{action}') EXECUTE FUNCTION {name}()" if table=="audit_logs" else f"CREATE TRIGGER {name}_trigger BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {name}()")
    def assert_rollback(self,table,action=None):
        item=self.pending()
        if table=="identity_grants":
            context=patch("customer_registration.create_grant",side_effect=RuntimeError("forced failure"))
        else:
            self.install_failure_trigger(table,action); context=patch("customer_registration._now",wraps=lambda:__import__("datetime").datetime.now().isoformat())
        with context:
            with self.assertRaises(Exception):approve_registration(item["id"],"partner-a",self.master)
        self.assertEqual(self.rows("SELECT status FROM customer_registration_requests")[0]["status"],"pending")
        self.assertEqual(self.rows("SELECT * FROM customers"),[]); self.assertEqual(self.rows("SELECT * FROM partner_users WHERE role='customer_owner'"),[]); self.assertEqual(self.rows("SELECT * FROM identity_grants WHERE role='customer_owner'"),[])
    def test_customer_failure_rolls_back(self):self.assert_rollback("customers")
    def test_identity_failure_rolls_back(self):self.assert_rollback("partner_users")
    def test_grant_failure_rolls_back(self):self.assert_rollback("identity_grants")
    def test_audit_failure_rolls_back(self):self.assert_rollback("audit_logs","customer_registration.approved")

if __name__=="__main__":unittest.main()
