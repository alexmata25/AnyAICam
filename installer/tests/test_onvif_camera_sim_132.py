"""The simulated ONVIF camera (installer/windows/sandbox/onvif-camera-sim.py) speaks exactly what the agent asks.

The agent's REAL code (anyaicam_agent.onvif_media and anyaicam_agent.discovery) runs against the simulator over
127.0.0.1 with real UDP and HTTP. The simulator binds an OS-assigned UDP port instead of 3702, so it never competes with
Windows' own WS-Discovery service. The agent's probes are pointed at that port: the multicast target is patched, and a
socket shim rewrites the fixed destination port 3702 of the unicast probe. Everything else is unmodified.
"""
import importlib.util
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent import discovery, onvif_media

SIM_PATH = Path(__file__).resolve().parents[1] / "windows" / "sandbox" / "onvif-camera-sim.py"
spec = importlib.util.spec_from_file_location("onvif_camera_sim", SIM_PATH)
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)

ENDPOINT = "urn:uuid:5f0c3b1e-1111-4222-8333-444455556666"
RTSP = "rtsp://127.0.0.1:8554/cam1"


class _Redirect(socket.socket):
    """The agent's unicast probe goes to (ip, 3702); send it to the simulator's port instead."""
    port = None

    def sendto(self, data, *args):
        address = args[-1]
        if address[1] == 3702:
            address = (address[0], _Redirect.port)
        return super().sendto(data, *args[:-1], address)


class SimulatorTests(unittest.TestCase):
    def start(self, **camera):
        options = dict(bind="127.0.0.1", http_port=0, rtsp=RTSP, endpoint=ENDPOINT, manufacturer="Hanwha", model="XNO-SIM")
        options.update(camera)
        self.camera = sim.Camera(**options)
        self.sim = sim.Simulator(self.camera, wsdd_port=0, reply_from_ephemeral=getattr(self, "ephemeral", False)).start()
        self.addCleanup(self.sim.stop)
        _Redirect.port = self.sim.wsdd_port

    def resolve(self, **credentials):
        with patch.object(onvif_media, "_WSDD_MULTICAST_ADDRESS", ("127.0.0.1", self.sim.wsdd_port)):
            return onvif_media.resolve_media_uri("127.0.0.1", ENDPOINT, timeout=3, **credentials)

    # ---------------------------------------------------------------- media resolution (provisioning / reconnect)
    def test_the_agent_resolves_the_main_stream_through_the_full_onvif_chain(self):
        self.start()
        result = self.resolve()
        self.assertEqual(result["status"], "resolved", result)
        self.assertEqual((result["rtsp_uri"], result["profile_token"], result["profile_name"]), (RTSP, "Profile_1", "MainStream"))
        self.assertEqual(result["profile_count"], 2)
        self.assertEqual([op for op, _ in self.camera.requests], ["GetCapabilities", "GetProfiles", "GetStreamUri"])

    def test_a_camera_that_requires_credentials_is_reported_then_resolved_with_them(self):
        self.start(username="admin", password="test-only-pass")
        self.assertEqual(self.resolve()["status"], "auth_required")
        self.assertEqual(self.resolve(username="admin", password="wrong")["status"], "auth_required")
        self.camera.requests.clear()
        result = self.resolve(username="admin", password="test-only-pass")
        self.assertEqual(result["status"], "resolved", result)
        # unauthenticated first, then the same call with a valid WS-Security digest, for each operation
        self.assertEqual(self.camera.requests, [("GetCapabilities", False), ("GetCapabilities", True), ("GetProfiles", False),
                                                ("GetProfiles", True), ("GetStreamUri", False), ("GetStreamUri", True)])
        self.assertNotIn("test-only-pass", repr(result))

    def test_a_reply_from_another_device_identity_is_not_used(self):
        self.start(endpoint="urn:uuid:00000000-1111-4222-8333-999999999999")
        with patch.object(onvif_media, "_WSDD_MULTICAST_ADDRESS", ("127.0.0.1", self.sim.wsdd_port)):
            self.assertIsNone(onvif_media._probe_xaddr("127.0.0.1", ENDPOINT, timeout=1.0))

    # ---------------------------------------------------------------- discovery (scan and CAM-5 add by address)
    def test_unicast_probe_finds_the_camera_with_its_endpoint_identity_and_scopes(self):
        self.start()
        with patch("anyaicam_agent.discovery.socket.socket", _Redirect):
            found = discovery._onvif_probe_host("127.0.0.1", timeout=1.5)
        self.assertEqual(found["127.0.0.1"]["endpoint"], ENDPOINT)
        record = discovery._inspect("127.0.0.1", found, {}, timeout=0.2)
        self.assertEqual(record["device_key"], ENDPOINT)
        self.assertEqual((record["manufacturer"], record["model"], record["onvif_support"]), ("Hanwha", "XNO-SIM", True))

    def test_a_reply_from_an_ephemeral_source_port_is_still_accepted(self):
        self.ephemeral = True
        self.start()
        with patch("anyaicam_agent.discovery.socket.socket", _Redirect):
            found = discovery._onvif_probe_host("127.0.0.1", timeout=1.5)
        self.assertEqual(found["127.0.0.1"]["endpoint"], ENDPOINT)

    def test_a_probe_for_another_device_type_gets_no_answer(self):
        self.start()
        probe = b'<e:Envelope><e:Body><d:Probe><d:Types>dn:Printer</d:Types></d:Probe></e:Body></e:Envelope>'
        self.assertIsNone(self.camera.probe_match(probe))
        self.assertIsNotNone(self.camera.probe_match(probe.replace(b"dn:Printer", b"dn:NetworkVideoTransmitter")))

    def test_a_stale_digest_or_another_user_is_refused(self):
        camera = sim.Camera(bind="127.0.0.1", http_port=0, rtsp=RTSP, username="admin", password="p")
        header = onvif_media._ws_security_header("admin", "p")
        self.assertTrue(camera.authorized(header))
        stale = header.replace(header.split("<wsu:Created>")[1].split("</wsu:Created>")[0], "2026-01-01T00:00:00Z")
        self.assertFalse(camera.authorized(stale))
        self.assertFalse(camera.authorized(header.replace("<Username>admin<", "<Username>other<")))


if __name__ == "__main__":
    unittest.main()
