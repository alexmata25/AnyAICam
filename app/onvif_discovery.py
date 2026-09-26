"""Real ONVIF WS-Discovery for the customer-facing camera setup step
(Phase 3 of the AWS-authoritative onboarding rework).

Distinct from app/main.py's existing v110_scan()/v110_probe_host(), which
is an admin-only raw TCP port scan across an IP range with no device
identity at all -- it cannot produce a stable device_key and is not reused
here. This module speaks the actual ONVIF WS-Discovery protocol (a UDP
multicast Probe to 239.255.255.250:3702) so a rediscovered physical camera
resolves to the SAME device_key (its ONVIF endpoint reference UUID) every
time, which is what dedup during provisioning depends on.

Read-only and credential-free by design: no authentication is attempted,
no default passwords are tried, and only reachability (TCP connect) is
checked for RTSP/ONVIF-HTTP ports -- matching the safety properties
already established for the existing appliance-agent discovery.py and
main.py's v110 discovery endpoint (private-network-only, bounded host/port
counts, no credential probing).

Fully unit-testable without a real network: send_probe/tcp_probe are
injectable, and probe() accepts pre-built synthetic WS-Discovery XML
responses in tests instead of opening a real UDP socket.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import uuid as uuid_module
from typing import Callable, Optional

WS_DISCOVERY_ADDRESS = ("239.255.255.250", 3702)
WS_DISCOVERY_TIMEOUT_SECONDS = 3.0
TCP_CONNECT_TIMEOUT_SECONDS = 0.75
RTSP_PORTS = (554, 8554)
ONVIF_HTTP_PORTS = (80, 8000, 8080)

_PROBE_MESSAGE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    '<e:Header><w:MessageID>uuid:{message_id}</w:MessageID>'
    '<w:To e:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
    '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'
).format
_ENDPOINT_UUID_PATTERN = re.compile(r"uuid:([0-9a-fA-F-]{36})")
_XADDRS_PATTERN = re.compile(r"<d:XAddrs>(.*?)</d:XAddrs>", re.DOTALL)


def default_send_probe(timeout: float = WS_DISCOVERY_TIMEOUT_SECONDS) -> list[str]:
    """Sends one WS-Discovery Probe over UDP multicast and collects
    whatever responses arrive within `timeout` seconds. Returns raw XML
    response bodies -- parsing is a separate, independently testable step
    (see parse_probe_response)."""
    message = _PROBE_MESSAGE(message_id=str(uuid_module.uuid4()))
    responses: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(message.encode("utf-8"), WS_DISCOVERY_ADDRESS)
        while True:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            responses.append(data.decode("utf-8", errors="replace"))
    finally:
        sock.close()
    return responses


def default_tcp_probe(host: str, port: int, timeout: float = TCP_CONNECT_TIMEOUT_SECONDS) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_probe_response(xml_text: str) -> Optional[dict]:
    """Extracts a device_key (the ONVIF endpoint reference UUID -- stable
    across reboot/DHCP, unlike an IP address) and the device's ONVIF
    service address (XAddrs) from one WS-Discovery ProbeMatch response.
    Returns None for anything that doesn't look like a real ProbeMatch."""
    uuid_match = _ENDPOINT_UUID_PATTERN.search(xml_text)
    xaddrs_match = _XADDRS_PATTERN.search(xml_text)
    if not uuid_match or not xaddrs_match:
        return None
    xaddrs = xaddrs_match.group(1).strip().split()
    if not xaddrs:
        return None
    onvif_endpoint = xaddrs[0]
    try:
        host = onvif_endpoint.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    except IndexError:
        return None
    return {
        "device_key": uuid_match.group(1).lower(),
        "onvif_endpoint": onvif_endpoint,
        "ip_address": host,
    }


def probe(
    *,
    send_probe: Callable[[], list[str]] = default_send_probe,
    tcp_probe: Callable[[str, int, float], bool] = default_tcp_probe,
    allowed_network: Optional[str] = None,
) -> list[dict]:
    """Runs one discovery pass and returns a de-duplicated (by device_key)
    list of {device_key, ip_address, onvif_endpoint, rtsp_reachable,
    onvif_reachable}. No credentials are read, sent, or guessed -- see the
    module docstring."""
    network = ipaddress.ip_network(allowed_network, strict=False) if allowed_network else None
    seen: dict[str, dict] = {}
    for xml_text in send_probe():
        parsed = parse_probe_response(xml_text)
        if not parsed or parsed["device_key"] in seen:
            continue
        try:
            address = ipaddress.ip_address(parsed["ip_address"])
        except ValueError:
            continue
        if not address.is_private:
            continue  # never trust/return a response claiming a public IP
        if network and address not in network:
            continue
        rtsp_reachable = any(tcp_probe(parsed["ip_address"], port, TCP_CONNECT_TIMEOUT_SECONDS) for port in RTSP_PORTS)
        onvif_reachable = any(tcp_probe(parsed["ip_address"], port, TCP_CONNECT_TIMEOUT_SECONDS) for port in ONVIF_HTTP_PORTS)
        seen[parsed["device_key"]] = {
            "device_key": parsed["device_key"],
            "ip_address": parsed["ip_address"],
            "onvif_endpoint": parsed["onvif_endpoint"],
            "rtsp_reachable": rtsp_reachable,
            "onvif_reachable": onvif_reachable,
        }
    return list(seen.values())
