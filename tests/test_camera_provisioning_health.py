import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.edge.camera_health import CameraHealthMonitor, CameraHealthStore
from app.edge.camera_inventory import CameraInventoryStore
from app.edge.camera_provisioning import (
    CameraConfigurationStore,
    CameraProbeService,
    credentialed_rtsp_url,
    private_camera_url,
)
from app.edge.provisioning_service import CameraProvisioningService


class OnvifResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=None):
        return b'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><GetDeviceInformationResponse xmlns="http://www.onvif.org/ver10/device/wsdl"><Manufacturer>Axis</Manufacturer><Model>P3265-LV</Model><FirmwareVersion>11.1</FirmwareVersion><SerialNumber>ABC123</SerialNumber></GetDeviceInformationResponse></s:Body></s:Envelope>'''


class SuccessfulProbe:
    def validate_rtsp(self, *_args):
        return {"valid": True, "codec": "h264", "width": 1920, "height": 1080, "fps": 25.0, "bitrate_bps": 2048000}

    def validate_onvif(self, *_args):
        return {"valid": True, "manufacturer": "Axis", "model": "P3265-LV"}

    def thumbnail(self, *_args):
        return b"jpeg"


class FakeReporter:
    def __init__(self):
        self.inventory = None

    def report(self, inventory):
        self.inventory = inventory
        serialized = json.dumps(inventory)
        assert "camera-password" not in serialized
        return {"status": "reported", "camera_count": len(inventory["cameras"])}


class CameraProvisioningTests(unittest.TestCase):
    def test_camera_urls_are_limited_to_literal_private_addresses(self):
        self.assertEqual(private_camera_url("rtsp://192.168.1.10:554/live", {"rtsp"}), "rtsp://192.168.1.10:554/live")
        self.assertEqual(credentialed_rtsp_url("rtsp://192.168.1.10/live", "camera user", "p@ss"), "rtsp://camera%20user:p%40ss@192.168.1.10/live")
        for url in ("rtsp://8.8.8.8/live", "rtsp://127.0.0.1/live", "rtsp://camera.example.test/live", "https://192.168.1.10/live"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                private_camera_url(url, {"rtsp"})

    def test_rtsp_validation_returns_fps_bitrate_and_never_uses_a_shell(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001", "bit_rate": "2500000"}], "format": {}}), stderr="")

        result = CameraProbeService(runner=runner).validate_rtsp("rtsp://192.168.1.20/live", "admin", "secret")
        self.assertTrue(result["valid"])
        self.assertEqual(result["fps"], 29.97)
        self.assertEqual(result["bitrate_bps"], 2500000)
        self.assertNotIn("shell", captured["kwargs"])
        self.assertIn("rtsp://admin:secret@192.168.1.20/live", captured["command"])

    def test_onvif_uses_username_token_digest_not_plaintext_password(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = request.data.decode("utf-8")
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return OnvifResponse()

        result = CameraProbeService(opener=opener).validate_onvif("http://192.168.1.20/onvif/device_service", "admin", "plain-password")
        self.assertTrue(result["valid"])
        self.assertEqual(result["manufacturer"], "Axis")
        self.assertIn("PasswordDigest", captured["body"])
        self.assertNotIn("plain-password", captured["body"])

    def test_persistent_configuration_redacts_credentials_from_api_view(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "provisioned_cameras.json"
            store = CameraConfigurationStore(path)
            safe = store.save_camera({"id": "one", "camera_number": 1, "name": "Front Door", "rtsp_url": "rtsp://192.168.1.20/live", "username": "admin", "password": "camera-password"})
            self.assertNotIn("username", safe)
            self.assertNotIn("password", safe)
            self.assertTrue(safe["credentials_configured"])
            self.assertEqual(store.get("one")["password"], "camera-password")
            self.assertNotIn("camera-password", json.dumps(store.safe_view()))

            store.save_camera({"id": "legacy", "camera_number": 2, "name": "Loading Dock", "rtsp_url": "rtsp://legacy:secret@192.168.1.21/live", "username": "legacy", "password": "secret"})
            serialized = json.dumps(store.safe_view())
            self.assertNotIn("legacy:secret", serialized)
            self.assertIn("rtsp://192.168.1.21/live", serialized)

    def test_provisioning_updates_inventory_and_synchronizes_safely(self):
        reporter = FakeReporter()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CameraInventoryStore(root / "inventory.json")
            inventory.reconcile("192.168.1.0/24", [{"id": "one", "name": "Camera 1", "ip_address": "192.168.1.20", "manufacturer": "Unknown", "model": "Unknown", "rtsp": True, "onvif": True, "stream_urls": ["rtsp://192.168.1.20/live"], "onvif_xaddrs": ["http://192.168.1.20/onvif/device_service"]}])
            configurations = CameraConfigurationStore(root / "configuration.json")
            health = CameraHealthMonitor(configurations, CameraHealthStore(root / "health.json"), SuccessfulProbe(), root / "hls", root / "recordings", 15, lambda _number: {"recording": "running"})
            service = CameraProvisioningService(inventory, configurations, SuccessfulProbe(), health, reporter_factory=lambda: reporter)
            result = service.provision("one", {"name": "Front Entrance", "camera_number": 1, "username": "admin", "password": "camera-password"})
            self.assertEqual(result["camera"]["name"], "Front Entrance")
            self.assertFalse(result["credentials_returned"])
            public = inventory.load()["cameras"][0]
            self.assertEqual(public["provisioning_status"], "provisioned")
            self.assertNotIn("password", public)
            synchronized = service.synchronize()
            self.assertTrue(synchronized["sanitized"])
            self.assertEqual(synchronized["cloud_report"]["status"], "reported")

    def test_rediscovery_preserves_friendly_name_and_provisioning_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CameraInventoryStore(Path(folder) / "inventory.json")
            camera = {"id": "one", "name": "Discovered Name", "ip_address": "192.168.1.20"}
            store.reconcile("192.168.1.0/24", [camera])
            store.update("one", {"name": "Loading Dock", "provisioning_status": "provisioned", "camera_number": 4})
            refreshed = store.reconcile("192.168.1.0/24", [{**camera, "name": "Vendor Name"}])
            self.assertEqual(refreshed["cameras"][0]["name"], "Loading Dock")
            self.assertEqual(refreshed["cameras"][0]["discovered_name"], "Vendor Name")


class CameraHealthTests(unittest.TestCase):
    def test_health_combines_rtsp_hls_metrics_and_recording_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hls = root / "hls"; hls.mkdir()
            recordings = root / "recordings" / "camera1"; recordings.mkdir(parents=True)
            (hls / "camera1_001.ts").write_bytes(b"segment")
            (hls / "camera1.m3u8").write_text("#EXTM3U\ncamera1_001.ts\n", encoding="utf-8")
            (recordings / "camera1_test.mkv").write_bytes(b"recording")
            configs = CameraConfigurationStore(root / "configuration.json")
            configs.save_camera({"id": "one", "camera_number": 1, "name": "Front Door", "rtsp_url": "rtsp://192.168.1.20/live", "username": "admin", "password": "secret"})
            monitor = CameraHealthMonitor(configs, CameraHealthStore(root / "health.json"), SuccessfulProbe(), hls, root / "recordings", 15, lambda _number: {"recording": "running"})
            camera = monitor.refresh()["cameras"][0]
            self.assertEqual(camera["state"], "healthy")
            self.assertEqual(camera["rtsp"], "online")
            self.assertEqual(camera["hls"], "fresh")
            self.assertEqual(camera["fps"], 25.0)
            self.assertEqual(camera["bitrate_bps"], 2048000)
            self.assertEqual(camera["recording"], "running")
            self.assertEqual(camera["latest_recording"]["file"], "camera1_test.mkv")


class CameraProvisioningIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.main = (root / "app" / "main.py").read_text(encoding="utf-8")
        cls.routes = (root / "app" / "edge" / "routes.py").read_text(encoding="utf-8")
        cls.cloud = (root / "app" / "edge" / "cloud_inventory.py").read_text(encoding="utf-8")

    def test_edge_only_routes_and_periodic_health_are_wired(self):
        self.assertIn('runtime_role not in {"edge", "combined"}', self.routes)
        self.assertIn('@app.post("/api/edge/cameras/{camera_id}/provision")', self.routes)
        self.assertIn('@app.post("/api/edge/cameras/health/refresh")', self.routes)
        self.assertIn("edge_camera_provisioning.refresh_health", self.main)
        self.assertIn('if RUNTIME_ROLE in {"edge", "combined"}:', self.main)
        self.assertIn("camera_configuration_store().by_number(camera_number)", self.main)
        self.assertIn('("camera-provisioning", "/edge/camera-provisioning"', self.main)

    def test_validation_preview_and_synchronization_routes_exist(self):
        self.assertIn('@app.post("/api/edge/cameras/{camera_id}/validate")', self.routes)
        self.assertIn('@app.post("/api/edge/cameras/{camera_id}/thumbnail")', self.routes)
        self.assertIn('@app.post("/api/edge/cameras/synchronize")', self.routes)
        self.assertIn('@app.get("/edge/camera-provisioning"', self.routes)

    def test_cloud_report_has_no_private_camera_fields(self):
        self.assertNotIn('"rtsp_url"', self.cloud)
        self.assertNotIn('"ip_address"', self.cloud)
        self.assertNotIn('"username"', self.cloud)
        self.assertNotIn('"password"', self.cloud)


if __name__ == "__main__":
    unittest.main()
