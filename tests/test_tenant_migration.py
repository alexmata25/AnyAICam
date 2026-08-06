import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


BASE_SCHEMA = """
CREATE TABLE customers(id TEXT PRIMARY KEY,name TEXT,created_at TEXT);
CREATE TABLE partner_users(id TEXT PRIMARY KEY,role TEXT,customer_id TEXT);
CREATE TABLE sites(id TEXT PRIMARY KEY,customer_id TEXT);
CREATE TABLE cameras(id TEXT PRIMARY KEY,customer_id TEXT);
CREATE TABLE appliances(id TEXT PRIMARY KEY,customer_id TEXT);
CREATE TABLE plans(id TEXT PRIMARY KEY,customer_id TEXT);
CREATE TABLE analytics_subscriptions(id TEXT PRIMARY KEY,customer_id TEXT);
CREATE TABLE invitations(id TEXT PRIMARY KEY,customer_id TEXT);
CREATE TABLE appliance_events(appliance_id TEXT,event_id TEXT,PRIMARY KEY(appliance_id,event_id));
CREATE TABLE user_sessions(id TEXT PRIMARY KEY,user_id TEXT);
CREATE TABLE customer_camera_permissions(user_id TEXT,camera_id TEXT,can_live INTEGER,can_playback INTEGER,can_download INTEGER,can_settings INTEGER);
"""


class TenantMigrationTests(unittest.TestCase):
    def test_legacy_customer_graph_is_backfilled_to_one_tenant(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "migration.db"
            db = sqlite3.connect(path)
            try:
                db.executescript(BASE_SCHEMA)
                db.execute("INSERT INTO customers VALUES('customer-a','Customer A','2026-08-01')")
                db.execute("INSERT INTO partner_users VALUES('viewer-a','customer_viewer','customer-a')")
                db.execute("INSERT INTO sites VALUES('site-a','customer-a')")
                db.execute("INSERT INTO cameras VALUES('camera-a','customer-a')")
                db.execute("INSERT INTO appliances VALUES('edge-a','customer-a')")
                db.execute("INSERT INTO customer_camera_permissions VALUES('viewer-a','camera-a',1,1,0,0)")
                db.commit()
            finally:
                db.close()

            prior_backend = os.environ.get("ANYAICAM_DATABASE_BACKEND")
            prior_path = os.environ.get("ANYAICAM_PARTNER_DB")
            os.environ["ANYAICAM_DATABASE_BACKEND"] = "sqlite"
            os.environ["ANYAICAM_PARTNER_DB"] = str(path)
            try:
                from tenancy.migrations import apply_tenant_migration
                apply_tenant_migration()
            finally:
                if prior_backend is None:
                    os.environ.pop("ANYAICAM_DATABASE_BACKEND", None)
                else:
                    os.environ["ANYAICAM_DATABASE_BACKEND"] = prior_backend
                if prior_path is None:
                    os.environ.pop("ANYAICAM_PARTNER_DB", None)
                else:
                    os.environ["ANYAICAM_PARTNER_DB"] = prior_path

            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            try:
                tenant_id = db.execute("SELECT tenant_id FROM customers WHERE id='customer-a'").fetchone()["tenant_id"]
                self.assertTrue(tenant_id)
                for table in ("sites", "cameras", "appliances"):
                    self.assertEqual(db.execute(f"SELECT tenant_id FROM {table}").fetchone()["tenant_id"], tenant_id)
                user = db.execute("SELECT identity_domain,customer_role,tenant_id FROM partner_users").fetchone()
                self.assertEqual((user["identity_domain"], user["customer_role"], user["tenant_id"]), ("customer", "viewer", tenant_id))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM tenant_memberships WHERE user_id='viewer-a'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT can_view_live FROM camera_user_access").fetchone()[0], 1)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
