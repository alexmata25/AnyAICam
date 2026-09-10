"""Phase 2A: genuine end-to-end proof of the non-interactive claim
workflow -- real HTTP over a real TCP socket (uvicorn), the actual
appliance-agent PortalClient and setup_wizard claim helpers (not
mocked), the actual cloud-side appliance_cloud.py/appliance_claims.py
routes, and the actual, unmodified first_enroll()/coordinated_reenroll()
machinery, ending in a real authenticate_appliance() success via
GET /api/appliance/commands (first_enroll()'s own verify_authentication
step) and an explicit heartbeat call.

Why a real server, not TestClient: PortalClient (appliance-agent) makes
real urllib.request.urlopen() calls -- an in-process ASGI TestClient
cannot receive those. This repo already has exactly this pattern for a
different cross-cutting concern; see test_login_csrf_http_integration.py's
own _ServerThread, reused here almost verbatim.

This file imports across the app/ and appliance-agent/ package
boundary deliberately -- it is the one place in the repo that proves
those two independently-developed halves actually agree on wire
format end to end, the same justification test_login_csrf_http_
integration.py gives for its own real-server approach.

The "customer" side of every scenario below is a raw HTTP POST with a
customer_owner session cookie (partner_portal._token(...)), standing
in for the claim page's own fetch() calls -- there is no browser/JS
test runner in this repo, so this is the same level of fidelity
test_appliance_claims.py's own TestClient-based tests already use for
the identical endpoints, just over a real socket instead of an
in-process ASGI transport.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]  # app/
AGENT_ROOT = Path(__file__).resolve().parents[2] / 'appliance-agent'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(AGENT_ROOT))

# Same convention as test_login_csrf_http_integration.py: pin the target
# DB and the claim-flow encryption key before the first `import
# partner_db`/`import appliance_claims` (pulled in transitively).
_IMPORT_TIME_DB = Path(tempfile.gettempdir()) / 'anyaicam-claim-e2e.db'
_IMPORT_TIME_DB.unlink(missing_ok=True)
os.environ.setdefault('ANYAICAM_DATABASE_BACKEND', 'sqlite')
os.environ['ANYAICAM_PARTNER_DB'] = str(_IMPORT_TIME_DB)

from cryptography.fernet import Fernet  # noqa: E402

_CLAIM_FLOW_KEY = Fernet.generate_key().decode()
os.environ['ANYAICAM_CLAIM_FLOW_SECRET_KEY'] = _CLAIM_FLOW_KEY

# Reserve a free loopback port before starting the server, same
# rationale as the CSRF integration test: hand the already-bound
# socket straight to uvicorn rather than binding twice.
_bound_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_bound_socket.bind(('127.0.0.1', 0))
PORT = _bound_socket.getsockname()[1]
ORIGIN = f'http://127.0.0.1:{PORT}'

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import appliance_activation  # noqa: E402
import appliance_claims  # noqa: E402
import appliance_cloud  # noqa: E402
import partner_portal  # noqa: E402
from partner_db import connection  # noqa: E402

from anyaicam_agent import setup_wizard  # noqa: E402
from anyaicam_agent.config import AgentConfig, clear_claim_state, load_claim_state  # noqa: E402
from anyaicam_agent.portal import PortalClient  # noqa: E402

# claim/complete calls the real, unmodified persist_activation(), which
# reads/writes ACTIVATION_IDENTITY_FILE -- a real local file (defaults
# to /app/recordings/appliance_identity.json), not scoped by anything
# request-specific. Because the server in this file runs inside THIS
# SAME test process (a background thread, not a subprocess), leaving
# this at its default would make every test here silently read/write
# whatever real file happens to sit at that path on the machine running
# the tests -- exactly the leakage the Phase 1 security-audit session
# already found and fixed for test_appliance_claims.py's own in-process
# TestClient tests. A placeholder path is set here (never left at the
# real default even before the first test runs); each test's own setUp()
# below points it at a fresh per-test path, since several tests in this
# file each complete a claim for a different device_id against the one
# shared server process.
appliance_activation.ACTIVATION_IDENTITY_FILE = Path(tempfile.gettempdir()) / 'anyaicam-claim-e2e-identity-unused.json'


def _shell_stub(title, icon, content, scripts=''):
    return f'<html><head><title>{title}</title></head><body>{content}{scripts}</body></html>'


app = FastAPI()
appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: '')
appliance_claims.register_appliance_claim_routes(app, shell=_shell_stub)


class _ServerThread(threading.Thread):
    """Runs the real ASGI app over a real TCP socket -- see this
    module's own docstring and test_login_csrf_http_integration.py's
    identical _ServerThread for why a real socket matters here."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self._sock = sock
        config = uvicorn.Config(app, log_level='warning')
        self.server = uvicorn.Server(config)

    def run(self):
        import asyncio
        asyncio.run(self.server.serve(sockets=[self._sock]))

    def wait_ready(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self.server, 'started', False):
                return
            time.sleep(0.05)
        raise RuntimeError('test server did not start in time')

    def stop(self):
        self.server.should_exit = True
        self.join(timeout=5)
        self._sock.close()


def _http_post(path, body, cookie=None):
    import urllib.error
    import urllib.request
    headers = {'Content-Type': 'application/json'}
    if cookie:
        headers['Cookie'] = f'{partner_portal.SESSION_COOKIE}={cookie}'
    request = urllib.request.Request(ORIGIN + path, data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode() or '{}')
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode())
        except (ValueError, json.JSONDecodeError):
            return error.code, {}


class ClaimFlowEndToEndTests(unittest.TestCase):
    """One TestCase class -- deliberately not split across several,
    since setUpClass/tearDownClass below own the one module-level
    socket/server-thread pair; a second TestCase subclass would try to
    bind/close the same already-consumed socket a second time."""
    """Shared live-server + fresh-customer/site/device fixture for
    every test in this file. A fresh device_id, customer_id, and
    site_id per test avoids any cross-test interference despite all
    tests sharing one running server and one sqlite file."""

    @classmethod
    def setUpClass(cls):
        with connection():
            pass  # triggers ensure_database_initialized()/initialize_database() against _IMPORT_TIME_DB
        cls.thread = _ServerThread(_bound_socket)
        cls.thread.start()
        cls.thread.wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.thread.stop()

    _counter = 0

    def setUp(self):
        appliance_cloud.activation_limiter.events.clear()
        appliance_claims.claim_begin_limiter.events.clear()
        appliance_claims.claim_status_limiter.events.clear()
        appliance_claims.claim_portal_limiter.events.clear()
        ClaimFlowEndToEndTests._counter += 1
        n = ClaimFlowEndToEndTests._counter
        self.customer_id = f'e2e-cust-{n}'
        self.site_id = f'e2e-site-{n}'
        self.partner_id = f'e2e-partner-{n}'
        self.email = f'owner{n}@example.test'
        self.device_id = f'11111111-1111-4111-8111-{n:012d}'
        with connection() as db:
            now = '2026-09-10T00:00:00'
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (self.partner_id, 'E2E Partner', 'approved', 'real', now))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (self.customer_id, self.partner_id, 'E2E Customer', self.email, 'active', 'real', now))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (self.site_id, self.customer_id, 'E2E Site', now))
        self.cookie = partner_portal._token(self.email, 'customer_owner', None, self.customer_id)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.agent_config_dir = root / 'etc'
        self.agent_state_dir = root / 'state'
        self.agent_vms_dir = root / 'vms'
        for p in (self.agent_config_dir, self.agent_state_dir, self.agent_vms_dir):
            p.mkdir()
        (self.agent_config_dir / 'appliance_identity.json').write_text(
            json.dumps({'appliance_id': self.device_id, 'installer_version': 'test', 'installed_at': '2026-09-10T00:00:00Z'}), encoding='utf-8',
        )
        # claim/complete's cloud-side handler also calls the real,
        # unmodified persist_activation() (matching activate_appliance()'s
        # own behavior in this monolith -- see appliance_activation.py's
        # own module docstring), so it needs a FRESH identity file per
        # TEST METHOD, not just once per module: several tests in this
        # file each complete a claim for a different device_id sharing
        # one server process, and a single shared file would make the
        # second such test hit a spurious ActivationConflict against the
        # first test's already-different cloud_id.
        appliance_activation.ACTIVATION_IDENTITY_FILE = root / 'cloud_appliance_identity.json'

    def tearDown(self):
        self.tmp.cleanup()

    def _agent_config(self):
        return AgentConfig(
            portal_url=ORIGIN, config_dir=str(self.agent_config_dir),
            state_dir=str(self.agent_state_dir), log_dir=str(Path(self.tmp.name) / 'log'),
            vms_recordings_path=str(self.agent_vms_dir),
        )

    def _begin(self, device_id=None):
        # A real device with no local state yet, doing exactly what
        # claim/begin's own docstring says: hand the claim_code for
        # this device_id to whoever calls it -- captured here once,
        # exactly as a technician reading it off the appliance's
        # terminal would, and used for the "customer" side of every
        # scenario below.
        status, body = _http_post('/api/appliance/claim/begin', {'device_id': device_id or self.device_id})
        self.assertEqual(status, 200, body)
        return body

    def _confirm(self, claim_code, site_id=None, cookie=None):
        return _http_post('/api/portal/claims/confirm', {'claim_code': claim_code, 'site_id': site_id or self.site_id}, cookie=cookie or self.cookie)


    def test_full_workflow_reaches_authenticate_appliance_success(self):
        session = self._begin()
        config = self._agent_config()
        client = PortalClient(config.portal_url)
        info = client.test()
        self.assertIn('mode', info)

        # 1. fresh appliance -> begin claim -> display claim code
        # (claim_begin above already opened the session; the agent's
        # own _open_or_resume_claim now resumes it -- the cloud sees an
        # existing pending claim for this device_id and reports
        # resumed=True with the same claim_session_id, exactly as it
        # would if the SAME agent process had made both calls.)
        device_id = setup_wizard._installer_device_id(config)
        self.assertEqual(device_id, self.device_id)
        state = setup_wizard._open_or_resume_claim(client, config, device_id)
        self.assertEqual(state['claim_session_id'], session['claim_session_id'])
        persisted = load_claim_state(config)
        self.assertEqual(persisted['claim_session_id'], state['claim_session_id'])

        # 2. customer authenticates, selects the authorized site, confirms
        status, lookup_body = _http_post('/api/portal/claims/lookup', {'claim_code': session['claim_code']}, cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(lookup_body['device_id'], device_id)
        status, confirm_body = self._confirm(session['claim_code'])
        self.assertEqual(status, 200, confirm_body)
        self.assertEqual(confirm_body, {'status': 'claimed', 'device_id': device_id})

        # 3. appliance polls status -> completes claim
        claim_proof = setup_wizard._wait_for_claim_proof(client, config, state, sleep_fn=lambda _: None)
        self.assertTrue(claim_proof)
        activated = setup_wizard._complete_claim_with_retry(client, config, state, sleep_fn=lambda _: None)
        self.assertEqual(activated['customer_id'], self.customer_id)
        self.assertEqual(activated['site_id'], self.site_id)
        # claim_main() itself calls clear_claim_state() immediately after
        # a successful _complete_claim_with_retry() -- done explicitly
        # here since this test drives the individual helpers directly
        # rather than calling claim_main() as one black box (so each
        # step's own result can be inspected along the way).
        clear_claim_state(config)
        self.assertIsNone(load_claim_state(config))

        # 4. persists credentials through the existing, unmodified
        #    activation machinery -- and authenticate_appliance()
        #    succeeds (first_enroll()'s own verify_authentication step
        #    IS a real authenticate_appliance() call against this live
        #    server; _finish_enrollment() calls it internally).
        with patch.object(setup_wizard.subprocess, 'run'), patch('builtins.input', return_value='n'):
            setup_wizard._finish_enrollment(config, activated)

        agent_json = json.loads((self.agent_config_dir / 'agent.json').read_text(encoding='utf-8'))
        credential_json = json.loads(config.credential_file.read_text(encoding='utf-8'))
        vms_identity = json.loads((self.agent_vms_dir / 'appliance_identity.json').read_text(encoding='utf-8'))
        self.assertEqual(agent_json['cloud_id'], activated['cloud_id'])
        self.assertEqual(vms_identity['customer_id'], self.customer_id)

        # Explicit, independent confirmation on top of first_enroll()'s
        # own internal check: a fresh heartbeat call authenticates
        # successfully with the persisted credential.
        authenticated_client = PortalClient(config.portal_url, credential_json['appliance_id'], credential_json['credential'])
        heartbeat = authenticated_client.request('POST', '/api/appliance/heartbeat', {'uptime_seconds': 42, 'cpu': 1, 'memory': 1})
        self.assertEqual(heartbeat.get('status'), 'accepted')

    def test_wrong_claim_code_is_rejected(self):
        self._begin()

        status, body = self._confirm('WRONGCOD')

        self.assertEqual(status, 404)

    def test_expired_claim_code_is_rejected(self):
        session = self._begin()
        with connection() as db:
            db.execute("UPDATE appliance_claims SET expires_at=? WHERE claim_session_id=?", ('2020-01-01T00:00:00', session['claim_session_id']))

        status, body = self._confirm(session['claim_code'])

        self.assertEqual(status, 404)

    def test_unauthorized_site_is_rejected(self):
        session = self._begin()
        with connection() as db:
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ('other-cust', self.partner_id, 'Other', 'other@example.test', 'active', 'real', '2026-09-10T00:00:00'))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ('other-site', 'other-cust', 'Other Site', '2026-09-10T00:00:00'))

        status, body = self._confirm(session['claim_code'], site_id='other-site')

        self.assertEqual(status, 403)
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (self.device_id.upper(),)).fetchone()['n']
        self.assertEqual(count, 0)

    def test_abandoned_claim_expires(self):
        session = self._begin()
        status, confirm_body = self._confirm(session['claim_code'])
        self.assertEqual(status, 200)
        with connection() as db:
            db.execute("UPDATE appliance_claims SET proof_expires_at=? WHERE claim_session_id=?", ('2020-01-01T00:00:00', session['claim_session_id']))

        status, status_body = _http_post('/api/appliance/claim/status', {'claim_session_id': session['claim_session_id']})

        self.assertEqual(status_body, {'status': 'expired'})
        with connection() as db:
            row = db.execute("SELECT claim_proof_encrypted FROM appliance_claims WHERE claim_session_id=?", (session['claim_session_id'],)).fetchone()
        self.assertIsNone(row['claim_proof_encrypted'])

    def test_appliance_restart_while_waiting(self):
        session = self._begin()

        # "Process A": opens/resumes the claim and persists local
        # state, then is discarded (standing in for a crash/reboot
        # before completion).
        config_a = self._agent_config()
        client_a = PortalClient(config_a.portal_url)
        state_a = setup_wizard._open_or_resume_claim(client_a, config_a, self.device_id)

        status, confirm_body = self._confirm(session['claim_code'])
        self.assertEqual(status, 200, confirm_body)

        # "Process B": a fresh config/client pointed at the SAME
        # state_dir, standing in for the appliance restarting.
        config_b = self._agent_config()
        client_b = PortalClient(config_b.portal_url)
        state_b = setup_wizard._open_or_resume_claim(client_b, config_b, self.device_id)
        self.assertEqual(state_b['claim_session_id'], state_a['claim_session_id'], 'process B must resume the SAME claim, never open a second one')

        claim_proof = setup_wizard._wait_for_claim_proof(client_b, config_b, state_b, sleep_fn=lambda _: None)
        activated = setup_wizard._complete_claim_with_retry(client_b, config_b, state_b, sleep_fn=lambda _: None)

        self.assertEqual(activated['customer_id'], self.customer_id)
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (self.device_id.upper(),)).fetchone()['n']
        self.assertEqual(count, 1)

    def test_lost_completion_response_then_retry(self):
        session = self._begin()
        status, _ = self._confirm(session['claim_code'])
        self.assertEqual(status, 200)
        config = self._agent_config()
        client = PortalClient(config.portal_url)
        state = {'device_id': self.device_id, 'claim_session_id': session['claim_session_id']}
        setup_wizard._wait_for_claim_proof(client, config, state, sleep_fn=lambda _: None)

        first = setup_wizard._complete_claim_with_retry(client, config, state, sleep_fn=lambda _: None)
        # Simulate the response being lost by simply calling again with
        # the identical, already-consumed session+proof pair -- exactly
        # what a retried anyaicam-setup --claim would do after a
        # restart, since claim_proof was durably persisted in `state`
        # before the first completion attempt.
        second = setup_wizard._complete_claim_with_retry(client, config, state, sleep_fn=lambda _: None)

        self.assertEqual(first, second, 'a retry must recover the identical activation result')
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (self.device_id.upper(),)).fetchone()['n']
        self.assertEqual(count, 1)

    def test_customer_double_confirm(self):
        session = self._begin()

        first_status, first_body = self._confirm(session['claim_code'])
        second_status, second_body = self._confirm(session['claim_code'])

        self.assertEqual(first_status, 200)
        self.assertIn(second_status, (403, 404, 409))

    def test_concurrent_completion(self):
        session = self._begin()
        status, _ = self._confirm(session['claim_code'])
        self.assertEqual(status, 200)
        status, status_body = _http_post('/api/appliance/claim/status', {'claim_session_id': session['claim_session_id']})
        claim_proof = status_body['claim_proof']

        results = []
        lock = threading.Lock()

        def worker():
            r = _http_post('/api/appliance/claim/complete', {'claim_session_id': session['claim_session_id'], 'claim_proof': claim_proof})
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        successes = [body for status, body in results if status == 200]
        self.assertGreaterEqual(len(successes), 1)
        self.assertEqual(len({body['appliance_id'] for body in successes}), 1)
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (self.device_id.upper(),)).fetchone()['n']
        self.assertEqual(count, 1)

    def test_missing_claim_flow_secret_key_fails_closed_without_leaking_secrets(self):
        session = self._begin()
        saved = os.environ.pop('ANYAICAM_CLAIM_FLOW_SECRET_KEY', None)
        try:
            status, body = self._confirm(session['claim_code'])
        finally:
            if saved is not None:
                os.environ['ANYAICAM_CLAIM_FLOW_SECRET_KEY'] = saved

        self.assertEqual(status, 503)
        self.assertNotIn(session['claim_code'], json.dumps(body))
        self.assertNotIn(saved or '', json.dumps(body))
        with connection() as db:
            claim_status = db.execute("SELECT status FROM appliance_claims WHERE claim_session_id=?", (session['claim_session_id'],)).fetchone()['status']
        self.assertEqual(claim_status, 'pending', 'a failed-closed confirm must never leave the claim half-claimed')

        # Confirming the key removal really was the cause, not
        # something else: the identical request succeeds once the key
        # is restored (already true again by this point via `finally`
        # above, but re-asserted explicitly for clarity).
        status, body = self._confirm(session['claim_code'])
        self.assertEqual(status, 200, body)


if __name__ == '__main__':
    unittest.main()
