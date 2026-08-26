"""Phase 3 (dynamic camera provisioning): real behavioral tests for
app/onvif_discovery.py. No FastAPI dependency -- fully importable, fully
testable without a real network via the injectable send_probe/tcp_probe
parameters (synthetic ONVIF devices, as the Phase 3 instructions require).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from onvif_discovery import parse_probe_response, probe  # noqa: E402


def synthetic_probe_match(device_uuid: str, xaddr: str) -> str:
    return (
        '<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
        f'<e:Header><w:MessageID>uuid:{device_uuid}</w:MessageID></e:Header>'
        '<e:Body><d:ProbeMatches><d:ProbeMatch>'
        f'<w:EndpointReference><w:Address>urn:uuid:{device_uuid}</w:Address></w:EndpointReference>'
        f'<d:XAddrs>{xaddr}</d:XAddrs>'
        '</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>'
    )


DEVICE_A = "12345678-1234-5678-1234-567812345678"
DEVICE_B = "87654321-4321-8765-4321-876543218765"


class ParseProbeResponseTests(unittest.TestCase):
    def test_extracts_device_key_and_onvif_endpoint(self):
        xml = synthetic_probe_match(DEVICE_A, "http://192.168.1.50/onvif/device_service")
        parsed = parse_probe_response(xml)
        self.assertEqual(parsed["device_key"], DEVICE_A.lower())
        self.assertEqual(parsed["onvif_endpoint"], "http://192.168.1.50/onvif/device_service")
        self.assertEqual(parsed["ip_address"], "192.168.1.50")

    def test_returns_none_for_garbage(self):
        self.assertIsNone(parse_probe_response("not xml at all"))
        self.assertIsNone(parse_probe_response("<e:Envelope></e:Envelope>"))


class ProbeTests(unittest.TestCase):
    def test_discovers_two_distinct_devices(self):
        responses = [
            synthetic_probe_match(DEVICE_A, "http://192.168.1.50/onvif/device_service"),
            synthetic_probe_match(DEVICE_B, "http://192.168.1.51/onvif/device_service"),
        ]
        results = probe(send_probe=lambda: responses, tcp_probe=lambda host, port, timeout: True)
        self.assertEqual({item["device_key"] for item in results}, {DEVICE_A.lower(), DEVICE_B.lower()})
        for item in results:
            self.assertTrue(item["rtsp_reachable"])
            self.assertTrue(item["onvif_reachable"])

    def test_duplicate_responses_for_the_same_device_key_are_deduplicated(self):
        # A real network can produce more than one WS-Discovery response
        # for the same physical camera (multicast retransmits) -- this
        # must never look like two different cameras.
        xml = synthetic_probe_match(DEVICE_A, "http://192.168.1.50/onvif/device_service")
        results = probe(send_probe=lambda: [xml, xml, xml], tcp_probe=lambda host, port, timeout: True)
        self.assertEqual(len(results), 1)

    def test_rediscovering_the_same_device_key_is_stable_across_ip_change(self):
        # The device_key (endpoint UUID) is what dedup/rebinding depends
        # on downstream -- it must stay identical even if DHCP moved the
        # camera to a different IP between two scans.
        first = probe(send_probe=lambda: [synthetic_probe_match(DEVICE_A, "http://192.168.1.50/onvif/device_service")],
                      tcp_probe=lambda host, port, timeout: True)
        second = probe(send_probe=lambda: [synthetic_probe_match(DEVICE_A, "http://192.168.1.99/onvif/device_service")],
                       tcp_probe=lambda host, port, timeout: True)
        self.assertEqual(first[0]["device_key"], second[0]["device_key"])
        self.assertNotEqual(first[0]["ip_address"], second[0]["ip_address"])

    def test_public_ip_responses_are_rejected(self):
        xml = synthetic_probe_match(DEVICE_A, "http://8.8.8.8/onvif/device_service")
        results = probe(send_probe=lambda: [xml], tcp_probe=lambda host, port, timeout: True)
        self.assertEqual(results, [])

    def test_network_scope_filters_out_of_range_devices(self):
        xml = synthetic_probe_match(DEVICE_A, "http://10.0.0.5/onvif/device_service")
        results = probe(send_probe=lambda: [xml], tcp_probe=lambda host, port, timeout: True,
                        allowed_network="192.168.1.0/24")
        self.assertEqual(results, [])

    def test_no_credentials_are_read_or_produced(self):
        xml = synthetic_probe_match(DEVICE_A, "http://192.168.1.50/onvif/device_service")
        results = probe(send_probe=lambda: [xml], tcp_probe=lambda host, port, timeout: True)
        serialized = str(results)
        for forbidden in ("username", "password", "admin", "credential"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_zero_devices_discovered_returns_empty_list_not_an_error(self):
        results = probe(send_probe=lambda: [], tcp_probe=lambda host, port, timeout: True)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
