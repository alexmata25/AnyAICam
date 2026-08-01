import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

from anyaicam_agent.commands import execute
from anyaicam_agent.config import AgentConfig,load_credential,save_credential
from anyaicam_agent.portal import PortalClient
from anyaicam_agent.queue import OfflineQueue


class AgentTests(unittest.TestCase):
    def test_credentials_are_stored_and_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            config=AgentConfig(state_dir=folder,config_dir=folder,log_dir=folder); save_credential(config,{'credential':'secret'}); self.assertEqual(load_credential(config)['credential'],'secret')

    def test_offline_queue_deduplicates_and_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            queue=OfflineQueue(Path(folder)/'queue.db'); self.assertTrue(queue.put('same','POST','/events',{'id':'same'})); self.assertFalse(queue.put('same','POST','/events',{'id':'same'})); self.assertEqual(queue.count(),1); self.assertEqual(queue.fail('same'),2); queue.success('same'); self.assertEqual(queue.count(),0)

    def test_remote_shell_is_rejected(self):
        status,result,error=execute('shell',{'command':'rm -rf /'},AgentConfig()); self.assertEqual(status,'failed'); self.assertIn('shell',error.lower())

    @patch('urllib.request.urlopen')
    def test_authenticated_request_has_replay_headers(self,urlopen):
        response=MagicMock(); response.read.return_value=b'{}'; response.__enter__.return_value=response; urlopen.return_value=response
        PortalClient('https://portal.example','appliance-1','credential').request('POST','/api/appliance/heartbeat',{'cpu':1}); request=urlopen.call_args.args[0]
        self.assertEqual(request.headers['Authorization'],'Bearer credential'); self.assertIn('X-appliance-id',request.headers); self.assertIn('X-request-timestamp',request.headers); self.assertIn('X-request-nonce',request.headers)

    @patch('urllib.request.urlopen')
    def test_camera_passwords_never_leave_agent(self,urlopen):
        response=MagicMock(); response.read.return_value=b'{}'; response.__enter__.return_value=response; urlopen.return_value=response
        PortalClient('https://portal.example','appliance-1','credential').request('POST','/api/appliance/cameras',{'cameras':[{'name':'Front','username':'admin','password':'hidden','rtsp_url':'rtsp://hidden'}]}); body=json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body,{'cameras':[{'name':'Front'}]})


if __name__=='__main__': unittest.main()
