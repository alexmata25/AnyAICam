"""Synthetic, in-memory records; no VMS imports, migrations or providers."""
import sqlite3
import unittest
from app.gateway_purchase_handoff import HandoffDenied, reconcile


class PurchaseHandoffTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE customer_entitlements(customer_id,product,status,expires_at,stripe_checkout_session_id);
            CREATE TABLE hardware_orders(id,customer_id,status,fulfillment_status,sku,quantity);
            CREATE TABLE appliances(id,customer_id,gateway_edition);
            INSERT INTO customer_entitlements VALUES('a','vms_license','active',NULL,'cs-a');
            INSERT INTO hardware_orders VALUES('order-a','a','paid','preparing','AIC-APPLIANCE-1',1);
            INSERT INTO appliances VALUES('box-a','a',NULL);
            INSERT INTO appliances VALUES('box-b','b','cloud_gateway');''')
        self.actor={'role':'customer_owner','customer_id':'a'}

    def tearDown(self): self.db.close()

    def call(self, **changes):
        args={'kind':'software_only','purchase_reference':'cs-a','requested_edition':'local_gateway'}
        args.update(changes)
        return reconcile(self.db,self.actor,**args)

    def test_software_purchase_waits_for_installation_and_never_grants(self):
        before=self.db.total_changes
        result=self.call()
        self.assertEqual(result['status'],'awaiting_installation_claim')
        self.assertFalse(result['purchase_binding_verified'])
        self.assertFalse(result['activation_authorized'])
        self.assertEqual(self.db.total_changes,before)

    def test_replay_is_idempotent_and_does_not_write(self):
        before=self.db.total_changes
        self.assertEqual(self.call(appliance_id='box-a'),self.call(appliance_id='box-a'))
        self.assertEqual(self.db.total_changes,before)
        self.assertEqual(self.call(appliance_id='box-a')['status'],'pending_verification')

    def test_duplicate_purchase_rows_fail_closed(self):
        self.db.execute("INSERT INTO customer_entitlements VALUES('a','vms_license','active',NULL,'cs-a')")
        with self.assertRaises(HandoffDenied): self.call()

    def test_cancelled_or_refunded_evidence_is_ineligible(self):
        for status in ('cancelled','refunded','disputed','pending'):
            self.db.execute('UPDATE customer_entitlements SET status=?',(status,))
            self.assertEqual(self.call()['status'],'payment_or_license_ineligible')
        self.db.execute("UPDATE hardware_orders SET status='refunded'")
        self.assertEqual(self.call(kind='preinstalled_appliance',purchase_reference='order-a')['status'],
                         'payment_or_license_ineligible')

    def test_hardware_fulfillment_and_shipment_wait_for_claim(self):
        kwargs={'kind':'preinstalled_appliance','purchase_reference':'order-a','requested_edition':'cloud_gateway'}
        self.assertEqual(self.call(**kwargs)['status'],'awaiting_fulfillment')
        self.db.execute("UPDATE hardware_orders SET fulfillment_status='shipped'")
        self.assertEqual(self.call(**kwargs)['status'],'awaiting_installation_claim')
        self.assertEqual(self.call(appliance_id='box-a',**kwargs)['status'],'pending_verification')

    def test_duplicate_unit_order_or_unknown_fulfillment_never_activates(self):
        self.db.execute("UPDATE hardware_orders SET quantity=2")
        with self.assertRaises(HandoffDenied): self.call(kind='preinstalled_appliance',purchase_reference='order-a')
        self.db.execute("UPDATE hardware_orders SET quantity=1,fulfillment_status='unknown'")
        self.assertEqual(self.call(kind='preinstalled_appliance',purchase_reference='order-a')['status'],'pending_verification')

    def test_edition_conflict_and_tenant_isolation(self):
        self.db.execute("UPDATE appliances SET gateway_edition='cloud_gateway' WHERE id='box-a'")
        self.assertEqual(self.call(appliance_id='box-a')['status'],'edition_conflict')
        with self.assertRaises(HandoffDenied): self.call(appliance_id='box-b')
        self.actor={'role':'customer_owner','customer_id':'b'}
        with self.assertRaises(HandoffDenied): self.call()

    def test_viewer_or_claim_id_cannot_authorize(self):
        self.actor={'role':'customer_viewer','customer_id':'a'}
        with self.assertRaises(HandoffDenied): self.call(appliance_id='box-a')
        self.actor={'role':'customer_owner','customer_id':'a'}
        with self.assertRaises(HandoffDenied): self.call(appliance_id='AAC-SYNTHETIC-CLAIM-ID')

    def test_missing_schema_fails_closed(self):
        self.db.execute('DROP TABLE customer_entitlements')
        with self.assertRaises(HandoffDenied): self.call()

    def test_malformed_kind_or_edition_is_rejected_cleanly(self):
        for value in ({}, []):
            with self.subTest(value=value), self.assertRaises(HandoffDenied): self.call(kind=value)
            with self.subTest(value=value), self.assertRaises(HandoffDenied): self.call(requested_edition=value)


if __name__=='__main__': unittest.main()
