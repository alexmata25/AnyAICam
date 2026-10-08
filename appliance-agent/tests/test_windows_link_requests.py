"""Windows zero-terminal linking (2026-10-08): on-demand claims without a
label (HeadlessClaim(on_demand=True)) and the Administrators-only link
requests (anyaicam_agent/windows_link.py) that replace the Linux loopback
"AnyAiCam Setup" page on Windows.

The permanent label code of Windows 1.3.0 is retired: a Windows PC opens a
claim only when an administrator asks to link it, without a label verifier,
so only the fresh code in that one link can claim it. Fake cloud of
test_headless_claim.py; nothing touches a network or opens a socket."""
import json
import logging
import os
import time
import unittest
from unittest import mock

from anyaicam_agent import service, windows
from anyaicam_agent.config import load_claim_state, save_claim_state, save_credential
from anyaicam_agent.headless_claim import ON_DEMAND_IDLE_SECONDS, PENDING_POLL_SECONDS, HeadlessClaim, ineligibility
from anyaicam_agent.windows_link import REQUEST_MAX_AGE, RESPONSE_MAX_AGE, STARTUP_GRACE, LinkRequests, request_folder
from test_headless_claim import ACTIVATED, DEVICE_ID, PORTAL, Base

NONCE = '0123456789abcdef0123456789abcdef'


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(record.getMessage())


class OnDemandBase(Base):
    def setUp(self):
        super().setUp()
        (self.folder / 'label_claim.json').unlink()  # Windows: no label

    def claim(self, finish=None):
        return HeadlessClaim(self.config, self.log, client=self.portal, on_demand=True,
                             finish_enrollment=finish or (lambda config, activated, ask_discovery=True: self.finished.append((activated, ask_discovery))))


# ====================================================================== on-demand claims
class OnDemandEligibilityTests(OnDemandBase):
    def test_needs_no_label(self):
        self.assertIsNone(ineligibility(self.config, on_demand=True))
        self.assertIsNotNone(HeadlessClaim.for_config(self.config, self.log, on_demand=True))

    def test_the_linux_appliance_still_needs_its_label(self):
        self.assertIn('label claim file', ineligibility(self.config))
        self.assertIsNone(HeadlessClaim.for_config(self.config, self.log))

    def test_still_needs_an_identity_and_an_https_cloud(self):
        self.config.portal_url = 'http://127.0.0.1:8000'
        self.assertIsNotNone(ineligibility(self.config, on_demand=True))
        self.config.portal_url = PORTAL
        (self.folder / 'appliance_identity.json').unlink()
        self.assertIsNotNone(ineligibility(self.config, on_demand=True))


class OnDemandClaimTests(OnDemandBase):
    def test_no_standing_claim_is_opened(self):
        claim = self.claim()
        for _ in range(3):
            self.assertEqual(claim.step(), ON_DEMAND_IDLE_SECONDS)
        self.assertEqual(self.portal.calls, [])

    def test_a_link_opens_a_claim_without_any_label(self):
        self.portal.begin_answers = [{'claim_session_id': 'sess-1', 'claim_code': 'AB12CD34'}]
        self.assertEqual(self.claim().link_code(), 'AB12CD34')
        self.assertEqual(self.portal.calls, [('begin', DEVICE_ID, load_claim_state(self.config)['device_secret'], None)])

    def test_each_link_gets_a_fresh_code_for_the_same_claim(self):
        self.portal.begin_answers = [{'claim_session_id': 'sess-1', 'claim_code': 'AAAA0001'},
                                     {'claim_session_id': 'sess-1', 'claim_code': 'BBBB0002'}]
        claim = self.claim()
        self.assertEqual(claim.link_code(), 'AAAA0001')
        secret = load_claim_state(self.config)['device_secret']
        self.assertEqual(claim.link_code(), 'BBBB0002')
        self.assertEqual(self.portal.calls[-1], ('begin', DEVICE_ID, secret, None))  # resumed, still no label

    def test_the_opened_claim_is_polled_and_completed(self):
        self.portal.begin_answers = [{'claim_session_id': 'sess-1', 'claim_code': 'AB12CD34'}]
        claim = self.claim()
        claim.link_code()
        self.portal.status_answers = [{'status': 'pending'}, {'status': 'claimed', 'claim_proof': 'proof-1'}]
        self.portal.complete_answers = [ACTIVATED]
        self.assertEqual(claim.step(), PENDING_POLL_SECONDS)
        claim.step(); claim.step()
        self.assertTrue(claim.done)
        self.assertEqual(self.finished, [(ACTIVATED, False)])

    def test_an_expired_claim_is_let_go_not_reopened(self):
        self.portal.begin_answers = [{'claim_session_id': 'sess-1', 'claim_code': 'AB12CD34'}]
        claim = self.claim()
        claim.link_code()
        self.portal.status_answers = [{'status': 'expired'}]
        self.assertEqual(claim.step(), 0.0)
        self.assertIsNone(load_claim_state(self.config))
        self.assertEqual(claim.step(), ON_DEMAND_IDLE_SECONDS)
        self.assertEqual([c[0] for c in self.portal.calls], ['begin', 'status'])

    def test_a_claim_left_open_by_windows_1_3_0_is_resumed_without_its_label(self):
        # Upgrade from 1.3.0 (label claim open): the next link resumes it with
        # the same device secret and sends no label; it expires as usual.
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'old', 'device_secret': 'secret-1',
                                       'portal_origin': PORTAL, 'headless': True})
        self.portal.status_answers = [{'status': 'pending'}]
        claim = self.claim()
        self.assertEqual(claim.step(), PENDING_POLL_SECONDS)
        self.portal.begin_answers = [{'claim_session_id': 'old', 'claim_code': 'CCCC0003'}]
        self.assertEqual(claim.link_code(), 'CCCC0003')
        self.assertEqual(self.portal.calls[-1], ('begin', DEVICE_ID, 'secret-1', None))


# ====================================================================== link requests
class FakeClaim:
    def __init__(self, codes):
        self.codes = list(codes)
        self.done = False

    def link_code(self):
        return self.codes.pop(0) if self.codes else None


class LinkRequestTests(OnDemandBase):
    def setUp(self):
        super().setUp()
        self.capture = Capture(); self.log.addHandler(self.capture); self.addCleanup(self.log.removeHandler, self.capture)
        self.log.setLevel(logging.DEBUG); self.addCleanup(self.log.setLevel, logging.NOTSET)
        self.claim_object = FakeClaim(['AB12CD34', 'EF56GH78'])
        self.is_linked = False
        self.requests = LinkRequests(self.config, self.log, headless=lambda: self.claim_object, linked=lambda: self.is_linked)
        self.dir = request_folder(self.config.state_dir)
        self.dir.mkdir()

    def ask(self, nonce=NONCE, age=0):
        path = self.dir / f'{nonce}.request'
        path.write_text('{}')
        if age:
            old = time.time() - age
            os.utime(path, (old, old))
        return path

    def answer(self, nonce=NONCE):
        path = self.dir / f'{nonce}.response'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def test_answers_with_a_link_that_carries_a_fresh_code_only_in_the_fragment(self):
        self.ask()
        self.assertEqual(self.requests.poll_once(), 1)
        self.assertEqual(self.answer(), {'status': 'ok', 'link': f'{PORTAL}/claim#code=AB12CD34&link=windows'})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [f'{NONCE}.response'])  # request gone, no .tmp
        other = 'f' * 32
        self.ask(other)
        self.requests.poll_once()
        self.assertEqual(self.answer(other)['link'], f'{PORTAL}/claim#code=EF56GH78&link=windows')

    def test_a_linked_pc_says_so(self):
        self.is_linked = True
        self.ask(); self.requests.poll_once()
        self.assertEqual(self.answer(), {'status': 'linked'})
        self.assertEqual(self.claim_object.codes, ['AB12CD34', 'EF56GH78'])  # no claim opened

    def test_waits_briefly_for_the_claim_then_says_unavailable(self):
        # Right after the service starts its claim may not exist yet.
        self.claim_object = None
        request = self.ask()
        self.assertEqual(self.requests.poll_once(), 0)
        self.assertIsNone(self.answer())
        self.assertTrue(request.exists())  # kept for the next poll
        self.claim_object = FakeClaim(['AB12CD34'])
        self.requests.poll_once()
        self.assertEqual(self.answer()['status'], 'ok')

    def test_unavailable_without_a_claim_or_a_code(self):
        self.claim_object = None
        self.ask(age=STARTUP_GRACE + 1); self.requests.poll_once()
        self.assertEqual(self.answer(), {'status': 'unavailable'})
        self.claim_object = FakeClaim(['not a code'])
        self.ask(); self.requests.poll_once()
        self.assertEqual(self.answer(), {'status': 'unavailable'})

    def test_only_an_https_cloud(self):
        self.config.portal_url = 'http://app.anyaicam.example'
        self.ask(); self.requests.poll_once()
        self.assertEqual(self.answer(), {'status': 'unavailable'})

    def test_malformed_old_or_odd_entries_are_never_answered(self):
        for name in ('ABCDEF0123456789abcdef0123456789.request', '../x.request', 'short.request'):
            try:
                (self.dir / name).write_text('{}')
            except OSError:
                pass
        self.ask(age=REQUEST_MAX_AGE + 5)
        (self.dir / ('e' * 32 + '.request')).mkdir()  # not a file
        self.assertEqual(self.requests.poll_once(), 0)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith('.response')], [])
        self.assertEqual(self.claim_object.codes, ['AB12CD34', 'EF56GH78'])

    def test_unread_answers_are_deleted(self):
        self.ask(); self.requests.poll_once()
        response = self.dir / f'{NONCE}.response'
        old = time.time() - RESPONSE_MAX_AGE - 5
        os.utime(response, (old, old))
        self.requests.poll_once()
        self.assertFalse(response.exists())

    def test_the_code_is_never_logged(self):
        self.ask(); self.requests.poll_once()
        self.assertTrue(self.capture.text)
        self.assertFalse(any('AB12CD34' in line for line in self.capture.text), self.capture.text)

    def test_the_thread_starts_and_stops(self):
        started = self.requests.start()
        self.ask()
        deadline = time.time() + 5
        while self.answer() is None and time.time() < deadline:
            time.sleep(0.05)
        started.stop()
        self.assertEqual(self.answer()['status'], 'ok')


# ====================================================================== service wiring
class ServiceWiringTests(OnDemandBase):
    def agent(self):
        return service.ApplianceAgent(self.config)

    def test_windows_never_starts_the_loopback_page_even_if_asked(self):
        with mock.patch.object(windows, 'IS_WINDOWS', True), mock.patch.dict(os.environ, {'ANYAICAM_LINK_PORT': '8790'}), \
                mock.patch.object(service, 'LinkServer', side_effect=AssertionError('LinkServer on Windows')):
            agent = self.agent()
            self.assertIsNone(agent.start_link_server())

    def test_windows_answers_link_requests_and_claims_on_demand(self):
        with mock.patch.object(windows, 'IS_WINDOWS', True):
            agent = self.agent()
            claim = agent.headless_claim_factory(self.config, self.log)
            self.assertTrue(claim.on_demand)
            requests = agent.start_link_requests()
            self.addCleanup(requests.stop)
            self.assertIsInstance(requests, LinkRequests)
            self.assertTrue(request_folder(self.config.state_dir).is_dir())

    def test_linux_keeps_its_page_and_label_claim_and_has_no_link_requests(self):
        agent = self.agent()
        self.assertIsNone(agent.start_link_requests())
        (self.folder / 'label_claim.json').write_text(json.dumps({'verifier': 'a' * 64}), encoding='utf-8')
        self.assertFalse(agent.headless_claim_factory(self.config, self.log).on_demand)

    def test_linked_is_reported_from_the_credential(self):
        agent = self.agent()
        self.assertFalse(agent.linked())
        save_credential(self.config, {'appliance_id': 'appl-1', 'credential': 'secret-credential'})
        self.assertTrue(agent.linked())


if __name__ == '__main__':
    unittest.main()
