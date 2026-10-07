"""Native Windows agent (anyaicam_agent/windows.py, 2026-10-07): the agent runs
as the AnyAiCamAgent service next to AnyAiCamVMS. Remote actions are a fixed
allowlist (restart VMS, restart agent) run through the installer's WinSW
wrappers; reboot and software updates are refused; the Linux updater,
privileged-marker and LAN-address paths are never used. Nothing here starts a
process: every external call is faked."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from anyaicam_agent import commands, metrics, service, setup_wizard, windows
from anyaicam_agent.config import AgentConfig


class FakeRun:
    def __init__(self, returncode=0, stdout='', error=None):
        self.calls = []
        self.returncode, self.stdout, self.error = returncode, stdout, error

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, '')


class WindowsCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(windows, 'IS_WINDOWS', True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'AnyAiCam'
        (self.root / 'service').mkdir(parents=True)
        for wrapper in ('AnyAiCamVMS.exe', 'AnyAiCamAgent.exe'):
            (self.root / 'service' / wrapper).write_bytes(b'')
        data = Path(self.tmp.name) / 'ProgramData'
        self.config = AgentConfig(config_dir=str(data / 'config'), state_dir=str(data / 'agent'),
                                  log_dir=str(data / 'logs'), vms_recordings_path=str(data / 'recordings'))
        for folder in (self.config.config_dir, self.config.state_dir, self.config.vms_recordings_path):
            Path(folder).mkdir(parents=True, exist_ok=True)


class PrivilegedActionTests(WindowsCase):
    def test_restarts_run_the_installed_winsw_wrappers(self):
        run = FakeRun()
        self.assertEqual(windows.run_privileged_action('restart_vms', root=self.root, run=run),
                         ('completed', {'requested': True, 'action': 'restart_vms'}, ''))
        self.assertEqual(windows.run_privileged_action('restart_agent', root=self.root, run=run)[0], 'completed')
        self.assertEqual(run.calls, [[str(self.root / 'service' / 'AnyAiCamVMS.exe'), 'restart'],
                                     [str(self.root / 'service' / 'AnyAiCamAgent.exe'), 'restart!']])

    def test_reboot_updates_and_anything_else_are_refused_without_running_anything(self):
        run = FakeRun()
        for action in ('reboot', 'install_update', 'wireguard_interface_up', 'apply_release', 'cmd.exe /c del'):
            status, result, error = windows.run_privileged_action(action, root=self.root, run=run)
            self.assertEqual((status, result), ('failed', {}), action)
            self.assertTrue(error, action)
        self.assertEqual(run.calls, [])
        self.assertIn('not supported on Windows', windows.run_privileged_action('reboot', root=self.root, run=run)[2])

    def test_failures_are_reported_not_raised(self):
        self.assertIn('exited with 1', windows.run_privileged_action('restart_vms', root=self.root, run=FakeRun(returncode=1))[2])
        self.assertIn('Could not run', windows.run_privileged_action('restart_vms', root=self.root, run=FakeRun(error=OSError('denied')))[2])
        (self.root / 'service' / 'AnyAiCamVMS.exe').unlink()
        self.assertEqual(windows.run_privileged_action('restart_vms', root=self.root, run=FakeRun())[0:2], ('failed', {}))

    def test_install_root_comes_from_the_bundled_interpreter_not_the_environment(self):
        python = self.root / 'runtime' / 'python' / 'python.exe'
        with patch.object(sys, 'executable', str(python)), patch.dict('os.environ', {'ANYAICAM_INSTALL_ROOT': 'C:\\evil'}):
            self.assertEqual(windows.install_root(), self.root.resolve())


class CommandTests(WindowsCase):
    def execute(self, command, payload, **kwargs):
        with patch.object(windows, 'run_privileged_action', return_value=('completed', {'requested': True}, '')) as action:
            result = commands.execute(command, payload, self.config, **kwargs)
        return result, action

    def test_restart_vms_runs_the_windows_action_and_writes_no_marker(self):
        result, action = self.execute('restart_vms', {'confirmed': True})
        self.assertEqual(result[0], 'completed')
        action.assert_called_once_with('restart_vms')
        self.assertFalse(self.config.pending_actions_dir.exists())

    def test_confirmation_is_still_required(self):
        result, action = self.execute('restart_vms', {})
        self.assertEqual(result, ('failed', {}, 'Confirmation is required for this action.'))
        action.assert_not_called()

    def test_reboot_goes_to_the_windows_refusal(self):
        result = commands.execute('reboot_appliance', {'confirmed': True}, self.config)
        self.assertEqual(result[0:2], ('failed', {}))
        self.assertIn('not supported on Windows', result[2])
        self.assertFalse(self.config.pending_actions_dir.exists())

    def test_install_update_is_refused_before_the_updater(self):
        state_machine, owner_update = MagicMock(), MagicMock()
        result = commands.execute('install_update', {'update_id': 'u1'}, self.config, state_machine=state_machine, owner_update=owner_update)
        self.assertEqual(result, ('failed', {}, windows.UPDATES_UNSUPPORTED))
        state_machine.assert_not_called(); owner_update.stage.assert_not_called()

    def test_restart_service_restarts_through_winsw_not_a_clean_exit(self):
        stop = threading.Event()
        result, action = self.execute('restart_service', {}, stop_event=stop)
        action.assert_called_once_with('restart_agent')
        self.assertFalse(stop.is_set())  # a clean exit would leave the service stopped


class ServiceTests(WindowsCase):
    def agent(self):
        agent = service.ApplianceAgent.__new__(service.ApplianceAgent)
        agent.config, agent.log = self.config, MagicMock()
        agent.state_machine, agent.result_relay = MagicMock(), MagicMock()
        agent.update_resume_failed, agent._next_source_check_at = False, 0.0
        return agent

    def test_the_linux_updater_paths_never_run(self):
        agent = self.agent()
        agent.resolve_update_state(); agent.check_for_source_update(); agent.relay_update_results()
        self.assertEqual(agent.state_machine.mock_calls, [])
        self.assertEqual(agent.result_relay.mock_calls, [])

    def test_lan_addresses_are_not_published(self):
        with patch.object(service, 'publish_lan_addresses') as publish:
            self.agent().publish_lan_addresses()
        publish.assert_not_called()


_ACTIVATION = {
    'appliance_id': 'appl-1', 'cloud_id': 'AIC-TEST0001', 'credential': 'secret-cred',
    'credential_id': 'cred-1', 'customer_id': 'cust-1', 'site_id': 'site-1', 'partner_id': None,
}


class FinishEnrollmentTests(WindowsCase):
    def test_links_the_vms_and_restarts_it_but_not_the_running_agent(self):
        self.config.portal_url = 'https://app.anyaicam.com'
        (Path(self.config.config_dir) / 'vms.env').write_text('ANYAICAM_APP_SECRETS=keep\nANYAICAM_CLOUD_URL=https://old\n', encoding='utf-8')
        with patch.object(setup_wizard, '_queue_privileged_action', return_value=('completed', {}, '')) as queue, \
             patch.object(setup_wizard, 'PortalClient') as client:
            client.return_value.request.return_value = {}
            setup_wizard._finish_enrollment(self.config, dict(_ACTIVATION), ask_discovery=False)
        self.assertEqual([c.args[1] for c in queue.call_args_list], ['restart_vms'])
        self.assertEqual((Path(self.config.config_dir) / 'vms.env').read_text(encoding='utf-8').splitlines(),
                         ['ANYAICAM_APP_SECRETS=keep', 'ANYAICAM_CLOUD_URL=https://app.anyaicam.com'])
        identity = json.loads((Path(self.config.vms_recordings_path) / 'appliance_identity.json').read_text(encoding='utf-8'))
        self.assertEqual(identity['cloud_id'], 'AIC-TEST0001')
        self.assertEqual(json.loads(self.config.credential_file.read_text(encoding='utf-8'))['credential'], 'secret-cred')


class PlatformReadingTests(WindowsCase):
    def test_arp_table_matches_the_linux_format(self):
        output = ('\nInterface: 192.168.1.20 --- 0xb\n  Internet Address      Physical Address      Type\n'
                  '  192.168.1.1           AA-BB-CC-DD-EE-01     dynamic\n'
                  '  192.168.1.64          aa-bb-cc-dd-ee-40     dynamic\n'
                  '  224.0.0.22            01-00-5e-00-00-16     static\n  garbage line\n')
        self.assertEqual(windows.parse_arp(output), {'192.168.1.1': 'aa:bb:cc:dd:ee:01', '192.168.1.64': 'aa:bb:cc:dd:ee:40',
                                                     '224.0.0.22': '01:00:5e:00:00:16'})
        self.assertEqual(windows.arp_table(run=FakeRun(error=OSError('no arp'))), {})

    def test_only_private_networks_are_scanned_at_most_a_slash_24(self):
        networks = windows.private_networks(['192.168.1.20', '10.1.2.3', '127.0.0.1', '169.254.3.4', '8.8.8.8', '192.168.1.99', 'bad'])
        self.assertEqual([str(n) for n in networks], ['192.168.1.0/24', '10.1.2.0/24'])

    def test_service_state(self):
        running = 'SERVICE_NAME: AnyAiCamAgent\n        STATE              : 4  RUNNING\n'
        self.assertEqual(windows.service_state('AnyAiCamAgent', run=FakeRun(stdout=running)), 'running')
        self.assertEqual(windows.service_state('AnyAiCamAgent', run=FakeRun(stdout='[SC] OpenService FAILED 1060')), 'not installed')

    def test_heartbeat_uses_windows_metrics(self):
        with patch.object(windows, 'uptime_seconds', return_value=4321), patch.object(windows, 'cpu_percent', return_value=12.5), \
             patch.object(windows, 'memory_percent', return_value=40.0), patch.object(metrics, 'local_ip', return_value='192.168.1.20'), \
             patch.object(metrics, 'disk_summary', return_value={}), patch.object(metrics, 'local_storage_state', return_value={}):
            heartbeat = metrics.collect(self.config, [])
        self.assertEqual((heartbeat['uptime_seconds'], heartbeat['cpu'], heartbeat['memory']), (4321, 12.5, 40.0))


if __name__ == '__main__':
    unittest.main()
