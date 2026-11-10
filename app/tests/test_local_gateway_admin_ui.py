"""Synthetic administrator queue checks; no application startup."""
import sys
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import local_gateway_admin_ui as ui


class AdminQueueTests(unittest.TestCase):
    def test_claim_and_license_context_is_scoped_and_never_proves_edition(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE appliance_claims(id TEXT,appliance_id TEXT,customer_id TEXT,status TEXT,revoked_at TEXT);
            CREATE TABLE customer_entitlements(id TEXT,customer_id TEXT,product TEXT,status TEXT,camera_slot_quantity INTEGER);
            INSERT INTO appliance_claims VALUES('claim-a','device-a','customer-a','completed',NULL);
            INSERT INTO customer_entitlements VALUES('license-a','customer-a','vms_license','active',4);
            INSERT INTO customer_entitlements VALUES('foreign-license','customer-b','vms_license','active',4);
        ''')
        def query(sql, args):
            return [dict(row) for row in db.execute(sql, args)]
        appliance = dict(id='device-a',customer_id='customer-a',activated_via='claim',gateway_edition=None)
        claim, licence = ui.claim_purchase_context(appliance, query)
        self.assertEqual(claim, 'Completed claim claim-a')
        self.assertIn('license-a: active (not edition evidence)', licence)
        self.assertNotIn('foreign-license', licence)
        db.execute("INSERT INTO appliance_claims VALUES('claim-b','device-a','customer-a','completed',NULL)")
        self.assertIn('Ambiguous', ui.claim_purchase_context(appliance, query)[0])
        db.execute("UPDATE appliance_claims SET revoked_at='now' WHERE id='claim-b'")
        self.assertEqual(ui.claim_purchase_context(appliance, query)[0], 'Completed claim claim-a')
        db.execute("UPDATE appliance_claims SET customer_id='customer-b' WHERE id='claim-a'")
        self.assertIn('mismatched', ui.claim_purchase_context(appliance, query)[0])
        db.execute("UPDATE appliance_claims SET status='expired' WHERE id='claim-a'")
        self.assertIn('No current completed claim', ui.claim_purchase_context(appliance, query)[0])

    def test_global_queue_is_read_only_and_escapes_untrusted_labels(self):
        appliances = [dict(id='dev-1', customer_id='c-1', activated_via='claim',
                           gateway_edition=None, cloud_id='<script>', state='online'),
                      dict(id='other', customer_id='c-1', activated_via='claim', gateway_edition='local_gateway')]
        customers = [dict(id='c-1', name='<Customer>', email='customer@example.test')]
        readiness = SimpleNamespace(_live=lambda aid: True, enrollment_ready=lambda aid: True)
        with patch.dict(sys.modules, {'appliance_readiness': readiness}):
            html = ui.verification_queue(appliances, customers, is_global_admin=True)
            self.assertIn('&lt;Customer&gt;', html)
            self.assertIn('&lt;script&gt;', html)
            self.assertIn('customer@example.test', html)
            self.assertIn('dev-1', html)
            self.assertIn('Pending verification', html)
            self.assertIn('Ready', html)
            self.assertNotIn('other', html)
            self.assertNotIn('<button', html)
            self.assertEqual(ui.verification_queue(appliances, customers, is_global_admin=False), '')

    def test_revoked_or_retired_credential_is_not_ready(self):
        appliance = dict(id='dev', customer_id='c', activated_via='claim', gateway_edition=None, state='revoked')
        readiness = SimpleNamespace(_live=lambda aid: False, enrollment_ready=lambda aid: True)
        with patch.dict(sys.modules, {'appliance_readiness': readiness}):
            html = ui.verification_queue([appliance], [], is_global_admin=True)
        self.assertIn('Unavailable or revoked', html)
        self.assertIn('Not ready', html)
        self.assertIn('Pending verification', html)


if __name__ == '__main__':
    unittest.main()
