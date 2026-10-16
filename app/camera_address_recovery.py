"""Automatic camera address recovery (VMS 1.2.7).

A camera's stream address (cameras.onvif_endpoint) is a fixed IP. When DHCP
gives the camera a new address -- after a power outage, a router restart, a
lease expiry -- the VMS kept connecting to the old one forever (Ryzen,
2026-10-12: Driveway Right moved .44 -> .43 and Driveway Left .70 -> .69 while
the VMS retried the old addresses for days). This module finds such a camera
again by its stored ONVIF identity (cameras.device_key, the WS-Discovery
EndpointReference UUID) and reconnects it without customer intervention.

Where it runs: inside the VMS process, edge/combined runtime only. The VMS
container sits on a Docker bridge, so it cannot use LAN multicast, but unicast
UDP reaches the LAN: a directed WS-Discovery Probe to <ip>:3702 and a legacy
unicast mDNS query to <ip>:5353 both answer in milliseconds (verified on the
Ryzen's LT Security cameras, 2026-10-15). Nothing here needs the cloud, so it
keeps working without internet.

Identity rules (the order is the safety argument):
  1. Only a device answering with this camera's exact ONVIF UUID is ever a
     candidate. An IP address alone never identifies a camera: a different
     device answering at the old address puts the camera on HOLD (no stream,
     no credentials sent there), it never "reconnects" to it.
  2. Two addresses answering with the same UUID is a refusal, not a choice.
  3. Secondary identity learned while the camera was healthy (its mDNS
     hostname -- model plus serial on LTS/Hikvision -- and its ONVIF hardware
     scope) must not contradict the candidate. A contradiction is a refusal.
  4. Only then are the stored credentials used: a short ffprobe of the same
     stream path on the candidate must return video before anything changes.

What changes: nothing in the cameras table. A recovered address is written to
camera_address_overrides together with the cloud endpoint it replaces. The
camera's credentials, recording settings, stream path, port and identity are
never touched; only the host part of the stream URL differs. The override
applies for as long as the cloud still holds the endpoint it was recovered
from, so the periodic cloud->edge sync (edge_camera_sync.py, which rewrites
onvif_endpoint every minute with the cloud's value) cannot undo it. A
DIFFERENT cloud endpoint is an intentional administrator change and wins: the
override is dropped and the camera uses the new cloud value.

Customer notification only when automatic recovery fails: a camera still not
found after NOTIFY_AFTER_SECONDS, or any identity conflict (duplicate UUID,
contradicting secondary identity). One alert per incident.

Every decision is recorded in camera_address_recovery_events (never a
credential; addresses are LAN-local data that stays on the appliance).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import random
import secrets
import select
import socket
import struct
import subprocess
import threading
import time
import uuid as uuid_module
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
ENABLED = os.environ.get("ANYAICAM_CAMERA_ADDRESS_RECOVERY", "true").strip().lower() not in {"0", "false", "no", "off"}
CHECK_INTERVAL_SECONDS = max(5, int(os.environ.get("ANYAICAM_CAMERA_RECOVERY_CHECK_SECONDS", "15")))
# Consecutive unanswered identity checks (at CHECK_INTERVAL_SECONDS) before a
# camera that is also not streaming is searched for.
MISSES_BEFORE_SEARCH = 2
PROBE_TIMEOUT_SECONDS = 1.0
GATE_PROBE_TIMEOUT_SECONDS = 0.5
SWEEP_TIMEOUT_SECONDS = 2.5
VERIFY_TIMEOUT_SECONDS = 12
# An identity check this recent lets a stream start without probing again.
CHECK_FRESH_SECONDS = 60
BACKOFF_SECONDS = (30, 60, 120, 300, 900)
NOTIFY_AFTER_SECONDS = max(60, int(os.environ.get("ANYAICAM_CAMERA_RECOVERY_NOTIFY_SECONDS", "900")))
MAX_SWEEP_HOSTS = 1024
WS_DISCOVERY_PORT = 3702
MDNS_PORT = 5353


class CameraIdentityHoldError(OSError):
    """Raised instead of returning a stream address when a different device
    answers at this camera's address. An OSError so every existing
    camera_url() caller (the process supervisor's start path, snapshot and
    probe helpers) already treats it as "camera not reachable right now"."""


# --------------------------------------------------------------------------
# Address helpers
# --------------------------------------------------------------------------

def host_of(endpoint: str | None) -> str | None:
    try:
        return urlsplit(endpoint or "").hostname
    except ValueError:
        return None


def replace_host(endpoint: str, new_host: str) -> str:
    """The same stream URL with only the host changed: scheme, port, path and
    query are kept exactly. Stored endpoints never carry credentials (the
    cloud strips them, appliance_cloud._sanitize_rtsp_uri); any userinfo is
    dropped all the same."""
    parsed = urlsplit(endpoint)
    netloc = new_host if not parsed.port else f"{new_host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def credentialed_url(endpoint: str, username: str, password: str) -> str:
    # Same construction as main.credentialed_rtsp_url() (duplicated so this
    # module never imports main). For a local ffprobe only, never logged.
    parsed = urlsplit(endpoint)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    if username or password:
        userinfo = quote(username, safe="") + (":" + quote(password, safe="") if password else "")
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((parsed.scheme or "rtsp", netloc, parsed.path, parsed.query, parsed.fragment))


def normalize_device_key(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text.startswith("urn:uuid:"):
        return None
    try:
        identity = uuid_module.UUID(text[9:])
    except ValueError:
        return None
    if identity.int == 0:
        return None
    return "urn:uuid:" + str(identity)


# --------------------------------------------------------------------------
# Unicast WS-Discovery and mDNS
# --------------------------------------------------------------------------

_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    '<e:Header><w:MessageID>uuid:{message_id}</w:MessageID>'
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
    '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'
)


@dataclass(frozen=True)
class Sighting:
    """One device's answer to a directed ONVIF Probe."""
    ip: str
    device_key: str
    hardware: str | None = None
    xaddr_hosts: tuple[str, ...] = ()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_probe_match(ip: str, payload: bytes | str) -> Optional[Sighting]:
    """A ProbeMatch's EndpointReference UUID only -- never the MessageID or
    RelatesTo UUIDs, which identify the exchange, not the device."""
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    for match in root.iter():
        if _local(match.tag) != "probematch":
            continue
        device_key = None
        xaddrs: tuple[str, ...] = ()
        hardware = None
        for node in match.iter():
            name = _local(node.tag)
            if name == "endpointreference":
                for child in node.iter():
                    if _local(child.tag) == "address" and device_key is None:
                        device_key = normalize_device_key(child.text)
            elif name == "xaddrs":
                hosts = []
                for item in (node.text or "").split():
                    host = host_of(item)
                    if host:
                        hosts.append(host.lower())
                xaddrs = tuple(hosts)
            elif name == "scopes":
                for scope in (node.text or "").split():
                    if scope.lower().startswith("onvif://www.onvif.org/hardware/"):
                        hardware = scope.split("/hardware/", 1)[1].replace("%20", " ").strip() or None
        if device_key:
            return Sighting(ip=ip, device_key=device_key, hardware=hardware, xaddr_hosts=xaddrs)
    return None


def _probe_bytes() -> bytes:
    return _PROBE.format(message_id=uuid_module.uuid4()).encode("utf-8")


def udp_probe_many(ips: Iterable[str], timeout: float) -> list[tuple[str, bytes]]:
    """Directed WS-Discovery Probes to every address from one UDP socket, then
    every reply that arrives within `timeout`. Unicast only: no multicast, no
    credentials, no TCP."""
    targets = list(dict.fromkeys(ips))
    replies: list[tuple[str, bytes]] = []
    if not targets:
        return replies
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        message = _probe_bytes()
        for ip in targets:
            try:
                sock.sendto(message, (ip, WS_DISCOVERY_PORT))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                break
            try:
                data, address = sock.recvfrom(65535)
            except OSError:
                continue
            replies.append((address[0], data))
    finally:
        sock.close()
    return replies


def mdns_reverse_name(ip: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> str | None:
    """Legacy unicast mDNS PTR query for <ip>'s reverse name, sent to the
    device itself. LT Security / Hikvision cameras answer with their own
    hostname (model + serial, e.g. CMIP1042W-28MA20250929AAWRGG6611454.local)
    -- a secondary identity readable before any credential is used."""
    try:
        reverse = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
        question = b"".join(bytes([len(part)]) + part.encode("ascii") for part in reverse.split(".")) + b"\x00"
    except (UnicodeEncodeError, ValueError):
        return None
    query_id = random.randint(1, 0xFFFF)
    packet = struct.pack(">HHHHHH", query_id, 0, 1, 0, 0, 0) + question + struct.pack(">HH", 12, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ip, MDNS_PORT))
        data, address = sock.recvfrom(9000)
    except OSError:
        return None
    finally:
        sock.close()
    if address[0] != ip:
        return None
    return parse_ptr_answer(data)


def _read_name(data: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    labels = []
    while True:
        if offset >= len(data) or depth > 10:
            raise ValueError("bad name")
        length = data[offset]
        if length == 0:
            return ".".join(labels), offset + 1
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack(">H", data[offset:offset + 2])[0] & 0x3FFF
            name, _ = _read_name(data, pointer, depth + 1)
            labels.append(name)
            return ".".join(labels), offset + 2
        labels.append(data[offset + 1:offset + 1 + length].decode("utf-8", errors="replace"))
        offset += 1 + length


def parse_ptr_answer(data: bytes) -> str | None:
    """The first PTR answer's target name, lower-cased, without the trailing
    '.local'. None for anything that is not a well-formed DNS response."""
    try:
        if len(data) < 12:
            return None
        flags, questions, answers = struct.unpack(">HHH", data[2:8])
        if not flags & 0x8000 or answers == 0:
            return None
        offset = 12
        for _ in range(questions):
            _, offset = _read_name(data, offset)
            offset += 4
        for _ in range(answers):
            _, offset = _read_name(data, offset)
            record_type, _cls, _ttl, length = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            if record_type == 12:
                target, _ = _read_name(data, offset)
                target = target.strip(".").lower()
                if target.endswith(".local"):
                    target = target[: -len(".local")]
                return target or None
            offset += length
    except (ValueError, struct.error):
        return None
    return None


def default_probe(ip: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> Optional[Sighting]:
    for source, data in udp_probe_many([ip], timeout):
        if source == ip:
            sighting = parse_probe_match(source, data)
            if sighting:
                return sighting
    return None


def default_sweep(ips: list[str], timeout: float = SWEEP_TIMEOUT_SECONDS) -> list[Sighting]:
    sightings = []
    for source, data in udp_probe_many(ips, timeout):
        sighting = parse_probe_match(source, data)
        if sighting:
            sightings.append(sighting)
    return sightings


def default_verify_stream(url: str, timeout: float = VERIFY_TIMEOUT_SECONDS) -> tuple[bool, dict]:
    """The credentialed check: ffprobe the stream and require a video stream.
    `url` carries credentials, exactly as the recording ffmpeg's own argv
    does; it is never logged or returned."""
    command = [
        "ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(int(timeout * 1_000_000)),
        "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "json", url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 3, check=False)
    except (OSError, subprocess.SubprocessError):
        return False, {"reason": "ffprobe_unavailable_or_timed_out"}
    if result.returncode != 0:
        return False, {"reason": "stream_not_opened"}
    try:
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except json.JSONDecodeError:
        return False, {"reason": "unreadable_probe_output"}
    if not streams:
        return False, {"reason": "no_video_stream"}
    stream = streams[0]
    return True, {"codec": stream.get("codec_name"), "width": stream.get("width"), "height": stream.get("height")}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS camera_address_overrides(
        camera_id TEXT PRIMARY KEY,
        device_key TEXT NOT NULL,
        cloud_endpoint TEXT NOT NULL,
        local_endpoint TEXT NOT NULL,
        previous_host TEXT,
        new_host TEXT NOT NULL,
        verified_by TEXT,
        recovered_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS camera_identity_evidence(
        camera_id TEXT PRIMARY KEY,
        device_key TEXT NOT NULL,
        mdns_name TEXT,
        onvif_hardware TEXT,
        learned_host TEXT,
        learned_at TEXT,
        last_verified_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS camera_address_recovery_events(
        id TEXT PRIMARY KEY,
        camera_id TEXT,
        camera_number INTEGER,
        event TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_camera_address_recovery_events_camera ON camera_address_recovery_events(camera_id, created_at)",
)


def ensure_schema(db) -> None:
    for statement in _SCHEMA:
        db.execute(statement)


def _connection():
    from partner_db import connection
    return connection()


def record_event(db, camera_id: str | None, camera_number, event: str, detail: dict | None = None) -> None:
    db.execute(
        "INSERT INTO camera_address_recovery_events(id,camera_id,camera_number,event,detail,created_at) VALUES(?,?,?,?,?,?)",
        (secrets.token_hex(8), camera_id, camera_number, event, json.dumps(detail or {}, sort_keys=True), datetime.now().isoformat()),
    )
    print(f"camera_address_recovery event={event} camera={camera_number} detail={json.dumps(detail or {}, sort_keys=True)}")


def _override(db, camera_id: str):
    try:
        return db.execute(
            "SELECT camera_id,device_key,cloud_endpoint,local_endpoint,previous_host,new_host FROM camera_address_overrides WHERE camera_id=?",
            (camera_id,),
        ).fetchone()
    except Exception:
        return None  # table not created yet: no override can exist


def resolve_endpoint(db, camera_id: str, device_key: str | None, cloud_endpoint: str, camera_number=None) -> str:
    """The endpoint to stream from: the recovered local address while the
    cloud still holds the endpoint it was recovered from, else the cloud's.
    Drops an override the cloud has superseded (an intentional change)."""
    override = _override(db, camera_id)
    if not override:
        return cloud_endpoint
    same_identity = normalize_device_key(override["device_key"]) == normalize_device_key(device_key)
    if same_identity and override["cloud_endpoint"] == cloud_endpoint:
        return override["local_endpoint"]
    db.execute("DELETE FROM camera_address_overrides WHERE camera_id=?", (camera_id,))
    record_event(db, camera_id, camera_number, "override_superseded", {
        "reason": "identity_changed" if not same_identity else "cloud_endpoint_changed",
        "recovered_host": override["new_host"], "cloud_host": host_of(cloud_endpoint),
    })
    return cloud_endpoint


# --------------------------------------------------------------------------
# Identity gate (consulted on every stream start)
# --------------------------------------------------------------------------

@dataclass
class _Check:
    host: str
    outcome: str  # "match" | "mismatch" | "no_reply"
    at: float
    other_device_key: str | None = None
    hardware: str | None = None


_checks: dict[str, _Check] = {}
_holds: dict[str, _Check] = {}
_lock = threading.Lock()


def _note_check(camera_id: str, check: _Check) -> None:
    with _lock:
        _checks[camera_id] = check
        if check.outcome == "mismatch":
            _holds[camera_id] = check
        elif check.outcome == "match" or (camera_id in _holds and _holds[camera_id].host != check.host):
            _holds.pop(camera_id, None)


def held(camera_id: str, host: str | None) -> _Check | None:
    with _lock:
        hold = _holds.get(camera_id)
    return hold if hold and hold.host == host else None


def reset_state() -> None:
    """For tests: forget every in-memory check and hold."""
    with _lock:
        _checks.clear()
        _holds.clear()
    _runtime.clear()


def identity_check(camera_id: str, device_key: str, host: str, probe=None, timeout=PROBE_TIMEOUT_SECONDS, clock=time.monotonic) -> _Check:
    sighting = (probe or default_probe)(host, timeout)
    expected = normalize_device_key(device_key)
    if sighting is None:
        check = _Check(host, "no_reply", clock())
    elif sighting.device_key == expected:
        check = _Check(host, "match", clock(), hardware=sighting.hardware)
    else:
        check = _Check(host, "mismatch", clock(), sighting.device_key)
    _note_check(camera_id, check)
    return check


def stream_endpoint(camera_id: str, device_key: str | None, cloud_endpoint: str, camera_number=None, *, db=None,
                    probe=None, clock=time.monotonic) -> str:
    """Called by the VMS wherever it is about to use a provisioned camera's
    address with that camera's credentials (main._provisioned_camera_stream
    and the talk modules). Returns the address to use, or raises
    CameraIdentityHoldError when a different device answers there.

    Uses the background worker's latest check when it is fresh; otherwise
    (first start after boot, or the worker not running) one directed probe of
    at most GATE_PROBE_TIMEOUT_SECONDS. A silent address is allowed through
    unchanged -- that is today's behavior for cameras without WS-Discovery,
    and the worker keeps watching it."""
    if not ENABLED or not normalize_device_key(device_key):
        # Not an ONVIF identity (agent fallback keys are mac-/ip- hashes):
        # nothing to verify against, so today's behavior is unchanged.
        return cloud_endpoint
    try:
        if db is not None:
            endpoint = resolve_endpoint(db, camera_id, device_key, cloud_endpoint, camera_number)
        else:
            with _connection() as connection_db:
                endpoint = resolve_endpoint(connection_db, camera_id, device_key, cloud_endpoint, camera_number)
    except CameraIdentityHoldError:
        raise
    except Exception:
        endpoint = cloud_endpoint
    host = host_of(endpoint)
    if not host:
        return endpoint
    with _lock:
        check = _checks.get(camera_id)
    if check is None or check.host != host or clock() - check.at > CHECK_FRESH_SECONDS:
        try:
            check = identity_check(camera_id, device_key, host, probe=probe, timeout=GATE_PROBE_TIMEOUT_SECONDS, clock=clock)
        except Exception:
            return endpoint
    if held(camera_id, host):
        raise CameraIdentityHoldError(
            f"A different device now answers at camera {camera_number or ''} address {host}; "
            "the camera is not connected until it is found again."
        )
    return endpoint


# --------------------------------------------------------------------------
# Recovery worker
# --------------------------------------------------------------------------

@dataclass
class _CameraRuntime:
    misses: int = 0
    failing_since: float | None = None
    next_search_at: float = 0.0
    attempts: int = 0
    notified: bool = False
    last_event: str | None = None


_runtime: dict[str, _CameraRuntime] = {}


@dataclass
class Dependencies:
    """Everything the worker touches outside this module, injectable so tests
    run without a network, ffprobe or the monolith."""
    probe: Callable[[str, float], Optional[Sighting]] = default_probe
    sweep: Callable[[list[str], float], list[Sighting]] = default_sweep
    mdns_name: Callable[[str, float], Optional[str]] = mdns_reverse_name
    verify_stream: Callable[[str, float], tuple[bool, dict]] = default_verify_stream
    credentials: Callable[[object, str], Optional[dict]] | None = None
    is_streaming: Callable[[int], bool] = lambda camera_number: False
    processes: Callable[[], list] = lambda: []
    alert: Callable[[dict], None] | None = None
    networks: Callable[[str], list] | None = None
    clock: Callable[[], float] = time.monotonic


def _stored_credentials(db, camera_id: str) -> Optional[dict]:
    from appliance_protocol import decrypt_camera_credentials
    row = db.execute("SELECT encrypted_blob FROM camera_credentials WHERE camera_id=?", (camera_id,)).fetchone()
    if not row:
        return {"username": "", "password": ""}  # a camera provisioned without credentials
    return decrypt_camera_credentials(row["encrypted_blob"])


def configured_networks() -> list:
    raw = os.environ.get("ANYAICAM_CAMERA_RECOVERY_NETWORKS", "")
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if network.version == 4 and network.is_private:
            networks.append(network)
    return networks


def search_networks(host: str) -> list:
    """Where to look for a moved camera: ANYAICAM_CAMERA_RECOVERY_NETWORKS when
    set, else the /24 its last known address belongs to (DHCP pools hand out
    addresses on the same LAN)."""
    configured = configured_networks()
    if configured:
        return configured
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return []
    if address.version != 4 or not address.is_private:
        return []
    return [ipaddress.ip_network(f"{host}/24", strict=False)]


def _in_networks(ip: str, networks: list) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_private and any(address in network for network in networks)


def provisioned_cameras(db) -> list[dict]:
    rows = db.execute(
        "SELECT id,camera_number,name,device_key,onvif_endpoint FROM cameras "
        "WHERE camera_number IS NOT NULL AND device_key IS NOT NULL AND onvif_endpoint IS NOT NULL "
        "AND COALESCE(status,'') <> 'removed'"
    ).fetchall()
    cameras = []
    for row in rows:
        if not str(row["onvif_endpoint"] or "").startswith("rtsp://") or not normalize_device_key(row["device_key"]):
            continue
        cameras.append(dict(row))
    return cameras


def _evidence(db, camera_id: str):
    return db.execute(
        "SELECT camera_id,device_key,mdns_name,onvif_hardware,learned_host FROM camera_identity_evidence WHERE camera_id=?",
        (camera_id,),
    ).fetchone()


def _learn_evidence(db, camera: dict, host: str, sighting_hardware: str | None, deps: Dependencies) -> None:
    """Trust on first use, only while this camera's own UUID answers at its
    own configured address. Evidence learned for a different identity (the
    camera row was re-provisioned) is replaced, never mixed."""
    now = datetime.now().isoformat()
    existing = _evidence(db, camera["id"])
    key = normalize_device_key(camera["device_key"])
    if existing and normalize_device_key(existing["device_key"]) == key and existing["mdns_name"] and existing["onvif_hardware"]:
        db.execute("UPDATE camera_identity_evidence SET last_verified_at=? WHERE camera_id=?", (now, camera["id"]))
        return
    name = deps.mdns_name(host, PROBE_TIMEOUT_SECONDS)
    same = existing is not None and normalize_device_key(existing["device_key"]) == key
    mdns_name = (existing["mdns_name"] if same and existing["mdns_name"] else None) or name
    hardware = (existing["onvif_hardware"] if same and existing["onvif_hardware"] else None) or sighting_hardware
    db.execute(
        "INSERT INTO camera_identity_evidence(camera_id,device_key,mdns_name,onvif_hardware,learned_host,learned_at,last_verified_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET device_key=excluded.device_key,mdns_name=excluded.mdns_name,"
        "onvif_hardware=excluded.onvif_hardware,learned_host=excluded.learned_host,learned_at=excluded.learned_at,"
        "last_verified_at=excluded.last_verified_at",
        (camera["id"], key, mdns_name, hardware, host, now, now),
    )
    if not same:
        record_event(db, camera["id"], camera["camera_number"], "identity_evidence_learned", {
            "host": host, "mdns_name": mdns_name, "onvif_hardware": hardware,
        })


def _terminate_streams(camera_number, host: str, endpoint: str, deps: Dependencies) -> int:
    """Stops this camera's running ffmpeg processes that are connected to
    `host` on this camera's stream path. The supervisor restarts them through
    camera_url(), which applies the hold or the recovered address."""
    path = urlsplit(endpoint).path
    stopped = 0
    for process in list(deps.processes()):
        try:
            if process.poll() is not None:
                continue
            args = process.args if isinstance(process.args, (list, tuple)) else [process.args]
            for arg in args:
                text = str(arg)
                if not text.startswith("rtsp://"):
                    continue
                parsed = urlsplit(text)
                if parsed.hostname == host and parsed.path == path:
                    process.terminate()
                    stopped += 1
                    break
        except Exception:
            continue
    return stopped


def _event_once(db, state: _CameraRuntime, camera: dict, event: str, detail: dict) -> None:
    if state.last_event != event:
        record_event(db, camera["id"], camera["camera_number"], event, detail)
        state.last_event = event


def _notify(db, state: _CameraRuntime, camera: dict, deps: Dependencies, reason: str, message: str) -> None:
    if state.notified:
        return
    state.notified = True
    record_event(db, camera["id"], camera["camera_number"], "customer_notified", {"reason": reason})
    if deps.alert is None:
        return
    try:
        deps.alert({
            "id": uuid_module.uuid4().hex, "event_id": None, "camera": camera["camera_number"], "site": "home",
            "event_type": "camera_recovery_failed", "timestamp": datetime.now().isoformat(),
            "message": message, "read": False,
        })
    except Exception:
        pass


def _schedule_backoff(state: _CameraRuntime, now: float) -> None:
    delay = BACKOFF_SECONDS[min(state.attempts, len(BACKOFF_SECONDS) - 1)]
    state.attempts += 1
    state.next_search_at = now + delay


def _camera_label(camera: dict) -> str:
    name = (camera.get("name") or "").strip()
    return f"Camera {camera['camera_number']}" + (f" ({name})" if name else "")


def _attempt_recovery(db, camera: dict, endpoint: str, host: str, state: _CameraRuntime, deps: Dependencies, now: float) -> str:
    """One search for this camera. Returns the outcome name."""
    key = normalize_device_key(camera["device_key"])
    networks = deps.networks(host) if deps.networks else search_networks(host)
    if not networks:
        _event_once(db, state, camera, "search_skipped", {"reason": "no_private_network_for_host", "host": host})
        _schedule_backoff(state, now)
        return "skipped"
    addresses = []
    for network in networks:
        for address in network.hosts():
            addresses.append(str(address))
            if len(addresses) >= MAX_SWEEP_HOSTS:
                break
    sightings = deps.sweep(addresses, SWEEP_TIMEOUT_SECONDS)
    matches = {}
    for sighting in sightings:
        if sighting.device_key != key or not _in_networks(sighting.ip, networks):
            continue
        # A device naming a different address for itself than the one it
        # answered from is not trusted as this camera's new address.
        if sighting.xaddr_hosts and sighting.ip.lower() not in sighting.xaddr_hosts:
            continue
        matches[sighting.ip] = sighting
    if not matches:
        _event_once(db, state, camera, "not_found", {"previous_host": host, "networks": [str(n) for n in networks]})
        _schedule_backoff(state, now)
        return "not_found"
    if len(matches) > 1:
        record_event(db, camera["id"], camera["camera_number"], "duplicate_identity", {"hosts": sorted(matches)})
        state.last_event = "duplicate_identity"
        _notify(db, state, camera, deps, "duplicate_identity",
                f"{_camera_label(camera)} could not be reconnected automatically: more than one device on the network "
                "reports its identity. Check the camera's network settings.")
        _schedule_backoff(state, now)
        return "duplicate_identity"
    candidate_ip, sighting = next(iter(matches.items()))
    if candidate_ip == host:
        return "present_at_configured_host"
    evidence = _evidence(db, camera["id"])
    secondary = []
    if evidence is not None and normalize_device_key(evidence["device_key"]) == key:
        if evidence["mdns_name"]:
            name = deps.mdns_name(candidate_ip, PROBE_TIMEOUT_SECONDS)
            if name is not None and name != evidence["mdns_name"]:
                return _refuse(db, camera, state, deps, now, "secondary_identity_mismatch",
                               {"candidate_host": candidate_ip, "check": "mdns_name"})
            if name == evidence["mdns_name"]:
                secondary.append("mdns_name")
        if evidence["onvif_hardware"] and sighting.hardware:
            if sighting.hardware != evidence["onvif_hardware"]:
                return _refuse(db, camera, state, deps, now, "secondary_identity_mismatch",
                               {"candidate_host": candidate_ip, "check": "onvif_hardware"})
            secondary.append("onvif_hardware")
    new_endpoint = replace_host(endpoint, candidate_ip)
    credentials = deps.credentials(db, camera["id"]) if deps.credentials else _stored_credentials(db, camera["id"])
    if credentials is None:
        _event_once(db, state, camera, "verification_skipped", {"reason": "stored_credentials_unreadable"})
        _schedule_backoff(state, now)
        return "credentials_unreadable"
    ok, info = deps.verify_stream(
        credentialed_url(new_endpoint, credentials.get("username", ""), credentials.get("password", "")), VERIFY_TIMEOUT_SECONDS
    )
    if not ok:
        record_event(db, camera["id"], camera["camera_number"], "verification_failed",
                     {"candidate_host": candidate_ip, **{k: v for k, v in info.items() if k == "reason"}})
        state.last_event = "verification_failed"
        _schedule_backoff(state, now)
        return "verification_failed"
    ensure_schema(db)
    db.execute(
        "INSERT INTO camera_address_overrides(camera_id,device_key,cloud_endpoint,local_endpoint,previous_host,new_host,verified_by,recovered_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET device_key=excluded.device_key,cloud_endpoint=excluded.cloud_endpoint,"
        "local_endpoint=excluded.local_endpoint,previous_host=excluded.previous_host,new_host=excluded.new_host,"
        "verified_by=excluded.verified_by,recovered_at=excluded.recovered_at",
        (camera["id"], key, camera["onvif_endpoint"], new_endpoint, host, candidate_ip,
         ",".join(["onvif_uuid", *secondary, "rtsp_stream"]), datetime.now().isoformat()),
    )
    record_event(db, camera["id"], camera["camera_number"], "recovered", {
        "previous_host": host, "new_host": candidate_ip, "verified_by": ["onvif_uuid", *secondary, "rtsp_stream"],
        "video": {k: info.get(k) for k in ("codec", "width", "height")},
    })
    _note_check(camera["id"], _Check(candidate_ip, "match", deps.clock()))
    stopped = _terminate_streams(camera["camera_number"], host, endpoint, deps)
    if stopped:
        record_event(db, camera["id"], camera["camera_number"], "stale_streams_stopped", {"host": host, "count": stopped})
    _runtime[camera["id"]] = _CameraRuntime()
    return "recovered"


def _refuse(db, camera, state, deps, now, reason: str, detail: dict) -> str:
    record_event(db, camera["id"], camera["camera_number"], reason, detail)
    state.last_event = reason
    _notify(db, state, camera, deps, reason,
            f"{_camera_label(camera)} could not be reconnected automatically: a device was found that does not match "
            "this camera's recorded identity. AnyAiCam did not connect to it.")
    _schedule_backoff(state, now)
    return reason


def run_pass(deps: Dependencies | None = None) -> dict:
    """One check of every provisioned camera. Returns {camera_id: outcome}."""
    deps = deps or Dependencies()
    outcomes = {}
    with _connection() as db:
        ensure_schema(db)
        for camera in provisioned_cameras(db):
            try:
                outcomes[camera["id"]] = _check_camera(db, camera, deps)
            except Exception as error:  # one camera never stops the others
                outcomes[camera["id"]] = "error"
                print(f"camera_address_recovery error camera={camera.get('camera_number')} type={type(error).__name__}")
    return outcomes


def _check_camera(db, camera: dict, deps: Dependencies) -> str:
    now = deps.clock()
    state = _runtime.setdefault(camera["id"], _CameraRuntime())
    endpoint = resolve_endpoint(db, camera["id"], camera["device_key"], camera["onvif_endpoint"], camera["camera_number"])
    host = host_of(endpoint)
    if not host:
        return "no_host"
    check = identity_check(camera["id"], camera["device_key"], host, probe=deps.probe, timeout=PROBE_TIMEOUT_SECONDS, clock=deps.clock)
    if check.outcome == "match":
        _learn_evidence(db, camera, host, check.hardware, deps)
        if state.failing_since is not None or state.last_event:
            record_event(db, camera["id"], camera["camera_number"], "reachable_at_configured_host", {"host": host})
        _runtime[camera["id"]] = _CameraRuntime()
        return "healthy"
    if check.outcome == "mismatch":
        if state.last_event != "identity_mismatch":
            record_event(db, camera["id"], camera["camera_number"], "identity_mismatch", {"host": host})
            state.last_event = "identity_mismatch"
            stopped = _terminate_streams(camera["camera_number"], host, endpoint, deps)
            if stopped:
                record_event(db, camera["id"], camera["camera_number"], "wrong_device_streams_stopped", {"host": host, "count": stopped})
        if state.failing_since is None:
            state.failing_since = now
        due = now >= state.next_search_at
    else:
        state.misses += 1
        if deps.is_streaming(camera["camera_number"]):
            # Streaming fine without answering WS-Discovery: nothing to fix.
            state.misses = 0
            return "streaming_without_discovery"
        if state.misses < MISSES_BEFORE_SEARCH:
            return "unanswered"
        if state.failing_since is None:
            state.failing_since = now
        due = now >= state.next_search_at
    outcome = _attempt_recovery(db, camera, endpoint, host, state, deps, now) if due else "waiting_backoff"
    if outcome == "present_at_configured_host":
        return outcome
    if outcome != "recovered" and state.failing_since is not None and now - state.failing_since >= NOTIFY_AFTER_SECONDS:
        _notify(db, state, camera, deps, "not_recovered",
                f"{_camera_label(camera)} is offline and AnyAiCam could not find it on the network automatically. "
                "Check that the camera has power and is connected to the network.")
    return outcome


async def camera_address_recovery_worker(deps_factory: Callable[[], Dependencies] | None = None) -> None:
    """Background loop: one pass every CHECK_INTERVAL_SECONDS, starting at
    once so a camera that moved while the appliance was off is found right
    after boot. Edge/combined runtime only."""
    if not ENABLED or RUNTIME_ROLE not in {"edge", "combined"}:
        return
    while True:
        try:
            await asyncio.to_thread(run_pass, deps_factory() if deps_factory else None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"camera_address_recovery pass failed type={type(error).__name__}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
