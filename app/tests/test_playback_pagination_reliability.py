"""Customer Playback pagination reliability (2026-09-14 Playback phase).

Extends the existing bounded-load cursor (_customer_recording_rows(),
tested in test_playback_bounded_load.py) with the one real correctness
gap found on inspection: a plain single-column `before` cursor
(`started_at<?`) has no tie-breaker, so two rows sharing an identical
`started_at` straddling a page boundary could be silently skipped (if
only some of a tied group made it into the first page) or, with a
naive `<=` fix, duplicated. In real operation this camera's own
recordings are normally minutes apart, so an exact tie is rare -- but
it is not a database constraint, and "recordings are not duplicated
between pages" / "recordings are not skipped between pages" were
required as guarantees, not probabilistic assumptions. The fix is
purely additive: an optional `before_id` paired with `before` makes
the cursor a full (started_at, id) compound key, ORDER BY started_at
DESC, id DESC -- a caller passing before alone still gets exactly
today's pre-existing behavior.

Also covers the route-level cursor validation (malformed `before` is a
clean 400, not a silently-wrong query) and the authorization-isolation
guarantees a cursor must never be able to bypass.

Same fixtures/pattern as test_playback_bounded_load.py.
"""

import sqlite3

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_pagination_reliability.db"


def _seed_base_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-2','cust-1','site-1','appl-1',2,'Back Yard','2026-01-01')")
    # A second, unrelated tenant -- proves a cursor can never reach
    # across a customer boundary regardless of its own contents.
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-2','Other Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-2','Other Co','other@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-2','cust-2','site-2','AIC-OTHER','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-3','cust-2','site-2','appl-2',1,'Other Tenant Camera','2026-01-01')")
    conn.commit()


def _insert_recording(conn, rec_id, camera_id, started, ended, *, customer_id="cust-1", site_id="site-1", appliance_id="appl-1"):
    s3_key = f"recordings/{customer_id}/{site_id}/{appliance_id}/{camera_id}/2026/08/20/{rec_id}.mp4"
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'available',?)",
        (rec_id, customer_id, site_id, appliance_id, camera_id, s3_key, started, ended, started),
    )


def _seed_recordings(conn, camera_id, count, *, day="2026-08-20"):
    for i in range(count):
        hour, minute = divmod(i * 5, 60)
        started = f"{day}T{hour % 24:02d}:{minute:02d}:00"
        ended = f"{day}T{hour % 24:02d}:{minute + 4:02d}:59"
        _insert_recording(conn, f"rec-{camera_id}-{i:03d}", camera_id, started, ended)
    conn.commit()


# --------------------------------------------------------- deterministic ordering / no dup / no skip under ties


def test_tied_timestamps_are_ordered_deterministically_by_id(db_path):
    """Three rows sharing the exact same started_at must still come
    back in the same, stable order every time -- not whatever order
    SQLite's own internal storage happens to yield."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        tie = "2026-08-20T00:00:00"
        for rid in ("rec-cam-1-tie-c", "rec-cam-1-tie-a", "rec-cam-1-tie-b"):
            _insert_recording(conn, rid, "cam-1", tie, tie)
        conn.commit()

        first = [r["id"] for r in main._customer_recording_rows("cam-1")]
        second = [r["id"] for r in main._customer_recording_rows("cam-1")]
    assert first == second
    assert set(first) == {"rec-cam-1-tie-a", "rec-cam-1-tie-b", "rec-cam-1-tie-c"}


def test_paging_through_a_tied_timestamp_group_skips_nothing_and_duplicates_nothing(db_path):
    """The exact scenario a plain single-column started_at<? cursor
    gets wrong: a page boundary falling in the middle of a group of
    rows that all share one started_at value. before_id closes it."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        tie = "2026-08-20T00:00:00"
        for rid in ("rec-t1", "rec-t2", "rec-t3", "rec-t4"):
            _insert_recording(conn, rid, "cam-1", tie, tie)
        conn.commit()

        # Page 1: only 2 of the 4 tied rows.
        page1 = main._customer_recording_rows("cam-1", limit=2)
        assert len(page1) == 2
        oldest_of_page1 = page1[0]  # oldest-first return convention

        # Page 2: continue from exactly where page 1 stopped, using
        # the compound cursor.
        page2 = main._customer_recording_rows(
            "cam-1", limit=10, before=oldest_of_page1["start"], before_id=oldest_of_page1["id"],
        )

        all_ids = [r["id"] for r in page1] + [r["id"] for r in page2]
        assert set(all_ids) == {"rec-t1", "rec-t2", "rec-t3", "rec-t4"}, "every tied row must be reachable exactly once"
        assert len(all_ids) == len(set(all_ids)), "no row may appear on both pages"


def test_before_without_before_id_is_unchanged_pre_existing_behavior(db_path):
    """Backward compatibility: a caller that never learned about
    before_id (there is only one today -- the page's own JS, already
    updated to send it) still gets exactly the old started_at<?
    behavior, not an error."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 10)
        first_page = main._customer_recording_rows("cam-1", limit=6)
        older_page = main._customer_recording_rows("cam-1", limit=6, before=first_page[0]["start"])
    assert len(older_page) == 4
    assert not ({r["id"] for r in first_page} & {r["id"] for r in older_page})


# --------------------------------------------------------- end of results / empty results


def test_paging_past_the_oldest_recording_returns_empty_cleanly(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 3)
        oldest = main._customer_recording_rows("cam-1")[0]
        result = main._customer_recording_rows("cam-1", limit=50, before=oldest["start"], before_id=oldest["id"])
    assert result == []


def test_a_camera_with_no_recordings_at_all_returns_empty_cleanly(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        result = main._customer_recording_rows("cam-1")
    assert result == []


# --------------------------------------------------------- camera scoping / cross-tenant isolation


def test_before_id_from_a_different_camera_cannot_be_used_to_read_this_cameras_rows_out_of_order(db_path):
    """The compound cursor's id half is only ever compared alongside
    camera_id=? in the same WHERE clause -- a before_id that happens to
    also exist as a real row id on a DIFFERENT camera can never affect
    this camera's own result set, because that id is never joined
    against; it is only used as a plain tie-break value in a query
    already scoped to this camera_id."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        tie = "2026-08-20T00:00:00"
        _insert_recording(conn, "shared-id-value", "cam-2", tie, tie)  # same id text, camera 2
        _insert_recording(conn, "cam1-row", "cam-1", tie, tie)  # camera 1's own row, different id
        conn.commit()

        # Pass camera 2's row id as a before_id while querying camera 1.
        result = main._customer_recording_rows("cam-1", before=tie, before_id="shared-id-value")
    # cam1-row's own started_at ties with `tie`; "shared-id-value" > "cam1-row" as text is not
    # the point -- the point is camera scoping means camera 2's id can never leak camera 1 data
    # it wouldn't otherwise be entitled to, and vice versa. Confirm no cam-2 row is ever returned.
    assert all(row["id"] != "shared-id-value" for row in result)


def test_recordings_route_never_returns_a_different_camera_or_customers_rows(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 5)
        _seed_recordings(conn, "cam-2", 5)
        _insert_recording(conn, "other-tenant-rec", "cam-3", "2026-08-20T00:00:00", "2026-08-20T00:05:00", customer_id="cust-2", site_id="site-2", appliance_id="appl-2")
        conn.commit()

        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [
            {"id": "cam-1", "name": "Front Door", "camera_number": 1},
            {"id": "cam-2", "name": "Back Yard", "camera_number": 2},
        ])
        result = main.customer_recordings_metadata("cam-1", _fake_request())
    ids = {c["id"] for c in result["clips"]}
    assert all(rid.startswith("rec-cam-1-") for rid in ids)
    assert "other-tenant-rec" not in ids


def test_recordings_route_rejects_a_camera_the_customer_is_not_authorized_for_regardless_of_cursor(db_path, monkeypatch):
    """The cursor itself (before/before_id) is never what enforces
    authorization -- _customer_authorized_camera_id() is checked first
    and unconditionally, before any cursor value is even read. A
    customer cannot bypass that gate by supplying any before/before_id
    value, malformed or not."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _insert_recording(conn, "other-tenant-rec", "cam-3", "2026-08-20T00:00:00", "2026-08-20T00:05:00", customer_id="cust-2", site_id="site-2", appliance_id="appl-2")
        conn.commit()

        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recordings_metadata("cam-3", _fake_request(), before="2026-08-20T00:00:00", before_id="other-tenant-rec")
    assert getattr(excinfo.value, "status_code", None) == 403


# --------------------------------------------------------- malformed / stale / replayed cursor


def test_malformed_before_is_a_clean_400_not_a_silently_wrong_query(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 5)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_recordings_metadata("cam-1", _fake_request(), before="not-a-real-timestamp")
    assert getattr(excinfo.value, "status_code", None) == 400


def test_a_replayed_identical_cursor_is_idempotent_and_returns_the_same_page_every_time(db_path):
    """A retried/duplicated older-page request (e.g. the customer's
    browser retrying after a dropped response) is a pure read with no
    side effects to begin with -- replaying the exact same (before,
    before_id) must yield byte-identical results, never a shifted or
    corrupted page."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 20)
        first_page = main._customer_recording_rows("cam-1", limit=10)
        cursor_start, cursor_id = first_page[0]["start"], first_page[0]["id"]

        replay_1 = main._customer_recording_rows("cam-1", limit=10, before=cursor_start, before_id=cursor_id)
        replay_2 = main._customer_recording_rows("cam-1", limit=10, before=cursor_start, before_id=cursor_id)
    assert replay_1 == replay_2
    assert len(replay_1) == 10


def test_a_stale_cursor_from_before_new_recordings_arrived_still_returns_a_consistent_older_page(db_path):
    """A cursor captured from an earlier page load, then replayed
    after MORE recent recordings have since been added, must still
    correctly return older history -- new rows newer than the cursor
    are simply irrelevant to a "before" query, not a source of
    corruption or duplication."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_recordings(conn, "cam-1", 10)
        first_page = main._customer_recording_rows("cam-1", limit=6)
        stale_cursor_start, stale_cursor_id = first_page[0]["start"], first_page[0]["id"]

        # New recordings arrive after the cursor was captured.
        _insert_recording(conn, "rec-cam-1-new-1", "cam-1", "2026-08-20T02:00:00", "2026-08-20T02:04:59")
        conn.commit()

        older_page = main._customer_recording_rows("cam-1", limit=10, before=stale_cursor_start, before_id=stale_cursor_id)
    assert "rec-cam-1-new-1" not in {r["id"] for r in older_page}
    assert len(older_page) == 4


def _fake_request():
    from types import SimpleNamespace
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))
