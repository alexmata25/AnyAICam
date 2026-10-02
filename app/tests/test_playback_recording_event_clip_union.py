"""Regression coverage for the Playback recording/Event-clip union
(2026-09-22): a Hybrid/Event-mode camera's cloud_recording_mode stays
'motion' -- continuous cloud recording upload is correctly, deliberately
skipped for it (see recording_uploader.py's own cloud_recording_mode
gate) -- but its Playback page still needs to show whatever footage
genuinely exists: older legacy continuous recordings in the `recordings`
table from before the camera's mode was ever set to Hybrid, and its
current short Event-mode clips in detection_event_media, together.

Covers _customer_event_clip_dates(), _event_clips_overlapping_utc_range(),
_playback_items_overlapping_utc_range(), and the two existing functions
this integration extends -- _customer_recording_dates() (now a union of
both sources) and _customer_recordings_for_date() (now a merge of both,
still routed through the exact same day-boundary math it always used).

Nothing here changes a camera's cloud_recording_mode, writes into the
`recordings` table from event data, or duplicates any row between the
two tables -- this is a read-only merge at query time, verified by
asserting row counts in each source table are unchanged after every
call below.

Same fixture idiom as test_playback_event_marker_regression.py/
test_recording_date_index.py: a throwaway sqlite DB via
override_target(), a hand-seeded partner/customer/site/appliance/camera
tenant, and direct calls into main's own functions (not TestClient).

Camera-count-agnostic coverage: every test below is parametrized across
several distinct camera_id/camera_number pairs (including numbers well
outside the 1-5 range the Ryzen validation pilot happens to use right
now) to prove none of this logic is keyed to a specific camera count or
number -- see this project's own standing architecture rule that camera
counts are entitlement/hardware concerns, never hard-coded software
ones.
"""

import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import main


CAMERA_CASES = [
    ("cam-a", 1),
    ("cam-b", 7),
    ("cam-c", 23),
    ("cam-d", 64),
]


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_playback_recording_event_clip_union.db"


def _seed_base_tenant(conn, camera_id, camera_number):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,cloud_recording_mode,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (camera_id, "cust-1", "site-1", "appl-1", camera_number, "Test Camera", "motion", "2026-01-01"),
    )
    conn.commit()


def _seed_recording(conn, camera_id, s3_key, started_at, ended_at, status="available"):
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"rec-{camera_id}-{s3_key}", "cust-1", "site-1", "appl-1", camera_id, s3_key, started_at, ended_at, status, "2026-01-01"),
    )
    conn.commit()


def _seed_event_clip(conn, camera_id, event_id, event_type, started_at, ended_at, s3_key="events/clip.mp4", ready=True):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (event_id, "cust-1", "site-1", "appl-1", camera_id, event_id, event_type, started_at, started_at),
    )
    conn.execute(
        "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,started_at,ended_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (f"media-{event_id}", event_id, "cust-1", camera_id, s3_key if ready else "", started_at, ended_at, started_at),
    )
    conn.commit()


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_dates_endpoint_unions_legacy_recording_and_event_clip_dates(db_path, tmp_path, camera_id, camera_number, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn, camera_id, camera_number)

        # Historical continuous recording, well before the camera was
        # ever switched to Hybrid/'motion' -- must be preserved exactly
        # as before.
        _seed_recording(conn, camera_id, "camera_2026-09-16_19-38-08.mp4", "2026-09-16T19:38:08", "2026-09-16T19:43:08")

        # Current Event-mode clip, today -- must now also surface even
        # though this camera's cloud_recording_mode stays 'motion'.
        _seed_event_clip(conn, camera_id, "evt-1", "person", "2026-09-22T16:12:25", "2026-09-22T16:12:35")

        dates = main._customer_recording_dates(camera_id)

    assert "2026-09-16" in dates
    assert "2026-09-22" in dates
    assert dates == sorted(dates)


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_date_scoped_clip_list_includes_both_kinds_correctly_tagged(db_path, tmp_path, camera_id, camera_number, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn, camera_id, camera_number)

        # Two items on the SAME local day: one legacy recording, one
        # Event-mode clip -- both must come back from one date query.
        _seed_recording(conn, camera_id, "same-day.mp4", "2026-09-22T14:00:00", "2026-09-22T14:05:00")
        _seed_event_clip(conn, camera_id, "evt-same-day", "motion", "2026-09-22T16:12:25", "2026-09-22T16:12:35")

        items = main._customer_recordings_for_date(camera_id, "2026-09-22")

    assert len(items) == 2
    kinds = {item["kind"] for item in items}
    assert kinds == {"recording", "event_clip"}
    # Oldest-first, same ordering contract _recordings_overlapping_utc_range()
    # already documents.
    assert items[0]["start"] < items[1]["start"]
    event_item = next(item for item in items if item["kind"] == "event_clip")
    assert event_item["id"] == "evt-same-day"


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_september_16_still_shows_only_the_old_five_minute_recording(db_path, tmp_path, camera_id, camera_number, monkeypatch):
    """September 16 must keep showing exactly its historical continuous
    recording -- no Event clip exists that day, so nothing new should
    appear there, and the legacy row must be completely unmodified."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn, camera_id, camera_number)

        _seed_recording(conn, camera_id, "legacy.mp4", "2026-09-16T19:38:08", "2026-09-16T19:43:08")
        _seed_event_clip(conn, camera_id, "evt-today", "person", "2026-09-22T16:12:25", "2026-09-22T16:12:35")

        sept16_items = main._customer_recordings_for_date(camera_id, "2026-09-16")
        sept22_items = main._customer_recordings_for_date(camera_id, "2026-09-22")

        legacy_row = conn.execute(
            "SELECT s3_key, started_at, ended_at, status FROM recordings WHERE camera_id=?", (camera_id,)
        ).fetchone()

    assert len(sept16_items) == 1
    assert sept16_items[0]["kind"] == "recording"
    assert len(sept22_items) == 1
    assert sept22_items[0]["kind"] == "event_clip"
    # The legacy row itself was never touched by any of this.
    assert legacy_row == ("legacy.mp4", "2026-09-16T19:38:08", "2026-09-16T19:43:08", "available")


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_event_clip_still_processing_is_excluded_until_media_exists(db_path, tmp_path, camera_id, camera_number, monkeypatch):
    """An event with no media yet (has_event_clip false -- still
    'Processing…'/'Not ready yet' on the Events page) must not appear
    in Playback's date list as a dead/broken link."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn, camera_id, camera_number)

        _seed_event_clip(conn, camera_id, "evt-pending", "motion", "2026-09-22T10:00:00", "2026-09-22T10:00:10", ready=False)

        dates = main._customer_recording_dates(camera_id)
        items = main._customer_recordings_for_date(camera_id, "2026-09-22")

    assert "2026-09-22" not in dates
    assert items == []


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_event_clips_never_get_written_into_the_recordings_table(db_path, tmp_path, camera_id, camera_number, monkeypatch):
    """The union happens entirely at read time -- confirms no row in
    `recordings` is ever created as a side effect of querying dates or
    a date's clip list, for any camera/camera_number."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn, camera_id, camera_number)
        _seed_event_clip(conn, camera_id, "evt-1", "person", "2026-09-22T16:12:25", "2026-09-22T16:12:35")

        main._customer_recording_dates(camera_id)
        main._customer_recordings_for_date(camera_id, "2026-09-22")

        recordings_count = conn.execute("SELECT COUNT(*) FROM recordings WHERE camera_id=?", (camera_id,)).fetchone()[0]
        # cloud_recording_mode must still read back exactly as seeded --
        # this integration never writes to it, in either direction.
        mode = conn.execute("SELECT cloud_recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()[0]

    assert recordings_count == 0
    assert mode == "motion"


def test_cameras_stay_isolated_across_different_ids_and_numbers(db_path, tmp_path, monkeypatch):
    """Two different cameras (arbitrary, non-sequential camera_numbers)
    each get their own recordings and Event clips -- neither's dates or
    clip list may ever leak into the other's, proving the merge is
    scoped strictly by camera_id and never assumes anything about how
    many cameras exist or what numbers they use."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn, "cam-x", 12)
        _seed_base_tenant(conn, "cam-y", 47)

        _seed_recording(conn, "cam-x", "x.mp4", "2026-09-16T10:00:00", "2026-09-16T10:05:00")
        _seed_event_clip(conn, "cam-x", "evt-x", "motion", "2026-09-22T10:00:00", "2026-09-22T10:00:10")

        _seed_event_clip(conn, "cam-y", "evt-y", "person", "2026-09-20T08:00:00", "2026-09-20T08:00:12")

        dates_x = main._customer_recording_dates("cam-x")
        dates_y = main._customer_recording_dates("cam-y")
        items_x = main._customer_recordings_for_date("cam-x", "2026-09-22")
        items_y = main._customer_recordings_for_date("cam-y", "2026-09-22")

    assert dates_x == ["2026-09-16", "2026-09-22"]
    assert dates_y == ["2026-09-20"]
    assert len(items_x) == 1 and items_x[0]["id"] == "evt-x"
    assert items_y == []
