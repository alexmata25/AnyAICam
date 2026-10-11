"""Codex final review (2026-10-11): camera<N>.alive -- which keeps an idle
on-demand camera "online" locally and in the cloud -- must require INCOMING
video evidence, never mere process liveness. A recorder whose ffmpeg is up
but whose stream stalled must let the camera go offline."""
import asyncio
import os
import time

import pytest

import live_hls_demand
import main


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _on_demand():
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=Clock())
    yield
    live_hls_demand.configure(mode="always")


def _recordings(tmp_path, monkeypatch, *, mtime_age=None):
    recordings = tmp_path / "recordings"
    camera = recordings / "camera2"
    camera.mkdir(parents=True)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings)
    if mtime_age is not None:
        segment = camera / "camera2_2026-10-11_10-00-00.mkv"
        segment.write_bytes(b"x" * 100)
        stamp = time.time() - mtime_age
        os.utime(segment, (stamp, stamp))
    return camera


def _run_idle_wait(monkeypatch, tmp_path, rounds=1):
    """Runs _wait_for_live_hls_demand() for `rounds` alive checks, with a
    recorder process reported as running the whole time."""
    hls = tmp_path / "hls"
    hls.mkdir(exist_ok=True)
    monkeypatch.setattr(main, "HLS_FOLDER", hls)
    monkeypatch.setitem(main.camera_process_state, 2, {"recording": "running", "live": "idle"})
    checks = {"n": 0}

    def wanted(number):
        checks["n"] += 1
        return checks["n"] > rounds

    monkeypatch.setattr(live_hls_demand, "wanted", wanted)
    monkeypatch.setattr(live_hls_demand, "IDLE_POLL_SECONDS", 0)
    asyncio.run(main._wait_for_live_hls_demand(2))
    return hls / "camera2.alive"


def test_stalled_recorder_process_does_not_refresh_alive(monkeypatch, tmp_path):
    _recordings(tmp_path, monkeypatch, mtime_age=120)            # ffmpeg up, file not written for 2 min
    monkeypatch.setattr(main.ai_frames, "last_frame_age", lambda n: None)   # no AI frames
    alive = _run_idle_wait(monkeypatch, tmp_path)
    assert not alive.exists()                                    # never written


def test_stalled_camera_goes_offline_in_the_agent_after_its_window(monkeypatch, tmp_path):
    from anyaicam_agent.camera_binding import LocalVmsStatusReader
    _recordings(tmp_path, monkeypatch, mtime_age=120)
    monkeypatch.setattr(main.ai_frames, "last_frame_age", lambda n: None)
    hls = tmp_path / "hls"
    hls.mkdir()
    alive = hls / "camera2.alive"
    alive.touch()                                                # last refreshed while video still arrived
    old = time.time() - 30
    os.utime(alive, (old, old))
    _run_idle_wait(monkeypatch, tmp_path)
    assert alive.stat().st_mtime == pytest.approx(old, abs=1)    # not refreshed by the stalled recorder
    status = LocalVmsStatusReader(hls, tmp_path / "recordings", stream_freshness_seconds=20).status(2)
    assert status["online"] is False


def test_motion_reader_frames_refresh_alive(monkeypatch, tmp_path):
    _recordings(tmp_path, monkeypatch, mtime_age=120)
    monkeypatch.setattr(main.ai_frames, "last_frame_age", lambda n: None)
    live_hls_demand.note_source_frame(2)                         # a substream frame just arrived
    assert _run_idle_wait(monkeypatch, tmp_path).exists()


def test_ai_reader_frames_refresh_alive(monkeypatch, tmp_path):
    _recordings(tmp_path, monkeypatch, mtime_age=120)
    monkeypatch.setattr(main.ai_frames, "last_frame_age", lambda n: 2.0)
    assert _run_idle_wait(monkeypatch, tmp_path).exists()


def test_growing_recorder_file_refreshes_alive(monkeypatch, tmp_path):
    _recordings(tmp_path, monkeypatch, mtime_age=3)              # written 3 s ago
    monkeypatch.setattr(main.ai_frames, "last_frame_age", lambda n: None)
    assert _run_idle_wait(monkeypatch, tmp_path).exists()


def test_event_mode_buffer_file_counts_and_other_cameras_do_not(tmp_path):
    camera = tmp_path / "camera2"
    buffer = camera / "_event_buffer"
    buffer.mkdir(parents=True)
    other = camera / "camera12_2026-10-11_10-00-00.mkv"          # camera 12, not camera 2
    other.write_bytes(b"x")
    assert not live_hls_demand.recorder_output_advancing((camera, buffer), 2)
    (buffer / "buf2_2026-10-11_10-00-00.mkv").write_bytes(b"x")
    assert live_hls_demand.recorder_output_advancing((camera, buffer), 2)
    assert not live_hls_demand.recorder_output_advancing((tmp_path / "missing",), 2)


def test_old_frames_do_not_count():
    clock = Clock()
    live_hls_demand.configure(mode="on_demand", idle_seconds=60, clock=clock)
    live_hls_demand.note_source_frame(5)
    assert live_hls_demand.camera_reachable(5)
    clock.now += live_hls_demand.REACHABLE_FRAME_SECONDS + 1
    assert not live_hls_demand.camera_reachable(5)
    assert live_hls_demand.camera_reachable(5, receiving_video=True)


def test_ai_reader_last_frame_age():
    import analytics_frames

    class Reader:
        _frame_mono = 990.0

    clock = Clock(1000.0)
    frames = analytics_frames.AnalyticsFrames(lambda n: None, mode="main", clock=clock, start_housekeeping=False)
    assert frames.last_frame_age(3) is None
    frames._readers[3] = Reader()
    assert frames.last_frame_age(3) == 10.0
