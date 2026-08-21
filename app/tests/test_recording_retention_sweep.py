"""R5 (recording-pipeline roadmap): tests for the recording retention
sweep -- the exact approved gate (a 2-day-plan customer's recording is
gone, object and catalog row, past day 2; a 30-day-plan customer's is
still present) plus the fail-safe edge cases around it.

Imports recording_retention_sweep, which imports partner_db -- per
this project's own already-documented constraint, partner_db's own
import-time schema init requires /app to exist, so this file can only
run inside the deployed container or via Windows-native Python, not
this WSL host's plain python3/pytest. Every test explicitly redirects
to a throwaway sqlite file via override_target() before seeding or
querying anything, so nothing here ever touches the real production
database. S3 deletion is always mocked -- no real AWS call is ever
made by this test file.
"""

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from database_backend import override_target
from partner_db import initialize_database

import recording_retention_sweep as rrs


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_r5.db"


def _seed_tenant(conn, customer_id, suffix):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, "partner-1", f"Test Co {suffix}", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
        (f"site-{suffix}", customer_id, "Main", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (f"appl-{suffix}", customer_id, f"site-{suffix}", f"AIC-{suffix}", "2026-01-01"),
    )


def _seed_plan(conn, customer_id, retention_days, created_at="2026-01-01T00:00:00"):
    conn.execute(
        "INSERT INTO plans(id,customer_id,retention_days,created_at) VALUES(?,?,?,?)",
        (f"plan-{customer_id}-{created_at}", customer_id, retention_days, created_at),
    )


def _seed_recording(conn, recording_id, customer_id, site_id, appliance_id, camera_id, started_at, status="available"):
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, "Camera", "2026-01-01"),
    )
    s3_key = f"recordings/{customer_id}/{site_id}/{appliance_id}/{camera_id}/2026/08/21/{recording_id}.mkv"
    ended_at = (datetime.fromisoformat(started_at) + timedelta(minutes=5)).isoformat()
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (recording_id, customer_id, site_id, appliance_id, camera_id, s3_key, started_at, ended_at, status, started_at),
    )
    return s3_key


def test_customer_retention_days_uses_most_recent_plan(db_path):
    """A customer who upgraded plans must be evaluated against their
    CURRENT plan, not an old one -- confirms the "most recent plans
    row" convention this module deliberately reused rather than
    inventing a second one."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1", "a")
        _seed_plan(conn, "cust-1", retention_days=2, created_at="2026-01-01T00:00:00")
        _seed_plan(conn, "cust-1", retention_days=30, created_at="2026-06-01T00:00:00")  # later upgrade
        conn.commit()
        result = rrs._customer_retention_days(conn, "cust-1")
    assert result == 30


def test_customer_retention_days_none_when_no_plan_on_file(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-1", "a")
        conn.commit()
        result = rrs._customer_retention_days(conn, "cust-1")
    assert result is None


def test_two_day_plan_recording_is_deleted_past_day_two(db_path):
    """The approved gate, positive case: a 2-day-plan customer's
    recording confirmed gone -- both the (mocked) S3 object delete
    call and the catalog row -- once past its retention window."""
    now = datetime(2026, 8, 21, 12, 0, 0)
    started_at = (now - timedelta(days=3)).isoformat()
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-short", "s")
        _seed_plan(conn, "cust-short", retention_days=2)
        _seed_recording(conn, "rec-short", "cust-short", "site-s", "appl-s", "cam-s", started_at)
        conn.commit()

        with patch.object(rrs, "_delete_recording_object", return_value=True) as mock_delete:
            result = rrs.run_retention_sweep_tick(now=now)

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT COUNT(*) FROM recordings WHERE id='rec-short'").fetchone()[0]

    assert result == {"checked": 1, "deleted": 1}
    assert remaining == 0
    mock_delete.assert_called_once()


def test_thirty_day_plan_recording_is_not_deleted_at_day_three(db_path):
    """The approved gate, negative case: a 30-day-plan customer's
    recording, at the same age that already expired the 2-day
    customer's, is confirmed still present -- object delete never even
    attempted."""
    now = datetime(2026, 8, 21, 12, 0, 0)
    started_at = (now - timedelta(days=3)).isoformat()
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-long", "l")
        _seed_plan(conn, "cust-long", retention_days=30)
        _seed_recording(conn, "rec-long", "cust-long", "site-l", "appl-l", "cam-l", started_at)
        conn.commit()

        with patch.object(rrs, "_delete_recording_object", return_value=True) as mock_delete:
            result = rrs.run_retention_sweep_tick(now=now)

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT COUNT(*) FROM recordings WHERE id='rec-long'").fetchone()[0]

    assert result == {"checked": 0, "deleted": 0}
    assert remaining == 1
    mock_delete.assert_not_called()


def test_recording_with_no_plan_on_file_is_never_deleted_regardless_of_age(db_path):
    now = datetime(2026, 8, 21, 12, 0, 0)
    started_at = (now - timedelta(days=9999)).isoformat()
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-noplan", "n")
        # deliberately no plan row for cust-noplan
        _seed_recording(conn, "rec-noplan", "cust-noplan", "site-n", "appl-n", "cam-n", started_at)
        conn.commit()

        with patch.object(rrs, "_delete_recording_object", return_value=True) as mock_delete:
            result = rrs.run_retention_sweep_tick(now=now)

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT COUNT(*) FROM recordings WHERE id='rec-noplan'").fetchone()[0]

    assert result == {"checked": 0, "deleted": 0}
    assert remaining == 1
    mock_delete.assert_not_called()


def test_failed_object_delete_keeps_the_catalog_row_for_retry(db_path):
    now = datetime(2026, 8, 21, 12, 0, 0)
    started_at = (now - timedelta(days=3)).isoformat()
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-fail", "f")
        _seed_plan(conn, "cust-fail", retention_days=2)
        _seed_recording(conn, "rec-fail", "cust-fail", "site-f", "appl-f", "cam-f", started_at)
        conn.commit()

        with patch.object(rrs, "_delete_recording_object", return_value=False):
            result = rrs.run_retention_sweep_tick(now=now)

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT COUNT(*) FROM recordings WHERE id='rec-fail'").fetchone()[0]

    assert result == {"checked": 1, "deleted": 0}
    assert remaining == 1  # unconfirmed delete -> row stays, retried next tick


def test_delete_object_fails_closed_when_role_arn_unset(monkeypatch):
    monkeypatch.delenv("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", raising=False)
    assert rrs._delete_recording_object("recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mkv") is False


def test_delete_object_fails_closed_when_bucket_unset(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", "arn:aws:iam::123456789012:role/fake")
    monkeypatch.delenv("ANYAICAM_RECORDING_S3_BUCKET", raising=False)
    assert rrs._delete_recording_object("recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mkv") is False


def test_recording_exactly_at_the_retention_boundary_is_not_yet_expired(db_path):
    """timedelta(days=N) strictly greater-than -- a recording exactly N
    days old is not yet past its window, only one that's MORE than N
    days old is. Confirms the boundary isn't off-by-one in the unsafe
    direction (deleting one day early)."""
    now = datetime(2026, 8, 21, 12, 0, 0)
    started_at = (now - timedelta(days=2)).isoformat()  # exactly 2 days old, retention_days=2
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant(conn, "cust-edge", "e")
        _seed_plan(conn, "cust-edge", retention_days=2)
        _seed_recording(conn, "rec-edge", "cust-edge", "site-e", "appl-e", "cam-e", started_at)
        conn.commit()

        with patch.object(rrs, "_delete_recording_object", return_value=True) as mock_delete:
            rrs.run_retention_sweep_tick(now=now)

    mock_delete.assert_not_called()
