"""Capability-driven camera integration layer (2026-09-24).

AnyAiCam must work with arbitrary camera counts and mixed brands/models, so
what a camera can do is DISCOVERED and RECORDED here -- once per camera, in
one common shape -- and every subsystem selects its behaviour from that
record instead of from camera numbers, brand names or URL shapes.

The record (credential-free; stored per camera_id in camera_capabilities):

    {"schema": 1, "probed_at": iso, "adapters": [...],
     "streams": [{"id", "role": "main"|"sub", "uri" (no credentials),
                  "codec", "width", "height", "fps", "bitrate_kbps",
                  "discovered_by", "verified": bool|None}],
     "audio": {"output": bool|None}, "ptz": bool|None,
     "snapshot_uri": str|None}

Discovery adapters run in order and their results merge:
  1. onvif_media  -- standard ONVIF Media GetProfiles/GetStreamUri/
                     GetSnapshotUri (codec, resolution, fps and bitrate
                     limits, audio output, PTZ). The primary mechanism.
  2. url_convention -- webrtc_publisher's substream_candidates() guess,
                     used ONLY when ONVIF reported no second stream.
A stream picked for use is then verified (webrtc_publisher's ffprobe
check) before it is trusted.

select_stream(record, purpose) is the one policy function:
  "recording" -> highest-resolution stream (the camera's main stream)
  "live_p2p"  -> smallest WebRTC-compatible (H.264) stream at least
                 P2P_MIN_WIDTH wide, falling back to any H.264, then main
  "analytics" -> smallest stream at least ANALYTICS_MIN_WIDTH wide
Recording and the live HLS encode still call camera_url() directly today;
moving them onto select_stream() is a later, separately-validated step --
for "recording" the policy returns the same main stream either way.

The five Ryzen cameras are regression fixtures only: nothing here depends
on their count, model or URL format.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SCHEMA_VERSION = 1
P2P_MIN_WIDTH = int(os.environ.get("ANYAICAM_P2P_MIN_WIDTH", "480"))
ANALYTICS_MIN_WIDTH = int(os.environ.get("ANYAICAM_ANALYTICS_MIN_WIDTH", "640"))
REPROBE_SECONDS = max(3600, int(os.environ.get("ANYAICAM_CAPABILITY_REPROBE_SECONDS", str(24 * 3600))))
WEBRTC_CODECS = {"H264"}

_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
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


# ------------------------------------------------------------ ONVIF adapter

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


def parse_onvif_profiles(root: ET.Element) -> list[dict]:
    """Pure parsing of a GetProfiles response (no network)."""
    profiles = []
    for profile in root.iter():
        if _local(profile.tag) != "Profiles":
            continue
        encoder = _child(profile, "VideoEncoderConfiguration")
        resolution = _child(encoder, "Resolution") if encoder is not None else None
        rate = _child(encoder, "RateControl") if encoder is not None else None
        profiles.append({
            "token": profile.get("token"),
            "codec": (_text(encoder, "Encoding") or "").upper() or None,
            "width": _int(_text(resolution, "Width")) if resolution is not None else None,
            "height": _int(_text(resolution, "Height")) if resolution is not None else None,
            "fps": _int(_text(rate, "FrameRateLimit")) if rate is not None else None,
            "bitrate_kbps": _int(_text(rate, "BitrateLimit")) if rate is not None else None,
            "audio_output": _child(profile, "AudioOutputConfiguration") is not None,
            "ptz": _child(profile, "PTZConfiguration") is not None,
        })
    return profiles


def _uri_from(root: ET.Element) -> str | None:
    found = _child(root, "Uri")
    return (found.text or "").strip() if found is not None and found.text else None


def onvif_media_adapter(context: dict, soap_call: Callable) -> dict:
    """Standard ONVIF Media discovery. soap_call(host, username, password,
    body, action) -> ET root (talk_down_discovery._soap_call in production,
    a fake in tests). Never raises; an unreachable/non-ONVIF camera just
    contributes nothing."""
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
        streams.append({"id": profile["token"], "uri": split_credentials(uri)[0], "codec": profile["codec"],
                        "width": profile["width"], "height": profile["height"], "fps": profile["fps"],
                        "bitrate_kbps": profile["bitrate_kbps"], "discovered_by": "onvif_media", "verified": None})
    if profiles:
        try:
            snapshot = _uri_from(soap_call(host, username, password,
                f'<trt:GetSnapshotUri xmlns:trt="{_MEDIA_NS}"><trt:ProfileToken>{profiles[0]["token"]}</trt:ProfileToken></trt:GetSnapshotUri>',
                f"{_MEDIA_NS}/GetSnapshotUri"))
        except Exception:
            snapshot = None
    return {
        "streams": streams,
        "audio": {"output": any(p["audio_output"] for p in profiles)} if profiles else {},
        "ptz": any(p["ptz"] for p in profiles) if profiles else None,
        "snapshot_uri": split_credentials(snapshot)[0] if snapshot else None,
    }


def url_convention_adapter(context: dict, candidates: Callable[[str], list[str]]) -> dict:
    """Fallback only: substream URLs guessed from well-known URL shapes."""
    return {"streams": [{"id": f"url-convention-{index}", "uri": split_credentials(url)[0], "codec": None,
                         "width": None, "height": None, "fps": None, "bitrate_kbps": None,
                         "discovered_by": "url_convention", "verified": None}
                        for index, url in enumerate(candidates(context["main_uri"]))]}


# ------------------------------------------------------------ record

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
             now: datetime | None = None) -> dict:
    main_uri, username, password = split_credentials(main_uri_with_credentials)
    context = {"host": urlsplit(main_uri).hostname or "", "username": username, "password": password, "main_uri": main_uri}
    record = {"schema": SCHEMA_VERSION, "probed_at": (now or datetime.now(timezone.utc)).isoformat(),
              "adapters": [], "streams": [], "audio": {}, "ptz": None, "snapshot_uri": None}
    if soap_call is not None and context["host"]:
        onvif = onvif_media_adapter(context, soap_call)
        if onvif.get("streams"):
            record["adapters"].append("onvif_media")
            record["streams"] = onvif["streams"]
            record["audio"] = onvif.get("audio") or {}
            record["ptz"] = onvif.get("ptz")
            record["snapshot_uri"] = onvif.get("snapshot_uri")
    if not any(s["uri"] == main_uri for s in record["streams"]):
        record["streams"].insert(0, {"id": "provisioned-main", "uri": main_uri, "codec": None, "width": None,
                                     "height": None, "fps": None, "bitrate_kbps": None,
                                     "discovered_by": "provisioning", "verified": None})
    if len(record["streams"]) < 2 and url_candidates is not None:
        guessed = url_convention_adapter(context, url_candidates)["streams"]
        if guessed:
            record["adapters"].append("url_convention")
            record["streams"].extend(guessed)
    record["streams"] = _assign_roles(record["streams"], main_uri)
    return record


def select_streams(record: dict, purpose: str) -> list[dict]:
    """Candidate streams for a purpose, best first (the caller verifies and
    falls through). Unknown purposes get the main stream."""
    streams = list(record.get("streams") or [])
    main = [s for s in streams if s.get("role") == "main"] or streams[:1]
    subs = [s for s in streams if s.get("role") != "main"]

    def width(stream):
        return stream.get("width") or 0

    if purpose == "live_p2p":
        compatible = [s for s in subs if (s.get("codec") in WEBRTC_CODECS or s.get("codec") is None)]
        sized = sorted([s for s in compatible if width(s) >= P2P_MIN_WIDTH], key=lambda s: (s.get("bitrate_kbps") or 0, width(s)))
        unknown = [s for s in compatible if not width(s)]
        small = sorted([s for s in compatible if 0 < width(s) < P2P_MIN_WIDTH], key=width, reverse=True)
        return sized + unknown + small + main
    if purpose == "analytics":
        sized = sorted([s for s in subs if width(s) >= ANALYTICS_MIN_WIDTH], key=width)
        return sized + main
    return main  # "recording" and anything else: the camera's full-quality main stream


def needs_reprobe(record: dict | None, main_uri: str, now: datetime | None = None) -> bool:
    if not record or record.get("schema") != SCHEMA_VERSION:
        return True
    if not any(s.get("uri") == split_credentials(main_uri)[0] for s in record.get("streams") or []):
        return True
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(record["probed_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return True
    return age >= REPROBE_SECONDS


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
