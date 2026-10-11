"""One full-resolution main-stream frame, on demand (2026-10-11,
video-pipeline P2a).

AI detection now runs on the camera's analytics stream (analytics_frames.py,
usually a 640x360 substream). LPR, PPE and face recognition crop the
detected vehicle/person and need the camera's full resolution for that
crop. They used to get it from the 4K live HLS frame (always) or, for LPR
only, from the Event-mode pre-event buffer -- which does not exist on a
Continuous-mode camera.

grab() returns ONE decoded frame of the camera's main stream:
  * Event mode: the newest frame of the camera's own pre-event buffer
    (lpr.latest_full_resolution_frame -- no extra camera session);
  * otherwise: a single-frame RTSP read of the main stream (one short
    session, decoded from the next keyframe, then closed).
Callers ask only when a vehicle/person box exists on a camera entitled to
LPR/PPE/face, results are cached ANYAICAM_AI_MAIN_GRAB_CACHE_SECONDS per
camera (one grab serves LPR, PPE and face in the same scan), at most one
grab per camera runs at a time, and a camera whose grabs keep failing is
left alone for a while. ANYAICAM_AI_MAIN_GRAB=false disables it (crops then
come from the analytics frame, as when the analytics frame is already full
resolution).

The URL carries credentials: FFmpeg's stderr is discarded and the command
is never logged."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable, Optional

MAIN_GRAB_ENABLED = os.environ.get("ANYAICAM_AI_MAIN_GRAB", "true").strip().lower() != "false"


def _float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


CACHE_SECONDS = _float_env("ANYAICAM_AI_MAIN_GRAB_CACHE_SECONDS", 2.5, 0.0)
TIMEOUT_SECONDS = _float_env("ANYAICAM_AI_MAIN_GRAB_TIMEOUT_SECONDS", 10.0, 2.0)
FAILURE_BACKOFF_SECONDS = _float_env("ANYAICAM_AI_MAIN_GRAB_BACKOFF_SECONDS", 60.0, 0.0)


def grab_command(url: str) -> list[str]:
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if url.lower().startswith(("rtsp://", "rtsps://")):
        args += ["-rtsp_transport", "tcp"]
    # BMP: no compression work for a frame that is decoded straight back.
    return args + ["-i", url, "-an", "-sn", "-dn", "-frames:v", "1", "-f", "image2pipe", "-c:v", "bmp", "pipe:1"]


def needs_full_resolution(analytics_width: int, main_width: Optional[int]) -> bool:
    """A grab only helps when the main stream has clearly more pixels
    (lpr.full_resolution_vehicle_crop applies the same 1.3x rule)."""
    if not main_width:
        return True  # unknown main size: let the crop helper decide after the grab
    return main_width >= analytics_width * 1.3


class MainFrameGrabber:
    def __init__(self, resolve_url: Callable[[int], Optional[str]], *, run: Callable = subprocess.run,
                 decode: Callable = None, buffer_frame: Callable[[int], object] = None,
                 clock: Callable[[], float] = time.monotonic, enabled: bool = None):
        self._resolve_url = resolve_url
        self._run = run
        self._decode = decode
        self._buffer_frame = buffer_frame
        self._clock = clock
        self.enabled = MAIN_GRAB_ENABLED if enabled is None else enabled
        self._cache: dict[int, tuple[float, object]] = {}
        self._failed_until: dict[int, float] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._guard = threading.Lock()
        self._generation: dict[int, int] = {}
        self.grabs = 0

    def _lock_for(self, camera_number: int) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(camera_number, threading.Lock())

    def _decode_image(self, raw: bytes):
        if self._decode is not None:
            return self._decode(raw)
        import cv2
        import numpy as np
        return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)

    def grab(self, camera_number: int):
        """A BGR frame of the main stream, or None. Blocking (run it in a
        worker thread). Never raises."""
        if not self.enabled:
            return None
        with self._guard:
            generation = self._generation.get(camera_number, 0)
        with self._lock_for(camera_number):
            now = self._clock()
            cached = self._cache.get(camera_number)
            if cached and now - cached[0] <= CACHE_SECONDS:
                return cached[1]
            if now < self._failed_until.get(camera_number, 0.0):
                return None
            frame = None
            if self._buffer_frame is not None:
                try:
                    frame = self._buffer_frame(camera_number)
                except Exception:
                    frame = None
            if frame is None:
                frame = self._grab_rtsp(camera_number)
            now = self._clock()
            with self._guard:
                if self._generation.get(camera_number, 0) != generation:
                    return None  # the camera was retired during the grab: never cache or use it
                if frame is None:
                    self._failed_until[camera_number] = now + FAILURE_BACKOFF_SECONDS
                self._cache[camera_number] = (now, frame)
            return frame

    def forget(self, camera_number: int) -> None:
        """Drop the camera's cached frame and back-off (camera removed)."""
        with self._guard:
            self._generation[camera_number] = self._generation.get(camera_number, 0) + 1
            self._cache.pop(camera_number, None)
            self._failed_until.pop(camera_number, None)

    def _grab_rtsp(self, camera_number: int):
        try:
            url = self._resolve_url(camera_number)
        except Exception:
            url = None
        if not url:
            return None
        self.grabs += 1
        try:
            result = self._run(grab_command(url), capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        raw = getattr(result, "stdout", b"") or b""
        if getattr(result, "returncode", 1) != 0 or not raw:
            return None
        try:
            return self._decode_image(raw)
        except Exception:
            return None


def full_resolution_crop(analytics_frame, box, full_frame, *, margin: float = 0.15):
    """The analytics-frame box (x, y, w, h) cut from the full-resolution
    frame (lpr.full_resolution_vehicle_crop's mapping), or the analytics
    crop when there is no usable full frame."""
    x, y, w, h = [int(v) for v in box]
    fallback = analytics_frame[max(0, y): y + h, max(0, x): x + w] if analytics_frame is not None else None
    if full_frame is None or analytics_frame is None:
        return fallback
    ah, aw = analytics_frame.shape[:2]
    fh, fw = full_frame.shape[:2]
    if fw < aw * 1.3:
        return fallback
    sx, sy = fw / float(aw), fh / float(ah)
    mx, my = w * margin, h * margin
    x1, y1 = max(0, int((x - mx) * sx)), max(0, int((y - my) * sy))
    x2, y2 = min(fw, int((x + w + mx) * sx)), min(fh, int((y + h + my) * sy))
    if x2 - x1 < 8 or y2 - y1 < 8:
        return fallback
    return full_frame[y1:y2, x1:x2]
