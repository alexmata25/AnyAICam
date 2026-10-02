"""Existing recording rows with the stale 300-second duration are repaired
(2026-10-02). Rows cataloged while ffprobe failed -- or before the catalog
probed durations at all -- kept the old fixed 300 s, so an 18-second
Event-mode clip claimed five minutes in Playback. The catalog now re-probes
such existing rows, bounded per call and once per process per row; a failed
probe, or a genuine 300 s Continuous segment, leaves the row unchanged."""
from datetime import datetime

import pytest

import main
from database_backend import override_target
from partner_db import connection
from test_recording_catalog_lock_resilience import _seed, _write_file, db_path  # noqa: F401


def _insert_stale_row(db_path, path, started):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,duration_seconds,size_bytes,status,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"rec-{path.stem}", "cust-1", "site-1", "appl-1", "cam-1", main.cloud_recording_s3_key(path, 1),
                        started.isoformat(), datetime(2026, 9, 21, 10, 5, 0).isoformat(), 300, 1024, "available", "2026-09-21"))


def _row(db_path, path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return dict(db.execute("SELECT duration_seconds,ended_at FROM recordings WHERE s3_key=?",
                                   (main.cloud_recording_s3_key(path, 1),)).fetchone())


@pytest.fixture(autouse=True)
def _fresh_verified_set(monkeypatch):
    monkeypatch.setattr(main, "_recording_durations_verified", set())


def test_an_existing_stale_event_clip_row_is_repaired(db_path, tmp_path, monkeypatch):
    _seed(db_path)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    started = datetime(2026, 9, 21, 10, 0, 0)
    path = _write_file(tmp_path / "recordings" / "camera1", 1, started)
    _insert_stale_row(db_path, path, started)
    monkeypatch.setattr(main, "_probe_recording_duration_seconds", lambda p: 18.4)
    with override_target(sqlite_path=str(db_path)):
        assert main._catalog_local_recordings_for_camera("cam-1") == 0  # nothing new, one repaired
    assert _row(db_path, path) == {"duration_seconds": 18, "ended_at": "2026-09-21T10:00:18.400000"}


def test_a_failed_probe_or_a_real_300_second_segment_changes_nothing_and_is_not_reprobed(db_path, tmp_path, monkeypatch):
    _seed(db_path)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    a = _write_file(tmp_path / "recordings" / "camera1", 1, datetime(2026, 9, 21, 10, 0, 0))
    b = _write_file(tmp_path / "recordings" / "camera1", 1, datetime(2026, 9, 21, 10, 5, 0))
    _insert_stale_row(db_path, a, datetime(2026, 9, 21, 10, 0, 0))
    _insert_stale_row(db_path, b, datetime(2026, 9, 21, 10, 5, 0))
    probes = []
    monkeypatch.setattr(main, "_probe_recording_duration_seconds",
                        lambda p: probes.append(p.name) or (None if p == a else 299.8))
    with override_target(sqlite_path=str(db_path)):
        main._catalog_local_recordings_for_camera("cam-1")
        main._catalog_local_recordings_for_camera("cam-1")
    assert _row(db_path, a)["duration_seconds"] == 300 and _row(db_path, b)["duration_seconds"] == 300
    assert sorted(probes) == sorted([a.name, b.name])  # probed once each, not on every call


def test_repairs_are_bounded_per_call(db_path, tmp_path, monkeypatch):
    _seed(db_path)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    monkeypatch.setattr(main, "RECORDING_DURATION_REPAIRS_PER_CALL", 2)
    paths = []
    for minute in range(5):
        started = datetime(2026, 9, 21, 11, minute, 0)
        path = _write_file(tmp_path / "recordings" / "camera1", 1, started)
        _insert_stale_row(db_path, path, started)
        paths.append(path)
    probes = []
    monkeypatch.setattr(main, "_probe_recording_duration_seconds", lambda p: probes.append(p.name) or 12.0)
    with override_target(sqlite_path=str(db_path)):
        main._catalog_local_recordings_for_camera("cam-1")
    assert len(probes) == 2
    with override_target(sqlite_path=str(db_path)):
        main._catalog_local_recordings_for_camera("cam-1")
        main._catalog_local_recordings_for_camera("cam-1")
    assert len(probes) == 5 and all(_row(db_path, p)["duration_seconds"] == 12 for p in paths)
