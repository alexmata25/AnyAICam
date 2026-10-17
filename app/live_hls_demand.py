"""On-demand live HLS encoding (2026-10-10, perf-substream-hls).

The live HLS encoder (main.py start_live_stream(): the camera's main stream
re-encoded with libx264, because H.265 cannot be stream-copied into
browser-playable HLS) ran for every camera all the time. Measured on the
Samsung appliance with one 8MP H.265 camera and no viewer: 3.26 cores.

ANYAICAM_LIVE_HLS_MODE:
  auto       (default since 2026-10-11, video-pipeline P2b) on_demand while
             NO always-on consumer of the live HLS output remains, else
             always; re-evaluated every AUTO_RECHECK_SECONDS by the policy
             main.py registers (set_auto_policy): AI frames and motion are
             not read from HLS (ANYAICAM_AI_SOURCE / ANYAICAM_MOTION_SOURCE
             not "hls"), and the appliance agent has advertised (fresh
             AGENT_MARKER in the HLS folder) that it reads camera<N>.alive --
             an older agent would report an idle camera offline. Per-camera
             HLS fallbacks (motion/AI after reader failures) keep that
             camera's encoder running through hold()/touch(), which are
             recorded in auto mode too.
  always     the encoder runs all the time -- the previous behaviour
             exactly; every function below that changes behaviour is a
             no-op in this mode.
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
the HLS folder while the camera is demonstrably sending video (a camera
frame reached the motion or AI reader within 15 s, or the recorder's current
file was written within 30 s -- never mere process liveness); both status
readers accept that file as well. The file is never written in "always"
mode."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

LIVE_HLS_MODES = ("always", "on_demand", "auto")
# Written by the appliance agent (anyaicam_agent.camera_binding) into the HLS
# folder while it runs a status reader that accepts camera<N>.alive.
AGENT_MARKER = "agent-reads-alive.marker"
AGENT_MARKER_FRESH_SECONDS = 900.0
AUTO_RECHECK_SECONDS = 30.0


def configured_mode(value: Optional[str] = None) -> str:
    raw = os.environ.get("ANYAICAM_LIVE_HLS_MODE", "auto") if value is None else value
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
_auto_policy: Optional[Callable[[], bool]] = None
_auto_log: Callable[[str], None] = lambda message: None
_auto_state: list = [None, -1e18]  # [last decision, evaluated at]
# When auto mode switched to on demand: every running encoder gets a full
# IDLE_SECONDS from then (a viewer mid-stream is never cut off by the switch).
_auto_on_demand_since: list = [None]


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
        _auto_state[:] = [None, -1e18]
        _auto_on_demand_since[0] = None


def mode() -> str:
    return _mode


def set_auto_policy(policy: Optional[Callable[[], bool]], log: Callable[[str], None] = None) -> None:
    """main.py: policy() -> True when no always-on HLS consumer remains."""
    global _auto_policy, _auto_log
    _auto_policy = policy
    if log is not None:
        _auto_log = log
    _auto_state[:] = [None, -1e18]


def on_demand() -> bool:
    if _mode == "on_demand":
        return True
    if _mode != "auto" or _auto_policy is None:
        return False
    now = _clock()
    if _auto_state[0] is not None and now - _auto_state[1] < AUTO_RECHECK_SECONDS:
        return _auto_state[0]
    try:
        decision = bool(_auto_policy())
    except Exception:
        decision = False
    if decision and not _auto_state[0]:
        _auto_on_demand_since[0] = now
    if decision != _auto_state[0]:
        _auto_log("Live HLS (auto): " + ("on demand -- no always-on consumer remains." if decision
                                         else "always on -- a consumer still needs it."))
    _auto_state[:] = [decision, now]
    return decision


def effective_mode() -> str:
    return "on_demand" if on_demand() else "always"


def agent_marker_fresh(hls_folder: Path, now: Optional[float] = None) -> bool:
    age = file_age(Path(hls_folder) / AGENT_MARKER, now)
    return age is not None and age <= AGENT_MARKER_FRESH_SECONDS


def touch(camera_number: int, reason: str = "request") -> None:
    """Something needs this camera's live HLS now (keeps it running for
    IDLE_SECONDS more). Cheap; safe from any thread; no-op in "always"."""
    if _mode == "always":
        return
    try:
        number = int(camera_number)
    except (TypeError, ValueError):
        return
    with _lock:
        _last_demand[number] = _clock()


def hold(camera_number: int, owner: str) -> None:
    """A long-lived internal reader keeps the encoder running until release()."""
    if _mode == "always":
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


def forget(camera_number: int) -> None:
    """Drop every demand, hold and source-frame record of a camera that is
    no longer configured (its encoder then idles in on_demand mode)."""
    try:
        number = int(camera_number)
    except (TypeError, ValueError):
        return
    with _lock:
        _last_demand.pop(number, None)
        _holds.pop(number, None)
        _last_source_frame.pop(number, None)


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
    if not on_demand():
        return True
    number = int(camera_number)
    with _lock:
        if _holds.get(number):
            return True
        last = _last_demand.get(number)
        now = _clock()
    since = _auto_on_demand_since[0] if _mode == "auto" else None
    if since is not None and (last is None or since > last):
        last = since
    if last is not None and now - last < _idle_seconds:
        return True
    return _provided(number)


def note_source_frame(camera_number: int) -> None:
    """The motion reader received a frame straight from the camera."""
    if _mode == "always":
        return
    with _lock:
        _last_source_frame[int(camera_number)] = _clock()


# Evidence windows for "the camera is really sending video" (Codex review
# 2026-10-11: a running-but-stalled recorder process is NOT evidence).
REACHABLE_FRAME_SECONDS = 15.0
RECORDER_ADVANCE_SECONDS = 30.0


def recent_source_frame(camera_number: int, within_seconds: float = REACHABLE_FRAME_SECONDS) -> bool:
    with _lock:
        last = _last_source_frame.get(int(camera_number))
        now = _clock()
    return last is not None and now - last <= within_seconds


def camera_reachable(camera_number: int, receiving_video: bool = False,
                     within_seconds: float = REACHABLE_FRAME_SECONDS) -> bool:
    """True only on INCOMING-video evidence: a frame from the camera within
    within_seconds (motion reader -> note_source_frame()), or the caller's
    own evidence (receiving_video: the AI reader got a frame recently, or
    the recorder's current output file is actually growing). Mere process
    liveness never counts."""
    return bool(receiving_video) or recent_source_frame(camera_number, within_seconds)


def recorder_output_advancing(folders, camera_number: int, now: Optional[float] = None,
                              within_seconds: float = RECORDER_ADVANCE_SECONDS) -> bool:
    """The camera's newest recording file (camera<N>_*.mkv / buf<N>_*.mkv in
    the given folders) was written within within_seconds -- the recorder is
    really receiving and writing video, not just alive."""
    now = time.time() if now is None else now
    prefixes = (f"camera{int(camera_number)}_", f"buf{int(camera_number)}_")
    newest = None
    for folder in folders:
        try:
            entries = os.scandir(folder)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not (entry.name.endswith(".mkv") and entry.name.startswith(prefixes)):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                newest = mtime if newest is None else max(newest, mtime)
    return newest is not None and 0 <= now - newest <= within_seconds


def alive_path(hls_folder: Path, camera_number: int) -> Path:
    return Path(hls_folder) / f"camera{int(camera_number)}{ALIVE_SUFFIX}"


def write_alive(hls_folder: Path, camera_number: int) -> None:
    if not on_demand():
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
    """Written within max_age on either side of now: a future mtime (the
    clock was stepped back after a now-disconnected camera last wrote it)
    is stale, not fresh. file_age() itself is unchanged for its callers."""
    try:
        age = time.time() - Path(manifest).stat().st_mtime
    except OSError:
        return False
    return abs(age) <= max_age


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
    return {"mode": _mode, "effective": effective_mode(), "holds": holds,
            "seconds_since_demand": None if last is None else round(now - last, 1)}
