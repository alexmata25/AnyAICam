"""Claim ID plus one-time code in the portal (proposal)."""
import os
import re
import subprocess
import sys
from pathlib import Path

import appliance_claims
import claim_attempts
import db_migrations
import partner_db
import partner_portal
from claim_id import claim_id_for

from test_appliance_claims import (  # noqa: F401 -- fixtures
    VALID_DEVICE_ID,
    _customer_cookie,
    _seeded_customer,
    claim_flow_key,
    client,
    db_path,
    identity_file,
)

DEVICE = 'device-secret-of-the-real-appliance-0123456789abcdef'
CLAIM_ID = claim_id_for(VALID_DEVICE_ID)
OTHER_ID = claim_id_for('11111111-2222-4333-8444-555555555555')
WRONG = '000000000000'


def _forget(*scopes):
    with partner_db.connection() as db:
        for scope in scopes:
            db.execute('DELETE FROM claim_attempt_counters WHERE scope LIKE ?', (scope + '%',))
    appliance_claims.claim_portal_limiter.events.clear()


def _begin(client):
    return client.post('/api/appliance/claim/begin', json={'device_id': VALID_DEVICE_ID, 'device_secret': DEVICE, 'long_code': True}).json()


def _lookup(client, **body):
    return client.post('/api/portal/claims/lookup', json=body, cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})


def test_id_with_its_code_finds_the_claim(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 200


def test_the_id_alone_never_finds_or_confirms(client, db_path):
    _seeded_customer(db_path)
    _begin(client)
    assert _lookup(client, claim_id=CLAIM_ID).status_code == 404
    confirmed = client.post('/api/portal/claims/confirm', json={'claim_id': CLAIM_ID, 'site_id': 'site-1'},
                            cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})
    assert confirmed.status_code == 400


def test_unknown_wrong_and_expired_answer_alike(client, db_path):
    _seeded_customer(db_path)
    _begin(client)
    wrong = _lookup(client, claim_id=CLAIM_ID, claim_code=WRONG)
    unknown = _lookup(client, claim_id=OTHER_ID, claim_code=WRONG)
    assert wrong.status_code == unknown.status_code == 404 and wrong.text == unknown.text


def test_a_code_from_another_pc_is_refused(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    assert _lookup(client, claim_id=OTHER_ID, claim_code=opened['claim_code']).status_code == 404


def test_five_wrong_codes_block_that_account_for_that_id(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    for _ in range(appliance_claims.CLAIM_ID_ACCOUNT_ATTEMPTS):
        assert _lookup(client, claim_id=CLAIM_ID, claim_code=WRONG).status_code == 404
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 404
    _forget('pair:')
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 200


def test_strangers_guessing_at_a_public_id_never_stop_the_owner(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    for _ in range(appliance_claims.CLAIM_ID_ALERT_ATTEMPTS * 2):  # each pass stands for another account
        _forget('pair:', 'account:', 'day:')
        assert _lookup(client, claim_id=CLAIM_ID, claim_code=WRONG).status_code == 404
    _forget('pair:', 'account:', 'day:')
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 200
    with partner_db.connection() as db:
        stored = db.execute('SELECT claim_code_hash FROM appliance_claims').fetchone()['claim_code_hash']
    assert partner_db.verify_password(opened['claim_code'], stored)


def test_guessing_at_one_id_raises_one_audit_event(client, db_path):
    _seeded_customer(db_path)
    _begin(client)
    for _ in range(appliance_claims.CLAIM_ID_ALERT_ATTEMPTS + 3):
        _forget('pair:', 'account:', 'day:')
        _lookup(client, claim_id=CLAIM_ID, claim_code=WRONG)
    with partner_db.connection() as db:
        events = db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='appliance_claim.code_guessing'").fetchone()
    assert events['n'] == 1


def test_one_account_cannot_spread_guesses_over_many_ids(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    for number in range(appliance_claims.CLAIM_CODE_ACCOUNT_ATTEMPTS):
        other = claim_id_for(f'11111111-2222-4333-8444-{number:012d}')
        assert _lookup(client, claim_id=other, claim_code=WRONG).status_code == 404
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 404


def test_code_only_guesses_share_the_account_limit(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    for _ in range(appliance_claims.CLAIM_CODE_ACCOUNT_ATTEMPTS):
        assert _lookup(client, claim_code='00000000').status_code == 404
    assert _lookup(client, claim_code=opened['claim_code']).status_code == 404


def test_right_codes_use_no_attempts(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    for _ in range(appliance_claims.CLAIM_ID_ACCOUNT_ATTEMPTS + 2):
        assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 200


def test_a_counter_starts_again_after_its_window(client, db_path):
    _seeded_customer(db_path)
    assert claim_attempts.attempt('account:window', 60, now=1000) == 1
    assert claim_attempts.attempt('account:window', 60, now=1059) == 2
    assert claim_attempts.attempt('account:window', 60, now=1060) == 1


def test_browser_answers_hold_no_device_uuid(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    found = _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code'])
    assert VALID_DEVICE_ID not in found.text and found.json()['claim_public_id'] == CLAIM_ID
    confirmed = client.post('/api/portal/claims/confirm', json={'claim_id': CLAIM_ID, 'claim_code': opened['claim_code'], 'site_id': 'site-1'},
                            cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})
    assert confirmed.status_code == 200 and VALID_DEVICE_ID not in confirmed.text
    enrollment = client.get('/api/portal/claims/enrollment', params={'claim': found.json()['claim_handle']},
                            cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})
    assert enrollment.status_code == 200 and VALID_DEVICE_ID not in enrollment.text


def test_claim_and_activation_logs_never_name_the_device_uuid():
    folder = Path(appliance_claims.__file__).parent
    for name in ('appliance_claims.py', 'appliance_cloud.py'):
        text = (folder / name).read_text(encoding='utf-8')
        assert not re.search(r'logger\.\w+\(.*(device_id|cloud_id)', text), name


def test_the_page_script_no_longer_reads_a_device_id(client, db_path):
    _seeded_customer(db_path)
    page = client.get('/customer/claim-appliance', cookies={partner_portal.SESSION_COOKIE: _customer_cookie()}).text
    assert 'body.device_id' not in page and 'enrollment?device_id' not in page


WORKER = """
import sys
sys.path.insert(0, sys.argv[1])
import claim_attempts
print(sum(claim_attempts.attempt('account:shared', 3600, now=1000) <= 5 for _ in range(4)))
"""


def _worker(db_path):
    folder = str(Path(appliance_claims.__file__).parent)
    return subprocess.Popen([sys.executable, '-c', WORKER, folder], env={**os.environ, 'ANYAICAM_PARTNER_DB': str(db_path)},
                            cwd=folder, stdout=subprocess.PIPE, text=True)


def test_workers_share_one_limit_and_a_restart_keeps_it(client, db_path):
    _seeded_customer(db_path)
    workers = [_worker(db_path) for _ in range(3)]  # 12 parallel attempts in 3 processes, limit 5
    allowed = [int(worker.communicate(timeout=120)[0]) for worker in workers]
    assert sum(allowed) == 5
    assert all(worker.returncode == 0 for worker in workers)
    again = _worker(db_path)  # a new process, as after a restart
    assert int(again.communicate(timeout=120)[0]) == 0


def test_the_code_check_runs_whether_or_not_a_claim_is_open(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    checked = []
    real = appliance_claims.verify_password
    monkeypatch.setattr(appliance_claims, 'verify_password', lambda *pair: checked.append(pair) or real(*pair))
    _lookup(client, claim_id=OTHER_ID, claim_code=WRONG)
    assert [pair[1] for pair in checked] == [appliance_claims.NO_CLAIM_CODE_HASH]


def test_existing_rows_get_their_claim_id_when_migrations_run(client, db_path):
    _seeded_customer(db_path)
    _begin(client)
    with partner_db.connection() as db:
        db.execute('UPDATE appliance_claims SET claim_public_id=NULL')
    db_migrations.apply_migrations()
    with partner_db.connection() as db:
        assert db.execute('SELECT claim_public_id FROM appliance_claims').fetchone()['claim_public_id'] == CLAIM_ID


def test_code_only_links_from_earlier_windows_versions_still_work(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    assert _lookup(client, claim_code=opened['claim_code']).status_code == 200


def test_limits_follow_the_customer_not_the_email(client, db_path):
    assert appliance_claims._account_of({'email': 'a@example.com', 'customer_id': 'c1'}) == appliance_claims._account_of({'email': 'b@example.com', 'customer_id': 'c1'})
    assert appliance_claims._account_of({'email': 'a@example.com', 'customer_id': 'c1'}) != appliance_claims._account_of({'email': 'a@example.com', 'customer_id': 'c2'})


def test_a_session_with_no_customer_gets_no_attempts(client, db_path):
    _seeded_customer(db_path)
    assert appliance_claims._take_code_attempt(appliance_claims._account_of({'email': 'a@example.com'})) is False
    with partner_db.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM claim_attempt_counters").fetchone()['n'] == 0


def test_the_daily_limit_outlasts_the_hourly_one(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    for _ in range(appliance_claims.CLAIM_CODE_ACCOUNT_DAY_ATTEMPTS):
        _forget('pair:', 'account:')
        assert _lookup(client, claim_id=OTHER_ID, claim_code=WRONG).status_code == 404
    _forget('pair:', 'account:')
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 404
    _forget('day:')
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 200


def test_cloud_wide_guessing_alerts_once_and_blocks_no_one(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    opened = _begin(client)
    monkeypatch.setattr(appliance_claims, 'CLAIM_CODE_CLOUD_ALERT_ATTEMPTS', 3)
    for _ in range(5):
        _forget('pair:', 'account:', 'day:')
        _lookup(client, claim_id=OTHER_ID, claim_code=WRONG)
    _forget('pair:', 'account:', 'day:')
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=opened['claim_code']).status_code == 200
    with partner_db.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='appliance_claim.cloud_code_guessing'").fetchone()['n'] == 1


def test_only_an_agent_that_asks_gets_the_long_code(client, db_path):
    _seeded_customer(db_path)
    short = client.post('/api/appliance/claim/begin', json={'device_id': VALID_DEVICE_ID, 'device_secret': DEVICE}).json()
    assert re.fullmatch(r'[0-9A-F]{8}', short['claim_code'])
    resumed = client.post('/api/appliance/claim/begin', json={'device_id': VALID_DEVICE_ID, 'device_secret': DEVICE, 'long_code': True}).json()
    assert resumed['resumed'] and len(resumed['claim_code']) == 12
    assert appliance_claims.normalize_label_code(resumed['claim_code']) == resumed['claim_code']


def test_the_claim_id_path_refuses_a_short_code(client, db_path):
    _seeded_customer(db_path)
    short = client.post('/api/appliance/claim/begin', json={'device_id': VALID_DEVICE_ID, 'device_secret': DEVICE}).json()
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=short['claim_code']).status_code == 404
    assert _lookup(client, claim_code=short['claim_code']).status_code == 200


def test_a_typed_long_code_tolerates_dashes_and_look_alikes(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client)
    code = opened['claim_code']
    typed = f'{code[:4]}-{code[4:8]} {code[8:]}'.lower().replace('0', 'o').replace('1', 'l')
    assert _lookup(client, claim_id=CLAIM_ID, claim_code=typed).status_code == 200


def test_the_page_accepts_a_long_code_in_the_link(client, db_path):
    _seeded_customer(db_path)
    page = client.get('/customer/claim-appliance', cookies={partner_portal.SESSION_COOKIE: _customer_cookie()}).text
    assert '[0-9A-Z]{12}' in page


def test_an_appliance_audit_actor_is_the_claim_id_not_the_uuid(client, db_path):
    _seeded_customer(db_path)
    partner_db.audit({'email': VALID_DEVICE_ID.upper(), 'role': 'appliance'}, 'x.test')
    partner_db.audit({'email': 'person@example.com', 'role': 'customer_owner'}, 'x.person')
    partner_db.audit({'email': 'AIC-ABCD1234', 'role': 'appliance'}, 'x.admin-code')
    with partner_db.connection() as db:
        actors = {item['action']: item['actor_email'] for item in db.execute('SELECT action,actor_email FROM audit_logs').fetchall()}
    assert actors['x.test'] == CLAIM_ID and actors['x.person'] == 'person@example.com' and actors['x.admin-code'] == 'AIC-ABCD1234'


def test_display_never_turns_a_uuid_into_anything_but_its_claim_id():
    from claim_id import cloud_id_label, display_cloud_id
    assert display_cloud_id(VALID_DEVICE_ID) == display_cloud_id(VALID_DEVICE_ID.upper()) == CLAIM_ID
    assert display_cloud_id('AIC-ABCD1234') == 'AIC-ABCD1234' and display_cloud_id(None) is None
    assert cloud_id_label(VALID_DEVICE_ID) == f'Claim ID {CLAIM_ID}' and cloud_id_label('AIC-ABCD1234') == 'Cloud ID AIC-ABCD1234'


def test_people_facing_sources_show_the_display_form():
    folder = Path(appliance_claims.__file__).parent
    for name, marker in (('partner_workspace.py', "'cloud_id':display_cloud_id(a['cloud_id'])"), ('provisioning_api.py', 'display_cloud_id(item["cloud_id"])'),
                         ('software_license.py', 'display_cloud_id(item["cloud_id"])'), ('software_update.py', 'display_cloud_id(row["cloud_id"])'),
                         ('build_orders.py', 'display_cloud_id(item["cloud_id"])'), ('live_view_page.py', 'escape(display_cloud_id('),
                         ('appliance_cloud.py', "escape(display_cloud_id(item['cloud_id']))"), ('cloud_features.py', "display_cloud_id(item['actor_email'])")):
        assert marker in (folder / name).read_text(encoding='utf-8'), name


def test_the_customer_read_model_shows_a_claimed_appliance_by_its_claim_id(client, db_path):
    _seeded_customer(db_path)
    with partner_db.connection() as db:
        customer = db.execute('SELECT id FROM customers').fetchone()['id']
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,activated_via) VALUES('a1',?,'site-1',?,'2026-10-31','claim')",
                   (customer, VALID_DEVICE_ID.upper()))
    import software_license
    shown = software_license.customer_appliances(customer)
    assert [item['cloud_id'] for item in shown] == [CLAIM_ID]


def test_the_local_activation_page_shows_the_claim_id_not_the_uuid():
    text = (Path(appliance_claims.__file__).parent / 'main.py').read_text(encoding='utf-8')
    assert '<strong>{escape(display_cloud_id(identity["cloud_id"]))}</strong>' in text
    assert '<span>Cloud ID</span><strong>{escape(identity["cloud_id"])}</strong>' not in text


def test_every_module_that_shows_the_claim_id_really_imports_the_helper():
    import ast
    folder = Path(appliance_claims.__file__).parent
    for name in ('partner_workspace', 'provisioning_api', 'software_license', 'software_license_panels', 'software_update',
                 'build_orders', 'live_view_page', 'appliance_cloud', 'cloud_features', 'partner_db'):
        tree = ast.parse((folder / f'{name}.py').read_text(encoding='utf-8'))
        imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == 'claim_id' for alias in node.names]
        assert 'display_cloud_id' in imported, name
