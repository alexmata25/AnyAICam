"""Persisted "already uploaded" record: tests for _load_uploaded_state(),
_save_uploaded_state(), _record_successful_upload(),
_already_uploaded_for_camera(), and their integration into
_remember_uploaded() / _pending_recording_files().

Exists to prevent the exact failure this was built in response to: a
container restart reset the in-memory-only _uploaded_files to empty,
so _relay_camera_once() treated every local file still on disk (the
whole local retention window -- confirmed in production: 245 files on
one camera) as an unseen upload candidate again, needlessly
re-verifying and re-uploading hours of already-registered history
before ever reaching genuinely new footage. This file did not exist in
the cloud catalog as a duplicate problem (the server's own dedup on
/available already absorbed it harmlessly), but it delayed real new
recordings reaching Playback for hours after every restart.

Fast/pure tests only -- no ffmpeg, no AWS, no network. Every test gets
its own isolated RECORDINGS_FOLDER/UPLOADED_FILE via tmp_path, and
resets the module's in-memory caches so no test can see another's,
matching test_recording_uploader_quarantine.py's own established
fixture pattern.
"""

import json
import logging
from datetime import datetime, timedelta

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path / "recordings")
    monkeypatch.setattr(ru, "CUTOFF_FILE", tmp_path / "state" / "recording_upload_cutoff.json")
    monkeypatch.setattr(ru, "QUARANTINE_FILE", tmp_path / "state" / "recording_upload_quarantine.json")
    monkeypatch.setattr(ru, "UPLOADED_FILE", tmp_path / "state" / "recording_upload_uploaded.json")
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._quarantine_cache = None
    ru._uploaded_state_cache = None
    ru._camera_map.clear()
    ru._uploaded_files.clear()
    yield tmp_path
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._quarantine_cache = None
    ru._uploaded_state_cache = None
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


def _simulate_restart():
    """Clears everything a real process restart would clear (the
    in-memory-only structures) while leaving disk state (and therefore
    the next _load_uploaded_state() call) untouched -- exactly what
    happens to the real worker across a container restart."""
    ru._uploaded_files.clear()
    ru._uploaded_state_cache = None


# --------------------------------------------------------- _load_uploaded_state() / _save_uploaded_state()


def test_missing_file_starts_empty():
    assert ru._load_uploaded_state() == {}


def test_corrupt_file_fails_safe_to_empty(caplog):
    ru.UPLOADED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ru.UPLOADED_FILE.write_text("{not valid json")
    caplog.set_level(logging.WARNING, logger="anyaicam.recording_uploader")
    assert ru._load_uploaded_state() == {}
    assert any("uploaded_state_corrupt_resetting" in r.message for r in caplog.records)


def test_save_then_load_round_trips():
    ru._save_uploaded_state({"1": ["camera1_a.mkv", "camera1_b.mkv"]})
    ru._uploaded_state_cache = None  # force a real re-read from disk
    assert ru._load_uploaded_state() == {"1": ["camera1_a.mkv", "camera1_b.mkv"]}


def test_save_write_failure_logs_and_does_not_raise(monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ru.Path, "write_text", _boom)
    caplog.set_level(logging.WARNING, logger="anyaicam.recording_uploader")
    ru._save_uploaded_state({"1": ["camera1_a.mkv"]})  # must not raise
    assert any("uploaded_state_write_failed" in r.message for r in caplog.records)


# --------------------------------------------------------- _record_successful_upload()


def test_record_successful_upload_persists_across_simulated_restart():
    ru._record_successful_upload(1, "camera1_x.mkv")
    _simulate_restart()
    assert "camera1_x.mkv" in ru._load_uploaded_state().get("1", [])


def test_record_successful_upload_is_idempotent():
    ru._record_successful_upload(1, "camera1_x.mkv")
    ru._record_successful_upload(1, "camera1_x.mkv")
    assert ru._load_uploaded_state()["1"].count("camera1_x.mkv") == 1


def test_record_successful_upload_trims_to_persisted_cap(monkeypatch):
    monkeypatch.setattr(ru, "MAX_PERSISTED_UPLOADED_FILES_PER_CAMERA", 3)
    for i in range(5):
        ru._record_successful_upload(1, f"camera1_{i}.mkv")
    entries = ru._load_uploaded_state()["1"]
    assert len(entries) == 3
    assert entries == ["camera1_2.mkv", "camera1_3.mkv", "camera1_4.mkv"]  # oldest dropped first


def test_different_cameras_are_isolated():
    ru._record_successful_upload(1, "camera1_x.mkv")
    ru._record_successful_upload(2, "camera2_x.mkv")
    state = ru._load_uploaded_state()
    assert state["1"] == ["camera1_x.mkv"]
    assert state["2"] == ["camera2_x.mkv"]


# --------------------------------------------------------- _remember_uploaded() also persists


def test_remember_uploaded_persists_to_disk_too():
    ru._remember_uploaded(1, "camera1_x.mkv")
    assert "camera1_x.mkv" in ru._uploaded_files[1]  # existing in-memory behavior, unchanged
    _simulate_restart()
    assert "camera1_x.mkv" in ru._load_uploaded_state().get("1", [])


# --------------------------------------------------------- _already_uploaded_for_camera()


def test_already_uploaded_unions_in_memory_and_persisted():
    ru._uploaded_files[1] = ["camera1_in_memory_only.mkv"]
    ru._record_successful_upload(1, "camera1_persisted_only.mkv")
    already = ru._already_uploaded_for_camera(1)
    assert already == {"camera1_in_memory_only.mkv", "camera1_persisted_only.mkv"}


def test_already_uploaded_empty_when_nothing_recorded():
    assert ru._already_uploaded_for_camera(1) == set()


# --------------------------------------------------------- end-to-end: the actual regression scenario


def test_restart_does_not_reintroduce_already_uploaded_files_as_pending(tmp_path):
    ru._cutoff_cache = datetime(2020, 1, 1)  # everything below counts as "after cutoff"
    old1 = _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 0, 0))
    old2 = _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 5, 0))
    # Simulate: both of these were uploaded successfully in a PREVIOUS
    # process lifetime (before the restart under test).
    ru._record_successful_upload(1, old1.name)
    ru._record_successful_upload(1, old2.name)

    _simulate_restart()

    # A genuinely new file completes after the restart.
    new_file = _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 10, 0))

    already = ru._already_uploaded_for_camera(1)
    pending = [p.name for p in ru._pending_recording_files(1, already)]

    assert old1.name not in pending
    assert old2.name not in pending
    assert new_file.name in pending


def test_without_the_fix_a_restart_would_have_reprocessed_everything(tmp_path):
    """Guards the regression directly: if _already_uploaded_for_camera()
    only consulted the in-memory _uploaded_files (the pre-fix
    behavior), a simulated restart would make every already-uploaded
    file pending again. This test proves the persisted half is what
    prevents that."""
    ru._cutoff_cache = datetime(2020, 1, 1)
    old = _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 0, 0))
    ru._record_successful_upload(1, old.name)
    _simulate_restart()

    pre_fix_already = set(ru._uploaded_files.get(1, []))  # what the old code would have used
    assert old.name in [p.name for p in ru._pending_recording_files(1, pre_fix_already)]

    post_fix_already = ru._already_uploaded_for_camera(1)  # what the fixed code uses
    assert old.name not in [p.name for p in ru._pending_recording_files(1, post_fix_already)]
