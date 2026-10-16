"""Reconnect, appliance side (2026-10-15): after a successful provisioning
job for an EXISTING camera, the agent moves only the host of its stream
address -- and only after the camera's own stream URL at the new address
accepts the job's credentials. Without a job id the old "already resolved,
nothing to do" behaviour is unchanged."""
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.service import ApplianceAgent

KEY = 'urn:uuid:fd430000-f7d4-11b4-83a1-142ffd60a285'
PATH = '/Streaming/Channels/101?transportmode=unicast&profile=Profile_1'
OLD = f'rtsp://192.168.0.44:554{PATH}'


def _agent(folder):
    config = AgentConfig(cloud_id='AIC-TEST1', portal_url='https://portal.example', state_dir=folder, config_dir=folder, log_dir=folder)
    return ApplianceAgent(config)


class ReconnectExistingCameraAddressTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)
        self.agent.client.request = MagicMock(return_value={'cameras': [{'id': 'cam-2', 'device_key': KEY, 'onvif_endpoint': OLD}]})

    def _run(self, ip='192.168.0.43', verified=(True, 'ok'), job_id='job-1'):
        with patch('anyaicam_agent.service.locate_device', return_value={'ip': ip}), \
             patch('anyaicam_agent.service.verify_rtsp_credentials', return_value=verified) as verify, \
             patch('anyaicam_agent.service.resolve_media_uri') as resolve:
            self.agent._resolve_media_uri_after_provisioning(KEY, {'username': 'admin', 'password': 'hunter2'}, job_id=job_id)
        posts = [c for c in self.agent.client.request.call_args_list if c.args[0] == 'POST']
        return verify, resolve, posts

    def test_moved_camera_is_verified_at_its_own_stream_url_then_only_the_host_is_submitted(self):
        verify, resolve, posts = self._run()
        verify.assert_called_once_with('192.168.0.43', 554, 'admin', 'hunter2', path=PATH)
        resolve.assert_not_called()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].args[1], '/api/appliance/AIC-TEST1/cameras/cam-2/media-uri')
        self.assertEqual(posts[0].args[2], {'device_key': KEY, 'rtsp_uri': f'rtsp://192.168.0.43:554{PATH}', 'provisioning_job_id': 'job-1'})
        self.assertNotIn('hunter2', str(posts[0].args[2]))

    def test_rejected_credentials_submit_nothing(self):
        _, _, posts = self._run(verified=(False, 'auth failed'))
        self.assertEqual(posts, [])

    def test_a_camera_still_at_its_address_submits_nothing(self):
        verify, _, posts = self._run(ip='192.168.0.44')
        verify.assert_not_called()
        self.assertEqual(posts, [])

    def test_without_a_job_id_an_existing_address_is_left_alone(self):
        verify, _, posts = self._run(job_id=None)
        verify.assert_not_called()
        self.assertEqual(posts, [])

    def test_poll_provisioning_passes_the_job_id(self):
        self.agent.client.request = MagicMock(return_value={'jobs': [{'id': 'job-7', 'device_key': KEY, 'credentials': {'username': 'a', 'password': 'b'}}]})
        self.agent.send_or_queue = MagicMock(return_value=True)
        self.agent._deliver_credential_to_local_vms = MagicMock()
        with patch('anyaicam_agent.service.verify_device', return_value=(True, 'ok')), \
             patch.object(self.agent, '_resolve_media_uri_after_provisioning') as resolve:
            self.agent.poll_provisioning()
        resolve.assert_called_once_with(KEY, {'username': 'a', 'password': 'b'}, job_id='job-7')


if __name__ == '__main__':
    unittest.main()
