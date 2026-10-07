"""Headless label claim (2026-10-07, anyaicam_agent/headless_claim.py): an
unclaimed appliance with only power and Ethernet opens, keeps open and
completes its own claim while anyaicam-agent.service waits for activation.
Everything here runs against a fake PortalClient; nothing touches a network."""
import hashlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from anyaicam_agent.config import AgentConfig, load_claim_state, save_claim_state, save_credential
from anyaicam_agent.headless_claim import (CLAIMED_RETRY_SECONDS, IN_PROGRESS_RETRY_SECONDS, PENDING_POLL_SECONDS,
                                           PROVISIONED_RETRY_SECONDS, HeadlessClaim, ineligibility)
from anyaicam_agent.portal import PortalError
from anyaicam_agent.service import ApplianceAgent

DEVICE_ID = '11111111-1111-4111-8111-111111111111'
LABEL = '7K3M9QX2H4TB'
VERIFIER = hashlib.sha256(('anyaicam-label-claim-v1:' + LABEL).encode()).hexdigest()
PORTAL = 'https://app.anyaicam.example'


class FakePortal:
    """Records calls; each method's next answers are queued per test."""
    def __init__(self):
        self.calls = []
        self.begin_answers, self.status_answers, self.complete_answers = [], [], []

    def _next(self, queue, name):
        answer = queue.pop(0) if queue else RuntimeError(f'unexpected {name}')
        if isinstance(answer, Exception):
            raise answer
        return answer

    def claim_begin(self, device_id, device_secret, label_verifier=None):
        self.calls.append(('begin', device_id, device_secret, label_verifier))
        return self._next(self.begin_answers, 'begin')

    def claim_status(self, claim_session_id, device_secret):
        self.calls.append(('status', claim_session_id, device_secret))
        return self._next(self.status_answers, 'status')

    def claim_complete(self, claim_session_id, claim_proof, device_secret):
        self.calls.append(('complete', claim_session_id, claim_proof, device_secret))
        return self._next(self.complete_answers, 'complete')


def _http(status, message):
    error = PortalError(message)
    error.status_code = status
    return error


ACTIVATED = {'appliance_id': 'appl-1', 'cloud_id': DEVICE_ID.upper(), 'credential': 'permanent-credential-value',
             'credential_id': 'cred-1', 'partner_id': None, 'customer_id': 'cust-1', 'site_id': 'site-1'}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.config = AgentConfig(cloud_id='', portal_url=PORTAL, state_dir=str(self.folder), config_dir=str(self.folder), log_dir=str(self.folder))
        (self.folder / 'appliance_identity.json').write_text(json.dumps({'appliance_id': DEVICE_ID}), encoding='utf-8')
        (self.folder / 'label_claim.json').write_text(json.dumps({'version': 1, 'verifier': VERIFIER}), encoding='utf-8')
        self.portal = FakePortal()
        self.finished = []
        self.log = logging.getLogger('test.headless')

    def claim(self, finish=None):
        return HeadlessClaim(self.config, self.log, client=self.portal,
                             finish_enrollment=finish or (lambda config, activated, ask_discovery=True: self.finished.append((activated, ask_discovery))))


class EligibilityTests(Base):
    def test_eligible_with_label_identity_and_cloud_portal(self):
        self.assertIsNone(ineligibility(self.config))

    def test_not_eligible_without_label_file(self):
        (self.folder / 'label_claim.json').unlink()
        self.assertIn('label claim file', ineligibility(self.config))
        self.assertIsNone(HeadlessClaim.for_config(self.config, self.log))

    def test_not_eligible_with_a_bad_verifier(self):
        (self.folder / 'label_claim.json').write_text(json.dumps({'verifier': 'not-hex'}), encoding='utf-8')
        self.assertIn('verifier', ineligibility(self.config))

    def test_not_eligible_without_a_uuid4_identity(self):
        (self.folder / 'appliance_identity.json').write_text(json.dumps({'appliance_id': 'AIC-1234'}), encoding='utf-8')
        self.assertIn('appliance_id', ineligibility(self.config))

    def test_not_eligible_with_the_installers_local_placeholder_portal(self):
        for url in ('http://127.0.0.1:8000', 'http://app.anyaicam.example', 'https://localhost', 'https://127.0.0.1'):
            self.config.portal_url = url
            self.assertIsNotNone(ineligibility(self.config), url)


class ClaimFlowTests(Base):
    def test_full_flow_opens_waits_completes_and_enrolls_without_discovery_prompt(self):
        claim = self.claim()
        self.portal.begin_answers.append({'claim_session_id': 'sess-1', 'claim_code': 'ABCD1234'})
        self.assertEqual(claim.step(), PENDING_POLL_SECONDS)
        kind, device_id, secret, verifier = self.portal.calls[0]
        self.assertEqual((kind, device_id, verifier), ('begin', DEVICE_ID, VERIFIER))
        self.assertGreaterEqual(len(secret), 32)
        state = load_claim_state(self.config)
        self.assertEqual(state['claim_session_id'], 'sess-1')
        self.assertEqual(state['device_secret'], secret)
        self.assertEqual(state['portal_origin'], PORTAL)

        self.portal.status_answers.append({'status': 'pending'})
        self.assertEqual(claim.step(), PENDING_POLL_SECONDS)

        self.portal.status_answers.append({'status': 'claimed', 'claim_proof': 'proof-1'})
        self.assertEqual(claim.step(), CLAIMED_RETRY_SECONDS)
        self.assertEqual(load_claim_state(self.config)['claim_proof'], 'proof-1')  # persisted before redeeming

        self.portal.complete_answers.append(dict(ACTIVATED))
        self.assertEqual(claim.step(), 0.0)
        self.assertEqual(self.portal.calls[-1], ('complete', 'sess-1', 'proof-1', secret))
        self.assertEqual(self.finished, [(ACTIVATED, False)])
        self.assertIsNone(load_claim_state(self.config))
        self.assertTrue(claim.done)

    def test_expired_claim_is_replaced_so_the_label_code_keeps_working(self):
        claim = self.claim()
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'old', 'device_secret': 's' * 40, 'portal_origin': PORTAL})
        self.portal.status_answers.append({'status': 'expired'})
        self.assertEqual(claim.step(), 0.0)
        self.assertIsNone(load_claim_state(self.config))
        self.portal.begin_answers.append({'claim_session_id': 'new'})
        claim.step()
        self.assertEqual(load_claim_state(self.config)['claim_session_id'], 'new')

    def test_restart_after_confirmation_resumes_with_the_saved_proof(self):
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'sess-1', 'device_secret': 'd' * 40,
                                       'portal_origin': PORTAL, 'claim_proof': 'proof-1'})
        claim = self.claim()  # a new process after a reboot
        self.portal.complete_answers.append(dict(ACTIVATED))
        claim.step()
        self.assertEqual(self.portal.calls, [('complete', 'sess-1', 'proof-1', 'd' * 40)])
        self.assertEqual(len(self.finished), 1)

    def test_lost_completion_response_is_retried_with_the_same_proof(self):
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'sess-1', 'device_secret': 'd' * 40,
                                       'portal_origin': PORTAL, 'claim_proof': 'proof-1'})
        claim = self.claim()
        self.portal.complete_answers += [PortalError('timed out'), dict(ACTIVATED)]
        self.assertGreater(claim.step(), 0)
        self.assertEqual(load_claim_state(self.config)['claim_proof'], 'proof-1')
        claim.step()
        self.assertEqual([c[2] for c in self.portal.calls], ['proof-1', 'proof-1'])
        self.assertEqual(len(self.finished), 1)

    def test_refused_completion_starts_a_new_claim(self):
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'sess-1', 'device_secret': 'd' * 40,
                                       'portal_origin': PORTAL, 'claim_proof': 'spent'})
        claim = self.claim()
        self.portal.complete_answers.append(_http(403, 'Claim proof is invalid or expired.'))
        claim.step()
        self.assertIsNone(load_claim_state(self.config))
        self.assertEqual(self.finished, [])

    def test_local_enrollment_failure_keeps_the_proof_for_a_retry(self):
        def failing_finish(config, activated, ask_discovery=True):
            raise SystemExit('rollback completed')
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 'sess-1', 'device_secret': 'd' * 40,
                                       'portal_origin': PORTAL, 'claim_proof': 'proof-1'})
        claim = self.claim(finish=failing_finish)
        self.portal.complete_answers.append(dict(ACTIVATED))
        self.assertGreater(claim.step(), 0)
        self.assertEqual(load_claim_state(self.config)['claim_proof'], 'proof-1')
        self.assertFalse(claim.done)

    def test_claim_for_another_portal_or_device_is_discarded_not_reused(self):
        for foreign in ({'portal_origin': 'https://evil.example'}, {'device_id': '22222222-2222-4222-9222-222222222222'}, {'device_secret': ''}):
            state = {'device_id': DEVICE_ID, 'claim_session_id': 'sess-x', 'device_secret': 'd' * 40, 'portal_origin': PORTAL, 'claim_proof': 'p'}
            state.update(foreign)
            save_claim_state(self.config, state)
            self.portal.calls.clear()
            self.portal.begin_answers.append({'claim_session_id': 'fresh'})
            self.claim().step()
            self.assertEqual([c[0] for c in self.portal.calls], ['begin'], foreign)
            self.assertEqual(load_claim_state(self.config)['claim_session_id'], 'fresh')

    def test_claim_still_open_elsewhere_waits(self):
        self.portal.begin_answers.append(_http(409, 'A claim is already in progress for this device.'))
        self.assertEqual(self.claim().step(), IN_PROGRESS_RETRY_SECONDS)
        self.assertIsNone(load_claim_state(self.config))

    def test_already_provisioned_waits_long(self):
        self.portal.begin_answers.append(_http(409, 'This device is already provisioned. Use the existing activation flow.'))
        self.assertEqual(self.claim().step(), PROVISIONED_RETRY_SECONDS)

    def test_network_errors_back_off_and_never_raise(self):
        claim = self.claim()
        waits = []
        for _ in range(8):
            self.portal.begin_answers.append(PortalError('unreachable'))
            waits.append(claim.step())
        self.assertEqual(waits[:4], [5.0, 10.0, 20.0, 40.0])
        self.assertEqual(max(waits), 300.0)

    def test_unexpected_exceptions_never_escape(self):
        claim = self.claim()
        claim.client = MagicMock()
        claim.client.claim_begin.side_effect = ValueError('boom')
        self.assertGreater(claim.step(), 0)

    def test_no_secret_is_ever_logged(self):
        claim = self.claim()
        with self.assertLogs('test.headless', level='DEBUG') as captured:
            self.portal.begin_answers.append({'claim_session_id': 'sess-secret-id'})
            claim.step()
            self.portal.status_answers += [PortalError('down'), {'status': 'claimed', 'claim_proof': 'proof-secret'}]
            claim.step(); claim.step()
            self.portal.complete_answers += [PortalError('down'), dict(ACTIVATED)]
            claim.step(); claim.step()
            self.log.debug('end')
        text = '\n'.join(captured.output)
        secret = self.portal.calls[0][2]
        for value in (secret, 'sess-secret-id', 'proof-secret', 'permanent-credential-value', VERIFIER, LABEL):
            self.assertNotIn(value, text)


class ServiceIntegrationTests(Base):
    def test_waiting_service_claims_itself_and_resumes_normal_operation(self):
        agent = ApplianceAgent(self.config)
        portal = self.portal
        portal.begin_answers.append({'claim_session_id': 'sess-1'})
        portal.status_answers += [{'status': 'pending'}, {'status': 'claimed', 'claim_proof': 'proof-1'}]
        portal.complete_answers.append(dict(ACTIVATED))

        def finish(config, activated, ask_discovery=True):
            self.assertFalse(ask_discovery)
            save_credential(config, {'appliance_id': activated['appliance_id'], 'credential_id': activated['credential_id'], 'credential': activated['credential']})

        agent.headless_claim_factory = lambda config, log: HeadlessClaim(config, log, client=portal, finish_enrollment=finish)
        clock = [1000.0]
        waits = []

        def fake_wait(seconds):  # no real sleeping: time moves by exactly the wait
            waits.append(seconds)
            clock[0] += seconds
        agent.stop_event.wait = fake_wait
        with patch('anyaicam_agent.service.time.monotonic', side_effect=lambda: clock[0]):
            agent._await_activation()
        self.assertTrue(all(0.5 <= w <= 10 for w in waits), waits)  # never busy-loops, never sleeps past a check
        self.assertEqual(agent.client.credential, 'permanent-credential-value')
        self.assertEqual([c[0] for c in portal.calls], ['begin', 'status', 'status', 'complete'])

    def test_active_appliance_removes_a_leftover_headless_claim_state(self):
        save_credential(self.config, {'appliance_id': 'a', 'credential_id': 'c', 'credential': 'x'})
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 's', 'device_secret': 'd' * 40,
                                       'portal_origin': PORTAL, 'claim_proof': 'spent', 'headless': True})
        ApplianceAgent(self.config)._await_activation()
        self.assertIsNone(load_claim_state(self.config))

    def test_active_appliance_leaves_a_terminal_claim_state_to_anyaicam_setup(self):
        save_credential(self.config, {'appliance_id': 'a', 'credential_id': 'c', 'credential': 'x'})
        save_claim_state(self.config, {'device_id': DEVICE_ID, 'claim_session_id': 's', 'device_secret': 'd' * 40, 'portal_origin': PORTAL})
        ApplianceAgent(self.config)._await_activation()
        self.assertIsNotNone(load_claim_state(self.config))

    def test_ineligible_service_only_waits_as_before(self):
        (self.folder / 'label_claim.json').unlink()
        agent = ApplianceAgent(self.config)
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                save_credential(self.config, {'appliance_id': 'a', 'credential_id': 'c', 'credential': 'set-by-anyaicam-setup'})
        agent.stop_event.wait = fake_wait
        agent._await_activation()
        self.assertEqual(waits, [10])
        self.assertEqual(agent.client.credential, 'set-by-anyaicam-setup')


if __name__ == '__main__':
    unittest.main()
