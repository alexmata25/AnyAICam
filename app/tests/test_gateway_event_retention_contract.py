"""Exercise existing event policy with an in-memory database; no service imports."""
from datetime import datetime
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import event_media_policy as policy


class GatewayEventRetentionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE customer_cloud_policy(customer_id TEXT, daily_cloud_seconds INTEGER, retention_days INTEGER);
            CREATE TABLE plans(customer_id TEXT, recording_mode TEXT, retention_days INTEGER, created_at TEXT);
        ''')
        self.entitlement = None
        stub = SimpleNamespace(entitlement_for_customer=lambda customer: self.entitlement)
        context = patch.dict(sys.modules, {'per_camera_billing': stub})
        context.start()
        self.addCleanup(context.stop)

    def test_no_default_six_hour_quota_and_event_clips_ignore_recording_allowance(self):
        self.assertIsNone(policy.cloud_policy_for_customer(self.db, 'c1')['daily_cloud_seconds'])
        self.db.execute("INSERT INTO plans VALUES('c1','motion',14,'2026-11-01')")
        self.db.execute("INSERT INTO customer_cloud_policy VALUES('c1',1,NULL)")
        with patch.object(policy, 'daily_seconds_used', side_effect=AssertionError('event quota must not be consulted')):
            self.assertEqual(policy.allows_event_media(self.db, 'c1', 'camera1', datetime(2026, 11, 1), 30), (True, ''))

    def test_entitlement_retention_and_customer_override_are_preserved(self):
        self.entitlement = {'status': 'active', 'cloud_event_storage': True, 'cloud_event_retention_days': 14}
        self.assertEqual(policy.motion_event_policy(self.db, 'c1'), {'retention_days': 14})
        self.db.execute("INSERT INTO customer_cloud_policy VALUES('c1',NULL,30)")
        self.assertEqual(policy.motion_event_policy(self.db, 'c1'), {'retention_days': 30})
        self.entitlement['status'] = 'cancelled'
        self.assertIsNone(policy.motion_event_policy(self.db, 'c1'))

    def test_unsupported_retention_and_missing_entitlement_are_refused(self):
        self.assertIsNone(policy.motion_event_policy(self.db, 'c1'))
        self.db.execute("INSERT INTO customer_cloud_policy VALUES('c1',NULL,6)")
        self.assertIsNone(policy.motion_event_policy(self.db, 'c1'))
