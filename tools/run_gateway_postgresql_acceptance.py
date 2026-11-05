"""Prepared Gateway PostgreSQL acceptance launcher; execution is unavailable.

This module does not import application code, connect to PostgreSQL, start a
service, execute migrations or launch tests. Identity checks are necessary
preconditions, not evidence of OS isolation or authority to run acceptance.
"""
import argparse
import os
from urllib.parse import urlsplit


EXPECTED_HOST = '127.0.0.1'
EXPECTED_PORT = 55432
EXPECTED_DATABASE = 'aac_gateway_acceptance'
EXPECTED_ROLE = 'aac_gateway_test'
EXPECTED_TARGET = 'postgresql://aac_gateway_test@127.0.0.1:55432/aac_gateway_acceptance'


class TargetRejected(ValueError):
    pass


def validate_target(target):
    """Reject alternate libpq connection forms, credentials and overrides."""
    if not isinstance(target, str) or target != EXPECTED_TARGET:
        raise TargetRejected('Target is not the exact proposed disposable endpoint.')
    parsed = urlsplit(target)
    if (parsed.scheme != 'postgresql' or parsed.hostname != EXPECTED_HOST or parsed.port != EXPECTED_PORT
            or parsed.username != EXPECTED_ROLE or parsed.password is not None
            or parsed.path != '/' + EXPECTED_DATABASE or parsed.query or parsed.fragment):
        raise TargetRejected('Invalid disposable endpoint.')
    return True


def validate_observed_identity(observed, *, expected_system_id, expected_data_directory):
    """Compare a synthetic or verified DB identity; never connect or trust it alone."""
    if (not isinstance(observed, dict) or not isinstance(expected_system_id, str)
            or not expected_system_id.isdecimal() or len(expected_system_id) < 10
            or not isinstance(expected_data_directory, str) or not expected_data_directory):
        raise TargetRejected('Independently recorded cluster identity is required.')
    expected = {'database': EXPECTED_DATABASE, 'role': EXPECTED_ROLE,
                'server_address': EXPECTED_HOST, 'server_port': EXPECTED_PORT,
                'system_identifier': expected_system_id, 'data_directory': expected_data_directory,
                'is_superuser': False, 'can_create_db': False, 'can_create_role': False,
                'in_recovery': False}
    if set(observed) != set(expected):
        raise TargetRejected('Database identity fields are incomplete or unexpected.')
    if any(type(observed[key]) is not type(value) or observed[key] != value for key, value in expected.items()):
        raise TargetRejected('Database identity or least-privilege requirements do not match.')
    return True


def environment_check(environ):
    """Forbid libpq and application target/credential overrides without printing them."""
    blocked = ('PG', 'ANYAICAM_DATABASE_', 'ANYAICAM_PARTNER_DB', 'AWS_', 'STRIPE_')
    if any(value and (key.upper().startswith(blocked) or
                      (key.upper().startswith('ANYAICAM_') and 'STRIPE' in key.upper()))
           for key, value in environ.items()):
        raise TargetRejected('Potential inherited provider or database configuration is present.')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', default=EXPECTED_TARGET, help='credential-free proposed test target')
    parser.add_argument('--run', action='store_true', help='reserved; always blocked')
    args = parser.parse_args(argv)
    try:
        validate_target(args.target)
        environment_check(os.environ)
    except (TargetRejected, ValueError):
        print('STOP: target or inherited configuration rejected; no connection was attempted.')
        return 2
    print('STOP: PostgreSQL acceptance unavailable: independent cluster identity, OS isolation,')
    print('test-only credentials, approved schema and concurrency executor are not verified.')
    print('No database connection, migration, service or test was started.')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
