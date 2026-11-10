"""Offline acceptance launcher tests: no database or provider connections."""
import contextlib
import io
import os
import unittest
from unittest.mock import patch

from tools import run_gateway_postgresql_acceptance as pg
from tools import run_local_gateway_acceptance as http


class LauncherSafetyTests(unittest.TestCase):
    def test_only_exact_credential_free_loopback_target(self):
        self.assertTrue(pg.validate_target(pg.EXPECTED_TARGET))
        bad = ('postgresql://aac_gateway_test@localhost:55432/aac_gateway_acceptance',
               'postgresql://aac_gateway_test:secret@127.0.0.1:55432/aac_gateway_acceptance',
               'postgresql://aac_gateway_test@127.0.0.1:5432/aac_gateway_acceptance',
               pg.EXPECTED_TARGET + '?host=production', pg.EXPECTED_TARGET + '#fragment',
               'postgresql://aac_gateway_test@127.0.0.1:55432/production',
               'host=127.0.0.1 dbname=aac_gateway_acceptance')
        for target in bad:
            with self.subTest(target=target), self.assertRaises(pg.TargetRejected):
                pg.validate_target(target)

    def test_synthetic_server_identity_and_role_mismatches_rejected(self):
        expected_id = '1234567890123456789'
        directory = r'C:\AAC-Gateway-Acceptance\pgdata'
        row = dict(database=pg.EXPECTED_DATABASE, role=pg.EXPECTED_ROLE,
                   server_address=pg.EXPECTED_HOST, server_port=pg.EXPECTED_PORT,
                   system_identifier=expected_id, data_directory=directory,
                   is_superuser=False, can_create_db=False, can_create_role=False, in_recovery=False)
        self.assertTrue(pg.validate_observed_identity(row, expected_system_id=expected_id,
                                                     expected_data_directory=directory))
        for key, value in (('database','production'), ('role','postgres'), ('server_port',5432),
                           ('system_identifier','1234567890123456788'), ('data_directory',r'C:\production'),
                           ('is_superuser',True), ('can_create_db',True), ('can_create_role',True),
                           ('in_recovery',True)):
            changed = dict(row); changed[key] = value
            with self.subTest(key=key), self.assertRaises(pg.TargetRejected):
                pg.validate_observed_identity(changed, expected_system_id=expected_id,
                                              expected_data_directory=directory)

    def test_inherited_target_or_provider_configuration_rejected_without_echo(self):
        for key in ('PGHOST', 'PGOPTIONS', 'AWS_SECRET_ACCESS_KEY', 'STRIPE_SECRET_KEY',
                    'ANYAICAM_DATABASE_URL', 'ANYAICAM_PARTNER_DB', 'ANYAICAM_STRIPE_SECRET_KEY'):
            with self.subTest(key=key), self.assertRaises(pg.TargetRejected):
                pg.environment_check({key:'secret-synthetic-value'})

    def test_postgres_run_is_always_blocked_without_network_or_database(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output), \
             patch('socket.socket.connect', side_effect=AssertionError('network')), \
             patch('sqlite3.connect', side_effect=AssertionError('database')):
            self.assertEqual(pg.main(['--run']), 2)
        self.assertIn('No database connection', output.getvalue())

    def test_http_isolation_stops_even_with_clean_credentials_and_synthetic_flags(self):
        with self.assertRaisesRegex(RuntimeError, 'credential isolation verifier'):
            http.test_credentials_check({})
        with self.assertRaisesRegex(RuntimeError, 'database isolation verifier'):
            http.database_isolation_check()
        with self.assertRaisesRegex(RuntimeError, 'filesystem isolation verifier'):
            http.confinement_check()
        with self.assertRaisesRegex(RuntimeError, 'outbound-denial verifier'):
            http.isolation_check(('synthetic', True, 0, 'synthetic'))
        with self.assertRaisesRegex(RuntimeError, 'inherited provider'):
            http.test_credentials_check({'AWS_SECRET_ACCESS_KEY':'synthetic-secret'})


if __name__ == '__main__':
    unittest.main()
