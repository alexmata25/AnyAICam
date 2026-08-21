"""R4 (recording-pipeline roadmap): tests for the real recording-catalog
query wired into customer Playback, and the presigned-URL fail-closed
behavior.

Imports `main` -- per this project's own already-documented constraint
(main.py hardcodes /app/... paths at import time, assuming the process
runs inside the Docker image), this file can only run inside the
deployed container or via Windows-native Python, not this WSL host's
plain python3/pytest. Merely importing `main` triggers partner_db's own
idempotent schema init (CREATE TABLE IF NOT EXISTS only) against
whatever ANYAICAM_PARTNER_DB currently points at -- harmless -- but
every test below explicitly redirects to a throwaway sqlite file via
override_target() before seeding or querying anything, so nothing here
ever writes fake test rows into the real production database.
"""

import sqlite3
from unittest.mock import patch

import pytest

from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_r4.db"


def _seed_base_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")


def _seed_recording(conn, camera_id="cam-1", s3_key=None, status="available"):
    s3_key = s3_key or f"recordings/cust-1/site-1/appl-1/{camera_id}/2026/08/21/clip.mkv"
    conn.execute(f"INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('{camera_id}','cust-1','site-1','appl-1','Camera','2026-01-01')")
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"rec-{s3_key}", "cust-1", "site-1", "appl-1", camera_id, s3_key, "2026-08-21T00:00:00", "2026-08-21T00:05:00", status, "2026-08-21T00:05:01"),
    )
    conn.commit()


def test_presigned_url_fails_closed_when_role_arn_unset(monkeypatch):
    monkeypatch.delenv("ANYAICAM_RECORDING_READ_ROLE_ARN", raising=False)
    assert main._presigned_recording_url("recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mkv") is None


def test_presigned_url_fails_closed_when_bucket_unset(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_READ_ROLE_ARN", "arn:aws:iam::123456789012:role/fake")
    monkeypatch.delenv("ANYAICAM_RECORDING_S3_BUCKET", raising=False)
    assert main._presigned_recording_url("recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mkv") is None


def test_customer_camera_recordings_skips_rows_with_no_signable_url(db_path, monkeypatch):
    monkeypatch.delenv("ANYAICAM_RECORDING_READ_ROLE_ARN", raising=False)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn)
        result = main._customer_camera_recordings("cam-1")
    assert result == []  # unconfigured read role -> every row skipped, never shown as a dead link


def test_customer_camera_recordings_returns_expected_shape_when_url_available(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, s3_key="recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mkv")
        with patch.object(main, "_presigned_recording_url", return_value="https://example.com/signed-url"):
            result = main._customer_camera_recordings("cam-1")
    assert result == [{
        "start": "2026-08-21T00:00:00",
        "end": "2026-08-21T00:05:00",
        "url": "https://example.com/signed-url",
        "name": "clip.mkv",
    }]


def test_customer_camera_recordings_excludes_expired_status(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, s3_key="recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/live.mkv", status="available")
        _seed_recording(conn, s3_key="recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/gone.mkv", status="expired")
        with patch.object(main, "_presigned_recording_url", return_value="https://example.com/signed-url"):
            result = main._customer_camera_recordings("cam-1")
    assert [item["name"] for item in result] == ["live.mkv"]


def test_customer_camera_recordings_never_crosses_camera_boundary(db_path):
    """A camera_id's recordings query must never surface another
    camera's rows, even within the same customer -- the caller
    (_customer_playback_cameras) is what enforces tenant/permission
    scoping, but this function's own WHERE camera_id=? must still be
    exact."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        _seed_recording(conn, camera_id="cam-1")
        _seed_recording(conn, camera_id="cam-2")
        with patch.object(main, "_presigned_recording_url", return_value="https://example.com/signed-url"):
            result = main._customer_camera_recordings("cam-1")
    assert len(result) == 1
    with override_target(sqlite_path=db_path):
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT camera_id FROM recordings").fetchall()
    assert {row[0] for row in rows} == {"cam-1", "cam-2"}  # both rows really exist in the table -- confirms the query, not the seed, did the filtering


def test_owner_sees_all_own_cameras_viewer_without_grant_sees_none(db_path):
    """Formalizes the exact gate this phase was approved against: the
    same SQL _customer_playback_cameras() already runs, re-verified
    here directly against a seeded DB rather than relied on from
    memory."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_base_tenant(conn)
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('cam-1','cust-1','site-1','appl-1','Camera 1','2026-01-01')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('cam-2','cust-1','site-1','appl-1','Camera 2','2026-01-01')")
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES('viewer-1','partner-1','viewer@example.com','Viewer','customer_viewer','x',1,'cust-1','2026-01-01')")
        conn.commit()

        owner_rows = conn.execute(
            "SELECT id, name FROM cameras WHERE customer_id=? ORDER BY camera_number, id", ("cust-1",)
        ).fetchall()
        viewer_rows = conn.execute(
            "SELECT c.id, c.name FROM cameras c "
            "JOIN customer_camera_permissions p ON p.camera_id=c.id AND p.user_id=? "
            "WHERE c.customer_id=? AND p.can_playback=1 ORDER BY c.camera_number, c.id",
            ("viewer-1", "cust-1"),
        ).fetchall()

    assert {row[0] for row in owner_rows} == {"cam-1", "cam-2"}
    assert viewer_rows == []
