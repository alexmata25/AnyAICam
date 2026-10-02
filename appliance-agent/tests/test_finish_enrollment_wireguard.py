"""_finish_enrollment()'s WireGuard enrollment hook (docs/wireguard-
remote-connectivity-plan.md Sec 5): gated behind ANYAICAM_WIREGUARD_
ENABLED (unset everywhere today, matching this codebase's own staged-
rollout precedent), and -- the one property most worth a dedicated
regression test -- a WireGuard enrollment failure must NEVER abort or
roll back the overall (already-succeeded) appliance activation, since
WireGuard is fully additive per the plan doc's own fail-safe
requirement (Sec 18). Same isolation style as
test_finish_enrollment_restart_privilege.py: _finish_enrollment() runs
for real (pure filesystem), only the privileged-action queue, the
portal HTTP verification call, and (here) PortalClient.wireguard_enroll
are mocked.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent.config import AgentConfig
from anyaicam_agent import setup_wizard


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

_WG_RESPONSE = {'tunnel_address': '10.70.0.2', 'gateway_public_key': 'g' * 43 + '=', 'gateway_endpoint': 'gw.example.test:51820', 'status': 'enrolled'}


class FinishEnrollmentWireguardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *, wireguard_enabled, enroll_side_effect=None, queue_result=('completed', {'requested': True, 'marker_id': 'm1'}, '')):
        env = {'ANYAICAM_WIREGUARD_ENABLED': 'true' if wireguard_enabled else 'false'}
        with patch.object(setup_wizard, '_queue_privileged_action', return_value=queue_result) as queue_mock, \
             patch.object(setup_wizard, 'PortalClient') as portal_client_cls, \
             patch.object(setup_wizard, 'enroll_wireguard') as enroll_mock, \
             patch.dict('os.environ', env, clear=False), \
             patch('builtins.input', return_value='n'):
            portal_client_cls.return_value.request.return_value = {}
            if enroll_side_effect is not None:
                enroll_mock.side_effect = enroll_side_effect
            else:
                enroll_mock.return_value = dict(_WG_RESPONSE, public_key='p' * 43 + '=', private_key='k' * 43 + '=')
            setup_wizard._finish_enrollment(self.config, dict(_ACTIVATION))
        return queue_mock, enroll_mock

    def test_disabled_by_default_never_calls_enroll_wireguard(self):
        _queue_mock, enroll_mock = self._run(wireguard_enabled=False)
        enroll_mock.assert_not_called()

    def test_disabled_by_default_never_queues_interface_up(self):
        queue_mock, _enroll_mock = self._run(wireguard_enabled=False)
        wireguard_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(wireguard_calls, [])

    def test_enabled_calls_enroll_wireguard_with_replace_existing_false_on_first_enrollment(self):
        _queue_mock, enroll_mock = self._run(wireguard_enabled=True)
        enroll_mock.assert_called_once()
        self.assertEqual(enroll_mock.call_args.kwargs.get('replace_existing'), False)

    def test_enabled_queues_interface_up_after_a_successful_enrollment(self):
        queue_mock, _enroll_mock = self._run(wireguard_enabled=True)
        wireguard_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(len(wireguard_calls), 1)
        self.assertEqual(wireguard_calls[0].args[2], {'confirmed': True})

    def test_a_failed_wireguard_enrollment_does_not_abort_activation(self):
        """The core fail-safe property: activation's own identity files
        (already committed by first_enroll() before this hook even
        runs) must remain in place regardless of what WireGuard does."""
        self._run(wireguard_enabled=True, enroll_side_effect=RuntimeError('portal unreachable'))
        agent_json = json.loads((Path(self.config.config_dir) / 'agent.json').read_text(encoding='utf-8'))
        self.assertEqual(agent_json['cloud_id'], _ACTIVATION['cloud_id'])
        credential_json = json.loads(self.config.credential_file.read_text(encoding='utf-8'))
        self.assertEqual(credential_json['credential'], _ACTIVATION['credential'])

    def test_a_failed_wireguard_enrollment_prints_a_warning_not_silence(self):
        with patch('builtins.print') as mock_print:
            self._run(wireguard_enabled=True, enroll_side_effect=RuntimeError('portal unreachable'))
        printed = ' '.join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn('WARNING', printed)
        self.assertIn('WireGuard', printed)

    def test_a_failed_wireguard_enrollment_never_queues_interface_up(self):
        queue_mock, _enroll_mock = self._run(wireguard_enabled=True, enroll_side_effect=RuntimeError('portal unreachable'))
        wireguard_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(wireguard_calls, [])

    def test_a_failed_interface_up_queue_is_a_warning_not_fatal(self):
        with patch('builtins.print') as mock_print:
            self._run(wireguard_enabled=True, queue_result=('failed', {}, 'pending_actions_dir is not writable'))
        printed = ' '.join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn('WARNING', printed)
        agent_json = json.loads((Path(self.config.config_dir) / 'agent.json').read_text(encoding='utf-8'))
        self.assertEqual(agent_json['cloud_id'], _ACTIVATION['cloud_id'])  # activation still intact

    def test_restart_vms_is_still_queued_even_when_wireguard_is_enabled(self):
        """WireGuard enrollment must never crowd out or replace the
        existing post-activation VMS-restart step."""
        queue_mock, _enroll_mock = self._run(wireguard_enabled=True)
        vms_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'restart_vms']
        self.assertEqual(len(vms_calls), 1)


if __name__ == '__main__':
    unittest.main()
