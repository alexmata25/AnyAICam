"""L-1/L-2 follow-up (Codex review of 9f77aaf, 2026-10-17): future-dated
recording segments and live playlists.

Both freshness bounds measured "now - mtime <= bound". A file whose mtime is
in the future (the clock was stepped back after it was written -- e.g. an
appliance that booted with its clock ahead and was then corrected by NTP, or
a recordings share whose server clock runs ahead) has a negative age, so it
passed as fresh: a stopped camera's last segment / a disconnected camera's
last playlist was analysed again until the wall clock caught up -- the exact
L-1/L-2 CPU and heat condition, for as long as the skew lasted.

The bound is now symmetric: |now - mtime| <= bound. A writer slightly ahead
(within the same bound) still counts as live, a far-future file is stale, and
after a clock step a camera's newly written segment / rewritten playlist is
used again at once.
"""
import os
import subprocess
import time

import pytest

cv2 = pytest.importorskip("cv2")
import numpy as np

import live_hls_demand
import lpr

WALL = 1_800_000_000.0
HOUR = 3600.0


# ---------------------------------------------------------------- L-1 (lpr)
@pytest.fixture()
def lpr_env(monkeypatch):
    lpr._full_frame_cache.clear()
    monkeypatch.setattr(lpr, "LPR_FULL_RES_FRAMES", True)
    monkeypatch.setattr(lpr, "LPR_FULL_RES_MAX_AGE_SECONDS", 10.0)
    calls = []

    def ffmpeg(args, **kwargs):
        calls.append(os.path.basename(args[args.index("-i") + 1]))
        ok, data = cv2.imencode(".png", np.full((1080, 1920, 3), 90, np.uint8))
        return type("R", (), {"stdout": data.tobytes()})()

    monkeypatch.setattr(lpr.subprocess, "run", ffmpeg)
    yield calls
    lpr._full_frame_cache.clear()


def _segment(folder, name, mtime):
    path = folder / name
    path.write_bytes(b"mkv")
    os.utime(path, (mtime, mtime))
    return path


def test_a_stopped_cameras_future_dated_segment_is_never_decoded(lpr_env, tmp_path):
    # Written while the clock was an hour ahead; the clock was corrected and the camera stopped.
    _segment(tmp_path, "buf3_skewed.mkv", WALL + HOUR)
    for tick in range(100):
        assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=tick * 3.0, wall_now=WALL + tick * 3.0) is None
    assert lpr_env == []


def test_after_a_clock_step_back_the_new_live_segment_is_used_not_the_future_ones(lpr_env, tmp_path):
    _segment(tmp_path, "buf2_a.mkv", WALL + HOUR - 30)  # written before the clock correction
    _segment(tmp_path, "buf2_b.mkv", WALL + HOUR)
    _segment(tmp_path, "buf2_c.mkv", WALL - 1)          # in progress, written after it
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=WALL) is not None
    assert lpr_env == ["buf2_c.mkv"]


def test_a_writer_slightly_ahead_still_counts_as_live(lpr_env, tmp_path):
    _segment(tmp_path, "buf2_b.mkv", WALL + 3)  # e.g. a recordings share whose clock runs 3 s ahead
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=WALL) is not None
    assert lpr_env == ["buf2_b.mkv"]


def test_the_future_bound_is_the_same_as_the_past_bound(lpr_env, tmp_path):
    _segment(tmp_path, "buf2_b.mkv", WALL + 10.5)
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0, wall_now=WALL) is None
    lpr._full_frame_cache.clear()
    os.utime(tmp_path / "buf2_b.mkv", (WALL + 9.5, WALL + 9.5))
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=2.0, wall_now=WALL) is not None


def test_a_reconnecting_camera_is_decoded_again_after_a_future_dated_stop(lpr_env, tmp_path):
    seg = _segment(tmp_path, "buf3_x.mkv", WALL + HOUR)
    assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=0.0, wall_now=WALL) is None
    os.utime(seg, (WALL + 20, WALL + 20))  # the camera writes again
    assert lpr.latest_full_resolution_frame(3, str(tmp_path), now=100.0, wall_now=WALL + 21) is not None
    assert lpr_env == ["buf3_x.mkv"]


# ------------------------------------------------------- L-2 (live playlist)
def test_playlist_fresh_rejects_a_far_future_playlist(tmp_path):
    manifest = tmp_path / "camera9.m3u8"
    manifest.write_text("#EXTM3U\n")
    future = time.time() + HOUR
    os.utime(manifest, (future, future))
    assert live_hls_demand.playlist_fresh(manifest, max_age=15.0) is False
    assert live_hls_demand.playlist_fresh(manifest) is False  # on-demand default bound too


def test_playlist_fresh_accepts_small_skew_and_current_writes(tmp_path):
    manifest = tmp_path / "camera9.m3u8"
    manifest.write_text("#EXTM3U\n")
    ahead = time.time() + 3
    os.utime(manifest, (ahead, ahead))
    assert live_hls_demand.playlist_fresh(manifest, max_age=15.0) is True
    os.utime(manifest, None)
    assert live_hls_demand.playlist_fresh(manifest, max_age=15.0) is True


def test_file_age_is_unchanged_for_its_other_callers(tmp_path):
    marker = tmp_path / "m"
    marker.write_text("")
    os.utime(marker, (WALL + HOUR, WALL + HOUR))
    assert live_hls_demand.file_age(marker, WALL) == 0.0  # agent marker / alive checks keep their semantics


@pytest.fixture()
def hls(tmp_path, monkeypatch):
    import main
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
        pytest.skip("ffmpeg not available")
    folder = tmp_path / "hls"
    folder.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-t", "4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", "-f", "hls", "-hls_time", "2",
                    "-hls_list_size", "0", "-hls_segment_filename", str(folder / "camera9_%06d.ts"),
                    str(folder / "camera9.m3u8")], check=True)
    monkeypatch.setattr(main, "HLS_FOLDER", folder)
    monkeypatch.setattr(live_hls_demand, "on_demand", lambda: False)
    return folder


def _date(folder, mtime):
    for path in folder.iterdir():
        os.utime(path, (mtime, mtime))


def test_a_disconnected_cameras_future_dated_playlist_is_not_analysed(hls):
    import main
    _date(hls, time.time() + HOUR)
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert frame is None
    assert error == {"ok": False, "error": "Live video is not arriving from this camera.", "detections": []}


def test_a_future_dated_playlist_never_reaches_the_detector(hls, monkeypatch):
    import main
    monkeypatch.setattr(main.ai_frames, "get_frame", lambda camera_number, wait=None: None)
    calls = []
    monkeypatch.setattr(main, "get_yolo_model", lambda: calls.append("yolo") or (_ for _ in ()).throw(AssertionError("YOLO ran on stale video")))
    _date(hls, time.time() + HOUR)
    result = main.detect_objects_frame(9)
    assert result["ok"] is False and calls == []


def test_a_future_dated_playlist_is_refused_in_on_demand_mode(hls, monkeypatch):
    import main
    monkeypatch.setattr(live_hls_demand, "on_demand", lambda: True)
    monkeypatch.setattr(live_hls_demand, "touch", lambda *a, **k: None)
    _date(hls, time.time() + HOUR)
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert frame is None and error["ok"] is False


def test_a_slightly_ahead_playlist_is_still_analysed(hls):
    import main
    _date(hls, time.time() + 3)
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert error is None and frame is not None


def test_analysis_resumes_when_the_live_writer_rewrites_after_a_clock_step(hls):
    import main
    _date(hls, time.time() + HOUR)
    assert main._read_hls_analytics_frame(9)[0] is None
    os.utime(hls / "camera9.m3u8", None)  # the live worker writes the playlist again on the corrected clock
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert error is None and frame is not None
