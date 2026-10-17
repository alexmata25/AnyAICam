"""Codex 2026-10-17 finding 1: when every analytic on a camera is switched off, its analytics reader's ffmpeg must
stop promptly -- not keep decoding until the reader's IDLE_STOP_SECONDS (120 s) idle stop.

AnalyticsFrames.release() stops one camera's reader now, keeping its generation / fallback state (unlike forget());
the detector calls it once, on the transition into pause_reason "analytics_disabled"."""
import time

import pytest

import analytics_frames
import main
from test_ai_activation_gate_127 import ALL_OFF, globals_off  # noqa: F401 -- fixture
from test_ai_substream_frames import FakeProcess, _reader


def _frames_with(reader):
    return analytics_frames.AnalyticsFrames(lambda n: None, mode="substream", reader_factory=lambda n: reader,
                                            start_housekeeping=False)


def test_release_kills_the_running_ffmpeg_immediately_without_waiting_for_the_idle_stop():
    process = FakeProcess(8, 6, 1, block_after=True)
    reader = _reader([process, FakeProcess(8, 6, 1, block_after=True)])
    frames = _frames_with(reader)
    assert frames.get_frame(3, wait=5)["ok"]
    generation = frames.generation(3)
    assert frames.release(3) is True
    deadline = time.monotonic() + 5
    while not process.killed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.killed                                  # no clock advance: not the 120 s idle stop
    assert frames.generation(3) == generation and not reader.retired
    reader._thread.join(5)
    frames.get_frame(3, wait=5)                            # analytics back on: a fresh ffmpeg starts
    deadline = time.monotonic() + 5
    while len(reader.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(reader.calls) == 2
    reader.stop()


def test_release_keeps_the_cameras_fallback_window_and_ignores_unknown_cameras():
    reader = _reader([FakeProcess(8, 6, 1, block_after=True)])
    frames = _frames_with(reader)
    assert frames.release(9) is False                      # no reader: nothing to do
    frames._hls_until[3] = time.monotonic() + 600
    frames._failures[3] = 3
    frames.get_frame(3)
    frames.release(3)
    assert 3 in frames._hls_until and frames._failures[3] == 3   # unlike forget()
    reader.stop()


# ---------------------------------------------------------------- the detector calls it once, on the switch-off
def _run_detector(monkeypatch, identity, *, already_paused=False, release=None):
    class Stop(BaseException):
        pass

    released = []

    async def no_sleep(_seconds):
        raise Stop()

    async def direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    class ImmediateSemaphore:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Frames:
        def release(self, camera):
            released.append(camera)
            if release is not None:
                return release(camera)
            return True

    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(main, "cv2", object())
    monkeypatch.setattr(main, "YOLO", object())
    monkeypatch.setattr(main, "get_yolo_model", lambda: None)
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (30.0, 300.0))
    monkeypatch.setattr(main, "detect_objects_frame",
                        lambda camera: {"ok": True, "frame": None, "frame_captured_at": None, "detections": []})
    monkeypatch.setattr(main, "save_yolo_events", lambda camera, result: [])
    monkeypatch.setattr(main.asyncio, "to_thread", direct)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main, "ai_inference_semaphore", ImmediateSemaphore())
    monkeypatch.setattr(main, "_ai_event_media_loop", object())
    monkeypatch.setattr(main, "ai_frames", Frames())
    state = main._new_ai_state()
    if already_paused:
        state.update(status="paused", pause_reason="analytics_disabled")
    monkeypatch.setitem(main.ai_detection_state, 7, state)
    monkeypatch.setitem(main.ai_person_last_event, 7, -1e18)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: dict(identity, camera_id="cam-7"))
    detector = main.ai_person_detector(7)
    try:
        detector.send(None)
    except Stop:
        pass
    finally:
        detector.close()
    return released, main.ai_detection_state[7]


def test_switching_every_analytic_off_releases_the_reader_at_once(globals_off):
    released, state = _run_detector(globals_off, {**ALL_OFF, "ppe_enabled": True})  # the Ryzen case: PPE off
    assert released == [7] and state["pause_reason"] == "analytics_disabled"


def test_an_already_paused_camera_is_not_released_again_every_pass(globals_off):
    released, _state = _run_detector(globals_off, {**ALL_OFF, "ppe_enabled": True}, already_paused=True)
    assert released == []


def test_a_camera_with_an_effective_analytic_keeps_its_reader(globals_off):
    globals_off.setattr(main.lpr, "LPR_ENABLED", True)
    released, state = _run_detector(globals_off, {**ALL_OFF, "lpr_enabled": True})
    assert released == [] and state.get("pause_reason") is None


def test_a_failing_release_never_breaks_the_detector(globals_off):
    def boom(camera):
        raise RuntimeError("reader gone")

    released, state = _run_detector(globals_off, {**ALL_OFF}, release=boom)
    assert released == [7] and state["pause_reason"] == "analytics_disabled"
