"""Regression coverage for a real, confirmed-live production incident:
appliance_cloud.py's heartbeat() has referenced appliances.restart_count
since restart-detection was added, but no migration in db_migrations.py
ever created the column. Every heartbeat that detected a genuine restart
(uptime_seconds dropping) crashed with sqlite3.OperationalError: no such
column: restart_count -- a 500 for every appliance, on any database that
had never had this column added by hand.

This is exactly the kind of gap a fresh database silently reproduces:
this suite builds a completely new database via partner_db.
initialize_database() (the same call every real deployment -- staging,
Samsung, a brand-new customer environment -- makes on first boot) and
proves both that the column exists afterward AND that a real restart-
detecting heartbeat succeeds end to end against it, rather than only
checking the column's presence in isolation.

Same minimal-app isolation pattern as test_appliance_identity_
integration.py: override_target() before appliance_cloud's import-time
schema init, a standalone FastAPI() app with only appliance_cloud's
routes, real TestClient requests, real appliance-credential
authentication.
"""
import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_restart_count_migration.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed_appliance(db, appliance_id: str, cloud_id: str, credential: str, partner_id="partner-1", customer_id="cust-1", site_id="site-1"):
    now = "2026-08-27T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, f"Partner {partner_id}", "approved", "real", now))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, partner_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (secrets.token_hex(4), appliance_id, password_hash(credential), now))


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())), "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer {credential}"}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_restart_count_migration.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _db(db_path):
    return override_target(sqlite_path=str(db_path))


def test_fresh_database_has_restart_count_column(db_path):
    # The literal regression: a database built from db_migrations.py
    # alone (nothing hand-patched) must already have this column --
    # never something a deployment discovers is missing only once an
    # appliance actually restarts in production.
    with _db(db_path):
        from partner_db import initialize_database, connection
        initialize_database()
        with connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(appliances)").fetchall()}
    assert "restart_count" in columns


def test_heartbeat_detecting_a_real_restart_succeeds_on_a_fresh_database(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")

    # First heartbeat establishes a high uptime -- the appliance has been
    # running a while.
    first = client.post(
        "/api/appliance/heartbeat",
        json={"uptime_seconds": 10000, "cpu": 10, "memory": 20, "camera_count": 5},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    assert first.status_code == 200, first.text

    # Second heartbeat reports a much lower uptime -- a genuine restart,
    # not measurement jitter (the 30s tolerance). Before this fix, the
    # UPDATE ... SET restart_count=... branch this triggers raised
    # sqlite3.OperationalError and the whole request 500'd.
    second = client.post(
        "/api/appliance/heartbeat",
        json={"uptime_seconds": 5, "cpu": 10, "memory": 20, "camera_count": 5},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    assert second.status_code == 200, second.text
    assert second.json()["restarted"] is True

    with _db(db_path):
        with connection() as db:
            row = db.execute("SELECT restart_count FROM appliances WHERE id=?", ("appl-1",)).fetchone()
    assert row["restart_count"] == 1


def test_heartbeat_without_a_restart_does_not_increment_restart_count(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")

    client.post(
        "/api/appliance/heartbeat",
        json={"uptime_seconds": 1000, "cpu": 10, "memory": 20, "camera_count": 5},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    response = client.post(
        "/api/appliance/heartbeat",
        json={"uptime_seconds": 1010, "cpu": 10, "memory": 20, "camera_count": 5},
        headers=_auth_headers("appl-1", "cred-1"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["restarted"] is False

    with _db(db_path):
        with connection() as db:
            row = db.execute("SELECT restart_count FROM appliances WHERE id=?", ("appl-1",)).fetchone()
    assert (row["restart_count"] or 0) == 0
