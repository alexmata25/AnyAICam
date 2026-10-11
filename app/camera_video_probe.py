"""Measured camera stream geometry on the edge (2026-10-11, video-pipeline).

Until now the only thing that ever looked at a camera's real streams was
the WebRTC publisher (webrtc_publisher.p2p_source_for), and only when P2P
was set up. This module measures every camera independently of P2P:

  1. ONVIF media profiles (camera_capabilities.discover, the existing
     adapter) give each stream's configured codec/width/height/fps;
  2. ffprobe on the main stream and on the analytics stream then measures
     what the camera really sends (the truth when the two disagree, and
     the only source for a camera without ONVIF).

Measurements are stored in the camera's existing capability record under
each stream's "measured" key ({"width","height","codec","fps","by","at"}).
The ONVIF-reported fields that P2P stream selection uses are NOT changed,
so this never alters which stream WebRTC picks.

Re-measured every ANYAICAM_VIDEO_PROBE_SECONDS (6 h), sooner after a failed
probe (ANYAICAM_VIDEO_PROBE_RETRY_SECONDS, 10 min), and at once when the
camera's main URL changes. A failed or impossible probe leaves the profile
on its safe defaults (video_profile.py) -- it never raises.

The result is also written, credential-free, to HLS_FOLDER/camera<N>.video
.json, where the appliance agent's status reader picks it up and reports it
to the cloud with the camera status it already sends (POST /api/appliance/
cameras); the cloud stores it in cameras.measured_* columns, separate from
the sales label cameras.resolution. No URL is ever logged or written."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import camera_capabilities
import video_profile


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


PROBE_SECONDS = _int_env("ANYAICAM_VIDEO_PROBE_SECONDS", 6 * 3600, 600)
RETRY_SECONDS = _int_env("ANYAICAM_VIDEO_PROBE_RETRY_SECONDS", 600, 60)
PROBE_TIMEOUT_SECONDS = _int_env("ANYAICAM_VIDEO_PROBE_TIMEOUT_SECONDS", 15, 3)
STATUS_SUFFIX = ".video.json"

_CODEC_NAMES = {"hevc": "H265", "h265": "H265", "h264": "H264", "avc": "H264", "mjpeg": "MJPEG", "mpeg4": "MPEG4"}


def normalize_codec(name) -> Optional[str]:
    if not name:
        return None
    text = str(name).strip().lower()
    if not text or len(text) > 16 or not text.replace("_", "").isalnum():
        return None
    return _CODEC_NAMES.get(text, text.upper())


def _fps(value) -> Optional[float]:
    """"20/1" / "30000/1001" / "0/0" -> float or None."""
    try:
        if isinstance(value, str) and "/" in value:
            num, den = value.split("/", 1)
            num, den = float(num), float(den)
            return round(num / den, 2) if den else None
        number = float(value)
        return round(number, 2) if number > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def parse_ffprobe(stdout: str) -> Optional[dict]:
    try:
        streams = json.loads(stdout or "{}").get("streams") or []
    except (TypeError, ValueError, AttributeError):
        return None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        width, height = video_profile._positive(stream.get("width")), video_profile._positive(stream.get("height"))
        if not width or not height:
            continue
        fps = _fps(stream.get("avg_frame_rate")) or _fps(stream.get("r_frame_rate"))
        if fps is not None and not 0 < fps <= 240:
            fps = None
        return {"width": width, "height": height, "codec": normalize_codec(stream.get("codec_name")), "fps": fps}
    return None


def ffprobe_stream(url: str, timeout: float = PROBE_TIMEOUT_SECONDS, run: Callable = subprocess.run) -> Optional[dict]:
    """{width,height,codec,fps} of the stream's first video track, or None.
    stderr is discarded and the URL (credentials) never leaves this call."""
    args = ["ffprobe", "-v", "error"]
    if str(url).lower().startswith(("rtsp://", "rtsps://")):
        args += ["-rtsp_transport", "tcp"]
    args += ["-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
             "-of", "json", str(url)]
    try:
        result = run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return parse_ffprobe(getattr(result, "stdout", ""))


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def measure(record: dict, main_url_with_credentials: str, *, probe: Callable[[str], Optional[dict]] = ffprobe_stream,
            now: Optional[datetime] = None) -> dict:
    """ffprobe the main stream and the analytics stream of a capability
    record (in place) and stamp record["video_probe"]. Credentials are
    re-applied only for the probe call."""
    _clean, username, password = camera_capabilities.split_credentials(main_url_with_credentials)
    stamp = _now_iso(now)
    targets = []
    main = video_profile.main_stream(record)
    if main is not None:
        targets.append(main)
    analytics = video_profile.analytics_stream(record)
    if analytics is not None and analytics is not main:
        targets.append(analytics)
    else:
        # No sized substream yet (URL-convention guesses have no size until
        # measured): measure the unsized subs too, smallest-known first.
        targets += [s for s in record.get("streams") or []
                    if isinstance(s, dict) and s is not main and s.get("uri") and s.get("verified") is not False
                    and not video_profile.stream_geometry(s)["width"]][:2]
    results = {}
    for stream in targets:
        try:
            measured = probe(camera_capabilities.with_credentials(stream["uri"], username, password))
        except Exception:
            measured = None
        if measured:
            stream["measured"] = {**measured, "by": "ffprobe", "at": stamp}
            results["main" if stream is main else "sub"] = "ok"
        else:
            results["main" if stream is main else "sub"] = "failed"
            if (stream is not main and stream.get("discovered_by") == "url_convention" and not stream.get("width")
                    and stream.get("verified") is None and "measured" not in stream):
                # A guessed substream URL that does not answer is not a stream.
                stream["verified"] = False
    main_geometry = video_profile.stream_geometry(main)
    record["video_probe"] = {
        "at": stamp,
        # "ok" only for a real measurement; ONVIF-configured values alone are
        # "configured" (shown as "configured (not yet measured)").
        "status": {"measured": "ok", "configured": "configured"}.get(main_geometry["source"], "failed"),
        "main": results.get("main", "skipped"),
        "sub": results.get("sub", "none"),
    }
    return record


def _merge_previous(new_record: dict, previous: Optional[dict]) -> dict:
    """Keep what the WebRTC publisher learned (verified flags) and earlier
    measurements of streams that still exist."""
    if not previous:
        return new_record
    old = {s.get("uri"): s for s in previous.get("streams") or [] if isinstance(s, dict)}
    for stream in new_record.get("streams") or []:
        before = old.get(stream.get("uri"))
        if not before:
            continue
        if stream.get("verified") is None and before.get("verified") is not None:
            stream["verified"] = before["verified"]
        if "measured" not in stream and isinstance(before.get("measured"), dict):
            stream["measured"] = before["measured"]
    return new_record


def probe_due(record: Optional[dict], main_url: str, now: Optional[datetime] = None) -> bool:
    if not record or camera_capabilities.needs_reprobe(record, main_url, now):
        return True
    info = record.get("video_probe") if isinstance(record.get("video_probe"), dict) else None
    if not info:
        return True
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(info["at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return True
    return age >= (PROBE_SECONDS if info.get("status") == "ok" else RETRY_SECONDS)


def refresh(main_url_with_credentials: str, previous: Optional[dict], *, soap_call: Optional[Callable],
            url_candidates: Optional[Callable], probe: Callable = ffprobe_stream, force: bool = False,
            now: Optional[datetime] = None) -> tuple[Optional[dict], bool]:
    """(record, changed). Re-discovers (ONVIF) only when the stored record
    is for another main URL or past the capability re-probe age, then
    measures. Returns (previous, False) when nothing is due."""
    if not force and not probe_due(previous, main_url_with_credentials, now):
        return previous, False
    if previous is None or camera_capabilities.needs_reprobe(previous, main_url_with_credentials, now):
        record = camera_capabilities.discover(main_url_with_credentials, soap_call=soap_call,
                                              url_candidates=url_candidates, now=now)
        # A record for another main URL (camera re-pointed) carries nothing over.
        record = _merge_previous(record, previous if _same_main(previous, main_url_with_credentials) else None)
    else:
        record = json.loads(json.dumps(previous))
    measure(record, main_url_with_credentials, probe=probe, now=now)
    return record, True


def _same_main(record: Optional[dict], main_url: str) -> bool:
    clean = camera_capabilities.split_credentials(main_url)[0]
    return bool(record) and any(isinstance(s, dict) and s.get("uri") == clean for s in record.get("streams") or [])


def geometry_changed(before: Optional[dict], after: Optional[dict]) -> bool:
    def key(record):
        profile = video_profile.build(record)
        return (profile.source.get("width"), profile.source.get("height"), profile.source.get("codec"),
                profile.analytics_uri, profile.analytics_source.get("width"), profile.analytics_source.get("height"))
    return key(before) != key(after)


# ------------------------------------------------------------ status file

def status_path(hls_folder, camera_number: int) -> Path:
    return Path(hls_folder) / f"camera{int(camera_number)}{STATUS_SUFFIX}"


def status_document(record: Optional[dict], analytics_choice: Optional[str] = None) -> dict:
    """What the agent reports to the cloud: geometry only, no URI.
    analytics_choice overrides the profile's substream policy when the AI
    source is configured otherwise (ANYAICAM_AI_SOURCE=main -> main_by_config)."""
    profile = video_profile.build(record)
    info = (record or {}).get("video_probe") if isinstance((record or {}).get("video_probe"), dict) else {}
    summary = profile.summary()
    summary["probed_at"] = info.get("at")
    status = info.get("status") or ("ok" if profile.measured else "unknown")
    if status == "ok" and not profile.measured:
        status = "configured" if profile.geometry_source == "configured" else "failed"
    summary["status"] = status
    if analytics_choice in video_profile.ANALYTICS_CHOICES:
        summary["analytics_choice"] = analytics_choice
    return summary


def write_status(hls_folder, camera_number: int, record: Optional[dict], analytics_choice: Optional[str] = None) -> None:
    path = status_path(hls_folder, camera_number)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(status_document(record, analytics_choice), sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


# ------------------------------------------------------------ per-process cache

class ProfileCache:
    """camera_number -> (profile, record, loaded at). Readers ask often
    (every clip, every live start, every AI frame); the database is read
    at most once per ttl per camera, and invalidate() after a re-probe."""

    def __init__(self, loader: Callable[[int], Optional[dict]], ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, live_max_kbps: Callable[[], int] = lambda: 0):
        self._loader = loader
        self._ttl = ttl
        self._clock = clock
        self._live_max_kbps = live_max_kbps
        self._lock = threading.Lock()
        self._cache: dict[int, tuple] = {}

    def record(self, camera_number: int) -> Optional[dict]:
        return self._entry(camera_number)[1]

    def profile(self, camera_number: int) -> video_profile.Profile:
        return self._entry(camera_number)[0]

    def _entry(self, camera_number: int):
        now = self._clock()
        with self._lock:
            cached = self._cache.get(camera_number)
            if cached and now - cached[2] < self._ttl:
                return cached
        try:
            record = self._loader(camera_number)
        except Exception:
            record = None
        try:
            profile = video_profile.build(record, self._live_max_kbps())
        except Exception:
            record = None
            profile = video_profile.build(None, self._live_max_kbps())
        entry = (profile, record, now)
        with self._lock:
            self._cache[camera_number] = entry
        return entry

    def invalidate(self, camera_number: Optional[int] = None) -> None:
        with self._lock:
            if camera_number is None:
                self._cache.clear()
            else:
                self._cache.pop(camera_number, None)
