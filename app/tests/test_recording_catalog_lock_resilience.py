"""Regression coverage for a real, confirmed-live Ryzen defect
(2026-09-22): the Playback/recording pipeline was found stuck showing
recordings from roughly a day earlier, and recent events often showed
"not ready"/no playable video, even though fresh local .mkv files were
being written continuously and correctly.

Traced end to end (detection -> local Event-mode persistence -> clip
file -> recording catalog row -> linked_recording_for -> Playback API)
directly on the appliance: local clip files were fine; the local
`recordings` catalog table for every camera had not received a single
new row in over 22 hours. Reproduced live by minting a real customer
session and calling the actual GET /api/customer/recordings/{camera_id}
route (the exact one the Playback page's own JS calls): it 500'd with
`sqlite3.OperationalError: database is locked` inside
`_catalog_local_recordings_for_camera()` (main.py).

Root cause: that function ran its entire multi-file backfill scan as
ONE transaction (database_backend.connect() only commits once, at the
very end of its `with connection()` block), held open across a real
ffprobe subprocess call per newly-discovered file. On an appliance with
several other independent writers hitting the same SQLite file every
few seconds (motion/AI detection, People Counting, event recording,
analytics sync), a lock collision partway through a multi-file backlog
scan rolled back EVERY file already inserted earlier in that same call
-- so once the catalog fell behind by any real backlog, each attempt to
catch up took long enough (one write lock held for the whole scan) that
a collision became more likely, not less, and every single attempt lost
all of its own progress. The backlog could only ever grow, never shrink
-- exactly the "stuck" symptom.

Fix: each newly-discovered file is committed the instant it's inserted,
with a small bounded retry on `sqlite3.OperationalError`, and a file
that still can't be inserted after retries is skipped (logged) rather
than raising -- so a lock collision can only ever cost one file's worth
of retry time, never erase earlier progress in the same call, and can
never turn a real customer's Playback request into a 500.
"""
import os
import sqlite3
import time as time_module
from contextlib import contextmanager
from datetime import datetime

import pytest

import main
import partner_db
from database_backend import override_target
from partner_db import connection, initialize_database


class _FlakyDB:
    """Wraps a real sqlite3 connection, injecting sqlite3.OperationalError
    on specific INSERT calls chosen by `should_fail`, and delegating
    everything else (including other statements and every non-execute
    attribute -- .commit()/.rollback()/.close()/row_factory) straight
    through. sqlite3.Connection is a C/immutable type that cannot be
    monkeypatched directly (`TypeError: cannot set 'execute' attribute
    of immutable type 'sqlite3.Connection'`), so the interception
    happens one level up, on partner_db.connection() itself, which
    `_catalog_local_recordings_for_camera()` re-imports fresh on every
    call (`from partner_db import connection`, inside the function
    body) -- monkeypatching the module-level name is enough."""

    def __init__(self, real_db, should_fail):
        self._real_db = real_db
        self._should_fail = should_fail

    def execute(self, sql, params=()):
        if self._should_fail(sql, params):
            raise sqlite3.OperationalError("database is locked")
        return self._real_db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real_db, name)


def _flaky_connection_factory(should_fail):
    real_connection = partner_db.connection

    @contextmanager
    def flaky_connection():
        with real_connection() as db:
            yield _FlakyDB(db, should_fail)

    return flaky_connection


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_recording_catalog_lock_resilience.db"


def _seed(db_path, camera_id="cam-1", camera_number=1):
    now = "2026-09-21T00:00:00"
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Customer','cust1@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Test Site',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (camera_id, "cust-1", "site-1", "appl-1", "Camera", camera_number, "configured", now),
            )


def _write_file(camera_folder, camera_number, dt, age_seconds=200):
    camera_folder.mkdir(parents=True, exist_ok=True)
    path = camera_folder / f"camera{camera_number}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path.write_bytes(b"x" * 1024)
    old_time = time_module.time() - age_seconds
    os.utime(path, (old_time, old_time))
    return path


def _recording_count(db_path, camera_id="cam-1"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT COUNT(*) AS n FROM recordings WHERE camera_id=?", (camera_id,)).fetchone()["n"]


def test_a_transient_lock_on_one_file_does_not_roll_back_earlier_files_already_catalogued_this_call(db_path, tmp_path, monkeypatch):
    """The actual confirmed-live defect: a lock collision on file 2 of 3
    must not erase file 1's already-successful insert from this same
    call. All three end up catalogued once the transient collision
    clears on retry."""
    _seed(db_path)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    camera_folder = tmp_path / "recordings" / "camera1"
    _write_file(camera_folder, 1, datetime(2026, 9, 21, 10, 0, 0))
    _write_file(camera_folder, 1, datetime(2026, 9, 21, 10, 5, 0))
    _write_file(camera_folder, 1, datetime(2026, 9, 21, 10, 10, 0))

    state = {"insert_calls": 0}

    def should_fail(sql, params):
        if not sql.startswith("INSERT INTO recordings"):
            return False
        state["insert_calls"] += 1
        return state["insert_calls"] == 2  # the second file's first insert attempt only

    monkeypatch.setattr(partner_db, "connection", _flaky_connection_factory(should_fail))
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    with override_target(sqlite_path=str(db_path)):
        added = main._catalog_local_recordings_for_camera("cam-1")

    assert added == 3
    assert _recording_count(db_path) == 3
    # A real retry happened (4 insert attempts for 3 files: file 2 failed once then succeeded).
    assert state["insert_calls"] == 4


def test_a_persistently_locked_file_is_skipped_without_raising_or_blocking_other_files(db_path, tmp_path, monkeypatch):
    """After exhausting retries for one stubborn file, the function logs
    and moves on rather than raising -- this is what keeps the real
    customer-facing GET /api/customer/recordings/{camera_id} route from
    ever 500ing just because one file's insert lost every race against
    a concurrent writer."""
    _seed(db_path)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    camera_folder = tmp_path / "recordings" / "camera1"
    _write_file(camera_folder, 1, datetime(2026, 9, 21, 10, 0, 0))
    stuck_path = _write_file(camera_folder, 1, datetime(2026, 9, 21, 10, 5, 0))
    _write_file(camera_folder, 1, datetime(2026, 9, 21, 10, 10, 0))

    stuck_marker = stuck_path.name

    def should_fail(sql, params):
        return sql.startswith("INSERT INTO recordings") and any(stuck_marker in str(p) for p in params)

    monkeypatch.setattr(partner_db, "connection", _flaky_connection_factory(should_fail))
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    with override_target(sqlite_path=str(db_path)):
        added = main._catalog_local_recordings_for_camera("cam-1")  # must not raise

    assert added == 2  # the two healthy files -- never the persistently-stuck one
    assert _recording_count(db_path) == 2


def test_a_persistently_locked_file_is_retried_on_the_next_call_not_lost_forever(db_path, tmp_path, monkeypatch):
    """A file skipped this pass (still too contested) is not treated as
    already-cataloged -- the very next call (e.g. the customer's next
    Playback page load/retry) picks it up once contention has cleared,
    which is what lets the appliance actually catch up on a backlog
    instead of staying stuck on the same file forever."""
    _seed(db_path)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    camera_folder = tmp_path / "recordings" / "camera1"
    _write_file(camera_folder, 1, datetime(2026, 9, 21, 10, 0, 0))

    def always_locked(sql, params):
        return sql.startswith("INSERT INTO recordings")

    real_connection = partner_db.connection
    monkeypatch.setattr(partner_db, "connection", _flaky_connection_factory(always_locked))
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    with override_target(sqlite_path=str(db_path)):
        first_pass = main._catalog_local_recordings_for_camera("cam-1")
    assert first_pass == 0
    assert _recording_count(db_path) == 0

    monkeypatch.setattr(partner_db, "connection", real_connection)  # contention cleared
    with override_target(sqlite_path=str(db_path)):
        second_pass = main._catalog_local_recordings_for_camera("cam-1")
    assert second_pass == 1
    assert _recording_count(db_path) == 1
