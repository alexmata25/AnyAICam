"""L-1 (2026-10-17): LPR must not keep decoding a stopped camera's last
recording-buffer segment.

Seen live on the Ryzen: after the driveway cameras stopped, every AI pass
still ran `ffmpeg -sseof -1.5 -i buf3_...mkv -vcodec png` on camera 3's last
30-second segment (lpr.latest_full_resolution_frame), once per cache period,
indefinitely -- full-resolution PNG decodes plus LPR on a frozen picture,
with the box at ~80-90 C. A segment older than LPR_FULL_RES_MAX_AGE_SECONDS
is now skipped, so a stopped camera costs nothing and LPR gets no stale frame;
a live camera behaves exactly as before.
"""
import os

import cv2
import numpy as np
import pytest

import lpr


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    lpr._full_frame_cache.clear()
    monkeypatch.setattr(lpr, "LPR_FULL_RES_FRAMES", True)
    monkeypatch.setattr(lpr, "LPR_FULL_RES_MAX_AGE_SECONDS", 10.0)
    yield
    lpr._full_frame_cache.clear()


def _png(shade=128):
    ok, data = cv2.imencode(".png", np.full((1080, 1920, 3), shade, np.uint8))
    assert ok
    return data.tobytes()


class _Ffmpeg:
    """Stands in for subprocess.run: records which segment was decoded and
    returns a real PNG (or nothing, for an undecodable segment)."""

    def __init__(self, undecodable=()):
        self.calls, self.undecodable = [], set(undecodable)

    def __call__(self, args, **kwargs):
        path = args[args.index("-i") + 1]
        self.calls.append(os.path.basename(path))
        return type("R", (), {"stdout": b"" if os.path.basename(path) in self.undecodable else _png()})()


def _segment(folder, name, mtime):
    path = folder / name
    path.write_bytes(b"mkv")
    os.utime(path, (mtime, mtime))
    return path


WALL = 1_800_000_000.0


def test_a_stopped_cameras_last_segment_is_never_decoded(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    _segment(tmp_path, "buf3_2026-10-17_00-30-05.mkv", WALL - 791)  # the Ryzen case: 13 minutes old
    for tick in range(200):  # ~10 minutes of AI passes, every cache period expired
        assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=tick * 3.0, wall_now=WALL + tick * 3.0) is None
    assert ffmpeg.calls == []


def test_a_live_camera_is_decoded_exactly_as_before(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    _segment(tmp_path, "buf2_a.mkv", WALL - 31)  # previous, finished segment
    _segment(tmp_path, "buf2_b.mkv", WALL - 1)   # in progress
    frame = lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=WALL)
    assert frame is not None and frame.shape == (1080, 1920, 3)
    assert ffmpeg.calls == ["buf2_b.mkv"]  # the newest one, as before


def test_the_previous_segment_is_still_used_while_a_new_one_starts(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg(undecodable={"buf2_b.mkv"})  # just rolled over: nothing decodable yet
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    _segment(tmp_path, "buf2_a.mkv", WALL - 2)
    _segment(tmp_path, "buf2_b.mkv", WALL - 0.5)
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=WALL) is not None
    assert ffmpeg.calls == ["buf2_b.mkv", "buf2_a.mkv"]


def test_a_stale_previous_segment_is_not_a_fallback(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg(undecodable={"buf2_b.mkv"})
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    _segment(tmp_path, "buf2_a.mkv", WALL - 600)
    _segment(tmp_path, "buf2_b.mkv", WALL - 1)
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=WALL) is None
    assert ffmpeg.calls == ["buf2_b.mkv"]


def test_decoding_stops_when_the_camera_stops_and_resumes_when_it_restarts(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    seg = _segment(tmp_path, "buf3_live.mkv", WALL)
    # live: one decode per cache period
    for tick in range(4):
        assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=tick * 3.0, wall_now=WALL + 0.5) is not None
    assert len(ffmpeg.calls) == 4
    # the camera stops: its segment ages past the bound, decoding stops
    for tick in range(4, 40):
        assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=tick * 3.0, wall_now=WALL + 60 + tick) is None
    assert len(ffmpeg.calls) == 4
    # the camera comes back: a fresh segment is decoded again
    os.utime(seg, (WALL + 200, WALL + 200))
    assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=500.0, wall_now=WALL + 201) is not None
    assert len(ffmpeg.calls) == 5


def test_the_disabled_switch_still_wins(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    monkeypatch.setattr(lpr, "LPR_FULL_RES_FRAMES", False)
    _segment(tmp_path, "buf2_b.mkv", WALL)
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=WALL) is None
    assert ffmpeg.calls == []


def test_the_lpr_scan_gets_no_full_frame_from_a_stopped_camera(monkeypatch, tmp_path):
    """The caller's view (main.run_lpr_scan, HLS path): a stale buffer hands
    scan_frame no full-resolution frame, so it reads plates only on what the
    analytics frame offers -- never on a frozen high-resolution picture."""
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    _segment(tmp_path, "buf3_old.mkv", WALL - 300)
    assert lpr.latest_full_resolution_frame(3, str(tmp_path), wall_now=WALL) is None
    assert ffmpeg.calls == []
