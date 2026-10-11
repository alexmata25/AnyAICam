"""video-pipeline P2a (2026-10-11): AI detection from the camera's analytics
stream (analytics_frames.py) instead of the 4K live HLS playlist, and
on-demand main-stream frames for LPR / PPE / face crops (main_frame_grab.py).

Fakes for the camera, YOLO and the process factory; where ffmpeg is
installed one reader test runs the real ffmpeg on a generated file."""
import io
import shutil
import subprocess
import threading
import time
import types
from datetime import datetime

import numpy as np
import pytest

import analytics_frames
import live_hls_demand
import main
import main_frame_grab

SECRET = "s3cr3t-P@ss"
SUB_URL = f"rtsp://admin:{SECRET}@192.0.2.30:554/Streaming/Channels/102"
MAIN_URL = f"rtsp://admin:{SECRET}@192.0.2.30:554/Streaming/Channels/101"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeProcess:
    """stdout yields `frames` raw BGR frames, then EOF (or blocks)."""

    def __init__(self, width, height, frames, block_after=False):
        self.width, self.height = width, height
        data = b"".join(bytes([i % 256]) * (width * height * 3) for i in range(1, frames + 1))
        self._data = io.BytesIO(data)
        self._block = threading.Event() if block_after else None
        self.killed = False
        self.returncode = None
        self.stdout = self

    def read(self, n):
        chunk = self._data.read(n)
        if not chunk and self._block is not None:
            self._block.wait(10)
            return b""
        return chunk

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        if self._block is not None:
            self._block.set()

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture(autouse=True)
def _reset():
    live_hls_demand.configure(mode="always")
    main._ai_detection_cache.clear()
    yield
    live_hls_demand.configure(mode="always")
    main._ai_detection_cache.clear()


# ---------------------------------------------------------------- modes / command

@pytest.mark.parametrize("value,expected", [(None, "main"), ("main", "main"), ("auto", "auto"), ("SUBSTREAM", "substream"),
                                            ("hls", "hls"), ("bogus", "hls"), ("", "hls")])
def test_ai_source_mode_parsing(monkeypatch, value, expected):
    monkeypatch.delenv("ANYAICAM_AI_SOURCE", raising=False)
    assert analytics_frames.configured_mode(value) == expected


def test_reader_command_low_fps_raw_bgr_over_tcp():
    command = analytics_frames.reader_command(SUB_URL, 2.0, 640, 360)
    assert command[:8] == ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-fflags", "nobuffer"]
    assert command[command.index("-i") + 1] == SUB_URL
    assert command[command.index("-vf") + 1] == f"fps=2,scale=640:360:flags={analytics_frames.READER_SCALE_FLAGS}"
    assert command[-5:] == ["-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]


# ---------------------------------------------------------------- reader

def _reader(processes, logs=None, clock=None, on_run_end=None, spec=None):
    spec = spec or analytics_frames.SourceSpec(SUB_URL, 8, 6, "substream")
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        assert kwargs["stderr"] is subprocess.DEVNULL   # credentials in ffmpeg errors never surface
        return processes.pop(0) if processes else FakeProcess(8, 6, 0)

    reader = analytics_frames.FrameReader(
        3, lambda n: spec, popen=popen, fps=2.0, clock=clock or time.monotonic,
        on_run_end=on_run_end, log=(logs.append if logs is not None else (lambda m: None)))
    reader.calls = calls
    return reader


def test_reader_shares_the_newest_frame_and_logs_no_url():
    logs = []
    reader = _reader([FakeProcess(8, 6, 3, block_after=True)], logs=logs)
    reader.ensure_running()
    got = reader.latest(max_age=5, wait=5)
    assert got is not None
    frame, captured_at, seq = got
    assert frame.shape == (6, 8, 3) and frame.flags.writeable
    assert isinstance(captured_at, datetime)
    deadline = time.monotonic() + 5
    while reader.latest(max_age=5)[2] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    frame2, _, seq2 = reader.latest(max_age=5)
    assert seq2 == 3 and frame2[0, 0, 0] == 3       # newest frame wins
    assert len(reader.calls) == 1                     # ONE ffmpeg for all consumers
    reader.stop()
    assert any("substream" in line for line in logs)
    assert not any(SECRET in line or "rtsp://" in line for line in logs)


def test_stalled_reader_is_killed_by_housekeeping(monkeypatch):
    monkeypatch.setattr(analytics_frames, "STALL_SECONDS", 3.0)
    clock = Clock()
    process = FakeProcess(8, 6, 1, block_after=True)
    ended = []
    reader = _reader([process], clock=clock, on_run_end=lambda n, frames, error: ended.append((frames, error)))
    reader.ensure_running()
    assert reader.latest(max_age=60, wait=5) is not None
    clock.now += 2
    reader.housekeeping()
    assert not process.killed
    clock.now += 2
    reader.housekeeping()
    assert process.killed
    deadline = time.monotonic() + 5
    while not ended and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ended and ended[0][1] == "stalled"
    reader.stop()


def test_idle_reader_stops(monkeypatch):
    clock = Clock()
    process = FakeProcess(8, 6, 1, block_after=True)
    reader = _reader([process], clock=clock)
    reader.ensure_running()
    assert reader.latest(max_age=60, wait=5) is not None
    clock.now += analytics_frames.IDLE_STOP_SECONDS + 1
    reader.housekeeping()
    assert process.killed


class StubReader:
    def __init__(self, frames=None, error=None):
        self.frames = list(frames or [])
        self.kind, self.size, self.last_error = "substream", (8, 6), error
        self.started = 0
        self.stopped = False

    def ensure_running(self):
        self.started += 1

    def latest(self, max_age=None, wait=0.0):
        return self.frames[0] if self.frames else None

    def stop(self):
        self.stopped = True

    def restart(self):
        pass

    def housekeeping(self):
        pass


def test_auto_falls_back_to_hls_after_repeated_failures_then_retries():
    clock = Clock()
    logs = []
    stub = StubReader(error="stream ended")
    frames = analytics_frames.AnalyticsFrames(lambda n: None, mode="auto", reader_factory=lambda n: stub,
                                              clock=clock, log=logs.append, start_housekeeping=False)
    result = frames.get_frame(1, wait=0)
    assert result["ok"] is False and "unavailable" in result["error"]
    for _ in range(analytics_frames.MAX_FAILURES):
        frames._note_run(1, 0, "stream ended")
    assert frames.get_frame(1, wait=0) is None          # None = use the live HLS path
    assert stub.stopped and any("live HLS stream" in line for line in logs)
    clock.now += analytics_frames.RETRY_SECONDS + 1
    stub.frames = [(np.zeros((6, 8, 3), np.uint8), datetime.now(), 7)]
    got = frames.get_frame(1, wait=0)
    assert got["ok"] and got["seq"] == 7


def test_substream_mode_never_falls_back_and_hls_mode_never_reads():
    stub = StubReader()
    sub = analytics_frames.AnalyticsFrames(lambda n: None, mode="substream", reader_factory=lambda n: stub,
                                           start_housekeeping=False)
    for _ in range(10):
        sub._note_run(1, 0, "stream ended")
    assert sub.get_frame(1, wait=0) is not None
    hls = analytics_frames.AnalyticsFrames(lambda n: None, mode="hls",
                                           reader_factory=lambda n: pytest.fail("no reader in hls mode"),
                                           start_housekeeping=False)
    assert hls.get_frame(1) is None


def test_a_healthy_run_resets_the_failure_count():
    frames = analytics_frames.AnalyticsFrames(lambda n: None, mode="auto", reader_factory=lambda n: StubReader(),
                                              start_housekeeping=False)
    frames._note_run(1, 0, "stream ended")
    frames._note_run(1, 0, "stream ended")
    frames._note_run(1, 500, "stream ended")
    frames._note_run(1, 0, "stream ended")
    assert not frames.uses_hls(1)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_reader_produces_profile_sized_bgr_frames(tmp_path):
    source = tmp_path / "sub.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=20",
                    "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", str(source)], check=True, timeout=120)
    reader = analytics_frames.FrameReader(1, lambda n: analytics_frames.SourceSpec(str(source), 640, 360, "substream"),
                                          fps=2.0, log=lambda m: None)
    reader.ensure_running()
    got = reader.latest(max_age=30, wait=20)
    reader.stop()
    assert got is not None and got[0].shape == (360, 640, 3)


# ---------------------------------------------------------------- detect_objects_frame

def _yolo(monkeypatch, boxes, counter):
    def box(x1, y1, x2, y2, cls=0):
        tensor = lambda values: [types.SimpleNamespace(item=lambda v=values: v, tolist=lambda v=values: v)]
        return types.SimpleNamespace(cls=tensor(cls), conf=tensor(0.9), xyxy=tensor([x1, y1, x2, y2]))

    result = types.SimpleNamespace(names={0: "person", 2: "car"}, boxes=[box(*b) for b in boxes])

    def predict(**kwargs):
        counter.append(kwargs["source"].shape)
        return [result]
    monkeypatch.setattr(main, "YOLO", object())
    monkeypatch.setattr(main, "get_yolo_model", lambda: types.SimpleNamespace(predict=predict))


class FixedFrames:
    def __init__(self, frame, seq=1, ok=True):
        self.frame, self.seq, self.ok = frame, seq, ok
        self.mode = "auto"

    def get_frame(self, camera_number, wait=None):
        if not self.ok:
            return {"ok": False, "error": "Analytics stream is starting.", "frame": None, "seq": None,
                    "captured_at": None, "source": "substream"}
        return {"ok": True, "frame": self.frame, "captured_at": datetime(2026, 10, 11, 12, 0, 0),
                "seq": self.seq, "source": "substream", "error": None}


def test_detect_objects_frame_reads_the_shared_substream_frame_and_never_touches_hls(monkeypatch, tmp_path):
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    monkeypatch.setattr(main.cv2, "VideoCapture", lambda *a: pytest.fail("the 4K HLS playlist must not be opened"))
    calls = []
    _yolo(monkeypatch, [(10, 20, 60, 120)], calls)
    frame = np.zeros((360, 640, 3), np.uint8)
    monkeypatch.setattr(main, "ai_frames", FixedFrames(frame))
    result = main.detect_objects_frame(5)
    assert result["ok"] and result["frame_source"] == "substream"
    assert result["frame"].shape == (360, 640, 3)
    assert result["frame_captured_at"] == datetime(2026, 10, 11, 12, 0, 0)
    assert [d["class_name"] for d in result["detections"]] == ["person"]
    assert live_hls_demand.state(5)["seconds_since_demand"] is None   # no HLS 'touch'
    assert not live_hls_demand.wanted(5)


def test_one_yolo_pass_per_shared_frame(monkeypatch):
    calls = []
    _yolo(monkeypatch, [(10, 20, 60, 120)], calls)
    frames = FixedFrames(np.zeros((360, 640, 3), np.uint8), seq=11)
    monkeypatch.setattr(main, "ai_frames", frames)
    first = main.detect_objects_frame(5)
    first["previous_frame_captured_at"] = "mutated by one caller"
    first["detections"][0]["x"] = -1
    second = main.detect_objects_frame(5)      # People Counting / Smart Rules on the same frame
    assert len(calls) == 1
    assert "previous_frame_captured_at" not in second and second["detections"][0]["x"] == 10
    frames.seq = 12
    main.detect_objects_frame(5)
    assert len(calls) == 2


def test_reader_not_ready_is_an_error_not_an_hls_read(monkeypatch):
    monkeypatch.setattr(main.cv2, "VideoCapture", lambda *a: pytest.fail("no HLS read while the reader starts"))
    _yolo(monkeypatch, [], [])
    monkeypatch.setattr(main, "ai_frames", FixedFrames(None, ok=False))
    result = main.detect_objects_frame(5)
    assert result["ok"] is False and "starting" in result["error"]


def test_hls_mode_is_the_previous_path_and_keeps_the_encoder(monkeypatch, tmp_path):
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    (tmp_path / "camera6.m3u8").write_text("#EXTM3U\n")
    calls = []
    _yolo(monkeypatch, [(10, 20, 60, 120)], calls)

    class Capture:
        def __init__(self, *a): pass
        def set(self, *a): pass
        def read(self): return True, np.zeros((2160, 3840, 3), np.uint8)
        def release(self): pass

    monkeypatch.setattr(main.cv2, "VideoCapture", Capture)
    monkeypatch.setattr(main.ai_frames, "mode", "hls")
    result = main.detect_objects_frame(6)
    assert result["ok"] and result["frame_source"] == "hls" and calls == [(2160, 3840, 3)]
    assert live_hls_demand.wanted(6)     # the HLS reader still keeps the encoder running


# ---------------------------------------------------------------- main-stream grab

def test_grab_command_single_frame_bmp():
    command = main_frame_grab.grab_command(MAIN_URL)
    assert "-frames:v" in command and command[command.index("-frames:v") + 1] == "1"
    assert command[-4:] == ["image2pipe", "-c:v", "bmp", "pipe:1"]


def test_grabber_caches_backs_off_and_prefers_the_event_buffer():
    clock = Clock()
    runs = []

    def run(args, **kwargs):
        runs.append(args)
        return types.SimpleNamespace(returncode=0, stdout=b"BM-fake")

    grabber = main_frame_grab.MainFrameGrabber(lambda n: MAIN_URL, run=run, decode=lambda raw: np.ones((2160, 3840, 3), np.uint8),
                                               clock=clock, enabled=True)
    assert grabber.grab(1).shape == (2160, 3840, 3)
    assert grabber.grab(1) is not None and len(runs) == 1          # cached for LPR + PPE + face in one scan
    clock.now += main_frame_grab.CACHE_SECONDS + 0.1
    grabber.grab(1)
    assert len(runs) == 2
    failing = main_frame_grab.MainFrameGrabber(lambda n: MAIN_URL, run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=b""),
                                               clock=clock, enabled=True)
    assert failing.grab(2) is None
    clock.now += main_frame_grab.CACHE_SECONDS + 0.1
    assert failing.grab(2) is None and failing.grabs == 1          # backing off, no new session
    buffer_frame = np.full((1944, 2592, 3), 5, np.uint8)
    event_mode = main_frame_grab.MainFrameGrabber(lambda n: pytest.fail("no RTSP grab in Event mode"),
                                                  buffer_frame=lambda n: buffer_frame, clock=clock, enabled=True)
    assert event_mode.grab(3) is buffer_frame
    disabled = main_frame_grab.MainFrameGrabber(lambda n: pytest.fail("disabled"), enabled=False)
    assert disabled.grab(4) is None


def test_full_resolution_crop_maps_the_analytics_box():
    analytics = np.zeros((360, 640, 3), np.uint8)
    full = np.zeros((2160, 3840, 3), np.uint8)
    full[600:1200, 1200:1800] = 255
    crop = main_frame_grab.full_resolution_crop(analytics, (200, 100, 100, 100), full, margin=0.0)
    assert crop.shape == (600, 600, 3) and crop.min() == 255
    assert main_frame_grab.full_resolution_crop(analytics, (200, 100, 100, 100), None).shape == (100, 100, 3)
    same = np.zeros((380, 660, 3), np.uint8)   # not clearly bigger: the analytics crop is used
    assert main_frame_grab.full_resolution_crop(analytics, (0, 0, 10, 10), same).shape == (10, 10, 3)


def _lpr_env(monkeypatch, *, mode="continuous", entitled=True):
    import lpr
    monkeypatch.setattr(lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(lpr, "LPR_FULL_RES_FRAMES", True)
    monkeypatch.setattr(lpr, "is_camera_enabled", lambda n: True)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: {"lpr_enabled": entitled})
    monkeypatch.setattr(main, "_local_recording_settings", lambda n: {"mode": mode})
    return lpr


def test_lpr_grabs_one_main_frame_only_when_a_vehicle_is_read_continuous_mode(monkeypatch):
    lpr = _lpr_env(monkeypatch)
    grabs = []
    full = np.zeros((2160, 3840, 3), np.uint8)
    monkeypatch.setattr(main, "ai_full_resolution_frame", lambda n, result: grabs.append(n) or full)
    seen = {}

    def scan_frame(camera, frame, boxes, full_frame=None, now=None):
        seen["callable"] = callable(full_frame)
        seen["full"] = full_frame() if callable(full_frame) else full_frame
        return []

    monkeypatch.setattr(lpr, "scan_frame", scan_frame)
    frame = np.zeros((360, 640, 3), np.uint8)
    car = {"class_name": "car", "x": 100, "y": 100, "width": 80, "height": 50}
    main.run_lpr_scan(1, {"frame": frame, "frame_source": "substream", "detections": [car]})
    assert seen["callable"] and seen["full"] is full and grabs == [1]
    seen.clear()
    main.run_lpr_scan(1, {"frame": frame, "frame_source": "substream", "detections": []})
    assert seen["full"] is None and grabs == [1]                   # no vehicle box: no grab
    grabs.clear()
    _lpr_env(monkeypatch, entitled=False)
    main.run_lpr_scan(1, {"frame": frame, "frame_source": "substream", "detections": [car]})
    assert grabs == []                                             # not entitled: nothing at all


def test_lpr_scan_frame_resolves_the_lazy_frame_only_for_an_attempted_vehicle(monkeypatch):
    import lpr
    monkeypatch.setattr(lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(lpr, "is_camera_enabled", lambda n: True)
    monkeypatch.setattr(lpr, "_due", lambda *a: True)
    monkeypatch.setattr(lpr, "should_attempt", lambda *a, **k: False)   # a parked car being skipped
    monkeypatch.setattr(lpr, "LPR_FULL_FRAME_FALLBACK", False)
    lpr.scan_frame(9, np.zeros((360, 640, 3), np.uint8), [(1, 1, 50, 50)], full_frame=lambda: pytest.fail("grabbed for a skipped vehicle"))
    calls = []
    monkeypatch.setattr(lpr, "should_attempt", lambda *a, **k: True)
    monkeypatch.setattr(lpr, "recognize_plate", lambda *a, **k: None)
    lpr.scan_frame(9, np.zeros((360, 640, 3), np.uint8), [(1, 1, 50, 50), (300, 100, 60, 60)],
                   full_frame=lambda: calls.append(1) or np.zeros((2160, 3840, 3), np.uint8))
    assert calls == [1]                                            # at most once per pass


def test_lpr_hls_source_keeps_the_previous_buffer_path(monkeypatch):
    lpr = _lpr_env(monkeypatch)
    buffer_calls = []
    monkeypatch.setattr(lpr, "latest_full_resolution_frame", lambda n, folder: buffer_calls.append(str(folder)) or None)
    monkeypatch.setattr(main, "ai_full_resolution_frame", lambda *a: pytest.fail("no RTSP grab on the HLS path"))
    monkeypatch.setattr(lpr, "scan_frame", lambda *a, **k: [])
    main.run_lpr_scan(1, {"frame": np.zeros((2160, 3840, 3), np.uint8), "frame_source": "hls", "detections": []})
    assert buffer_calls and buffer_calls[0].endswith("_event_buffer")


def test_ai_full_resolution_frame_skips_when_frame_is_already_full_res(monkeypatch):
    monkeypatch.setattr(main.main_frame_grabber, "grab", lambda n: "GRABBED")
    monkeypatch.setattr(main, "camera_profile", lambda n: types.SimpleNamespace(source={"width": 3840}))
    small = {"frame": np.zeros((360, 640, 3), np.uint8), "frame_source": "substream"}
    assert main.ai_full_resolution_frame(1, small) == "GRABBED"
    assert main.ai_full_resolution_frame(1, dict(small, frame_source="hls")) is None
    # A main-stream READER frame is scaled (<= 1280x720): crops still get the full frame.
    assert main.ai_full_resolution_frame(1, dict(small, frame_source="main")) == "GRABBED"
    full = {"frame": np.zeros((2160, 3840, 3), np.uint8), "frame_source": "main"}
    assert main.ai_full_resolution_frame(1, full) is None
    monkeypatch.setattr(main, "camera_profile", lambda n: types.SimpleNamespace(source={"width": 704}))
    assert main.ai_full_resolution_frame(1, small) is None


def test_analytics_source_spec_uses_measured_substream_with_credentials(monkeypatch):
    monkeypatch.setattr(main.ai_frames, "mode", "auto")  # substream policy is opt-in (ANYAICAM_AI_SOURCE=auto)
    import camera_capabilities
    import video_profile
    clean_main = camera_capabilities.split_credentials(MAIN_URL)[0]
    record = {"schema": 1, "video_probe": {"status": "ok", "at": "2026-10-11T00:00:00+00:00"}, "streams": [
        {"uri": clean_main, "role": "main", "measured": {"width": 3840, "height": 2160, "codec": "H265"}},
        {"uri": clean_main.replace("/101", "/102"), "role": "sub", "measured": {"width": 640, "height": 360, "codec": "H265"}}]}
    monkeypatch.setattr(main, "camera_url", lambda n: MAIN_URL)
    monkeypatch.setattr(main, "camera_video_profiles", types.SimpleNamespace(
        record=lambda n: record, profile=lambda n: video_profile.build(record)))
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", lambda url: pytest.fail("measured: no probe"))
    spec = main.analytics_source_spec(1)
    assert (spec.kind, spec.width, spec.height) == ("substream", 640, 360)
    assert spec.url.endswith("/102") and camera_capabilities.split_credentials(spec.url)[1:] == ("admin", SECRET)


def test_analytics_source_spec_unmeasured_camera_probes_the_substream_then_main(monkeypatch):
    monkeypatch.setattr(main.ai_frames, "mode", "auto")
    monkeypatch.setattr(main, "camera_url", lambda n: MAIN_URL)
    monkeypatch.setattr(main, "camera_video_profiles", types.SimpleNamespace(
        record=lambda n: None, profile=lambda n: main.video_profile.build(None)))
    monkeypatch.setattr(main, "motion_substream_url", lambda n: SUB_URL)
    probes = []

    def probe(url):
        probes.append(url[-3:])
        return None if url == SUB_URL else {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0}

    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", probe)
    spec = main.analytics_source_spec(1)
    assert probes == ["102", "101"] and (spec.kind, spec.width, spec.height) == ("main", 1280, 720)
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", lambda url: None)
    assert main.analytics_source_spec(1) is None                    # unreachable camera: no crash
    monkeypatch.setattr(main, "camera_url", lambda n: (_ for _ in ()).throw(RuntimeError("not configured")))
    assert main.analytics_source_spec(1) is None
