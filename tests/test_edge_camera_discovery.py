import json
import tempfile
import unittest
from pathlib import Path

from app.edge.camera_discovery import (
    CameraDiscoveryService,
    DiscoveryOptions,
    _safe_onvif_url,
    _safe_rtsp_url,
    parse_ws_discovery,
    private_network,
)
from app.edge.camera_inventory import CameraInventoryStore
from app.edge.cloud_inventory import CloudInventoryReporter


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


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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

    def test_cloud_report_uses_existing_endpoint_without_private_camera_data(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse({"status": "accepted", "credentials_received": False})

        reporter = CloudInventoryReporter(
            "https://cloud.example.test",
            "appliance-one",
            "registration-credential",
            opener=opener,
        )
        result = reporter.report({
            "cameras": [{
                "id": "camera-one",
                "name": "Front Door",
                "status": "online",
                "manufacturer": "Hikvision",
                "model": "DS-2CD2043G2",
                "onvif": True,
                "rtsp": True,
                "ip_address": "192.168.50.1",
                "stream_urls": ["rtsp://192.168.50.1/live"],
            }]
        })

        self.assertEqual(captured["url"], "https://cloud.example.test/api/appliance/cameras")
        self.assertEqual(result["status"], "reported")
        serialized = json.dumps(captured["payload"])
        self.assertNotIn("192.168.50.1", serialized)
        self.assertNotIn("stream_urls", serialized)
        self.assertNotIn("credential", serialized)
        self.assertEqual(captured["payload"]["cameras"][0]["protocols"], ["onvif", "rtsp"])

    def test_unregistered_appliance_skips_cloud_report(self):
        result = CloudInventoryReporter("", "", "").report({"cameras": []})
        self.assertEqual(result["status"], "skipped")


class EdgeDiscoveryIntegrationSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.main = (cls.root / "app" / "main.py").read_text(encoding="utf-8")
        cls.routes = (cls.root / "app" / "edge" / "routes.py").read_text(encoding="utf-8")

    def test_edge_routes_are_registered_without_cloud_api_changes(self):
        self.assertIn("register_edge_discovery_routes(", self.main)
        self.assertIn('runtime_role=RUNTIME_ROLE', self.main)
        self.assertIn('@app.post("/api/edge/cameras/discover")', self.routes)
        self.assertIn('@app.get("/api/edge/cameras/inventory")', self.routes)
        self.assertIn('@app.post("/api/edge/cameras/inventory/report")', self.routes)


if __name__ == "__main__":
    unittest.main()
