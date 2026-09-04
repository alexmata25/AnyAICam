"""Permanent local-recording retention, age-only (2026-09-04): approved
for this lab/test Samsung appliance specifically -- not customer data,
so no upload-confirmation gate is required. delete_expired_recordings()
deletes a local .mkv once it is older than RETENTION_DAYS, and removes
the matching local Playback catalog row in the same pass. The camera's
newest file (presumed still being actively written) is never a
deletion candidate, matching recording_uploader._completed_recording_
files()'s own guarantee.

Same DB-isolation convention as test_recording_retention_sweep.py:
every test redirects to a throwaway sqlite file via override_target()
before seeding or touching anything, so nothing here ever touches the
real production database. Same container-only import constraint as
this suite's other main.py-importing tests (partner_db's schema init
requires /app to exist).
"""
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_retention_v2.db"


@pytest.fixture()
def recordings_root(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", root)
    monkeypatch.setattr(main, "MOTION_EVENTS_FILE", root / "motion_events.jsonl")
    monkeypatch.setattr(main, "MOTION_THUMBNAILS_FOLDER", root / "motion_thumbnails")
    monkeypatch.setattr(main, "RETENTION_DAYS", 2)
    return root


def _seed_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('p1','P','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES('c1','p1','C','c1@example.com','active','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('s1','c1','Main','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) "
        "VALUES('a1','c1','s1','AIC-1','2026-01-01')"
    )


def _seed_camera(conn, camera_id, camera_number):
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (camera_id, "c1", "s1", "a1", f"Camera {camera_number}", camera_number, "2026-01-01"),
    )


def _write_recording_file(root: Path, camera_number: int, filename: str, age_hours: float) -> Path:
    folder = root / f"camera{camera_number}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(b"fake-mkv-bytes")
    mtime = (datetime.now() - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def _catalog_row(conn, camera_id, path: Path, camera_number: int):
    s3_key = main.cloud_recording_s3_key(path, camera_number)
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,"
        "duration_seconds,size_bytes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"rec-{path.name}", "c1", "s1", "a1", camera_id, s3_key,
            "2026-01-01T00:00:00", "2026-01-01T00:05:00", 300, len(path.read_bytes()),
            "available", "2026-01-01T00:00:00",
        ),
    )
    return s3_key


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_expired_recording_is_deleted(db_path, recordings_root):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = _conn(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1", 1)
        old = _write_recording_file(recordings_root, 1, "camera1_2026-08-30_00-00-00.mkv", age_hours=72)
        _write_recording_file(recordings_root, 1, "camera1_2026-09-02_00-00-00.mkv", age_hours=1)  # newest guard
        _catalog_row(conn, "cam-1", old, 1)
        conn.commit()
        conn.close()

        main.delete_expired_recordings()

    assert not old.exists()


def test_recent_recording_is_preserved(db_path, recordings_root):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = _conn(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1", 1)
        recent = _write_recording_file(recordings_root, 1, "camera1_2026-09-01_00-00-00.mkv", age_hours=1)
        _write_recording_file(recordings_root, 1, "camera1_2026-09-02_00-00-00.mkv", age_hours=0.1)  # newest
        _catalog_row(conn, "cam-1", recent, 1)
        conn.commit()
        conn.close()

        main.delete_expired_recordings()

    assert recent.exists(), "younger than RETENTION_DAYS must be preserved"


def test_recording_right_at_the_boundary_is_preserved(db_path, recordings_root):
    """A file just inside the window (47.9h) must survive; the 2-day
    threshold is a floor, not a rounding target."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = _conn(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1", 1)
        boundary = _write_recording_file(recordings_root, 1, "camera1_2026-09-01_00-00-00.mkv", age_hours=47.9)
        _write_recording_file(recordings_root, 1, "camera1_2026-09-02_00-00-00.mkv", age_hours=0.1)
        _catalog_row(conn, "cam-1", boundary, 1)
        conn.commit()
        conn.close()

        main.delete_expired_recordings()

    assert boundary.exists()


def test_newest_in_progress_file_is_never_touched_even_if_old(db_path, recordings_root):
    """Belt-and-suspenders: even a very old file is never a candidate
    if it's the single newest file present for its camera."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = _conn(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1", 1)
        newest = _write_recording_file(recordings_root, 1, "camera1_2026-08-01_00-00-00.mkv", age_hours=1000)
        _catalog_row(conn, "cam-1", newest, 1)
        conn.commit()
        conn.close()

        main.delete_expired_recordings()

    assert newest.exists(), "the single newest file per camera is always presumed in-progress"


def test_catalog_row_removed_together_with_deleted_file(db_path, recordings_root):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = _conn(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1", 1)
        old = _write_recording_file(recordings_root, 1, "camera1_2026-08-30_00-00-00.mkv", age_hours=72)
        _write_recording_file(recordings_root, 1, "camera1_2026-09-02_00-00-00.mkv", age_hours=1)
        s3_key = _catalog_row(conn, "cam-1", old, 1)
        conn.commit()
        conn.close()

        main.delete_expired_recordings()

        conn2 = _conn(db_path)
        remaining = conn2.execute(
            "SELECT COUNT(*) AS n FROM recordings WHERE camera_id=? AND s3_key=?", ("cam-1", s3_key)
        ).fetchone()["n"]

    assert not old.exists()
    assert remaining == 0, "the matching Playback catalog row must be removed in the same cleanup"


def test_no_orphaned_catalog_rows_after_mixed_cleanup(db_path, recordings_root):
    """A realistic mixed pass across two cameras: some files expired
    (deleted), some recent (kept), each camera's newest file always
    kept. After the pass, every remaining catalog row must point at a
    file that still exists, and every deleted file's row must be gone."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = _conn(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1", 1)
        _seed_camera(conn, "cam-2", 2)

        deleted_expected = []
        kept_expected = []

        for camera_number, camera_id in ((1, "cam-1"), (2, "cam-2")):
            old = _write_recording_file(recordings_root, camera_number, f"camera{camera_number}_2026-08-30_00-00-00.mkv", age_hours=72)
            _catalog_row(conn, camera_id, old, camera_number)
            deleted_expected.append(old)

            recent = _write_recording_file(recordings_root, camera_number, f"camera{camera_number}_2026-09-01_12-00-00.mkv", age_hours=1)
            _catalog_row(conn, camera_id, recent, camera_number)
            kept_expected.append(recent)

            newest = _write_recording_file(recordings_root, camera_number, f"camera{camera_number}_2026-09-02_00-00-00.mkv", age_hours=0.1)
            kept_expected.append(newest)

        conn.commit()
        conn.close()

        main.delete_expired_recordings()

        conn2 = _conn(db_path)
        rows = conn2.execute("SELECT camera_id, s3_key FROM recordings").fetchall()
        cameras = {r["id"]: int(r["camera_number"]) for r in conn2.execute(
            "SELECT id, camera_number FROM cameras WHERE camera_number IS NOT NULL"
        ).fetchall()}

    orphans = 0
    for row in rows:
        camera_number = cameras.get(row["camera_id"])
        filename = row["s3_key"].rsplit("/", 1)[-1]
        local_path = recordings_root / f"camera{camera_number}" / filename
        if not local_path.exists():
            orphans += 1

    assert orphans == 0
    for path in deleted_expected:
        assert not path.exists()
    for path in kept_expected:
        assert path.exists()

