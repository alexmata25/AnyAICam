import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "app"))

from customer_experience.pages import onboarding_wizard_page
from customer_experience.service import CustomerExperienceService


class CustomerExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "customer-experience.db"
        db = sqlite3.connect(self.path)
        try:
            db.executescript("""
                CREATE TABLE tenants(id TEXT PRIMARY KEY,name TEXT,status TEXT,tenant_type TEXT);
                CREATE TABLE cameras(id TEXT PRIMARY KEY,tenant_id TEXT,name TEXT,status TEXT,site_id TEXT,appliance_id TEXT,resolution TEXT);
                CREATE TABLE appliances(id TEXT PRIMARY KEY,tenant_id TEXT,cloud_id TEXT,state TEXT,online_status TEXT,last_check_in TEXT,cpu REAL,memory REAL,disk REAL,disk_capacity REAL,recording_used REAL,last_error TEXT,site_id TEXT);
                CREATE TABLE ai_events(id TEXT PRIMARY KEY,tenant_id TEXT,event_type TEXT,event_timestamp TEXT,camera_id TEXT,payload_json TEXT);
                CREATE TABLE notifications(id TEXT PRIMARY KEY,customer_id TEXT,title TEXT,severity TEXT,message TEXT,timestamp TEXT,camera_id TEXT,read_at TEXT);
                CREATE TABLE recording_assets(id TEXT PRIMARY KEY,tenant_id TEXT,size_bytes INTEGER);
                CREATE TABLE tenant_subscriptions(id TEXT PRIMARY KEY,tenant_id TEXT,plan_code TEXT,status TEXT,camera_limit INTEGER,renews_at TEXT,created_at TEXT);
                CREATE TABLE partner_users(id TEXT PRIMARY KEY,tenant_id TEXT,identity_domain TEXT,name TEXT,email TEXT,customer_role TEXT,account_status TEXT,approved INTEGER,created_at TEXT);
                CREATE TABLE sites(id TEXT PRIMARY KEY,tenant_id TEXT,name TEXT,address TEXT,site_type TEXT);
                CREATE TABLE camera_user_access(tenant_id TEXT,user_id TEXT,camera_id TEXT,can_view_live INTEGER,can_playback INTEGER,can_download INTEGER,can_manage INTEGER);
            """)
            for tenant in ("tenant-a", "tenant-b"):
                db.execute("INSERT INTO tenants VALUES(?,?,?,?)", (tenant, tenant, "active", "customer"))
                db.execute("INSERT INTO sites VALUES(?,?,?,?,?)", (f"site-{tenant}", tenant, "Main Site", "Address", "default"))
                db.execute("INSERT INTO appliances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"edge-{tenant}", tenant, f"CLOUD-{tenant}", "online", "online", "now", 10, 20, 30, 100, 12, None, f"site-{tenant}"))
                db.execute("INSERT INTO cameras VALUES(?,?,?,?,?,?,?)", (f"camera-{tenant}", tenant, f"Camera {tenant}", "online", f"site-{tenant}", f"edge-{tenant}", "4mp"))
                db.execute("INSERT INTO ai_events VALUES(?,?,?,?,?,?)", (f"event-{tenant}", tenant, "person", "now", f"camera-{tenant}", "{}"))
                db.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?)", (f"alert-{tenant}", tenant, "Person detected", "info", tenant, "now", f"camera-{tenant}", None))
                db.execute("INSERT INTO recording_assets VALUES(?,?,?)", (f"recording-{tenant}", tenant, 1024))
                db.execute("INSERT INTO tenant_subscriptions VALUES(?,?,?,?,?,?,?)", (f"plan-{tenant}", tenant, "starter", "active", 4, None, "now"))
                db.execute("INSERT INTO partner_users VALUES(?,?,?,?,?,?,?,?,?)", (f"user-{tenant}", tenant, "customer", tenant, f"{tenant}@example.test", "customer_admin", "active", 1, "now"))
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
            finally:
                db.close()

        self.service = CustomerExperienceService(connection)
        self.admin = {"id": "user-tenant-a", "tenant_id": "tenant-a", "identity_domain": "customer", "customer_role": "customer_admin", "enabled": True}

    def tearDown(self):
        self.temporary.cleanup()

    def test_dashboard_never_returns_another_tenants_business_data(self):
        result = self.service.dashboard(self.admin)
        self.assertEqual(result["tenant"]["id"], "tenant-a")
        self.assertEqual({item["id"] for item in result["cameras"]}, {"camera-tenant-a"})
        self.assertEqual({item["id"] for item in result["events"]}, {"event-tenant-a"})
        self.assertEqual({item["id"] for item in result["alerts"]}, {"alert-tenant-a"})
        self.assertEqual({item["id"] for item in result["appliances"]}, {"edge-tenant-a"})

    def test_customer_administration_lists_are_tenant_scoped(self):
        self.assertEqual({item["id"] for item in self.service.users(self.admin)}, {"user-tenant-a"})
        self.assertEqual({item["id"] for item in self.service.sites(self.admin)}, {"site-tenant-a"})
        self.assertEqual({item["id"] for item in self.service.cameras(self.admin)}, {"camera-tenant-a"})

    def test_viewer_cannot_open_customer_administration(self):
        viewer = {**self.admin, "customer_role": "viewer"}
        with self.assertRaises(PermissionError):
            self.service.users(viewer)

    def test_wizard_contains_every_required_customer_step(self):
        html, scripts = onboarding_wizard_page([])
        for label in ("Company information", "Primary administrator", "First site", "Edge appliance assignment", "Camera discovery", "Subscription selection", "Invitation email", "Completion summary"):
            self.assertIn(label, html)
        self.assertIn("/api/tenants/onboard", scripts)
        self.assertIn("result.next_steps.camera_discovery", scripts)


if __name__ == "__main__":
    unittest.main()
