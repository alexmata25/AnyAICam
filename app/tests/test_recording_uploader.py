"""R3 (recording-pipeline roadmap): focused tests for
recording_uploader.py's pure/filesystem logic -- no network, no AWS, no
DB. Uses monkeypatch to point RECORDINGS_FOLDER at a pytest tmp_path
for the duration of each test, so nothing here ever touches the real
/app/recordings path.
"""

from datetime import datetime, timedelta, timezone

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_recordings_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    yield tmp_path


def _touch(path, name, mtime_offset_seconds=0):
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / name
    file_path.write_bytes(b"fake-mkv-bytes")
    if mtime_offset_seconds:
        import os
        stat = file_path.stat()
        os.utime(file_path, (stat.st_atime, stat.st_atime + mtime_offset_seconds))
    return file_path


def test_filename_pattern_matches_start_recording_shape():
    pattern = ru._recording_filename_pattern(1)
    assert pattern.fullmatch("camera1_2026-08-21_00-05-00.mkv")
    assert not pattern.fullmatch("camera2_2026-08-21_00-05-00.mkv")  # different camera
    assert not pattern.fullmatch("camera1_2026-08-21_00-05-00.mp4")  # wrong extension
    assert not pattern.fullmatch("camera10_2026-08-21_00-05-00.mkv")  # camera1 must not match camera10's files
    assert not pattern.fullmatch("../camera1_2026-08-21_00-05-00.mkv")  # path traversal attempt


def test_recording_started_at_parses_the_embedded_timestamp(tmp_path):
    path = tmp_path / "camera1" / "camera1_2026-08-21_00-05-00.mkv"
    started = ru._recording_started_at(path, 1)
    assert started == datetime(2026, 8, 21, 0, 5, 0)


def test_recording_started_at_returns_none_for_unparseable_name(tmp_path):
    path = tmp_path / "camera1" / "camera1_not-a-real-timestamp.mkv"
    assert ru._recording_started_at(path, 1) is None


def test_newest_file_is_excluded_as_still_open(_isolated_recordings_folder):
    folder = _isolated_recordings_folder / "camera1"
    _touch(folder, "camera1_2026-08-21_00-00-00.mkv", mtime_offset_seconds=0)
    _touch(folder, "camera1_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    completed = [item.name for item in ru._completed_recording_files(1)]
    assert completed == ["camera1_2026-08-21_00-00-00.mkv"]


def test_single_file_is_never_uploaded_while_it_might_still_be_open(_isolated_recordings_folder):
    folder = _isolated_recordings_folder / "camera1"
    _touch(folder, "camera1_2026-08-21_00-00-00.mkv")
    assert ru._completed_recording_files(1) == []


def test_no_folder_yet_returns_empty_not_an_error(_isolated_recordings_folder):
    assert ru._completed_recording_files(1) == []


def test_completed_files_ignore_a_different_cameras_folder(_isolated_recordings_folder):
    folder1 = _isolated_recordings_folder / "camera1"
    folder2 = _isolated_recordings_folder / "camera2"
    _touch(folder1, "camera1_2026-08-21_00-00-00.mkv")
    _touch(folder1, "camera1_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    _touch(folder2, "camera2_2026-08-21_00-00-00.mkv")
    _touch(folder2, "camera2_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    completed_1 = [item.name for item in ru._completed_recording_files(1)]
    completed_2 = [item.name for item in ru._completed_recording_files(2)]
    assert completed_1 == ["camera1_2026-08-21_00-00-00.mkv"]
    assert completed_2 == ["camera2_2026-08-21_00-00-00.mkv"]


def test_pending_excludes_already_uploaded_files(_isolated_recordings_folder):
    folder = _isolated_recordings_folder / "camera1"
    _touch(folder, "camera1_2026-08-21_00-00-00.mkv")
    _touch(folder, "camera1_2026-08-21_00-05-00.mkv", mtime_offset_seconds=10)
    pending = ru._pending_recording_files(1, already_uploaded={"camera1_2026-08-21_00-00-00.mkv"})
    assert pending == []


def test_session_expires_soon_treats_missing_expiration_as_expired():
    assert ru._session_expires_soon({}) is True
    assert ru._session_expires_soon({"expires_at": None}) is True
    assert ru._session_expires_soon({"expires_at": "not-a-date"}) is True


def test_session_expires_soon_treats_timezone_naive_as_expired():
    naive = datetime.now() + timedelta(hours=1)
    assert ru._session_expires_soon({"expires_at": naive.isoformat()}) is True


def test_session_expires_soon_false_for_a_healthy_future_expiration():
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert ru._session_expires_soon({"expires_at": future.isoformat()}) is False


def test_session_expires_soon_true_within_the_renewal_margin():
    soon = datetime.now(timezone.utc) + timedelta(seconds=ru.SESSION_RENEW_MARGIN_SECONDS - 5)
    assert ru._session_expires_soon({"expires_at": soon.isoformat()}) is True
