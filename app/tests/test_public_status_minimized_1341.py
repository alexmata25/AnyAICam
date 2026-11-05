"""Routes open without sign-in say nothing that identifies a machine. /version
is the release and no more; the sweep calls every parameterless GET route with no
session, so a field that reappears elsewhere is caught.
"""
import json
import re

import pytest

import cloud_features
import main
import partner_portal
from claim_id import claim_id_for
from test_camera_slot_enforcement import client, db_path  # noqa: F401 -- fixtures

DEVICE = '4f3b2a10-9c8d-4e7f-a6b5-0123456789ab'
REMOVED = {'cloud_id', 'hostname', 'environment', 'runtime_role', 'aws_region', 'started_at', 'uptime_seconds'}
HOSTNAME = 'customer-office-pc'


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


@pytest.fixture
def identified(monkeypatch):
    monkeypatch.setattr(main, 'own_appliance_identity', lambda: {'cloud_id': DEVICE.upper()})
    monkeypatch.setattr(main, 'appliance_hostname', lambda: HOSTNAME)
    monkeypatch.setattr(main, 'AWS_REGION', 'us-east-1')


def _anonymous(client, path):
    client.cookies.clear()
    return client.get(path)


def test_version_is_exactly_the_three_public_fields(client, identified):
    answer = _anonymous(client, '/version')
    assert answer.status_code == 200
    assert answer.json() == {'product': 'AnyAiCam VMS', 'version': main.APP_VERSION, 'build_id': main.BUILD_ID}


@pytest.mark.parametrize('value', [HOSTNAME, DEVICE, DEVICE.upper(), claim_id_for(DEVICE), 'us-east-1'])
def test_version_text_holds_none_of_the_values(client, identified, value):
    assert value not in _anonymous(client, '/version').text


def test_a_query_or_header_cannot_widen_version(client, identified):
    client.cookies.clear()
    answer = client.get('/version', params={'debug': '1', 'full': 'true'}, headers={'X-Forwarded-For': '127.0.0.1'})
    assert set(answer.json()) == {'product', 'version', 'build_id'}


def test_version_says_nothing_more_to_a_forged_session_cookie(client, identified):
    client.cookies.clear()
    client.cookies.set('anyaicam_session', 'forged-value')
    assert set(client.get('/version').json()) == {'product', 'version', 'build_id'}


def test_health_keeps_what_load_balancers_and_deploys_read(client, identified):
    body = _anonymous(client, '/health').json()
    assert body['status'] == 'ok' and body['build_id'] == main.BUILD_ID and body['version'] == main.APP_VERSION
    assert not REMOVED & set(body)


def test_ready_drops_the_identity(client, identified):
    assert not REMOVED & set(_keys(_anonymous(client, '/ready').json()))


def test_the_readiness_snapshot_itself_keeps_its_fields_for_signed_in_pages():
    snapshot = main.readiness_snapshot()
    assert snapshot['environment'] == main.DEPLOYMENT_ENV and snapshot['cloud']['runtime_role'] == main.RUNTIME_ROLE


def test_the_staff_badge_source_keeps_its_identity(identified):
    identifier = main.build_identifier()
    assert identifier['hostname'] == HOSTNAME and identifier['cloud_id'] == claim_id_for(DEVICE)


def test_a_signed_out_page_shows_only_the_version_badge(client, identified):
    page = _anonymous(client, '/partner-login')
    assert page.status_code == 200 and f'<div class="sidebar-build-badge">v{main.APP_VERSION}</div>' in page.text
    assert HOSTNAME not in page.text and claim_id_for(DEVICE) not in page.text and main.BUILD_ID + ' ·' not in page.text


def test_staff_still_see_the_full_badge_and_customers_do_not(client, identified, monkeypatch):
    monkeypatch.setattr(partner_portal, 'partner_identity', lambda request: {'email': 'staff@example.com', 'role': 'administrator'})
    assert HOSTNAME in _anonymous(client, '/partner-login').text
    monkeypatch.setattr(partner_portal, 'partner_identity', lambda request: {'email': 'owner@example.com', 'role': 'customer_owner'})
    assert HOSTNAME not in _anonymous(client, '/partner-login').text


def test_health_ready_keeps_its_checks_and_drops_the_environment(client, identified):
    body = _anonymous(client, '/health/ready').json()
    assert body['status'] == 'ready' and body['checks'] and 'environment' not in body


def test_a_failing_health_ready_drops_it_too(client, identified, monkeypatch):
    monkeypatch.setattr(cloud_features, 'deployment_status', lambda: {
        'status': 'degraded', 'environment': 'production', 'checks': {'configuration': 'failed', 'database': 'ok'}})
    answer = _anonymous(client, '/health/ready')
    assert answer.status_code == 503 and 'environment' not in answer.json()['detail']


def test_no_open_route_returns_a_removed_field(client, identified):
    client.cookies.clear()
    leaks = {}
    paths = sorted({route.path for route in main.app.routes
                    if 'GET' in (getattr(route, 'methods', None) or ()) and '{' not in route.path})
    assert len(paths) > 100
    for path in paths:
        try:
            answer = client.get(path, follow_redirects=False)
        except Exception:  # a route that needs setup this fixture lacks gave nothing away
            continue
        if answer.status_code != 200:
            continue
        found = set()
        if 'json' in answer.headers.get('content-type', ''):
            try:
                found = REMOVED & set(_keys(answer.json()))
            except ValueError:
                pass
        if HOSTNAME in answer.text or DEVICE in answer.text.lower() or claim_id_for(DEVICE) in answer.text:
            found |= {'value'}
        if found:
            leaks[path] = sorted(found)
    assert leaks == {}
