"""AI analytics frames from the camera's analytics stream (2026-10-11,
video-pipeline P2a).

Measured on the Samsung appliance (one 8MP H.265 camera): YOLO person/
vehicle detection (main.ai_person_detector, on for every camera by default)
opened cv2.VideoCapture on the camera's 4K live HLS playlist every ~5 s.
That kept the 4K libx264 live encode (3.3-3.5 cores) mandatory with no
viewer, decoded a 4K frame per scan, and analysed a frame ~6 s behind real
time (FFmpeg starts a live playlist 3 segments back). YOLO runs at
imgsz=640 anyway, so a 4K frame bought it nothing.

Now ONE persistent FFmpeg per camera reads the camera's analytics stream
(video_profile: the smallest stream at least 640 px wide -- the substream on
a normal camera; the main stream on a camera that has no usable substream),
at ANYAICAM_AI_READER_FPS (2), scaled to the profile's analytics frame size
(fits 1280x720, never upscaled), as raw BGR. The newest frame is shared by
every analytics consumer (ai_person_detector, people counting, Smart
Rules); main.detect_objects_frame() also reuses one YOLO pass per frame.

ANYAICAM_AI_SOURCE:
  auto       (default) the analytics-stream reader; after MAX_FAILURES failed
             reader runs the camera's frames come from the live HLS playlist
             (the previous path, which keeps the encoder running) for
             RETRY_SECONDS, then the reader is tried again.
  substream  the reader only (failures never switch to HLS).
  hls        the previous behaviour exactly: cv2.VideoCapture on the live
             HLS playlist for every scan.
An unrecognised value means "hls".

LPR, PPE and face crops need resolution the analytics stream does not
have; main.py grabs ONE main-stream frame on demand (main_frame_grab.py)
only when a vehicle/person box exists on a camera entitled to them.

The reader URL carries the camera's credentials: FFmpeg's stderr is
discarded, the command is never logged, and log lines name only the camera
number and the stream kind."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

AI_SOURCE_MODES = ("auto", "substream", "hls")


def configured_mode(value: Optional[str] = None) -> str:
    raw = os.environ.get("ANYAICAM_AI_SOURCE", "auto") if value is None else value
    mode = (raw or "").strip().lower()
    return mode if mode in AI_SOURCE_MODES else "hls"


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(os.environ.get(name, str(default)) or default)))
    except ValueError:
        return default


AI_SOURCE = configured_mode()
READER_FPS = _float_env("ANYAICAM_AI_READER_FPS", 2.0, 0.2, 10.0)
# A frame older than this is not "now" any more.
FRESH_SECONDS = _float_env("ANYAICAM_AI_FRAME_MAX_AGE_SECONDS", 3.0, 0.5, 30.0)
# How long a caller waits for the reader's first frame.
FIRST_FRAME_WAIT_SECONDS = _float_env("ANYAICAM_AI_FIRST_FRAME_WAIT_SECONDS", 8.0, 0.0, 60.0)
# No frame for this long: the run is killed and counts as failed.
STALL_SECONDS = _float_env("ANYAICAM_AI_READER_STALL_SECONDS", 15.0, 3.0, 300.0)
# Nobody asked for a frame for this long: the reader stops (restarts on demand).
IDLE_STOP_SECONDS = _float_env("ANYAICAM_AI_READER_IDLE_SECONDS", 120.0, 10.0, 3600.0)
MAX_FAILURES = int(_float_env("ANYAICAM_AI_READER_MAX_FAILURES", 3, 1, 100))
RETRY_SECONDS = _float_env("ANYAICAM_AI_READER_RETRY_SECONDS", 1800.0, 60.0, 86400.0)
RESTART_BACKOFF_SECONDS = 5.0
HEALTHY_FRAMES = 10


@dataclass
class SourceSpec:
    """Where a reader reads from. url carries credentials: never log it."""
    url: str
    width: int
    height: int
    kind: str  # "substream" | "main"


def reader_command(url: str, fps: float, width: int, height: int) -> list[str]:
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if url.lower().startswith(("rtsp://", "rtsps://")):
        # Same input options as the motion reader's substream command. (No
        # nobuffer for a file: it drops the packets read while probing.)
        args += ["-rtsp_transport", "tcp", "-fflags", "nobuffer"]
    return args + [
        "-i", url,
        "-an", "-sn", "-dn",
        "-vf", f"fps={fps:g},scale={int(width)}:{int(height)}",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
    ]


def _read_exactly(stream, size: int) -> Optional[bytes]:
    chunks, remaining = [], size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class FrameReader:
    """One camera's persistent reader thread. Pure plumbing: the caller
    injects the source resolver, process factory and clocks."""

    def __init__(self, camera_number: int, resolve: Callable[[int], Optional[SourceSpec]], *,
                 popen: Callable = subprocess.Popen, fps: float = None, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = datetime.now, on_run_end: Callable = None,
                 log: Callable[[str], None] = print):
        self.camera_number = int(camera_number)
        self._resolve = resolve
        self._popen = popen
        self._fps = READER_FPS if fps is None else fps
        self._clock = clock
        self._wall = wall_clock
        self._on_run_end = on_run_end
        self._log = log
        self._lock = threading.Condition()
        self._frame = None
        self._seq = 0
        self._captured_at: Optional[datetime] = None
        self._frame_mono = 0.0
        self._last_request = clock()
        self._run_started = 0.0
        self._process = None
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._restart = False
        self.kind: Optional[str] = None
        self.size: Optional[tuple[int, int]] = None
        self.last_error: Optional[str] = None

    # ---------------------------------------------------------------- control
    def ensure_running(self) -> None:
        with self._lock:
            self._last_request = self._clock()
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name=f"ai-reader-{self.camera_number}", daemon=True)
            self._thread.start()

    def restart(self) -> None:
        """Re-resolve the source (e.g. the camera's measured size changed)."""
        with self._lock:
            self._restart = True
            self._frame = None
        self._kill()

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            self._lock.notify_all()
        self._kill()

    def _kill(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def housekeeping(self) -> None:
        """Called about once a second: kills a stalled run, stops an idle
        reader."""
        now = self._clock()
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            idle = now - self._last_request > IDLE_STOP_SECONDS
            last = self._frame_mono or self._run_started
            stalled = self._process is not None and self._run_started and now - last > STALL_SECONDS
        if running and idle:
            self.stop()
        elif stalled:
            self.last_error = "stalled"
            self._kill()

    # ---------------------------------------------------------------- frames
    def latest(self, max_age: float = None, wait: float = 0.0):
        """(frame, captured_at, seq) of a frame no older than max_age, or None."""
        max_age = FRESH_SECONDS if max_age is None else max_age
        deadline = self._clock() + max(0.0, wait)
        with self._lock:
            self._last_request = self._clock()
            while True:
                if self._frame is not None and self._clock() - self._frame_mono <= max_age:
                    return self._frame, self._captured_at, self._seq
                remaining = deadline - self._clock()
                if remaining <= 0 or self._stop:
                    return None
                self._lock.wait(min(remaining, 0.5))

    # ---------------------------------------------------------------- thread
    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stop:
                    return
                self._restart = False
            frames, error = 0, None
            spec = None
            try:
                spec = self._resolve(self.camera_number)
            except Exception as exc:
                error = f"source unavailable ({type(exc).__name__})"
            if spec is None or not spec.width or not spec.height:
                error = error or "no analytics stream"
            else:
                if (self.kind, self.size) != (spec.kind, (spec.width, spec.height)):
                    self._log(f"Camera {self.camera_number} AI frames: reading the camera "
                              f"{'substream' if spec.kind == 'substream' else 'main stream'} "
                              f"at {self._fps:g} fps, {spec.width}x{spec.height}.")
                self.kind, self.size = spec.kind, (spec.width, spec.height)
                frames, error = self._read(spec)
            self.last_error = error
            if self._on_run_end is not None:
                try:
                    self._on_run_end(self.camera_number, frames, error)
                except Exception:
                    pass
            with self._lock:
                if self._stop:
                    return
                if not self._restart:
                    self._lock.wait(RESTART_BACKOFF_SECONDS)

    def _read(self, spec: SourceSpec) -> tuple[int, Optional[str]]:
        size = spec.width * spec.height * 3
        try:
            process = self._popen(reader_command(spec.url, self._fps, spec.width, spec.height),
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                  bufsize=size)
        except (OSError, ValueError) as exc:
            return 0, f"ffmpeg unavailable ({type(exc).__name__})"
        with self._lock:
            self._process = process
            self._run_started = self._clock()
            self._frame_mono = 0.0
        frames = 0
        error = None
        try:
            import numpy as np
            while True:
                raw = _read_exactly(process.stdout, size)
                if raw is None:
                    error = self.last_error if self.last_error == "stalled" else "stream ended"
                    break
                # writable (consumers draw on / crop it), like a cv2 frame
                frame = np.frombuffer(raw, dtype=np.uint8).reshape((spec.height, spec.width, 3)).copy()
                frames += 1
                with self._lock:
                    self._frame = frame
                    self._seq += 1
                    self._captured_at = self._wall()
                    self._frame_mono = self._clock()
                    self._lock.notify_all()
                    if self._stop or self._restart:
                        break
        except Exception as exc:
            error = f"read failed ({type(exc).__name__})"
        finally:
            with self._lock:
                self._process = None
                self._run_started = 0.0
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        return frames, error


class AnalyticsFrames:
    """All cameras' readers, the auto-mode HLS fallback bookkeeping and the
    housekeeping thread."""

    def __init__(self, resolve: Callable[[int], Optional[SourceSpec]], *, mode: str = None,
                 reader_factory: Callable = None, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, start_housekeeping: bool = True):
        self.mode = configured_mode(mode) if mode is not None else AI_SOURCE
        self._resolve = resolve
        self._clock = clock
        self._log = log
        self._factory = reader_factory or (lambda number: FrameReader(
            number, self._resolve, clock=clock, on_run_end=self._note_run, log=log))
        self._readers: dict[int, FrameReader] = {}
        self._failures: dict[int, int] = {}
        self._hls_until: dict[int, float] = {}
        self._lock = threading.Lock()
        self._housekeeping_started = not start_housekeeping

    def _reader(self, camera_number: int) -> FrameReader:
        with self._lock:
            reader = self._readers.get(camera_number)
            if reader is None:
                reader = self._readers[camera_number] = self._factory(camera_number)
            start_thread = not self._housekeeping_started
            self._housekeeping_started = True
        if start_thread:
            threading.Thread(target=self._housekeeping_loop, name="ai-reader-housekeeping", daemon=True).start()
        return reader

    def _housekeeping_loop(self) -> None:
        while True:
            time.sleep(1.0)
            self.housekeeping()

    def housekeeping(self) -> None:
        with self._lock:
            readers = list(self._readers.values())
        for reader in readers:
            try:
                reader.housekeeping()
            except Exception:
                pass

    def _note_run(self, camera_number: int, frames: int, error: Optional[str]) -> None:
        if frames >= HEALTHY_FRAMES and error in (None, "stream ended"):
            # A long healthy run that ended (camera reboot, network blip).
            with self._lock:
                self._failures[camera_number] = 0
            return
        with self._lock:
            failures = self._failures.get(camera_number, 0) + 1
            self._failures[camera_number] = failures
            fall_back = self.mode == "auto" and failures >= MAX_FAILURES
            if fall_back:
                self._failures[camera_number] = 0
                self._hls_until[camera_number] = self._clock() + RETRY_SECONDS
        if fall_back:
            self._log(f"Camera {camera_number} AI frame reader failed {failures} times ({error or 'no frames'}); "
                      f"using the live HLS stream for {int(RETRY_SECONDS // 60)} minutes.")
            reader = self._readers.get(camera_number)
            if reader is not None:
                reader.stop()

    def uses_hls(self, camera_number: int) -> bool:
        if self.mode == "hls":
            return True
        if self.mode == "auto":
            with self._lock:
                return self._clock() < self._hls_until.get(camera_number, 0.0)
        return False

    def get_frame(self, camera_number: int, wait: float = None) -> Optional[dict]:
        """None: use the live HLS playlist (mode hls, or auto fallback).
        Else {"ok", "frame", "captured_at", "seq", "source", "error"}."""
        if self.uses_hls(camera_number):
            return None
        reader = self._reader(camera_number)
        reader.ensure_running()
        got = reader.latest(wait=FIRST_FRAME_WAIT_SECONDS if wait is None else wait)
        if self.uses_hls(camera_number):
            return None  # it just fell back while we waited
        if got is None:
            return {"ok": False, "frame": None, "captured_at": None, "seq": None, "source": reader.kind,
                    "error": "Analytics stream is starting." if not reader.last_error
                    else f"Analytics stream unavailable ({reader.last_error})."}
        frame, captured_at, seq = got
        return {"ok": True, "frame": frame, "captured_at": captured_at, "seq": seq, "source": reader.kind, "error": None}

    def restart(self, camera_number: int) -> None:
        with self._lock:
            reader = self._readers.get(camera_number)
        if reader is not None:
            reader.restart()

    def stop_all(self) -> None:
        with self._lock:
            readers = list(self._readers.values())
        for reader in readers:
            reader.stop()

    def state(self, camera_number: int) -> dict:
        with self._lock:
            reader = self._readers.get(camera_number)
            failures = self._failures.get(camera_number, 0)
        return {"mode": self.mode, "uses_hls": self.uses_hls(camera_number), "failures": failures,
                "kind": getattr(reader, "kind", None), "size": getattr(reader, "size", None),
                "last_error": getattr(reader, "last_error", None)}
