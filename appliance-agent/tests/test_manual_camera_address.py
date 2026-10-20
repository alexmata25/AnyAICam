"""Manual camera add by IP address, appliance side (CAM-5, 2026-10-20).

A discovery job may carry one customer-entered LAN address. The agent
re-validates it, probes only that host, builds the SAME record scan() builds
(same device_key precedence), stores it, and sends the same redacted result.
Verification later finds the camera again at its stored address when a full
scan cannot see it -- but only if it still presents the same device_key."""
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from anyaicam_agent import discovery, provisioning
from anyaicam_agent.config import AgentConfig
from anyaicam_agent.service import ApplianceAgent

KEY = 'urn:uuid:5f0c3b1e-1111-4222-8333-444455556666'


def _agent(folder):
    config = AgentConfig(cloud_id='AIC-TEST1', portal_url='https://portal.example', state_dir=folder, config_dir=folder, log_dir=folder)
    return ApplianceAgent(config)


class ManualTargetValidationTests(unittest.TestCase):
    def test_local_ipv4_addresses_are_accepted(self):
        for address in ('192.168.1.64', '10.0.0.5', '172.16.4.9', '169.254.10.10', '100.64.1.2', ' 192.168.1.64 '):
            self.assertEqual(discovery.manual_target(address), address.strip())

    def test_anything_else_is_refused(self):
        for address in ('8.8.8.8', '127.0.0.1', '0.0.0.0', '224.0.0.251', '255.255.255.255', 'camera.local',
                        '192.168.1', '192.168.1.300', 'fe80::1', '', None, '192.168.1.64; rm -rf /'):
            self.assertIsNone(discovery.manual_target(address), address)

    def test_a_refused_address_is_never_probed(self):
        with patch('anyaicam_agent.discovery._onvif_probe_host') as probe, patch('anyaicam_agent.discovery._port') as port:
            self.assertIsNone(discovery.probe_host('8.8.8.8'))
        probe.assert_not_called(); port.assert_not_called()


class ProbeHostTests(unittest.TestCase):
    def _probe(self, *, onvif=None, open_ports=(554,), arp=None):
        with patch('anyaicam_agent.discovery._onvif_probe_host', return_value=onvif or {}), \
             patch('anyaicam_agent.discovery._port', side_effect=lambda ip, port, timeout=.25: port in open_ports), \
             patch('anyaicam_agent.discovery.arp_table', return_value=arp or {}):
            return discovery.probe_host('192.168.1.64')

    def test_an_onvif_camera_gets_its_endpoint_uuid_as_device_key(self):
        record = self._probe(onvif={'192.168.1.64': {'scopes': 'onvif://www.onvif.org/manufacturer/Hanwha onvif://www.onvif.org/model/XNO', 'endpoint': KEY}})
        self.assertEqual(record['device_key'], KEY)
        self.assertEqual((record['manufacturer'], record['model'], record['rtsp_port']), ('Hanwha', 'XNO', 554))
        self.assertTrue(record['manual_target'])

    def test_without_onvif_the_same_precedence_as_a_scan_applies(self):
        by_mac = self._probe(arp={'192.168.1.64': 'AA:BB:CC:DD:EE:FF'})
        self.assertTrue(by_mac['device_key'].startswith('mac-'))
        off_subnet = self._probe()
        self.assertTrue(off_subnet['device_key'].startswith('ip-'))

    def test_the_record_matches_what_a_scan_builds_for_the_same_host(self):
        onvif = {'192.168.1.64': {'scopes': '', 'endpoint': KEY}}
        manual = self._probe(onvif=onvif, arp={'192.168.1.64': 'aa:bb:cc:dd:ee:ff'})
        with patch('anyaicam_agent.discovery._port', side_effect=lambda ip, port, timeout=.25: port == 554):
            scanned = discovery._inspect('192.168.1.64', onvif, {'192.168.1.64': 'aa:bb:cc:dd:ee:ff'})
        self.assertEqual({k: v for k, v in manual.items() if k != 'manual_target'}, scanned)

    def test_no_camera_at_the_address(self):
        self.assertIsNone(self._probe(open_ports=()))

    def test_full_scan_still_uses_the_shared_inspect(self):
        with patch('anyaicam_agent.discovery.local_networks', return_value=[]), \
             patch('anyaicam_agent.discovery._onvif_probe', return_value={'192.168.1.64': {'scopes': '', 'endpoint': KEY}}), \
             patch('anyaicam_agent.discovery.arp_table', return_value={}), \
             patch('anyaicam_agent.discovery._port', side_effect=lambda ip, port, timeout=.25: port == 554):
            results = discovery.scan()
        self.assertEqual([r['device_key'] for r in results], [KEY])
        self.assertNotIn('manual_target', results[0])


class LocateDeviceFallbackTests(unittest.TestCase):
    def test_a_manually_added_camera_is_found_again_at_its_stored_address(self):
        known = [{'device_key': KEY, 'ip': '10.9.8.7', 'manual_target': True}]
        with patch('anyaicam_agent.provisioning.scan', return_value=[]), \
             patch('anyaicam_agent.provisioning.probe_host', return_value={'device_key': KEY, 'ip': '10.9.8.7'}) as probe:
            found = provisioning.locate_device(KEY, None, known)
        probe.assert_called_once_with('10.9.8.7')
        self.assertEqual(found['ip'], '10.9.8.7')

    def test_a_different_device_now_at_that_address_is_not_accepted(self):
        known = [{'device_key': KEY, 'ip': '10.9.8.7', 'manual_target': True}]
        with patch('anyaicam_agent.provisioning.scan', return_value=[]), \
             patch('anyaicam_agent.provisioning.probe_host', return_value={'device_key': 'urn:uuid:00000000-0000-4000-8000-000000000001', 'ip': '10.9.8.7'}):
            self.assertIsNone(provisioning.locate_device(KEY, None, known))

    def test_discovered_cameras_are_never_probed_by_address(self):
        known = [{'device_key': KEY, 'ip': '192.168.1.64'}]
        with patch('anyaicam_agent.provisioning.scan', return_value=[]), \
             patch('anyaicam_agent.provisioning.probe_host') as probe:
            self.assertIsNone(provisioning.locate_device(KEY, None, known))
        probe.assert_not_called()

    def test_a_scan_hit_wins_and_no_address_probe_is_made(self):
        known = [{'device_key': KEY, 'ip': '10.9.8.7', 'manual_target': True}]
        with patch('anyaicam_agent.provisioning.scan', return_value=[{'device_key': KEY, 'ip': '192.168.1.80'}]), \
             patch('anyaicam_agent.provisioning.probe_host') as probe:
            self.assertEqual(provisioning.locate_device(KEY, None, known)['ip'], '192.168.1.80')
        probe.assert_not_called()


class PollDiscoveryManualJobTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.agent = _agent(self._tmpdir.name)
        self.agent.send_or_queue = MagicMock(return_value=True)

    def _poll(self, job, found=None):
        self.agent.client.request = MagicMock(return_value={'jobs': [job]})
        with patch('anyaicam_agent.service.probe_host', return_value=found) as probe, \
             patch('anyaicam_agent.service.scan') as scan:
            self.agent.poll_discovery()
        payload = self.agent.send_or_queue.call_args.args[1]
        return probe, scan, payload

    def test_a_manual_job_probes_only_that_address_and_sends_a_redacted_result(self):
        found = {'device_key': KEY, 'ip': '10.9.8.7', 'mac_address': 'aa:bb:cc:dd:ee:ff', 'manual_target': True,
                 'manufacturer': 'Hanwha', 'model': 'XNO', 'onvif_support': True, 'rtsp_support': True, 'rtsp_port': 554}
        probe, scan, payload = self._poll({'id': 'job-1', 'target_ip': '10.9.8.7'}, found)
        probe.assert_called_once_with('10.9.8.7'); scan.assert_not_called()
        self.assertEqual(payload['status'], 'complete')
        self.assertEqual([r['device_key'] for r in payload['results']], [KEY])
        self.assertNotIn('10.9.8.7', str(payload)); self.assertNotIn('aa:bb:cc', str(payload))
        stored = self.agent.discovered_store.cameras()
        self.assertEqual((stored[0]['device_key'], stored[0]['ip'], stored[0]['manual_target']), (KEY, '10.9.8.7', True))

    def test_nothing_at_the_address_completes_with_no_results(self):
        _, _, payload = self._poll({'id': 'job-2', 'target_ip': '10.9.8.7'}, None)
        self.assertEqual((payload['status'], payload['results']), ('complete', []))

    def test_an_address_the_cloud_should_have_refused_is_refused_here_too(self):
        probe, scan, payload = self._poll({'id': 'job-3', 'target_ip': '8.8.8.8'})
        probe.assert_not_called(); scan.assert_not_called()
        self.assertEqual(payload['status'], 'error')

    def test_verification_is_given_the_stored_records(self):
        self.agent.client.request = MagicMock(return_value={'jobs': [{'id': 'job-7', 'device_key': KEY, 'credentials': {'username': 'a', 'password': 'b'}}]})
        self.agent._deliver_credential_to_local_vms = MagicMock()
        self.agent.discovered_store.save_scan([{'device_key': KEY, 'ip': '10.9.8.7', 'manual_target': True, 'mac_address': 'Unknown'}])
        with patch('anyaicam_agent.service.verify_device', return_value=(False, 'no')) as verify:
            self.agent.poll_provisioning()
        known = verify.call_args.args[3]
        self.assertEqual(known[0]['ip'], '10.9.8.7')


if __name__ == '__main__':
    unittest.main()
