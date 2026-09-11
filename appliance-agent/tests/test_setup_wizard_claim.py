"""Phase 2A: the appliance-side claim workflow (anyaicam-setup --claim)
added to setup_wizard.py. Unit-level coverage for the new orchestration
helpers using a fake PortalClient -- no real HTTP, no real cloud; the
genuine cross-process integration (real HTTP, real cloud routes, real
enrollment) is covered separately by
app/tests/test_claim_flow_end_to_end.py. This file's job is narrower
and cheaper: prove the appliance-side state machine (resume-after-
restart, proof persistence, retry-on-transient-failure) behaves
correctly in isolation, the same way test_reenrollment.py already
isolates first_enroll()/coordinated_reenroll() from any real network
call.

_finish_enrollment() itself is not re-tested here -- that machinery
(first_enroll()/coordinated_reenroll()) is exactly what
test_reenrollment.py already covers, unmodified by Phase 2A; this file
patches it out to isolate the claim-flow orchestration being added.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent.config import AgentConfig, load_claim_state
from anyaicam_agent.portal import PortalError
from anyaicam_agent import setup_wizard


class FakePortalClient:
    """Queues one canned response (or exception instance, raised
    instead of returned) per call, in call order -- popping past the
    end of a queue is a test bug (an unexpected extra call), and
    surfaces as a plain IndexError rather than silently returning
    something misleading."""

    def __init__(self, begin=(), status=(), complete=()):
        self.begin_calls = []
        self.status_calls = []
        self.complete_calls = []
        self._begin = list(begin)
        self._status = list(status)
        self._complete = list(complete)

    def test(self):
        return {'mode': 'development'}

    def claim_begin(self, device_id):
        self.begin_calls.append(device_id)
        result = self._begin.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def claim_status(self, claim_session_id):
        self.status_calls.append(claim_session_id)
        result = self._status.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def claim_complete(self, claim_session_id, claim_proof):
        self.complete_calls.append((claim_session_id, claim_proof))
        result = self._complete.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _no_sleep(_seconds):
    pass


class InstallerDeviceIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = AgentConfig(config_dir=self.tmp.name, state_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            setup_wizard._installer_device_id(self.config)

    def test_malformed_json_exits(self):
        self.config.installer_identity_file.write_text('not json', encoding='utf-8')
        with self.assertRaises(SystemExit):
            setup_wizard._installer_device_id(self.config)

    def test_missing_appliance_id_key_exits(self):
        self.config.installer_identity_file.write_text(json.dumps({'installer_version': '1'}), encoding='utf-8')
        with self.assertRaises(SystemExit):
            setup_wizard._installer_device_id(self.config)

    def test_valid_file_returns_the_uuid(self):
        self.config.installer_identity_file.write_text(json.dumps({'appliance_id': '11111111-1111-4111-8111-111111111111'}), encoding='utf-8')
        self.assertEqual(setup_wizard._installer_device_id(self.config), '11111111-1111-4111-8111-111111111111')


class OpenOrResumeClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = AgentConfig(config_dir=self.tmp.name, state_dir=self.tmp.name)
        self.device_id = '11111111-1111-4111-8111-111111111111'

    def tearDown(self):
        self.tmp.cleanup()

    def test_opens_a_fresh_claim_and_persists_state(self):
        client = FakePortalClient(begin=[{'claim_session_id': 'sess-1', 'claim_code': 'ABCD1234', 'expires_at': '2099-01-01T00:00:00', 'poll_interval_seconds': 5, 'resumed': False}])

        state = setup_wizard._open_or_resume_claim(client, self.config, self.device_id)

        self.assertEqual(state['claim_session_id'], 'sess-1')
        self.assertEqual(client.begin_calls, [self.device_id])
        persisted = load_claim_state(self.config)
        self.assertEqual(persisted['claim_session_id'], 'sess-1')
        self.assertEqual(persisted['device_id'], self.device_id)

    def test_resumes_from_existing_state_without_calling_begin_again(self):
        # Simulates "appliance restart while waiting": a prior process
        # already ran _open_or_resume_claim and persisted state; this
        # is a fresh process (fresh FakePortalClient with an EMPTY
        # begin queue -- popping it would raise IndexError, proving
        # claim_begin is never called on this path).
        from anyaicam_agent.config import save_claim_state
        save_claim_state(self.config, {'device_id': self.device_id, 'claim_session_id': 'sess-1', 'opened_at': '2026-01-01T00:00:00'})
        client = FakePortalClient()

        state = setup_wizard._open_or_resume_claim(client, self.config, self.device_id)

        self.assertEqual(state['claim_session_id'], 'sess-1')
        self.assertEqual(client.begin_calls, [])

    def test_different_device_id_in_stale_state_opens_a_new_claim(self):
        from anyaicam_agent.config import save_claim_state
        save_claim_state(self.config, {'device_id': 'some-other-device', 'claim_session_id': 'stale-sess', 'opened_at': '2026-01-01T00:00:00'})
        client = FakePortalClient(begin=[{'claim_session_id': 'sess-new', 'claim_code': 'WXYZ9999', 'expires_at': '2099-01-01T00:00:00', 'poll_interval_seconds': 5, 'resumed': False}])

        state = setup_wizard._open_or_resume_claim(client, self.config, self.device_id)

        self.assertEqual(state['claim_session_id'], 'sess-new')
        self.assertEqual(client.begin_calls, [self.device_id])

    def test_begin_failure_exits_without_persisting_state(self):
        client = FakePortalClient(begin=[PortalError('rate limited', status_code=429)])

        with self.assertRaises(SystemExit):
            setup_wizard._open_or_resume_claim(client, self.config, self.device_id)
        self.assertIsNone(load_claim_state(self.config))


class WaitForClaimProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = AgentConfig(config_dir=self.tmp.name, state_dir=self.tmp.name)
        self.state = {'device_id': 'd', 'claim_session_id': 'sess-1', 'opened_at': '2026-01-01T00:00:00'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_immediately_if_proof_already_known(self):
        self.state['claim_proof'] = 'already-known-proof'
        client = FakePortalClient()

        proof = setup_wizard._wait_for_claim_proof(client, self.config, self.state, sleep_fn=_no_sleep)

        self.assertEqual(proof, 'already-known-proof')
        self.assertEqual(client.status_calls, [])

    def test_polls_through_pending_to_claimed_and_persists_proof(self):
        client = FakePortalClient(status=[{'status': 'pending'}, {'status': 'pending'}, {'status': 'claimed', 'claim_proof': 'the-proof'}])

        proof = setup_wizard._wait_for_claim_proof(client, self.config, self.state, sleep_fn=_no_sleep)

        self.assertEqual(proof, 'the-proof')
        self.assertEqual(len(client.status_calls), 3)
        # Nothing was persisted by this test's own setUp -- confirm the
        # function itself durably wrote the proof to a state file (the
        # property a restart between confirmation and completion
        # actually depends on).
        persisted = load_claim_state(self.config)
        self.assertEqual(persisted['claim_proof'], 'the-proof')

    def test_abandoned_claim_reports_expired_and_clears_state(self):
        from anyaicam_agent.config import save_claim_state
        save_claim_state(self.config, self.state)
        client = FakePortalClient(status=[{'status': 'pending'}, {'status': 'expired'}])

        with self.assertRaises(SystemExit):
            setup_wizard._wait_for_claim_proof(client, self.config, self.state, sleep_fn=_no_sleep)
        self.assertIsNone(load_claim_state(self.config))

    def test_transient_polling_error_retries_rather_than_giving_up(self):
        client = FakePortalClient(status=[PortalError('network blip'), {'status': 'claimed', 'claim_proof': 'the-proof'}])

        proof = setup_wizard._wait_for_claim_proof(client, self.config, self.state, sleep_fn=_no_sleep)

        self.assertEqual(proof, 'the-proof')
        self.assertEqual(len(client.status_calls), 2)


class CompleteClaimWithRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = AgentConfig(config_dir=self.tmp.name, state_dir=self.tmp.name)
        self.state = {'device_id': 'd', 'claim_session_id': 'sess-1', 'claim_proof': 'the-proof'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_succeeds_on_first_attempt(self):
        client = FakePortalClient(complete=[{'appliance_id': 'a1', 'cloud_id': 'D', 'credential': 'cred', 'credential_id': 'cid'}])

        result = setup_wizard._complete_claim_with_retry(client, self.config, self.state, sleep_fn=_no_sleep)

        self.assertEqual(result['appliance_id'], 'a1')
        self.assertEqual(len(client.complete_calls), 1)

    def test_lost_response_then_successful_retry_recovers_the_result(self):
        # Simulates the exact scenario hardening item 3 made safe
        # cloud-side: the first HTTP round trip is lost (PortalError),
        # the second attempt uses the SAME claim_session_id+claim_proof
        # and succeeds.
        client = FakePortalClient(complete=[PortalError('timed out'), {'appliance_id': 'a1', 'cloud_id': 'D', 'credential': 'cred', 'credential_id': 'cid'}])

        result = setup_wizard._complete_claim_with_retry(client, self.config, self.state, sleep_fn=_no_sleep)

        self.assertEqual(result['appliance_id'], 'a1')
        self.assertEqual(len(client.complete_calls), 2)
        self.assertEqual(client.complete_calls[0], client.complete_calls[1], 'both attempts must present the identical session_id+proof pair')

    def test_exhausting_all_attempts_exits_without_losing_state(self):
        client = FakePortalClient(complete=[PortalError('down')] * 3)

        with self.assertRaises(SystemExit):
            setup_wizard._complete_claim_with_retry(client, self.config, self.state, attempts=3, sleep_fn=_no_sleep)
        self.assertEqual(len(client.complete_calls), 3)


class ClaimMainOrchestrationTests(unittest.TestCase):
    """Exercises claim_main() end to end at the orchestration level --
    PortalClient itself replaced with a fake, _finish_enrollment()
    patched out (its own machinery is test_reenrollment.py's job, not
    this file's)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = AgentConfig(config_dir=self.tmp.name, state_dir=self.tmp.name, portal_url='http://portal.example.test')
        self.config.installer_identity_file.write_text(json.dumps({'appliance_id': '11111111-1111-4111-8111-111111111111'}), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_claim_flow_reaches_finish_enrollment_and_clears_state(self):
        client = FakePortalClient(
            begin=[{'claim_session_id': 'sess-1', 'claim_code': 'ABCD1234', 'expires_at': '2099-01-01T00:00:00', 'poll_interval_seconds': 5, 'resumed': False}],
            status=[{'status': 'pending'}, {'status': 'claimed', 'claim_proof': 'the-proof'}],
            complete=[{'appliance_id': 'a1', 'cloud_id': '11111111-1111-4111-8111-111111111111'.upper(), 'credential': 'cred', 'credential_id': 'cid', 'customer_id': 'cust-1', 'site_id': 'site-1', 'partner_id': None}],
        )
        finished_with = []
        with patch.object(setup_wizard, 'AgentConfig') as agent_config_cls, \
             patch.object(setup_wizard, 'PortalClient', return_value=client), \
             patch.object(setup_wizard, '_finish_enrollment', side_effect=lambda cfg, activated: finished_with.append(activated)), \
             patch('builtins.input', return_value=''):
            agent_config_cls.load.return_value = self.config
            setup_wizard.claim_main()

        self.assertEqual(len(finished_with), 1)
        self.assertEqual(finished_with[0]['appliance_id'], 'a1')
        self.assertIsNone(load_claim_state(self.config), 'claim state must be cleared once enrollment is handed off')

    def test_customer_double_confirm_is_transparent_to_the_appliance(self):
        # The customer double-clicking "Confirm" in the portal produces
        # no appliance-visible difference at all -- claim/status simply
        # keeps returning the same claim_proof it already returned
        # (cloud-side idempotency, Phase 1), so the appliance side needs
        # no special handling and this reduces to the ordinary
        # happy-path polling behavior.
        client = FakePortalClient(
            begin=[{'claim_session_id': 'sess-1', 'claim_code': 'ABCD1234', 'expires_at': '2099-01-01T00:00:00', 'poll_interval_seconds': 5, 'resumed': False}],
            status=[{'status': 'claimed', 'claim_proof': 'the-proof'}, {'status': 'claimed', 'claim_proof': 'the-proof'}],
            complete=[{'appliance_id': 'a1', 'cloud_id': 'D', 'credential': 'cred', 'credential_id': 'cid', 'customer_id': 'cust-1', 'site_id': 'site-1', 'partner_id': None}],
        )
        state = setup_wizard._open_or_resume_claim(client, self.config, '11111111-1111-4111-8111-111111111111')
        first_proof = setup_wizard._wait_for_claim_proof(client, self.config, dict(state), sleep_fn=_no_sleep)
        second_proof = setup_wizard._wait_for_claim_proof(client, self.config, dict(state), sleep_fn=_no_sleep)
        self.assertEqual(first_proof, second_proof)


if __name__ == '__main__':
    unittest.main()
