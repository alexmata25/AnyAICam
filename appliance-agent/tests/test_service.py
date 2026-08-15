"""RDM-2 (device-side integration, Group 2A): focused tests for
anyaicam_agent.service.ApplianceAgent's new startup update-resolution
wiring -- construction, resolve_update_state()'s fail-safe sequencing,
and its call-order placement inside run().

No network, no AWS -- PortalClient is never actually asked to make a
request in this file (resolve_update_state() and its collaborators are
purely local/filesystem). Where run()'s own credential-check needs to
pass, a real local credential file is written via save_credential(), not
a network call.

Enforcement of the update_resume_failed interlock against actual
install_update dispatch is Group 2C's own test scope, not this file's --
Group 2A only establishes that the flag is set correctly and that
resolve_update_state() itself never crashes agent startup.
"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.config import AgentConfig, save_credential
from anyaicam_agent.service import ApplianceAgent
from anyaicam_agent.updater import installer
from anyaicam_agent.updater.models import PendingValidation
import json


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = AgentConfig(state_dir=self._tmp.name, config_dir=self._tmp.name, log_dir=self._tmp.name)

    def _make_tar(self, path: Path, version: str) -> Path:
        with tarfile.open(path, mode="w") as tar:
            data = version.encode()
            info = tarfile.TarInfo(name="VERSION")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return path


class ConstructionTests(ServiceTestCase):
    def test_state_machine_is_constructed(self):
        agent = ApplianceAgent(self.config)
        self.assertIsNotNone(agent.state_machine)

    def test_update_resume_failed_starts_false(self):
        agent = ApplianceAgent(self.config)
        self.assertFalse(agent.update_resume_failed)

    def test_state_machine_restart_signal_is_wired_to_this_agents_stop_event(self):
        # RDM-2 Group 2B: proves the real wiring end-to-end -- calling
        # the state machine's restart_signal must set THIS agent's own
        # stop_event, the same one commands.py's existing
        # restart_service handler sets. No longer Group 2A's
        # do-nothing placeholder.
        agent = ApplianceAgent(self.config)
        self.assertFalse(agent.stop_event.is_set())
        agent.state_machine.restart_signal()
        self.assertTrue(agent.stop_event.is_set())

    def test_each_agent_instance_gets_its_own_independent_restart_signal(self):
        agent_a = ApplianceAgent(self.config)
        agent_b = ApplianceAgent(self.config)
        agent_a.state_machine.restart_signal()
        self.assertTrue(agent_a.stop_event.is_set())
        self.assertFalse(agent_b.stop_event.is_set())


class ResolveUpdateStateTests(ServiceTestCase):
    def test_nothing_pending_leaves_flag_false_and_raises_nothing(self):
        agent = ApplianceAgent(self.config)
        agent.resolve_update_state()  # must not raise
        self.assertFalse(agent.update_resume_failed)

    def test_resume_if_pending_exception_sets_the_flag_and_does_not_propagate(self):
        agent = ApplianceAgent(self.config)
        agent.state_machine.resume_if_pending = MagicMock(side_effect=RuntimeError("corrupt history db"))
        agent.resolve_update_state()  # must not raise
        self.assertTrue(agent.update_resume_failed)

    def test_sweep_orphaned_state_exception_does_not_set_the_flag_and_does_not_propagate(self):
        agent = ApplianceAgent(self.config)
        agent.state_machine.sweep_orphaned_state = MagicMock(side_effect=OSError("disk error"))
        agent.resolve_update_state()  # must not raise
        self.assertFalse(agent.update_resume_failed)

    def test_sweep_orphaned_state_still_runs_even_when_resume_raised(self):
        agent = ApplianceAgent(self.config)
        agent.state_machine.resume_if_pending = MagicMock(side_effect=RuntimeError("boom"))
        sweep = MagicMock(return_value={"removed_staging_entries": [], "pre_activation_in_progress_update_ids": []})
        agent.state_machine.sweep_orphaned_state = sweep
        agent.resolve_update_state()
        sweep.assert_called_once()

    def test_a_real_concluding_resume_result_is_handled_without_raising(self):
        # First-ever activation, marker present, health_check defaults to
        # None (Group 2A's own placeholder) -- resume_if_pending() runs
        # the REAL Group 6 logic end-to-end: unhealthy (no health_check)
        # -> _begin_rollback() -> no prior version to roll back to ->
        # ROLLBACK_FAILED. Proves the real wiring, not just a mock.
        tar_path = self._make_tar(Path(self._tmp.name) / "1.0.0.tar", "1.0.0")
        installer.install_candidate(tar_path, "1.0.0", self.config.update_versions_dir, self.config.update_staging_dir)
        installer.activate("1.0.0", self.config.update_versions_dir, self.config.current_version_pointer_file)

        agent = ApplianceAgent(self.config)
        agent.state_machine.history.begin_attempt("upd-1", "", "1.0.0", now=1000.0)
        marker = PendingValidation(update_id="upd-1", from_version="", to_version="1.0.0",
                                    attempt="install", deadline="2099-01-01T00:00:00+00:00")
        self.config.pending_validation_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.pending_validation_file.write_text(json.dumps(marker.as_dict()), encoding="utf-8")

        agent.resolve_update_state()  # must not raise

        self.assertFalse(agent.update_resume_failed)
        self.assertFalse(self.config.pending_validation_file.exists())
        self.assertTrue(agent.state_machine.history.is_terminal("upd-1"))
        row = agent.state_machine.history.get("upd-1")
        self.assertEqual(row["state"], "rollback_failed")


class RunCallOrderTests(ServiceTestCase):
    def test_resolve_update_state_runs_before_the_main_loop(self):
        save_credential(self.config, {"appliance_id": "app-1", "credential": "secret"})
        agent = ApplianceAgent(self.config)
        agent.stop_event.set()  # loop body must never execute
        agent.resolve_update_state = MagicMock()
        agent.cycle = MagicMock()

        agent.run()

        agent.resolve_update_state.assert_called_once()
        agent.cycle.assert_not_called()

    def test_run_still_raises_when_appliance_is_not_activated(self):
        agent = ApplianceAgent(self.config)  # no credential saved
        with self.assertRaises(RuntimeError):
            agent.run()


if __name__ == "__main__":
    unittest.main()
