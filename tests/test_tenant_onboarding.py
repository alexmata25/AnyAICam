import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from tenancy.service import TenantOnboardingService


SCHEMA = """
CREATE TABLE partners(id TEXT PRIMARY KEY,name TEXT,approval_status TEXT,source TEXT,created_at TEXT);
CREATE TABLE tenants(id TEXT PRIMARY KEY,slug TEXT UNIQUE,name TEXT,status TEXT,tenant_type TEXT,created_at TEXT,created_by TEXT);
CREATE TABLE customers(id TEXT PRIMARY KEY,partner_id TEXT,name TEXT,company TEXT,email TEXT,phone TEXT,status TEXT,source TEXT,created_at TEXT,created_by TEXT,tenant_id TEXT);
CREATE TABLE sites(id TEXT PRIMARY KEY,customer_id TEXT,name TEXT,address TEXT,site_type TEXT,created_at TEXT,tenant_id TEXT);
CREATE TABLE partner_users(id TEXT PRIMARY KEY,partner_id TEXT,email TEXT UNIQUE,name TEXT,role TEXT,password_hash TEXT,approved INTEGER,customer_id TEXT,created_at TEXT,account_status TEXT,must_change_password INTEGER,tenant_id TEXT,identity_domain TEXT,platform_role TEXT,customer_role TEXT);
CREATE TABLE tenant_memberships(tenant_id TEXT,user_id TEXT,role TEXT,status TEXT,created_at TEXT,created_by TEXT,PRIMARY KEY(tenant_id,user_id));
CREATE TABLE tenant_subscriptions(id TEXT PRIMARY KEY,tenant_id TEXT,plan_code TEXT,status TEXT,camera_limit INTEGER,starts_at TEXT,renews_at TEXT,created_at TEXT,created_by TEXT);
CREATE TABLE tenant_licenses(id TEXT PRIMARY KEY,tenant_id TEXT,subscription_id TEXT,status TEXT,camera_limit INTEGER,created_at TEXT);
CREATE TABLE appliances(id TEXT PRIMARY KEY,customer_id TEXT,site_id TEXT,cloud_id TEXT,appliance_type TEXT,online_status TEXT,camera_capacity INTEGER,created_at TEXT,tenant_id TEXT,activation_status TEXT,state TEXT);
CREATE TABLE invitations(id TEXT PRIMARY KEY,email TEXT,role TEXT,customer_id TEXT,status TEXT,temporary_password_hash TEXT,email_preview TEXT,expires_at TEXT,created_at TEXT,created_by TEXT,tenant_id TEXT);
CREATE TABLE cameras(id TEXT PRIMARY KEY,customer_id TEXT,site_id TEXT,name TEXT,created_at TEXT,tenant_id TEXT);
CREATE TABLE camera_user_access(tenant_id TEXT,user_id TEXT,camera_id TEXT,can_view_live INTEGER,can_playback INTEGER,can_download INTEGER,can_manage INTEGER,granted_by TEXT,created_at TEXT,updated_at TEXT,PRIMARY KEY(tenant_id,user_id,camera_id));
"""


class TenantOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "tenants.db"
        db = sqlite3.connect(self.path)
        try:
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()

        @contextmanager
        def connection():
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        self.connection = connection
        self.service = TenantOnboardingService(connection, password_hasher=lambda value: f"hashed:{value}")
        self.actor = {"id": "platform-owner", "enabled": True, "identity_domain": "platform", "platform_role": "owner"}

    def tearDown(self):
        self.temporary.cleanup()

    def test_onboarding_creates_complete_tenant_graph_atomically(self):
        result = self.service.onboard(self.actor, {
            "tenant_name": "Northwind Security", "admin_name": "Nora Admin",
            "admin_email": "nora@example.com", "site_name": "Warehouse",
            "plan_code": "professional", "camera_limit": 12,
        })
        tenant_id = result["tenant"]["id"]
        with self.connection() as db:
            self.assertEqual(db.execute("SELECT tenant_id FROM customers").fetchone()["tenant_id"], tenant_id)
            self.assertEqual(db.execute("SELECT customer_role FROM partner_users").fetchone()["customer_role"], "customer_admin")
            self.assertEqual(db.execute("SELECT tenant_id FROM sites").fetchone()["tenant_id"], tenant_id)
            self.assertEqual(db.execute("SELECT camera_limit FROM tenant_subscriptions").fetchone()["camera_limit"], 12)
            self.assertEqual(db.execute("SELECT tenant_id FROM appliances").fetchone()["tenant_id"], tenant_id)

    def test_only_owner_or_sales_can_create_tenant(self):
        customer = {"enabled": True, "identity_domain": "customer", "customer_role": "customer_admin", "tenant_id": "tenant-a"}
        with self.assertRaises(PermissionError):
            self.service.onboard(customer, {"tenant_name": "Blocked", "admin_name": "User", "admin_email": "user@example.com"})

    def test_camera_sharing_rejects_cross_tenant_user(self):
        result = self.service.onboard(self.actor, {"tenant_name": "Tenant A", "admin_name": "Admin A", "admin_email": "a@example.com"})
        tenant_id = result["tenant"]["id"]
        with self.connection() as db:
            db.execute("INSERT INTO cameras(id,customer_id,site_id,name,created_at,tenant_id) VALUES('cam-a',?,?, 'Camera A','now',?)", (tenant_id, result["default_site"]["id"], tenant_id))
            db.execute("INSERT INTO partner_users(id,email,name,role,password_hash,approved,created_at,account_status,must_change_password,tenant_id,identity_domain,customer_role) VALUES('other','other@example.com','Other','viewer','x',1,'now','active',0,'tenant-b','customer','viewer')")
        admin = {"id": result["primary_administrator"]["id"], "enabled": True, "identity_domain": "customer", "customer_role": "customer_admin", "tenant_id": tenant_id}
        with self.assertRaises(ValueError):
            self.service.grant_camera_access(admin, tenant_id, "other", "cam-a", {"can_view_live": True})


if __name__ == "__main__":
    unittest.main()
