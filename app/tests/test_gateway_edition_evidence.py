"""No real database, provider, application startup or migrations."""
import unittest
from unittest.mock import Mock
from app import gateway_edition_evidence as evidence


class EditionEvidenceTests(unittest.TestCase):
    def test_both_categories_and_editions_fail_closed_for_plausible_references(self):
        db = Mock()
        for source in evidence.SOURCE_CATEGORIES:
            for edition in evidence.EDITIONS:
                with self.subTest(source=source, edition=edition):
                    result = evidence.verify_edition_evidence(db, source=source,
                        reference='paid-order-or-authorization-123', customer_id='customer-a',
                        appliance_id='appliance-a', edition=edition)
                    self.assertEqual(result.status, 'unavailable')
                    self.assertFalse(result)
        db.assert_not_called()
        self.assertFalse(db.method_calls)

    def test_unsupported_or_malformed_inputs_never_verify(self):
        base = dict(source=evidence.VERIFIED_GATEWAY_PURCHASE, reference='ref',
                    customer_id='customer-a', appliance_id='appliance-a', edition='local_gateway')
        for change in ({'source':'vms_license'}, {'source':'hardware_order'},
                       {'source':'claim'}, {'source':'administrator_assertion'},
                       {'source':[]}, {'edition':'full_vms'}, {'edition':[]},
                       {'reference':''}, {'reference':' ref '}, {'customer_id':None},
                       {'appliance_id':''}):
            with self.subTest(change=change):
                result = evidence.verify_edition_evidence(None, **{**base, **change})
                self.assertEqual(result.status, 'invalid')
                self.assertFalse(result)

    def test_source_reports_which_producer_is_missing(self):
        base = dict(reference='ref', customer_id='customer-a', appliance_id='appliance-a', edition='cloud_gateway')
        purchase = evidence.verify_edition_evidence(None, source=evidence.VERIFIED_GATEWAY_PURCHASE, **base)
        internal = evidence.verify_edition_evidence(None, source=evidence.AUTHORIZED_INTERNAL_PROVISIONING, **base)
        self.assertIn('purchase', purchase.reason)
        self.assertIn('authorization', internal.reason)
        with self.assertRaises(AttributeError):
            purchase.status = 'verified'


if __name__ == '__main__':
    unittest.main()
