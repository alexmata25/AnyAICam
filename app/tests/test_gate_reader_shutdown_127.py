"""1.2.7 combined check (Yellow, 2026-10-17): with Red's activation gate
(904594d / 73665f0) and Yellow's recovery fix together, a camera with no
effectively active analytic ends up with NO full-resolution AI reader --
the Ryzen case: driveway cameras 2/3 had ppe_enabled=1 while PPE was off
appliance-wide, and their 1440p readers ran at 20-28 % CPU each.

Chain proven here with the real AnalyticsFrames + FrameReader (ffmpeg
replaced by a fake process): gate says nothing is active -> the detector
asks for no frame (Red's test) -> after IDLE_STOP_SECONDS the shared
housekeeping kills the reader's ffmpeg; turning an analytic on again
starts a fresh reader; recovery clears the HLS fallback but never starts
a reader by itself.
"""
import io
import threading
import time

import pytest

import analytics_frames
import main

URL = "rtsp://u:p@192.168.0.43:554/Streaming/Channels/101"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeProcess:
    """One 8x6 BGR frame, then blocks like a live camera until killed."""

    def __init__(self):
        self._data = io.BytesIO(b"\x07" * (8 * 6 * 3))
        self._block = threading.Event()
        self.killed = False
        self.returncode = None
        self.stdout = self

    def read(self, n):
        chunk = self._data.read(n)
        if not chunk:
            self._block.wait(10)
            return b""
        return chunk

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._block.set()

    terminate = kill

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture()
def frames():
    clock = Clock()
    processes = []

    def popen(command, **kwargs):
        processes.append(FakeProcess())
        return processes[-1]

    spec = analytics_frames.SourceSpec(URL, 8, 6, "main")

    def factory(camera_number):
        return analytics_frames.FrameReader(camera_number, lambda n: spec, popen=popen, fps=2.0, clock=clock,
                                            log=lambda m: None)

    f = analytics_frames.AnalyticsFrames(lambda n: spec, mode="main", reader_factory=factory, clock=clock,
                                         log=lambda m: None, start_housekeeping=False)
    yield f, clock, processes
    for p in processes:
        p.kill()


RYZEN_CAM23 = {"smart_motion_enabled": 0, "people_counting_enabled": 0, "lpr_enabled": 0, "ppe_enabled": 1,
               "facial_recognition_enabled": 0}


def test_the_ryzen_case_ends_with_no_full_resolution_reader(frames, monkeypatch):
    f, clock, processes = frames
    monkeypatch.setattr(main.ppe, "PPE_ENABLED", False)
    # Before the change: a detector pass asked for a frame, so a reader runs.
    got = f.get_frame(2, wait=5)
    assert got and got["ok"] and len(processes) == 1 and not processes[0].killed
    # The gate: nothing is effectively active, so the detector pauses and asks for no frame.
    assert main.effective_analytics(RYZEN_CAM23, 2) == []
    clock.now += analytics_frames.IDLE_STOP_SECONDS + 1
    f.housekeeping()
    assert processes[0].killed  # the 1440p decode is gone


def test_an_effective_analytic_keeps_its_reader_and_a_new_one_starts_on_demand(frames, monkeypatch):
    f, clock, processes = frames
    monkeypatch.setattr(main.lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(main.lpr, "LPR_CAMERAS", None)
    identity = dict(RYZEN_CAM23, ppe_enabled=0, lpr_enabled=1)
    assert main.effective_analytics(identity, 2) == ["lpr_enabled"]
    for _ in range(5):  # the detector keeps asking: the reader is never idle
        assert f.get_frame(2, wait=5)["ok"]
        clock.now += analytics_frames.IDLE_STOP_SECONDS / 2
        f.housekeeping()
    assert len(processes) == 1 and not processes[0].killed
    clock.now += analytics_frames.IDLE_STOP_SECONDS + 1  # LPR switched off: no more requests
    f.housekeeping()
    assert processes[0].killed
    assert f.get_frame(2, wait=5)["ok"]  # switched on again: a fresh reader
    assert len(processes) == 2 and not processes[1].killed


def test_recovery_clears_the_fallback_but_never_starts_a_reader(frames, monkeypatch):
    f, clock, processes = frames
    f._hls_until[2] = clock.now + 1800
    monkeypatch.setattr(main, "ai_frames", f)
    main.note_live_stream_recovered(2)
    assert f.uses_hls(2) is False
    assert processes == []  # no reader without a detector request
