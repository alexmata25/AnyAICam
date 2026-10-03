"""Combined local browser regression. OS installation is mocked; no external traffic."""
import dataclasses
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import datetime
import socket
import threading
import time
import ipaddress
import uvicorn

import pytest
from test_customer_downloads import (  # noqa: F401
    OWNER, COMMIT, PACKAGE, _cookie, _seed, _license, _publish,
    db_path, license_portal, portal, storage,
)
from test_appliance_claims import (  # noqa: F401
    DEVICE_SECRET, VALID_DEVICE_ID, identity_file, claim_flow_key,
)
import appliance_claims
import cloud_security
import partner_portal
from partner_db import password_hash
from database_backend import override_target

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'installer'))
import customer_setup as setup
from build_release_installer import write_deterministic_tar

def _https_server(app, db_path, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'local-test')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path/'cert.pem', tmp_path/'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    origin = 'https://127.0.0.1:' + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path), lifespan='off', log_level='warning', access_log=False, timeout_graceful_shutdown=1, timeout_keep_alive=1))
    def serve():
        with override_target(sqlite_path=str(db_path)):
            server.run(sockets=[sock])
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, 'Local HTTPS fixture failed to start'
    return origin, server, thread, sock


@pytest.mark.parametrize('manual', [False, True], ids=['browser-handoff', 'manual-recovery'])
def test_download_launcher_signin_location_confirm_claim(
        manual, license_portal, db_path, storage, tmp_path, monkeypatch,
        identity_file, claim_flow_key, request):
    from playwright.sync_api import sync_playwright, expect
    client, _, _ = license_portal
    ORIGIN, server, server_thread, server_socket = _https_server(client.app, db_path, tmp_path)
    def stop_server():
        server.should_exit = True
        server_thread.join(timeout=5)
        server_socket.close()
        assert not server_thread.is_alive()
    request.addfinalizer(stop_server)
    import main
    monkeypatch.setattr(main, 'PUBLIC_BASE_URL', ORIGIN)
    _seed(db_path)
    _license(db_path, product='camera_slots_local')
    _license(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("""INSERT INTO partner_users
            (id,email,name,role,password_hash,approved,customer_id,created_at)
            VALUES('owner-install','owner@example.test','Owner','customer_owner',?,1,'cust-1','2026-01-01')""",
            (password_hash('Test-install-password-42!'),))
    monkeypatch.setattr(cloud_security, 'settings', dataclasses.replace(
        cloud_security.settings, csrf_enabled=True, allowed_origins=[ORIGIN]))
    for limiter in (appliance_claims.claim_begin_limiter,
                    appliance_claims.claim_status_limiter,
                    appliance_claims.claim_portal_limiter):
        limiter.events.clear()
    # Representative downloadable package: real launcher bytes + integrity manifest.
    # The destructive install/validate processes are deliberately mocked below.
    package_root = tmp_path / 'payload'
    package_root.mkdir()
    source = Path(setup.__file__).parent
    for name in ('customer_setup.py', 'setup.sh'):
        (package_root / name).write_bytes((source / name).read_bytes())
    for name in ('install.sh', 'validate.sh', 'release.env'):
        (package_root / name).write_bytes(b'LOCAL TEST FIXTURE ONLY\n')
    entries = [{'path': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
               for p in package_root.iterdir()]
    (package_root / 'artifact-files.json').write_text(json.dumps(entries))
    package = tmp_path / PACKAGE
    write_deterministic_tar(package_root, package, 0, frozenset({'setup.sh'}))
    _publish(package)
    # Prove middleware guards the actual claim-confirm route, not just login.
    client.cookies.clear()
    denied = client.post('/api/portal/claims/confirm', json={'claim_code': 'ABCD2345', 'site_id': 'site-1'},
                         cookies=_cookie(*OWNER), headers={'Origin': ORIGIN})
    assert denied.status_code == 403 and 'CSRF' in denied.text
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(accept_downloads=True, ignore_https_errors=True)
        urls, claim_posts = [], []
        bridge = None

        def route(request_route):
            request = request_route.request
            urls.append(request.url)
            if request.url.startswith(ORIGIN + '/') or (bridge and
                request.url.startswith(f'http://127.0.0.1:{bridge.server.server_port}/')):
                request_route.continue_()
            else:
                request_route.abort()  # no external network is permitted

        context.on('response', lambda response: claim_posts.append((response.url, response.status))
            if '/api/portal/claims/' in response.url else None)

        context.route('**/*', route)
        page = context.new_page()
        context.add_cookies([{'name': partner_portal.SESSION_COOKIE, 'value': _cookie(*OWNER)[partner_portal.SESSION_COOKIE],
                             'url': ORIGIN}])
        try:
            page.goto(ORIGIN + '/subscription-portal')
            panel = page.locator('#install-anyaicam').inner_text()
            assert 'Download AnyAiCam' in panel, panel
            with page.expect_download() as download:
                page.get_by_role('link', name='Download AnyAiCam', exact=True).click()
            downloaded = tmp_path / 'downloaded.tar.gz'
            download.value.save_as(downloaded)
            assert downloaded.read_bytes() == package.read_bytes()
            import tarfile
            extracted = tmp_path / 'extracted'
            with tarfile.open(downloaded) as archive:
                archive.extractall(extracted, filter='data')
            setup.verify_package(extracted)
            calls, opened = [], {}
            monkeypatch.setattr(setup, '__file__', str(extracted / 'customer_setup.py'))
            monkeypatch.setattr(setup, 'require_fresh_desktop_install', lambda: None)
            monkeypatch.setattr(setup.subprocess, 'run', lambda args, **_kw: calls.append(args))

            def begin_claim(portal):
                nonlocal bridge
                client.cookies.clear()
                response = client.post('/api/appliance/claim/begin',
                    json={'device_secret': DEVICE_SECRET, 'device_id': VALID_DEVICE_ID})
                assert response.status_code == 200
                opened.update(response.json())
                bridge = setup.ClaimBridge(opened['claim_code'], portal)
                return 0

            monkeypatch.setattr(setup, 'run_claim', begin_claim)
            assert setup.main(['--portal-url=' + ORIGIN, '--product-mode=local']) == 0
            assert [Path(c[2]).name for c in calls] == ['install.sh', 'validate.sh']
            context.clear_cookies()  # force actual customer sign-in after installation
            if manual:
                page.goto(ORIGIN + '/customer/claim-appliance')
                claim_page = page
            else:
                page.goto(f'http://127.0.0.1:{bridge.server.server_port}/#{bridge.token}')
                with page.expect_popup() as popup:
                    page.get_by_role('button', name='Connect AnyAiCam').click()
                claim_page = popup.value
            claim_page.wait_for_load_state()
            assert '/customer-login.html?next=' in claim_page.url, (claim_page.url, claim_page.content()[:1000], urls)
            claim_page.locator('#email').fill('owner@example.test')
            claim_page.locator('#password').fill('Test-install-password-42!')
            claim_page.locator('#login button.submit').click()
            claim_page.wait_for_url(ORIGIN + '/customer/claim-appliance')
            claim_page.locator('#claim-code-input').wait_for(state='visible')
            if manual:
                claim_page.locator('#claim-code-input').fill(opened['claim_code'])
                claim_page.locator('#claim-lookup-button').click()
            else:
                expect(claim_page.locator('#claim-code-input')).to_have_value(opened['claim_code'], timeout=10000)
                assert page.evaluate('location.hash') == ''
            claim_page.locator('#claim-step-confirm').wait_for(state='visible')
            # Looking up/prefilling the code must never confirm ownership.
            status_payload = {'claim_session_id': opened['claim_session_id'], 'device_secret': DEVICE_SECRET}
            assert client.post('/api/appliance/claim/status', json=status_payload).json()['status'] == 'pending'
            claim_page.locator('#claim-site-select').select_option('site-1')
            claim_page.locator('#claim-confirm-button').click()
            claim_page.locator('#claim-step-done').wait_for(state='visible')
            status = client.post('/api/appliance/claim/status', json=status_payload).json()
            assert status['claim_proof']
            result = client.post('/api/appliance/claim/complete', json={
                **status_payload, 'claim_proof': status['claim_proof']})
            assert result.status_code == 200, result.text
            with sqlite3.connect(db_path) as db:
                row = db.execute('SELECT customer_id,site_id FROM appliances WHERE cloud_id=?',
                                 (VALID_DEVICE_ID.upper(),)).fetchone()
            assert row == ('cust-1', 'site-1')
            assert all(code == 200 for _, code in claim_posts)
            assert not any(opened['claim_code'] in url or DEVICE_SECRET in url for url in urls)
        finally:
            if bridge:
                bridge.close()
            context.close()
            browser.close()
            stop_server()

@pytest.mark.parametrize('endpoint', ['lookup', 'confirm'])
@pytest.mark.parametrize('case', ['missing', 'mismatch', 'expired', 'origin'])
def test_claim_routes_keep_csrf_and_origin_gates(endpoint, case, license_portal, db_path, monkeypatch):
    from token_security import sign
    client, _, _ = license_portal
    _seed(db_path)
    origin = 'https://app.example.test'
    monkeypatch.setattr(cloud_security, 'settings', dataclasses.replace(
        cloud_security.settings, csrf_enabled=True, allowed_origins=[origin]))
    token = sign('csrf', -10 if case == 'expired' else 28800)
    client.cookies.clear()
    client.cookies.update({**_cookie(*OWNER), 'anyaicam_csrf': token})
    headers = {'Origin': 'https://untrusted.example.test' if case == 'origin' else origin}
    if case != 'missing':
        headers['X-CSRF-Token'] = 'tampered' if case == 'mismatch' else token
    response = client.post('/api/portal/claims/' + endpoint,
        headers=headers, json={'claim_code': 'ABCD2345', 'site_id': 'site-1'})
    assert response.status_code == 403
    assert ('Origin' if case == 'origin' else 'CSRF') in response.text
    assert 'claim_proof' not in response.text
