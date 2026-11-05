"""The agent's logs and status line show the Claim ID. Its config file keeps the
real value, because every cloud call needs it."""
import json
from pathlib import Path
from types import SimpleNamespace

from anyaicam_agent import cli
from anyaicam_agent.claim_id import claim_id_for, display_cloud_id, mask_device_path
from anyaicam_agent.headless_claim import CLAIM_CODE_PATTERN
from anyaicam_agent.portal import PortalClient

DEVICE = '4f3b2a10-9c8d-4e7f-a6b5-0123456789ab'
CLOUD_ID = DEVICE.upper()


def test_a_device_uuid_shows_as_its_claim_id_in_either_case():
    assert display_cloud_id(CLOUD_ID) == display_cloud_id(DEVICE) == claim_id_for(DEVICE)


def test_an_admin_cloud_id_and_an_empty_value_pass_through():
    assert display_cloud_id('AIC-1234') == 'AIC-1234'
    assert display_cloud_id('') == '' and display_cloud_id(None) is None


def test_a_queued_route_is_logged_without_the_uuid():
    line = mask_device_path(f'/api/appliance/{CLOUD_ID}/scan-jobs/job-1')
    assert line == f'/api/appliance/{claim_id_for(DEVICE)}/scan-jobs/job-1'


def test_other_routes_are_left_alone():
    assert mask_device_path('/api/appliance/heartbeat') == '/api/appliance/heartbeat'
    assert mask_device_path('/api/appliance/AIC-1234/scan-jobs') == '/api/appliance/AIC-1234/scan-jobs'


def test_the_status_command_prints_the_claim_id(monkeypatch, capsys, tmp_path):
    config = SimpleNamespace(cloud_id=CLOUD_ID, portal_url='https://portal.example.test', mode='hybrid', queue_file=Path(tmp_path / 'queue.db'), credential_file=Path(tmp_path / 'credential.json'))
    monkeypatch.setattr(cli.AgentConfig, 'load', classmethod(lambda cls: config))
    monkeypatch.setattr(cli.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='active'))
    cli.status_main()
    printed = capsys.readouterr().out
    assert DEVICE not in printed.lower()
    assert json.loads(printed)['cloud_id'] == claim_id_for(DEVICE)


def _sent(**claim):
    client = PortalClient('https://portal.example')
    sent = []
    client.request = lambda method, path, payload, authenticated: sent.append(payload) or {}
    client.claim_begin('device', 'secret', **claim)
    return sent[0]


def test_every_claim_path_asks_for_the_long_code():
    assert _sent()['long_code'] is True
    assert _sent(label_verifier='a' * 64)['long_code'] is True
    assert _sent(clear_label_verifier=True)['long_code'] is True


def test_the_agent_takes_either_code_so_an_older_cloud_still_works():
    assert CLAIM_CODE_PATTERN.match('ABCD1234') and CLAIM_CODE_PATTERN.match('ABCD1234EFGH')
    assert not CLAIM_CODE_PATTERN.match('ABCD12345')
