"""Disposable PostgreSQL integration test for the AWS Cost Dashboard migration.

This test requires two empty PostgreSQL databases and pg_dump/pg_restore.
It is run only by the branch-scoped GitHub Actions PostgreSQL job; it never
connects to a production or shared staging database.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

import pytest

import db_migrations
from database_backend import connect


SOURCE_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "").strip()
RESTORE_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_RESTORE_URL", "").strip()


def _require_isolated_urls() -> None:
    assert SOURCE_URL and RESTORE_URL, (
        "Set both test PostgreSQL URLs to dedicated disposable databases."
    )
    source, restore = urlsplit(SOURCE_URL), urlsplit(RESTORE_URL)
    assert source.scheme in {"postgres", "postgresql"}
    assert restore.scheme in {"postgres", "postgresql"}
    assert source.hostname in {"localhost", "127.0.0.1", "postgres"}
    assert restore.hostname in {"localhost", "127.0.0.1", "postgres"}
    assert source.path.strip("/") != restore.path.strip("/"), (
        "Upgrade and restore URLs must name different disposable databases."
    )


def _assert_command_ok(argv: list[str]) -> None:
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    assert result.returncode == 0, (
        f"{argv[0]} exited {result.returncode}; stderr: {result.stderr[-4000:]}"
    )


def _table_exists(db, table_name: str) -> bool:
    return db.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=current_schema() AND table_name=?) AS present",
        (table_name,),
    ).fetchone()["present"]


def test_postgresql_additive_migration_repeat_and_backup_restore(monkeypatch):
    _require_isolated_urls()
    assert shutil.which("pg_dump"), "pg_dump must be installed in the isolated CI runner"
    assert shutil.which("pg_restore"), "pg_restore must be installed in the isolated CI runner"

    monkeypatch.setenv("ANYAICAM_DATABASE_BACKEND", "postgresql")
    monkeypatch.setenv("ANYAICAM_DATABASE_URL", SOURCE_URL)

    full_migrations = list(db_migrations.MIGRATIONS)
    migration_id = "20261015_aws_cost_dashboard_snapshots"
    dashboard_migrations = [entry for entry in full_migrations if entry[0] == migration_id]
    assert len(dashboard_migrations) == 1, "Expected exactly one registered dashboard migration"
    monkeypatch.setattr(
        db_migrations,
        "MIGRATIONS",
        [entry for entry in full_migrations if entry[0] != migration_id],
    )

    # Initialize the historical schema while deliberately withholding only
    # the new dashboard migration, then seed recognizable synthetic rows.
    # Import after patching: partner_db performs initialization at import
    # time, so importing it above would apply the full migration list first.
    from partner_db import initialize_database

    initialize_database()
    with connect() as db:
        db.execute(
            "INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",
            ("ci-partner", "CI Partner", "approved", "test", "2026-10-15T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
            ("ci-customer", "ci-partner", "Synthetic Customer", "ci@example.invalid", "active", "test", "2026-10-15T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
            ("ci-site", "ci-customer", "Synthetic Site", "2026-10-15T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,created_at) VALUES(?,?,?,?,?,?)",
            ("ci-appliance", "ci-customer", "ci-site", "AIC-CI-ONLY", "linux", "2026-10-15T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("ci-camera", "ci-customer", "ci-site", "ci-appliance", "Synthetic Camera", "1080p", "active", "2026-10-15T00:00:00Z"),
        )
        assert not _table_exists(db, "aws_cost_monthly_snapshots")
        assert db.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version=?",
            ("20261015_aws_cost_dashboard_snapshots",),
        ).fetchone()["n"] == 0

    # Take a real PostgreSQL custom-format backup while the DB is still in
    # the pre-dashboard state. The restore target is a separate empty DB.
    with tempfile.TemporaryDirectory(prefix="vms-cost-pg-backup-") as temp_dir:
        backup_path = os.path.join(temp_dir, "pre-dashboard.dump")
        _assert_command_ok([
            "pg_dump", "--format=custom", "--no-owner", "--no-acl",
            "--file", backup_path, "--dbname", SOURCE_URL,
        ])

        monkeypatch.setattr(db_migrations, "MIGRATIONS", full_migrations)
        db_migrations.apply_migrations()
        db_migrations.apply_migrations()

        with connect() as db:
            version = db.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version=?",
                (migration_id,),
            ).fetchone()["n"]
            assert version == 1
            for table in (
                "aws_cost_monthly_snapshots",
                "aws_camera_usage_snapshots",
                "aws_lifecycle_rule_snapshots",
            ):
                assert _table_exists(db, table), f"Missing additive migration table: {table}"
            assert db.execute("SELECT name FROM customers WHERE id=?", ("ci-customer",)).fetchone()["name"] == "Synthetic Customer"
            assert db.execute("SELECT name FROM cameras WHERE id=?", ("ci-camera",)).fetchone()["name"] == "Synthetic Camera"

        _assert_command_ok([
            "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl",
            "--dbname", RESTORE_URL, backup_path,
        ])

        restore_host = urlsplit(RESTORE_URL).hostname
        assert restore_host in {"localhost", "127.0.0.1", "postgres"}
        monkeypatch.setenv("ANYAICAM_DATABASE_URL", RESTORE_URL)
        with connect() as restored:
            assert restored.execute(
                "SELECT name FROM customers WHERE id=?", ("ci-customer",)
            ).fetchone()["name"] == "Synthetic Customer"
            assert restored.execute(
                "SELECT name FROM cameras WHERE id=?", ("ci-camera",)
            ).fetchone()["name"] == "Synthetic Camera"
            assert not _table_exists(restored, "aws_cost_monthly_snapshots")
            assert restored.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version=?",
                (migration_id,),
            ).fetchone()["n"] == 0
