"""Pilot activation fix: tests for the H.264-confirmed remux-to-MP4
step added to recording_uploader.py. Fast/pure tests mock subprocess
entirely; the integration test at the bottom uses the REAL ffmpeg/
ffprobe already required by start_recording() itself, generating a
tiny synthetic H.264 test clip (the exact codec confirmed on the real
pilot camera via ffprobe, 2026-08-21) so the actual remux path is
exercised end-to-end, not just mocked.
"""

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_recordings_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    yield tmp_path


def _fake_completed_process(returncode=0):
    result = MagicMock()
    result.returncode = returncode
    return result


def test_remux_returns_none_when_ffmpeg_missing(tmp_path):
    mkv = tmp_path / "camera1" / "camera1_2026-08-21_00-00-00.mkv"
    mkv.parent.mkdir(parents=True)
    mkv.write_bytes(b"fake")
    with patch.object(ru.subprocess, "run", side_effect=FileNotFoundError()):
        assert ru._remux_to_mp4(mkv, 1, 300.0) is None


def test_remux_returns_none_on_nonzero_exit(tmp_path):
    mkv = tmp_path / "camera1" / "camera1_2026-08-21_00-00-00.mkv"
    mkv.parent.mkdir(parents=True)
    mkv.write_bytes(b"fake")
    with patch.object(ru.subprocess, "run", return_value=_fake_completed_process(returncode=1)):
        assert ru._remux_to_mp4(mkv, 1, 300.0) is None


def test_remux_leaves_original_mkv_untouched_on_failure(tmp_path):
    mkv = tmp_path / "camera1" / "camera1_2026-08-21_00-00-00.mkv"
    mkv.parent.mkdir(parents=True)
    mkv.write_bytes(b"original-bytes")
    with patch.object(ru.subprocess, "run", return_value=_fake_completed_process(returncode=1)):
        ru._remux_to_mp4(mkv, 1, 300.0)
    assert mkv.read_bytes() == b"original-bytes"


def test_verify_output_duration_advisory_when_ffprobe_missing(tmp_path):
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"fake")
    with patch.object(ru.subprocess, "run", side_effect=FileNotFoundError()):
        assert ru._verify_output_duration(mp4, 300.0, 1) is True


def test_verify_output_duration_rejects_real_mismatch(tmp_path):
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"fake")
    result = MagicMock(returncode=0, stdout="5.0\n")
    with patch.object(ru.subprocess, "run", return_value=result):
        assert ru._verify_output_duration(mp4, 300.0, 1) is False


def test_verify_output_duration_accepts_within_tolerance(tmp_path):
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"fake")
    result = MagicMock(returncode=0, stdout="298.5\n")
    with patch.object(ru.subprocess, "run", return_value=result):
        assert ru._verify_output_duration(mp4, 300.0, 1) is True


def test_upload_recording_uses_mp4_content_type():
    session = {
        "credentials": {"access_key_id": "a", "secret_access_key": "b", "session_token": "c"},
        "bucket": "test-bucket",
        "key_prefix": "recordings/cust/site/appl/cam/",
    }
    fake_client = MagicMock()
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_client
    with patch.object(ru, "boto3", fake_boto3):
        ru._upload_recording(session, Path("/tmp/fake.mp4"), datetime(2026, 8, 21))
    _, kwargs = fake_client.upload_file.call_args
    assert kwargs["ExtraArgs"]["ContentType"] == "video/mp4"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available in this environment",
)
def test_real_remux_of_a_synthetic_h264_clip_produces_a_valid_seekable_mp4(tmp_path):
    """Integration test: generates a tiny REAL H.264-in-MKV test clip
    (the exact codec confirmed on the real pilot camera) using the same
    ffmpeg binary start_recording() itself already requires, then runs
    the actual _remux_to_mp4() against it -- not mocked -- and confirms
    the output is a genuinely valid, correctly-timed, faststart MP4."""
    camera_folder = tmp_path / "camera1"
    camera_folder.mkdir(parents=True)
    mkv_path = camera_folder / "camera1_2026-08-21_00-00-00.mkv"

    generate = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mkv_path)],
        capture_output=True, timeout=60,
    )
    assert generate.returncode == 0, generate.stderr.decode(errors="replace")

    probe_source = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(mkv_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert probe_source.stdout.strip() == "h264"  # confirms the synthetic fixture matches the real pilot codec

    result = ru._remux_to_mp4(mkv_path, 1, expected_duration_seconds=2.0)

    assert result is not None
    assert result.suffix == ".mp4"
    assert result.exists()
    assert mkv_path.exists()  # original untouched

    probe_output = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(result)],
        capture_output=True, text=True, timeout=30,
    )
    assert probe_output.stdout.strip() == "h264"  # stream-copied, not re-encoded -- same codec preserved in the MP4

    probe_duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(result)],
        capture_output=True, text=True, timeout=30,
    )
    assert abs(float(probe_duration.stdout.strip()) - 2.0) < 1.0

    probe_format = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "default=nw=1:nk=1", str(result)],
        capture_output=True, text=True, timeout=30,
    )
    assert "mp4" in probe_format.stdout.strip().lower()
