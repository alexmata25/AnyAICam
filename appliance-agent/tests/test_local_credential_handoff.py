"""Cloud->edge camera-configuration sync (2026-09-12), agent-side half.

Regression coverage for _deliver_credential_to_local_vms(): the ONE
plaintext credential poll_provisioning() already legitimately holds in
memory for a successfully-verified job is handed once, over loopback, to
this same box's own VMS process -- so it can be encrypted and persisted
locally (see app/main.py's provisioned_camera_credential() for the
receiving half, and app/tests/test_edge_camera_sync.py for the
reconciliation side). This agent process itself never persists the
credential in any form, at any point, before or after this fix -- these
tests prove that too.
"""

import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.service import ApplianceAgent


def _agent(folder):
    config = AgentConfig(cloud_id='AIC-TEST1', portal_url='https://portal.example', state_dir=folder, config_dir=folder, log_dir=folder)
    agent = ApplianceAgent(config)
    agent.client.credential = 'agent-own-bearer-credential'
    return agent


class DeliverCredentialToLocalVmsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)

    def test_posts_to_the_local_loopback_vms_url_with_the_agents_own_bearer_credential(self):
        captured = {}

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{}'

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['method'] = request.get_method()
            captured['headers'] = dict(request.headers)
            captured['body'] = json.loads(request.data.decode())
            return FakeResponse()

        with patch('anyaicam_agent.service.urllib.request.urlopen', side_effect=fake_urlopen):
            self.agent._deliver_credential_to_local_vms('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2'})

        self.assertEqual(captured['url'], 'http://127.0.0.1:8000/api/local/provisioned-camera-credential')
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['headers'].get('Authorization'), 'Bearer agent-own-bearer-credential')
        self.assertEqual(captured['body'], {'device_key': 'urn:uuid:aaaa', 'username': 'admin', 'password': 'hunter2'})

    def test_missing_credentials_dict_sends_nothing(self):
        with patch('anyaicam_agent.service.urllib.request.urlopen') as urlopen_mock:
            self.agent._deliver_credential_to_local_vms('urn:uuid:aaaa', None)
        urlopen_mock.assert_not_called()

    def test_empty_device_key_sends_nothing(self):
        with patch('anyaicam_agent.service.urllib.request.urlopen') as urlopen_mock:
            self.agent._deliver_credential_to_local_vms('', {'username': 'admin', 'password': 'hunter2'})
        urlopen_mock.assert_not_called()

    def test_local_delivery_failure_is_logged_not_raised(self):
        import urllib.error
        with patch('anyaicam_agent.service.urllib.request.urlopen', side_effect=urllib.error.URLError('connection refused')):
            self.agent._deliver_credential_to_local_vms('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2'})  # must not raise

    def test_failure_log_line_never_contains_the_credential(self):
        import urllib.error
        with patch('anyaicam_agent.service.urllib.request.urlopen', side_effect=urllib.error.URLError('connection refused')), \
             patch.object(self.agent.log, 'warning') as warning_mock:
            self.agent._deliver_credential_to_local_vms('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2-secret'})
        warning_mock.assert_called_once()
        logged_text = ' '.join(str(a) for a in warning_mock.call_args.args) + ' '.join(str(v) for v in warning_mock.call_args.kwargs.values())
        self.assertNotIn('admin', logged_text)
        self.assertNotIn('hunter2-secret', logged_text)


class PollProvisioningLocalHandoffWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)

    def test_successful_job_triggers_the_local_handoff_with_the_same_credentials(self):
        job = {'id': 'job-1', 'device_key': 'urn:uuid:aaaa', 'credentials': {'username': 'admin', 'password': 'hunter2'}}
        self.agent.client.request = MagicMock(return_value={'jobs': [job]})
        with patch('anyaicam_agent.service.verify_device', return_value=(True, 'ok')), \
             patch.object(self.agent, '_resolve_media_uri_after_provisioning'), \
             patch.object(self.agent, '_deliver_credential_to_local_vms') as deliver_mock:
            self.agent.poll_provisioning()
        deliver_mock.assert_called_once_with('urn:uuid:aaaa', {'username': 'admin', 'password': 'hunter2'})

    def test_failed_job_never_triggers_the_local_handoff(self):
        job = {'id': 'job-1', 'device_key': 'urn:uuid:aaaa', 'credentials': {'username': 'admin', 'password': 'hunter2'}}
        self.agent.client.request = MagicMock(return_value={'jobs': [job]})
        with patch('anyaicam_agent.service.verify_device', return_value=(False, 'nope')), \
             patch.object(self.agent, '_deliver_credential_to_local_vms') as deliver_mock:
            self.agent.poll_provisioning()
        deliver_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
