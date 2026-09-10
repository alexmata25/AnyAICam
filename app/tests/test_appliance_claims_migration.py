"""Bare migration-applies-cleanly check for the appliance_claims table
(Phase 1 of the non-interactive/self-service claim flow -- see
docs/non-interactive-activation-phase1-plan.md). No routes exist yet;
this only proves the schema itself is sound before any code builds on
it. See app/tests/test_appliance_claims.py for the full route-level
test suite added in a later commit.
"""
from database_backend import override_target


def test_appliance_claims_table_and_indexes_are_created(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "test_migration.db")):
        from partner_db import connection, initialize_database
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(appliance_claims)").fetchall()}
            indexes = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='appliance_claims'").fetchall()}

    assert columns == {
        "id", "device_id", "claim_session_id", "claim_code_hash", "claim_proof_hash",
        "claim_proof_plaintext", "status", "customer_id", "site_id", "claimed_by",
        "appliance_id", "proof_expires_at", "expires_at", "claimed_at", "completed_at",
        "revoked_at", "created_at",
    }
    assert "idx_appliance_claims_device_id" in indexes
    assert "idx_appliance_claims_status" in indexes


def test_migration_is_recorded_and_reapplying_is_a_no_op(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "test_migration_idempotent.db")):
        from partner_db import connection, initialize_database
        initialize_database()
        initialize_database()  # must not raise on a second call
        with connection() as db:
            applied = db.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE version='20260910_appliance_claims'").fetchone()["n"]

    assert applied == 1
