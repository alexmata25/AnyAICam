"""Stored audit rows hold no device UUID, and 32-bit claim codes have one budget
across all accounts. Stored rows and real routes, not rendered output or source
text, except where a test is about the source.
"""
import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import appliance_claims
import partner_db
import partner_portal
from claim_id import claim_id_for

from test_appliance_claims import (  # noqa: F401 -- fixtures
    DEVICE_SECRET,
    VALID_DEVICE_ID,
    _claim_through_to_proof,
    _customer_cookie,
    _seeded_customer,
    claim_flow_key,
    client,
    db_path,
    identity_file,
)

REPO = Path(appliance_claims.__file__).parents[1]
CLOUD_ID = VALID_DEVICE_ID.upper()
CLAIM_ID = claim_id_for(VALID_DEVICE_ID)
LONG_GUESS = '000000000000'
SHORT_GUESS = 'FFFFFFFF'


def _uuid_in(db_path):
    """Every audit row, every column, in either case."""
    conn = sqlite3.connect(db_path)
    found = [item for item in conn.execute('SELECT * FROM audit_logs').fetchall()
             if VALID_DEVICE_ID in ' '.join(str(cell) for cell in item).lower()]
    conn.close()
    return found


def _complete(client, db_path):
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    answer = client.post('/api/appliance/claim/complete', json={
        'device_secret': DEVICE_SECRET, 'claim_session_id': claim_session_id, 'claim_proof': claim_proof})
    assert answer.status_code == 200
    return answer.json()


def _account(number):
    return {'email': f'owner{number}@example.com', 'role': 'customer_owner', 'customer_id': f'cust-{number}'}


def _begin(client, **extra):
    answer = client.post('/api/appliance/claim/begin', json={'device_id': VALID_DEVICE_ID, 'device_secret': DEVICE_SECRET, **extra})
    assert answer.status_code == 200
    return answer.json()


def _spent():
    found = partner_db.row('SELECT attempts FROM claim_attempt_counters WHERE scope=?', (appliance_claims.SHORT_CODE_CLOUD_SCOPE,))
    return found['attempts'] if found else 0


def _find(identity, **payload):
    return appliance_claims._find_claim_for_portal(payload, identity)


def _budget(monkeypatch, limit):
    monkeypatch.setattr(appliance_claims, 'SHORT_CODE_CLOUD_ATTEMPTS', limit)


def _spend_it(monkeypatch):
    _budget(monkeypatch, 1)
    for _ in range(2):
        assert _find(_account(90), claim_code=SHORT_GUESS) is None


# ---------------------------------------------------------------- audit storage
def test_a_completed_claim_stores_the_claim_id_as_its_actor(client, db_path):
    answer = _complete(client, db_path)
    assert answer['cloud_id'] == CLOUD_ID  # the device still gets its real identity
    stored = partner_db.row("SELECT actor_email,actor_role FROM audit_logs WHERE action='appliance_claim.completed'")
    assert stored == {'actor_email': CLAIM_ID, 'actor_role': 'appliance'}
    assert _uuid_in(db_path) == []


def test_claim_activation_stores_no_uuid_in_any_audit_column(client, db_path, monkeypatch):
    answer = _complete(client, db_path)
    monkeypatch.setattr('appliance_readiness.enrollment_ready', lambda appliance_id: True)
    assert appliance_claims.record_claim_activation(answer['appliance_id'])
    assert partner_db.row("SELECT actor_email FROM audit_logs WHERE action='appliance.activated'")['actor_email'] == CLAIM_ID
    assert _uuid_in(db_path) == []


def test_claim_completion_hands_audit_the_claim_id_already_masked(client, db_path, monkeypatch):
    seen = []
    monkeypatch.setattr(appliance_claims, 'audit', lambda actor, *rest: seen.append(actor))
    _complete(client, db_path)
    assert {'email': CLAIM_ID, 'role': 'appliance'} in seen


def test_no_direct_audit_insert_takes_a_raw_cloud_id():
    """audit() is the one mask, so a direct INSERT must carry its own."""
    for path in sorted(REPO.joinpath('app').glob('*.py')):
        lines = path.read_text(encoding='utf-8').splitlines()
        for index, line in enumerate(lines):
            if 'INSERT INTO audit_logs' in line:
                nearby = '\n'.join(lines[index:index + 4])
                assert 'cloud_id' not in nearby or 'display_cloud_id' in nearby, f'{path.name}:{index + 1}'


# ---------------------------------------------------------------- the shared budget
def test_many_accounts_share_one_budget_for_short_codes(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    opened = _begin(client)
    assert re.fullmatch(r'[0-9A-F]{8}', opened['claim_code'])
    _budget(monkeypatch, 12)
    for number in range(5):  # 20 guesses, each account well under its own limit
        for _ in range(4):
            assert _find(_account(number), claim_code=SHORT_GUESS) is None
    assert _spent() == 20
    # Past the budget nothing is looked up, so even the right code gets no answer.
    assert _find(_account(6), claim_code=opened['claim_code']) is None
    assert partner_db.row("SELECT COUNT(*) AS n FROM audit_logs WHERE action='appliance_claim.short_code_budget_spent'")['n'] == 1


def test_under_the_budget_a_short_code_still_links_an_older_agent(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    opened = _begin(client)
    _budget(monkeypatch, 12)
    assert _find(_account(1), claim_code=SHORT_GUESS) is None
    assert _find(_account(2), claim_code=opened['claim_code'])['device_id'] == VALID_DEVICE_ID


def test_a_correct_short_code_costs_the_budget_nothing(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    opened = _begin(client)
    _budget(monkeypatch, 1)
    for _ in range(4):  # lookup and confirm both present the code
        assert _find(_account(1), claim_code=opened['claim_code'])
    assert _spent() == 0


def test_an_account_past_its_own_limit_spends_no_more_of_the_budget(client, db_path):
    _seeded_customer(db_path)
    for _ in range(appliance_claims.CLAIM_CODE_ACCOUNT_ATTEMPTS + 5):
        _find(_account(1), claim_code=SHORT_GUESS)
    assert _spent() == appliance_claims.CLAIM_CODE_ACCOUNT_ATTEMPTS


def test_long_guesses_never_spend_the_budget(client, db_path):
    _seeded_customer(db_path)
    _begin(client)
    assert _find(_account(1), claim_code=LONG_GUESS) is None
    assert _find(_account(1), label_code=LONG_GUESS) is None
    assert _spent() == 0


def test_a_spent_budget_leaves_long_codes_claim_ids_and_labels_working(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    label = 'A1B2C3D4E5F6'
    opened = _begin(client, long_code=True, label_verifier=appliance_claims.label_verifier(label))
    _spend_it(monkeypatch)
    assert _find(_account(1), claim_code=opened['claim_code'])
    assert _find(_account(2), claim_id=CLAIM_ID, claim_code=opened['claim_code'])
    assert _find(_account(3), label_code=label)['matched_label']


def test_the_budget_comes_back_after_its_window(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    _spend_it(monkeypatch)
    later = appliance_claims._now() + timedelta(seconds=appliance_claims.CLAIM_CODE_ACCOUNT_WINDOW_SECONDS + 1)
    monkeypatch.setattr(appliance_claims, '_now', lambda: later)
    opened = _begin(client)
    assert _find(_account(1), claim_code=opened['claim_code'])


def test_the_budget_answers_exactly_like_a_wrong_code(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    opened = _begin(client)
    cookies = {partner_portal.SESSION_COOKIE: _customer_cookie()}
    wrong = client.post('/api/portal/claims/lookup', json={'claim_code': SHORT_GUESS}, cookies=cookies)
    _spend_it(monkeypatch)
    blocked = client.post('/api/portal/claims/lookup', json={'claim_code': opened['claim_code']}, cookies=cookies)
    assert (wrong.status_code, wrong.json()) == (blocked.status_code, blocked.json()) == (404, {'detail': 'Claim code not found or expired.'})


# ---------------------------------------------------------------- migration
@pytest.mark.parametrize('extra', [{}, {'clear_label_verifier': True}, {'label_verifier': 'a' * 64}],
                         ids=['terminal', 'windows-on-demand', 'linux-label'])
def test_a_shipped_agent_keeps_the_eight_characters_it_requires(client, db_path, extra):
    _seeded_customer(db_path)
    code = _begin(client, **extra)['claim_code']
    assert re.fullmatch(r'^[0-9A-Z]{8}$', code)  # headless_claim.CLAIM_CODE_PATTERN as shipped


def test_a_long_code_typed_on_the_page_confirms_through_the_label_field(client, db_path):
    """The page sends any 12 characters as label_code."""
    _seeded_customer(db_path)
    code = _begin(client, long_code=True)['claim_code']
    cookies = {partner_portal.SESSION_COOKIE: _customer_cookie()}
    assert client.post('/api/portal/claims/lookup', json={'label_code': code}, cookies=cookies).status_code == 200
    answer = client.post('/api/portal/claims/confirm', json={'label_code': code, 'site_id': 'site-1'}, cookies=cookies)
    assert answer.status_code == 200 and answer.json()['status'] == 'claimed'


def test_a_real_label_still_confirms_on_its_own_column(client, db_path):
    _seeded_customer(db_path)
    label = 'A1B2C3D4E5F6'
    _begin(client, label_verifier=appliance_claims.label_verifier(label))
    answer = client.post('/api/portal/claims/confirm', json={'label_code': label, 'site_id': 'site-1'},
                         cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})
    assert answer.status_code == 200


def test_a_wrong_label_now_costs_an_account_attempt(client, db_path):
    _seeded_customer(db_path)
    for _ in range(appliance_claims.CLAIM_CODE_ACCOUNT_ATTEMPTS):
        assert _find(_account(1), label_code=LONG_GUESS) is None
    label = 'A1B2C3D4E5F6'
    _begin(client, label_verifier=appliance_claims.label_verifier(label))
    assert _find(_account(1), label_code=label)  # the label itself still gets through
    assert partner_db.row("SELECT attempts FROM claim_attempt_counters WHERE scope='account:cust-1'")['attempts'] == 20


# ---------------------------------------------------------------- recovery and identity
def test_an_existing_device_is_refused_with_words_agents_still_match(client, db_path):
    _complete(client, db_path)
    answer = client.post('/api/appliance/claim/begin', json={'device_id': VALID_DEVICE_ID, 'device_secret': DEVICE_SECRET})
    assert answer.status_code == 409
    assert 'already provisioned' in answer.json()['detail'] and 'contact support' in answer.json()['detail']


@pytest.mark.parametrize('device_id', ['4f3b2a10-9c8d-1e7f-a6b5-0123456789ab', '4f3b2a10-9c8d-4e7f-06b5-0123456789ab', 'AIC-12345678'],
                         ids=['version-1', 'wrong-variant', 'admin-code'])
def test_claim_begin_takes_only_the_v4_form(client, db_path, device_id):
    answer = client.post('/api/appliance/claim/begin', json={'device_id': device_id, 'device_secret': DEVICE_SECRET})
    assert answer.status_code == 400


def test_every_generator_of_a_device_identity_is_the_os_random_source():
    linux = REPO.joinpath('installer', '09-identity.sh').read_text(encoding='utf-8')
    windows = REPO.joinpath('installer', 'windows', 'cloud-link.ps1').read_text(encoding='utf-8')
    assert 'appliance_id="$(cat /proc/sys/kernel/random/uuid)"' in linux
    assert '$applianceId = [guid]::NewGuid().ToString().ToLowerInvariant()' in windows
    agent = ''.join(path.read_text(encoding='utf-8') for path in REPO.joinpath('appliance-agent', 'anyaicam_agent').rglob('*.py'))
    assert not re.search(r'uuid\.uuid[135]\(', agent)
    assert not re.search(r"\bappliance_id['\"]?\s*[:=]\s*[^,\n]*(MachineGuid|hostname|getnode)", agent)
