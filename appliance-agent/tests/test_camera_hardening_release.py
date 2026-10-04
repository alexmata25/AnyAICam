"""Focused standard-library regressions for the selectively ported camera hardening."""
import logging
import socketserver
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from anyaicam_agent import discovery, provisioning
from anyaicam_agent.camera_binding import (
    CameraBindingStore,
    DiscoveredCameraStore,
    auto_bind_discovered_cameras,
    reconcile_cloud_cameras,
    stable_device_key,
)


UUID = "urn:uuid:414e5941-4943-414d-8123-123456789abc"


class _RtspHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            line = self.rfile.readline()
            if not line:
                return
            request = line.decode("ascii", "replace").strip()
            headers = {}
            while True:
                line = self.rfile.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                key, _, value = line.decode("ascii", "replace").partition(":")
                headers[key.lower()] = value.strip()
            method = request.split(" ", 1)[0]
            self.server.requests.append(request)
            status = "200 OK"
            extra = ""
            body = b""
            if method == "DESCRIBE" and self.server.protected:
                status = "401 Unauthorized"
                extra = 'WWW-Authenticate: Basic realm="test-camera"\r\n'
            elif method == "DESCRIBE":
                body = b"v=0\r\n"
                extra = "Content-Type: application/sdp\r\n"
            response = (f"RTSP/1.0 {status}\r\nCSeq: {headers.get('cseq', '1')}\r\n"
                        f"{extra}Content-Length: {len(body)}\r\n\r\n").encode() + body
            try:
                self.wfile.write(response)
                self.wfile.flush()
            except OSError:
                return


class _RtspServer:
    def __init__(self, protected=False):
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _RtspHandler)
        self.server.daemon_threads = True
        self.server.protected = protected
        self.server.requests = []
        self.port = self.server.server_address[1]

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class CameraHardeningTests(unittest.TestCase):
    def test_only_canonical_non_nil_onvif_uuid_is_macless_stable_identity(self):
        self.assertEqual(stable_device_key(UUID.upper()), UUID)
        for value in ("ip-123", "mac-123", "urn:uuid:1234", "urn:uuid:00000000-0000-0000-0000-000000000000"):
            with self.subTest(value=value):
                self.assertIsNone(stable_device_key(value))

    def test_duplicate_uuid_at_different_endpoints_is_ambiguous(self):
        with TemporaryDirectory() as td:
            store = CameraBindingStore(Path(td) / "bindings.json")
            records = DiscoveredCameraStore(Path(td) / "discovered.json")
            cloud = [{"id": "cam-1", "camera_number": 1, "device_key": UUID}]
            first = {"device_key": UUID, "ip": "192.0.2.1", "mac_address": "Unknown"}
            second = {**first, "device_key": UUID.upper(), "ip": "192.0.2.2"}
            self.assertEqual(auto_bind_discovered_cameras(cloud, [first, second], store), [])
            records.save_scan([first, second])
            self.assertTrue(records.cameras()[0]["identity_ambiguous"])
            self.assertEqual(auto_bind_discovered_cameras(cloud, records.cameras(), store), [])

    def test_uuid_cannot_replace_established_mac_or_authorize_wrong_cloud_camera(self):
        with TemporaryDirectory() as td:
            store = CameraBindingStore(Path(td) / "bindings.json")
            store.bind("cam-1", 1, "02:00:00:00:00:01", device_key=UUID)
            discovered = [{"device_key": UUID, "ip": "192.0.2.1", "mac_address": "02:00:00:00:00:02"}]
            cloud = [{"id": "cam-1", "camera_number": 1, "device_key": UUID}]
            self.assertEqual(auto_bind_discovered_cameras(cloud, discovered, store), [])
            self.assertEqual(store.bindings()[0]["mac_address"], "02:00:00:00:00:01")
            reader = Mock()
            wrong_cloud = [{"id": "cam-1", "camera_number": 1, "device_key": "urn:uuid:414e5941-4943-414d-8123-123456789abd"}]
            result = reconcile_cloud_cameras(wrong_cloud, discovered, store.bindings(), reader)
            self.assertEqual(result[0]["last_error"], "binding_device_key_mismatch")
            reader.status.assert_not_called()

    def test_transient_arp_miss_does_not_erase_established_mac_binding(self):
        with TemporaryDirectory() as td:
            store = CameraBindingStore(Path(td) / "bindings.json")
            store.bind("cam-1", 1, "02:00:00:00:00:01", device_key=UUID)
            discovered = [{"device_key": UUID, "ip": "192.0.2.1", "mac_address": "Unknown"}]
            cloud = [{"id": "cam-1", "camera_number": 1, "device_key": UUID}]
            self.assertEqual(auto_bind_discovered_cameras(cloud, discovered, store), [])
            self.assertEqual(store.bindings()[0]["mac_address"], "02:00:00:00:00:01")

    def test_established_cloud_camera_identity_cannot_be_replaced_by_same_mac(self):
        with TemporaryDirectory() as td:
            store = CameraBindingStore(Path(td) / "bindings.json")
            other_uuid = "urn:uuid:414e5941-4943-414d-8123-123456789abd"
            store.bind("cam-1", 1, "02:00:00:00:00:01", device_key=UUID)
            discovered = [{"device_key": other_uuid, "ip": "192.0.2.1", "mac_address": "02:00:00:00:00:01"}]
            cloud = [{"id": "cam-1", "camera_number": 1, "device_key": other_uuid}]
            self.assertEqual(auto_bind_discovered_cameras(cloud, discovered, store), [])
            self.assertEqual(store.bindings()[0]["device_key"], UUID)

    def test_probe_message_id_is_not_mistaken_for_endpoint_uuid(self):
        message_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        text = (f'<e:Envelope xmlns:e="urn:test" xmlns:w="urn:wsa"><e:Header>'
                f'<w:MessageID>uuid:{message_id}</w:MessageID></e:Header></e:Envelope>')
        self.assertIsNone(discovery._endpoint_uuid(text))

    def test_anonymous_camera_must_pass_actual_describe_and_query_is_redacted(self):
        with _RtspServer() as camera:
            device = {"ip": "127.0.0.1", "rtsp_support": True, "rtsp_port": camera.port}
            with patch.object(provisioning, "locate_device", return_value=device):
                success, detail = provisioning.verify_device(UUID, None)
        self.assertTrue(success, detail)
        self.assertTrue(any(request.startswith("DESCRIBE ") for request in camera.server.requests))

    def test_protected_camera_is_rejected_when_customer_supplies_no_credentials(self):
        with _RtspServer(protected=True) as camera:
            device = {"ip": "127.0.0.1", "rtsp_support": True, "rtsp_port": camera.port}
            with patch.object(provisioning, "locate_device", return_value=device):
                success, detail = provisioning.verify_device(UUID, None)
        self.assertFalse(success)
        self.assertIn("no credentials", detail)

    def test_rtsp_endpoint_must_be_same_camera_and_cannot_embed_credentials(self):
        for uri in ("rtsp://192.0.2.2:8554/live", "rtsp://u:p@127.0.0.1/live",
                    "https://127.0.0.1/live", "rtsp://127.0.0.1:0/live", "rtsp://127.0.0.1/live#fragment"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                provisioning.stream_target({"ip": "127.0.0.1", "rtsp_uri": uri}, UUID)

    def test_rtsp_query_is_not_written_to_auth_diagnostics(self):
        with _RtspServer() as camera:
            from io import StringIO
            output = StringIO()
            handler = logging.StreamHandler(output)
            logger = logging.getLogger("anyaicam.agent")
            old_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                provisioning.classify_rtsp_authentication("127.0.0.1", camera.port, "", "", "/live?token=private-query")
            finally:
                logger.removeHandler(handler)
                logger.setLevel(old_level)
        self.assertIn("path=/live ", output.getvalue())
        self.assertNotIn("private-query", output.getvalue())


if __name__ == "__main__":
    unittest.main()
