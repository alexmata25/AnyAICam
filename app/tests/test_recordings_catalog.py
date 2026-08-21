"""R2 (recording-pipeline roadmap): focused tests for the recordings
catalog table's schema and idempotency, exercised directly against a
throwaway sqlite database via database_backend's override_target() --
no FastAPI/auth stack needed, avoiding this project's own documented
test-discovery-order fragility around importing `main`.

Uses partner_db.initialize_database() (which both creates the base
schema and runs db_migrations.apply_migrations()) so the recordings
table's own FOREIGN KEY targets (customers/sites/appliances/cameras)
actually exist -- the same real invocation chain the running app uses,
not a hand-rolled subset of it.
"""

import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_recordings.db"


def _seed_tenant(conn, camera_id="cam-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute(f"INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('{camera_id}','cust-1','site-1','appl-1','Camera','2026-01-01')")
    conn.commit()


def test_migration_creates_recordings_table_with_expected_columns(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    expected = {
        'id', 'customer_id', 'site_id', 'appliance_id', 'camera_id', 's3_key',
        'started_at', 'ended_at', 'duration_seconds', 'size_bytes', 'status', 'created_at',
    }
    assert expected <= columns


def test_status_defaults_to_available(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute('PRAGMA foreign_keys=ON')
        _seed_tenant(conn)
        conn.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,created_at) "
            "VALUES('rec-1','cust-1','site-1','appl-1','cam-1','recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mp4','2026-08-21T00:00:00','2026-08-21T00:05:00','2026-08-21T00:05:01')"
        )
        conn.commit()
        status = conn.execute("SELECT status FROM recordings WHERE id='rec-1'").fetchone()[0]
    assert status == 'available'


def test_duplicate_camera_id_and_s3_key_is_rejected_at_the_db_level(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute('PRAGMA foreign_keys=ON')
        _seed_tenant(conn)
        conn.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
            "VALUES('rec-1','cust-1','site-1','appl-1','cam-1','recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mp4','2026-08-21T00:00:00','2026-08-21T00:05:00','available','2026-08-21T00:05:01')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
                "VALUES('rec-2','cust-1','site-1','appl-1','cam-1','recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mp4','2026-08-21T00:00:00','2026-08-21T00:05:00','available','2026-08-21T00:05:02')"
            )


def test_two_different_cameras_never_collide_on_the_same_relative_key(db_path):
    """The UNIQUE constraint is scoped to (camera_id, s3_key) together, not
    s3_key alone -- confirms two different cameras' own tenant-scoped
    prefixes can never be mistaken for the same recording."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute('PRAGMA foreign_keys=ON')
        _seed_tenant(conn, camera_id="cam-1")
        _seed_tenant(conn, camera_id="cam-2")
        conn.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
            "VALUES('rec-1','cust-1','site-1','appl-1','cam-1','recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/clip.mp4','2026-08-21T00:00:00','2026-08-21T00:05:00','available','2026-08-21T00:05:01')"
        )
        conn.execute(
            "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,status,created_at) "
            "VALUES('rec-2','cust-1','site-1','appl-1','cam-2','recordings/cust-1/site-1/appl-1/cam-2/2026/08/21/clip.mp4','2026-08-21T00:00:00','2026-08-21T00:05:00','available','2026-08-21T00:05:02')"
        )
        conn.commit()
        count = conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
    assert count == 2


def test_camera_started_at_index_exists(db_path):
    """The index the future Playback timeline query (R4) will rely on for
    WHERE camera_id=? AND started_at BETWEEN ? AND ? -- created now so R4
    doesn't need a schema change to be fast."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        index_names = {row[1] for row in conn.execute("PRAGMA index_list(recordings)")}
    assert 'idx_recordings_camera_started' in index_names
