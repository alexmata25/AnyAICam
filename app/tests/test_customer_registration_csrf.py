import dataclasses, os, sqlite3, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND","sqlite"); os.environ.setdefault("ANYAICAM_CSRF_ENABLED","true")
from fastapi import HTTPException
from fastapi.testclient import TestClient
import cloud_security, main, partner_portal
from customer_registration import approve_registration, assign_registration, create_pending_registration, reject_registration
from database_backend import override_target
from partner_db import authenticate_detailed, initialize_database, password_hash
from token_security import sign

class CustomerRegistrationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True); root=Path(self.temp.name)
        self.db_path=root/"partner.db"; self.users_file=root/"must-not-exist.json"
        os.environ["ANYAICAM_PARTNER_DB"]=str(self.db_path)
        self.db_override=override_target(sqlite_path=self.db_path); self.db_override.__enter__(); initialize_database()
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','Partner A','approved','real','now')")
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-b','Partner B','approved','real','now')")
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES('master','partner-a','master@example.test','Master','administrator',?,1,'now')",(password_hash("master-password-123"),))
            db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by) VALUES('master-grant','master','administrator','global',NULL,'now','test')")
        self.settings_patch=patch.object(cloud_security,"settings",dataclasses.replace(cloud_security.settings,csrf_enabled=True,secure_cookies=False,allowed_origins=["http://testserver"]))
        self.users_patch=patch.object(main,"USERS_FILE",self.users_file)
        self.settings_patch.start(); self.users_patch.start()
        self.client=TestClient(main.app,base_url="http://testserver",follow_redirects=False)
        self.master={"master":True,"email":"master@example.test","partner_id":None}
    def tearDown(self):
        self.client.close(); self.users_patch.stop(); self.settings_patch.stop(); self.db_override.__exit__(None,None,None); self.temp.cleanup()
    def rows(self,sql,args=()):
        db=sqlite3.connect(self.db_path); db.row_factory=sqlite3.Row
        try:return [dict(x) for x in db.execute(sql,args).fetchall()]
        finally:db.close()
    def post(self,email="new@example.test",token=None):
        token=token or sign("csrf",28800); self.client.cookies.set("anyaicam_csrf",token)
        return self.client.post("/customer-register",data={"display_name":"New Customer","email":email,"password":"correct-horse-battery-staple","csrf_token":token})
    def pending(self,email="new@example.test"):
        create_pending_registration("New Customer",email,"correct-horse-battery-staple")
        return self.rows("SELECT * FROM customer_registration_requests WHERE email=?",(email,))[0]
    def test_get_sets_csrf_and_form_submits_token(self):
        r=self.client.get("/customer-register"); self.assertEqual(r.status_code,200); self.assertIn("anyaicam_csrf=",r.headers.get("set-cookie","")); self.assertIn('name="csrf_token"',r.text)
    def test_valid_registration_is_authoritative_pending_and_branded(self):
        r=self.post(); self.assertEqual(r.status_code,200); self.assertIn("request was submitted",r.text); self.assertNotEqual(r.headers.get("content-type"),"application/json")
        item=self.rows("SELECT * FROM customer_registration_requests")[0]; self.assertEqual(item["status"],"pending"); self.assertIsNone(item["partner_id"]); self.assertFalse(self.users_file.exists())
    def test_missing_and_invalid_csrf_are_403(self):
        missing=self.client.post("/customer-register",data={"display_name":"New Customer","email":"missing@example.test","password":"correct-horse-battery-staple"})
        self.client.cookies.set("anyaicam_csrf",sign("csrf",28800)); invalid=self.client.post("/customer-register",data={"display_name":"New Customer","email":"invalid@example.test","password":"correct-horse-battery-staple","csrf_token":"bad"})
        self.assertEqual(missing.status_code,403); self.assertEqual(invalid.status_code,403)
    def test_normalized_duplicate_and_existing_identity_rejected(self):
        self.assertEqual(self.post("Case@Example.Test").status_code,200); self.assertEqual(self.post("case@example.test").status_code,200)
        with sqlite3.connect(self.db_path) as db:db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES('u','partner-a','used@example.test','U','customer_owner',?,1,'now')",(password_hash("password-12345"),))
        self.assertEqual(self.post("used@example.test").status_code,409)
    def test_concurrent_duplicate_protection(self):
        outcomes=[]
        def run():
            try:outcomes.append(create_pending_registration("N","race@example.test","correct-horse-battery-staple"))
            except HTTPException as error:outcomes.append(error.status_code)
        threads=[threading.Thread(target=run) for _ in range(2)]
        [x.start() for x in threads]; [x.join() for x in threads]
        self.assertEqual(len(self.rows("SELECT * FROM customer_registration_requests WHERE email='race@example.test'")),1)
    def test_pending_password_compatible_and_login_blocked(self):
        item=self.pending(); self.assertTrue(item["password_hash"].startswith("pbkdf2_sha256$310000$"))
        user,reason=authenticate_detailed("new@example.test","correct-horse-battery-staple"); self.assertIsNone(user); self.assertEqual(reason,"invalid")
    def test_master_visibility_excludes_password_hash(self):
        self.pending()
        cookie=partner_portal._token("master@example.test","administrator","partner-a",None,None)
        r=self.client.get("/api/customer-registration-requests",cookies={partner_portal.SESSION_COOKIE:cookie})
        self.assertEqual(r.status_code,200); self.assertEqual(len(r.json()["requests"]),1); self.assertNotIn("password_hash",r.json()["requests"][0])
    def test_approval_api_requires_auth_and_uses_existing_csrf_model(self):
        item=self.pending(); self.assertEqual(self.client.get("/api/customer-registration-requests").status_code,401)
        token=sign("csrf",28800); self.client.cookies.set("anyaicam_csrf",token)
        self.client.cookies.set(partner_portal.SESSION_COOKIE,partner_portal._token("master@example.test","administrator","partner-a",None,None))
        response=self.client.post(f"/api/customer-registration-requests/{item['id']}/approve",json={"partner_id":"partner-a"},headers={"X-CSRF-Token":token})
        self.assertEqual(response.status_code,200); self.assertEqual(response.json()["status"],"complete")
    def test_tenant_isolation_and_cross_tenant_denial(self):
        item=self.pending(); actor={"master":False,"email":"owner@a.test","partner_id":"partner-a"}
        with self.assertRaises(HTTPException) as denied:approve_registration(item["id"],"partner-b",actor)
        self.assertEqual(denied.exception.status_code,403); self.assertEqual(self.rows("SELECT status FROM customer_registration_requests")[0]["status"],"pending")
    def test_master_assignment_makes_request_visible_only_to_assigned_partner(self):
        item=self.pending(); assign_registration(item["id"],"partner-a",self.master)
        partner_a={"master":False,"email":"owner@a.test","partner_id":"partner-a"}; partner_b={"master":False,"email":"owner@b.test","partner_id":"partner-b"}
        self.assertEqual(approve_registration(item["id"],None,partner_a)["status"],"complete")
        with self.assertRaises(HTTPException):approve_registration(item["id"],None,partner_b)
    def test_partner_cannot_assign_unassigned_request(self):
        item=self.pending()
        with self.assertRaises(HTTPException) as denied:assign_registration(item["id"],"partner-a",{"master":False,"email":"owner@a.test","partner_id":"partner-a"})
        self.assertEqual(denied.exception.status_code,403)
    def test_partner_request_listing_is_tenant_scoped(self):
        item=self.pending(); assign_registration(item["id"],"partner-a",self.master)
        cookie_a=partner_portal._token("owner@a.test","partner_owner","partner-a",None,None)
        cookie_b=partner_portal._token("owner@b.test","partner_owner","partner-b",None,None)
        visible=self.client.get("/api/customer-registration-requests",cookies={partner_portal.SESSION_COOKIE:cookie_a})
        hidden=self.client.get("/api/customer-registration-requests",cookies={partner_portal.SESSION_COOKIE:cookie_b})
        self.assertEqual(len(visible.json()["requests"]),1); self.assertEqual(hidden.json()["requests"],[])
    def test_approval_creates_linked_identity_and_one_grant(self):
        item=self.pending(); result=approve_registration(item["id"],"partner-a",self.master)
        customer=self.rows("SELECT * FROM customers")[0]; user=self.rows("SELECT * FROM partner_users WHERE email='new@example.test'")[0]; grants=self.rows("SELECT * FROM identity_grants WHERE user_id=?",(user["id"],))
        self.assertEqual(customer["partner_id"],"partner-a"); self.assertEqual(user["customer_id"],customer["id"]); self.assertEqual(user["approved"],1)
        self.assertEqual(len(grants),1); self.assertEqual((grants[0]["scope_type"],grants[0]["scope_id"]),("customer",customer["id"]))
        again=approve_registration(item["id"],"partner-a",self.master); self.assertEqual(again["user_id"],result["user_id"]); self.assertEqual(len(self.rows("SELECT * FROM identity_grants WHERE user_id=?",(user["id"],))),1)
    def test_approved_login_and_customer_portal_routing(self):
        item=self.pending(); approve_registration(item["id"],"partner-a",self.master); user,reason=authenticate_detailed("new@example.test","correct-horse-battery-staple")
        self.assertEqual(reason,"ok"); self.assertEqual(user["customer_id"],self.rows("SELECT id FROM customers")[0]["id"]); self.assertEqual(partner_portal.destination_for_role(user["role"]),"/customer-account")
    def test_rejection_blocks_login_and_creates_no_grant(self):
        item=self.pending(); reject_registration(item["id"],"Not approved",self.master)
        self.assertEqual(self.rows("SELECT status FROM customer_registration_requests")[0]["status"],"rejected"); self.assertEqual(self.rows("SELECT * FROM identity_grants WHERE role='customer_owner'"),[]); self.assertEqual(authenticate_detailed("new@example.test","correct-horse-battery-staple")[1],"invalid")
    def test_reject_after_approval_revokes_access(self):
        item=self.pending(); approve_registration(item["id"],"partner-a",self.master); reject_registration(item["id"],"Revoked",self.master)
        self.assertEqual(authenticate_detailed("new@example.test","correct-horse-battery-staple")[1],"revoked"); self.assertIsNotNone(self.rows("SELECT revoked_at FROM identity_grants WHERE role='customer_owner'")[0]["revoked_at"])
    def assert_approval_rollback(self,table,when=""):
        item=self.pending()
        with sqlite3.connect(self.db_path) as db:db.execute(f"CREATE TRIGGER fail_{table} BEFORE INSERT ON {table} {when} BEGIN SELECT RAISE(ABORT,'forced failure'); END")
        with self.assertRaises(Exception):approve_registration(item["id"],"partner-a",self.master)
        self.assertEqual(self.rows("SELECT status FROM customer_registration_requests")[0]["status"],"pending"); self.assertEqual(self.rows("SELECT * FROM customers"),[]); self.assertEqual(self.rows("SELECT * FROM partner_users WHERE role='customer_owner'"),[]); self.assertEqual(self.rows("SELECT * FROM identity_grants WHERE role='customer_owner'"),[])
    def test_customer_creation_failure_rolls_back(self):self.assert_approval_rollback("customers")
    def test_identity_creation_failure_rolls_back(self):self.assert_approval_rollback("partner_users")
    def test_grant_creation_failure_rolls_back(self):self.assert_approval_rollback("identity_grants")
    def test_approval_audit_failure_rolls_back(self):self.assert_approval_rollback("audit_logs","WHEN NEW.action='customer_registration.approved'")
    def test_registration_audit_failure_rolls_back(self):
        with sqlite3.connect(self.db_path) as db:db.execute("CREATE TRIGGER fail_request_audit BEFORE INSERT ON audit_logs WHEN NEW.action='customer_registration.requested' BEGIN SELECT RAISE(ABORT,'forced failure'); END")
        with self.assertRaises(Exception):create_pending_registration("N","audit@example.test","correct-horse-battery-staple")
        self.assertEqual(self.rows("SELECT * FROM customer_registration_requests"),[])

if __name__=="__main__":unittest.main()
