"""Dedicated dispatch coverage for the two WireGuard privileged actions
(wireguard_interface_up/down), added to privileged_watcher.py's DISPATCH
table for docs/wireguard-remote-connectivity-plan.md Sec 5 step 3/Sec 16.

test_rdm4_privileged_actions.py's own generic, DISPATCH-wide tests
(no-shell-metacharacters, marker-content-never-trusted, dry-run-never-
executes, etc.) already cover these two entries automatically since
they iterate watcher.DISPATCH.items() -- this file only adds the
argv-shape assertions specific to these two actions, matching the
existing per-action test style (test_real_run_executes_exactly_the_
fixed_argv_for_restart_vms is the direct precedent).

No test here brings up a real WireGuard interface or invokes a real
`wg-quick` -- subprocess.run is always mocked, matching every other
test in this file's sibling.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_WATCHER_PATH = Path(__file__).resolve().parents[1] / 'system' / 'privileged_watcher.py'
_spec = importlib.util.spec_from_file_location('privileged_watcher_wireguard_test', _WATCHER_PATH)
watcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watcher)


class WireguardDispatchTests(unittest.TestCase):
    def test_interface_up_is_a_fixed_wg_quick_argv_pointed_at_config_dir(self):
        self.assertEqual(watcher.DISPATCH['wireguard_interface_up'], ['wg-quick', 'up', '/etc/anyaicam/wireguard/wg0.conf'])

    def test_interface_down_is_a_fixed_wg_quick_argv_pointed_at_config_dir(self):
        self.assertEqual(watcher.DISPATCH['wireguard_interface_down'], ['wg-quick', 'down', '/etc/anyaicam/wireguard/wg0.conf'])

    def test_interface_up_and_down_use_the_identical_config_path(self):
        # Must always agree -- a bring-up and a tear-down that pointed at
        # different files would be a real, silent misconfiguration.
        up_path = watcher.DISPATCH['wireguard_interface_up'][-1]
        down_path = watcher.DISPATCH['wireguard_interface_down'][-1]
        self.assertEqual(up_path, down_path)

    def test_real_run_executes_exactly_the_fixed_argv_for_interface_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wireguard_interface_up.json'
            path.write_text(json.dumps({'type': 'wireguard_interface_up', 'command_id': 'abc'}))
            with patch.object(watcher.subprocess, 'run') as run:
                watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_called_once_with(['wg-quick', 'up', '/etc/anyaicam/wireguard/wg0.conf'], check=False)
        self.assertFalse(path.exists())  # consumed

    def test_real_run_executes_exactly_the_fixed_argv_for_interface_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wireguard_interface_down.json'
            path.write_text(json.dumps({'type': 'wireguard_interface_down', 'command_id': 'abc'}))
            with patch.object(watcher.subprocess, 'run') as run:
                watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_called_once_with(['wg-quick', 'down', '/etc/anyaicam/wireguard/wg0.conf'], check=False)

    def test_dry_run_never_executes_wireguard_interface_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wireguard_interface_up.json'
            path.write_text(json.dumps({'type': 'wireguard_interface_up', 'command_id': 'abc'}))
            with patch.object(watcher.subprocess, 'run') as run:
                argv = watcher.process_marker(path, dry_run=True)
            run.assert_not_called()
            self.assertEqual(argv, ['wg-quick', 'up', '/etc/anyaicam/wireguard/wg0.conf'])
            self.assertTrue(path.exists())  # dry-run never deletes the marker

    def test_marker_extra_fields_never_reach_the_wg_quick_subprocess_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wireguard_interface_up.json'
            path.write_text(json.dumps({
                'type': 'wireguard_interface_up', 'command_id': 'abc',
                'config_path': '/tmp/attacker-controlled.conf', 'argv': ['rm', '-rf', '/'],
            }))
            with patch.object(watcher.subprocess, 'run') as run:
                watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_called_once_with(['wg-quick', 'up', '/etc/anyaicam/wireguard/wg0.conf'], check=False)


if __name__ == '__main__':
    unittest.main()
