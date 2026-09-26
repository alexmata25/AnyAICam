"""Regression coverage for a confirmed-live lifecycle defect:
_finish_enrollment()'s restart_service() called `systemctl restart
anyaicam-agent.service` directly, as the unprivileged `anyaicam` system
user anyaicam-setup is documented to run as (see appliance-agent/
scripts/install.sh's own "Run: sudo -u anyaicam ...anyaicam-setup"),
which owns /etc/anyaicam and /var/lib/anyaicam but has no authority to
restart a system unit, and no interactive desktop session for polkit to
prompt through. Confirmed live on Ryzen (2026-09-12): the restart hung/
failed with a CalledProcessError, which first_enroll() correctly
treated as fatal and rolled back the freshly issued identity files --
even though the credential itself had already been durably issued
server-side and, combined with the separate claim-state lifecycle
defect (see test_setup_wizard_claim.py's own coverage), could never be
recovered afterward.

The fix: restart_service() now queues the restart through the same
root-owned privileged-watcher marker mechanism already used for
restart_vms a few lines below it (appliance-agent/system/
privileged_watcher.py's fixed DISPATCH table -- the only thing on this
device actually allowed to touch systemd/Docker), instead of inventing
a new sudoers/polkit rule. And unlike the old behavior, a failure to
queue or run it is now only a warning, never fatal: RC4's own
_await_activation() already makes anyaicam-agent.service pick up a
freshly written credential.json on its own within ~10 seconds without
needing a restart at all, and verify_authentication() (a direct HTTPS
call to the portal with the new credential) is what actually proves
activation worked -- neither depends on this restart succeeding.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent.config import AgentConfig
from anyaicam_agent import setup_wizard

# privileged_watcher.py is not part of the anyaicam_agent package (it's a
# separate, root-deployed script -- see its own docstring), so it's
# loaded directly by path, matching test_rdm4_privileged_actions.py's
# own convention.
_WATCHER_PATH = Path(__file__).resolve().parents[1] / 'system' / 'privileged_watcher.py'
_spec = importlib.util.spec_from_file_location('privileged_watcher_restart_agent_test', _WATCHER_PATH)
watcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watcher)


class RestartAgentDispatchTests(unittest.TestCase):
    def test_restart_agent_is_a_plain_fixed_systemctl_argv(self):
        self.assertEqual(watcher.DISPATCH['restart_agent'], ['systemctl', 'restart', 'anyaicam-agent.service'])


def _config(tmp_path):
    config = AgentConfig(
        config_dir=str(tmp_path / 'etc'), state_dir=str(tmp_path / 'state'),
        log_dir=str(tmp_path / 'log'), vms_recordings_path=str(tmp_path / 'vms'),
    )
    for directory in (Path(config.config_dir), Path(config.state_dir), Path(config.vms_recordings_path)):
        directory.mkdir(parents=True, exist_ok=True)
    return config


_ACTIVATION = {
    'appliance_id': 'appl-1', 'cloud_id': 'AIC-TEST0001', 'credential': 'secret-cred',
    'credential_id': 'cred-1', 'customer_id': 'cust-1', 'site_id': 'site-1', 'partner_id': None,
}


class FinishEnrollmentRestartServiceTests(unittest.TestCase):
    """_finish_enrollment() itself run for real (first_enroll() included
    -- pure filesystem, no network) with only the privileged-action queue
    and the portal HTTP verification call mocked out, matching this
    repo's existing test_reenrollment.py-style isolation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, queue_result):
        with patch.object(setup_wizard, '_queue_privileged_action', return_value=queue_result) as queue_mock, \
             patch.object(setup_wizard, 'PortalClient') as portal_client_cls, \
             patch('builtins.input', return_value='n'):
            portal_client_cls.return_value.request.return_value = {}
            setup_wizard._finish_enrollment(self.config, dict(_ACTIVATION))
        return queue_mock

    def test_restart_uses_the_privileged_marker_mechanism_not_a_direct_systemctl_call(self):
        queue_mock = self._run(('completed', {'requested': True, 'marker_id': 'm1'}, ''))
        # restart_vms is queued too (a separate, pre-existing call a few
        # lines below restart_service() in the same function) -- isolate
        # the specific call this fix is about.
        agent_restart_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'restart_agent']
        self.assertEqual(len(agent_restart_calls), 1, 'restart_service() must queue exactly one restart_agent action')
        self.assertEqual(agent_restart_calls[0].args[2], {'confirmed': True})

    def test_a_failed_agent_restart_queue_does_not_abort_enrollment(self):
        # The core fix: this must NOT raise, and the identity files
        # first_enroll() just wrote must remain in place -- its own
        # rollback-on-failure must never trigger just because queuing
        # the restart failed.
        self._run(('failed', {}, 'pending_actions_dir is not writable'))
        agent_json = json.loads((Path(self.config.config_dir) / 'agent.json').read_text(encoding='utf-8'))
        self.assertEqual(agent_json['cloud_id'], _ACTIVATION['cloud_id'])
        credential_json = json.loads(self.config.credential_file.read_text(encoding='utf-8'))
        self.assertEqual(credential_json['credential'], _ACTIVATION['credential'])

    def test_a_failed_agent_restart_queue_prints_a_warning_not_silence(self):
        with patch('builtins.print') as mock_print:
            self._run(('failed', {}, 'pending_actions_dir is not writable'))
        printed = ' '.join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn('WARNING', printed)
        self.assertIn('10 seconds', printed)


if __name__ == '__main__':
    unittest.main()
