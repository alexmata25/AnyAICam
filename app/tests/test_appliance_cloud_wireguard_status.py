"""Coverage for the wireguard_status/wireguard_last_handshake_at
heartbeat fields (docs/wireguard-remote-connectivity-plan.md Sec 14):
schema migration exists and is idempotent, an appliance that reports
the field gets it persisted with an enum-validated value, an appliance
that omits it (every real appliance today, since no agent yet sends
this field) leaves the column untouched via COALESCE rather than
clobbering a prior real value with NULL, and an invalid/unknown status
value is silently dropped rather than 500ing or persisting garbage --
the exact same pattern test_appliance_cloud_restart_count.py already
established for restart_count, applied here to the new fields.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_cloud_wireguard_status.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed_appliance(db, appliance_id: str, cloud_id: str, credential: str):
    now = "2026-08-21T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Test Customer", "test@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,uptime_seconds) VALUES(?,?,?,?,?,?)", (appliance_id, "cust-1", "site-1", cloud_id, now, 0))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-1", appliance_id, password_hash(credential), now))


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_wireguard_status.db"


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


def _heartbeat(client, **extra):
    payload = {"uptime_seconds": 100, "cpu": 1, "memory": 1, **extra}
    return client.post("/api/appliance/heartbeat", headers=_auth_headers("appl-1", "test-credential"), json=payload)


def _wireguard_columns(db_path, appliance_id="appl-1"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT wireguard_status, wireguard_last_handshake_at FROM appliances WHERE id=?", (appliance_id,)).fetchone()
    return row["wireguard_status"], row["wireguard_last_handshake_at"]


# ------------------------------------------------------------- schema/migration


def test_wireguard_columns_exist_after_initialize(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "test_migration.db")):
        from partner_db import connection as conn, initialize_database
        initialize_database()
        with conn() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(appliances)").fetchall()}
    assert "wireguard_status" in columns
    assert "wireguard_last_handshake_at" in columns


def test_wireguard_columns_migration_is_idempotent(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "test_migration_idempotent.db")):
        from partner_db import initialize_database
        for _ in range(3):
            initialize_database()  # must never raise "duplicate column"


# ------------------------------------------------------------------------- heartbeat


def test_wireguard_status_defaults_to_null_when_never_reported(client, db_path):
    _seeded(db_path)
    response = _heartbeat(client)
    assert response.status_code == 200
    assert _wireguard_columns(db_path) == (None, None)


def test_reported_wireguard_status_is_persisted(client, db_path):
    _seeded(db_path)
    response = _heartbeat(client, wireguard_status="enrolled")
    assert response.status_code == 200
    assert _wireguard_columns(db_path) == ("enrolled", None)


def test_reported_handshake_timestamp_is_persisted_alongside_status(client, db_path):
    _seeded(db_path)
    response = _heartbeat(client, wireguard_status="active", wireguard_last_handshake_at="2026-09-17T12:00:00")
    assert response.status_code == 200
    assert _wireguard_columns(db_path) == ("active", "2026-09-17T12:00:00")


def test_an_unknown_status_value_is_silently_dropped_not_persisted_or_500(client, db_path):
    _seeded(db_path)
    response = _heartbeat(client, wireguard_status="totally-made-up")
    assert response.status_code == 200
    assert _wireguard_columns(db_path) == (None, None)


def test_a_later_heartbeat_omitting_the_field_never_clobbers_a_prior_real_value(client, db_path):
    """COALESCE semantics: an appliance that once reported a real status
    and then sends a heartbeat without the field (e.g. an older agent
    build after a downgrade) must not have its last-known status erased."""
    _seeded(db_path)
    _heartbeat(client, wireguard_status="active", wireguard_last_handshake_at="2026-09-17T12:00:00")
    response = _heartbeat(client)  # no wireguard_status this time
    assert response.status_code == 200
    assert _wireguard_columns(db_path) == ("active", "2026-09-17T12:00:00")


def test_status_can_transition_forward_on_a_later_heartbeat(client, db_path):
    _seeded(db_path)
    _heartbeat(client, wireguard_status="enrolled")
    response = _heartbeat(client, wireguard_status="active", wireguard_last_handshake_at="2026-09-17T13:00:00")
    assert response.status_code == 200
    assert _wireguard_columns(db_path) == ("active", "2026-09-17T13:00:00")


# --------------------------------------------------- existing behavior unaffected


def test_heartbeat_without_any_wireguard_fields_behaves_exactly_as_before(client, db_path):
    _seeded(db_path)
    response = _heartbeat(client)
    assert response.status_code == 200
    assert response.json()["restarted"] is False
