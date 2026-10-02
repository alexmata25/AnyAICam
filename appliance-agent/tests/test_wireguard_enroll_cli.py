"""anyaicam-setup --wireguard-enroll (setup_wizard.wireguard_enroll_main()):
the standalone trigger for an ALREADY-activated appliance to enroll into
WireGuard, added because _finish_enrollment()'s own WireGuard hook only
ever runs from interactive_main()/claim_main() -- i.e. only at first
activation or re-claim, never from service.py's long-running daemon. An
appliance activated before this feature existed has no other path that
ever reaches enroll_wireguard().

Hard requirements under test (all explicit, see setup_wizard.py's own
wireguard_enroll_main() docstring/comments): reuses the existing
appliance identity unchanged; calls the existing enroll_wireguard()
logic directly rather than reimplementing it; never touches
first_enroll()/coordinated_reenroll() (no camera/credential/customer
rotation); safe and idempotent to re-run.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent.config import AgentConfig, load_wireguard_identity, save_credential, save_wireguard_identity
from anyaicam_agent.portal import PortalError
from anyaicam_agent import setup_wizard


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
}


class WireguardEnrollCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _activate(self):
        """An already-activated appliance, as it would really exist on a
        real Ryzen predating this feature: credential.json + agent.json
        present, no wireguard_identity.json yet."""
        save_credential(self.config, dict(_CREDENTIAL))
        agent_path = Path(self.config.config_dir) / 'agent.json'
        agent_path.write_text(json.dumps(_AGENT_JSON, indent=2), encoding='utf-8')

    def _agent_json_bytes(self):
        return (Path(self.config.config_dir) / 'agent.json').read_bytes()

    def _run(self, *, enroll_side_effect=None, enroll_return=None, queue_result=('completed', {'requested': True, 'marker_id': 'm1'}, '')):
        with patch.object(setup_wizard, 'AgentConfig') as config_cls, \
             patch.object(setup_wizard, '_queue_privileged_action', return_value=queue_result) as queue_mock, \
             patch.object(setup_wizard, 'PortalClient') as portal_client_cls, \
             patch.object(setup_wizard, 'enroll_wireguard') as enroll_mock, \
             patch.object(setup_wizard, 'first_enroll') as first_enroll_mock, \
             patch.object(setup_wizard, 'coordinated_reenroll') as reenroll_mock:
            config_cls.load.return_value = self.config
            if enroll_side_effect is not None:
                enroll_mock.side_effect = enroll_side_effect
            else:
                enroll_mock.return_value = enroll_return or dict(_WG_RESPONSE, public_key='p' * 43 + '=', private_key='k' * 43 + '=')
            setup_wizard.wireguard_enroll_main()
        return queue_mock, enroll_mock, portal_client_cls, first_enroll_mock, reenroll_mock

    # -- not yet activated ------------------------------------------------

    def test_not_activated_raises_systemexit_without_calling_enroll(self):
        with patch.object(setup_wizard, 'AgentConfig') as config_cls, \
             patch.object(setup_wizard, 'enroll_wireguard') as enroll_mock:
            config_cls.load.return_value = self.config  # no credential.json written
            with self.assertRaises(SystemExit) as ctx:
                setup_wizard.wireguard_enroll_main()
        self.assertIn('not been activated', str(ctx.exception))
        enroll_mock.assert_not_called()

    # -- an already-enrolled (activated, no WireGuard yet) appliance ------

    def test_first_run_on_an_already_activated_appliance_calls_enroll_wireguard(self):
        self._activate()
        _queue_mock, enroll_mock, portal_client_cls, first_enroll_mock, reenroll_mock = self._run()
        enroll_mock.assert_called_once()
        self.assertEqual(enroll_mock.call_args.kwargs.get('replace_existing'), False)
        # Built the portal client from the EXISTING credential, not a new one.
        portal_client_cls.assert_called_once_with(self.config.portal_url, 'appl-1', 'secret-cred')
        first_enroll_mock.assert_not_called()
        reenroll_mock.assert_not_called()

    def test_first_run_queues_interface_up(self):
        self._activate()
        queue_mock, _enroll_mock, *_ = self._run()
        calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[2], {'confirmed': True})

    def test_first_run_preserves_existing_identity_files_byte_for_byte(self):
        self._activate()
        before_credential = self.config.credential_file.read_bytes()
        before_agent = self._agent_json_bytes()
        self._run()
        self.assertEqual(self.config.credential_file.read_bytes(), before_credential)
        self.assertEqual(self._agent_json_bytes(), before_agent)

    # -- retries: running it twice must not duplicate anything ------------

    def test_running_twice_in_a_row_calls_enroll_wireguard_both_times(self):
        """enroll_wireguard() itself is idempotent (reuses the existing
        local keypair, and the cloud side returns the existing row for
        the same public_key) -- this command must call it every time,
        never skip it, and let that existing idempotency do the work."""
        self._activate()
        with patch.object(setup_wizard, 'AgentConfig') as config_cls, \
             patch.object(setup_wizard, '_queue_privileged_action', return_value=('completed', {}, '')) as queue_mock, \
             patch.object(setup_wizard, 'PortalClient'), \
             patch.object(setup_wizard, 'enroll_wireguard') as enroll_mock:
            config_cls.load.return_value = self.config
            response = dict(_WG_RESPONSE, public_key='p' * 43 + '=', private_key='k' * 43 + '=')
            enroll_mock.return_value = response
            setup_wizard.wireguard_enroll_main()
            # Second run: enroll_wireguard's own idempotent real behavior
            # is to return the SAME identity again -- simulate that by
            # actually persisting it, so this run's "previous" read sees it.
            save_wireguard_identity(self.config, response)
            setup_wizard.wireguard_enroll_main()
        self.assertEqual(enroll_mock.call_count, 2)
        # Only the FIRST run's interface-up queuing is real -- the second
        # run recognizes nothing changed and does not re-queue it.
        interface_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(len(interface_calls), 1)

    def test_already_wireguard_enrolled_with_identical_config_is_a_clean_noop(self):
        self._activate()
        identity = dict(_WG_RESPONSE, public_key='p' * 43 + '=', private_key='k' * 43 + '=')
        save_wireguard_identity(self.config, identity)
        queue_mock, enroll_mock, *_ = self._run(enroll_return=dict(identity))
        enroll_mock.assert_called_once()  # still called -- idempotency is enroll_wireguard's job, not skipped here
        interface_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(interface_calls, [])  # nothing changed -> no re-queue

    def test_already_wireguard_enrolled_noop_prints_no_interface_change_needed(self):
        self._activate()
        identity = dict(_WG_RESPONSE, public_key='p' * 43 + '=', private_key='k' * 43 + '=')
        save_wireguard_identity(self.config, identity)
        with patch('builtins.print') as mock_print:
            self._run(enroll_return=dict(identity))
        printed = ' '.join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn('already enrolled', printed)

    def test_reconciliation_when_cloud_state_changed_requeues_interface_up(self):
        """E.g. the cloud-side peer was revoked and re-enrolled under a
        new tunnel address for the same key -- a real, legitimate
        reconcile, not a duplicate."""
        self._activate()
        old_identity = dict(_WG_RESPONSE, public_key='p' * 43 + '=', private_key='k' * 43 + '=')
        save_wireguard_identity(self.config, old_identity)
        new_identity = dict(old_identity, tunnel_address='10.70.0.9')
        queue_mock, _enroll_mock, *_ = self._run(enroll_return=new_identity)
        interface_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(len(interface_calls), 1)

    # -- failure recovery ---------------------------------------------------

    def test_enrollment_failure_raises_systemexit_and_touches_nothing_locally(self):
        self._activate()
        before_credential = self.config.credential_file.read_bytes()
        before_agent = self._agent_json_bytes()
        with self.assertRaises(SystemExit) as ctx:
            self._run(enroll_side_effect=PortalError('portal unreachable'))
        self.assertIn('safe to re-run', str(ctx.exception))
        self.assertEqual(self.config.credential_file.read_bytes(), before_credential)
        self.assertEqual(self._agent_json_bytes(), before_agent)
        self.assertIsNone(load_wireguard_identity(self.config))

    def test_failure_then_successful_retry_recovers_cleanly(self):
        self._activate()
        with self.assertRaises(SystemExit):
            self._run(enroll_side_effect=PortalError('timeout'))
        queue_mock, enroll_mock, *_ = self._run()  # a normal successful run afterward
        enroll_mock.assert_called_once()
        interface_calls = [c for c in queue_mock.call_args_list if c.args[1] == 'wireguard_interface_up']
        self.assertEqual(len(interface_calls), 1)

    def test_a_failed_interface_up_queue_is_a_warning_not_a_crash(self):
        self._activate()
        with patch('builtins.print') as mock_print:
            self._run(queue_result=('failed', {}, 'pending_actions_dir is not writable'))
        printed = ' '.join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn('WARNING', printed)
        self.assertIn('WireGuard', printed)

    # -- CLI dispatch --------------------------------------------------------

    def test_main_dispatches_wireguard_enroll_flag(self):
        with patch.object(setup_wizard, 'wireguard_enroll_main') as target, \
             patch.object(setup_wizard, 'claim_main') as claim_mock, \
             patch.object(setup_wizard, 'interactive_main') as interactive_mock, \
             patch('sys.argv', ['anyaicam-setup', '--wireguard-enroll']):
            setup_wizard.main()
        target.assert_called_once()
        claim_mock.assert_not_called()
        interactive_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
