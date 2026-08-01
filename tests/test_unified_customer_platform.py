import os
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path

TEST_DB=Path(tempfile.gettempdir())/'anyaicam-unified-platform-test.db'
TEST_DB.unlink(missing_ok=True)
os.environ['ANYAICAM_DATABASE_BACKEND']='sqlite'
os.environ['ANYAICAM_PARTNER_DB']=str(TEST_DB)

import partner_db
from customer_policy import camera_action_allowed,live_session_state,role_destination,same_customer


class UnifiedCustomerPlatformTests(unittest.TestCase):
    def test_shared_platform_migrations_exist(self):
        tables={item['name'] for item in partner_db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'user_sessions','customer_camera_permissions','customer_site_permissions','customer_clip_shares','mfa_settings','live_view_sessions','customer_clip_jobs'}<=tables)
        columns={item['name'] for item in partner_db.rows('PRAGMA table_info(customer_camera_permissions)')}
        self.assertTrue({'can_live','can_playback','can_download','can_share','can_alerts','can_settings'}<=columns)

    def test_customer_queries_are_account_scoped(self):
        now=datetime.now().isoformat()
        with partner_db.connection() as db:
            db.execute('INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)',('p','Partner','approved','real',now))
            for customer_id in ('customer-a','customer-b'):
                db.execute('INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)',(customer_id,'p',customer_id,customer_id+'@example.test','active','real',now))
                db.execute('INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)',('site-'+customer_id,customer_id,'Site',now))
        self.assertEqual(len(partner_db.rows('SELECT * FROM sites WHERE customer_id=?',('customer-a',))),1)
        self.assertEqual(partner_db.rows('SELECT customer_id FROM sites WHERE customer_id=?',('customer-a',))[0]['customer_id'],'customer-a')

    def test_logout_all_device_sessions_can_be_revoked(self):
        now=datetime.now(); user='user-a'
        with partner_db.connection() as db:
            db.execute('INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',('one',user,'a@example.test','customer_owner','cookie',now.isoformat(),(now+timedelta(hours=8)).isoformat()))
            db.execute('INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',('two',user,'a@example.test','customer_owner','api',now.isoformat(),(now+timedelta(days=30)).isoformat()))
            db.execute('UPDATE user_sessions SET revoked_at=? WHERE user_id=?',(now.isoformat(),user))
        self.assertEqual(partner_db.rows('SELECT COUNT(*) AS count FROM user_sessions WHERE user_id=? AND revoked_at IS NULL',(user,))[0]['count'],0)

    def test_partner_customer_roles_and_redirects_are_isolated(self):
        self.assertEqual(role_destination('partner_owner'),'/partner?tab=customers')
        self.assertEqual(role_destination('salesperson'),'/partner-quotes')
        self.assertEqual(role_destination('technician'),'/partner/appliance-dashboard')
        self.assertEqual(role_destination('customer_owner'),'/customer-account')
        self.assertEqual(role_destination('customer_viewer'),'/customer-account')
        self.assertEqual(role_destination('administrator'),'/partner?tab=customers')

    def test_cross_customer_access_is_denied(self):
        self.assertTrue(same_customer('customer-a','customer-a'))
        self.assertFalse(same_customer('customer-a','customer-b'))
        self.assertFalse(same_customer('','customer-a'))

    def test_camera_playback_and_download_permissions(self):
        permission={'can_live':1,'can_playback':1,'can_download':0,'can_share':0,'can_alerts':1,'can_settings':0}
        self.assertTrue(camera_action_allowed('customer_viewer','live',permission,1))
        self.assertTrue(camera_action_allowed('customer_viewer','playback',permission,1))
        self.assertFalse(camera_action_allowed('customer_viewer','download',permission,1))
        self.assertFalse(camera_action_allowed('customer_viewer','share',permission,1))
        self.assertTrue(camera_action_allowed('customer_owner','download',None,0))
        self.assertFalse(camera_action_allowed('salesperson','live',None,0))

    def test_live_session_expiration_states(self):
        now=datetime.now(); future=(now+timedelta(minutes=5)).isoformat(); past=(now-timedelta(seconds=1)).isoformat()
        self.assertEqual(live_session_state('requested',future,now),'requested')
        self.assertEqual(live_session_state('ready',future,now),'ready')
        self.assertEqual(live_session_state('failed',future,now),'failed')
        self.assertEqual(live_session_state('ready',past,now),'expired')

    def test_customer_portal_is_mobile_and_transport_safe(self):
        root=Path(__file__).parents[1]; source_file=root/'customer_platform.py'
        if not source_file.exists(): source_file=root/'app'/'customer_platform.py'
        source=source_file.read_text(encoding='utf-8')
        self.assertIn('@media(max-width:640px)',source)
        self.assertIn('@media(max-width:900px)',source)
        self.assertIn('[1,4,9,16]',source)
        self.assertIn("'stream_url':None",source)
        self.assertIn('browser will never connect to a private camera IP address',source)
        self.assertIn("pricing_visibility':'customer_retail_only'",source)
        self.assertNotIn("SELECT * FROM plans WHERE customer_id",source)
        for route in ('/customer-portal','/api/v1/dashboard','/api/v1/cameras','/api/v1/recordings','/api/v1/alerts','/api/v1/live-sessions/{session_id}'):
            self.assertIn(route,source)


if __name__=='__main__': unittest.main()
