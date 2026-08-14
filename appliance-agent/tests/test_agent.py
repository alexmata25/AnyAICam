import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
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


    def _relay_config(self,folder):
        return AgentConfig(state_dir=folder,config_dir=folder,log_dir=folder)

    def _read_relay_state(self,config):
        return json.loads(config.live_relay_commands_file.read_text(encoding='utf-8'))

    def test_start_live_relay_writes_desired_state_and_completes(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,result,error=execute('start_live_relay',{'camera_number':5,'camera_id':'camera-5'},config)
            self.assertEqual(status,'completed'); self.assertEqual(error,'')
            self.assertEqual(result,{'camera_number':5,'active':True})
            state=self._read_relay_state(config)
            self.assertEqual(state,{'5':{'camera_id':'camera-5','active':True}})

    def test_stop_live_relay_updates_the_same_camera_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            execute('start_live_relay',{'camera_number':5,'camera_id':'camera-5'},config)
            status,result,error=execute('stop_live_relay',{'camera_number':5,'camera_id':'camera-5'},config)
            self.assertEqual(status,'completed'); self.assertEqual(error,'')
            self.assertEqual(result,{'camera_number':5,'active':False})
            state=self._read_relay_state(config)
            self.assertEqual(state,{'5':{'camera_id':'camera-5','active':False}})

    def test_second_camera_command_preserves_the_first_cameras_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            execute('start_live_relay',{'camera_number':5,'camera_id':'camera-5'},config)
            execute('start_live_relay',{'camera_number':7,'camera_id':'camera-7'},config)
            state=self._read_relay_state(config)
            self.assertEqual(state,{
                '5':{'camera_id':'camera-5','active':True},
                '7':{'camera_id':'camera-7','active':True},
            })

    def test_missing_camera_number_fails_safely_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,result,error=execute('start_live_relay',{'camera_id':'camera-5'},config)
            self.assertEqual(status,'failed'); self.assertEqual(result,{})
            self.assertFalse(config.live_relay_commands_file.exists())

    def test_non_numeric_camera_number_fails_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,_,error=execute('start_live_relay',{'camera_number':'not-a-number','camera_id':'camera-5'},config)
            self.assertEqual(status,'failed')

    def test_out_of_range_camera_number_fails_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            for bad_number in (0,-1,257,10000):
                with self.subTest(bad_number=bad_number):
                    status,_,_=execute('start_live_relay',{'camera_number':bad_number,'camera_id':'camera-5'},config)
                    self.assertEqual(status,'failed')
            self.assertFalse(config.live_relay_commands_file.exists())

    def test_boolean_camera_number_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,_,_=execute('start_live_relay',{'camera_number':True,'camera_id':'camera-5'},config)
            self.assertEqual(status,'failed')
            self.assertFalse(config.live_relay_commands_file.exists())

    def test_missing_camera_id_fails_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,result,_=execute('start_live_relay',{'camera_number':5},config)
            self.assertEqual(status,'failed'); self.assertEqual(result,{})

    def test_blank_camera_id_fails_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,_,_=execute('start_live_relay',{'camera_number':5,'camera_id':'   '},config)
            self.assertEqual(status,'failed')

    def test_oversized_camera_id_fails_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,_,_=execute('start_live_relay',{'camera_number':5,'camera_id':'x'*129},config)
            self.assertEqual(status,'failed')
            self.assertFalse(config.live_relay_commands_file.exists())

    def test_non_string_camera_id_fails_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,_,_=execute('start_live_relay',{'camera_number':5,'camera_id':12345},config)
            self.assertEqual(status,'failed')

    def test_malformed_non_dict_payload_fails_without_raising(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            for bad_payload in (None,'not-a-dict',['camera-5'],5):
                with self.subTest(bad_payload=bad_payload):
                    status,result,error=execute('start_live_relay',bad_payload,config)
                    self.assertEqual(status,'failed'); self.assertEqual(result,{})

    def test_existing_corrupted_state_file_recovers_to_empty_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            config.live_relay_commands_file.parent.mkdir(parents=True,exist_ok=True)
            config.live_relay_commands_file.write_text('{not valid json',encoding='utf-8')
            status,_,_=execute('start_live_relay',{'camera_number':5,'camera_id':'camera-5'},config)
            self.assertEqual(status,'completed')
            state=self._read_relay_state(config)
            self.assertEqual(state,{'5':{'camera_id':'camera-5','active':True}})

    def test_existing_non_dict_json_state_recovers_to_empty_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            config.live_relay_commands_file.parent.mkdir(parents=True,exist_ok=True)
            config.live_relay_commands_file.write_text('["not","a","dict"]',encoding='utf-8')
            status,_,_=execute('start_live_relay',{'camera_number':5,'camera_id':'camera-5'},config)
            self.assertEqual(status,'completed')
            state=self._read_relay_state(config)
            self.assertEqual(state,{'5':{'camera_id':'camera-5','active':True}})

    def test_write_failure_returns_failed_tuple_and_never_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            with patch('pathlib.Path.write_text',side_effect=OSError('disk full')):
                status,result,error=execute('start_live_relay',{'camera_number':5,'camera_id':'camera-5'},config)
            self.assertEqual(status,'failed'); self.assertEqual(result,{}); self.assertTrue(error)

    def test_unsupported_command_still_rejected_with_relay_commands_present(self):
        status,result,error=execute('shell',{'command':'rm -rf /'},AgentConfig())
        self.assertEqual(status,'failed'); self.assertIn('shell',error.lower())

    def test_restart_service_behavior_is_unchanged(self):
        stop_event=Event()
        status,result,error=execute('restart_service',{},AgentConfig(),stop_event)
        self.assertEqual(status,'completed'); self.assertEqual(result,{'restart_requested':True}); self.assertTrue(stop_event.is_set())

    def test_run_diagnostics_behavior_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,result,error=execute('run_diagnostics',{},config)
            self.assertEqual(status,'completed'); self.assertIn('hostname',result); self.assertIn('credential_exists',result)

    @patch('anyaicam_agent.commands.scan',return_value=[])
    def test_refresh_cameras_behavior_is_unchanged(self,mock_scan):
        status,result,error=execute('refresh_cameras',{},AgentConfig())
        self.assertEqual(status,'completed'); self.assertEqual(result,{'cameras':[]}); mock_scan.assert_called_once()

    def test_error_text_never_contains_the_camera_id(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            status,_,error=execute('start_live_relay',{'camera_number':None,'camera_id':'super-secret-camera-id'},config)
            self.assertEqual(status,'failed')
            self.assertNotIn('super-secret-camera-id',error)

    def test_desired_state_file_never_contains_credential_shaped_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self._relay_config(folder)
            execute('start_live_relay',{
                'camera_number':5,'camera_id':'camera-5',
                'username':'admin','password':'hunter2','rtsp_url':'rtsp://admin:hunter2@10.0.0.5/',
            },config)
            raw=config.live_relay_commands_file.read_text(encoding='utf-8')
            for forbidden in ('username','password','rtsp_url','hunter2','admin'):
                self.assertNotIn(forbidden,raw)

if __name__=='__main__': unittest.main()
