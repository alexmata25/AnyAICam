"""Regression coverage for the restart_count schema defect found during
real-hardware Phase 2A claim validation: appliance_cloud.py's heartbeat()
unconditionally executes 'UPDATE appliances SET restart_count=...' the
moment it infers a restart from a dropping uptime_seconds value, but no
migration anywhere ever added that column -- confirmed live on Samsung as
a sqlite3.OperationalError: no such column: restart_count, crashing both
a genuine restart's heartbeat and every offline-queued heartbeat replayed
after connectivity was restored (each one looking like a restart relative
to whatever uptime was last stored). See db_migrations.py's own comment
on the fix.

Imports appliance_cloud (which imports partner_db, triggering its
import-time schema init) -- per this project's own documented constraint,
this file redirects to a throwaway sqlite file via override_target()
before that import happens, so nothing here ever touches the real
production database.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_cloud_restart_count.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed_appliance(db, appliance_id: str, cloud_id: str, credential: str):
    now = "2026-08-21T00:00:00"
    db.execute(
        "INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",
        ("partner-1", "Test Partner", "approved", "real", now),
    )
    db.execute(
        "INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
        ("cust-1", "partner-1", "Test Customer", "test@example.test", "active", "real", now),
    )
    db.execute(
        "INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
        ("site-1", "cust-1", "Test Site", now),
    )
    db.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,uptime_seconds) VALUES(?,?,?,?,?,?)",
        (appliance_id, "cust-1", "site-1", cloud_id, now, 0),
    )
    db.execute(
        "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
        ("cred-1", appliance_id, password_hash(credential), now),
    )


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_restart_count.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _seeded(db_path, appliance_id="appl-1", cloud_id="AIC-TEST0001", credential="test-credential"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, appliance_id, cloud_id, credential)


def _restart_count(db_path, appliance_id="appl-1"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT restart_count FROM appliances WHERE id=?", (appliance_id,)).fetchone()["restart_count"]


def _heartbeat(client, uptime_seconds):
    return client.post(
        "/api/appliance/heartbeat",
        headers=_auth_headers("appl-1", "test-credential"),
        json={"uptime_seconds": uptime_seconds, "cpu": 1, "memory": 1},
    )


# ------------------------------------------------------------- schema/migration


def test_restart_count_column_exists_after_initialize(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "test_migration.db")):
        from partner_db import connection as conn, initialize_database
        initialize_database()
        with conn() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(appliances)").fetchall()}
    assert "restart_count" in columns


def test_restart_count_migration_is_idempotent_across_repeated_initialize(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "test_migration_idempotent.db")):
        from partner_db import initialize_database
        for _ in range(3):
            initialize_database()  # must never raise "duplicate column: restart_count"


# ------------------------------------------------------------------------- heartbeat


def test_normal_heartbeat_succeeds_and_restart_count_stays_zero(client, db_path):
    _seeded(db_path)

    response = _heartbeat(client, uptime_seconds=100)

    assert response.status_code == 200
    assert response.json()["restarted"] is False
    assert _restart_count(db_path) == 0


def test_restart_detected_heartbeat_succeeds_and_increments_restart_count(client, db_path):
    """This is the exact crash reproduced live on Samsung: previously a
    500 (sqlite3.OperationalError: no such column: restart_count)."""
    _seeded(db_path)
    _heartbeat(client, uptime_seconds=500)  # establish a baseline uptime

    response = _heartbeat(client, uptime_seconds=10)  # dropped by more than the 30s tolerance -> a restart

    assert response.status_code == 200
    assert response.json()["restarted"] is True
    assert _restart_count(db_path) == 1


def test_restart_count_increments_correctly_across_multiple_restarts(client, db_path):
    _seeded(db_path)
    _heartbeat(client, uptime_seconds=500)
    _heartbeat(client, uptime_seconds=10)   # restart #1
    _heartbeat(client, uptime_seconds=200)
    response = _heartbeat(client, uptime_seconds=5)  # restart #2

    assert response.status_code == 200
    assert _restart_count(db_path) == 2


def test_queued_heartbeat_replay_drains_instead_of_repeatedly_returning_500(client, db_path):
    """Reproduces the exact production scenario: a live heartbeat with a
    fresh, high uptime lands first, then a stale offline-queued heartbeat
    (an older payload, queued while the appliance couldn't reach the
    portal, replayed once connectivity returns) arrives with a lower
    uptime -- indistinguishable from a real restart to heartbeat()'s own
    logic, and previously 500'd every single time it was retried rather
    than draining."""
    _seeded(db_path)
    live = _heartbeat(client, uptime_seconds=9000)
    assert live.status_code == 200

    replayed = _heartbeat(client, uptime_seconds=120)  # the stale queued payload's uptime, sent after the live one

    assert replayed.status_code == 200
    assert replayed.json()["restarted"] is True
    assert _restart_count(db_path) == 1

    # Once replayed successfully, the offline queue would mark this item
    # done and never resend it -- a further live heartbeat must keep
    # succeeding normally, proving nothing is left in a broken state.
    following = _heartbeat(client, uptime_seconds=9060)
    assert following.status_code == 200
    assert following.json()["restarted"] is False


# --------------------------------------------------- existing behavior unaffected


def test_unauthenticated_heartbeat_is_still_rejected(client, db_path):
    _seeded(db_path)

    response = client.post("/api/appliance/heartbeat", json={"uptime_seconds": 100})

    assert response.status_code == 401


def test_wrong_credential_heartbeat_is_still_rejected(client, db_path):
    _seeded(db_path)

    response = client.post(
        "/api/appliance/heartbeat",
        headers=_auth_headers("appl-1", "totally-wrong-credential"),
        json={"uptime_seconds": 100},
    )

    assert response.status_code == 403


def test_heartbeat_still_updates_state_and_last_check_in(client, db_path):
    _seeded(db_path)

    response = _heartbeat(client, uptime_seconds=100)
    assert response.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT state, online_status, last_check_in FROM appliances WHERE id=?", ("appl-1",)).fetchone()
    assert row["state"] == "online"
    assert row["online_status"] == "online"
    assert row["last_check_in"]
