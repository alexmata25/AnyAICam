"""Regression coverage for restoring the 20260824_camera_discovery
migration (blocker #1 of the release-prep audit): this branch's
db_migrations.py was missing it entirely, which was the confirmed root
cause of "no such table: camera_provisioning_requests"
(partner_workspace.py's provisioning-job queue) and all 12
test_camera_discovery_provisioning.py failures.

Three database states are exercised, matching the three real
situations this fix has to be safe against:
  1. A genuinely fresh database -- the migration must run and create
     camera_provisioning_requests.
  2. An "upgrade" database that already has this exact migration
     version recorded in schema_migrations (production's real state,
     confirmed via SSH) -- apply_migrations() must skip re-running it
     entirely, never re-touching the existing schema.
  3. The conflict this fix actually had to solve: a database that
     already has the cameras.device_key column (added by this same
     branch's own, separate, already-idempotent camera_columns block)
     but has NEVER had 20260824_camera_discovery recorded -- true for
     this branch's own already-running installations (Samsung,
     almost certainly). Restoring production's migration verbatim
     (including its own `ALTER TABLE cameras ADD COLUMN device_key`)
     crashes with "duplicate column name" here; this is the state
     that proved it, and now proves the fix.
"""
import sqlite3

import pytest

from database_backend import override_target
from db_migrations import apply_migrations
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_discovery_migration.db"


def _table_names(db_path):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _index_names(db_path):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def _applied_versions(db_path):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}


# =============================================================== 1. fresh database


def test_fresh_database_creates_camera_provisioning_requests(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
    tables = _table_names(db_path)
    assert "camera_provisioning_requests" in tables
    assert "20260824_camera_discovery" in _applied_versions(db_path)
    indexes = _index_names(db_path)
    assert "idx_camera_provisioning_appliance_status" in indexes
    # Both device_key indexes exist -- production's appliance-scoped
    # one (restored, see db_migrations.py's own comment on why it's
    # created in the unconditional block rather than this migration's
    # raw SQL) and this branch's pre-existing customer-scoped unique
    # one. The divergence itself is deliberately left unresolved.
    assert "idx_cameras_appliance_device_key" in indexes
    assert "idx_cameras_customer_device_key" in indexes


def test_fresh_database_camera_provisioning_requests_has_the_real_columns(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(camera_provisioning_requests)").fetchall()}
    assert columns == {
        "id", "customer_id", "appliance_id", "site_id", "device_key", "camera_name",
        "recording_mode", "analytics_json", "encrypted_credentials", "status",
        "camera_id", "message", "created_at", "updated_at",
    }


# =============================================================== 2. upgrade: migration already recorded (production's real state)


def test_migration_already_recorded_is_never_reapplied(db_path):
    """Simulates production: schema already has camera_provisioning_
    requests (created independently, standing in for production's own
    real history) and schema_migrations already lists this version --
    apply_migrations() must skip it outright, not attempt to recreate
    or alter anything."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        # Prove the guard actually does something: delete the table,
        # keep the version marked applied, and confirm it's NOT
        # recreated -- if the guard were broken this would come back.
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE camera_provisioning_requests")
            conn.commit()
        assert "camera_provisioning_requests" not in _table_names(db_path)
        apply_migrations()
    assert "camera_provisioning_requests" not in _table_names(db_path)  # still skipped -- version already recorded


# =============================================================== 3. the real conflict: device_key column exists, migration not recorded


def test_device_key_already_present_without_the_migration_recorded_does_not_crash(db_path):
    """The exact state that broke the naive restore (confirmed against
    this repo's own local dev database before the fix): device_key
    already added via the unconditional camera_columns block, but
    20260824_camera_discovery was never in schema_migrations because
    it didn't exist in this branch until now. Must not raise
    "duplicate column name" and must still create camera_provisioning_
    requests."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        with sqlite3.connect(db_path) as conn:
            # Roll back to the "before this fix" state: forget this
            # migration was ever applied, but leave device_key (and
            # everything apply_migrations()'s unconditional block
            # already added) alone -- exactly what an already-running
            # branch installation looks like.
            conn.execute("DELETE FROM schema_migrations WHERE version='20260824_camera_discovery'")
            conn.commit()
        assert "device_key" in {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(cameras)").fetchall()}
        apply_migrations()  # must not raise sqlite3.OperationalError: duplicate column name: device_key
    assert "20260824_camera_discovery" in _applied_versions(db_path)
    assert "camera_provisioning_requests" in _table_names(db_path)


def test_reapplying_migrations_on_an_already_fully_migrated_database_is_a_safe_no_op(db_path):
    """General idempotency: running apply_migrations() twice in a row
    on an already-fully-migrated database must never raise."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        apply_migrations()  # second call -- must not raise
    assert "camera_provisioning_requests" in _table_names(db_path)
