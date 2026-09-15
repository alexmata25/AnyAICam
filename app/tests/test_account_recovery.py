import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_security import clear_login_failures, consume_password_reset, create_password_reset, login_blocked, record_login_failure
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash, row, verify_password


@pytest.fixture()
def recovery_db(tmp_path):
    path = tmp_path / 'recovery.db'
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,created_at) VALUES('partner-a','Partner A','approved',?)", (datetime.now().isoformat(),))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('customer-a','partner-a','Customer A','customer@example.test','active',?)", (datetime.now().isoformat(),))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) VALUES('user-a','partner-a','customer@example.test','Customer','customer_owner',?,1,'customer-a',?,'active')", (password_hash('old-password-123'), datetime.now().isoformat()))
        yield path


def test_expired_lockout_starts_a_new_failure_window(recovery_db):
    with override_target(sqlite_path=str(recovery_db)):
        for _ in range(5):
            record_login_failure('customer@example.test')
        assert login_blocked('customer@example.test') is True
        with connection() as db:
            db.execute('UPDATE account_lockouts SET locked_until=?,last_attempt_at=? WHERE email=?', ((datetime.now()-timedelta(minutes=1)).isoformat(), (datetime.now()-timedelta(minutes=16)).isoformat(), 'customer@example.test'))
        assert login_blocked('customer@example.test') is False
        record_login_failure('customer@example.test')
        state=row('SELECT attempts,locked_until FROM account_lockouts WHERE email=?', ('customer@example.test',))
        assert state == {'attempts': 1, 'locked_until': None}


def test_successful_reset_revokes_sessions_and_clears_lockout(recovery_db):
    with override_target(sqlite_path=str(recovery_db)):
        for _ in range(5):
            record_login_failure('customer@example.test')
        with connection() as db:
            db.execute("INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,expires_at) VALUES('session-a','user-a','customer@example.test','customer_owner','partner',?,?)", (datetime.now().isoformat(), (datetime.now()+timedelta(days=1)).isoformat()))
        raw=create_password_reset('user-a','customer@example.test')
        assert consume_password_reset(raw, 'new-password-456') == 'customer_owner'
        assert login_blocked('customer@example.test') is False
        assert row('SELECT * FROM account_lockouts WHERE email=?', ('customer@example.test',)) is None
        account=row('SELECT password_hash FROM partner_users WHERE id=?', ('user-a',))
        assert verify_password('old-password-123', account['password_hash']) is False
        assert verify_password('new-password-456', account['password_hash']) is True
        assert row('SELECT revoked_at FROM user_sessions WHERE id=?', ('session-a',))['revoked_at'] is not None
        assert row('SELECT used_at FROM password_reset_tokens WHERE user_id=?', ('user-a',))['used_at'] is not None
        assert consume_password_reset(raw, 'another-password-789') is None
