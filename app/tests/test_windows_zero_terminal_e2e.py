"""Windows zero-terminal linking, end to end (2026-10-08).

A Windows customer links the PC without ever seeing, copying or typing a
claim code, and without any local web page:

  setup's last page / Start menu "Link this computer to AnyAiCam"
    (an administrator: open-claim-link.ps1 writes a request into the
     Administrators-only agent\\link-requests folder)
    -> the agent (windows_link.LinkRequests) answers with
       <portal>/claim#code=XXXXXXXX&link=windows from an on-demand claim
       (HeadlessClaim(on_demand=True): no label, no standing claim)
    -> the cloud's /claim and claim page (code hidden, looked up by itself,
       Windows wording); the signed-in owner chooses the site and confirms
    -> the agent redeems the confirmation and enrolls the PC.

Real HTTP over real sockets: the cloud's appliance_claims/appliance_cloud
routes under uvicorn, the agent's real PortalClient, HeadlessClaim,
_finish_enrollment and LinkRequests (the request folder is a temporary one;
the browser is the HTTP requests a browser makes).
"""
import json
import os
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

_DB = Path(tempfile.gettempdir()) / f'anyaicam-windows-zero-terminal-e2e-{os.getpid()}.db'
_DB.unlink(missing_ok=True)
os.environ.setdefault('ANYAICAM_DATABASE_BACKEND', 'sqlite')
os.environ.setdefault('ANYAICAM_PARTNER_DB', str(_DB))
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault('ANYAICAM_CLAIM_FLOW_SECRET_KEY', Fernet.generate_key().decode())

_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_sock.bind(('127.0.0.1', 0))
CLOUD = f'http://127.0.0.1:{_sock.getsockname()[1]}'

import http.client  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import appliance_activation  # noqa: E402
import appliance_claims  # noqa: E402
import appliance_cloud  # noqa: E402
import partner_portal  # noqa: E402
from partner_db import connection  # noqa: E402

from anyaicam_agent import setup_wizard, windows_link  # noqa: E402
from anyaicam_agent.config import AgentConfig, load_claim_state, load_credential, save_claim_state  # noqa: E402
from anyaicam_agent.headless_claim import HeadlessClaim  # noqa: E402
from anyaicam_agent.portal import PortalClient  # noqa: E402
from anyaicam_agent.windows_link import LinkRequests, request_folder  # noqa: E402

app = FastAPI()
appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: '')
appliance_claims.register_appliance_claim_routes(app, shell=lambda title, icon, content, scripts='': f'<html><body>{content}{scripts}</body></html>')

# The link must name an https cloud; this test cloud is plain http on
# loopback, so the link names this stand-in and the test reads the code from it.
LINK_ORIGIN = 'https://cloud.anyaicam.test'


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
    def __init__(self):
        self.lines = []

    def _add(self, msg, *args, **kwargs):
        self.lines.append(msg % args if args else str(msg))

    debug = info = warning = error = exception = _add


class WindowsZeroTerminalTests(unittest.TestCase):
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
        self.customer, self.site, self.email = f'wz-cust-{n}', f'wz-site-{n}', f'owner-{n}@example.test'
        self.device_id = str(uuid.uuid4())
        with connection() as db:
            now = '2026-10-08T00:00:00'
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (f'wz-p-{n}', 'P', 'approved', 'real', now))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
                       (self.customer, f'wz-p-{n}', 'Customer', self.email, 'active', 'real', now))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (self.site, self.customer, 'Home', now))
        self.cookie = partner_portal._token(self.email, 'customer_owner', None, self.customer)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for sub in ('config', 'agent', 'recordings', 'logs'):
            (root / sub).mkdir()
        # A Windows install: identity, no label (retired 2026-10-08).
        (root / 'config' / 'appliance_identity.json').write_text(json.dumps({'appliance_id': self.device_id}), encoding='utf-8')
        self.config = AgentConfig(portal_url=CLOUD, config_dir=str(root / 'config'), state_dir=str(root / 'agent'),
                                  log_dir=str(root / 'logs'), vms_recordings_path=str(root / 'recordings'))
        appliance_activation.ACTIVATION_IDENTITY_FILE = root / 'cloud-identity-unused.json'
        self.log = Log()
        for patcher in (patch.object(setup_wizard.subprocess, 'run'),
                        patch.object(windows_link, 'portal_origin', lambda url: LINK_ORIGIN)):
            patcher.start(); self.addCleanup(patcher.stop)
        self.claim = HeadlessClaim(self.config, self.log, client=PortalClient(CLOUD), on_demand=True)
        self.requests = LinkRequests(self.config, self.log, headless=lambda: None if self.claim.done else self.claim,
                                     linked=lambda: bool((load_credential(self.config) or {}).get('credential')))
        request_folder(self.config.state_dir).mkdir()

    # ---------------------------------------------------------------- the PC
    def run_agent(self, rounds=6):
        for _ in range(rounds):
            if self.claim.done:
                return
            self.claim.step()

    def administrator_asks(self):
        """open-claim-link.ps1: a request in the Administrators-only folder,
        the agent's answer read back."""
        nonce = uuid.uuid4().hex
        folder = request_folder(self.config.state_dir)
        (folder / f'{nonce}.request').write_text('{}')
        self.requests.poll_once()
        response = folder / f'{nonce}.response'
        answer = json.loads(response.read_text(encoding='utf-8'))
        response.unlink()
        return answer

    def link_code(self):
        answer = self.administrator_asks()
        self.assertEqual(answer['status'], 'ok')
        link = urlsplit(answer['link'])
        self.assertEqual((f'{link.scheme}://{link.netloc}{link.path}', link.query), (f'{LINK_ORIGIN}/claim', ''))
        fragment = dict(part.split('=', 1) for part in link.fragment.split('&'))
        self.assertEqual(fragment['link'], 'windows')
        return fragment['code']

    # ---------------------------------------------------------------- the browser
    def customer_confirms(self, code):
        status, entry = _cloud('GET', '/claim')
        self.assertEqual(status, 200)
        self.assertIn("sessionStorage.setItem('anyaicam.claimLinkSource','windows')", entry)
        self.assertNotIn(code, entry)
        status, page = _cloud('GET', '/customer/claim-appliance?return=setup', cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertIn("const fromWindows=Boolean(linkCode)&&linkSource==='windows';", page)
        self.assertIn('Link this computer to AnyAiCam again', page)
        self.assertNotIn(code, page)
        status, found = _cloud('POST', '/api/portal/claims/lookup', {'claim_code': code}, cookie=self.cookie)
        if status != 200:
            return status
        self.assertEqual(found['device_id'], self.device_id)
        status, _ = _cloud('POST', '/api/portal/claims/confirm', {'claim_code': code, 'site_id': self.site}, cookie=self.cookie)
        return status

    def pending_claims(self):
        with connection() as db:
            return [dict(r) for r in db.execute("SELECT label_verifier_hash FROM appliance_claims WHERE device_id=? AND status='pending'", (self.device_id,))]

    # ================================================================ tests
    def test_an_administrator_links_the_pc_without_any_code_or_label(self):
        self.run_agent(3)
        self.assertEqual(self.pending_claims(), [])  # no standing claim before anyone asks
        code = self.link_code()
        self.assertEqual([c['label_verifier_hash'] for c in self.pending_claims()], [None])  # no label: only this code matches
        self.assertEqual(self.customer_confirms(code), 200)
        self.run_agent()
        self.assertTrue(self.claim.done)
        credential = load_credential(self.config)
        self.assertTrue(credential and credential.get('credential'))
        with connection() as db:
            row = dict(db.execute('SELECT customer_id, site_id FROM appliances WHERE cloud_id=?', (self.device_id.upper(),)).fetchone())
        self.assertEqual(row, {'customer_id': self.customer, 'site_id': self.site})
        self.assertEqual(self.administrator_asks(), {'status': 'linked'})
        everything = '\n'.join(self.log.lines)
        self.assertNotIn(code, everything)
        self.assertNotIn(credential['credential'], everything)

    def test_each_request_replaces_the_previous_code(self):
        first = self.link_code()
        second = self.link_code()
        self.assertNotEqual(first, second)
        status, _ = _cloud('POST', '/api/portal/claims/lookup', {'claim_code': first}, cookie=self.cookie)
        self.assertEqual(status, 404)
        self.assertEqual(self.customer_confirms(second), 200)

    def test_an_expired_link_is_not_reopened_by_itself(self):
        code = self.link_code()
        with connection() as db:
            db.execute("UPDATE appliance_claims SET expires_at='2000-01-01T00:00:00' WHERE device_id=?", (self.device_id,))
        self.run_agent(3)
        self.assertIsNone(load_claim_state(self.config))
        self.assertEqual(self.pending_claims(), [])
        status, _ = _cloud('POST', '/api/portal/claims/lookup', {'claim_code': code}, cookie=self.cookie)
        self.assertEqual(status, 404)
        self.assertEqual(self.customer_confirms(self.link_code()), 200)  # a new request works at once

    def test_a_label_claim_left_open_by_1_3_0_is_resumed_and_linked(self):
        # Windows 1.3.0 kept a label claim open; after the upgrade the agent
        # resumes it with its own device secret (no new label is sent).
        secret = 'device-secret-from-1.3.0-xxxxxxxxxxxxxxxx'
        session = PortalClient(CLOUD).claim_begin(self.device_id, secret, label_verifier='b' * 64)
        save_claim_state(self.config, {'device_id': self.device_id, 'claim_session_id': session['claim_session_id'],
                                       'device_secret': secret, 'portal_origin': CLOUD.lower(), 'headless': True})
        self.run_agent(1)
        self.assertEqual(self.customer_confirms(self.link_code()), 200)
        self.run_agent()
        self.assertTrue(self.claim.done)

    def test_another_accounts_site_is_refused(self):
        code = self.link_code()
        status, _ = _cloud('POST', '/api/portal/claims/lookup', {'claim_code': code}, cookie=self.cookie)
        self.assertEqual(status, 200)
        status, _ = _cloud('POST', '/api/portal/claims/confirm', {'claim_code': code, 'site_id': 'not-my-site'}, cookie=self.cookie)
        self.assertIn(status, (403, 404))
        self.run_agent(2)
        self.assertFalse(self.claim.done)


if __name__ == '__main__':
    unittest.main()
