"""Where the motion detector reads its 1 fps 160x90 grayscale frames from
(2026-10-10, perf-substream-hls).

Measured on the Samsung appliance (one 8MP camera, main stream H.265
3840x2160 ~20 fps, substream H.265 640x360): the motion reader decoded the
camera's 4K live HLS output (itself a full libx264 re-encode of the main
stream) just to produce 1 fps 160x90 gray frames -- 0.64 of a core -- and it
kept the 4K live HLS encode (3.26 cores) mandatory even with no viewer.

The detector scales every frame to exactly 160x90 gray before comparing, so
its thresholds do not depend on the source resolution. Reading the camera's
own substream over RTSP gives it the same frames (from the same field of
view) for a fraction of the decode cost, and ~6 s earlier than the HLS
playlist (FFmpeg starts a live playlist 3 segments behind).

ANYAICAM_MOTION_SOURCE:
  auto       (default) the camera's substream when it has a distinct one,
             else the live HLS playlist; after MAX_FAILURES consecutive
             failed substream runs the HLS playlist is used for
             RETRY_SECONDS, then the substream is tried again.
  substream  the substream whenever one exists (failures never switch to
             HLS); a camera without a distinct substream still uses HLS.
  hls        the previous behaviour exactly: always the live HLS playlist.
An unrecognised value means "hls".

Substream discovery reuses what the codebase already knows: the camera's
stored capability record (camera_capabilities.py -- ONVIF media profiles,
written by the WebRTC publisher), and only when there is no usable record,
the vendor-neutral URL convention guess (webrtc_publisher.
substream_candidates). Capability URIs are credential-free; the camera's
own credentials are re-applied only to the URL handed to FFmpeg. No URL is
ever logged or returned by describe()."""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

import camera_capabilities

MOTION_SOURCE_MODES = ("auto", "substream", "hls")
MOTION_FRAME_FILTER = "fps=1,scale=160:90,format=gray"
MOTION_FRAME_WIDTH = 160
MOTION_FRAME_HEIGHT = 90


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


def configured_mode(value: Optional[str] = None) -> str:
    raw = os.environ.get("ANYAICAM_MOTION_SOURCE", "auto") if value is None else value
    mode = (raw or "").strip().lower()
    return mode if mode in MOTION_SOURCE_MODES else "hls"


MOTION_SOURCE = configured_mode()
# Consecutive failed substream runs before "auto" falls back to HLS.
SUBSTREAM_MAX_FAILURES = _int_env("ANYAICAM_MOTION_SUBSTREAM_MAX_FAILURES", 3, 1)
# How long "auto" stays on HLS after that before trying the substream again.
SUBSTREAM_RETRY_SECONDS = _int_env("ANYAICAM_MOTION_SUBSTREAM_RETRY_SECONDS", 1800, 60)
# A substream that delivers no frame for this long is treated as failed
# (an RTSP session that stays open but sends nothing would otherwise block
# the reader forever).
SUBSTREAM_STALL_SECONDS = _int_env("ANYAICAM_MOTION_SUBSTREAM_STALL_SECONDS", 30, 5)
# A substream run that delivered at least this many frames was healthy.
SUBSTREAM_HEALTHY_FRAMES = 30


def hls_command(manifest: str) -> list[str]:
    """The motion reader's original command, unchanged."""
    return [
        "ffmpeg", "-loglevel", "error", "-fflags", "nobuffer",
        "-i", str(manifest), "-an", "-vf", MOTION_FRAME_FILTER,
        "-f", "rawvideo", "pipe:1",
    ]


def substream_command(url: str) -> list[str]:
    """Same frames (1 fps, 160x90, gray, raw) read straight from the
    camera's substream over RTSP/TCP. The URL carries credentials: the
    caller must discard FFmpeg's stderr and never log this list."""
    args = ["ffmpeg", "-loglevel", "error"]
    if url.lower().startswith(("rtsp://", "rtsps://")):
        args += ["-rtsp_transport", "tcp"]
    return args + [
        "-fflags", "nobuffer", "-i", url, "-an", "-vf", MOTION_FRAME_FILTER,
        "-f", "rawvideo", "pipe:1",
    ]


def _stream_width(stream: dict) -> int:
    try:
        return int(stream.get("width") or 0)
    except (TypeError, ValueError):
        return 0


def substream_from_record(record: Optional[dict], main_url: str) -> Optional[str]:
    """Credential-free URI of the best substream for motion in a stored
    capability record, or None. The record must describe this camera's
    CURRENT main stream (a record for a URL the camera no longer uses is
    ignored). Smallest stream at least 160 px wide first, then streams of
    unknown size; the main stream is never returned."""
    if not record or not isinstance(record.get("streams"), list):
        return None
    clean_main = camera_capabilities.split_credentials(main_url)[0]
    streams = [s for s in record["streams"] if isinstance(s, dict) and s.get("uri")]
    if not any(s.get("uri") == clean_main for s in streams):
        return None
    subs = [s for s in streams if s.get("role") != "main" and s.get("uri") != clean_main and s.get("verified") is not False]
    sized = sorted([s for s in subs if _stream_width(s) >= MOTION_FRAME_WIDTH], key=_stream_width)
    unknown = [s for s in subs if not _stream_width(s)]
    for stream in sized + unknown:
        return stream["uri"]
    return None


def resolve_substream_url(main_url: str, record: Optional[dict],
                          url_candidates: Optional[Callable[[str], list[str]]]) -> Optional[str]:
    """Credentialed substream URL for FFmpeg, or None when the camera has no
    distinct substream. Never logs."""
    if not main_url:
        return None
    _clean, username, password = camera_capabilities.split_credentials(main_url)
    clean_sub = substream_from_record(record, main_url)
    if clean_sub:
        url = camera_capabilities.with_credentials(clean_sub, username, password)
    else:
        url = None
        if url_candidates is not None:
            try:
                guesses = [u for u in (url_candidates(main_url) or []) if u and u != main_url]
            except Exception:
                guesses = []
            url = guesses[0] if guesses else None
    if not url or camera_capabilities.split_credentials(url)[0] == camera_capabilities.split_credentials(main_url)[0]:
        return None
    return url


class MotionSourceSelector:
    """Per-camera choice between the substream and the HLS playlist, with
    failure counting. Pure bookkeeping: the caller supplies the substream
    URL resolver and the clock, so this is unit-testable without FFmpeg."""

    def __init__(self, mode: str = None, *, max_failures: int = None, retry_seconds: float = None,
                 clock: Callable[[], float] = time.monotonic):
        self.mode = configured_mode(mode) if mode is not None else MOTION_SOURCE
        self.max_failures = SUBSTREAM_MAX_FAILURES if max_failures is None else max(1, int(max_failures))
        self.retry_seconds = SUBSTREAM_RETRY_SECONDS if retry_seconds is None else float(retry_seconds)
        self._clock = clock
        self._failures: dict[int, int] = {}
        self._hls_until: dict[int, float] = {}
        self._last_kind: dict[int, str] = {}

    def choose(self, camera_number: int, resolve_substream: Callable[[int], Optional[str]]) -> tuple[str, Optional[str]]:
        """("substream", credentialed URL) or ("hls", None)."""
        if self.mode == "hls":
            return "hls", None
        if self.mode == "auto" and self._clock() < self._hls_until.get(camera_number, 0.0):
            return "hls", None
        try:
            url = resolve_substream(camera_number)
        except Exception:
            url = None
        return ("substream", url) if url else ("hls", None)

    def note_run(self, camera_number: int, kind: str, frames: int, stalled: bool = False) -> Optional[str]:
        """Records how a reader run ended. Returns a log-safe message when
        the camera just fell back to HLS, else None."""
        if kind != "substream":
            return None
        if frames >= SUBSTREAM_HEALTHY_FRAMES and not stalled:
            self._failures[camera_number] = 0
            return None
        failures = self._failures.get(camera_number, 0) + 1
        self._failures[camera_number] = failures
        if self.mode != "auto" or failures < self.max_failures:
            return None
        self._failures[camera_number] = 0
        self._hls_until[camera_number] = self._clock() + self.retry_seconds
        return (f"Camera {camera_number} motion substream failed {failures} times; "
                f"reading the live HLS stream for {int(self.retry_seconds // 60)} minutes.")

    def note_kind(self, camera_number: int, kind: str) -> Optional[str]:
        """A log-safe message when the camera's motion source changes."""
        if self._last_kind.get(camera_number) == kind:
            return None
        self._last_kind[camera_number] = kind
        if self.mode == "hls":
            return None  # the previous behaviour, previous logs
        return f"Camera {camera_number} motion detector source: {'camera substream' if kind == 'substream' else 'live HLS stream'}."

    def failures(self, camera_number: int) -> int:
        return self._failures.get(camera_number, 0)

    def current_kind(self, camera_number: int) -> Optional[str]:
        return self._last_kind.get(camera_number)
