"""ANYAICAM_AI_SOURCE=main -- the release-candidate default (owner rule:
never silently sacrifice detection quality). The persistent AI reader reads
the camera's MAIN stream at its native resolution
(ANYAICAM_AI_MAIN_READER_SIZE=native, default): YOLO gets exactly the frame
it gets today (validated: 501/501 frames pixel-identical to the 4K decode,
identical detections), without the live HLS encoder.
ANYAICAM_AI_MAIN_READER_SIZE=analytics scales into the analytics box (fits
1280x720) -- cheaper but measurably different (~85 % box agreement).
auto/substream stay opt-in; hls is the exact rollback."""
import shutil
import subprocess
import types

import numpy as np
import pytest

import analytics_frames
import camera_capabilities
import camera_video_probe
import live_hls_demand
import video_profile

SECRET = "main-pw"
MAIN_URL = f"rtsp://admin:{SECRET}@192.0.2.50:554/Streaming/Channels/101"
CLEAN_MAIN = camera_capabilities.split_credentials(MAIN_URL)[0]


def _record(main=(3840, 2160), sub=(640, 360), status="ok"):
    return {"schema": 1, "probed_at": "2099-01-01T00:00:00+00:00",
            "video_probe": {"status": status, "at": "2099-01-01T00:00:00+00:00"},
            "streams": [{"uri": CLEAN_MAIN, "role": "main", "measured": {"width": main[0], "height": main[1], "codec": "H265"}},
                        {"uri": CLEAN_MAIN.replace("/101", "/102"), "role": "sub",
                         "measured": {"width": sub[0], "height": sub[1], "codec": "H265"}}]}


def test_default_mode_is_main(monkeypatch):
    monkeypatch.delenv("ANYAICAM_AI_SOURCE", raising=False)
    assert analytics_frames.configured_mode() == "main"
    assert analytics_frames.configured_mode("MAIN") == "main"
    assert analytics_frames.configured_mode("bogus") == "hls"          # unknown = the previous behaviour
    import main
    assert main.ai_frames.mode == analytics_frames.AI_SOURCE


@pytest.mark.parametrize("width,height,expected", [
    (3840, 2160, (1280, 720)), (2592, 1944, (960, 720)), (4000, 3000, (960, 720)), (1920, 1080, (1280, 720)),
    (1280, 720, (1280, 720)), (704, 576, (704, 576)), (2880, 2880, (1280, 1280)),
])
def test_main_mode_analytics_size_scales_into_the_analytics_box(monkeypatch, width, height, expected):
    import main
    record = _record(main=(width, height))
    monkeypatch.setattr(main.ai_frames, "mode", "main")
    monkeypatch.setattr(analytics_frames, "MAIN_READER_SIZE", "analytics")
    monkeypatch.setattr(main, "camera_url", lambda n: MAIN_URL)
    monkeypatch.setattr(main, "camera_video_profiles", types.SimpleNamespace(
        record=lambda n: record, profile=lambda n: video_profile.build(record)))
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", lambda url: pytest.fail("measured: no probe"))
    monkeypatch.setattr(main, "motion_substream_url", lambda n: pytest.fail("main mode never uses the substream"))
    spec = main.analytics_source_spec(1)
    assert spec.url == MAIN_URL and spec.kind == "main" and spec.reason == "main_by_config" and not spec.native
    assert (spec.width, spec.height) == expected


@pytest.mark.parametrize("width,height", [(3840, 2160), (2592, 1944), (4000, 3000), (1920, 1080), (704, 576)])
def test_main_mode_default_reads_the_main_stream_at_native_resolution(monkeypatch, width, height):
    import main
    record = _record(main=(width, height))
    monkeypatch.delenv("ANYAICAM_AI_MAIN_READER_SIZE", raising=False)
    assert analytics_frames.MAIN_READER_SIZE == "native"
    monkeypatch.setattr(main.ai_frames, "mode", "main")
    monkeypatch.setattr(main, "camera_url", lambda n: MAIN_URL)
    monkeypatch.setattr(main, "camera_video_profiles", types.SimpleNamespace(
        record=lambda n: record, profile=lambda n: video_profile.build(record)))
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", lambda url: pytest.fail("measured: no probe"))
    spec = main.analytics_source_spec(1)
    assert (spec.url, spec.kind, spec.native, spec.width, spec.height) == (MAIN_URL, "main", True, width, height)
    command = analytics_frames.reader_command(spec.url, 2.0, spec.width, spec.height, native=spec.native)
    assert command[command.index("-vf") + 1] == "fps=2"                 # no scaling at all
    # A native frame is the full-resolution picture: no extra main grab for crops.
    monkeypatch.setattr(main.main_frame_grabber, "grab", lambda n: pytest.fail("no grab for a native frame"))
    result = {"frame": np.zeros((height, width, 3), np.uint8), "frame_source": "main"}
    assert main.ai_full_resolution_frame(1, result) is None


def test_native_read_of_a_configured_only_camera_probes_the_true_size(monkeypatch):
    import main
    record = _record(main=(1920, 1080), status="configured")
    for stream in record["streams"]:
        stream["width"], stream["height"] = stream["measured"]["width"], stream["measured"]["height"]
        del stream["measured"]
    monkeypatch.setattr(main.ai_frames, "mode", "main")
    monkeypatch.setattr(main, "camera_url", lambda n: MAIN_URL)
    monkeypatch.setattr(main, "camera_video_profiles", types.SimpleNamespace(
        record=lambda n: record, profile=lambda n: video_profile.build(record)))
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", lambda url: {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0})
    spec = main.analytics_source_spec(1)
    assert (spec.width, spec.height, spec.native) == (3840, 2160, True)   # never the configured 1920x1080


def test_main_mode_unmeasured_camera_probes_the_main_stream(monkeypatch):
    import main
    monkeypatch.setattr(main.ai_frames, "mode", "main")
    monkeypatch.setattr(main, "camera_url", lambda n: MAIN_URL)
    monkeypatch.setattr(main, "camera_video_profiles", types.SimpleNamespace(
        record=lambda n: None, profile=lambda n: video_profile.build(None)))
    probed = []
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream",
                        lambda url: probed.append(url) or {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0})
    spec = main.analytics_source_spec(1)
    assert probed == [MAIN_URL] and (spec.kind, spec.width, spec.height, spec.native) == ("main", 3840, 2160, True)
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", lambda url: None)
    assert main.analytics_source_spec(1) is None                          # unreachable: no crash


def test_main_mode_falls_back_to_hls_after_failures_like_auto():
    class Stub:
        kind, size, last_error, reason = "main", (1280, 720), "stream ended", "main_by_config"

        def ensure_running(self): pass
        def latest(self, max_age=None, wait=0.0): return None
        def stop(self): self.stopped = True
        def restart(self): pass
        def housekeeping(self): pass

    stub = Stub()
    frames = analytics_frames.AnalyticsFrames(lambda n: None, mode="main", reader_factory=lambda n: stub,
                                              log=lambda m: None, start_housekeeping=False)
    assert frames.get_frame(1, wait=0)["ok"] is False
    for _ in range(analytics_frames.MAX_FAILURES):
        frames._note_run(1, 0, "stream ended")
    assert frames.get_frame(1, wait=0) is None and frames.uses_hls(1)


def test_hls_is_the_exact_rollback(monkeypatch, tmp_path):
    import main
    live_hls_demand.configure(mode="on_demand", idle_seconds=60)
    try:
        monkeypatch.setattr(main.ai_frames, "mode", "hls")
        monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
        monkeypatch.setattr(main, "YOLO", object())
        result = main.detect_objects_frame(3)
        assert result["ok"] is False and live_hls_demand.wanted(3)       # the old HLS path, keeping the encoder
    finally:
        live_hls_demand.configure(mode="always")


def test_main_mode_keeps_on_demand_hls_effective(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    (tmp_path / live_hls_demand.AGENT_MARKER).touch()
    monkeypatch.setattr(main.ai_frames, "mode", "main")
    monkeypatch.setattr(main._motion_source_selector, "mode", "auto")
    assert main._live_hls_auto_policy() is True


def test_main_mode_reports_main_by_config(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main.ai_frames, "mode", "main")
    assert main._effective_analytics_choice() == "main_by_config"
    camera_video_probe.write_status(tmp_path, 2, _record(), main._effective_analytics_choice())
    import json
    assert json.loads((tmp_path / "camera2.video.json").read_text())["analytics_choice"] == "main_by_config"
    monkeypatch.setattr(main.ai_frames, "mode", "auto")
    assert main._effective_analytics_choice() is None


def test_reader_scale_uses_area_interpolation_by_default():
    command = analytics_frames.reader_command(MAIN_URL, 2.0, 1280, 720)
    assert command[command.index("-vf") + 1] == f"fps=2,scale=1280:720:flags={analytics_frames.READER_SCALE_FLAGS}"
    assert analytics_frames.READER_SCALE_FLAGS == "area"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_native_main_reader_frames_equal_the_opencv_decode(tmp_path):
    import cv2
    source = tmp_path / "main.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=2560x1440:rate=2", "-t", "3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "1", str(source)], check=True, timeout=180)
    reader = analytics_frames.FrameReader(1, lambda n: analytics_frames.SourceSpec(str(source), 2560, 1440, "main", "main_by_config", native=True),
                                          fps=2.0, log=lambda m: None)
    reader.ensure_running()
    got = reader.latest(max_age=60, wait=60)
    reader.stop()
    assert got is not None and got[0].shape == (1440, 2560, 3)
    capture = cv2.VideoCapture(str(source))
    decoded = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        decoded.append(frame)
    capture.release()
    assert any(np.array_equal(got[0], frame) for frame in decoded)       # pixel-identical to what YOLO gets today


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_main_reader_scales_a_4k_source_to_1280x720(tmp_path):
    source = tmp_path / "main.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=10", "-t", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(source)], check=True, timeout=180)
    reader = analytics_frames.FrameReader(1, lambda n: analytics_frames.SourceSpec(str(source), 1280, 720, "main", "main_by_config"),
                                          fps=2.0, log=lambda m: None)
    reader.ensure_running()
    got = reader.latest(max_age=60, wait=60)
    reader.stop()
    assert got is not None and got[0].shape == (720, 1280, 3)
