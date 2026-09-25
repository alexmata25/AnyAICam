"""Capability-driven camera integration layer (2026-09-24, schema 2 2026-09-25).

AnyAiCam must work with arbitrary camera counts and mixed brands/models, so
what a camera can do is DISCOVERED and RECORDED here -- once per camera, in
one common shape -- and every subsystem selects its behaviour from that
record (camera_stream_selection.py) instead of from camera numbers, brand
names or URL shapes.

The record (credential-free; stored per camera_id in camera_capabilities).
Anything nobody could determine is None -- never guessed:

    {"schema": 2, "probed_at": iso, "probe_status": "fresh"|"stale",
     "last_attempt_at": iso, "adapters": [...],
     "device": {"manufacturer", "model", "firmware"},
     "onvif": {"supported", "auth": "ws_security"|"none"|None,
               "device_service", "media_service"},
     "auth": {"credentials_configured": bool, "onvif", "rtsp"},
     "streams": [{"id", "role": "main"|"sub", "uri" (no credentials),
                  "transport": {"protocol", "scheme", "port", "rtsp_transport"},
                  "codec", "width", "height", "fps", "bitrate_kbps",
                  "audio": {"present", "codec"},
                  "discovered_by", "verified": bool|None}],
     "audio": {"input", "input_codec", "output", "two_way"},
     "ptz": {"supported", "pan_tilt", "zoom"},
     "snapshot": {"supported", "uri"},
     "events": {"supported", "analytics"},
     "extensions": {adapter_name: {...}}}

Discovery adapters run in order and their results merge:
  1. onvif_media  -- standard ONVIF Media GetProfiles/GetStreamUri/
                     GetSnapshotUri: streams (codec, resolution, fps and
                     bitrate limits, per-profile audio), audio input/
                     output, PTZ. The primary mechanism.
  2. onvif_device -- standard ONVIF Device GetDeviceInformation/
                     GetCapabilities: make/model/firmware, service
                     addresses, events/analytics/PTZ services. Only tried
                     once onvif_media answered, so a camera without ONVIF
                     costs no extra calls.
  3. url_convention -- webrtc_publisher's substream_candidates() guess,
                     used ONLY when ONVIF reported no second stream.
ONVIF is never required: a camera reachable only through its provisioned
RTSP URL still gets a valid record (its main stream).

If a reprobe cannot reach ONVIF but an earlier probe did, the earlier
ONVIF-derived data is kept (probe_status "stale", retried sooner) instead
of being replaced with a main-stream-only record -- a transient outage
must not silently move P2P onto the full-bitrate main stream for a day.

The five Ryzen cameras are regression fixtures only: nothing here depends
on their count, model or URL format.
"""
from __future__ import annotations

import copy
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from camera_stream_selection import (  # noqa: F401 -- re-exported for existing callers
    ANALYTICS_MIN_WIDTH, P2P_MIN_WIDTH, WEBRTC_CODECS, select_streams, snapshot_uri, supports,
)

SCHEMA_VERSION = 2
REPROBE_SECONDS = max(3600, int(os.environ.get("ANYAICAM_CAPABILITY_REPROBE_SECONDS", str(24 * 3600))))
STALE_REPROBE_SECONDS = max(300, int(os.environ.get("ANYAICAM_CAPABILITY_STALE_REPROBE_SECONDS", "3600")))
RTSP_TRANSPORT = "tcp"  # what every AnyAiCam FFmpeg/MediaMTX consumer uses
_DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}

_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"


# ------------------------------------------------------------ credentials

def split_credentials(url: str) -> tuple[str, str, str]:
    """(credential-free url, username, password) -- credentials are never
    stored in a capability record, only re-applied at the point of use."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = host + (f":{parts.port}" if parts.port else "")
    clean = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return clean, unquote(parts.username or ""), unquote(parts.password or "")


def with_credentials(url: str, username: str, password: str) -> str:
    parts = urlsplit(url)
    netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    if username or password:
        netloc = quote(username, safe="") + (":" + quote(password, safe="") if password else "") + "@" + netloc
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def transport_for(uri: str) -> dict:
    """Non-secret transport details of a (credential-free) stream URI."""
    parts = urlsplit(uri)
    scheme = (parts.scheme or "").lower() or None
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme or "")
    except ValueError:
        port = None
    return {"protocol": "rtsp" if scheme in ("rtsp", "rtsps") else scheme, "scheme": scheme, "port": port,
            "rtsp_transport": RTSP_TRANSPORT if scheme in ("rtsp", "rtsps") else None}


# ------------------------------------------------------------ ONVIF transport

def onvif_soap_call(host: str, username: str, password: str, body: str, action: str) -> ET.Element:
    """Production soap_call: the same WS-Security UsernameToken client
    talk_down_discovery already uses against these cameras, routed to the
    standard ONVIF device or media service path by the action's
    namespace. Credentials go only into the digest header; never logged."""
    import urllib.request
    import talk_down_discovery as tdd
    service = "device_service" if action.startswith(_DEVICE_NS) else "media_service"
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"{tdd._ws_security_header(username, password) if (username or password) else ''}"
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )
    request = urllib.request.Request(
        f"http://{host}:{tdd.ONVIF_PORT}/onvif/{service}", data=envelope.encode(), method="POST",
        headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
    )
    with urllib.request.urlopen(request, timeout=tdd.ONVIF_TIMEOUT_SECONDS) as response:
        return ET.fromstring(response.read())


# ------------------------------------------------------------ ONVIF parsing

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element, name):
    for item in element.iter():
        if _local(item.tag) == name:
            return item
    return None


def _text(element, name):
    found = _child(element, name) if element is not None else None
    return (found.text or "").strip() if found is not None and found.text else None


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _has(element, name) -> bool:
    return element is not None and _child(element, name) is not None


def parse_onvif_profiles(root: ET.Element) -> list[dict]:
    """Pure parsing of a GetProfiles response (no network)."""
    profiles = []
    for profile in root.iter():
        if _local(profile.tag) != "Profiles":
            continue
        encoder = _child(profile, "VideoEncoderConfiguration")
        resolution = _child(encoder, "Resolution") if encoder is not None else None
        rate = _child(encoder, "RateControl") if encoder is not None else None
        audio_encoder = _child(profile, "AudioEncoderConfiguration")
        ptz = _child(profile, "PTZConfiguration")
        profiles.append({
            "token": profile.get("token"),
            "codec": (_text(encoder, "Encoding") or "").upper() or None,
            "width": _int(_text(resolution, "Width")) if resolution is not None else None,
            "height": _int(_text(resolution, "Height")) if resolution is not None else None,
            "fps": _int(_text(rate, "FrameRateLimit")) if rate is not None else None,
            "bitrate_kbps": _int(_text(rate, "BitrateLimit")) if rate is not None else None,
            "audio_codec": (_text(audio_encoder, "Encoding") or "").upper() or None if audio_encoder is not None else None,
            "audio_encoder": audio_encoder is not None,
            "audio_input": _child(profile, "AudioSourceConfiguration") is not None,
            "audio_output": _child(profile, "AudioOutputConfiguration") is not None,
            "ptz": ptz is not None,
            "ptz_pan_tilt": (_has(ptz, "DefaultContinuousPanTiltVelocitySpace") or _has(ptz, "DefaultAbsolutePantTiltPositionSpace")) if ptz is not None else None,
            "ptz_zoom": (_has(ptz, "DefaultContinuousZoomVelocitySpace") or _has(ptz, "DefaultAbsoluteZoomPositionSpace")) if ptz is not None else None,
        })
    return profiles


def _uri_from(root: ET.Element) -> str | None:
    found = _child(root, "Uri")
    return (found.text or "").strip() if found is not None and found.text else None


def _any_true(values):
    values = [v for v in values if v is not None]
    return any(values) if values else None


# ------------------------------------------------------------ adapters

def onvif_media_adapter(context: dict, soap_call: Callable) -> dict:
    """Standard ONVIF Media discovery. soap_call(host, username, password,
    body, action) -> ET root (onvif_soap_call in production, a fake in
    tests). Never raises; an unreachable/non-ONVIF camera just contributes
    nothing."""
    host, username, password = context["host"], context["username"], context["password"]
    try:
        root = soap_call(host, username, password, f'<trt:GetProfiles xmlns:trt="{_MEDIA_NS}"/>',
                         f"{_MEDIA_NS}/GetProfiles")
        profiles = [p for p in parse_onvif_profiles(root) if p.get("token")]
    except Exception:
        return {}
    streams, snapshot = [], None
    for profile in profiles:
        body = (f'<trt:GetStreamUri xmlns:trt="{_MEDIA_NS}" xmlns:tt="{_SCHEMA_NS}"><trt:StreamSetup>'
                '<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>'
                f'</trt:StreamSetup><trt:ProfileToken>{profile["token"]}</trt:ProfileToken></trt:GetStreamUri>')
        try:
            uri = _uri_from(soap_call(host, username, password, body, f"{_MEDIA_NS}/GetStreamUri"))
        except Exception:
            uri = None
        if not uri:
            continue
        clean = split_credentials(uri)[0]
        streams.append({"id": profile["token"], "uri": clean, "transport": transport_for(clean), "codec": profile["codec"],
                        "width": profile["width"], "height": profile["height"], "fps": profile["fps"],
                        "bitrate_kbps": profile["bitrate_kbps"],
                        "audio": {"present": profile["audio_encoder"], "codec": profile["audio_codec"]},
                        "discovered_by": "onvif_media", "verified": None})
    if profiles:
        try:
            snapshot = _uri_from(soap_call(host, username, password,
                f'<trt:GetSnapshotUri xmlns:trt="{_MEDIA_NS}"><trt:ProfileToken>{profiles[0]["token"]}</trt:ProfileToken></trt:GetSnapshotUri>',
                f"{_MEDIA_NS}/GetSnapshotUri"))
        except Exception:
            snapshot = None
    if not profiles:
        return {}
    audio_input = any(p["audio_input"] or p["audio_encoder"] for p in profiles)
    audio_output = any(p["audio_output"] for p in profiles)
    input_codecs = [p["audio_codec"] for p in profiles if p["audio_codec"]]
    ptz_profiles = [p for p in profiles if p["ptz"]]
    return {
        "streams": streams,
        "audio": {"input": audio_input, "input_codec": input_codecs[0] if input_codecs else None,
                  "output": audio_output, "two_way": audio_input and audio_output},
        "ptz": {"supported": bool(ptz_profiles),
                "pan_tilt": _any_true(p["ptz_pan_tilt"] for p in ptz_profiles) if ptz_profiles else False,
                "zoom": _any_true(p["ptz_zoom"] for p in ptz_profiles) if ptz_profiles else False},
        "snapshot": {"supported": bool(snapshot), "uri": split_credentials(snapshot)[0] if snapshot else None},
    }


def _service_path(xaddr: str | None) -> str | None:
    """A service address without credentials (host kept: it is the
    camera's own LAN address, already present in its stream URIs)."""
    return split_credentials(xaddr)[0] if xaddr else None


def onvif_device_adapter(context: dict, soap_call: Callable) -> dict:
    """Standard ONVIF Device service: make/model/firmware and which
    optional services (events, analytics, PTZ) the camera exposes. Serial
    numbers and MAC addresses are deliberately not recorded. Never raises."""
    host, username, password = context["host"], context["username"], context["password"]
    result: dict = {}
    try:
        info = soap_call(host, username, password, f'<tds:GetDeviceInformation xmlns:tds="{_DEVICE_NS}"/>',
                         f"{_DEVICE_NS}/GetDeviceInformation")
        result["device"] = {"manufacturer": _text(info, "Manufacturer"), "model": _text(info, "Model"),
                            "firmware": _text(info, "FirmwareVersion")}
    except Exception:
        pass
    try:
        caps = soap_call(host, username, password,
                         f'<tds:GetCapabilities xmlns:tds="{_DEVICE_NS}"><tds:Category>All</tds:Category></tds:GetCapabilities>',
                         f"{_DEVICE_NS}/GetCapabilities")
        def xaddr(section):
            node = _child(caps, section)
            return _text(node, "XAddr") if node is not None else None
        result["services"] = {"device": _service_path(xaddr("Device")), "media": _service_path(xaddr("Media")),
                              "events": _service_path(xaddr("Events")), "analytics": _service_path(xaddr("Analytics")),
                              "ptz": _service_path(xaddr("PTZ"))}
    except Exception:
        pass
    return result


def url_convention_adapter(context: dict, candidates: Callable[[str], list[str]]) -> dict:
    """Fallback only: substream URLs guessed from well-known URL shapes."""
    streams = []
    for index, url in enumerate(candidates(context["main_uri"])):
        clean = split_credentials(url)[0]
        streams.append({"id": f"url-convention-{index}", "uri": clean, "transport": transport_for(clean), "codec": None,
                        "width": None, "height": None, "fps": None, "bitrate_kbps": None,
                        "audio": {"present": None, "codec": None}, "discovered_by": "url_convention", "verified": None})
    return {"streams": streams}


# ------------------------------------------------------------ record

def empty_record(now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return {"schema": SCHEMA_VERSION, "probed_at": stamp, "probe_status": "fresh", "last_attempt_at": stamp,
            "adapters": [], "device": {"manufacturer": None, "model": None, "firmware": None},
            "onvif": {"supported": None, "auth": None, "device_service": None, "media_service": None},
            "auth": {"credentials_configured": None, "onvif": None, "rtsp": None},
            "streams": [], "audio": {"input": None, "input_codec": None, "output": None, "two_way": None},
            "ptz": {"supported": None, "pan_tilt": None, "zoom": None},
            "snapshot": {"supported": None, "uri": None},
            "events": {"supported": None, "analytics": None}, "extensions": {}}


def upgrade(record: dict | None) -> dict | None:
    """Reads any stored record into the current shape (a copy; unknown
    fields None). Schema-1 records keep their original "schema" value so
    needs_reprobe() still asks for a fresh probe."""
    if not record:
        return record
    out = empty_record()
    out.update({k: copy.deepcopy(v) for k, v in record.items() if k in out and not isinstance(out[k], dict)})
    for key in ("device", "onvif", "auth", "audio", "ptz", "snapshot", "events", "extensions"):
        value = record.get(key)
        if isinstance(value, dict):
            out[key].update(copy.deepcopy(value))
    if isinstance(record.get("ptz"), bool):
        out["ptz"]["supported"] = record["ptz"]
    if "snapshot_uri" in record and not isinstance(record.get("snapshot"), dict):
        out["snapshot"] = {"supported": bool(record["snapshot_uri"]) if "onvif_media" in (record.get("adapters") or []) else None,
                           "uri": record["snapshot_uri"]}
    if "onvif_media" in out["adapters"] and out["onvif"]["supported"] is None:
        out["onvif"]["supported"] = True
    for stream in out["streams"]:
        stream.setdefault("transport", transport_for(stream.get("uri") or ""))
        stream.setdefault("audio", {"present": None, "codec": None})
    out["probe_status"] = record.get("probe_status") or "fresh"
    out["last_attempt_at"] = record.get("last_attempt_at") or record.get("probed_at")
    return out


def _assign_roles(streams: list[dict], main_uri: str) -> list[dict]:
    """The largest known stream (or the provisioned main URI) is "main";
    every other stream is "sub"."""
    def area(stream):
        return (stream.get("width") or 0) * (stream.get("height") or 0)
    largest = max(streams, key=area) if any(area(s) for s in streams) else None
    for stream in streams:
        is_main = stream is largest if largest is not None else stream["uri"] == main_uri
        stream["role"] = "main" if is_main else "sub"
    return streams


def discover(main_uri_with_credentials: str, *, soap_call: Callable | None, url_candidates: Callable[[str], list[str]] | None,
             now: datetime | None = None, previous: dict | None = None) -> dict:
    """Probes a camera and returns its capability record. previous (the
    stored record, any schema) is only used when this probe could not
    reach ONVIF but an earlier one did: that earlier data is kept, marked
    stale, rather than lost."""
    now = now or datetime.now(timezone.utc)
    main_uri, username, password = split_credentials(main_uri_with_credentials)
    context = {"host": urlsplit(main_uri).hostname or "", "username": username, "password": password, "main_uri": main_uri}
    record = empty_record(now)
    record["auth"]["credentials_configured"] = bool(username or password)
    onvif_answered = False
    if soap_call is not None and context["host"]:
        onvif = onvif_media_adapter(context, soap_call)
        if onvif:
            onvif_answered = True
            record["adapters"].append("onvif_media")
            record["onvif"]["supported"] = True
            record["onvif"]["auth"] = "ws_security" if (username or password) else "none"
            record["auth"]["onvif"] = record["onvif"]["auth"]
            record["streams"] = onvif["streams"]
            for key in ("audio", "ptz", "snapshot"):
                record[key].update(onvif[key])
            device = onvif_device_adapter(context, soap_call)
            if device:
                record["adapters"].append("onvif_device")
                record["device"].update(device.get("device") or {})
                services = device.get("services") or {}
                record["onvif"]["device_service"] = services.get("device")
                record["onvif"]["media_service"] = services.get("media")
                if services:
                    record["events"] = {"supported": bool(services.get("events")), "analytics": bool(services.get("analytics"))}
                    if services.get("ptz") and not record["ptz"]["supported"]:
                        record["ptz"]["supported"] = True
                        record["ptz"]["pan_tilt"] = record["ptz"]["zoom"] = None

    carried = _carry_forward(previous, main_uri, now) if not onvif_answered else None
    if carried is not None:
        return carried

    if not any(s["uri"] == main_uri for s in record["streams"]):
        record["streams"].insert(0, {"id": "provisioned-main", "uri": main_uri, "transport": transport_for(main_uri),
                                     "codec": None, "width": None, "height": None, "fps": None, "bitrate_kbps": None,
                                     "audio": {"present": None, "codec": None},
                                     "discovered_by": "provisioning", "verified": None})
    if len(record["streams"]) < 2 and url_candidates is not None:
        guessed = url_convention_adapter(context, url_candidates)["streams"]
        if guessed:
            record["adapters"].append("url_convention")
            record["extensions"]["url_convention"] = {"guessed_streams": len(guessed)}
            record["streams"].extend(guessed)
    record["streams"] = _assign_roles(record["streams"], main_uri)
    return record


def _carry_forward(previous: dict | None, main_uri: str, now: datetime) -> dict | None:
    """The earlier ONVIF-derived record, marked stale, when it describes
    the same provisioned main stream; otherwise None."""
    earlier = upgrade(previous)
    if not earlier or "onvif_media" not in (earlier.get("adapters") or []):
        return None
    if not any(s.get("uri") == main_uri for s in earlier.get("streams") or []):
        return None  # re-provisioned camera: the old record describes something else
    earlier["schema"] = SCHEMA_VERSION
    earlier["probe_status"] = "stale"
    earlier["last_attempt_at"] = now.isoformat()
    return earlier


def _age_seconds(stamp, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


def needs_reprobe(record: dict | None, main_uri: str, now: datetime | None = None) -> bool:
    if not record or record.get("schema") != SCHEMA_VERSION:
        return True
    if not any(s.get("uri") == split_credentials(main_uri)[0] for s in record.get("streams") or []):
        return True
    now = now or datetime.now(timezone.utc)
    if record.get("probe_status") == "stale":
        age = _age_seconds(record.get("last_attempt_at"), now)
        return age is None or age >= STALE_REPROBE_SECONDS
    age = _age_seconds(record.get("probed_at"), now)
    return age is None or age >= REPROBE_SECONDS


# ------------------------------------------------------------ persistence

def load(db, camera_id: str) -> dict | None:
    row = db.execute("SELECT capabilities_json FROM camera_capabilities WHERE camera_id=?", (camera_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def save(db, camera_id: str, record: dict) -> None:
    db.execute(
        "INSERT INTO camera_capabilities(camera_id,capabilities_json,probed_at) VALUES(?,?,?) "
        "ON CONFLICT(camera_id) DO UPDATE SET capabilities_json=excluded.capabilities_json, probed_at=excluded.probed_at",
        (camera_id, json.dumps(record, sort_keys=True), record.get("probed_at")),
    )
