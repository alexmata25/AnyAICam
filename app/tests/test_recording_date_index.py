"""Playback date-index (2026-09-02): tests for the recording-index work
that lets a customer retrieve continuous-recording footage by camera ->
date -> time, including older retained days such as last week.

Covers _catalog_local_recordings_for_camera() (Phase 1, reconciled
verbatim from the accepted live production implementation),
_customer_recordings_for_date()/_local_date_bounds_to_utc() (Phase 2),
_customer_recording_dates() (Phase 3), the date=-aware
/api/customer/recordings/{camera_id} route and the new
/api/customer/recordings/{camera_id}/dates route (Phase 4), and the
on-demand cataloging that makes an old retained date discoverable even
if that camera's Playback page has never previously been opened
(Phase 5).

Follows this project's own already-documented pattern for these tests
(see test_recordings_catalog.py/test_customer_recordings_r4.py): a
throwaway sqlite database via database_backend.override_target(), with
partner_db.initialize_database() creating the real schema (including
the recordings table's FOREIGN KEY targets and the camera_number
column db_migrations.py adds). main.py hardcodes /app/... paths at
import time, so -- like every other test file here -- this only runs
inside the deployed container or via Windows-native Python.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_recording_date_index.db"


def _seed_base_tenant(conn, camera_id="cam-1", camera_number=1):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (camera_id, "cust-1", "site-1", "appl-1", "Camera", camera_number, "2026-01-01"),
    )
    conn.commit()


def _seed_recording(conn, camera_id, s3_key, started_at, ended_at, status="available"):
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"rec-{s3_key}", "cust-1", "site-1", "appl-1", camera_id, s3_key, started_at, ended_at, status, "2026-08-21T00:05:01"),
    )
    conn.commit()


def _write_recording_file(camera_folder, camera_number, dt, size_bytes=1024, age_seconds=200):
    """A real .mkv on disk named the way recording_start()'s own filename
    convention parses it (camera{N}_{YYYY-MM-DD}_{HH-MM-SS}.mkv), old
    enough (older than CLOUD_UPLOAD_MIN_FILE_AGE_SECONDS) to pass
    _catalog_local_recordings_for_camera()'s own still-writing safety
    gate."""
    camera_folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = camera_folder / name
    path.write_bytes(b"x" * size_bytes)
    import os
    old_time = __import__("time").time() - age_seconds
    os.utime(path, (old_time, old_time))
    return path


# --------------------------------------------------------- Phase 1: local filename backfill


def test_local_filename_backfill_creates_a_recording_row(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)

        camera_folder = tmp_path / "recordings" / "camera1"
        _write_recording_file(camera_folder, 1, datetime(2026, 8, 25, 14, 0, 0))

        added = main._catalog_local_recordings_for_camera("cam-1")
        rows = conn.execute("SELECT camera_id, started_at, ended_at FROM recordings").fetchall()

    assert added == 1
    assert len(rows) == 1
    assert rows[0][0] == "cam-1"
    # recording_start() (filename parsing), not filesystem mtime, is the
    # authoritative timestamp -- the file's mtime was backdated above for
    # the "not still being written" gate only.
    assert rows[0][1] == "2026-08-25T14:00:00"
    assert rows[0][2] == "2026-08-25T14:05:00"


def test_a_file_too_young_to_have_finished_writing_is_skipped(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)

        camera_folder = tmp_path / "recordings" / "camera1"
        # age_seconds=5 is younger than CLOUD_UPLOAD_MIN_FILE_AGE_SECONDS
        # (>=30) -- ffmpeg may still be actively writing this segment.
        _write_recording_file(camera_folder, 1, datetime(2026, 8, 25, 14, 5, 0), age_seconds=5)

        added = main._catalog_local_recordings_for_camera("cam-1")
        count = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]

    assert added == 0
    assert count == 0


# --------------------------------------------------------- Phase 1: duplicate catalog protection


def test_cataloging_the_same_files_twice_never_creates_duplicate_rows(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)

        camera_folder = tmp_path / "recordings" / "camera1"
        _write_recording_file(camera_folder, 1, datetime(2026, 8, 25, 14, 0, 0))
        _write_recording_file(camera_folder, 1, datetime(2026, 8, 25, 14, 5, 0))

        first_pass = main._catalog_local_recordings_for_camera("cam-1")
        second_pass = main._catalog_local_recordings_for_camera("cam-1")
        third_pass = main._catalog_local_recordings_for_camera("cam-1")
        count = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]

    assert first_pass == 2
    assert second_pass == 0
    assert third_pass == 0
    assert count == 2  # (camera_id, s3_key) UNIQUE constraint holds regardless


def test_camera_with_no_camera_number_is_skipped_not_crashed(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
        # camera_number left NULL -- e.g. a camera never fully provisioned.
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('cam-unprovisioned','cust-1','site-1','appl-1','Camera','2026-01-01')")
        conn.commit()

        added = main._catalog_local_recordings_for_camera("cam-unprovisioned")

    assert added == 0


# --------------------------------------------------------- Phase 2: local-date -> UTC bounds


def test_local_date_bounds_convert_central_midnight_to_utc():
    query_start, query_end = main._local_date_bounds_to_utc("2026-08-25")
    # America/Chicago in late August is CDT (UTC-5) -- local midnight
    # 2026-08-25 00:00:00 is 2026-08-25 05:00:00 UTC.
    assert query_start == "2026-08-25T05:00:00"
    assert query_end == "2026-08-26T05:00:00"


def test_local_date_bounds_use_the_same_convention_as_customer_camera_events():
    """_local_date_bounds_to_utc() must not drift from the already-proven
    _customer_camera_events() local-day conversion -- computed here via
    the exact same APPLIANCE_TIMEZONE round trip, independently, and
    compared."""
    date = "2026-01-15"  # winter -- CST (UTC-6), a different offset than August
    query_start, query_end = main._local_date_bounds_to_utc(date)
    local_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=main.APPLIANCE_TIMEZONE)
    expected_start = local_start.astimezone(main.ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    expected_end = (local_start + timedelta(days=1)).astimezone(main.ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    assert query_start == expected_start
    assert query_end == expected_end


# --------------------------------------------------------- Phase 2: recording crossing midnight


def test_recording_that_starts_before_local_midnight_and_ends_after_appears_on_both_dates(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        # Local Central time 2026-08-24 23:58:00 -> 2026-08-25 00:03:00
        # (UTC 2026-08-25 04:58:00 -> 05:03:00) -- genuinely straddles
        # local midnight.
        _seed_recording(conn, "cam-1", "seg-midnight.mkv", "2026-08-25T04:58:00", "2026-08-25T05:03:00")

        day_before = main._customer_recordings_for_date("cam-1", "2026-08-24")
        day_of = main._customer_recordings_for_date("cam-1", "2026-08-25")
        day_after = main._customer_recordings_for_date("cam-1", "2026-08-26")

    assert len(day_before) == 1
    assert len(day_of) == 1
    assert day_before[0]["id"] == day_of[0]["id"]
    assert day_after == []


def test_a_started_at_only_bound_would_have_wrongly_excluded_the_midnight_segment(db_path, tmp_path, monkeypatch):
    """Directly proves the interval-overlap requirement: a naive
    started_at-only filter for 2026-08-25 would miss a segment that
    started the previous local day and merely ran past midnight."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, "cam-1", "seg-midnight.mkv", "2026-08-25T04:58:00", "2026-08-25T05:03:00")

        query_start, query_end = main._local_date_bounds_to_utc("2026-08-25")
        started_at_only_count = conn.execute(
            "SELECT COUNT(*) FROM recordings WHERE camera_id=? AND started_at>=? AND started_at<?",
            ("cam-1", query_start, query_end),
        ).fetchone()[0]

        day_of = main._customer_recordings_for_date("cam-1", "2026-08-25")

    assert started_at_only_count == 0  # the naive approach this function deliberately avoids
    assert len(day_of) == 1  # the real, shipped overlap-based query correctly includes it


# --------------------------------------------------------- Phase 2: chronological ordering


def test_several_segments_on_one_day_are_returned_chronologically(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        # Inserted out of order on purpose.
        _seed_recording(conn, "cam-1", "seg-c.mkv", "2026-08-25T18:00:00", "2026-08-25T18:05:00")
        _seed_recording(conn, "cam-1", "seg-a.mkv", "2026-08-25T05:00:00", "2026-08-25T05:05:00")
        _seed_recording(conn, "cam-1", "seg-b.mkv", "2026-08-25T12:00:00", "2026-08-25T12:05:00")

        result = main._customer_recordings_for_date("cam-1", "2026-08-25")

    assert [row["name"] for row in result] == ["seg-a.mkv", "seg-b.mkv", "seg-c.mkv"]


def test_date_with_no_recordings_returns_an_empty_list(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, "cam-1", "seg-a.mkv", "2026-08-25T05:00:00", "2026-08-25T05:05:00")

        result = main._customer_recordings_for_date("cam-1", "2026-09-01")

    assert result == []


# --------------------------------------------------------- Phase 3: dates-with-recordings query


def test_customer_recording_dates_reports_every_local_date_with_footage(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, "cam-1", "seg-1.mkv", "2026-08-20T12:00:00", "2026-08-20T12:05:00")
        _seed_recording(conn, "cam-1", "seg-2.mkv", "2026-08-25T12:00:00", "2026-08-25T12:05:00")
        # Crosses local midnight -- contributes both overlapped dates.
        _seed_recording(conn, "cam-1", "seg-3.mkv", "2026-08-27T04:58:00", "2026-08-27T05:03:00")

        dates = main._customer_recording_dates("cam-1")

    assert dates == ["2026-08-20", "2026-08-25", "2026-08-26", "2026-08-27"]


def test_customer_recording_dates_never_uses_a_naive_utc_date_group_by(db_path, tmp_path, monkeypatch):
    """A recording near a UTC-day boundary that is NOT near local
    midnight must still be attributed to its one true local date, not
    split by a bare SQL date() extraction on the stored UTC column."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        # UTC 2026-08-26T00:02:00 is local (Central, UTC-5) 2026-08-25
        # 19:02:00 -- entirely within one local day, despite crossing a
        # UTC midnight.
        _seed_recording(conn, "cam-1", "seg-utc-boundary.mkv", "2026-08-26T00:02:00", "2026-08-26T00:07:00")

        dates = main._customer_recording_dates("cam-1")

    assert dates == ["2026-08-25"]


# --------------------------------------------------------- Phase 5: on-demand cataloging of old retained days


def test_an_old_retained_date_is_discovered_the_first_time_it_is_queried(db_path, tmp_path, monkeypatch):
    """No prior Playback visit for this camera has ever happened (the
    recordings table starts empty) -- an old file already on disk must
    still be discoverable purely by querying that date, with no
    separate always-running background service."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)

        camera_folder = tmp_path / "recordings" / "camera1"
        old_date = datetime.now() - timedelta(days=7)
        _write_recording_file(camera_folder, 1, old_date.replace(hour=10, minute=0, second=0, microsecond=0))

        before_count = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
        result = main._customer_recordings_for_date("cam-1", old_date.strftime("%Y-%m-%d"))
        after_count = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]

    assert before_count == 0
    assert len(result) == 1
    assert after_count == 1


def test_an_old_retained_date_is_also_discovered_through_the_dates_query(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)

        camera_folder = tmp_path / "recordings" / "camera1"
        old_date = datetime.now() - timedelta(days=10)
        _write_recording_file(camera_folder, 1, old_date.replace(hour=9, minute=0, second=0, microsecond=0))

        dates = main._customer_recording_dates("cam-1")

    assert old_date.strftime("%Y-%m-%d") in dates


# --------------------------------------------------------- retention: stale rows never presented as playable


def test_expired_status_recordings_are_excluded_from_date_query(db_path, tmp_path, monkeypatch):
    """Reuses the existing retention/index cleanup behavior's own
    status='expired' convention (see _customer_camera_recordings'
    already-accepted status filtering) -- this feature does not build a
    second cleanup system."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, "cam-1", "seg-live.mkv", "2026-08-25T12:00:00", "2026-08-25T12:05:00", status="available")
        _seed_recording(conn, "cam-1", "seg-expired.mkv", "2026-08-25T13:00:00", "2026-08-25T13:05:00", status="expired")

        result = main._customer_recordings_for_date("cam-1", "2026-08-25")

    assert [row["name"] for row in result] == ["seg-live.mkv"]


def test_expired_status_recordings_are_excluded_from_the_dates_list(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        # This camera's only footage on 2026-08-30 has already been
        # deleted/expired by retention -- that date must not be offered
        # as a playable calendar entry.
        _seed_recording(conn, "cam-1", "seg-gone.mkv", "2026-08-30T12:00:00", "2026-08-30T12:05:00", status="expired")

        dates = main._customer_recording_dates("cam-1")

    assert "2026-08-30" not in dates


# --------------------------------------------------------- existing behavior unchanged: near= / pagination


def test_existing_near_lookup_is_unaffected_by_the_date_index_work(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, "cam-1", "seg-a.mkv", "2026-08-25T12:00:00", "2026-08-25T12:05:00")

        result = main._customer_recording_rows("cam-1", near="2026-08-25T12:02:00")

    assert len(result) == 1
    assert result[0]["name"] == "seg-a.mkv"


def test_existing_limit_before_pagination_is_unaffected_by_the_date_index_work(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        for hour in range(5):
            _seed_recording(conn, "cam-1", f"seg-{hour}.mkv", f"2026-08-25T0{hour}:00:00", f"2026-08-25T0{hour}:05:00")

        first_page = main._customer_recording_rows("cam-1", limit=2)
        older_page = main._customer_recording_rows("cam-1", limit=2, before=first_page[0]["start"])

    assert [row["name"] for row in first_page] == ["seg-3.mkv", "seg-4.mkv"]
    assert [row["name"] for row in older_page] == ["seg-1.mkv", "seg-2.mkv"]


# --------------------------------------------------------- routes: date param validation and auth


def test_route_rejects_invalid_date_format(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)
        with pytest.raises(main.HTTPException) as excinfo:
            main.customer_recordings_metadata(camera_id="cam-1", request=None, date="not-a-date")
    assert excinfo.value.status_code == 400


def test_route_returns_date_scoped_clips_when_date_is_given(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, "cam-1", "seg-a.mkv", "2026-08-25T12:00:00", "2026-08-25T12:05:00")

        monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)
        result = main.customer_recordings_metadata(camera_id="cam-1", request=None, date="2026-08-25")

    assert result == {"clips": [{"id": "rec-seg-a.mkv", "start": "2026-08-25T12:00:00", "end": "2026-08-25T12:05:00", "name": "seg-a.mkv"}]}


def test_unauthorized_camera_is_rejected_for_the_date_route(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: False)
        with pytest.raises(main.HTTPException) as excinfo:
            main.customer_recordings_metadata(camera_id="cam-not-mine", request=None, date="2026-08-25")
    assert excinfo.value.status_code == 403


def test_unauthorized_camera_is_rejected_for_the_dates_availability_route(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: False)
        with pytest.raises(main.HTTPException) as excinfo:
            main.customer_recording_dates(camera_id="cam-not-mine", request=None)
    assert excinfo.value.status_code == 403


def test_dates_route_returns_the_available_dates_list(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, "cam-1", "seg-a.mkv", "2026-08-25T12:00:00", "2026-08-25T12:05:00")

        monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)
        result = main.customer_recording_dates(camera_id="cam-1", request=None)

    assert result == {"dates": ["2026-08-25"]}
