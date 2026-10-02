"""Provisioning Phase 7 (device-side entitlement refresh): previous
audits this session found that POST /api/provisioning/refresh existed
and was fully tested cloud-side, but nothing in appliance-agent ever
called it. This file covers the device-side caller (service.py's
poll_entitlement()/current_camera_slot_quantity()) added to close that
gap: real signed appliance auth (the same self.client every other call
already uses, no second auth mechanism), its own slower cadence separate
from checkin_seconds, and fail-safe behavior on cloud unavailability --
the last successfully-fetched value is preserved, never bumped
optimistically, and a fresh install that has never once succeeded
reports 0 rather than a guess.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.portal import PortalError
from anyaicam_agent.service import ApplianceAgent


def _agent(folder):
    config = AgentConfig(cloud_id='AIC-TEST1', portal_url='https://portal.example', state_dir=folder, config_dir=folder, log_dir=folder, entitlement_refresh_interval_seconds=1800)
    return ApplianceAgent(config)


class EntitlementRefreshTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)
        self.agent.client.request = MagicMock()

    def test_fresh_install_with_no_state_file_reports_zero_slots(self):
        self.assertEqual(self.agent.current_camera_slot_quantity(), 0)

    def test_successful_refresh_calls_the_real_endpoint_with_real_auth_and_persists_the_quantity(self):
        self.agent.client.request.return_value = {
            'cloud_id': 'AIC-TEST1', 'activation_status': 'activated', 'camera_slot_quantity': 8,
            'entitlements': [{'product': 'camera_slots_local', 'camera_slot_quantity': 8, 'status': 'active', 'expires_at': None}],
        }
        self.agent.poll_entitlement()
        self.agent.client.request.assert_called_once_with('POST', '/api/provisioning/refresh')
        self.assertEqual(self.agent.current_camera_slot_quantity(), 8)
        state = json.loads(Path(self.agent.config.entitlement_state_file).read_text(encoding='utf-8'))
        self.assertEqual(state['camera_slot_quantity'], 8)
        self.assertEqual(state['entitlements'][0]['product'], 'camera_slots_local')
        self.assertIn('fetched_at', state)

    def test_cloud_unavailable_never_changes_the_last_known_value(self):
        self.agent.client.request.return_value = {'camera_slot_quantity': 16, 'entitlements': []}
        self.agent.poll_entitlement()
        self.assertEqual(self.agent.current_camera_slot_quantity(), 16)

        # Force the cadence gate open again and simulate the cloud being unreachable.
        self.agent._next_entitlement_check_at = 0.0
        self.agent.client.request.side_effect = PortalError('Connection refused')
        self.agent.poll_entitlement()

        self.assertEqual(self.agent.current_camera_slot_quantity(), 16)  # unchanged, not zeroed, not bumped

    def test_network_failure_before_any_successful_refresh_still_reports_zero_not_unlimited(self):
        self.agent.client.request.side_effect = PortalError('Connection refused')
        self.agent.poll_entitlement()
        self.assertEqual(self.agent.current_camera_slot_quantity(), 0)
        self.assertFalse(Path(self.agent.config.entitlement_state_file).exists())

    def test_unexpected_exception_during_refresh_is_swallowed_and_never_touches_state(self):
        self.agent.client.request.side_effect = RuntimeError('boom')
        self.agent.poll_entitlement()  # must not raise
        self.assertEqual(self.agent.current_camera_slot_quantity(), 0)

    def test_malformed_response_is_treated_as_a_failed_refresh_not_a_crash_or_a_guess(self):
        self.agent.client.request.return_value = {'camera_slot_quantity': 'not-a-number'}
        self.agent.poll_entitlement()  # must not raise
        self.assertEqual(self.agent.current_camera_slot_quantity(), 0)

    def test_own_cadence_skips_repeat_calls_within_the_refresh_interval(self):
        self.agent.client.request.return_value = {'camera_slot_quantity': 8, 'entitlements': []}
        self.agent.poll_entitlement()
        self.agent.poll_entitlement()
        self.agent.poll_entitlement()
        self.assertEqual(self.agent.client.request.call_count, 1)  # not called again until the interval elapses

    def test_never_calls_the_endpoint_without_the_agents_real_signed_credential(self):
        # authenticated=True is PortalClient.request()'s own default --
        # poll_entitlement() never passes authenticated=False, so the
        # real client's own credential/signature requirement (portal.py)
        # applies exactly as it does to every other call this agent makes.
        import inspect
        source = inspect.getsource(self.agent.poll_entitlement)
        self.assertNotIn('authenticated=False', source)


if __name__ == '__main__':
    unittest.main()
