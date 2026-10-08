"""Zero-terminal onboarding (2026-10-08): the appliance's own "AnyAiCam Setup"
page (anyaicam_agent/link_server.py) and HeadlessClaim.link_code().

A customer links the appliance from a browser on the appliance itself: the
page sends the browser to <portal>/claim#code=<code> -- nobody sees, copies or
types a Cloud ID, activation token or claim code. These tests run the real
HTTP server on an ephemeral loopback port against the fake cloud of
test_headless_claim.py; nothing touches a real network."""
import http.client
import json
import logging
import re
import threading
import unittest
from urllib.parse import urlsplit

from anyaicam_agent.config import load_claim_state, save_claim_state, save_credential
from anyaicam_agent.link_server import LINK_LIMIT, LinkServer
from test_headless_claim import ACTIVATED, DEVICE_ID, PORTAL, VERIFIER, Base, _http

TOKEN_FIELD = re.compile(r'name="token" value="([^"]+)"')


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(record.getMessage())


# ====================================================================== link_code()
class LinkCodeTests(Base):
    def test_opens_the_claim_when_none_is_open_and_the_step_loop_follows_it(self):
        self.portal.begin_answers = [{'claim_session_id': 'sess-1', 'claim_code': 'AB12CD34'}]
        claim = self.claim()
        self.assertEqual(claim.link_code(), 'AB12CD34')
        state = load_claim_state(self.config)
        self.assertEqual((state['claim_session_id'], state['device_id'], state['portal_origin']), ('sess-1', DEVICE_ID, PORTAL))
        self.assertNotIn('AB12CD34', json.dumps(state))  # the code is never stored
        self.assertEqual(self.portal.calls[0][3], VERIFIER)  # same label verifier as the headless claim
        self.portal.status_answers = [{'status': 'pending'}]
        claim.step()
        self.assertEqual(self.portal.calls[-1][:2], ('status', 'sess-1'))

    def test_resumes_the_open_claim_with_its_own_device_secret(self):
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'sess-1', 'device_secret': 'secret-1',
                                       'portal_origin': PORTAL, 'headless': True})
        self.portal.begin_answers = [{'claim_session_id': 'sess-1', 'claim_code': 'FFFF0001'}]
        self.assertEqual(self.claim().link_code(), 'FFFF0001')
        self.assertEqual(self.portal.calls, [('begin', DEVICE_ID, 'secret-1', VERIFIER)])
        self.assertEqual(load_claim_state(self.config)['claim_session_id'], 'sess-1')

    def test_after_a_reboot_a_new_process_resumes_the_same_claim(self):
        self.portal.begin_answers = [{'claim_session_id': 'sess-1', 'claim_code': 'AAAA0001'},
                                     {'claim_session_id': 'sess-1', 'claim_code': 'BBBB0002'}]
        self.claim().link_code()
        secret = load_claim_state(self.config)['device_secret']
        self.assertEqual(self.claim().link_code(), 'BBBB0002')  # a fresh HeadlessClaim, as after a reboot
        self.assertEqual(self.portal.calls[-1][2], secret)

    def test_follows_a_new_session_when_the_saved_one_expired(self):
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'old', 'device_secret': 'secret-1',
                                       'portal_origin': PORTAL, 'headless': True})
        self.portal.begin_answers = [{'claim_session_id': 'new', 'claim_code': 'CCCC0003'}]
        self.assertEqual(self.claim().link_code(), 'CCCC0003')
        self.assertEqual(load_claim_state(self.config)['claim_session_id'], 'new')

    def test_never_while_a_confirmation_is_being_redeemed(self):
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'sess-1', 'device_secret': 'secret-1',
                                       'portal_origin': PORTAL, 'claim_proof': 'proof', 'headless': True})
        self.assertIsNone(self.claim().link_code())
        self.assertEqual(self.portal.calls, [])

    def test_discards_a_claim_for_another_portal(self):
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'x', 'device_secret': 's',
                                       'portal_origin': 'https://elsewhere.example', 'headless': True})
        self.portal.begin_answers = [{'claim_session_id': 'sess-2', 'claim_code': 'DDDD0004'}]
        self.assertEqual(self.claim().link_code(), 'DDDD0004')
        self.assertNotEqual(self.portal.calls[0][2], 's')

    def test_cloud_failures_return_none_never_raise_and_never_log_a_code(self):
        capture = Capture(); self.log.addHandler(capture); self.addCleanup(self.log.removeHandler, capture)
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'sess-1', 'device_secret': 'secret-1',
                                       'portal_origin': PORTAL, 'headless': True})
        self.portal.begin_answers = [_http(503, 'down'), ConnectionError('boom'), {'claim_session_id': 'sess-1', 'claim_code': 'not a code!'}]
        claim = self.claim()
        self.assertIsNone(claim.link_code())
        self.assertIsNone(claim.link_code())
        self.assertIsNone(claim.link_code())  # malformed codes are never passed on
        self.assertFalse(any('secret-1' in line for line in capture.text))

    def test_an_already_provisioned_appliance_gets_none(self):
        self.portal.begin_answers = [_http(409, 'This device is already provisioned.')]
        self.assertIsNone(self.claim().link_code())

    def test_link_code_and_the_step_loop_never_run_at_once(self):
        claim = self.claim()
        entered, release = threading.Event(), threading.Event()

        def slow_begin(*args, **kwargs):
            entered.set(); release.wait(5)
            return {'claim_session_id': 'sess-1', 'claim_code': 'EEEE0005'}

        self.portal.claim_begin = slow_begin
        worker = threading.Thread(target=claim.step); worker.start()
        self.assertTrue(entered.wait(5))
        got = []
        linker = threading.Thread(target=lambda: got.append(claim.link_code())); linker.start()
        linker.join(0.3)
        self.assertTrue(linker.is_alive(), 'link_code must wait for the step in progress')
        release.set(); worker.join(5); linker.join(5)
        self.assertEqual(len(got), 1)


# ====================================================================== the page
class FakeClaim:
    def __init__(self, code='AB12CD34'):
        self.code, self.done, self.calls = code, False, 0

    def link_code(self):
        self.calls += 1
        return self.code


class LinkServerBase(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeClaim()
        self.is_linked = False
        self.capture = Capture(); self.log.addHandler(self.capture); self.log.setLevel(logging.DEBUG)
        self.addCleanup(self.log.removeHandler, self.capture)
        self.server = LinkServer(self.config, self.log, headless=lambda: self.fake, linked=lambda: self.is_linked, port=0).start()
        self.addCleanup(self.server.stop)
        self.host = f'127.0.0.1:{self.server.port}'

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=10)
        hdrs = {'Host': self.host}
        hdrs.update(headers or {})
        if body is not None:
            hdrs.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        conn.request(method, path, body=body, headers=hdrs)
        response = conn.getresponse()
        data = response.read().decode('utf-8', 'replace')
        conn.close()
        return response, data

    def token(self):
        return TOKEN_FIELD.search(self.request('GET', '/')[1]).group(1)

    def link(self, token=None, headers=None):
        hdrs = {'Origin': f'http://{self.host}', 'Sec-Fetch-Site': 'same-origin'}
        hdrs.update(headers or {})
        return self.request('POST', '/link', body=f'token={token if token is not None else self.token()}', headers=hdrs)


class LinkServerTests(LinkServerBase):
    def test_listens_on_loopback_only(self):
        self.assertEqual(self.server.httpd.server_address[0], '127.0.0.1')
        with self.assertRaises(ValueError):
            LinkServer(self.config, self.log, headless=lambda: None, linked=lambda: False, port=0, host='0.0.0.0')

    def test_unlinked_page_offers_one_button_and_shows_no_code_or_identifiers(self):
        response, page = self.request('GET', '/')
        self.assertEqual(response.status, 200)
        self.assertIn('Link this appliance', page)
        self.assertIn('nothing to copy or type', page)
        for hidden in ('AB12CD34', DEVICE_ID, VERIFIER, 'Cloud ID', 'activation token', 'claim code'):
            self.assertNotIn(hidden.lower(), page.lower())
        self.assertEqual(self.fake.calls, 0)  # nothing is fetched just by viewing the page
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertEqual(response.getheader('X-Frame-Options'), 'DENY')
        self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
        self.assertIn(f"form-action 'self' {PORTAL}", response.getheader('Content-Security-Policy'))

    def test_link_sends_the_browser_to_the_cloud_with_the_code_only_in_the_fragment(self):
        response, body = self.link()
        self.assertEqual(response.status, 303)
        location = urlsplit(response.getheader('Location'))
        self.assertEqual(f'{location.scheme}://{location.netloc}{location.path}', f'{PORTAL}/claim')
        self.assertEqual((location.query, location.fragment), ('', 'code=AB12CD34'))
        self.assertNotIn('AB12CD34', body)
        self.assertFalse(any('AB12CD34' in line for line in self.capture.text), 'the code must never be logged')

    def test_rejects_other_host_names_dns_rebinding(self):
        for host in ('evil.example', f'evil.example:{self.server.port}', '127.0.0.1', f'192.168.1.5:{self.server.port}'):
            response, _ = self.request('GET', '/', headers={'Host': host})
            self.assertEqual(response.status, 421, host)
            response, _ = self.link(headers={'Host': host})
            self.assertEqual(response.status, 421, host)
        self.assertEqual(self.fake.calls, 0)

    def test_link_requires_the_page_token_and_a_same_origin_request(self):
        for token, headers in (('', {}), ('wrong', {}), (None, {'Origin': 'https://evil.example'}),
                               (None, {'Origin': 'null'}), (None, {'Sec-Fetch-Site': 'cross-site'}),
                               (None, {'Sec-Fetch-Site': 'same-site'})):
            response, _ = self.link(token=token, headers=headers)
            self.assertEqual(response.status, 403, (token, headers))
        self.assertEqual(self.fake.calls, 0)

    def test_a_bad_body_length_is_refused_without_reading(self):
        for length in ('-1', 'abc', '99999'):
            response, _ = self.request('POST', '/link', body='', headers={'Origin': f'http://{self.host}', 'Content-Length': length})
            self.assertIn(response.status, (400, 413), length)
        self.assertEqual(self.fake.calls, 0)

    def test_link_is_rate_limited(self):
        token = self.token()
        statuses = [self.link(token=token)[0].status for _ in range(LINK_LIMIT + 1)]
        self.assertEqual(statuses, [303] * LINK_LIMIT + [429])

    def test_cloud_unreachable_shows_a_retry_page(self):
        self.fake.code = None
        response, page = self.link()
        self.assertEqual(response.status, 503)
        self.assertIn('try again', page)

    def test_not_available_without_a_headless_claim(self):
        server = LinkServer(self.config, self.log, headless=lambda: None, linked=lambda: False, port=0).start()
        self.addCleanup(server.stop)
        conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=10)
        conn.request('GET', '/', headers={'Host': f'127.0.0.1:{server.port}'})
        page = conn.getresponse().read().decode()
        self.assertIn('not available', page)
        self.assertNotIn('name="token"', page)

    def test_once_linked_the_page_and_link_lead_to_the_cloud_setup(self):
        token = self.token()  # a tab left open from before the appliance was linked
        self.is_linked = True
        response, page = self.request('GET', '/')
        self.assertIn('linked to your AnyAiCam account', page)
        self.assertIn(f'{PORTAL}/customer/setup', page)
        self.assertNotIn('name="token"', page)
        response, _ = self.link(token=token)
        self.assertEqual((response.status, response.getheader('Location')), (303, f'{PORTAL}/customer/setup'))
        self.assertEqual(self.fake.calls, 0)

    def test_status_endpoint_for_the_desktop_launcher(self):
        _, body = self.request('GET', '/status')
        self.assertEqual(json.loads(body), {'linked': False, 'can_link': True, 'portal': PORTAL})
        self.is_linked = True
        self.assertTrue(json.loads(self.request('GET', '/status')[1])['linked'])


class AgentWiringTests(Base):
    def test_agent_shares_its_headless_claim_with_the_page_and_reports_linked(self):
        from anyaicam_agent.service import ApplianceAgent
        agent = ApplianceAgent(self.config)
        self.assertFalse(agent.linked())
        save_credential(self.config, {'appliance_id': 'appl-1', 'credential': 'c'})
        self.assertTrue(agent.linked())

    def test_setup_page_can_be_turned_off_and_a_busy_port_is_not_fatal(self):
        import os
        from unittest.mock import patch
        from anyaicam_agent.service import ApplianceAgent
        agent = ApplianceAgent(self.config)
        with patch.dict(os.environ, {'ANYAICAM_LINK_PORT': '0'}):
            self.assertIsNone(agent.start_link_server())
        blocker = LinkServer(self.config, self.log, headless=lambda: None, linked=lambda: False, port=0)
        self.addCleanup(blocker.httpd.server_close)
        with patch.dict(os.environ, {'ANYAICAM_LINK_PORT': str(blocker.port)}):
            self.assertIsNone(agent.start_link_server())  # address in use -> warning only


class LateProvisioningTests(Base):
    """Codex finding 1 on fc50f44: on a fresh install the agent starts before
    the installer writes its identity / label (the installer now restarts it;
    this is the agent's own defense): while waiting it checks again, quietly,
    and opens its claim once it can -- no restart needed."""

    def test_a_label_written_after_start_is_picked_up_while_waiting(self):
        import time
        from unittest.mock import patch
        from anyaicam_agent import service
        from anyaicam_agent.headless_claim import HeadlessClaim
        label = self.folder / 'label_claim.json'
        saved = label.read_text(encoding='utf-8'); label.unlink()
        capture = Capture(); self.log.addHandler(capture); self.addCleanup(self.log.removeHandler, capture)
        agent = service.ApplianceAgent(self.config)
        agent.log = self.log
        agent.headless_claim_factory = lambda config, log: HeadlessClaim.for_config(config, log, client=self.portal, finish_enrollment=lambda *a, **k: None)
        self.portal.begin_answers = [{'claim_session_id': 'sess-late', 'claim_code': 'ABCD0001'}]
        self.portal.status_answers = [{'status': 'pending'}] * 50
        with patch.object(service, 'HEADLESS_RECHECK_SECONDS', 0.05), patch.object(service, 'ACTIVATION_POLL_INTERVAL_SECONDS', 0.05):
            worker = threading.Thread(target=agent._await_activation); worker.start()
            time.sleep(0.4)
            self.assertIsNone(agent.headless)  # still ineligible: no label yet
            label.write_text(saved, encoding='utf-8')
            deadline = time.time() + 5
            while not any(c[0] == 'begin' for c in self.portal.calls) and time.time() < deadline:
                time.sleep(0.05)
            agent.stop_event.set(); worker.join(5)
        self.assertIsNotNone(agent.headless)
        self.assertEqual([c for c in self.portal.calls if c[0] == 'begin'][0][3], VERIFIER)
        self.assertEqual(load_claim_state(self.config)['claim_session_id'], 'sess-late')
        not_available = [line for line in capture.text if 'Headless claim not available' in line]
        self.assertEqual(len(not_available), 1, 'the re-checks must not log on every attempt')


if __name__ == '__main__':
    unittest.main()
