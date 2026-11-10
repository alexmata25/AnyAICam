"""Gateway Basic Motion reaches the customer inbox locally, with no provider calls."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.tests.test_gateway_licensing import load_functions
import gateway_notifications


class GatewayLocalNotificationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
        CREATE TABLE cameras(id,customer_id,site_id,appliance_id,camera_number,status,name);
        INSERT INTO cameras VALUES('c1','customer1','site1','a1',1,'active','Test');
        CREATE TABLE partner_users(id,email,role,customer_id,approved,account_status,camera_access_mode);
        INSERT INTO partner_users VALUES('owner','owner@test','customer_owner','customer1',1,'active','all');
        INSERT INTO partner_users VALUES('viewer','viewer@test','customer_viewer','customer1',1,'active','selected');
        INSERT INTO partner_users VALUES('denied','denied@test','customer_viewer','customer1',1,'active','selected');
        INSERT INTO partner_users VALUES('other','other@test','customer_owner','customer2',1,'active','all');
        CREATE TABLE customer_camera_permissions(user_id,camera_id,can_alerts);
        INSERT INTO customer_camera_permissions VALUES('viewer','c1',1);
        CREATE TABLE notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,event_type,severity,title,message,timestamp,thumbnail,created_at,dedupe_key,read_at,acknowledged_at,dismissed_at,bookmarked_at,UNIQUE(user_id,dedupe_key));
        CREATE TABLE notification_deliveries(id,notification_id,channel,status,provider,error,recipient,attempt,created_at,provider_message_id,provider_error_code);
        CREATE TABLE detection_event_media(detection_event_id,s3_key);
        ''')
        @contextmanager
        def connection():
            yield self.db
            self.db.commit()
        self.connection = connection
        def rows(query, params=()):
            return [dict(row) for row in self.db.execute(query, params).fetchall()]
        self.engine = ModuleType('notification_engine')
        self.external = Mock(return_value={'email': False, 'sms': False, 'email_address': '', 'phone_number': ''})
        self.push = Mock()
        self.engine.__dict__.update(connection=connection, rows=rows, datetime=datetime, secrets=secrets,
            SUPPORTED={'motion'}, OPT_IN_EVENT_TYPES=set(), EMERGENCY_EVENT_TYPES=set(),
            _quiet_hours_clock=lambda now: now.time(), event_type_label=lambda value: value,
            event_type_message=lambda value: value, _external_channels=self.external, _enqueue_mobile_push=self.push,
            CHANNELS={'in_app': SimpleNamespace(send=Mock(return_value={'status': 'delivered', 'provider': 'local'}))})
        load_functions('notification_engine.py', {'fanout_appliance_event', 'notification_event_type', 'event_dedupe_key'}, self.engine.__dict__)
        context = patch.dict(sys.modules, {'partner_db': SimpleNamespace(connection=connection), 'notification_engine': self.engine,
            'partner_portal': SimpleNamespace(partner_identity=lambda request: request.identity)})
        context.start(); self.addCleanup(context.stop)
        env = patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'local_gateway', 'ANYAICAM_RUNTIME_ROLE': 'edge'})
        env.start(); self.addCleanup(env.stop)
        licensed = patch('vms_capacity.camera_licensed', return_value=True)
        licensed.start(); self.addCleanup(licensed.stop)
        self.alert = {'id': 'alert1', 'event_id': 'motion1', 'camera': 1, 'event_type': 'motion',
                      'timestamp': '2026-11-02T10:00:00', 'message': 'Motion detected'}

    def test_local_motion_is_deduplicated_scoped_and_never_fans_out_externally(self):
        self.assertEqual(gateway_notifications.publish_local_motion(self.alert), 2)
        self.assertEqual(gateway_notifications.publish_local_motion(self.alert), 0)
        recipients = {row[0] for row in self.db.execute('SELECT user_id FROM notifications')}
        self.assertEqual(recipients, {'owner', 'viewer'})
        self.assertEqual({row[0] for row in self.db.execute('SELECT channel FROM notification_deliveries')}, {'in_app'})
        self.external.assert_not_called(); self.push.assert_not_called()

    def test_customer_inbox_reads_local_alert_and_existing_read_state(self):
        gateway_notifications.publish_local_motion(self.alert)
        ns = {'CUSTOMER_PORTAL_ROLES': {'customer_owner','customer_viewer'}}
        load_functions('main.py', {'_customer_notifications'}, ns)
        request = SimpleNamespace(identity={'role':'customer_owner','customer_id':'customer1','email':'owner@test'})
        alerts = ns['_customer_notifications'](request, inbox_view='active')
        self.assertEqual(len(alerts), 1); self.assertFalse(alerts[0]['read'])
        self.db.execute("UPDATE notifications SET read_at='2026-11-02T10:01:00' WHERE user_id='owner'")
        self.assertTrue(ns['_customer_notifications'](request, inbox_view='active')[0]['read'])
        request.identity = {'role':'customer_viewer','customer_id':'customer1','email':'denied@test'}
        self.assertEqual(ns['_customer_notifications'](request, inbox_view='active'), [])

    def test_append_keeps_legacy_alert_and_adds_customer_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'alerts.jsonl'
            ns = {'IN_APP_ALERTS_FILE': target, 'json': json, 'dispatch_external_notifications': Mock(), 'structured_log': Mock()}
            load_functions('main.py', {'append_in_app_alert'}, ns)
            ns['append_in_app_alert'](self.alert)
            self.assertEqual(json.loads(target.read_text())['event_id'], 'motion1')
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 2)
            ns['structured_log'].assert_not_called()
            ns['dispatch_external_notifications'].assert_called_once_with(self.alert)

    def test_cloud_gateway_uses_the_same_local_inbox(self):
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'cloud_gateway'}):
            self.assertEqual(gateway_notifications.publish_local_motion(self.alert), 2)
        self.external.assert_not_called(); self.push.assert_not_called()

    def test_all_camera_viewer_reads_own_local_motion_without_explicit_permission_rows(self):
        self.db.execute("UPDATE partner_users SET camera_access_mode='all' WHERE id='denied'")
        ns = {'CUSTOMER_PORTAL_ROLES':{'customer_owner','customer_viewer'}}
        load_functions('main.py', {'_customer_notifications'}, ns)
        request = SimpleNamespace(identity={'role':'customer_viewer', 'customer_id':'customer1', 'email':'denied@test'})
        for edition in ('local_gateway', 'cloud_gateway'):
            with self.subTest(edition=edition), patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':edition}):
                self.db.execute('DELETE FROM notifications')
                self.assertEqual(gateway_notifications.publish_local_motion(self.alert), 3)
                expected = self.db.execute("SELECT id FROM notifications WHERE user_id='denied'").fetchone()[0]
                # Camera-less rows never become visible to a viewer through
                # the all-camera exception, even if fan-out created one.
                self.engine.fanout_appliance_event({'customer_id':'customer1', 'site_id':'site1'},
                    {'id':'system', 'event_type':'motion', 'dedupe_key':'system'}, local_only=True)
                for inbox in (None, 'active'):
                    result = ns['_customer_notifications'](request, inbox_view=inbox, camera_number=1)
                    self.assertEqual([row['id'] for row in result], [expected])
                self.assertEqual([row['id'] for row in ns['_customer_notifications'](request)], [expected])
                request.identity['customer_id'] = 'customer2'
                self.assertEqual(ns['_customer_notifications'](request), [])
                request.identity['customer_id'] = 'customer1'
        self.external.assert_not_called(); self.push.assert_not_called()

    def test_all_camera_viewer_explicit_alert_denial_overrides_mode_and_hides_old_rows(self):
        self.db.execute("UPDATE partner_users SET camera_access_mode='all' WHERE id='denied'")
        self.assertEqual(gateway_notifications.publish_local_motion(self.alert), 3)
        ns = {'CUSTOMER_PORTAL_ROLES':{'customer_owner','customer_viewer'}}
        load_functions('main.py', {'_customer_notifications'}, ns)
        request = SimpleNamespace(identity={'role':'customer_viewer', 'customer_id':'customer1', 'email':'denied@test'})
        self.assertEqual(len(ns['_customer_notifications'](request, inbox_view='active')), 1)
        self.db.execute("INSERT INTO customer_camera_permissions VALUES('denied','c1',0)")
        self.assertEqual(ns['_customer_notifications'](request, inbox_view='active'), [])
        self.assertEqual(gateway_notifications.publish_local_motion({**self.alert, 'event_id':'motion2'}), 2)
        self.db.execute("UPDATE customer_camera_permissions SET can_alerts=1 WHERE user_id='denied'")
        self.assertEqual(len(ns['_customer_notifications'](request, inbox_view='active')), 1)
        self.db.execute("UPDATE partner_users SET camera_access_mode='selected' WHERE id='denied'")
        self.assertEqual(len(ns['_customer_notifications'](request, inbox_view='active')), 1)
        self.db.execute("DELETE FROM customer_camera_permissions WHERE user_id='denied'")
        self.assertEqual(ns['_customer_notifications'](request, inbox_view='active'), [])

    def test_removed_camera_does_not_consume_active_notification_capacity(self):
        import vms_capacity
        self.db.execute("UPDATE cameras SET camera_number=2 WHERE id='c1'")
        self.db.execute("INSERT INTO cameras VALUES('removed','customer1','site1','a1',1,'removed','Old')")
        alert = {**self.alert, 'camera': 2}
        # Exercise the real capacity selection rather than accepting every camera.
        with patch.object(vms_capacity, 'load_capacity', return_value={'camera_slot_quantity': 1}), \
             patch.object(vms_capacity, 'camera_licensed', side_effect=lambda number, numbers:
                          number in vms_capacity.licensed_camera_numbers(numbers)):
            for edition in ('local_gateway', 'cloud_gateway'):
                with self.subTest(edition=edition), patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': edition}):
                    self.db.execute('DELETE FROM notification_deliveries')
                    self.db.execute('DELETE FROM notifications')
                    self.assertEqual(gateway_notifications.publish_local_motion(alert), 2)
                    self.assertEqual(gateway_notifications.publish_local_motion({**alert, 'camera': 1}), 0)
        self.external.assert_not_called(); self.push.assert_not_called()

    def test_unlicensed_camera_and_full_vms_do_not_use_gateway_bridge(self):
        with patch('vms_capacity.camera_licensed', return_value=False):
            self.assertEqual(gateway_notifications.publish_local_motion(self.alert), 0)
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'full_vms'}):
            self.assertEqual(gateway_notifications.publish_local_motion(self.alert), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)

    def test_existing_fanout_default_preserves_external_policy_and_push(self):
        self.engine.fanout_appliance_event({'customer_id':'customer1','site_id':'site1'},
            {**self.alert, 'camera_id':'c1', 'dedupe_key':'c1:motion1'})
        self.assertEqual(self.external.call_count, 2)
        self.assertEqual(self.push.call_count, 2)
