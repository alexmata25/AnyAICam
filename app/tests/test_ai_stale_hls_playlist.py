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

PROPOSED fix (not applied): require a fresh playlist in every mode, using the
existing live_hls_demand.playlist_fresh(). Tests marked xfail(strict=True)
demonstrate today's behaviour and flip to passing once the fix lands; the
others guard live detection and reconnection and must pass before and after.
"""
import os
import subprocess
import time

import pytest

cv2 = pytest.importorskip("cv2")

L2_PENDING = pytest.mark.xfail(strict=True, reason="L-2 proposed fix not applied yet")


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


@L2_PENDING
def test_a_disconnected_cameras_stale_playlist_is_not_analysed(hls):
    import main
    _age(hls, 600)  # ten minutes without a new segment: the camera is gone
    frame, captured_at, error = main._read_hls_analytics_frame(9)
    assert frame is None
    assert error is not None and error["ok"] is False


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
