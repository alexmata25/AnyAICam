"""ops/ryzen_wireguard_enroll_only.py: the one-off, enrollment-ONLY
invocation for a Ryzen release (f07e1d0) that already has
wireguard.enroll_wireguard() but predates setup_wizard.py's own
--wireguard-enroll CLI flag (c332aa9) -- and, even if it were present,
that flag's own wireguard_enroll_main() queues wireguard_interface_up
the moment enrollment changes anything, which this specific rollout
step must not do.

Hard requirement under test, the entire reason this script exists
separately from wireguard_enroll_main(): calls enroll_wireguard()
exactly like that function does, but NEVER queues
wireguard_interface_up or any other privileged action -- this script
imports no privileged-action-queuing function at all.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent.config import AgentConfig, save_credential
from anyaicam_agent.portal import PortalError

import ryzen_wireguard_enroll_only as script


def _config(tmp_path):
    config = AgentConfig(
        config_dir=str(tmp_path / 'etc'), state_dir=str(tmp_path / 'state'),
        log_dir=str(tmp_path / 'log'), vms_recordings_path=str(tmp_path / 'vms'),
    )
    for directory in (Path(config.config_dir), Path(config.state_dir), Path(config.vms_recordings_path)):
        directory.mkdir(parents=True, exist_ok=True)
    return config


_CREDENTIAL = {'appliance_id': 'appl-1', 'credential_id': 'cred-1', 'credential': 'secret-cred'}
_AGENT_JSON = {'cloud_id': 'AIC-TEST0001', 'portal_url': 'https://portal.example.test', 'mode': 'production'}
_WG_RESPONSE = {
    'tunnel_address': '10.70.0.2', 'gateway_public_key': 'g' * 43 + '=',
    'gateway_endpoint': 'gw.example.test:51820', 'status': 'enrolled',
    'public_key': 'p' * 43 + '=', 'private_key': 'k' * 43 + '=',
}


class RyzenWireguardEnrollOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _activate(self):
        save_credential(self.config, dict(_CREDENTIAL))
        (Path(self.config.config_dir) / 'agent.json').write_text(json.dumps(_AGENT_JSON, indent=2), encoding='utf-8')

    def _run(self, argv=('--yes',), enroll_side_effect=None, enroll_return=None):
        with patch.object(script, 'AgentConfig') as config_cls, \
             patch.object(script, 'PortalClient') as portal_client_cls, \
             patch.object(script, 'enroll_wireguard') as enroll_mock, \
             patch.object(script.sys, 'argv', ['ryzen_wireguard_enroll_only.py', *argv]):
            config_cls.load.return_value = self.config
            if enroll_side_effect is not None:
                enroll_mock.side_effect = enroll_side_effect
            else:
                enroll_mock.return_value = enroll_return or dict(_WG_RESPONSE)
            exit_code = script.main()
        return exit_code, enroll_mock, portal_client_cls

    # -- the one thing this script exists to guarantee ---------------------

    def test_never_imports_or_calls_any_privileged_action_queue(self):
        """The entire reason this script exists separately from
        wireguard_enroll_main(): no interface bring-up may be queued by
        this action. Asserted at the strongest level available -- the
        module itself has no such name to call."""
        self.assertFalse(hasattr(script, '_queue_privileged_action'))
        self.assertNotIn('_queue_privileged_action', dir(script))

    # -- confirmation gate ---------------------------------------------------

    def test_without_yes_flag_refuses_and_never_calls_enroll(self):
        self._activate()
        exit_code, enroll_mock, _portal = self._run(argv=())
        self.assertEqual(exit_code, 1)
        enroll_mock.assert_not_called()

    # -- not yet activated ---------------------------------------------------

    def test_not_activated_refuses_without_calling_enroll(self):
        # no credential.json written
        exit_code, enroll_mock, _portal = self._run()
        self.assertEqual(exit_code, 1)
        enroll_mock.assert_not_called()

    # -- happy path: enrollment-only, no side effects beyond that ----------

    def test_activated_appliance_calls_enroll_wireguard_with_existing_credential(self):
        self._activate()
        exit_code, enroll_mock, portal_client_cls = self._run()
        self.assertEqual(exit_code, 0)
        enroll_mock.assert_called_once()
        self.assertEqual(enroll_mock.call_args.kwargs.get('replace_existing'), False)
        # Built from the EXISTING credential -- never a new/rotated one.
        portal_client_cls.assert_called_once_with(self.config.portal_url, 'appl-1', 'secret-cred')

    def test_preserves_existing_identity_files_byte_for_byte(self):
        self._activate()
        before_credential = self.config.credential_file.read_bytes()
        before_agent = (Path(self.config.config_dir) / 'agent.json').read_bytes()
        self._run()
        self.assertEqual(self.config.credential_file.read_bytes(), before_credential)
        self.assertEqual((Path(self.config.config_dir) / 'agent.json').read_bytes(), before_agent)

    def test_running_twice_calls_enroll_wireguard_both_times(self):
        """enroll_wireguard() itself is idempotent -- this script must
        call it every time and let that existing idempotency do the
        work, never skip the call."""
        self._activate()
        _exit1, enroll_mock1, _ = self._run()
        _exit2, enroll_mock2, _ = self._run()
        enroll_mock1.assert_called_once()
        enroll_mock2.assert_called_once()

    # -- failure is non-fatal-to-the-appliance and safely retryable --------

    def test_portal_error_reports_failure_and_returns_nonzero(self):
        self._activate()
        exit_code, enroll_mock, _portal = self._run(enroll_side_effect=PortalError('gateway unreachable'))
        self.assertEqual(exit_code, 1)
        enroll_mock.assert_called_once()

    # -- never a secret in stdout --------------------------------------------

    def test_success_output_never_contains_the_private_key(self, ):
        self._activate()
        with patch('builtins.print') as print_mock:
            self._run()
        printed = '\n'.join(str(call.args[0]) if call.args else '' for call in print_mock.call_args_list)
        self.assertNotIn(_WG_RESPONSE['private_key'], printed)
        self.assertNotIn(_CREDENTIAL['credential'], printed)
        # The non-secret fields ARE expected to be surfaced.
        self.assertIn(_WG_RESPONSE['tunnel_address'], printed)


if __name__ == '__main__':
    unittest.main()
