"""Regression coverage for a confirmed-live lifecycle defect: service.py's
run() raised RuntimeError immediately whenever no credential existed yet,
treating a freshly-installed, not-yet-claimed appliance as a fatal error.
Combined with the systemd unit's own `Restart=always`/`RestartSec=10`,
this turned "installed but not yet claimed" -- the FIRST real state of
every fresh appliance, and exactly the state installer/validate.sh runs
in -- into a permanent crash loop. Confirmed live on Ryzen (2026-09-11):
journalctl showed 600+ restarts on a correctly installed, deliberately-
still-unclaimed appliance, every single one logging the identical
"Appliance is not activated" traceback.

The fix (_await_activation(), called from run() before the main cycle
loop) waits for anyaicam-setup to write a real credential instead of
raising, re-checking credential.json directly on a short fixed interval
so a single long-running process transitions cleanly from waiting to
active the moment activation completes -- never crashing, never
depending on install/validate/claim happening in a different order than
they actually do.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from anyaicam_agent.config import AgentConfig, save_credential
from anyaicam_agent.service import ApplianceAgent


def _agent(folder):
    config = AgentConfig(cloud_id='', portal_url='https://portal.example', state_dir=folder, config_dir=folder, log_dir=folder)
    return ApplianceAgent(config)


class AwaitActivationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)

    def test_no_credential_and_stop_requested_returns_without_raising(self):
        """The exact Ryzen shape: a fresh, unclaimed appliance whose
        process is asked to stop (e.g. systemctl stop, or a real SIGTERM)
        while still waiting. Must return cleanly -- never raise -- and
        must not have mutated self.client's credential/appliance_id."""
        self.agent.stop_event.set()
        self.agent._await_activation()  # must not raise
        self.assertIsNone(self.agent.client.credential)
        self.assertIsNone(self.agent.client.appliance_id)

    def test_run_does_not_raise_when_unclaimed_and_stop_requested(self):
        """run() itself (not just the extracted helper) must not raise
        RuntimeError for an unclaimed appliance -- this is the exact
        call path systemd invokes via ExecStart."""
        self.agent.stop_event.set()
        self.agent.cycle = MagicMock()
        self.agent.run()  # must not raise
        self.agent.cycle.assert_not_called()

    def test_already_credentialed_agent_returns_immediately_without_waiting(self):
        """An already-activated appliance (the normal case after the
        first claim) must never enter the wait loop at all -- confirmed
        by never calling stop_event.wait."""
        save_credential(self.agent.config, {'appliance_id': 'appl-1', 'credential_id': 'cred-1', 'credential': 'secret-token'})
        agent = _agent(self._tmpdir.name)  # fresh instance -- __init__ loads the credential just written
        agent.stop_event.wait = MagicMock()
        agent._await_activation()
        agent.stop_event.wait.assert_not_called()
        self.assertEqual(agent.client.credential, 'secret-token')

    def test_credential_written_while_waiting_is_detected_without_restarting_the_process(self):
        """The core fix: a single long-running process transitions from
        waiting to active the moment anyaicam-setup writes credential.json
        -- it does not need to be killed/restarted to notice. Simulates
        time passing (stop_event.wait) without a real sleep by writing
        the credential as that mock's side effect on its first call."""
        calls = []

        def fake_wait(_seconds):
            calls.append(1)
            if len(calls) == 1:
                save_credential(self.agent.config, {'appliance_id': 'appl-2', 'credential_id': 'cred-2', 'credential': 'newly-activated-token'})
            else:
                self.agent.stop_event.set()  # safety net so a real bug here can't hang the test suite

        self.agent.stop_event.wait = MagicMock(side_effect=fake_wait)
        self.agent._await_activation()

        self.assertEqual(self.agent.client.credential, 'newly-activated-token')
        self.assertEqual(self.agent.client.appliance_id, 'appl-2')
        self.assertEqual(len(calls), 1, "must stop polling the instant a real credential is found, not loop again first")

    def test_run_proceeds_into_the_normal_cycle_loop_once_activation_is_detected(self):
        """End-to-end: run() itself, not just the helper, actually
        resumes normal operation (calls cycle()) once activation is
        detected mid-wait -- not just that the wait loop exits."""
        def fake_wait(_seconds):
            if self.agent.client.credential is None:
                save_credential(self.agent.config, {'appliance_id': 'appl-3', 'credential_id': 'cred-3', 'credential': 'token-3'})
            else:
                self.agent.stop_event.set()  # let run()'s own main loop exit after exactly one cycle()

        self.agent.stop_event.wait = MagicMock(side_effect=fake_wait)
        self.agent.cycle = MagicMock()
        self.agent.resolve_update_state = MagicMock()

        self.agent.run()  # must not raise

        self.agent.cycle.assert_called_once()
        self.assertEqual(self.agent.client.credential, 'token-3')


if __name__ == '__main__':
    unittest.main()
