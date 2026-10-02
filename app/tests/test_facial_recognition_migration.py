"""AAC database migration -- Phase 1.

SQLite coverage runs unconditionally (this project's default backend).
PostgreSQL coverage mirrors test_customer_registration_postgresql.py's
own established pattern exactly: gated behind ANYAICAM_TEST_POSTGRES_URL,
skipped by default (no server available in this sandbox), but a real,
runnable test against an actual PostgreSQL database when that env var
is set. A third, always-on check statically confirms the migration's
raw SQL survives database_backend.py's own SQLite->PostgreSQL rewriting
without leftover SQLite-only syntax, so PostgreSQL compatibility is
exercised in this sandbox even without a live server.
"""

import os
import re
import unittest

import pytest

from database_backend import _postgres_sql, override_target
from db_migrations import MIGRATIONS

FACIAL_TABLES = (
    "facial_people",
    "facial_embeddings",
    "facial_watchlists",
    "facial_watchlist_members",
    "facial_events",
    "facial_rules",
    "facial_settings",
)


def _facial_migration_sql() -> str:
    for version, sql in MIGRATIONS:
        if version == "20260908_facial_recognition":
            return sql
    raise AssertionError("20260908_facial_recognition migration not found")


# --------------------------------------------------------------- SQLite (default backend)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_facial_migration.db"


def test_sqlite_migration_creates_every_facial_table(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection, initialize_database

        initialize_database()
        with connection() as db:
            tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table in FACIAL_TABLES:
        assert table in tables, f"{table} was not created by the migration"


def test_sqlite_migration_is_recorded_and_idempotent(db_path):
    with override_target(sqlite_path=str(db_path)):
        from db_migrations import apply_migrations
        from partner_db import connection, initialize_database

        initialize_database()
        with connection() as db:
            applied_once = {row["version"] for row in db.execute("SELECT version FROM schema_migrations").fetchall()}
        assert "20260908_facial_recognition" in applied_once
        # Re-running must never fail (IF NOT EXISTS everywhere) and must
        # never re-insert the schema_migrations row a second time.
        apply_migrations()
        with connection() as db:
            count = db.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version=?", ("20260908_facial_recognition",)
            ).fetchone()["n"]
        assert count == 1


def test_sqlite_facial_embeddings_cascades_on_person_delete(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection, initialize_database

        initialize_database()
        with connection() as db:
            now = "2026-09-08T00:00:00"
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('c1','p1','C','c@example.test','active','real',?)", (now,))
            db.execute("INSERT INTO facial_people(id,customer_id,display_name,status,created_at,updated_at) VALUES('person-1','c1','Alice','active',?,?)", (now, now))
            db.execute(
                "INSERT INTO facial_embeddings(id,person_id,customer_id,engine,engine_version,embedding_json,created_at) VALUES('emb-1','person-1','c1','haar_intensity','1','[1.0]',?)",
                (now,),
            )
            db.execute("DELETE FROM facial_people WHERE id='person-1'")
            remaining = db.execute("SELECT COUNT(*) AS n FROM facial_embeddings").fetchone()["n"]
    assert remaining == 0


def test_sqlite_facial_watchlist_members_cascades_on_watchlist_delete(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection, initialize_database

        initialize_database()
        with connection() as db:
            now = "2026-09-08T00:00:00"
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('c1','p1','C','c@example.test','active','real',?)", (now,))
            db.execute("INSERT INTO facial_people(id,customer_id,display_name,status,created_at,updated_at) VALUES('person-1','c1','Alice','active',?,?)", (now, now))
            db.execute("INSERT INTO facial_watchlists(id,customer_id,name,classification,created_at,updated_at) VALUES('wl-1','c1','Banned','alert',?,?)", (now, now))
            db.execute("INSERT INTO facial_watchlist_members(watchlist_id,person_id,added_at) VALUES('wl-1','person-1',?)", (now,))
            db.execute("DELETE FROM facial_watchlists WHERE id='wl-1'")
            remaining = db.execute("SELECT COUNT(*) AS n FROM facial_watchlist_members").fetchone()["n"]
    assert remaining == 0


def test_sqlite_watchlist_name_is_unique_per_customer(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection, initialize_database
        import sqlite3

        initialize_database()
        with connection() as db:
            now = "2026-09-08T00:00:00"
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('c1','p1','C','c@example.test','active','real',?)", (now,))
            db.execute("INSERT INTO facial_watchlists(id,customer_id,name,classification,created_at,updated_at) VALUES('wl-1','c1','Banned','alert',?,?)", (now, now))
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO facial_watchlists(id,customer_id,name,classification,created_at,updated_at) VALUES('wl-2','c1','Banned','alert',?,?)", (now, now))


# --------------------------------------------------------------- PostgreSQL compatibility


def test_migration_sql_survives_postgres_rewriting_without_sqlite_only_syntax():
    """Always-on, server-free check: every statement in the facial
    migration, after database_backend._postgres_sql()'s own SQLite->
    PostgreSQL rewriting, is free of SQLite-only constructs (no bare
    `?` placeholders, no AUTOINCREMENT, no BLOB) -- the same guarantee
    every other migration in this file already relies on, since this
    migration reuses only plain, portable SQL types (TEXT/INTEGER/REAL)
    and never needed BLOB/AUTOINCREMENT/INSERT OR IGNORE."""
    sql = _facial_migration_sql()
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    assert statements, "expected at least one CREATE statement"
    for statement in statements:
        converted = _postgres_sql(statement)
        assert "?" not in converted
        assert "AUTOINCREMENT" not in converted.upper()
        assert re.search(r"\bBLOB\b", converted, re.I) is None


TEST_POSTGRES_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "")
if TEST_POSTGRES_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"] = "postgresql"
    os.environ["ANYAICAM_DATABASE_URL"] = TEST_POSTGRES_URL


@unittest.skipUnless(TEST_POSTGRES_URL, "set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class PostgreSQLFacialMigrationTests(unittest.TestCase):
    """Runs for real against PostgreSQL when a disposable test database
    is configured. Skipped (not failed) in every environment without
    one, including this Phase 1 sandbox -- see the Phase 1 report for
    why PostgreSQL itself could not be exercised live in this session."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row

        from db_migrations import apply_migrations

        apply_migrations()
        cls._connect = lambda self: psycopg.connect(TEST_POSTGRES_URL, row_factory=dict_row)

    def test_every_facial_table_exists(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
            ).fetchall()
        tables = {row["table_name"] for row in rows}
        for table in FACIAL_TABLES:
            self.assertIn(table, tables)

    def test_facial_embeddings_cascades_on_person_delete(self):
        now = "2026-09-08T00:00:00"
        with self._connect() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('pg-p1','P','approved','real',%s) ON CONFLICT DO NOTHING", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('pg-c1','pg-p1','C','pgc@example.test','active','real',%s) ON CONFLICT DO NOTHING", (now,))
            db.execute("INSERT INTO facial_people(id,customer_id,display_name,status,created_at,updated_at) VALUES('pg-person-1','pg-c1','Alice','active',%s,%s)", (now, now))
            db.execute(
                "INSERT INTO facial_embeddings(id,person_id,customer_id,engine,engine_version,embedding_json,created_at) VALUES('pg-emb-1','pg-person-1','pg-c1','haar_intensity','1','[1.0]',%s)",
                (now,),
            )
            db.execute("DELETE FROM facial_people WHERE id='pg-person-1'")
            remaining = db.execute("SELECT COUNT(*) AS n FROM facial_embeddings WHERE person_id='pg-person-1'").fetchone()["n"]
        self.assertEqual(remaining, 0)
