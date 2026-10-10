"""Codec discovery reuses the authorized provisioning credentials only
in memory, probes every ONVIF stream, and sends sanitized profile
metadata without credentials. Successful reprovisioning refreshes codec
observations even when a prior main URI already existed."""

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.service import ApplianceAgent


def _agent(folder):
    config = AgentConfig(cloud_id='AIC-TEST1', portal_url='https://portal.example', state_dir=folder, config_dir=folder, log_dir=folder)
    return ApplianceAgent(config)


class ResolveMediaProfilesAfterProvisioningTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)
        self.agent.client.request = MagicMock()

    def test_profiles_are_submitted_without_any_credential_in_the_payload(self):
        streams = [{'profile_token': 'main', 'profile_name': 'MainStream', 'stream_role': 'main',
                    'rtsp_uri': 'rtsp://192.0.2.10:554/main', 'advertised_codec': 'h264',
                    'observed_codec': 'h265'}]
        self.agent.client.request.return_value = {'cameras': [{'id': 'cam-1', 'device_key': 'urn:uuid:aaaa', 'onvif_endpoint': 'rtsp://old/main'}]}
        with patch('anyaicam_agent.service.locate_device', return_value={'ip': '192.0.2.10'}), \
             patch('anyaicam_agent.service.resolve_media_streams', return_value={'status': 'resolved', 'streams': streams}) as resolve_mock:
            self.agent._resolve_media_uri_after_provisioning('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2'})
        resolve_mock.assert_called_once_with('192.0.2.10', 'urn:uuid:aaaa', username='admin', password='hunter2')
        post_calls = [c for c in self.agent.client.request.call_args_list if c.args[0] == 'POST']
        self.assertEqual(len(post_calls), 2)
        self.assertTrue(post_calls[0].args[1].endswith('/media-uri'))
        self.assertEqual(post_calls[0].args[2], {'device_key': 'urn:uuid:aaaa', 'rtsp_uri': 'rtsp://192.0.2.10:554/main'})
        self.assertTrue(post_calls[1].args[1].endswith('/media-profiles'))
        submitted_payload = post_calls[1].args[2]
        self.assertEqual(submitted_payload, {'device_key': 'urn:uuid:aaaa', 'streams': streams})
        self.assertNotIn('admin', str(submitted_payload))
        self.assertNotIn('hunter2', str(submitted_payload))

    def test_device_not_found_on_lan_submits_nothing(self):
        self.agent.client.request.return_value = {'cameras': [{'id': 'cam-1', 'device_key': 'urn:uuid:aaaa', 'onvif_endpoint': None}]}
        with patch('anyaicam_agent.service.locate_device', return_value=None):
            self.agent._resolve_media_uri_after_provisioning('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2'})
        post_calls = [c for c in self.agent.client.request.call_args_list if c.args[0] == 'POST']
        self.assertEqual(post_calls, [])

    def test_reprovision_revalidates_even_when_a_media_uri_already_exists(self):
        self.agent.client.request.return_value = {'cameras': [{'id': 'cam-1', 'device_key': 'urn:uuid:aaaa', 'onvif_endpoint': 'rtsp://already/there'}]}
        with patch('anyaicam_agent.service.locate_device', return_value={'ip': '192.0.2.10'}), \
             patch('anyaicam_agent.service.resolve_media_streams', return_value={'status': 'resolved', 'streams': [
                 {'profile_token': 'main', 'stream_role': 'main', 'rtsp_uri': 'rtsp://192.0.2.10/new',
                  'advertised_codec': 'h265', 'observed_codec': 'h265'}]}) as resolve_mock:
            self.agent._resolve_media_uri_after_provisioning('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2'})
        resolve_mock.assert_called_once()
        self.assertTrue(any(call.args[1].endswith('/media-profiles') for call in self.agent.client.request.call_args_list if call.args[0] == 'POST'))

    def test_auth_required_result_submits_nothing(self):
        self.agent.client.request.return_value = {'cameras': [{'id': 'cam-1', 'device_key': 'urn:uuid:aaaa', 'onvif_endpoint': None}]}
        with patch('anyaicam_agent.service.locate_device', return_value={'ip': '192.0.2.10'}), \
             patch('anyaicam_agent.service.resolve_media_streams', return_value={'status': 'auth_required', 'streams': []}):
            self.agent._resolve_media_uri_after_provisioning('urn:uuid:aaaa', {'username': 'admin', 'password': 'wrong'})
        post_calls = [c for c in self.agent.client.request.call_args_list if c.args[0] == 'POST']
        self.assertEqual(post_calls, [])


class PollProvisioningWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)

    def test_successful_job_triggers_resolution_with_the_same_credentials(self):
        job = {'id': 'job-1', 'device_key': 'urn:uuid:aaaa', 'credentials': {'username': 'admin', 'password': 'hunter2'}}
        self.agent.client.request = MagicMock(return_value={'jobs': [job]})
        with patch('anyaicam_agent.service.verify_device', return_value=(True, 'ok')), \
             patch.object(self.agent, '_resolve_media_uri_after_provisioning') as resolve_after:
            self.agent.poll_provisioning()
        resolve_after.assert_called_once_with('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2'})

    def test_failed_job_never_triggers_resolution(self):
        job = {'id': 'job-1', 'device_key': 'urn:uuid:aaaa', 'credentials': {'username': 'admin', 'password': 'hunter2'}}
        self.agent.client.request = MagicMock(return_value={'jobs': [job]})
        with patch('anyaicam_agent.service.verify_device', return_value=(False, 'nope')), \
             patch.object(self.agent, '_resolve_media_uri_after_provisioning') as resolve_after:
            self.agent.poll_provisioning()
        resolve_after.assert_not_called()


if __name__ == '__main__':
    unittest.main()
