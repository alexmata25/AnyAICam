"""On-demand live HLS encoding (2026-10-10, perf-substream-hls).

The live HLS encoder (main.py start_live_stream(): the camera's main stream
re-encoded with libx264, because H.265 cannot be stream-copied into
browser-playable HLS) ran for every camera all the time. Measured on the
Samsung appliance with one 8MP H.265 camera and no viewer: 3.26 cores.

ANYAICAM_LIVE_HLS_MODE:
  always     (default) the encoder runs all the time -- the previous
             behaviour exactly; every function below that changes behaviour
             is a no-op in this mode.
  on_demand  the encoder runs only while something needs it:
               * a browser/proxy request for the camera's playlist or a
                 segment (/static/hls/..., the customer live playlist,
                 segment and still routes) -- touch();
               * an internal reader that is using the playlist right now
                 (AI/LPR/PPE/face/people counting/Smart Rules frames via
                 detect_objects_frame(), the motion reader while it reads
                 HLS) -- touch() or hold();
               * the live relay uploader while a cloud viewer is watching
                 -- a demand provider;
             and stops ANYAICAM_LIVE_HLS_IDLE_SECONDS after the last demand.
An unrecognised value means "always".

While a camera's encoder is idle its playlist goes stale, so readers that
used playlist freshness as "camera online" (main.py _legacy_camera_status,
the appliance agent's LocalVmsStatusReader) would report it offline. In
on_demand mode the live supervisor therefore refreshes camera{N}.alive in
the HLS folder while the camera is demonstrably reachable (its recorder is
running, or the motion reader got a substream frame recently); both status
readers accept that file as well. The file is never written in "always"
mode."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

LIVE_HLS_MODES = ("always", "on_demand")


def configured_mode(value: Optional[str] = None) -> str:
    raw = os.environ.get("ANYAICAM_LIVE_HLS_MODE", "always") if value is None else value
    mode = (raw or "").strip().lower().replace("-", "_")
    return mode if mode in LIVE_HLS_MODES else "always"


def _float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


LIVE_HLS_MODE = configured_mode()
# How long the encoder keeps running after the last demand.
IDLE_SECONDS = _float_env("ANYAICAM_LIVE_HLS_IDLE_SECONDS", 60.0, 10.0)
# A playlist written within this many seconds is "live" for internal readers.
FRESH_SECONDS = 10.0
# How often an idle supervisor re-checks demand (a viewer's first request
# starts the encoder within this long).
IDLE_POLL_SECONDS = 1.0
ALIVE_SUFFIX = ".alive"

_lock = threading.Lock()
_mode = LIVE_HLS_MODE
_idle_seconds = IDLE_SECONDS
_clock: Callable[[], float] = time.monotonic
_last_demand: dict[int, float] = {}
_holds: dict[int, set[str]] = {}
_providers: list[Callable[[], Iterable[int]]] = []
_last_source_frame: dict[int, float] = {}


def configure(mode: Optional[str] = None, idle_seconds: Optional[float] = None,
              clock: Optional[Callable[[], float]] = None) -> None:
    """Tests (and nothing else) re-point the mode/clock; clears all state."""
    global _mode, _idle_seconds, _clock
    with _lock:
        _mode = configured_mode(mode) if mode is not None else LIVE_HLS_MODE
        _idle_seconds = IDLE_SECONDS if idle_seconds is None else float(idle_seconds)
        _clock = clock or time.monotonic
        _last_demand.clear()
        _holds.clear()
        _providers.clear()
        _last_source_frame.clear()


def mode() -> str:
    return _mode


def on_demand() -> bool:
    return _mode == "on_demand"


def touch(camera_number: int, reason: str = "request") -> None:
    """Something needs this camera's live HLS now (keeps it running for
    IDLE_SECONDS more). Cheap; safe from any thread; no-op in "always"."""
    if _mode != "on_demand":
        return
    try:
        number = int(camera_number)
    except (TypeError, ValueError):
        return
    with _lock:
        _last_demand[number] = _clock()


def hold(camera_number: int, owner: str) -> None:
    """A long-lived internal reader keeps the encoder running until release()."""
    if _mode != "on_demand":
        return
    with _lock:
        _holds.setdefault(int(camera_number), set()).add(owner)
        _last_demand[int(camera_number)] = _clock()


def release(camera_number: int, owner: str) -> None:
    with _lock:
        owners = _holds.get(int(camera_number))
        if owners and owner in owners:
            owners.discard(owner)
            _last_demand[int(camera_number)] = _clock()  # idle timer starts now


def add_provider(provider: Callable[[], Iterable[int]]) -> None:
    """provider() -> camera numbers that currently need live HLS (e.g. the
    relay uploader's active cameras). Must be cheap and must not raise."""
    with _lock:
        if provider not in _providers:
            _providers.append(provider)


def _provided(camera_number: int) -> bool:
    with _lock:
        providers = list(_providers)
    for provider in providers:
        try:
            if camera_number in set(int(n) for n in (provider() or ())):
                return True
        except Exception:
            continue
    return False


def wanted(camera_number: int) -> bool:
    """True when the encoder for this camera should be running."""
    if _mode != "on_demand":
        return True
    number = int(camera_number)
    with _lock:
        if _holds.get(number):
            return True
        last = _last_demand.get(number)
        now = _clock()
    if last is not None and now - last < _idle_seconds:
        return True
    return _provided(number)


def note_source_frame(camera_number: int) -> None:
    """The motion reader received a frame straight from the camera."""
    if _mode != "on_demand":
        return
    with _lock:
        _last_source_frame[int(camera_number)] = _clock()


def camera_reachable(camera_number: int, recorder_running: bool, within_seconds: float = 20.0) -> bool:
    with _lock:
        last = _last_source_frame.get(int(camera_number))
        now = _clock()
    return bool(recorder_running) or (last is not None and now - last <= within_seconds)


def alive_path(hls_folder: Path, camera_number: int) -> Path:
    return Path(hls_folder) / f"camera{int(camera_number)}{ALIVE_SUFFIX}"


def write_alive(hls_folder: Path, camera_number: int) -> None:
    if _mode != "on_demand":
        return
    path = alive_path(hls_folder, camera_number)
    try:
        path.touch(exist_ok=True)
        os.utime(path, None)
    except OSError:
        pass


def file_age(path: Path, now: Optional[float] = None) -> Optional[float]:
    try:
        return max(0.0, (time.time() if now is None else now) - Path(path).stat().st_mtime)
    except OSError:
        return None


def playlist_fresh(manifest: Path, max_age: float = FRESH_SECONDS) -> bool:
    age = file_age(manifest)
    return age is not None and age <= max_age


def mark_playlist_stale(manifest: Path) -> None:
    """After an idle stop: age the playlist's mtime so every freshness check
    (local live playlist route, status, analytics) treats it as stale at
    once, while its content stays for FFmpeg's append_list on the next
    start (segment numbering continues, so no segment name is reused)."""
    try:
        old = time.time() - 3600
        os.utime(manifest, (old, old))
    except OSError:
        pass


def camera_number_from_hls_path(path: str) -> Optional[int]:
    """camera number for "/static/hls/camera12.m3u8" or ".../camera12_000000001.ts"."""
    name = path.rsplit("/", 1)[-1]
    if not name.startswith("camera"):
        return None
    rest = name[len("camera"):]
    digits = ""
    for char in rest:
        if char.isdigit():
            digits += char
        else:
            break
    if not digits:
        return None
    tail = rest[len(digits):]
    if tail == ".m3u8" or (tail.startswith("_") and tail.endswith(".ts")):
        return int(digits)
    return None


def state(camera_number: int) -> dict:
    with _lock:
        holds = sorted(_holds.get(int(camera_number), ()))
        last = _last_demand.get(int(camera_number))
        now = _clock()
    return {"mode": _mode, "holds": holds,
            "seconds_since_demand": None if last is None else round(now - last, 1)}
