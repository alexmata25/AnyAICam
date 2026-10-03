"""Local-only launcher/bridge tests; never executes sudo or an installer."""
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customer_setup as setup
from build_release_installer import INSTALLER_RUNTIME_FILES, write_deterministic_tar

PORTAL = 'https://portal.example.test'
CODE = 'ABCD2345'


@pytest.mark.parametrize('value', ['http://portal.example.test', 'https://user:pass@portal.example.test',
    'https://portal.example.test/page', 'https://portal.example.test/?code=secret',
    'https://portal.example.test/#secret', 'https://bad host', 'https://portal.example.test:bad'])
def test_rejects_insecure_or_ambiguous_portal_addresses(value):
    with pytest.raises(ValueError):
        setup.portal_origin(value)


def test_portal_origin_matches_browser_normalization():
    assert setup.portal_origin('https://PORTAL.example.test:443/') == PORTAL
    assert setup.portal_origin(PORTAL + ':8443') == PORTAL + ':8443'


def _request(bridge, path='/', headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', bridge.server.server_port, timeout=2)
    try:
        conn.request('GET', path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.fixture()
def bridge():
    value = setup.ClaimBridge(CODE, PORTAL)
    try:
        yield value
    finally:
        value.close()


def test_loopback_document_contains_no_claim_code_and_is_not_cacheable(bridge, capsys):
    status, headers, body = _request(bridge)
    assert status == 200 and CODE.encode() not in body and bridge.token.encode() not in body
    assert bridge.server.server_address[0] == '127.0.0.1'
    assert headers['Cache-Control'] == 'no-store' and headers['Referrer-Policy'] == 'no-referrer'
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert 'Access-Control-Allow-Origin' not in headers
    assert not capsys.readouterr().err


def test_code_read_requires_nonce_exact_host_and_same_origin(bridge):
    assert _request(bridge, '/handoff')[0] == 403
    assert _request(bridge, '/handoff', {'X-AnyAiCam-Local-Token': 'wrong'})[0] == 403
    headers = {'X-AnyAiCam-Local-Token': bridge.token}
    assert _request(bridge, '/handoff', dict(headers, Host='evil.example'))[0] == 403
    assert _request(bridge, '/handoff', dict(headers, Origin='https://evil.example'))[0] == 403
    status, _, body = _request(bridge, '/handoff', headers)
    assert status == 200 and json.loads(body) == {'claim_code': CODE, 'portal_url': PORTAL}
    assert _request(bridge, '/handoff?code=' + CODE, headers)[0] == 404


def test_expired_bridge_refuses_code(bridge):
    bridge.expires = 0
    assert _request(bridge, '/handoff', {'X-AnyAiCam-Local-Token': bridge.token})[0] == 403


def test_browser_url_never_contains_claim_secrets(bridge, monkeypatch):
    opened = Mock(return_value=True)
    monkeypatch.setattr(setup.webbrowser, 'open', opened)
    assert bridge.open_browser()
    url = opened.call_args.args[0]
    assert url.startswith('http://127.0.0.1:') and '/#' in url
    assert CODE not in url and PORTAL not in url


def _package(tmp_path):
    entries = []
    for name in ('install.sh', 'validate.sh', 'setup.sh', 'customer_setup.py', 'release.env'):
        data = name.encode()
        (tmp_path / name).write_bytes(data)
        entries.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest()})
    (tmp_path / 'artifact-files.json').write_text(json.dumps(entries))
    return entries


def test_package_integrity_is_checked_before_privileged_execution(tmp_path):
    entries = _package(tmp_path)
    setup.verify_package(tmp_path)
    (tmp_path / 'install.sh').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='changed'):
        setup.verify_package(tmp_path)
    entries[0]['path'] = '../outside'
    (tmp_path / 'artifact-files.json').write_text(json.dumps(entries))
    with pytest.raises(ValueError, match='unsafe'):
        setup.verify_package(tmp_path)


def test_incomplete_manifest_is_refused(tmp_path):
    entries = _package(tmp_path)
    (tmp_path / 'artifact-files.json').write_text(json.dumps(entries[1:]))
    with pytest.raises(ValueError, match='incomplete'):
        setup.verify_package(tmp_path)


def test_main_delegates_install_validate_and_existing_claim_in_order(tmp_path, monkeypatch):
    _package(tmp_path)
    monkeypatch.setattr(setup, '__file__', str(tmp_path / 'customer_setup.py'))
    monkeypatch.setattr(setup, 'require_fresh_desktop_install', lambda: None)
    calls = []
    monkeypatch.setattr(setup.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(setup, 'run_claim', lambda portal: calls.append(portal) or 0)
    assert setup.main(['--portal-url=' + PORTAL, '--product-mode=hybrid']) == 0
    assert calls == [['sudo', 'bash', str(tmp_path / 'install.sh'), '--product-mode=hybrid'],
                     ['sudo', 'bash', str(tmp_path / 'validate.sh')], PORTAL]


def test_install_failure_never_starts_claim(tmp_path, monkeypatch):
    _package(tmp_path)
    monkeypatch.setattr(setup, '__file__', str(tmp_path / 'customer_setup.py'))
    monkeypatch.setattr(setup, 'require_fresh_desktop_install', lambda: None)
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(setup.subprocess, 'run', fail)
    claim = Mock()
    monkeypatch.setattr(setup, 'run_claim', claim)
    assert setup.main(['--portal-url=' + PORTAL, '--product-mode=local']) == 1
    claim.assert_not_called()


def test_launcher_refuses_existing_install_and_root(monkeypatch):
    monkeypatch.setattr(setup.sys, 'platform', 'linux')
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0, raising=False)
    with pytest.raises(ValueError, match='not as root'):
        setup.require_fresh_desktop_install()
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(setup.Path, 'read_text', lambda *_a, **_kw: 'ID=ubuntu\nVERSION_ID="24.04"\n')
    monkeypatch.setattr(setup.Path, 'exists', lambda _p: True)
    with pytest.raises(ValueError, match='already installed'):
        setup.require_fresh_desktop_install()


def test_claim_uses_existing_cli_and_no_secrets_as_arguments(monkeypatch, capsys):
    process = Mock(stdout=io.StringIO('Claim code: ' + CODE + '\nAssigned site: site-1\n'))
    process.wait.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(setup.subprocess, 'Popen', popen)
    fake_bridge = Mock()
    fake_bridge.open_browser.return_value = True
    constructor = Mock(return_value=fake_bridge)
    monkeypatch.setattr(setup, 'ClaimBridge', constructor)
    assert setup.run_claim(PORTAL) == 0
    constructor.assert_called_once_with(CODE, PORTAL)
    fake_bridge.close.assert_called_once()
    command = popen.call_args.args[0]
    assert command == ['sudo', '-u', 'anyaicam', '/opt/anyaicam-agent/venv/bin/anyaicam-setup',
                       '--claim', '--portal-url=' + PORTAL, '--mode=production']
    assert CODE not in str(command) and CODE not in capsys.readouterr().out


def test_bridge_failure_keeps_existing_manual_claim_recovery(monkeypatch, capsys):
    process = Mock(stdout=io.StringIO('Claim code: ' + CODE + '\n'))
    process.wait.return_value = 0
    monkeypatch.setattr(setup.subprocess, 'Popen', lambda *_a, **_kw: process)
    monkeypatch.setattr(setup, 'ClaimBridge', Mock(side_effect=OSError('no local socket')))
    assert setup.run_claim(PORTAL) == 0
    assert 'Setup code: ' + CODE in capsys.readouterr().out


def test_new_launcher_is_packaged_and_shell_launcher_is_executable(tmp_path):
    assert 'customer_setup.py' in INSTALLER_RUNTIME_FILES and 'setup.sh' in INSTALLER_RUNTIME_FILES
    root = tmp_path / 'package'
    root.mkdir()
    (root / 'setup.sh').write_bytes(b'#!/bin/bash\nexit 0\n')
    archive = tmp_path / 'package.tar.gz'
    write_deterministic_tar(root, archive, 0, frozenset({'setup.sh'}))
    import tarfile
    with tarfile.open(archive) as tf:
        assert tf.getmember('setup.sh').mode & 0o111



def test_malformed_manifest_is_refused_without_launching_anything(tmp_path):
    (tmp_path / 'artifact-files.json').write_text(json.dumps([None]))
    with pytest.raises(ValueError, match='invalid file verification'):
        setup.verify_package(tmp_path)


def test_interrupted_claim_cleans_up_child_and_bridge(monkeypatch):
    class InterruptedStream:
        def __iter__(self):
            yield 'Claim code: ' + CODE + '\n'
            raise KeyboardInterrupt()
        def close(self):
            pass
    process = Mock(stdout=InterruptedStream())
    monkeypatch.setattr(setup.subprocess, 'Popen', lambda *_a, **_kw: process)
    fake_bridge = Mock()
    monkeypatch.setattr(setup, 'ClaimBridge', lambda *_a: fake_bridge)
    with pytest.raises(KeyboardInterrupt):
        setup.run_claim(PORTAL)
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    fake_bridge.close.assert_called_once()
