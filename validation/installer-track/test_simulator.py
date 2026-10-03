import importlib.util
from pathlib import Path
import unittest
from xml.etree import ElementTree as ET

spec=importlib.util.spec_from_file_location('simulator',Path(__file__).with_name('onvif_simulator.py'))
sim=importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


class SimulatorContractTests(unittest.TestCase):
    def test_onvif_profiles_and_stream_uri_are_parseable(self):
        for action in ('GetServices','GetCapabilities','GetProfiles','GetVideoSources',
                       'GetStreamUri','GetDeviceInformation','GetSystemDateAndTime'):
            with self.subTest(action=action):
                ET.fromstring(sim.response(action,'192.168.253.10'))
        xml=ET.fromstring(sim.response('GetStreamUri','192.168.253.10'))
        uri=next(x.text for x in xml.iter() if x.tag.endswith('}Uri'))
        self.assertEqual(uri,'rtsp://192.168.253.10:8554/testcamera')

    def test_pinned_vms_parser_uses_endpoint_uuid_instead_of_message_uuid(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'app'))
        from onvif_discovery import parse_probe_response
        data=sim.discovery('192.168.253.10','uuid:test-request')
        ET.fromstring(data)
        candidate=parse_probe_response(data.decode())
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate['device_key'],sim.CAMERA_ID.lower())
        self.assertIn(b'uuid:test-request',data)


    def test_discovery_xml_has_stable_endpoint_and_unique_message_ids(self):
        first=ET.fromstring(sim.discovery('192.168.253.10','uuid:test-request'))
        second=ET.fromstring(sim.discovery('192.168.253.10','uuid:test-request'))
        def value(xml, name):
            return next(x.text for x in xml.iter() if x.tag.endswith('}'+name))
        self.assertEqual(value(first,'Address'),'urn:uuid:'+sim.CAMERA_ID)
        self.assertEqual(value(first,'Address'),value(second,'Address'))
        self.assertNotEqual(value(first,'MessageID'),value(second,'MessageID'))
        self.assertEqual(value(first,'RelatesTo'),'uuid:test-request')

    def test_mutation_and_nonisolated_network_are_refused(self):
        for action in ('SetNetworkInterfaces','SystemReboot','FactoryReset'):
            with self.assertRaises(ValueError): sim.response(action,'192.168.253.10')
        for ip,cidr in [('8.8.8.8','0.0.0.0/0'),('192.168.1.2','192.168.253.0/24'),
                        ('192.168.1.2','192.168.0.0/16')]:
            with self.assertRaises(ValueError): sim.validate_network(ip,cidr)
