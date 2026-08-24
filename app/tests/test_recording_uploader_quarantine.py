"""Bounded quarantine: tests for _record_upload_failure(), _is_quarantined(),
_clear_upload_failures(), their persistence via _load_quarantine_state(),
and their integration into _pending_recording_files()/_relay_camera_once().

Exists to prevent the exact failure this was built in response to: a
single permanently-bad recording (confirmed via real ffprobe/ffmpeg
checks -- a genuinely truncated file from a real appliance
interruption, not a guess) retried forever, blocking every valid,
newer recording behind it for that camera indefinitely.

Fast/pure tests only -- no ffmpeg, no AWS, no network. Every test gets
its own isolated RECORDINGS_FOLDER/QUARANTINE_FILE via tmp_path, and
resets the module's in-memory quarantine cache so no test can see
another's, matching test_recording_upload_cutoff.py's own established
fixture pattern.
"""

import json
from datetime import datetime, timedelta

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path / "recordings")
    monkeypatch.setattr(ru, "CUTOFF_FILE", tmp_path / "state" / "recording_upload_cutoff.json")
    monkeypatch.setattr(ru, "QUARANTINE_FILE", tmp_path / "state" / "recording_upload_quarantine.json")
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._quarantine_cache = None
    ru._camera_map.clear()
    ru._uploaded_files.clear()
    yield tmp_path
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._quarantine_cache = None
    ru._camera_map.clear()
    ru._uploaded_files.clear()


def _make_recording(tmp_path, camera_number, started_at, *, newest=False):
    folder = ru._recording_folder(camera_number)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{started_at.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = folder / name
    path.write_bytes(b"fake")
    if not newest:
        _make_recording(tmp_path, camera_number, started_at + timedelta(days=3650), newest=True)
    return path


# --------------------------------------------------------- _record_upload_failure() / threshold


def test_failures_below_threshold_do_not_quarantine():
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD - 1):
        ru._record_upload_failure(1, "camera1_bad.mkv", "remux_or_transcode_failed")
    assert not ru._is_quarantined(1, "camera1_bad.mkv")


def test_reaching_threshold_quarantines_the_file():
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, "camera1_bad.mkv", "remux_or_transcode_failed")
    assert ru._is_quarantined(1, "camera1_bad.mkv")


def test_a_different_file_on_the_same_camera_is_unaffected():
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, "camera1_bad.mkv", "remux_or_transcode_failed")
    assert not ru._is_quarantined(1, "camera1_good.mkv")


def test_the_same_filename_on_a_different_camera_is_unaffected():
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, "camera1_2026-08-22_18-38-16.mkv", "remux_or_transcode_failed")
    assert not ru._is_quarantined(2, "camera1_2026-08-22_18-38-16.mkv")


def test_warning_logged_exactly_once_at_the_moment_of_quarantine(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="anyaicam.recording_uploader")
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD + 2):  # two extra calls past the threshold
        ru._record_upload_failure(1, "camera1_bad.mkv", "remux_or_transcode_failed")
    quarantine_logs = [r for r in caplog.records if "file_quarantined" in r.message]
    assert len(quarantine_logs) == 1
    message = quarantine_logs[0].message
    assert "camera=1" in message
    assert "camera1_bad.mkv" in message
    assert "reason=remux_or_transcode_failed" in message
    assert f"failure_count={ru.QUARANTINE_FAILURE_THRESHOLD}" in message


def test_success_clears_the_failure_count():
    ru._record_upload_failure(1, "camera1_recovered.mkv", "upload_failed")
    ru._record_upload_failure(1, "camera1_recovered.mkv", "upload_failed")
    ru._clear_upload_failures(1, "camera1_recovered.mkv")
    state = ru._load_quarantine_state()
    assert "1:camera1_recovered.mkv" not in state["failures"]
    # And the two-below-threshold failures don't carry forward into a
    # later, unrelated retry sequence for the same file.
    ru._record_upload_failure(1, "camera1_recovered.mkv", "upload_failed")
    assert not ru._is_quarantined(1, "camera1_recovered.mkv")


# --------------------------------------------------------- persistence across a fresh process/cache


def test_quarantine_state_survives_a_cache_reset(tmp_path):
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, "camera1_bad.mkv", "remux_or_transcode_failed")
    assert ru._is_quarantined(1, "camera1_bad.mkv")

    ru._quarantine_cache = None  # simulates a fresh process re-reading from disk
    assert ru._is_quarantined(1, "camera1_bad.mkv")


def test_quarantine_file_actually_contains_the_expected_shape(tmp_path):
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(4, "camera4_2026-08-22_18-38-15.mkv", "remux_or_transcode_failed")
    raw = json.loads(ru.QUARANTINE_FILE.read_text())
    assert raw["quarantined"] == ["4:camera4_2026-08-22_18-38-15.mkv"]
    assert raw["failures"]["4:camera4_2026-08-22_18-38-15.mkv"] == ru.QUARANTINE_FAILURE_THRESHOLD


def test_corrupt_quarantine_file_fails_safe_to_empty(tmp_path):
    ru.QUARANTINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.QUARANTINE_FILE.write_text("not valid json{{{")
    assert not ru._is_quarantined(1, "anything.mkv")  # doesn't crash, doesn't wrongly quarantine
    # And still fully functional afterward -- a corrupt file resets to
    # empty rather than permanently breaking the mechanism.
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, "camera1_new.mkv", "upload_failed")
    assert ru._is_quarantined(1, "camera1_new.mkv")


# --------------------------------------------------------- integration: _pending_recording_files()


def test_pending_recording_files_excludes_a_quarantined_file_but_keeps_others(tmp_path):
    ru._cutoff_cache = datetime(2020, 1, 1)  # before every file's own timestamp -- nothing here is pre-cutoff backlog
    ru._camera_map[1] = {"camera_id": "cam-1", "site_id": "site-1", "recording_mode": None}
    older = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 18, 38, 16), newest=True)
    newer = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 20, 39, 46), newest=True)
    _make_recording(tmp_path, 1, datetime(2026, 8, 22, 20, 39, 46) + timedelta(days=3650), newest=True)  # pushes `newer` out of the still-open slot
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, older.name, "remux_or_transcode_failed")

    pending = ru._pending_recording_files(1, already_uploaded=set())

    assert older not in pending
    assert newer in pending


def test_pending_recording_files_still_oldest_first_among_non_quarantined(tmp_path):
    ru._cutoff_cache = datetime(2020, 1, 1)
    ru._camera_map[1] = {"camera_id": "cam-1", "site_id": "site-1", "recording_mode": None}
    quarantined = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 18, 38, 16), newest=True)
    middle = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 20, 39, 46), newest=True)
    later = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 20, 44, 46), newest=True)
    _make_recording(tmp_path, 1, datetime(2026, 8, 22, 20, 44, 46) + timedelta(days=3650), newest=True)  # still-open placeholder
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, quarantined.name, "remux_or_transcode_failed")

    pending = ru._pending_recording_files(1, already_uploaded=set())

    assert [p.name for p in pending] == [middle.name, later.name]


# --------------------------------------------------------- integration: _relay_camera_once()


def test_a_permanently_failing_file_stops_being_attempted_after_threshold_while_a_later_good_file_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", 1)
    ru._cutoff_cache = datetime(2020, 1, 1)
    ru._camera_map[1] = {"camera_id": "cam-1", "site_id": "site-1", "recording_mode": None}
    monkeypatch.setattr(ru, "_upload_currently_authorized", lambda: True)
    monkeypatch.setattr(ru, "_sessions", {1: {"credentials": {}, "bucket": "b", "key_prefix": "p", "expires_at": "2099-01-01T00:00:00"}})
    monkeypatch.setattr(ru, "_session_expires_soon", lambda session: False)

    bad = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 18, 38, 16), newest=True)
    good = _make_recording(tmp_path, 1, datetime(2026, 8, 22, 20, 39, 46), newest=True)
    _make_recording(tmp_path, 1, datetime(2026, 8, 22, 20, 39, 46) + timedelta(days=3650), newest=True)  # still-open placeholder

    prepare_calls = []

    def fake_prepare(local_path, camera_number, expected_duration_seconds):
        prepare_calls.append(local_path.name)
        if local_path.name == bad.name:
            return None
        staged = tmp_path / "staged.mp4"
        staged.write_bytes(b"fake-mp4")
        return staged

    monkeypatch.setattr(ru, "_prepare_cloud_copy", fake_prepare)
    monkeypatch.setattr(ru, "_upload_recording", lambda session, mp4_path, started_at: "some/key.mp4")
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: {"status": "accepted"})
    monkeypatch.setattr(ru, "_cleanup_staged_file", lambda path: None)

    # Scan repeatedly -- MAX_FILES_PER_SCAN=1 means the oldest pending
    # (bad) file is the only one attempted until it's quarantined.
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._relay_camera_once(1, "cam-1")

    assert prepare_calls == [bad.name] * ru.QUARANTINE_FAILURE_THRESHOLD
    assert ru._is_quarantined(1, bad.name)
    assert bad.exists()  # never moved, modified, or deleted

    # One more scan: the bad file is now excluded, so the good file
    # behind it finally gets a real attempt and succeeds.
    ru._relay_camera_once(1, "cam-1")
    assert prepare_calls[-1] == good.name
    assert good.name in ru._uploaded_files.get(1, [])
