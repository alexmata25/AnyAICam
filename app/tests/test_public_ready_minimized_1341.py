"""/ready answers ready or not and no more, without changing the status code or
the text installers and updates match. Full detail stays behind sign-in.
"""
import copy
import re

import pytest

import main
from test_camera_slot_enforcement import client, db_path  # noqa: F401 -- fixtures

SECRET_PATH = 'C:/customer/recordings-folder'
SNAPSHOT = {
    'ready': True,
    'environment': 'production',
    'runtime_role': 'edge',
    'self_test': {
        'ok': True,
        'checks': [{'name': 'ffmpeg_available', 'ok': True, 'detail': SECRET_PATH, 'critical': True}],
        'configuration_issues': [{'key': 'SES_SENDER', 'severity': 'warning', 'message': 'SES_SENDER is missing.'}],
        'checked_at': '2026-10-31T18:00:00',
    },
    'cloud': {'email_last_delivery': 'customer@example.com', 'cloud_storage_class': 'GLACIER_IR', 'force_https': True},
    'cloud_upload': {'enabled': True, 'last_error': 'bucket customer-footage denied'},
    'cameras_online': 7,
    'cameras_total': 9,
    'recording_workers': 7,
    'checked_at': '2026-10-31T18:00:00',
}
# The matchers in installer/validate.sh and appliance-agent/system/apply_release.py.
VALIDATE_TEXT = '"self_test":{"ok":true'
APPLY_RELEASE = re.compile(r'"self_test"\s*:\s*\{\s*"ok"\s*:\s*true')


def _state(monkeypatch, *, ready=True, ok=True):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot['ready'], snapshot['self_test']['ok'] = ready, ok
    monkeypatch.setattr(main, 'readiness_snapshot', lambda: snapshot)


def _ready(client):
    client.cookies.clear()
    return client.get('/ready')


@pytest.mark.parametrize('ready,ok,status', [(True, True, 200), (False, True, 503), (False, False, 503)])
def test_the_body_and_status_for_each_state(client, monkeypatch, ready, ok, status):
    _state(monkeypatch, ready=ready, ok=ok)
    answer = _ready(client)
    assert answer.status_code == status
    assert answer.json() == {'ready': ready, 'self_test': {'ok': ok}}


def test_a_ready_true_with_a_failed_self_test_keeps_the_snapshots_answer(client, monkeypatch):
    _state(monkeypatch, ready=True, ok=False)
    answer = _ready(client)
    assert answer.status_code == 200 and answer.json()['self_test'] == {'ok': False}


@pytest.mark.parametrize('ready', [True, False])
def test_installers_and_updates_still_see_a_passing_self_test(client, monkeypatch, ready):
    _state(monkeypatch, ready=ready, ok=True)
    text = _ready(client).text
    assert VALIDATE_TEXT in text and APPLY_RELEASE.search(text)


def test_installers_and_updates_still_see_a_failing_self_test(client, monkeypatch):
    _state(monkeypatch, ready=False, ok=False)
    text = _ready(client).text
    assert VALIDATE_TEXT not in text and not APPLY_RELEASE.search(text)


@pytest.mark.parametrize('value', [SECRET_PATH, 'customer@example.com', 'GLACIER_IR', 'customer-footage',
                                   'SES_SENDER', 'ffmpeg_available', 'production', 'cameras', 'recording_workers',
                                   'checked_at', 'cloud'])
def test_operational_detail_is_not_in_the_text(client, monkeypatch, value):
    _state(monkeypatch)
    assert value not in _ready(client).text


def test_a_query_header_or_forged_cookie_cannot_widen_it(client, monkeypatch):
    _state(monkeypatch)
    client.cookies.clear()
    client.cookies.set('anyaicam_session', 'forged-value')
    answer = client.get('/ready', params={'debug': '1', 'full': 'true'}, headers={'X-Forwarded-For': '127.0.0.1'})
    assert answer.json() == {'ready': True, 'self_test': {'ok': True}}


def test_the_real_snapshot_also_reduces_to_the_two_fields(client):
    client.cookies.clear()
    answer = client.get('/ready')
    assert answer.status_code in (200, 503) and set(answer.json()) == {'ready', 'self_test'}
    assert set(answer.json()['self_test']) == {'ok'}


def test_the_snapshot_keeps_its_detail_for_signed_in_pages():
    snapshot = main.readiness_snapshot()
    assert {'checks', 'configuration_issues'} <= set(snapshot['self_test'])
    assert {'cloud', 'cloud_upload', 'cameras_total', 'recording_workers'} <= set(snapshot)


def test_diagnostics_refuses_a_signed_out_request(client, monkeypatch):
    _state(monkeypatch)
    client.cookies.clear()
    answer = client.get('/api/operations/diagnostics', follow_redirects=False)
    assert 'readiness' not in answer.text and SECRET_PATH not in answer.text


def test_diagnostics_refuses_a_user_without_manage_settings(monkeypatch):
    _state(monkeypatch)
    monkeypatch.setattr(main, 'current_user', lambda request: {'id': 'viewer', 'role': 'viewer'})
    monkeypatch.setattr(main, 'has_permission', lambda user, permission: False)
    answer = main.operations_diagnostics(_REQUEST)
    assert 'diagnostics' not in answer and SECRET_PATH not in str(answer)


def test_diagnostics_gives_an_administrator_the_detail(monkeypatch):
    _state(monkeypatch)
    monkeypatch.setattr(main, 'current_user', lambda request: {'id': 'admin', 'role': 'admin'})
    monkeypatch.setattr(main, 'has_permission', lambda user, permission: permission == 'manage_settings')
    readiness = main.operations_diagnostics(_REQUEST)['diagnostics']['readiness']
    assert readiness['self_test']['checks'][0]['detail'] == SECRET_PATH


def test_the_operations_page_links_diagnostics_and_not_the_public_route():
    source = main.Path(main.__file__).read_text(encoding='utf-8')
    assert 'href="/api/operations/diagnostics"' in source and 'href="/ready"' not in source


class _Request:
    cookies, headers = {}, {}


_REQUEST = _Request()
