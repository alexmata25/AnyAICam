"""Isolated tests for the appliance-agent's implemented camera scanner.

The scanner lives in ``appliance-agent/anyaicam_agent/discovery.py``. There
is no ``app.edge.camera_discovery`` package in this release; the cloud VMS
does not perform LAN discovery. Probe functions are replaced in these tests
so no network traffic is sent to cameras or other hosts.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = ROOT / "appliance-agent"
sys.path.insert(0, str(AGENT_ROOT))

from anyaicam_agent import discovery  # noqa: E402


ONVIF_RESPONSE = b"""<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">
 <e:Header><a:MessageID>uuid:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa</a:MessageID></e:Header>
 <e:Body><a:EndpointReference><a:Address>urn:uuid:11111111-2222-4333-8444-555555555555</a:Address>
 </a:EndpointReference></e:Body></e:Envelope>"""


class EdgeCameraDiscoveryTests(unittest.TestCase):
    def test_endpoint_uuid_uses_device_identity_not_probe_message_id(self):
        self.assertEqual(
            discovery._endpoint_uuid(ONVIF_RESPONSE.decode("utf-8")),
            "urn:uuid:11111111-2222-4333-8444-555555555555",
        )

    def test_invalid_or_nil_endpoint_uuid_is_rejected(self):
        for value in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            xml = ONVIF_RESPONSE.decode("utf-8").replace(
                "11111111-2222-4333-8444-555555555555", value
            )
            with self.subTest(value=value):
                self.assertIsNone(discovery._endpoint_uuid(xml))

    def test_scan_uses_onvif_identity_and_scope_metadata_without_network_io(self):
        scopes = (
            "onvif://www.onvif.org/name/Front_Door "
            "onvif://www.onvif.org/manufacturer/Hikvision "
            "onvif://www.onvif.org/model/DS-2CD2043G2"
        )
        onvif = {
            "192.168.50.1": {
                "scopes": scopes,
                "endpoint": "urn:uuid:11111111-2222-4333-8444-555555555555",
            }
        }

        def port(ip, port, _timeout=0.25):
            return ip == "192.168.50.1" and port == 554

        with (
            patch.object(discovery, "_onvif_probe", return_value=onvif),
            patch.object(discovery, "arp_table", return_value={}),
            patch.object(discovery, "_port", side_effect=port),
        ):
            cameras = discovery.scan(["192.168.50.0/30"], max_hosts=8)

        self.assertEqual(len(cameras), 1)
        camera = cameras[0]
        self.assertEqual(camera["name"], "Camera 192.168.50.1")
        self.assertEqual(camera["manufacturer"], "Hikvision")
        self.assertEqual(camera["model"], "DS-2CD2043G2")
        self.assertEqual(
            camera["device_key"],
            "urn:uuid:11111111-2222-4333-8444-555555555555",
        )
        self.assertTrue(camera["onvif_support"])
        self.assertTrue(camera["rtsp_support"])
        self.assertEqual(camera["rtsp_port"], 554)

    def test_scan_falls_back_to_hashed_mac_identity_and_detects_alternate_rtsp_port(self):
        with (
            patch.object(discovery, "_onvif_probe", return_value={}),
            patch.object(discovery, "arp_table", return_value={"192.168.50.1": "AA:BB:CC:DD:EE:FF"}),
            patch.object(
                discovery,
                "_port",
                side_effect=lambda ip, port, _timeout=0.25: ip == "192.168.50.1" and port == 8554,
            ),
        ):
            cameras = discovery.scan(["192.168.50.0/30"], max_hosts=8)

        self.assertEqual(len(cameras), 1)
        camera = cameras[0]
        self.assertTrue(camera["rtsp_support"])
        self.assertEqual(camera["rtsp_port"], 8554)
        self.assertTrue(camera["device_key"].startswith("mac-"))
        self.assertNotIn("AA:BB:CC:DD:EE:FF", camera["device_key"])


if __name__ == "__main__":
    unittest.main()
