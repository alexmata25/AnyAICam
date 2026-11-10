"""Inspect the proposed migration without executing it or opening a database."""
import ast
from pathlib import Path
import unittest


class EditionDecisionSchemaSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (Path(__file__).resolve().parents[1] / 'db_migrations.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        matches = [node.elts[1].value for node in ast.walk(tree)
                   if isinstance(node, ast.Tuple) and len(node.elts) == 2
                   and isinstance(node.elts[0], ast.Constant)
                   and node.elts[0].value == '20261104_appliance_edition_provisioning_decisions'
                   and isinstance(node.elts[1], ast.Constant)]
        if len(matches) != 1:
            raise AssertionError('Expected exactly one decision migration')
        cls.sql = ' '.join(matches[0].lower().split())

    def test_binds_claim_customer_appliance_evidence_and_actor(self):
        for column in ('customer_id', 'appliance_id', 'claim_id', 'edition', 'evidence_source',
                       'evidence_reference', 'evidence_version', 'verified_by', 'verified_at'):
            with self.subTest(column=column):
                self.assertIn(column + ' text not null', self.sql)
        self.assertIn("edition in ('local_gateway','cloud_gateway')", self.sql)
        for entity in ('customer', 'appliance', 'claim'):
            self.assertIn('foreign key(' + entity + '_id)', self.sql)

    def test_one_unrevoked_decision_and_no_backfill(self):
        self.assertIn('unique index', self.sql)
        self.assertIn('(appliance_id) where revoked_at is null', self.sql)
        self.assertNotIn('insert into', self.sql)
        self.assertNotIn('update appliances', self.sql)


if __name__ == '__main__':
    unittest.main()
