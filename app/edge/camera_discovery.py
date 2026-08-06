"""Bounded ONVIF and RTSP discovery that runs only on an edge appliance."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


WS_DISCOVERY_ADDRESS = ("239.255.255.250", 3702)
DEFAULT_PORTS = (80, 443, 554, 8000, 8080, 8443, 8554)
RTSP_PORTS = {554, 8554}
MAX_DISCOVERY_HOSTS = 256
ONVIF_DEVICE_ACTION = "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation"
ONVIF_PROFILES_ACTION = "http://www.onvif.org/ver10/media/wsdl/GetProfiles"
ONVIF_STREAM_ACTION = "http://www.onvif.org/ver10/media/wsdl/GetStreamUri"


@dataclass(frozen=True)
class DiscoveryOptions:
    network: str
    ports: tuple[int, ...] = DEFAULT_PORTS
    connect_timeout: float = 0.35
    onvif_timeout: float = 1.5
    max_hosts: int = MAX_DISCOVERY_HOSTS


def private_network(value: str, max_hosts: int = MAX_DISCOVERY_HOSTS):
    network = ipaddress.ip_network(value.strip(), strict=False)
    allowed = (
        any(network.subnet_of(block) for block in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ))
        if network.version == 4 else network.subnet_of(ipaddress.ip_network("fc00::/7"))
    )
    if not allowed:
        raise ValueError("Camera discovery is limited to private networks.")
    if network.num_addresses > max_hosts:
        raise ValueError(f"Camera discovery is limited to {max_hosts} addresses.")
    return network


def _text_by_local_name(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name and element.text:
            return element.text.strip()
    return ""


def parse_ws_discovery(payload: bytes) -> dict | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    xaddrs = _text_by_local_name(root, "XAddrs").split()
    scopes = _text_by_local_name(root, "Scopes").split()
    address = _text_by_local_name(root, "Address")
    if not xaddrs and not address:
        return None
    return {"device_uuid": address, "xaddrs": xaddrs, "scopes": scopes}


def scope_value(scopes: list[str], key: str) -> str:
    marker = f"onvif://www.onvif.org/{key}/"
    for scope in scopes:
        if scope.lower().startswith(marker.lower()):
            return unquote(scope[len(marker):]).replace("_", " ")
    return ""


VENDORS = {
    "axis": "Axis",
    "dahua": "Dahua",
    "hikvision": "Hikvision",
    "hanwha": "Hanwha",
    "reolink": "Reolink",
    "uniview": "Uniview",
    "ubiquiti": "Ubiquiti",
    "bosch": "Bosch",
    "sony": "Sony",
    "vivotek": "Vivotek",
}


def manufacturer_hint(scopes: list[str], model: str = "") -> str:
    text = " ".join(scopes + [model]).lower()
    return next((label for needle, label in VENDORS.items() if needle in text), "Unknown")


def _safe_rtsp_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(("rtsp", f"{host}{port}", parsed.path, parsed.query, ""))


def _safe_onvif_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme.lower(), f"{host}{port}", parsed.path, parsed.query, ""))


def candidate_stream_urls(host: str, manufacturer: str, port: int = 554) -> list[str]:
    paths = {
        "Hikvision": ["/Streaming/Channels/101", "/Streaming/Channels/102"],
        "Dahua": ["/cam/realmonitor?channel=1&subtype=0"],
        "Axis": ["/axis-media/media.amp"],
        "Reolink": ["/h264Preview_01_main"],
        "Ubiquiti": ["/s0"],
    }.get(manufacturer, ["/live", "/stream1"])
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return [f"rtsp://{url_host}:{port}{path}" for path in paths]


def ws_discover(timeout: float) -> list[dict]:
    message_id = f"uuid:{uuid.uuid4()}"
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
    <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
      <e:Header><w:MessageID>{message_id}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
      <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
    </e:Envelope>""".encode("utf-8")
    devices: dict[str, dict] = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.settimeout(min(timeout, 0.25))
            sock.sendto(payload, WS_DISCOVERY_ADDRESS)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    response, _ = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                parsed = parse_ws_discovery(response)
                if parsed:
                    key = parsed.get("device_uuid") or " ".join(parsed.get("xaddrs", []))
                    devices[key] = parsed
    except OSError:
        return []
    return list(devices.values())


def open_ports(host: str, ports: tuple[int, ...], timeout: float) -> list[int]:
    found = []
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                found.append(port)
        except OSError:
            continue
    return found


def _soap_request(url: str, action: str, body: str, timeout: float) -> bytes:
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
    <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>{body}</s:Body></s:Envelope>""".encode("utf-8")
    request = Request(
        url,
        data=envelope,
        method="POST",
        headers={
            "Content-Type": f'application/soap+xml; charset="utf-8"; action="{action}"',
            "User-Agent": "AnyAiCam-Edge-Discovery/0.9",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read(1024 * 1024)


def onvif_metadata(xaddr: str, timeout: float) -> dict:
    result = {"manufacturer": "", "model": "", "stream_urls": []}
    try:
        information = _soap_request(
            xaddr,
            ONVIF_DEVICE_ACTION,
            '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
            timeout,
        )
        root = ET.fromstring(information)
        result["manufacturer"] = _text_by_local_name(root, "Manufacturer")
        result["model"] = _text_by_local_name(root, "Model")
    except (OSError, ET.ParseError, ValueError):
        pass

    try:
        profiles_payload = _soap_request(
            xaddr,
            ONVIF_PROFILES_ACTION,
            '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>',
            timeout,
        )
        profiles_root = ET.fromstring(profiles_payload)
        tokens = [
            element.attrib.get("token", "")
            for element in profiles_root.iter()
            if element.tag.rsplit("}", 1)[-1] == "Profiles"
        ]
        for token in [token for token in tokens if token][:4]:
            stream_payload = _soap_request(
                xaddr,
                ONVIF_STREAM_ACTION,
                f'<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl"><StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream><Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup><ProfileToken>{token}</ProfileToken></GetStreamUri>',
                timeout,
            )
            stream_root = ET.fromstring(stream_payload)
            stream_url = _safe_rtsp_url(_text_by_local_name(stream_root, "Uri"))
            if stream_url and stream_url not in result["stream_urls"]:
                result["stream_urls"].append(stream_url)
    except (OSError, ET.ParseError, ValueError):
        pass
    return result


class CameraDiscoveryService:
    def __init__(
        self,
        ws_probe=ws_discover,
        port_probe=open_ports,
        metadata_probe=onvif_metadata,
    ):
        self.ws_probe = ws_probe
        self.port_probe = port_probe
        self.metadata_probe = metadata_probe

    def scan(self, options: DiscoveryOptions) -> list[dict]:
        network = private_network(options.network, options.max_hosts)
        ports = tuple(sorted({port for port in options.ports if 1 <= int(port) <= 65535}))
        if not ports:
            raise ValueError("At least one valid discovery port is required.")

        onvif_by_host: dict[str, dict] = {}
        for device in self.ws_probe(options.onvif_timeout):
            for xaddr in device.get("xaddrs", []):
                try:
                    host = urlsplit(xaddr).hostname
                    address = ipaddress.ip_address(host or "")
                except ValueError:
                    continue
                if address in network:
                    onvif_by_host[str(address)] = device

        hosts = [str(host) for host in network.hosts()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(hosts) or 1)) as pool:
            port_results = dict(
                zip(
                    hosts,
                    pool.map(
                        lambda host: self.port_probe(host, ports, options.connect_timeout),
                        hosts,
                    ),
                )
            )

        cameras = []
        for host in hosts:
            device = onvif_by_host.get(host)
            detected_ports = port_results.get(host, [])
            rtsp_ports = [port for port in detected_ports if port in RTSP_PORTS]
            if not device and not rtsp_ports:
                continue
            scopes = device.get("scopes", []) if device else []
            xaddrs = [
                safe_url
                for url in (device.get("xaddrs", []) if device else [])
                if (safe_url := _safe_onvif_url(url))
            ]
            metadata = self.metadata_probe(xaddrs[0], options.onvif_timeout) if xaddrs else {}
            model = metadata.get("model") or scope_value(scopes, "hardware") or "Unknown"
            manufacturer = metadata.get("manufacturer") or manufacturer_hint(scopes, model)
            onvif_stream_urls = [
                safe_url
                for url in (metadata.get("stream_urls") or [])
                if (safe_url := _safe_rtsp_url(url))
            ]
            stream_urls = list(onvif_stream_urls)
            if rtsp_ports and not stream_urls:
                stream_urls = candidate_stream_urls(host, manufacturer, rtsp_ports[0])
            identity = (
                (device or {}).get("device_uuid")
                or (xaddrs[0] if xaddrs else "")
                or f"camera://{host}:{rtsp_ports[0] if rtsp_ports else 0}"
            )
            cameras.append(
                {
                    "id": uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
                    "name": scope_value(scopes, "name") or f"Camera {host}",
                    "ip_address": host,
                    "manufacturer": manufacturer or "Unknown",
                    "model": model or "Unknown",
                    "onvif": bool(device),
                    "rtsp": bool(rtsp_ports or stream_urls),
                    "onvif_xaddrs": xaddrs,
                    "open_ports": detected_ports,
                    "stream_urls": stream_urls,
                    "stream_url_source": (
                        "onvif" if onvif_stream_urls
                        else "candidate" if stream_urls else "none"
                    ),
                    "stream_urls_verified": bool(onvif_stream_urls),
                }
            )
        return cameras
