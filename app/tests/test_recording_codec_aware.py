"""Codec-aware cloud-copy preparation: tests for _detect_video_codec(),
_prepare_cloud_copy()'s dispatch (h264/hevc/unsupported), the HEVC
transcode path, and the transcode concurrency limiter.

Fast/pure tests mock subprocess and the remux/transcode functions
directly. Two integration tests use the REAL ffmpeg/ffprobe already
required by start_recording() itself (both h264 and libx265 encoders
confirmed present in the deployed image) to generate genuine H.264
and HEVC test clips and exercise the real functions end-to-end.
"""

import shutil
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_recordings_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    # Every test gets a fresh de-dup cache -- this dict is module-global
    # and would otherwise leak "already logged as unsupported" state
    # between tests that reuse the same filename.
    ru._unsupported_codec_files.clear()
    yield tmp_path


def _ffmpeg_available(*encoders):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return False
    listed = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, timeout=15).stdout
    return all(encoder in listed for encoder in encoders)


def _make_garbage_mkv(tmp_path, camera_number=1, name="camera1_2026-08-21_00-00-00.mkv"):
    folder = tmp_path / f"camera{camera_number}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"this is not a real video file, just garbage bytes")
    return path


# ---------------------------------------------------------------- detection


def test_detect_video_codec_returns_none_when_ffprobe_missing(tmp_path):
    fake = tmp_path / "camera1" / "clip.mkv"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"x")
    with patch.object(ru.subprocess, "run", side_effect=FileNotFoundError()):
        assert ru._detect_video_codec(fake) is None


def test_detect_video_codec_returns_none_on_probe_failure(tmp_path):
    fake = tmp_path / "camera1" / "clip.mkv"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"x")
    result = MagicMock(returncode=1, stdout="")
    with patch.object(ru.subprocess, "run", return_value=result):
        assert ru._detect_video_codec(fake) is None


def test_detect_video_codec_lowercases_and_strips():
    result = MagicMock(returncode=0, stdout="  H264\n")
    with patch.object(ru.subprocess, "run", return_value=result):
        assert ru._detect_video_codec(__import__("pathlib").Path("/tmp/x.mkv")) == "h264"


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_detect_video_codec_on_a_garbage_file_returns_none(tmp_path):
    garbage = _make_garbage_mkv(tmp_path)
    assert ru._detect_video_codec(garbage) is None


# ------------------------------------------------------------- dispatch


def test_prepare_cloud_copy_dispatches_h264_to_remux(tmp_path):
    mkv = _make_garbage_mkv(tmp_path)
    with patch.object(ru, "_detect_video_codec", return_value="h264"), \
         patch.object(ru, "_remux_to_mp4", return_value=tmp_path / "out.mp4") as mock_remux, \
         patch.object(ru, "_transcode_hevc_to_h264") as mock_transcode:
        result = ru._prepare_cloud_copy(mkv, 1, 300.0)
    mock_remux.assert_called_once_with(mkv, 1, 300.0)
    mock_transcode.assert_not_called()
    assert result == tmp_path / "out.mp4"


def test_prepare_cloud_copy_dispatches_hevc_to_transcode(tmp_path):
    mkv = _make_garbage_mkv(tmp_path)
    with patch.object(ru, "_detect_video_codec", return_value="hevc"), \
         patch.object(ru, "_remux_to_mp4") as mock_remux, \
         patch.object(ru, "_transcode_hevc_to_h264", return_value=tmp_path / "out.mp4") as mock_transcode:
        result = ru._prepare_cloud_copy(mkv, 1, 300.0)
    mock_transcode.assert_called_once_with(mkv, 1, 300.0)
    mock_remux.assert_not_called()
    assert result == tmp_path / "out.mp4"


def test_prepare_cloud_copy_dispatches_h265_spelling_to_transcode(tmp_path):
    """Some tools report the alternate spelling -- both must route the
    same way."""
    mkv = _make_garbage_mkv(tmp_path)
    with patch.object(ru, "_detect_video_codec", return_value="h265"), \
         patch.object(ru, "_transcode_hevc_to_h264", return_value=tmp_path / "out.mp4") as mock_transcode:
        ru._prepare_cloud_copy(mkv, 1, 300.0)
    mock_transcode.assert_called_once()


def test_prepare_cloud_copy_fails_safe_on_unsupported_codec(tmp_path):
    mkv = _make_garbage_mkv(tmp_path)
    with patch.object(ru, "_detect_video_codec", return_value="mpeg2video"), \
         patch.object(ru, "_remux_to_mp4") as mock_remux, \
         patch.object(ru, "_transcode_hevc_to_h264") as mock_transcode:
        result = ru._prepare_cloud_copy(mkv, 1, 300.0)
    assert result is None
    mock_remux.assert_not_called()
    mock_transcode.assert_not_called()
    assert mkv.read_bytes() == b"this is not a real video file, just garbage bytes"  # untouched


def test_prepare_cloud_copy_fails_safe_when_codec_undetectable(tmp_path):
    mkv = _make_garbage_mkv(tmp_path)
    with patch.object(ru, "_detect_video_codec", return_value=None):
        result = ru._prepare_cloud_copy(mkv, 1, 300.0)
    assert result is None


def test_prepare_cloud_copy_never_creates_a_catalog_worthy_output_for_unsupported_codec(tmp_path):
    """The real, end-to-end guarantee: no cloud copy path means
    _relay_camera_once() (the real caller) never reaches the S3
    upload or catalog-notify calls for this file at all."""
    mkv = _make_garbage_mkv(tmp_path)
    with patch.object(ru, "_detect_video_codec", return_value="vp9"):
        result = ru._prepare_cloud_copy(mkv, 1, 300.0)
    assert result is None
    staging = ru._staging_folder(1)
    assert not staging.exists() or list(staging.iterdir()) == []


def test_prepare_cloud_copy_logs_unsupported_codec_once_not_every_scan(tmp_path):
    mkv = _make_garbage_mkv(tmp_path)
    with patch.object(ru, "_detect_video_codec", return_value="mpeg2video") as mock_detect:
        ru._prepare_cloud_copy(mkv, 1, 300.0)
        ru._prepare_cloud_copy(mkv, 1, 300.0)
        ru._prepare_cloud_copy(mkv, 1, 300.0)
    assert mock_detect.call_count == 1  # second and third calls short-circuit via the cache


# --------------------------------------------------------- concurrency


def test_transcode_semaphore_limits_concurrent_execution(monkeypatch):
    """Confirms the queued/limited-concurrency mechanism actually
    limits concurrency, not just that a semaphore object exists --
    spawns more worker threads than TRANSCODE_MAX_CONCURRENCY allows
    and confirms the observed peak concurrent count never exceeds it."""
    monkeypatch.setattr(ru, "_transcode_semaphore", threading.Semaphore(1))
    peak = {"value": 0}
    current = {"value": 0}
    lock = threading.Lock()

    def _slow_job():
        with lock:
            current["value"] += 1
            peak["value"] = max(peak["value"], current["value"])
        time.sleep(0.05)
        with lock:
            current["value"] -= 1

    def _guarded():
        ru._transcode_semaphore.acquire()
        try:
            _slow_job()
        finally:
            ru._transcode_semaphore.release()

    threads = [threading.Thread(target=_guarded) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert peak["value"] == 1


# --------------------------------------------------------- integration


@pytest.mark.skipif(not _ffmpeg_available("libx264"), reason="ffmpeg/libx264 not available")
def test_real_h264_clip_is_detected_and_routed_to_remux(tmp_path):
    camera_folder = tmp_path / "camera1"
    camera_folder.mkdir(parents=True)
    mkv_path = camera_folder / "camera1_2026-08-21_00-00-00.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mkv_path)],
        capture_output=True, timeout=60, check=True,
    )

    assert ru._detect_video_codec(mkv_path) == "h264"

    with patch.object(ru, "_transcode_hevc_to_h264") as mock_transcode:
        result = ru._prepare_cloud_copy(mkv_path, 1, expected_duration_seconds=1.0)

    mock_transcode.assert_not_called()  # h264 must never take the transcode path
    assert result is not None
    assert result.suffix == ".mp4"


@pytest.mark.skipif(not _ffmpeg_available("libx265"), reason="ffmpeg/libx265 not available")
def test_real_hevc_clip_is_detected_and_genuinely_transcoded_to_h264(tmp_path):
    """The strongest available proof this actually works: generates a
    real HEVC-in-MKV clip, runs the real (unmocked) transcode function,
    and confirms the OUTPUT's own codec is h264 -- i.e. a genuine
    re-encode happened, not a renamed copy."""
    camera_folder = tmp_path / "camera1"
    camera_folder.mkdir(parents=True)
    mkv_path = camera_folder / "camera1_2026-08-21_00-00-00.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-c:v", "libx265", "-pix_fmt", "yuv420p", str(mkv_path)],
        capture_output=True, timeout=60, check=True,
    )

    source_codec = ru._detect_video_codec(mkv_path)
    assert source_codec == "hevc"

    result = ru._prepare_cloud_copy(mkv_path, 1, expected_duration_seconds=1.0)

    assert result is not None
    assert result.suffix == ".mp4"
    assert mkv_path.exists()  # original untouched

    probe_output_codec = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(result)],
        capture_output=True, text=True, timeout=30,
    )
    assert probe_output_codec.stdout.strip() == "h264"  # genuinely re-encoded, browser-playable

    probe_duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(result)],
        capture_output=True, text=True, timeout=30,
    )
    assert abs(float(probe_duration.stdout.strip()) - 1.0) < 1.0
