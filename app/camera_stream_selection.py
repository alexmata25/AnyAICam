"""Runtime stream selection from a camera capability record (2026-09-25).

Pure policy, no I/O: camera_capabilities.py DISCOVERS what a camera can do
and stores it; this module only DECIDES which of the recorded streams a
subsystem should try, best first. The caller verifies a candidate (e.g.
webrtc_publisher's probe) and falls through to the next one.

Every function degrades safely: a record from an older schema, a record
with unknown codecs/resolutions, or no record at all still yields an
answer (ultimately the camera's provisioned main stream), and a capability
nobody could determine is reported as None ("unknown"), never guessed.

Nothing here knows about camera numbers, counts, brands or URL shapes.
"""
from __future__ import annotations

import os

P2P_MIN_WIDTH = int(os.environ.get("ANYAICAM_P2P_MIN_WIDTH", "480"))
ANALYTICS_MIN_WIDTH = int(os.environ.get("ANYAICAM_ANALYTICS_MIN_WIDTH", "640"))
# What browsers decode over WebRTC everywhere today. A stream whose codec
# is unknown is still a candidate (the caller's probe decides); a stream
# with a known, different codec (H265, JPEG, MPEG4, ...) never is.
WEBRTC_CODECS = {"H264"}


def _width(stream: dict) -> int:
    return stream.get("width") or 0


def _area(stream: dict) -> int:
    return (stream.get("width") or 0) * (stream.get("height") or 0)


def _main_streams(streams: list[dict]) -> list[dict]:
    """The camera's full-quality stream(s), best first. A record with no
    role information treats its first stream (the provisioned main) as
    main, exactly as before roles existed."""
    mains = [s for s in streams if s.get("role") == "main"] or streams[:1]
    return sorted(mains, key=lambda s: (_area(s), s.get("fps") or 0, s.get("bitrate_kbps") or 0), reverse=True)


def select_streams(record: dict | None, purpose: str) -> list[dict]:
    """Candidate streams for a purpose, best first.

    "recording" -> the full-quality main stream (largest, then highest fps)
    "live_p2p"  -> the lightest WebRTC-compatible substream that is at
                   least P2P_MIN_WIDTH wide, then compatible substreams of
                   unknown size, then smaller ones, then the main stream
    "analytics" -> the smallest substream at least ANALYTICS_MIN_WIDTH
                   wide, then the main stream
    Any other purpose gets the main stream."""
    streams = list((record or {}).get("streams") or [])
    if not streams:
        return []
    main = _main_streams(streams)
    subs = [s for s in streams if s not in main]

    if purpose == "live_p2p":
        compatible = [s for s in subs if s.get("codec") in WEBRTC_CODECS or s.get("codec") is None]
        known_h264 = [s for s in compatible if s.get("codec") in WEBRTC_CODECS]
        unknown_codec = [s for s in compatible if s.get("codec") is None]
        ordered = []
        for group in (known_h264, unknown_codec):
            ordered += sorted([s for s in group if _width(s) >= P2P_MIN_WIDTH], key=lambda s: (_width(s), s.get("bitrate_kbps") or 0))
        ordered += [s for s in compatible if not _width(s) and s not in ordered]
        ordered += sorted([s for s in compatible if 0 < _width(s) < P2P_MIN_WIDTH], key=_width, reverse=True)
        return ordered + main
    if purpose == "analytics":
        return sorted([s for s in subs if _width(s) >= ANALYTICS_MIN_WIDTH], key=_width) + main
    return main


def _tri(value):
    return value if isinstance(value, bool) else None


def supports(record: dict | None, feature: str) -> bool | None:
    """True / False / None (unknown) for a camera feature. Reads both the
    current record shape and schema-1 records (audio.output, ptz bool,
    snapshot_uri) so a camera probed before an upgrade still answers."""
    record = record or {}
    audio = record.get("audio") or {}
    ptz = record.get("ptz")
    if feature in ("talk", "audio_output"):
        return _tri(audio.get("output"))
    if feature == "audio_input":
        return _tri(audio.get("input"))
    if feature == "two_way_audio":
        if audio.get("two_way") is not None:
            return _tri(audio.get("two_way"))
        values = (audio.get("input"), audio.get("output"))
        if False in values:
            return False
        return True if values == (True, True) else None
    if feature == "ptz":
        return _tri(ptz.get("supported")) if isinstance(ptz, dict) else _tri(ptz)
    if feature == "snapshot":
        snapshot = record.get("snapshot")
        if isinstance(snapshot, dict):
            return _tri(snapshot.get("supported"))
        return True if record.get("snapshot_uri") else None
    if feature == "onvif":
        return _tri((record.get("onvif") or {}).get("supported"))
    if feature in ("events", "analytics"):
        return _tri((record.get("events") or {}).get("supported" if feature == "events" else "analytics"))
    return None


def snapshot_uri(record: dict | None) -> str | None:
    """The camera's own snapshot endpoint (credential-free), if it has one."""
    record = record or {}
    snapshot = record.get("snapshot")
    if isinstance(snapshot, dict):
        return snapshot.get("uri")
    return record.get("snapshot_uri")
