"""Windows 1.3.2 (Blue): manual camera add by IP address (CAM-5, Red 4b562ed4) on the Windows agent (CAM-10).

Red's test_manual_camera_address.py pins the platform-independent behaviour. This file pins what differs on Windows:
  * the MAC comes from `arp -a` (windows.arp_table), which lists only on-link neighbours -- an off-subnet camera has
    no entry, so its device_key is the ip- hash, exactly as on Linux; an on-link one gets the same mac- key as Linux;
  * a Windows UDP socket raises ConnectionResetError (WSAECONNRESET) from recvfrom when the camera answers the
    unicast WS-Discovery probe with ICMP port-unreachable (no ONVIF) -- the probe must treat that as "no ONVIF" and
    the RTSP check must still find the camera;
  * the reply to the unicast probe is outbound-initiated, so it needs no inbound firewall rule (firewall.ps1 adds
    none for discovery); a camera that answers from another source port is still accepted (match on address only).
Real-device behaviour through Windows Defender Firewall stays an acceptance-VM check (CAM-10).
"""
import hashlib
import unittest
from unittest.mock import patch

from anyaicam_agent import discovery, windows

# `arp -a` on Windows 11 (one interface): the camera 192.168.1.64 is on-link; 10.20.30.40 is on another subnet.
ARP_OUTPUT = """
Interface: 192.168.1.20 --- 0x7
  Internet Address      Physical Address      Type
  192.168.1.1           f0-9f-c2-11-22-33     dynamic
  192.168.1.64          00-09-18-ab-cd-ef     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""


class _ResetSocket:
    """A Windows UDP socket after ICMP port-unreachable: every recvfrom raises WSAECONNRESET."""
    def __init__(self, *args, **kwargs):
        self.sent = []

    def settimeout(self, value):
        pass

    def sendto(self, data, address):
        self.sent.append(address)

    def recvfrom(self, size):
        raise ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host')

    def close(self):
        pass


class _ReplySocket(_ResetSocket):
    def __init__(self, reply_from):
        super().__init__()
        self.reply_from = reply_from

    def recvfrom(self, size):
        body = ('<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
                'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
                'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><e:Body><d:ProbeMatches><d:ProbeMatch>'
                '<w:EndpointReference><w:Address>urn:uuid:5f0c3b1e-1111-4222-8333-444455556666</w:Address></w:EndpointReference>'
                '<d:Scopes>onvif://www.onvif.org/manufacturer/Hanwha onvif://www.onvif.org/model/XNO</d:Scopes>'
                '</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>')
        return body.encode(), self.reply_from


class WindowsManualCameraTests(unittest.TestCase):
    def _probe(self, ip, *, sock=_ResetSocket, open_ports=(554,)):
        with patch.object(windows, 'IS_WINDOWS', True), \
             patch.object(windows, 'arp_table', lambda: windows.parse_arp(ARP_OUTPUT)), \
             patch('anyaicam_agent.discovery.socket.socket', sock), \
             patch('anyaicam_agent.discovery._port', side_effect=lambda host, port, timeout=.25: port in open_ports):
            return discovery.probe_host(ip)

    def test_the_windows_arp_table_feeds_the_agent(self):
        with patch.object(windows, 'IS_WINDOWS', True), \
             patch.object(windows, 'arp_table', lambda: windows.parse_arp(ARP_OUTPUT)):
            table = discovery.arp_table()
        self.assertEqual(table['192.168.1.64'], '00:09:18:ab:cd:ef')
        self.assertNotIn('10.20.30.40', table)

    def test_an_off_subnet_camera_gets_the_ip_hash_device_key(self):
        record = self._probe('10.20.30.40')
        self.assertEqual(record['mac_address'], 'Unknown')
        self.assertTrue(record['device_key'].startswith('ip-'))
        self.assertTrue(record['manual_target'])

    def test_an_on_link_camera_gets_the_same_mac_key_as_on_linux(self):
        record = self._probe('192.168.1.64')
        self.assertEqual(record['device_key'], 'mac-' + hashlib.sha256(b'00:09:18:ab:cd:ef').hexdigest()[:32])

    def test_wsaeconnreset_on_the_unicast_probe_means_no_onvif_not_a_failure(self):
        with patch('anyaicam_agent.discovery.socket.socket', _ResetSocket):
            self.assertEqual(discovery._onvif_probe_host('10.20.30.40', timeout=.5), {})
        record = self._probe('10.20.30.40')                       # RTSP still answers -> the camera is found
        self.assertEqual((record['rtsp_port'], record['manufacturer'], record['onvif_support']), (554, 'Unknown', False))

    def test_no_onvif_and_no_rtsp_is_no_camera(self):
        self.assertIsNone(self._probe('10.20.30.40', open_ports=()))

    def test_a_reply_from_another_source_port_of_the_same_camera_is_accepted(self):
        record = self._probe('10.20.30.40', sock=lambda *a, **k: _ReplySocket(('10.20.30.40', 49152)))
        self.assertEqual(record['device_key'], 'urn:uuid:5f0c3b1e-1111-4222-8333-444455556666')
        self.assertEqual(record['manufacturer'], 'Hanwha')

    def test_a_reply_from_another_host_is_ignored(self):
        with patch('anyaicam_agent.discovery.socket.socket', lambda *a, **k: _ReplySocket(('10.20.30.41', 3702))):
            self.assertEqual(discovery._onvif_probe_host('10.20.30.40', timeout=.5), {})


if __name__ == '__main__':
    unittest.main()
