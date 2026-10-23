"""Migrations are atomic and safe to rerun (2026-10-23, EC2 step-05 failure).

The 2026-10-22 cutover start of e69106cd was killed inside apply_migrations(). Python's sqlite3 only opens a transaction
before INSERT/UPDATE/DELETE, so the first new migration's ALTER TABLE had already committed on its own while its
schema_migrations row was rolled back: the live database kept pending_analytics_links.licensed_quantity with no marker,
and every later start failed with "duplicate column name". These tests pin the fix: one migration commits its statements
and its marker together, and an ADD COLUMN whose column already exists is skipped.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from database_backend import override_target

APP = Path(__file__).resolve().parents[1]
FIRST_NEW = "20261012_pending_analytics_license_quantity"


def _init(path):
    with override_target(sqlite_path=str(path)):
        from partner_db import initialize_database
        initialize_database()


def _markers(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
    finally:
        con.close()


def _columns(path, table):
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    finally:
        con.close()


def _apply(path):
    with override_target(sqlite_path=str(path)):
        import db_migrations
        db_migrations.apply_migrations()


def test_clean_database_applies_every_migration_and_a_rerun_changes_nothing(tmp_path):
    import db_migrations
    db = tmp_path / "clean.db"
    _init(db)
    first = _markers(db)
    assert {v for v, _ in db_migrations.MIGRATIONS} <= first
    _apply(db)
    _apply(db)
    assert _markers(db) == first


def test_the_live_partial_state_column_without_marker_now_migrates(tmp_path):
    """Exactly the live EC2 state found by 05d-r6 / 05f: licensed_quantity exists, its marker does not."""
    db = tmp_path / "partial.db"
    _init(db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM schema_migrations WHERE version=?", (FIRST_NEW,))
    con.commit(); con.close()
    assert "licensed_quantity" in _columns(db, "pending_analytics_links") and FIRST_NEW not in _markers(db)
    _apply(db)                                   # before the fix: sqlite3.OperationalError duplicate column name
    assert FIRST_NEW in _markers(db)
    assert _columns(db, "pending_analytics_links").count("licensed_quantity") == 1


def test_every_new_marker_missing_with_their_schema_present_reruns_cleanly(tmp_path):
    """Worst case: all versioned migrations' objects exist but none of their markers (all ADD COLUMNs are re-attempted)."""
    import db_migrations
    db = tmp_path / "allmissing.db"
    _init(db)
    versions = [v for v, _ in db_migrations.MIGRATIONS]
    con = sqlite3.connect(db)
    con.executemany("DELETE FROM schema_migrations WHERE version=?", [(v,) for v in versions])
    con.commit(); con.close()
    _apply(db)
    assert set(versions) <= _markers(db)


def test_a_failing_migration_rolls_back_its_ddl_and_its_marker(tmp_path, monkeypatch):
    import db_migrations
    db = tmp_path / "failing.db"
    _init(db)
    bad = ("29991231_atomicity_probe", "CREATE TABLE IF NOT EXISTS atomic_probe(a TEXT);"
                                       "ALTER TABLE atomic_probe ADD COLUMN b TEXT;"
                                       "SELECT * FROM no_such_table_atomicity")
    monkeypatch.setattr(db_migrations, "MIGRATIONS", db_migrations.MIGRATIONS + [bad])
    with pytest.raises(sqlite3.OperationalError):
        _apply(db)
    assert "29991231_atomicity_probe" not in _markers(db)
    assert _columns(db, "atomic_probe") == []    # the CREATE TABLE and ALTER TABLE were rolled back with it
    monkeypatch.setattr(db_migrations, "MIGRATIONS", db_migrations.MIGRATIONS[:-1] + [(bad[0], bad[1].rsplit(";", 1)[0])])
    _apply(db)
    assert "29991231_atomicity_probe" in _markers(db) and _columns(db, "atomic_probe") == ["a", "b"]


KILLED_START = r'''
import os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
real_connect = sqlite3.connect
def connect(*a, **k):
    con = real_connect(*a, **k)
    con.create_function("die", 0, lambda: os._exit(9))      # like SIGKILL: no rollback, no commit
    return con
sqlite3.connect = connect
import db_migrations
from database_backend import override_target
db_migrations.MIGRATIONS = db_migrations.MIGRATIONS + [("29991231_kill_probe",
    "CREATE TABLE IF NOT EXISTS kill_probe(a TEXT);ALTER TABLE kill_probe ADD COLUMN b TEXT;SELECT die()")]
with override_target(sqlite_path=sys.argv[2]):
    db_migrations.apply_migrations()
'''


def _killed_start(db):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-c", KILLED_START, str(APP), str(db)], capture_output=True, text=True, timeout=120, env=env)
    assert p.returncode == 9, p.stderr[-2000:]


def test_a_start_killed_inside_a_migration_leaves_nothing_of_it(tmp_path):
    db = tmp_path / "killed.db"
    _init(db)
    _killed_start(db)
    sqlite3.connect(db).execute("SELECT 1").fetchone()       # the next opener rolls back the hot journal
    assert "29991231_kill_probe" not in _markers(db)
    assert _columns(db, "kill_probe") == []                  # before the fix the CREATE TABLE had committed on its own


def test_the_pre_fix_behaviour_is_what_the_live_database_shows(tmp_path):
    """Control: with the transaction removed (the pre-fix loop) the same killed start DOES leave the table behind --
    so the killed-start test above really exercises the fix."""
    db = tmp_path / "control.db"
    _init(db)
    pre_fix = KILLED_START.replace("import db_migrations\n", "import db_migrations\n"
        "def apply_pre_fix():\n"
        "    from database_backend import connect\n"
        "    with connect() as con:\n"
        "        for version, script in db_migrations.MIGRATIONS:\n"
        "            if con.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone(): continue\n"
        "            for st in [x.strip() for x in script.split(';') if x.strip()]: con.execute(st)\n"
        "            con.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)', (version, 'x'))\n"
        "db_migrations.apply_migrations = apply_pre_fix\n", 1)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-c", pre_fix, str(APP), str(db)], capture_output=True, text=True, timeout=120, env=env)
    assert p.returncode == 9, p.stderr[-2000:]
    sqlite3.connect(db).execute("SELECT 1").fetchone()
    assert "29991231_kill_probe" not in _markers(db)
    assert _columns(db, "kill_probe") == ["a", "b"]          # partial: schema committed, marker lost


RACE_START = r'''
import os, sqlite3, sys, time
sys.path.insert(0, sys.argv[1])
flag, role = sys.argv[3], sys.argv[4]
real_connect = sqlite3.connect
def slow():
    if role == "A":
        open(flag, "w").close()      # A is inside the migration, holding the write lock
        time.sleep(2.0)
    return 1
def connect(*a, **k):
    con = real_connect(*a, **k)
    con.create_function("slow", 0, slow)
    return con
sqlite3.connect = connect
import db_migrations
from database_backend import override_target
if role == "B":
    deadline = time.time() + 30
    while not os.path.exists(flag) and time.time() < deadline:
        time.sleep(0.02)
db_migrations.MIGRATIONS = db_migrations.MIGRATIONS + [("29991231_race_probe",
    "CREATE TABLE IF NOT EXISTS race_probe(a TEXT);SELECT slow()")]
with override_target(sqlite_path=sys.argv[2]):
    db_migrations.apply_migrations()
print("OK", role)
'''


def test_two_processes_starting_together_apply_a_pending_migration_once(tmp_path):
    """Codex MIG-127-01: B reads schema_migrations while A is inside the same pending migration, waits for A's write
    lock, and must then see A's marker (re-check under the lock) instead of re-running it and failing on the marker's
    primary key."""
    db = tmp_path / "race.db"
    _init(db)
    flag = tmp_path / "a-inside"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    a = subprocess.Popen([sys.executable, "-c", RACE_START, str(APP), str(db), str(flag), "A"], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    b = subprocess.Popen([sys.executable, "-c", RACE_START, str(APP), str(db), str(flag), "B"], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    out_a, err_a = a.communicate(timeout=120)
    out_b, err_b = b.communicate(timeout=120)
    assert a.returncode == 0 and "OK A" in out_a, err_a[-2000:]
    assert b.returncode == 0 and "OK B" in out_b, err_b[-2000:]
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='29991231_race_probe'").fetchone()[0] == 1
    con.close()


def test_an_existing_column_with_a_different_definition_is_a_schema_mismatch(tmp_path, monkeypatch):
    """Codex MIG-127-02: a same-name column is only accepted if it is the column the migration would have created."""
    import db_migrations
    db = tmp_path / "mismatch.db"
    _init(db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mismatch_probe(a TEXT, c TEXT)")
    con.commit(); con.close()
    monkeypatch.setattr(db_migrations, "MIGRATIONS", db_migrations.MIGRATIONS + [
        ("29991231_mismatch_probe", "ALTER TABLE mismatch_probe ADD COLUMN c INTEGER NOT NULL DEFAULT 1")])
    with pytest.raises(RuntimeError, match="Schema mismatch in migration 29991231_mismatch_probe"):
        _apply(db)
    assert "29991231_mismatch_probe" not in _markers(db)
