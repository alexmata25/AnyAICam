"""The Claim ID attempt counters on PostgreSQL.

Live tests need ANYAICAM_TEST_POSTGRES_URL set to a disposable database, in the
style of test_camera_provisioning_postgresql.py. SQLite takes one writer at a
time, so it cannot show what these do: separate connections racing the same row.
The translation tests need no server and always run.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

TEST_URL = os.environ.get('ANYAICAM_TEST_POSTGRES_URL', '')
psycopg = pytest.importorskip('psycopg') if TEST_URL else None
if TEST_URL:
    os.environ['ANYAICAM_DATABASE_BACKEND'] = 'postgresql'
    os.environ['ANYAICAM_DATABASE_URL'] = TEST_URL

import appliance_claims
import claim_attempts
from database_backend import _postgres_sql, override_target
from partner_db import connection, initialize_database

live = pytest.mark.skipif(not TEST_URL, reason='set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database')
WORKERS = 16


class _Recorder:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((_postgres_sql(sql), params))
        return self

    def fetchone(self):
        return {'attempts': 1}


def _recorded(monkeypatch, call):
    from contextlib import contextmanager
    recorder = _Recorder()

    @contextmanager
    def fake_connection():
        yield recorder

    monkeypatch.setattr(claim_attempts, 'connection', fake_connection)
    call()
    return recorder.statements


def test_every_counter_statement_translates_with_matching_placeholders(monkeypatch):
    statements = _recorded(monkeypatch, lambda: claim_attempts.attempt('scope', 60, 1000)) + \
        _recorded(monkeypatch, lambda: claim_attempts.give_back('scope'))
    assert len(statements) == 3
    for sql, params in statements:
        assert '?' not in sql and 'INSERT OR' not in sql
        assert sql.count('%s') == len(params)


def test_the_counter_upsert_is_one_statement_that_returns_the_count(monkeypatch):
    upsert = _recorded(monkeypatch, lambda: claim_attempts.attempt('scope', 60, 1000))[1][0]
    assert 'ON CONFLICT(scope) DO UPDATE' in upsert and upsert.rstrip().endswith('RETURNING attempts')


@pytest.fixture()
def scope():
    prefix = f'pg-test:{uuid.uuid4().hex}'
    with override_target(database_url=TEST_URL):
        initialize_database()
        yield prefix
        with connection() as db:
            db.execute('DELETE FROM claim_attempt_counters WHERE scope LIKE ?', (prefix + '%',))


def _parallel(call, count):
    def task(_):
        with override_target(database_url=TEST_URL):
            return call()
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(task, range(count)))


def _count(name):
    with connection() as db:
        found = db.execute('SELECT attempts FROM claim_attempt_counters WHERE scope=?', (name,)).fetchone()
    return found['attempts'] if found else None


@live
def test_the_session_uses_read_committed(scope):
    """The upsert's guarantees below are for this level; another would raise instead."""
    with connection() as db:
        assert db.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'] == 'read committed'


@live
def test_parallel_attempts_each_get_a_different_count(scope):
    moment = 1_800_000_000
    counts = _parallel(lambda: claim_attempts.attempt(scope, 900, moment), 100)
    assert sorted(counts) == list(range(1, 101))
    assert _count(scope) == 100


@live
def test_parallel_attempts_across_a_window_edge_restart_once(scope):
    claim_attempts.attempt(scope, 900, 1_800_000_000)
    counts = _parallel(lambda: claim_attempts.attempt(scope, 900, 1_800_001_000), 50)
    assert sorted(counts) == list(range(1, 51))


@live
def test_parallel_returns_never_go_below_zero(scope):
    moment = 1_800_000_000
    for _ in range(10):
        claim_attempts.attempt(scope, 900, moment)
    _parallel(lambda: claim_attempts.give_back(scope), 40)
    assert _count(scope) == 0


@live
def test_a_limit_holds_exactly_under_parallel_guesses(scope, monkeypatch):
    account = f'{scope}:account'
    monkeypatch.setattr(appliance_claims, 'CLAIM_CODE_ACCOUNT_ATTEMPTS', 20)
    allowed = _parallel(lambda: appliance_claims._take_code_attempt(account), 80)
    assert allowed.count(True) == 20
    with connection() as db:
        db.execute('DELETE FROM claim_attempt_counters WHERE scope IN (?,?)', (f'account:{account}', f'day:{account}'))


@live
def test_a_cloud_wide_alert_level_is_reached_by_exactly_one_request(scope):
    counts = _parallel(lambda: claim_attempts.attempt(scope, 3600, 1_800_000_000), 60)
    assert counts.count(50) == 1


@live
def test_old_rows_are_cleared_while_workers_count_with_no_deadlock(scope):
    moment = 1_800_000_000
    with connection() as db:
        for index in range(300):
            db.execute('INSERT INTO claim_attempt_counters(scope,attempts,window_started_at) VALUES(?,?,?)',
                       (f'{scope}:old:{index}', 1, moment - 2 * claim_attempts.COUNTER_RETENTION_SECONDS))
    names = [f'{scope}:new:{index % 8}' for index in range(200)]
    tasks = iter(names)
    results = _parallel(lambda: claim_attempts.attempt(next(tasks), 900, moment), len(names))
    assert len(results) == 200 and all(results)
    with connection() as db:
        left = db.execute('SELECT COUNT(*) AS n FROM claim_attempt_counters WHERE scope LIKE ?', (f'{scope}:old:%',)).fetchone()['n']
    assert left == 0


@live
def test_two_requests_in_a_failed_transaction_do_not_keep_a_half_count(scope):
    """connection() commits on success only; a failed caller keeps no partial state."""
    with pytest.raises(RuntimeError):
        with connection() as db:
            db.execute('INSERT INTO claim_attempt_counters(scope,attempts,window_started_at) VALUES(?,1,1)', (scope,))
            raise RuntimeError('stop')
    assert _count(scope) is None
