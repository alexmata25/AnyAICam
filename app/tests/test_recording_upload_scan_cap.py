"""Per-scan upload cap (ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN):
tests for _parse_max_files_per_scan() and its enforcement in
_relay_camera_once(). Built so the cloud-upload pilot test can be
genuinely bounded (at most N files per camera per scan) instead of
uploading however many files the cutoff happens to have made eligible
by the time the flag is flipped -- which, hours after cutoff
establishment, can already be dozens of legitimate, non-backlog files.

Fast/pure tests only -- no ffmpeg, no AWS, no network. The upload
chain itself (_ensure_session, _prepare_cloud_copy, _upload_recording,
_control_plane_post) is mocked here exactly as it already is in
test_recording_uploader.py's sibling tests, since this feature is
entirely about *which* and *how many* files reach that chain, not
about the chain's own internals (covered elsewhere).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(ru, "CUTOFF_FILE", tmp_path / "state" / "recording_upload_cutoff.json")
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._uploaded_files.clear()
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", None)  # explicit default each test, not whatever the real env produced at import time
    yield tmp_path
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._uploaded_files.clear()


def _make_eligible_recording(tmp_path, camera_number, started_at):
    """Writes one fake recording file. Does NOT create a "still open"
    placeholder itself -- call _make_still_open_placeholder() once,
    after every real file for a camera has been created, so there is
    exactly one such placeholder (not one per call, which would itself
    become an extra, unintended "eligible" file since only the single
    lexicographically-last candidate is ever excluded)."""
    folder = ru._recording_folder(camera_number)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{started_at.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = folder / name
    path.write_bytes(b"fake-recording")
    return path


def _make_still_open_placeholder(camera_number):
    """Exactly one far-future file so every real file created so far
    for this camera is treated as "closed" -- _completed_recording_files()
    excludes only the single lexicographically-last candidate."""
    folder = ru._recording_folder(camera_number)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_2099-01-01_00-00-00.mkv"
    (folder / name).write_bytes(b"placeholder-still-open")


def _wire_fake_upload_chain(monkeypatch, tmp_path):
    """Every processed file gets its own separate fake staging output
    (never the original recording file itself) so _cleanup_staged_file()
    only ever touches the fake, never anything real."""
    def _fake_ensure_session(camera_number, camera_id):
        return {"credentials": {"access_key_id": "a", "secret_access_key": "b", "session_token": "c"}, "bucket": "test-bucket", "key_prefix": "recordings/x/y/z/w/"}

    def _fake_prepare_cloud_copy(mkv_path, camera_number, expected_duration_seconds):
        staging = tmp_path / "_test_staging"
        staging.mkdir(exist_ok=True)
        fake_mp4 = staging / (mkv_path.stem + ".mp4")
        fake_mp4.write_bytes(b"fake-mp4")
        return fake_mp4

    def _fake_upload_recording(session, local_path, started_at):
        return f"recordings/x/y/z/w/{local_path.name}"

    def _fake_control_plane_post(path, payload):
        return {"status": "accepted"}

    monkeypatch.setattr(ru, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(ru, "_prepare_cloud_copy", _fake_prepare_cloud_copy)
    monkeypatch.setattr(ru, "_upload_recording", _fake_upload_recording)
    monkeypatch.setattr(ru, "_control_plane_post", _fake_control_plane_post)


# --------------------------------------------------------- parsing/config


def test_unset_env_var_means_no_cap(monkeypatch):
    monkeypatch.delenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", raising=False)
    assert ru._parse_max_files_per_scan() is None


def test_empty_env_var_means_no_cap(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "")
    assert ru._parse_max_files_per_scan() is None


def test_value_of_one_parses_as_one(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "1")
    assert ru._parse_max_files_per_scan() == 1


def test_larger_value_parses_correctly(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "7")
    assert ru._parse_max_files_per_scan() == 7


def test_zero_fails_safe_to_one_not_to_no_cap(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "0")
    assert ru._parse_max_files_per_scan() == 1


def test_negative_fails_safe_to_one_not_to_no_cap(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "-5")
    assert ru._parse_max_files_per_scan() == 1


def test_non_numeric_fails_safe_to_one_not_to_no_cap(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "not-a-number")
    assert ru._parse_max_files_per_scan() == 1


def test_whitespace_only_means_no_cap_same_as_empty(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "   ")
    assert ru._parse_max_files_per_scan() is None


# ------------------------------------------------------ enforcement in _relay_camera_once


def test_unset_cap_processes_every_eligible_file_exactly_as_before(tmp_path, monkeypatch):
    _wire_fake_upload_chain(monkeypatch, tmp_path)
    ru._cutoff_cache = datetime.now() - timedelta(hours=1)
    for minute in (10, 20, 30):
        _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=minute))
    _make_still_open_placeholder(1)

    ru._relay_camera_once(1, "camera-1-id")

    assert len(ru._uploaded_files.get(1, [])) == 3  # every eligible file, unset cap == today's exact behavior


def test_cap_of_one_processes_exactly_one_file_per_camera_per_scan(tmp_path, monkeypatch):
    _wire_fake_upload_chain(monkeypatch, tmp_path)
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", 1)
    ru._cutoff_cache = datetime.now() - timedelta(hours=1)
    for minute in (10, 20, 30):
        _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=minute))
    _make_still_open_placeholder(1)

    ru._relay_camera_once(1, "camera-1-id")

    assert len(ru._uploaded_files.get(1, [])) == 1


def test_cap_of_two_caps_at_exactly_two_when_three_are_eligible(tmp_path, monkeypatch):
    _wire_fake_upload_chain(monkeypatch, tmp_path)
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", 2)
    ru._cutoff_cache = datetime.now() - timedelta(hours=1)
    for minute in (10, 20, 30):
        _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=minute))
    _make_still_open_placeholder(1)

    ru._relay_camera_once(1, "camera-1-id")

    assert len(ru._uploaded_files.get(1, [])) == 2


def test_cap_larger_than_eligible_count_uploads_all_of_them(tmp_path, monkeypatch):
    _wire_fake_upload_chain(monkeypatch, tmp_path)
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", 100)
    ru._cutoff_cache = datetime.now() - timedelta(hours=1)
    for minute in (10, 20):
        _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=minute))
    _make_still_open_placeholder(1)

    ru._relay_camera_once(1, "camera-1-id")

    assert len(ru._uploaded_files.get(1, [])) == 2  # capped at 100 but only 2 exist -- no error, no padding


def test_pre_cutoff_files_remain_excluded_regardless_of_cap(tmp_path, monkeypatch):
    _wire_fake_upload_chain(monkeypatch, tmp_path)
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", 10)
    cutoff = datetime.now()
    ru._cutoff_cache = cutoff
    old = _make_eligible_recording(tmp_path, 1, cutoff - timedelta(days=1))
    _make_eligible_recording(tmp_path, 1, cutoff + timedelta(minutes=5))
    _make_still_open_placeholder(1)

    ru._relay_camera_once(1, "camera-1-id")

    uploaded = set(ru._uploaded_files.get(1, []))
    assert old.name not in uploaded
    assert len(uploaded) == 1  # only the one genuinely post-cutoff file


def test_files_beyond_the_cap_remain_on_disk_untouched_and_eligible_next_scan(tmp_path, monkeypatch):
    _wire_fake_upload_chain(monkeypatch, tmp_path)
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", 1)
    ru._cutoff_cache = datetime.now() - timedelta(hours=1)
    first = _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=30))
    second = _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=20))
    _make_still_open_placeholder(1)
    second_bytes = second.read_bytes()

    ru._relay_camera_once(1, "camera-1-id")

    uploaded = set(ru._uploaded_files.get(1, []))
    assert len(uploaded) == 1
    # Whichever one didn't upload this scan is still on disk, unaltered,
    # and still eligible (not in _uploaded_files) for the next scan.
    leftover_name = ({first.name, second.name} - uploaded).pop()
    leftover_path = ru._recording_folder(1) / leftover_name
    assert leftover_path.exists()
    if leftover_name == second.name:
        assert leftover_path.read_bytes() == second_bytes
    still_pending = ru._pending_recording_files(1, uploaded)
    assert any(p.name == leftover_name for p in still_pending)


def test_next_scan_picks_up_the_leftover_file_capped_run_after_capped_run(tmp_path, monkeypatch):
    """Two scans, cap=1: proves nothing is lost, only deferred -- both
    eligible files eventually upload, one per scan."""
    _wire_fake_upload_chain(monkeypatch, tmp_path)
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", 1)
    ru._cutoff_cache = datetime.now() - timedelta(hours=1)
    _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=30))
    _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=20))
    _make_still_open_placeholder(1)

    ru._relay_camera_once(1, "camera-1-id")
    assert len(ru._uploaded_files.get(1, [])) == 1

    ru._relay_camera_once(1, "camera-1-id")
    assert len(ru._uploaded_files.get(1, [])) == 2  # the leftover from scan 1 uploaded on scan 2
