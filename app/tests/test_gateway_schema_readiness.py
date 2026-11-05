"""Read-only inspection of hand-built disposable schemas; no migrations."""
import sqlite3
import unittest
from app import gateway_schema_readiness as readiness


class GatewaySchemaReadinessTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        for table, columns in readiness.COLUMNS.items():
            self.db.execute('CREATE TABLE '+table+' ('+', '.join(name+(' INTEGER' if ((table == 'internal_gateway_provisioning_authorizations' and name == 'version') or (table == 'cloud_gateway_camera_entitlements' and name in {'camera_number','version'}) or (table == 'cloud_gateway_subscription_evidence' and name in {'quantity','amount_cents','interval_count','current_period_end','payment_verified','event_created'}) or (table in {'cloud_gateway_evidence_holds','cloud_gateway_webhook_correlation'} and name == 'event_created') or (table == 'cloud_gateway_refresh_leases' and name == 'lease_until') or (table == 'gateway_checkout_attempts' and name in {'quantity','authorization_invalidated'}) or (table == 'cloud_gateway_retention_settings' and name in {'retention_days','version'})) else ' TEXT') for name in sorted(columns))+')')
        self.db.execute('CREATE TABLE schema_migrations(version TEXT, applied_at TEXT)')
        for i, version in enumerate(readiness.VERSIONS):
            self.db.execute('INSERT INTO schema_migrations VALUES(?,?)',(version,f'{i:02d}'))
        self.db.execute("CREATE UNIQUE INDEX idx_claim_one_live_session_per_device ON appliance_claims(LOWER(device_id)) WHERE status IN ('pending','claimed') AND revoked_at IS NULL")
        self.db.execute("CREATE UNIQUE INDEX idx_appliance_claim_one_current_completed ON appliance_claims(appliance_id) WHERE appliance_id IS NOT NULL AND status='completed' AND revoked_at IS NULL")
        self.db.execute('CREATE UNIQUE INDEX idx_appliance_edition_current_decision ON appliance_edition_provisioning_decisions(appliance_id) WHERE revoked_at IS NULL')
        self.db.execute("CREATE UNIQUE INDEX idx_internal_gateway_active_authorization ON internal_gateway_provisioning_authorizations(appliance_id) WHERE status='active'")
        self.db.execute("CREATE UNIQUE INDEX idx_cloud_gateway_camera_current ON cloud_gateway_camera_entitlements(camera_id) WHERE status IN ('pending','active','suspended')")
        self.db.execute("CREATE UNIQUE INDEX idx_gateway_checkout_one_unresolved ON gateway_checkout_attempts(appliance_id) WHERE status IN ('reserved','creating','open','review','paid_review')")
        self.db.execute("CREATE UNIQUE INDEX idx_gateway_claim_one_pending_recovery ON gateway_claim_recovery_requests(appliance_id) WHERE status='pending_manual_verification'")

    def test_complete_schema_and_missing_or_weakened_index(self):
        self.assertEqual(readiness.inspect_gateway_schema(self.db,'sqlite'),{'ready':True,'issues':[]})
        self.db.execute('DROP INDEX idx_claim_one_live_session_per_device')
        self.db.execute("CREATE UNIQUE INDEX idx_claim_one_live_session_per_device ON appliance_claims(LOWER(device_id)) WHERE status='pending'")
        result = readiness.inspect_gateway_schema(self.db,'sqlite')
        self.assertFalse(result['ready'])
        self.assertTrue(any('idx_claim_one_live_session_per_device' in issue for issue in result['issues']))

    def test_detects_duplicates_missing_columns_and_ordering(self):
        self.db.execute('DROP INDEX idx_appliance_claim_one_current_completed')
        for claim_id in ('one','two'):
            self.db.execute("INSERT INTO appliance_claims(id,appliance_id,device_id,status) VALUES(?,?,?,?)",(claim_id,'device','uuid','completed'))
        self.db.execute("UPDATE schema_migrations SET applied_at='99' WHERE version=?",(readiness.VERSIONS[1],))
        self.db.execute('DROP TABLE internal_gateway_provisioning_authorizations')
        result = readiness.inspect_gateway_schema(self.db,'sqlite')
        self.assertFalse(result['ready'])
        self.assertTrue(any('Duplicate current completed' in issue for issue in result['issues']))
        self.assertTrue(any('ordering conflict' in issue for issue in result['issues']))
        self.assertTrue(any('columns in internal_gateway' in issue for issue in result['issues']))

    def test_unknown_backend_fails_closed(self):
        self.assertFalse(readiness.inspect_gateway_schema(self.db,'other')['ready'])

    def test_detects_pending_duplicates_missing_migration_and_bad_column_type(self):
        self.db.execute('DROP INDEX idx_claim_one_live_session_per_device')
        for claim_id in ('one','two'):
            self.db.execute("INSERT INTO appliance_claims(id,device_id,status) VALUES(?,?,?)",(claim_id,'device','pending'))
        self.db.execute('DELETE FROM schema_migrations WHERE version=?',(readiness.VERSIONS[-1],))
        result = readiness.inspect_gateway_schema(self.db,'sqlite')
        self.assertTrue(any('Duplicate current pending/claimed' in item for item in result['issues']))
        self.assertTrue(any('Missing migration record' in item for item in result['issues']))
        self.db.execute('ALTER TABLE internal_gateway_provisioning_authorizations RENAME TO old_authorizations')
        self.db.execute('CREATE TABLE internal_gateway_provisioning_authorizations(version TEXT)')
        result = readiness.inspect_gateway_schema(self.db,'sqlite')
        self.assertTrue(any('Incompatible column type' in item for item in result['issues']))

    def test_postgresql_index_metadata_must_be_valid_and_exact(self):
        sql = "CREATE UNIQUE INDEX idx_claim_one_live_session_per_device ON public.appliance_claims USING btree (lower(device_id)) WHERE ((status = ANY (ARRAY['pending'::text, 'claimed'::text])) AND (revoked_at IS NULL))"
        record = {'table_name':'appliance_claims','valid':True,'sql':sql}
        self.assertTrue(readiness._index_compatible(record, readiness.SESSION_INDEX),readiness._compact(sql))
        self.assertFalse(readiness._index_compatible({**record,'valid':False}, readiness.SESSION_INDEX))
        self.assertFalse(readiness._index_compatible({**record,'sql':sql.replace('lower(device_id)', 'lower(device_id), customer_id')}, readiness.SESSION_INDEX))


if __name__ == '__main__': unittest.main()
