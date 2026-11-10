"""Synthetic signed contract cases; no database, provider or application start."""
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch
from app import gateway_purchase_handoff as handoff


class EvidenceContractTests(unittest.TestCase):
    key=b'synthetic-test-key-not-for-prod-00001'
    def setUp(self):
        self.doc={'schema_version':1,'evidence_id':'ev-a','revision':1,'customer_id':'customer-a',
                  'edition':'cloud_gateway','stripe_product_id':'prod_test','stripe_price_id':'price_test',
                  'payment_reference':'payment-test','payment_status':'paid','reversal_state':'none',
                  'license_reference':'license-a','license_customer_id':'customer-a','license_status':'active',
                  'license_capacity':4,'fulfillment_kind':'software_only','hardware_order_id':None,
                  'hardware_unit_id':None,'fulfillment_status':'not_applicable','installation_id':'box-a',
                  'cloud_subscription_id':'sub-test','cloud_subscription_status':'active','cloud_quantity':2,'issued_at':100,'expires_at':200}
        self.patcher=patch.object(handoff,'PURCHASE_EVIDENCE_CONTRACT_READY',True); self.patcher.start()
    def tearDown(self): self.patcher.stop()
    def sign(self,doc):
        data=json.dumps(doc,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
        return hmac.new(self.key,data,hashlib.sha256).hexdigest()
    def check(self, doc=None, **kwargs):
        doc=self.doc if doc is None else doc
        args={'key':self.key,'now':150,'customer_id':'customer-a','edition':doc.get('edition'),
              'installation_id':'box-a','claim_verified':True}; args.update(kwargs)
        return handoff.validate_signed_candidate(doc,self.sign(doc),**args)
    def test_gate_is_disabled_by_default(self):
        with patch.object(handoff,'PURCHASE_EVIDENCE_CONTRACT_READY',False):
            self.assertEqual(self.check(),{'status':'unavailable'})
    def test_valid_is_candidate_only_not_activation(self):
        self.assertEqual(self.check()['status'],'candidate_only')
    def test_tamper_and_untrusted_key_fail(self):
        with self.assertRaises(handoff.HandoffDenied):
            handoff.validate_signed_candidate(self.doc,'0'*64,key=self.key,now=150,customer_id='customer-a',
                edition='cloud_gateway',installation_id='box-a',claim_verified=True)
        with self.assertRaises(handoff.HandoffDenied): self.check(key=b'short')
    def test_wrong_customer_edition_licence_and_claim_fail(self):
        for args in ({'customer_id':'customer-b'}, {'edition':'local_gateway'},
                     {'installation_id':'box-b'}, {'claim_verified':False}):
            with self.subTest(args=args),self.assertRaises(handoff.HandoffDenied): self.check(**args)
        self.doc['license_customer_id']='customer-b'
        with self.assertRaises(handoff.HandoffDenied): self.check()
    def test_duplicate_stale_conflicting_revision(self):
        result=self.check(); previous={'evidence_id':'ev-a','revision':1,'digest':result['digest'],
                                      'binding_digest':result['binding_digest'],'installation_id':'box-a'}
        self.assertTrue(self.check(previous=previous)['duplicate'])
        self.doc['cloud_quantity']=3
        with self.assertRaises(handoff.HandoffDenied): self.check(previous=previous)
        self.doc['revision']=2
        self.assertEqual(self.check(previous=previous)['status'],'candidate_only')
        previous['revision']=3
        with self.assertRaises(handoff.HandoffDenied): self.check(previous=previous)
    def test_refunds_cancellation_and_licence_revocation_are_ineligible(self):
        for name,value in (('payment_status','canceled'),('reversal_state','refunded'),
                           ('reversal_state','disputed'),('license_status','revoked'),('cloud_subscription_status','canceled')):
            doc=dict(self.doc);doc[name]=value
            self.assertEqual(self.check(doc)['status'],'ineligible')
    def test_missing_or_invalid_quantity_revision_and_time_fail(self):
        for name,value in (('cloud_quantity',None),('cloud_quantity',0),('cloud_quantity',True),
                           ('revision',0),('revision',True),('edition',None),('expires_at',150)):
            doc=dict(self.doc);doc[name]=value
            with self.subTest(name=name,value=value),self.assertRaises(handoff.HandoffDenied):
                self.check(doc)
    def test_software_only_waits_for_claim(self):
        self.doc['installation_id']=None
        self.assertEqual(self.check(installation_id=None,claim_verified=False)['status'],'awaiting_claim')
    def test_hardware_fulfillment_and_reassignment(self):
        self.doc.update(fulfillment_kind='preinstalled_appliance',hardware_order_id='ord-a',
                        hardware_unit_id='unit-a',fulfillment_status='preparing')
        self.assertEqual(self.check()['status'],'awaiting_fulfillment')
        self.doc['fulfillment_status']='shipped'; self.doc['installation_id']='other-box'
        with self.assertRaises(handoff.HandoffDenied): self.check()
        self.doc['installation_id']='box-a'
        result=self.check(); self.assertEqual(result['status'],'candidate_only')
        previous={'evidence_id':'ev-a','revision':1,'digest':result['digest'],
                  'binding_digest':result['binding_digest'],'installation_id':'box-a'}
        self.doc.update(revision=2,hardware_unit_id='unit-b')
        with self.assertRaises(handoff.HandoffDenied): self.check(previous=previous)
        self.doc.update(hardware_unit_id='unit-a',installation_id='other-box')
        with self.assertRaises(handoff.HandoffDenied): self.check(previous=previous,installation_id='other-box')
    def test_local_must_not_claim_cloud_quantity(self):
        self.doc.update(edition='local_gateway',cloud_subscription_id=None,cloud_subscription_status=None,cloud_quantity=None)
        self.assertEqual(self.check()['status'],'candidate_only')
        self.doc['cloud_quantity']=1
        with self.assertRaises(handoff.HandoffDenied): self.check()

    def test_new_revision_cannot_silently_replace_product_price_or_subscription(self):
        result=self.check()
        previous={'evidence_id':'ev-a','revision':1,'digest':result['digest'],
                  'binding_digest':result['binding_digest'],'installation_id':'box-a'}
        for key,value in (('stripe_product_id','prod_other'),('stripe_price_id','price_other'),
                          ('cloud_subscription_id','sub-other')):
            doc=dict(self.doc); doc.update(revision=2,**{key:value})
            with self.subTest(key=key),self.assertRaises(handoff.HandoffDenied):
                self.check(doc,previous=previous)

    def test_malformed_hardware_status_is_rejected_cleanly(self):
        self.doc.update(fulfillment_kind='preinstalled_appliance',hardware_order_id='ord-a',
                        hardware_unit_id='unit-a',fulfillment_status=[])
        with self.assertRaises(handoff.HandoffDenied): self.check()


if __name__=='__main__': unittest.main()
