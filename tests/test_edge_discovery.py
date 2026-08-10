"""Stage 1 edge camera discovery tests.

Ported from the old (unmerged) edge/ package's tests/test_edge_camera_discovery.py
(source: feature/phase5-sprint2b-camera-provisioning @ c317f99, content verified
unchanged through feature/phase6-logout-csrf-hardening @ 393ffc7), adapted to
this repo's flat app/ module layout (app.edge.camera_discovery -> edge_discovery,
app.edge.camera_inventory -> camera_inventory) and with the CloudInventoryReporter
tests dropped — cloud/provider contact is out of scope for Stage 1.

All discovery is exercised through injected fakes (ws_probe/port_probe/metadata_probe).
No real socket or HTTP call is made by any test in this file.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app" if (ROOT / "app").exists() else ROOT.parent / "app"
sys.path.insert(0, str(APP))

from edge_discovery import (
    CameraDiscoveryService,
    DiscoveryOptions,
    _safe_onvif_url,
    _safe_rtsp_url,
    parse_ws_discovery,
    private_network,
)
from camera_inventory import CameraInventoryStore


ONVIF_RESPONSE = b"""<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <e:Body><d:ProbeMatches><d:ProbeMatch>
  <a:EndpointReference><a:Address>urn:uuid:camera-one</a:Address></a:EndpointReference>
  <d:Scopes>onvif://www.onvif.org/name/Front_Door onvif://www.onvif.org/hardware/DS-2CD2043G2</d:Scopes>
  <d:XAddrs>http://192.168.50.1/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></e:Body>
</e:Envelope>"""


class EdgeCameraDiscoveryTests(unittest.TestCase):
    def test_discovery_accepts_only_bounded_rfc1918_networks(self):
        self.assertEqual(str(private_network("192.168.50.0/24")), "192.168.50.0/24")
        for network in ("8.8.8.0/24", "127.0.0.0/30", "169.254.0.0/24"):
            with self.subTest(network=network), self.assertRaises(ValueError):
                private_network(network)
        with self.assertRaises(ValueError):
            private_network("10.0.0.0/16")

    def test_onvif_response_exposes_device_identity_and_scopes(self):
        device = parse_ws_discovery(ONVIF_RESPONSE)
        self.assertEqual(device["device_uuid"], "urn:uuid:camera-one")
        self.assertEqual(
            device["xaddrs"], ["http://192.168.50.1/onvif/device_service"]
        )
        self.assertIn("onvif://www.onvif.org/name/Front_Door", device["scopes"])

    def test_scan_combines_onvif_rtsp_vendor_model_and_sanitized_stream_url(self):
        device = parse_ws_discovery(ONVIF_RESPONSE)

        def port_probe(host, _ports, _timeout):
            return [80, 554] if host == "192.168.50.1" else []

        discovery = CameraDiscoveryService(
            ws_probe=lambda _timeout: [device],
            port_probe=port_probe,
            metadata_probe=lambda _url, _timeout: {
                "manufacturer": "Hikvision",
                "model": "DS-2CD2043G2",
                "stream_urls": ["rtsp://admin:secret@192.168.50.1:554/live"],
            },
        )
        cameras = discovery.scan(DiscoveryOptions("192.168.50.0/30"))

        self.assertEqual(len(cameras), 1)
        camera = cameras[0]
        self.assertEqual(camera["name"], "Front Door")
        self.assertEqual(camera["manufacturer"], "Hikvision")
        self.assertEqual(camera["model"], "DS-2CD2043G2")
        self.assertTrue(camera["onvif"])
        self.assertTrue(camera["rtsp"])
        self.assertEqual(camera["stream_urls"], ["rtsp://192.168.50.1:554/live"])

    def test_rtsp_url_sanitizer_never_persists_userinfo(self):
        self.assertEqual(
            _safe_rtsp_url("rtsp://admin:password@192.168.1.5:554/live?profile=1"),
            "rtsp://192.168.1.5:554/live?profile=1",
        )
        self.assertIsNone(_safe_rtsp_url("https://192.168.1.5/live"))
        self.assertEqual(
            _safe_onvif_url("http://admin:password@192.168.1.5/onvif/device_service"),
            "http://192.168.1.5/onvif/device_service",
        )

    def test_inventory_persists_discovery_and_marks_missing_camera_offline(self):
        camera = {
            "id": "camera-one",
            "name": "Front Door",
            "ip_address": "192.168.50.1",
            "manufacturer": "Hikvision",
            "model": "DS-2CD2043G2",
            "onvif": True,
            "rtsp": True,
            "stream_urls": ["rtsp://192.168.50.1/live"],
            "password": "must-not-persist",
            "camera_token": "must-also-not-persist",
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_inventory.json"
            store = CameraInventoryStore(path)
            first = store.reconcile("192.168.50.0/24", [camera])
            second = store.reconcile("192.168.50.0/24", [])

            self.assertTrue(path.exists())
            self.assertEqual(first["cameras"][0]["status"], "online")
            self.assertEqual(second["cameras"][0]["status"], "offline")
            self.assertNotIn("must-not-persist", path.read_text(encoding="utf-8"))
            self.assertNotIn("must-also-not-persist", path.read_text(encoding="utf-8"))


class CameraInventorySanitizationTests(unittest.TestCase):
    """Defense-in-depth coverage for CameraInventoryStore's allowlist and URL sanitization."""

    def test_reconcile_scrubs_legacy_fields_from_existing_disk_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_inventory.json"
            path.write_text(json.dumps({
                "version": 1,
                "updated_at": None,
                "last_scan": None,
                "cameras": [{
                    "id": "camera-one",
                    "name": "Front Door",
                    "ip_address": "192.168.50.1",
                    "discovery_network": "192.168.50.0/24",
                    "status": "online",
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "last_seen": "2026-01-01T00:00:00+00:00",
                    "last_scan": "2026-01-01T00:00:00+00:00",
                    # legacy Stage 4/5 fields that must not survive reconcile:
                    "provisioning_status": "provisioned",
                    "camera_number": 1,
                    "health_state": "healthy",
                    "recording": True,
                    "fps": 25.0,
                    "bitrate_bps": 2048000,
                    "username": "admin",
                    "password": "must-not-persist",
                }],
            }), encoding="utf-8")

            store = CameraInventoryStore(path)
            result = store.reconcile("192.168.50.0/24", [])

            camera = result["cameras"][0]
            for legacy_key in (
                "provisioning_status", "camera_number", "health_state",
                "recording", "fps", "bitrate_bps", "username", "password",
            ):
                self.assertNotIn(legacy_key, camera)
            self.assertEqual(camera["status"], "offline")
            self.assertNotIn("password", path.read_text(encoding="utf-8"))
            self.assertNotIn("must-not-persist", path.read_text(encoding="utf-8"))

    def test_stream_urls_never_persist_userinfo(self):
        camera = {
            "id": "camera-one",
            "ip_address": "192.168.1.5",
            "stream_urls": [
                "rtsp://user:password@192.168.1.5:554/live",
                "not-a-url",
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_inventory.json"
            store = CameraInventoryStore(path)
            result = store.reconcile("192.168.1.0/24", [camera])

            stream_urls = result["cameras"][0]["stream_urls"]
            self.assertEqual(stream_urls, ["rtsp://192.168.1.5:554/live"])
            serialized = path.read_text(encoding="utf-8")
            self.assertNotIn("user:password", serialized)
            self.assertNotIn("not-a-url", serialized)

    def test_onvif_xaddrs_never_persist_userinfo(self):
        camera = {
            "id": "camera-one",
            "ip_address": "192.168.1.5",
            "onvif_xaddrs": ["http://user:password@192.168.1.5/onvif/device_service"],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "camera_inventory.json"
            store = CameraInventoryStore(path)
            result = store.reconcile("192.168.1.0/24", [camera])

            xaddrs = result["cameras"][0]["onvif_xaddrs"]
            self.assertEqual(xaddrs, ["http://192.168.1.5/onvif/device_service"])
            self.assertNotIn("user:password", path.read_text(encoding="utf-8"))


class V110ScanSourceTests(unittest.TestCase):
    """Static checks that the live v1.1.0 scan route (app/main.py) keeps RTSP
    candidate URLs out of the browser response and V110_DISCOVERY_FILE
    history, retaining them only for CameraInventoryStore reconciliation.

    Source-inspection only - app/main.py is not imported here (matching this
    repo's existing convention, e.g. tests/test_edge_streaming.py's
    EdgeStreamingIntegrationSourceTests) and no production code is touched.
    """

    @classmethod
    def setUpClass(cls):
        cls.main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )

    def test_replacement_b_appends_redacted_dict_not_raw_result(self):
        self.assertIn("inventory_candidates.append(result)", self.main)
        self.assertIn("devices.append({", self.main)
        self.assertIn(
            'if key not in {"stream_urls", "stream_url_source", "stream_urls_verified"}',
            self.main,
        )
        self.assertNotIn("devices.append(result)", self.main)

    def test_replacement_c_reconcile_uses_inventory_candidates_not_devices(self):
        self.assertIn(
            "CameraInventoryStore(V110_INVENTORY_FILE).reconcile(str(network), inventory_candidates)",
            self.main,
        )
        self.assertNotIn("reconcile(str(network), devices)", self.main)


if __name__ == "__main__":
    unittest.main()
