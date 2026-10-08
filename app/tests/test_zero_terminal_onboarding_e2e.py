"""Zero-terminal Linux onboarding, end to end (2026-10-08).

A normal customer completes onboarding without a terminal and without ever
seeing, copying or typing a Cloud ID, activation token or claim code:

  appliance desktop "AnyAiCam Setup" page (agent LinkServer, 127.0.0.1)
    -> "Link this appliance" -> 303 to <portal>/claim#code=...
    -> the cloud's /claim page (code moved into the tab, out of the URL)
    -> the signed-in customer's claim page looks the appliance up and the
       customer confirms the site
    -> the agent's headless claim redeems the confirmation (device secret,
       single-use proof, credential) and enrolls the appliance
    -> the setup page now reports the appliance linked.

Real HTTP over real sockets: the cloud's appliance_claims/appliance_cloud
routes under uvicorn, the agent's real PortalClient, HeadlessClaim,
_finish_enrollment and LinkServer. The "browser" is the HTTP requests a
browser makes (the claim page's JS calls are the same POSTs); there is no JS
runner in this repo. Failure, retry and reboot cases are below the happy path.
"""
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]  # app/
AGENT_ROOT = Path(__file__).resolve().parents[2] / 'appliance-agent'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(AGENT_ROOT))

_DB = Path(tempfile.gettempdir()) / f'anyaicam-zero-terminal-e2e-{os.getpid()}.db'
_DB.unlink(missing_ok=True)
os.environ.setdefault('ANYAICAM_DATABASE_BACKEND', 'sqlite')
os.environ.setdefault('ANYAICAM_PARTNER_DB', str(_DB))
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault('ANYAICAM_CLAIM_FLOW_SECRET_KEY', Fernet.generate_key().decode())

_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_sock.bind(('127.0.0.1', 0))
CLOUD = f'http://127.0.0.1:{_sock.getsockname()[1]}'

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import appliance_activation  # noqa: E402
import appliance_claims  # noqa: E402
import appliance_cloud  # noqa: E402
import partner_portal  # noqa: E402
from partner_db import connection  # noqa: E402

from anyaicam_agent import setup_wizard  # noqa: E402
from anyaicam_agent.config import AgentConfig, load_claim_state, load_credential  # noqa: E402
from anyaicam_agent.headless_claim import HeadlessClaim  # noqa: E402
from anyaicam_agent.link_server import LinkServer  # noqa: E402
from anyaicam_agent.portal import PortalClient  # noqa: E402

app = FastAPI()
appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: '')
appliance_claims.register_appliance_claim_routes(app, shell=lambda title, icon, content, scripts='': f'<html><body>{content}{scripts}</body></html>')

LABEL_VERIFIER = 'a' * 64
TOKEN = re.compile(r'name="token" value="([^"]+)"')


class _Server(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))

    def run(self):
        import asyncio
        asyncio.run(self.server.serve(sockets=[_sock]))

    def wait_ready(self):
        deadline = time.time() + 10
        while not getattr(self.server, 'started', False):
            if time.time() > deadline:
                raise RuntimeError('cloud test server did not start')
            time.sleep(0.05)


def _cloud(method, path, body=None, cookie=None):
    parts = urlsplit(CLOUD)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    headers = {'Content-Type': 'application/json'}
    if cookie:
        headers['Cookie'] = f'{partner_portal.SESSION_COOKIE}={cookie}'
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    data = response.read().decode('utf-8', 'replace')
    conn.close()
    try:
        return response.status, json.loads(data)
    except ValueError:
        return response.status, data


class Log:
    """A logger stand-in that keeps every line, to prove no code is logged."""
    def __init__(self):
        self.lines = []

    def _add(self, msg, *args, **kwargs):
        self.lines.append(msg % args if args else str(msg))

    debug = info = warning = error = exception = _add


class ZeroTerminalOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connection():
            pass
        cls.cloud = _Server()
        cls.cloud.start()
        cls.cloud.wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.cloud.server.should_exit = True
        cls.cloud.join(5)

    def setUp(self):
        for limiter in (appliance_cloud.activation_limiter, appliance_claims.claim_begin_limiter,
                        appliance_claims.claim_status_limiter, appliance_claims.claim_portal_limiter):
            limiter.events.clear()
        n = uuid.uuid4().hex[:8]
        self.customer, self.site, self.email = f'zt-cust-{n}', f'zt-site-{n}', f'owner-{n}@example.test'
        self.device_id = str(uuid.uuid4())
        with connection() as db:
            now = '2026-10-08T00:00:00'
            self.partner = f'zt-p-{n}'
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (self.partner, 'P', 'approved', 'real', now))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
                       (self.customer, f'zt-p-{n}', 'Customer', self.email, 'active', 'real', now))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (self.site, self.customer, 'Home', now))
        self.cookie = partner_portal._token(self.email, 'customer_owner', None, self.customer)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for sub in ('etc', 'state', 'vms', 'log'):
            (root / sub).mkdir()
        (root / 'etc' / 'appliance_identity.json').write_text(json.dumps({'appliance_id': self.device_id}), encoding='utf-8')
        (root / 'etc' / 'label_claim.json').write_text(json.dumps({'version': 1, 'verifier': LABEL_VERIFIER}), encoding='utf-8')
        self.config = AgentConfig(portal_url=CLOUD, config_dir=str(root / 'etc'), state_dir=str(root / 'state'),
                                  log_dir=str(root / 'log'), vms_recordings_path=str(root / 'vms'))
        appliance_activation.ACTIVATION_IDENTITY_FILE = root / 'cloud-identity-unused.json'
        self.log = Log()
        patcher = patch.object(setup_wizard.subprocess, 'run')
        patcher.start(); self.addCleanup(patcher.stop)
        self.boot()

    # ---------------------------------------------------------------- the appliance
    def boot(self, portal=None):
        """Start (or, after a "reboot", restart) the agent's claim and page."""
        if getattr(self, 'page', None):
            self.page.stop()
        client = PortalClient(portal or CLOUD)
        self.claim = HeadlessClaim(self.config, self.log, client=client)
        self.page = LinkServer(self.config, self.log, headless=lambda: None if self.claim.done else self.claim,
                               linked=lambda: bool((load_credential(self.config) or {}).get('credential')), port=0).start()
        self.addCleanup(self.page.stop)

    def run_agent(self, rounds=6):
        for _ in range(rounds):
            if self.claim.done:
                return
            self.claim.step()

    # ---------------------------------------------------------------- the browser
    def desktop(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.page.port, timeout=30)
        host = f'127.0.0.1:{self.page.port}'
        headers = {'Host': host}
        if method == 'POST':
            headers.update({'Origin': f'http://{host}', 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/x-www-form-urlencoded'})
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode('utf-8', 'replace')
        conn.close()
        return response, data

    def click_link(self):
        """Open AnyAiCam Setup and click "Link this appliance": returns where
        the browser goes and the code it carries in the fragment."""
        response, page = self.desktop('GET', '/')
        self.assertEqual(response.status, 200)
        self.assertIn('Link this appliance', page)
        response, body = self.desktop('POST', '/link', f'token={TOKEN.search(page).group(1)}')
        if response.status != 303:
            return response.status, None
        location = urlsplit(response.getheader('Location'))
        self.assertEqual(f'{location.scheme}://{location.netloc}{location.path}', f'{CLOUD}/claim')
        self.assertEqual(location.query, '')
        code = dict(part.split('=', 1) for part in location.fragment.split('&'))['code']
        for text in (page, body):
            self.assertNotIn(code, text)  # never on any page the customer sees
        return 303, code

    def customer_confirms(self, code):
        """What the browser does with the fragment, signed in: /claim stores it
        in the tab and continues to the claim page, whose script looks the
        appliance up and, on "Confirm", confirms it for the chosen site."""
        status, entry = _cloud('GET', '/claim')
        self.assertEqual(status, 200)
        self.assertIn("sessionStorage.setItem('anyaicam.claimCode'", entry)
        self.assertIn("location.replace('/customer/claim-appliance?return=setup')", entry)
        self.assertNotIn(code, entry)  # the fragment never reaches the server
        status, claim_page = _cloud('GET', '/customer/claim-appliance?return=setup', cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertIn("sessionStorage.getItem('anyaicam.claimCode')", claim_page)
        self.assertIn('if(linkCode)return {claim_code:linkCode};', claim_page)
        status, found = _cloud('POST', '/api/portal/claims/lookup', {'claim_code': code}, cookie=self.cookie)
        if status != 200:
            return status
        self.assertEqual(found['device_id'], self.device_id)
        status, _ = _cloud('POST', '/api/portal/claims/confirm', {'claim_code': code, 'site_id': self.site}, cookie=self.cookie)
        return status

    def enrollment(self):
        status, body = _cloud('GET', f'/api/portal/claims/enrollment?device_id={self.device_id}', cookie=self.cookie)
        self.assertEqual(status, 200)
        return body

    def heartbeat(self):
        """The appliance's normal operation: an authenticated heartbeat with its own credential."""
        credential = load_credential(self.config)
        PortalClient(CLOUD, credential['appliance_id'], credential['credential']).request(
            'POST', '/api/appliance/heartbeat', {'uptime_seconds': 30, 'cpu': 1, 'memory': 1})

    def appliances(self):
        with connection() as db:
            return [dict(r) for r in db.execute('SELECT cloud_id, customer_id, site_id FROM appliances WHERE cloud_id=?', (self.device_id.upper(),))]

    def assert_enrolled(self):
        self.assertTrue(self.claim.done)
        credential = load_credential(self.config)
        self.assertTrue(credential and credential.get('credential'))
        self.assertEqual(self.appliances(), [{'cloud_id': self.device_id.upper(), 'customer_id': self.customer, 'site_id': self.site}])
        identity = json.loads((Path(self.config.vms_recordings_path) / 'appliance_identity.json').read_text(encoding='utf-8'))
        self.assertEqual((identity['customer_id'], identity['site_id']), (self.customer, self.site))
        response, page = self.desktop('GET', '/')
        self.assertIn('linked to your AnyAiCam account', page)
        self.assertTrue(json.loads(self.desktop('GET', '/status')[1])['linked'])
        everything = '\n'.join(self.log.lines)
        self.assertNotIn(credential['credential'], everything)

    # ================================================================ happy path
    def test_customer_onboards_without_a_terminal_or_any_code(self):
        self.run_agent(1)  # the appliance opens its claim on its own
        status, code = self.click_link()
        self.assertEqual(status, 303)
        self.assertRegex(code, r'^[0-9A-F]{8}$')
        self.assertEqual(self.customer_confirms(code), 200)
        self.run_agent()
        self.assert_enrolled()
        # Codex finding 3: linked (and discovery) only once THIS appliance runs
        # with its credential -- enrolled is not yet ready until it checks in.
        self.assertEqual(self.enrollment()['status'], 'enrolling')
        self.heartbeat()
        ready = self.enrollment()
        self.assertEqual(ready['status'], 'ready')
        with connection() as db:
            self.assertEqual(db.execute('SELECT id FROM appliances WHERE cloud_id=?', (self.device_id.upper(),)).fetchone()[0], ready['appliance_id'])
        self.assertFalse(any(code in line for line in self.log.lines), 'the claim code must never be logged')
        self.assertIsNone(load_claim_state(self.config))

    # ================================================================ failure / retry / reboot
    def test_reboot_after_link_before_the_customer_confirms(self):
        status, code = self.click_link()
        self.boot()  # power cut: a new agent process with the same saved claim
        self.assertEqual(self.customer_confirms(code), 200)  # the link already in the browser still works
        self.run_agent()
        self.assert_enrolled()

    def test_reboot_after_confirm_before_the_appliance_redeems_it(self):
        _, code = self.click_link()
        self.assertEqual(self.customer_confirms(code), 200)
        self.claim.step()  # learns the proof and saves it ...
        self.assertTrue(load_claim_state(self.config).get('claim_proof'))
        self.boot()        # ... then the power goes out before it redeems it
        self.run_agent()
        self.assert_enrolled()

    def test_clicking_link_again_replaces_the_earlier_link(self):
        _, first = self.click_link()
        _, second = self.click_link()
        self.assertNotEqual(first, second)
        self.assertEqual(self.customer_confirms(first), 404)  # an old tab cannot claim it
        self.assertEqual(self.customer_confirms(second), 200)
        self.run_agent()
        self.assert_enrolled()

    def test_cloud_unreachable_then_retry(self):
        dead = socket.socket(); dead.bind(('127.0.0.1', 0)); port = dead.getsockname()[1]; dead.close()
        self.boot(portal=f'http://127.0.0.1:{port}')
        status, code = self.click_link()
        self.assertEqual((status, code), (503, None))
        response, page = self.desktop('GET', '/')
        self.assertIn('Link this appliance', page)  # the customer can simply try again
        self.boot()
        _, code = self.click_link()
        self.assertEqual(self.customer_confirms(code), 200)
        self.run_agent()
        self.assert_enrolled()

    def test_an_expired_claim_is_replaced_when_the_customer_links(self):
        self.run_agent(1)
        session = load_claim_state(self.config)['claim_session_id']
        with connection() as db:
            db.execute("UPDATE appliance_claims SET expires_at='2000-01-01T00:00:00' WHERE claim_session_id=?", (session,))
        _, code = self.click_link()
        self.assertNotEqual(load_claim_state(self.config)['claim_session_id'], session)  # the agent follows the new claim
        self.assertEqual(self.customer_confirms(code), 200)
        self.run_agent()
        self.assert_enrolled()

    def test_a_linked_appliance_never_opens_another_claim(self):
        _, code = self.click_link()
        self.customer_confirms(code)
        self.run_agent()
        self.assert_enrolled()
        with connection() as db:
            before = db.execute('SELECT COUNT(*) FROM appliance_claims WHERE device_id=?', (self.device_id,)).fetchone()[0]
        token = TOKEN.search(self.desktop('GET', '/')[1] or '')  # no link form once linked
        self.assertIsNone(token)
        response, _ = self.desktop('POST', '/link', 'token=' + self.page.token)
        self.assertEqual((response.status, response.getheader('Location')), (303, f'{CLOUD}/customer/setup'))
        with connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM appliance_claims WHERE device_id=?', (self.device_id,)).fetchone()[0], before)
        self.assertEqual(len(self.appliances()), 1)

    def test_a_local_enrollment_failure_is_never_reported_as_linked(self):
        """Codex finding 3: claim/complete creates the cloud row, but the
        appliance fails to save its identity and rolls back -- the claim page
        must keep waiting; the agent retries with the same proof and only then
        does the appliance become ready."""
        _, code = self.click_link()
        self.assertEqual(self.customer_confirms(code), 200)
        real_finish = self.claim.finish_enrollment
        self.claim.finish_enrollment = lambda *a, **k: (_ for _ in ()).throw(SystemExit('disk full'))
        self.run_agent(3)
        self.assertFalse(self.claim.done)
        self.assertEqual(len(self.appliances()), 1)             # the cloud row exists ...
        self.assertEqual(self.enrollment()['status'], 'enrolling')  # ... but it is not linked
        self.claim.finish_enrollment = real_finish
        self.claim.failures = 0
        self.run_agent()
        self.assert_enrolled()
        self.heartbeat()
        self.assertEqual(self.enrollment()['status'], 'ready')

    def test_another_customer_cannot_use_a_link_meant_for_a_different_site(self):
        _, code = self.click_link()
        other_site = f'other-{uuid.uuid4().hex[:6]}'
        with connection() as db:
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
                       ('other-cust-' + other_site, self.partner, 'Other', other_site + '@example.test', 'active', 'real', '2026-10-08'))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (other_site, 'other-cust-' + other_site, 'Other', '2026-10-08'))
        status, _ = _cloud('POST', '/api/portal/claims/confirm', {'claim_code': code, 'site_id': other_site}, cookie=self.cookie)
        self.assertEqual(status, 403)  # a site of another account is refused; nothing is linked
        self.assertEqual(self.appliances(), [])


if __name__ == '__main__':
    unittest.main()
