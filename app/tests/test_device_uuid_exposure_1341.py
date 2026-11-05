"""A claimed appliance's cloud_id is its device UUID, and claim/begin accepts any
unclaimed UUID with a caller-chosen device secret, so it stays out of pages, API
answers, logs and support bundles. Admin-assigned AIC- codes are not sensitive and
must keep working. Real routes on the real app, except where a test says otherwise.
"""
import ast
import io
import json
import logging
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import access_log_redaction
import appliance_activation
import appliance_identity
import cloud_features
import main
import partner_portal
from claim_id import claim_id_for
from partner_db import connection
from test_camera_slot_enforcement import (  # noqa: F401 -- fixtures and helpers
    _seed_and_activate_appliance,
    _seed_tenant,
    client,
    db_path,
)

DEVICE = '4f3b2a10-9c8d-4e7f-a6b5-0123456789ab'
CLOUD_ID = DEVICE.upper()
CLAIM_ID = claim_id_for(DEVICE)
RECOVER = '/api/customer/appliances/appl-1/activation-token/recover'
APP = Path(main.__file__).parent
AGENT = APP.parent / 'appliance-agent' / 'anyaicam_agent'


def _owner(client):
    client.cookies.set(partner_portal.SESSION_COOKIE,
                       partner_portal._token('slot-test@example.test', 'customer_owner', None, 'cust-1', None))
    return client


def _claimed(db_path):
    _seed_tenant(db_path)
    _seed_and_activate_appliance(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliances SET cloud_id=?,activated_via='claim',activation_token_hash='hash-marker',"
                 "capacity_identity_transition_json=? WHERE id='appl-1'",
                 (CLOUD_ID, json.dumps({'from_identity': {'appliance_id': DEVICE}})))
    conn.commit()
    conn.close()


def _absent(text):
    assert DEVICE not in text.lower()


def _identity(monkeypatch, cloud_id=CLOUD_ID):
    monkeypatch.setattr(main, 'own_appliance_identity', lambda: {'appliance_id': 'appl-1', 'cloud_id': cloud_id, 'credential': 'c'})


def _tokens(db_path):
    return sqlite3.connect(db_path).execute('SELECT id,revoked_at FROM appliance_activation_tokens ORDER BY id').fetchall()


def test_token_recovery_refuses_a_claimed_appliance_and_changes_nothing(client, db_path):
    _claimed(db_path)
    before = _tokens(db_path)
    answer = _owner(client).post(RECOVER)
    assert answer.status_code == 409
    _absent(answer.text)
    assert _tokens(db_path) == before


def test_a_uuid_shaped_cloud_id_is_refused_even_without_the_claim_marker(client, db_path):
    _claimed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliances SET activated_via=NULL WHERE id='appl-1'")
    conn.commit()
    conn.close()
    assert _owner(client).post(RECOVER).status_code == 409


def test_token_recovery_still_works_for_an_admin_cloud_id(client, db_path):
    _seed_tenant(db_path)
    _seed_and_activate_appliance(db_path)
    body = _owner(client).post(RECOVER).json()
    assert body['cloud_id'] == 'AIC-SLOT1' and body['provisioning_qr_payload'] == f"AIC-SLOT1|{body['activation_token']}"


def test_already_provisioned_shows_the_claim_id_and_offers_no_recovery(client, db_path):
    _claimed(db_path)
    answer = _owner(client).post('/api/customer/appliances/provision', json={})
    _absent(answer.text)
    body = answer.json()
    assert body['status'] == 'already_provisioned' and body['cloud_id'] == CLAIM_ID and body['activation_pending'] is False


def test_the_setup_page_holds_only_the_fields_its_script_reads(client, db_path):
    _claimed(db_path)
    page = _owner(client).get('/customer/setup', params={'step': 3, 'appliance': 'appl-1'})
    assert page.status_code == 200
    _absent(page.text)
    assert 'hash-marker' not in page.text and 'capacity_identity_transition_json' not in page.text
    assert 'activation_token_hash' not in page.text and CLAIM_ID in page.text


def test_the_setup_page_keeps_an_admin_cloud_id_and_what_its_script_needs(client, db_path):
    _seed_tenant(db_path)
    _seed_and_activate_appliance(db_path)
    text = _owner(client).get('/customer/setup', params={'step': 3, 'appliance': 'appl-1'}).text
    embedded = json.loads(re.search(r'appliances=(\[.*?\]);function selectedAppliance', text).group(1))
    assert embedded[0]['id'] == 'appl-1' and embedded[0]['cloud_id'] == 'AIC-SLOT1'
    assert set(embedded[0]) == {'id', 'cloud_id', 'site_id', 'software_version', 'online_status', 'last_check_in',
                                'enrollment_ready', 'site_name'}


def test_two_appliances_are_told_apart_without_the_uuid(client, db_path):
    _claimed(db_path)
    _seed_and_activate_appliance(db_path, appliance_id='appl-2', cloud_id='AIC-SLOT2')
    page = _owner(client).get('/customer/setup').text
    assert f'ending {CLOUD_ID[-4:]}' not in page and f'ending {CLAIM_ID[-4:]}' in page


def test_the_unauthenticated_version_route_holds_no_identity(client, monkeypatch):
    _identity(monkeypatch)
    client.cookies.clear()
    answer = client.get('/version')
    _absent(answer.text)
    assert CLAIM_ID not in answer.text and set(answer.json()) == {'product', 'version', 'build_id'}


def test_the_build_identifier_behind_the_staff_badge_shows_the_claim_id(monkeypatch):
    _identity(monkeypatch)
    assert main.build_identifier()['cloud_id'] == CLAIM_ID


def test_a_support_bundle_holds_no_device_uuid(client, monkeypatch):
    _identity(monkeypatch)
    with zipfile.ZipFile(io.BytesIO(main.build_support_bundle({'email': 'staff@example.test'}))) as bundle:
        names = bundle.namelist()
        assert 'version.json' in names
        for name in names:
            _absent(bundle.read(name).decode('utf-8', 'replace'))


def test_an_activation_conflict_does_not_answer_with_the_uuid(client, db_path, monkeypatch):
    _seed_tenant(db_path)
    token = _seed_and_activate_appliance(db_path)
    monkeypatch.setattr(appliance_activation, 'local_activation_tracking_applies', lambda: True)
    monkeypatch.setattr(appliance_activation, 'load_persisted_identity', lambda: {'cloud_id': CLOUD_ID})
    client.cookies.clear()
    answer = client.post('/api/appliance/activate', json={'cloud_id': 'AIC-SLOT1', 'activation_token': token})
    assert answer.status_code == 409
    _absent(answer.text)


def test_the_stored_identity_conflict_message_holds_no_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(appliance_activation, 'ACTIVATION_IDENTITY_FILE', tmp_path / 'identity.json')
    monkeypatch.setattr(appliance_activation, 'load_persisted_identity', lambda: {'cloud_id': CLOUD_ID, 'activation_version': 1})
    with pytest.raises(appliance_activation.ActivationConflict) as refused:
        appliance_activation.persist_activation(appliance_id='a', cloud_id='AIC-OTHER', credential='c', customer_id='x', site_id='y', partner_id=None)
    _absent(str(refused.value))
    assert CLAIM_ID in str(refused.value)


def test_the_cloud_identity_refusals_hold_no_uuid(client, db_path):
    with connection() as db:
        for call in (lambda: appliance_identity.build_manifest(db, cloud_id=CLOUD_ID),
                     lambda: appliance_identity.authenticate_operator(db, email='a@example.test', password='x', portal='customer', cloud_id=CLOUD_ID)):
            with pytest.raises(ValueError) as refused:
                call()
            _absent(str(refused.value))


def test_the_manifest_fetch_answers_with_the_claim_id(client, tmp_path, monkeypatch):
    class Backend:
        def fetch_manifest(self, *, cloud_id):
            assert cloud_id == CLOUD_ID  # the cloud is still called with the real value
            return {'appliance': {'cloud_id': cloud_id}, 'identities': [], 'manifest_version': 1}
    _identity(monkeypatch)
    monkeypatch.setattr(appliance_identity, 'get_cloud_identity_backend', Backend)
    monkeypatch.setattr(main, 'USERS_FILE', tmp_path / 'users.json')
    monkeypatch.setattr(main, 'SESSIONS_FILE', tmp_path / 'sessions.json')
    main.save_users([{'id': 'admin-1', 'email': 'admin@example.test', 'role': 'administrator', 'enabled': True, 'camera_ids': []}])
    answer = client.post('/api/operations/appliance-identity/fetch-manifest', cookies={main.SESSION_COOKIE_NAME: main.create_session('admin-1')})
    assert answer.status_code == 200, answer.text
    assert answer.json()['cloud_id'] == CLAIM_ID


@pytest.mark.parametrize('path', [
    f'/api/appliance/{CLOUD_ID}/scan-jobs',
    f'/api/appliance/{DEVICE}/cameras/cam-1/media-uri',
    f'/api/appliance-legacy/{CLOUD_ID}/scan-jobs/job-1',
    f'/api/appliance/{CLOUD_ID}/identity-manifest?x=1',
    f'/api/appliance/{CLOUD_ID}',
])
def test_access_log_lines_show_the_claim_id(path):
    cleaned = access_log_redaction.redact_url(path)
    _absent(cleaned)
    assert CLAIM_ID in cleaned


def test_access_log_redaction_still_hides_secrets_and_leaves_other_paths():
    assert access_log_redaction.redact_url(f'/api/appliance/{CLOUD_ID}/x?token=abc') == f'/api/appliance/{CLAIM_ID}/x?token=<redacted>'
    assert access_log_redaction.redact_url('/api/appliance/AIC-SLOT1/scan-jobs') == '/api/appliance/AIC-SLOT1/scan-jobs'
    assert access_log_redaction.redact_url(f'/api/cameras/{DEVICE}') == f'/api/cameras/{DEVICE}'
    assert access_log_redaction.redact_url(7) == 7


def test_the_access_log_filter_rewrites_a_real_record():
    record = logging.LogRecord('uvicorn.access', logging.INFO, 'x', 1, '%s "%s %s HTTP/%s" %d',
                               ('10.0.0.1:1', 'GET', f'/api/appliance/{CLOUD_ID}/scan-jobs', '1.1', 200), None)
    assert access_log_redaction.RedactQuerySecretsFilter().filter(record)
    _absent(record.getMessage())


LOG_CALLS = {'debug', 'info', 'warning', 'error', 'exception', 'critical', 'structured_log'}


def _raw_cloud_ids(node):
    """Mentions of a cloud_id in this expression outside display_cloud_id(...)."""
    if isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'display_cloud_id':
        return []
    found = []
    if isinstance(node, ast.Constant) and node.value == 'cloud_id':
        found.append(node)
    if isinstance(node, ast.Attribute) and node.attr == 'cloud_id':
        found.append(node)
    if isinstance(node, ast.Name) and node.id == 'cloud_id':
        found.append(node)
    for child in ast.iter_child_nodes(node):
        found += _raw_cloud_ids(child)
    return found


def _sources():
    yield from (p for p in APP.glob('*.py') if not p.name.startswith('main_v'))
    yield from AGENT.glob('*.py')


def test_no_log_call_names_a_raw_cloud_id():
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'attr', getattr(node.func, 'id', ''))
            if name in LOG_CALLS and any(_raw_cloud_ids(arg) for arg in [*node.args, *[k.value for k in node.keywords]]):
                offenders.append(f'{path.name}:{node.lineno}')
    assert offenders == []


def test_no_structured_log_field_is_named_cloud_id():
    tree = ast.parse((APP / 'main.py').read_text(encoding='utf-8'))
    named = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'structured_log'
             and any(k.arg in ('cloud_id', 'device_id') for k in node.keywords)]
    assert named == []


def test_no_page_escapes_a_raw_cloud_id():
    pattern = re.compile(r'''escape\(\s*(?:str\()?\w+(?:\.get\(|\[)["']cloud_id''')
    offenders = []
    for path in APP.glob('*.py'):
        if path.name.startswith('main_v') or path.name in ('fulfillment_admin.py', 'software_license_panels.py'):  # their rows arrive masked
            continue
        offenders += [f'{path.name}:{n}' for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1) if pattern.search(line)]
    assert offenders == []


def test_the_reset_audit_note_shows_the_claim_id():
    assert '''{display_cloud_id(previous['cloud_id'])!r}''' in (APP / 'main.py').read_text(encoding='utf-8')


def test_a_revoked_credential_gets_no_token_and_is_sent_to_support(client, db_path):
    _claimed(db_path)
    with connection() as db:
        db.execute('UPDATE appliance_credentials SET revoked_at=? WHERE appliance_id=?', ('2026-10-31T00:00:00', 'appl-1'))
    before = _tokens(db_path)
    answer = _owner(client).post(RECOVER)
    assert answer.status_code == 409 and 'contact support' in answer.json()['detail']
    assert 'activation_token' not in answer.text and _tokens(db_path) == before


def test_the_setup_page_names_a_claim_id_as_a_claim_id(client, db_path):
    _claimed(db_path)
    page = _owner(client).get('/customer/setup', params={'step': 3, 'appliance': 'appl-1'}).text
    assert "startsWith('CID-')?'Claim ID':'Cloud ID'" in page


def test_the_export_still_masks_a_row_written_before_actors_were_masked(client, db_path, monkeypatch):
    _seed_tenant(db_path)
    with connection() as db:
        db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
                   "VALUES(?,'appliance','old.row','appliance','appl-1','{}','2026-01-01T00:00:00')", (CLOUD_ID,))
    monkeypatch.setattr(cloud_features, 'require_partner_access',
                        lambda *args, **kwargs: {'email': 'admin@example.com', 'role': 'administrator'})
    # On its own, past the admin session middleware: the route's masking is the point.
    route = next(item for item in main.app.routes if getattr(item, 'path', '') == '/api/admin/audit-export')
    bare = FastAPI()
    bare.add_api_route('/export', route.endpoint)
    answer = TestClient(bare).get('/export', params={'format': 'json'})
    assert answer.status_code == 200 and CLAIM_ID in answer.text
    _absent(answer.text)
