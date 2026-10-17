"""L-2 (proposed, 2026-10-17): the AI frame reader must not run detection on a
disconnected camera's stale live playlist.

Seen on the Ryzen: cameras 2/3 could not be reached on their main stream, so
the analytics pipeline fell back to their live HLS playlist
(analytics_frames.uses_hls). main._read_hls_analytics_frame() checks the
playlist's freshness only in on-demand live mode; in the default "always"
mode it opens the playlist with cv2.VideoCapture even when no segment has been
written for minutes, so YOLO / people counting / LPR keep running on frozen
video (Python VMS at 360-450 % CPU, box at ~87 C, with only two cameras
actually streaming).

Fix (owner decision 2026-10-17, required for 1.2.7): a fresh playlist is
required in every live mode (main.HLS_ANALYTICS_MAX_AGE_SECONDS, default 15 s,
reusing live_hls_demand.playlist_fresh()). Live detection, reconnection and the
missing-playlist message are unchanged.
"""
import os
import subprocess
import time

import pytest

cv2 = pytest.importorskip("cv2")

@pytest.fixture()
def hls(tmp_path, monkeypatch):
    """A real two-segment live HLS playlist for camera 9 in a temporary folder."""
    import live_hls_demand
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
    monkeypatch.setattr(live_hls_demand, "on_demand", lambda: False)  # the Ryzen's "always" mode
    return folder


def _age(folder, seconds):
    old = time.time() - seconds
    for path in folder.iterdir():
        os.utime(path, (old, old))


def test_a_live_playlist_still_feeds_detection(hls):
    import main
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert error is None and frame is not None and frame.shape[:2] == (360, 640)


def test_a_disconnected_cameras_stale_playlist_is_not_analysed(hls):
    import main
    _age(hls, 600)  # ten minutes without a new segment: the camera is gone
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert frame is None
    assert error == {"ok": False, "error": "Live video is not arriving from this camera.", "detections": []}


def test_the_same_stale_playlist_is_refused_in_on_demand_mode(hls, monkeypatch):
    import live_hls_demand
    import main
    monkeypatch.setattr(live_hls_demand, "on_demand", lambda: True)
    monkeypatch.setattr(live_hls_demand, "touch", lambda *a, **k: None)
    _age(hls, 600)
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert frame is None and error["ok"] is False


def test_a_playlist_just_inside_the_bound_is_still_analysed(hls, monkeypatch):
    import main
    monkeypatch.setattr(main, "HLS_ANALYTICS_MAX_AGE_SECONDS", 15.0)
    _age(hls, 12)  # a short hiccup, well under the bound
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert error is None and frame is not None


def test_the_bound_is_configurable_with_a_safe_floor():
    import importlib.util
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'HLS_ANALYTICS_MAX_AGE_SECONDS = max(4.0, float(os.environ.get("ANYAICAM_HLS_ANALYTICS_MAX_AGE_SECONDS", "15")))' in source


def test_a_stale_frame_never_reaches_the_detector(hls, monkeypatch):
    """End to end through detect_objects_frame on the HLS path: no YOLO call
    and no LPR pass for a disconnected camera."""
    import main
    monkeypatch.setattr(main.ai_frames, "get_frame", lambda camera_number, wait=None: None)  # HLS fallback
    calls = []
    monkeypatch.setattr(main, "get_yolo_model", lambda: calls.append("yolo") or (_ for _ in ()).throw(AssertionError("YOLO ran on stale video")))
    _age(hls, 600)
    result = main.detect_objects_frame(9)
    assert result["ok"] is False and result.get("frame") is None
    assert calls == []


def test_detection_resumes_when_the_camera_reconnects(hls):
    import main
    _age(hls, 600)
    main._read_hls_analytics_frame(9)  # whatever happens while it is gone
    os.utime(hls / "camera9.m3u8", None)  # the live worker writes a new playlist again
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert error is None and frame is not None


def test_a_missing_playlist_is_still_reported_as_not_ready(hls):
    import main
    (hls / "camera9.m3u8").unlink()
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert frame is None and error["error"] == "Live HLS stream is not ready."
