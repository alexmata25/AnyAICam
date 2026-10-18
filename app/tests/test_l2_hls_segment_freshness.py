"""L-2 segment freshness (Codex RC6 review, release blocker): a FRESH live playlist can list STALE or FUTURE-DATED
segments -- the files FFmpeg actually decodes. The playlist check alone let those frames reach YOLO and LPR, and an
implausible segment time silently became "now". The analytics reader now validates the segments FFmpeg will open (the
last three listed, FFmpeg's live start) before cv2.VideoCapture and refuses the frame instead of re-dating it.

No FFmpeg needed: real files with controlled mtimes; cv2.VideoCapture / YOLO / lpr.scan_frame are replaced by recorders
so the tests prove a refused segment never reaches them."""
import os
import time
from datetime import datetime, timedelta

import pytest

import detection_timing as dt

MAX_AGE = 15.0


def _playlist(folder, names, *, segment_mtimes, playlist_mtime=None, duration=2.0):
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(duration)}", "#EXT-X-MEDIA-SEQUENCE:0"]
    for name in names:
        lines += [f"#EXTINF:{duration:.6f},", name]
    manifest = folder / "camera9.m3u8"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, mtime in zip(names, segment_mtimes):
        if mtime is None:
            continue
        path = folder / name
        path.write_bytes(b"ts")
        os.utime(path, (mtime, mtime))
    now = time.time() if playlist_mtime is None else playlist_mtime
    os.utime(manifest, (now, now))
    return manifest


def _check(manifest, now):
    return dt.hls_live_segments_problem(manifest.parent, dt.parse_hls_segments(manifest.read_text(encoding="utf-8")),
                                        now=now, max_age=MAX_AGE)


NAMES = [f"camera9_{i:06d}.ts" for i in range(6)]


# ------------------------------------------------------------------ the validator


def test_a_live_playlist_with_current_segments_is_accepted_with_a_plausible_frame_time(tmp_path):
    now = time.time()
    manifest = _playlist(tmp_path, NAMES, segment_mtimes=[now - 2 * (5 - i) for i in range(6)])
    frame_time, problem = _check(manifest, now)
    assert problem is None
    # FFmpeg starts 3 segments back: newest end (now) minus 3 x 2 s
    assert abs((datetime.fromtimestamp(now) - frame_time).total_seconds() - 6.0) < 0.01


def test_a_fresh_playlist_listing_stale_segments_is_refused(tmp_path):
    now = time.time()
    manifest = _playlist(tmp_path, NAMES, segment_mtimes=[now - 600] * 6, playlist_mtime=now)
    frame_time, problem = _check(manifest, now)
    assert frame_time is None and "stale" in problem


def test_a_fresh_playlist_listing_future_dated_segments_is_refused(tmp_path):
    now = time.time()
    manifest = _playlist(tmp_path, NAMES, segment_mtimes=[now + 3600] * 6, playlist_mtime=now)
    frame_time, problem = _check(manifest, now)
    assert frame_time is None and "future-dated" in problem


def test_only_the_segments_ffmpeg_decodes_matter(tmp_path):
    """Old segments earlier in a long playlist are never opened (FFmpeg starts 3 from the end)."""
    now = time.time()
    mtimes = [now - 900, now - 900, now - 900, now - 4, now - 2, now]
    manifest = _playlist(tmp_path, NAMES, segment_mtimes=mtimes)
    assert _check(manifest, now)[1] is None


def test_one_stale_segment_among_the_decoded_ones_is_enough_to_refuse(tmp_path):
    now = time.time()
    mtimes = [now - 10, now - 8, now - 6, now - 300, now - 2, now]
    manifest = _playlist(tmp_path, NAMES, segment_mtimes=mtimes)
    assert "stale" in _check(manifest, now)[1]


def test_a_missing_decoded_segment_is_refused(tmp_path):
    now = time.time()
    manifest = _playlist(tmp_path, NAMES, segment_mtimes=[now] * 5 + [None])
    assert "missing" in _check(manifest, now)[1]


@pytest.mark.parametrize("name", ["../camera1_000001.ts", "sub/camera9_000001.ts", "C:camera9.ts", "..\\x.ts"])
def test_a_segment_reference_outside_the_folder_is_refused(tmp_path, name):
    now = time.time()
    folder = tmp_path / "hls"
    manifest = _playlist(folder, [name], segment_mtimes=[None])
    assert "unexpected segment reference" in _check(manifest, now)[1]


def test_small_clock_skew_is_still_live(tmp_path):
    """The existing L-2 rule: a writer slightly ahead (3 s) still counts as live."""
    now = time.time()
    manifest = _playlist(tmp_path, NAMES[:2], segment_mtimes=[now + 3, now + 3])
    frame_time, problem = _check(manifest, now)
    assert problem is None and frame_time <= datetime.fromtimestamp(now)


def test_an_implausible_frame_time_is_refused_not_replaced_by_now(tmp_path):
    """Segments 10 s ahead pass the symmetric file bound, but the frame FFmpeg returns would be dated ~6 s in the
    future: refused, never re-dated to 'now'."""
    now = time.time()
    manifest = _playlist(tmp_path, NAMES[:2], segment_mtimes=[now + 10, now + 10])
    frame_time, problem = _check(manifest, now)
    assert frame_time is None and "future" in problem


def test_an_empty_playlist_is_refused(tmp_path):
    now = time.time()
    manifest = _playlist(tmp_path, [], segment_mtimes=[])
    assert "no segments" in _check(manifest, now)[1]


# ------------------------------------------------------------------ the reader: refused before VideoCapture


@pytest.fixture()
def live(tmp_path, monkeypatch):
    import live_hls_demand
    import main
    folder = tmp_path / "hls"
    folder.mkdir()
    monkeypatch.setattr(main, "HLS_FOLDER", folder)
    monkeypatch.setattr(live_hls_demand, "on_demand", lambda: False)
    opened = []

    class FakeCapture:
        def __init__(self, path):
            opened.append(path)

        def set(self, *_):
            return True

        def read(self):
            import numpy
            return True, numpy.zeros((360, 640, 3), dtype="uint8")

        def release(self):
            pass

    monkeypatch.setattr(main.cv2, "VideoCapture", FakeCapture)
    return folder, opened


@pytest.mark.parametrize("label,offset", [("stale", -600), ("future", 3600)])
def test_the_reader_refuses_before_videocapture(live, label, offset):
    import main
    folder, opened = live
    now = time.time()
    _playlist(folder, NAMES, segment_mtimes=[now + offset] * 6, playlist_mtime=now)  # fresh playlist
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert frame is None and captured_at is None
    assert error == {"ok": False, "error": "Live video is not arriving from this camera.", "detections": []}
    assert opened == []  # cv2.VideoCapture was never opened on the playlist


def test_the_reader_still_reads_a_genuinely_live_playlist(live):
    import main
    folder, opened = live
    now = time.time()
    _playlist(folder, NAMES, segment_mtimes=[now - 2 * (5 - i) for i in range(6)])
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert error is None and frame is not None and len(opened) == 1
    assert captured_at is not None and timedelta(0) <= datetime.now() - captured_at < timedelta(seconds=30)


@pytest.mark.parametrize("offset", [-600, 3600])
def test_a_refused_segment_never_reaches_yolo_or_lpr(live, monkeypatch, offset):
    import lpr
    import main
    folder, opened = live
    now = time.time()
    _playlist(folder, NAMES, segment_mtimes=[now + offset] * 6, playlist_mtime=now)
    monkeypatch.setattr(main.ai_frames, "get_frame", lambda camera_number, wait=None: None)  # forces the HLS reader
    calls = []
    monkeypatch.setattr(main, "get_yolo_model", lambda: calls.append("yolo") or (_ for _ in ()).throw(AssertionError("YOLO ran")))
    monkeypatch.setattr(lpr, "scan_frame", lambda *a, **k: calls.append("lpr") or [])
    monkeypatch.setattr(lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(lpr, "is_camera_enabled", lambda camera_number: True)
    result = main.detect_objects_frame(9)
    assert result["ok"] is False and result.get("frame") is None
    assert main.run_lpr_scan(9, result) == []
    assert calls == [] and opened == []
