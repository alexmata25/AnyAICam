"""_report_upload_backlog(): tests for the fleet-visible upload-backlog
summary posted once per scan cycle to POST /api/appliance/recordings/
backlog.

Fast/pure tests only -- no ffmpeg, no AWS, no real network (the control
plane call is monkeypatched to a recording fake, matching the existing
test suite's own established pattern for _control_plane_post()).
"""

from datetime import datetime, timedelta

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path / "recordings")
    monkeypatch.setattr(ru, "CUTOFF_FILE", tmp_path / "state" / "recording_upload_cutoff.json")
    monkeypatch.setattr(ru, "QUARANTINE_FILE", tmp_path / "state" / "recording_upload_quarantine.json")
    monkeypatch.setattr(ru, "UPLOADED_FILE", tmp_path / "state" / "recording_upload_uploaded.json")
    ru._cutoff_cache = datetime(2020, 1, 1)
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


def _set_camera_map(*, camera_1_mode=None, camera_2_mode="disabled"):
    ru._camera_map.clear()
    ru._camera_map[1] = {"camera_id": "cam-1-id", "recording_mode": camera_1_mode}
    ru._camera_map[2] = {"camera_id": "cam-2-id", "recording_mode": camera_2_mode}


def test_counts_pending_files_across_eligible_cameras(tmp_path, monkeypatch):
    _set_camera_map()
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 0, 0), newest=True)
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 5, 0), newest=True)
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 10, 0), newest=True)  # stands in as "still being written", excluded
    _make_recording(tmp_path, 2, datetime(2026, 8, 20, 8, 0, 0))  # camera 2 is disabled -- must not count regardless

    posted = {}

    def fake_post(path, payload):
        posted["path"] = path
        posted["payload"] = payload
        return {"status": "accepted"}

    monkeypatch.setattr(ru, "_control_plane_post", fake_post)
    ru._report_upload_backlog()

    assert posted["path"] == "/api/appliance/recordings/backlog"
    assert posted["payload"]["pending_count"] == 2  # only camera 1's two files
    assert posted["payload"]["quarantined_count"] == 0


def test_excludes_disabled_camera_entirely(tmp_path, monkeypatch):
    _set_camera_map(camera_1_mode="disabled")
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 0, 0))

    posted = {}
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: posted.update(payload) or {"status": "accepted"})
    ru._report_upload_backlog()

    assert posted["pending_count"] == 0


def test_excludes_already_uploaded_files(tmp_path, monkeypatch):
    _set_camera_map()
    already = _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 0, 0), newest=True)
    ru._record_successful_upload(1, already.name)
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 5, 0), newest=True)  # still pending
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 10, 0), newest=True)  # stands in as "still being written", excluded

    posted = {}
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: posted.update(payload) or {"status": "accepted"})
    ru._report_upload_backlog()

    assert posted["pending_count"] == 1


def test_excludes_quarantined_files_from_pending_but_counts_them_separately(tmp_path, monkeypatch):
    _set_camera_map()
    bad = _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 0, 0), newest=True)
    for _ in range(ru.QUARANTINE_FAILURE_THRESHOLD):
        ru._record_upload_failure(1, bad.name, "remux_or_transcode_failed")
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 5, 0), newest=True)  # a genuinely pending file
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 10, 0), newest=True)  # stands in as "still being written", excluded

    posted = {}
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: posted.update(payload) or {"status": "accepted"})
    ru._report_upload_backlog()

    assert posted["pending_count"] == 1  # the quarantined file is excluded from pending
    assert posted["quarantined_count"] == 1


def test_missing_camera_identity_is_skipped_without_error(monkeypatch):
    ru._camera_map.clear()  # no known cameras at all

    posted = {}
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: posted.update(payload) or {"status": "accepted"})
    ru._report_upload_backlog()  # must not raise

    assert posted["pending_count"] == 0
    assert posted["quarantined_count"] == 0


def test_control_plane_unreachable_does_not_raise(tmp_path, monkeypatch):
    _set_camera_map()
    _make_recording(tmp_path, 1, datetime(2026, 8, 20, 8, 0, 0))
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: None)  # matches real unreachable-control-plane return value

    ru._report_upload_backlog()  # must not raise
