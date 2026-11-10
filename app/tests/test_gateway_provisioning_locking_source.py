"""Source review only: these checks do not exercise PostgreSQL."""
import ast
from pathlib import Path
import unittest


class LockingSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'gateway_internal_provisioning.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        cls.functions = {n.name:ast.get_source_segment(source,n) for n in tree.body if isinstance(n,ast.FunctionDef)}
        cls.migrations = (root / 'db_migrations.py').read_text(encoding='utf-8')
        cls.main = (root / 'main.py').read_text(encoding='utf-8')
        cls.claims = (root / 'appliance_claims.py').read_text(encoding='utf-8')

    def test_appliance_claim_authorization_lock_order(self):
        bound = self.functions['_bound_claim']
        self.assertLess(bound.index('UPDATE appliances'),bound.index('UPDATE appliance_claims'))
        verifier = self.functions['verify_internal_test']
        self.assertLess(verifier.index('_bound_claim('),verifier.index('UPDATE internal_gateway_provisioning_authorizations'))
        change = self.functions['change_authorization']
        self.assertLess(change.index('UPDATE appliances'),change.index('UPDATE appliance_claims'))
        self.assertLess(change.index('UPDATE appliance_claims'),change.index('UPDATE internal_gateway_provisioning_authorizations'))
        self.assertLess(change.index('verify_internal_test('),change.index('SET status=?,version=?'))

    def test_partial_uniqueness_and_unmounted_routes(self):
        self.assertIn("ON internal_gateway_provisioning_authorizations(appliance_id) WHERE status='active'",self.migrations)
        self.assertIn('ON appliance_edition_provisioning_decisions(appliance_id) WHERE revoked_at IS NULL',self.migrations)
        self.assertIn("WHERE appliance_id IS NOT NULL AND status='completed' AND revoked_at IS NULL",self.migrations)
        self.assertNotIn('register_unmounted_test_routes',self.main)

    def test_claim_completion_associates_appliance_within_connection_block(self):
        start = self.claims.index('    def claim_complete(')
        body = self.claims[start:self.claims.index('    if shell is None:',start)]
        self.assertLess(body.index('with connection() as db:'),body.index("UPDATE appliance_claims SET status='completed'"))
        self.assertLess(body.index("UPDATE appliance_claims SET status='completed'"),body.index('UPDATE appliance_claims SET appliance_id=?'))


if __name__ == '__main__': unittest.main()
