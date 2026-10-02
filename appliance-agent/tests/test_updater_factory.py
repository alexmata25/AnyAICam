"""RDM-2 (device-side integration, Group 2A): focused tests for
anyaicam_agent.updater.factory -- build_update_state_machine() and its
placeholder collaborators.

No network, no AWS, no real device files -- all paths point inside a
per-test temporary directory.
"""

import sys
import tempfile
import unittest
from pathlib import Path

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.updater.factory import _UnconfiguredSource, build_update_state_machine
from anyaicam_agent.updater.history import UpdateHistory
from anyaicam_agent.updater.source import PackageDownloadError, SourceUnavailable, UpdateSourceProvider
from anyaicam_agent.updater.state_machine import UpdateStateMachine
from anyaicam_agent.updater.verify import PackageVerifier


class FactoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = AgentConfig(state_dir=self._tmp.name, config_dir=self._tmp.name, log_dir=self._tmp.name)


class BuildUpdateStateMachineTests(FactoryTestCase):
    def test_returns_an_update_state_machine(self):
        machine = build_update_state_machine(self.config)
        self.assertIsInstance(machine, UpdateStateMachine)

    def test_paths_are_sourced_from_config(self):
        machine = build_update_state_machine(self.config)
        self.assertEqual(machine.versions_dir, Path(self.config.update_versions_dir))
        self.assertEqual(machine.staging_dir, Path(self.config.update_staging_dir))
        self.assertEqual(machine.pointer_file, Path(self.config.current_version_pointer_file))
        self.assertEqual(machine.pending_validation_file, Path(self.config.pending_validation_file))

    def test_device_target_and_channel_are_sourced_from_config(self):
        machine = build_update_state_machine(self.config)
        self.assertEqual(machine.device_target, self.config.update_target)
        self.assertEqual(machine.channel, self.config.update_channel)

    def test_history_points_at_configs_update_history_file(self):
        machine = build_update_state_machine(self.config)
        self.assertIsInstance(machine.history, UpdateHistory)
        self.assertEqual(machine.history.path, self.config.update_history_file)

    def test_verifier_points_at_configs_trusted_public_key_file(self):
        machine = build_update_state_machine(self.config)
        self.assertIsInstance(machine.verifier, PackageVerifier)
        self.assertEqual(machine.verifier._trusted_public_key_file, self.config.trusted_public_key_file)

    def test_default_source_is_the_unconfigured_placeholder(self):
        machine = build_update_state_machine(self.config)
        self.assertIsInstance(machine.source, _UnconfiguredSource)
        self.assertIsInstance(machine.source, UpdateSourceProvider)

    def test_default_source_fails_closed_on_check_for_manifest(self):
        machine = build_update_state_machine(self.config)
        with self.assertRaises(SourceUnavailable):
            machine.source.check_for_manifest("1.0.0", "anyaicam-appliance", "stable")

    def test_default_source_fails_closed_on_download_package(self):
        machine = build_update_state_machine(self.config)
        with self.assertRaises(PackageDownloadError):
            machine.source.download_package({"update_id": "upd-1"}, Path(self._tmp.name) / "out.pkg")

    def test_default_restart_signal_does_not_raise(self):
        machine = build_update_state_machine(self.config)
        machine.restart_signal()  # must not raise

    def test_default_health_check_is_none(self):
        machine = build_update_state_machine(self.config)
        self.assertIsNone(machine.health_check)

    def test_caller_supplied_restart_signal_is_used_instead_of_the_placeholder(self):
        calls = []
        machine = build_update_state_machine(self.config, restart_signal=lambda: calls.append(1))
        machine.restart_signal()
        self.assertEqual(calls, [1])

    def test_caller_supplied_health_check_is_used_instead_of_none(self):
        machine = build_update_state_machine(self.config, health_check=lambda: True)
        self.assertTrue(machine.health_check())

    def test_caller_supplied_source_is_used_instead_of_the_placeholder(self):
        class _Marker(UpdateSourceProvider):
            def check_for_manifest(self, current_version, target, channel):
                return None
            def download_package(self, manifest_dict, destination_path):
                pass
        marker = _Marker()
        machine = build_update_state_machine(self.config, source=marker)
        self.assertIs(machine.source, marker)


if __name__ == "__main__":
    unittest.main()
