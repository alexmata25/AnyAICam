import json
import os
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path

TEST_DB=Path(tempfile.gettempdir())/'anyaicam-pwa-mobile-test.db'
TEST_DB.unlink(missing_ok=True)
os.environ.setdefault('ANYAICAM_DATABASE_BACKEND','sqlite')

import database_backend

with database_backend.override_target(sqlite_path=TEST_DB):
    import partner_db
from customer_policy import notification_scope_allowed
from mobile_security import issue_mobile_tokens,register_device,revoke_device,rotate_refresh_token
from notification_engine import fanout_appliance_event


class PwaMobileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # See test_partner_website.py: scope this class's database access to
        # its own TEST_DB rather than relying on whichever test module's
        # import ran last to leave ANYAICAM_PARTNER_DB pointed here.
        # addClassCleanup is registered immediately after __enter__() so the
        # override is always torn down, even if a later line in setUpClass
        # (e.g. one of the seed inserts below) raises.
        cls._target=database_backend.override_target(sqlite_path=TEST_DB)
        cls._target.__enter__()
        cls.addClassCleanup(cls._target.__exit__,None,None,None)

        now=datetime.now().isoformat()
        with partner_db.connection() as db:
            db.execute('INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)',('partner','Partner','approved','real',now))
            for customer in ('customer-a','customer-b'):
                db.execute('INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)',(customer,'partner',customer,customer+'@example.test','active','real',now))
                db.execute('INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)',('site-'+customer,customer,'Site',now))
            db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) VALUES(?,?,?,?,?,?,1,?,?,?)',('user-a','partner','owner@example.test','Owner','customer_owner',partner_db.password_hash('Password123!'),'customer-a',now,'active'))
            db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) VALUES(?,?,?,?,?,?,1,?,?,?)',('viewer-a','partner','viewer@example.test','Viewer','customer_viewer',partner_db.password_hash('Password123!'),'customer-a',now,'active'))
            db.execute('INSERT INTO cameras(id,customer_id,site_id,name,status,created_at) VALUES(?,?,?,?,?,?)',('other-camera','customer-a','site-customer-a','Other camera','online',now))
            db.execute('INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_download,can_share,can_alerts,can_settings) VALUES(?,?,?,?,?,?,?,?)',('viewer-a','other-camera',1,1,0,0,1,0))

    def test_pwa_manifest_and_routes(self):
        root=Path(__file__).parents[1]; root=root/'app' if (root/'app').exists() else root; manifest=json.loads((root/'pwa'/'manifest.webmanifest').read_text(encoding='utf-8')); routes=(root/'pwa_routes.py').read_text(encoding='utf-8')
        self.assertEqual(manifest['display'],'standalone'); self.assertEqual(manifest['start_url'].split('?')[0],'/customer-portal')
        self.assertIn('/manifest.webmanifest',routes); self.assertIn('/service-worker.js',routes); self.assertIn('/offline',routes)
        self.assertTrue((root/'pwa'/'service-worker.js').exists()); self.assertTrue((root/'pwa'/'offline.html').exists())

    def test_mobile_authentication_and_refresh_rotation(self):
        user=partner_db.row('SELECT * FROM partner_users WHERE id=?',('user-a',)); device=register_device(user,'phone-rotation','phone','ios','1.0'); original=issue_mobile_tokens(user,device)
        access=partner_db.row('SELECT * FROM user_sessions WHERE id=?',(original['session_id'],)); duration=datetime.fromisoformat(access['expires_at'])-datetime.fromisoformat(access['created_at']); self.assertLessEqual(duration,timedelta(minutes=15,seconds=1))
        rotated,status=rotate_refresh_token(original['refresh_token']); self.assertEqual(status,'ok'); self.assertNotEqual(rotated['refresh_token'],original['refresh_token']); self.assertEqual(rotated['family_id'],original['family_id'])
        rejected,status=rotate_refresh_token(original['refresh_token']); self.assertIsNone(rejected); self.assertEqual(status,'reuse_detected')
        self.assertIsNotNone(partner_db.row('SELECT revoked_at FROM mobile_devices WHERE id=?',(device,))['revoked_at'])

    def test_device_revocation(self):
        user=partner_db.row('SELECT * FROM partner_users WHERE id=?',('user-a',)); device=register_device(user,'android-revoke','phone','android','1.0'); tokens=issue_mobile_tokens(user,device); revoke_device(device,user['id'])
        self.assertIsNotNone(partner_db.row('SELECT revoked_at FROM mobile_devices WHERE id=?',(device,))['revoked_at'])
        self.assertIsNotNone(partner_db.row('SELECT revoked_at FROM user_sessions WHERE id=?',(tokens['session_id'],))['revoked_at'])

    def test_notification_customer_site_and_camera_isolation(self):
        self.assertTrue(notification_scope_allowed('customer-a','customer-a','site-a',{'site-a'},'camera-a',{'camera-a'}))
        self.assertFalse(notification_scope_allowed('customer-a','customer-b','site-b',{'site-a'},'camera-b',{'camera-a'}))
        self.assertFalse(notification_scope_allowed('customer-a','customer-a','site-b',{'site-a'},'camera-a',{'camera-a'}))
        self.assertFalse(notification_scope_allowed('customer-a','customer-a','site-a',{'site-a'},'camera-b',{'camera-a'}))

    def test_notification_fanout_stays_in_customer_and_camera_scope(self):
        created=fanout_appliance_event({'customer_id':'customer-a','site_id':'site-customer-a'},{'id':'event-one','event_type':'motion','camera_id':'camera-a','timestamp':datetime.now().isoformat()})
        self.assertEqual(created,1)
        recipients={item['user_id'] for item in partner_db.rows('SELECT user_id FROM notifications WHERE event_id=?',('event-one',))}
        self.assertEqual(recipients,{'user-a'})
        self.assertEqual(len(partner_db.rows('SELECT id FROM notifications WHERE customer_id=?',('customer-b',))),0)

    def test_notification_and_device_migrations(self):
        tables={item['name'] for item in partner_db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'mobile_devices','mobile_refresh_tokens','notification_preferences','notifications','notification_deliveries'}<=tables)

    def test_mobile_responsive_routes_and_navigation(self):
        root=Path(__file__).parents[1]; root=root/'app' if (root/'app').exists() else root; source=(root/'customer_platform.py').read_text(encoding='utf-8')
        self.assertIn("('cameras','▣','Cameras'),('alerts','♢','Alerts'),('dashboard','⌁','Dashboard'),('sites','⌂','Sites'),('account','⚙','Account')",source)
        self.assertIn('@media(max-width:640px)',source); self.assertIn('@media(max-width:900px)',source); self.assertIn("navigator.serviceWorker.register('/service-worker.js')",source)
        for route in ('/api/v1/auth/token','/api/v1/cameras','/api/v1/sites','/api/v1/alerts','/api/v1/events','/api/v1/recordings'):
            self.assertIn(route,source)


if __name__=='__main__': unittest.main()
