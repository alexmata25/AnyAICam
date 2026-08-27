"""HTTP-level regression coverage: POST /api/appliance/activate now
also durably persists this appliance's own activation identity (gap #1)
as its last step, and own_appliance_identity() (main.py) reads it back
with no environment configuration required -- proving the full
activate -> persist -> "restart" -> identity-still-available chain end
to end, plus the conflict/idempotency/recovery rules around it.
"""
import secrets
import time
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_activation_endpoint.db"):
    import appliance_activation
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed_appliance_with_token(db, appliance_id: str, cloud_id: str, token: str, partner_id="partner-1", customer_id="cust-1", site_id="site-1", expires_in_hours=24):
    now = "2026-08-27T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Partner", "approved", "real", now))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, "Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, now))
    db.execute(
        "INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
        (secrets.token_hex(4), appliance_id, password_hash(token), (datetime.now() + timedelta(hours=expires_in_hours)).isoformat(), now),
    )


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_activation.db"


@pytest.fixture()
def identity_file(tmp_path, monkeypatch):
    path = tmp_path / "appliance_identity.json"
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", path)
    # activation_limiter is a shared module-level singleton, never reset
    # by database/identity-file isolation -- see test_appliance_
    # activation_ux.py's matching fixture comment for the full reason
    # this file resets it too (this file is itself a heavy consumer of
    # POST /api/appliance/activate and runs before that one alphabetically).
    appliance_cloud.activation_limiter.events.clear()
    return path


@pytest.fixture()
def client(db_path, identity_file):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _db(db_path):
    return override_target(sqlite_path=str(db_path))


# =============================================================== activate -> persist -> "restart" -> still available


def test_successful_activation_persists_the_identity_file(client, db_path, identity_file):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "activation-token-1")

    response = client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "activation-token-1"})
    assert response.status_code == 200
    credential = response.json()["credential"]

    persisted = appliance_activation.load_persisted_identity()
    assert persisted["cloud_id"] == "AIC-1"
    assert persisted["credential"] == credential
    assert persisted["customer_id"] == "cust-1"
    assert persisted["site_id"] == "site-1"


def test_identity_is_readable_after_a_simulated_restart(client, db_path, identity_file, monkeypatch):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "activation-token-1")
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "activation-token-1"})

    # Simulate a restart: no env vars set, a completely fresh call into
    # own_appliance_identity()'s equivalent (load_persisted_identity())
    # against the same on-disk file -- nothing in memory carries over.
    monkeypatch.delenv("ANYAICAM_APPLIANCE_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CLOUD_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CREDENTIAL", raising=False)
    reloaded = appliance_activation.load_persisted_identity()
    assert reloaded is not None
    assert reloaded["cloud_id"] == "AIC-1"


def test_main_own_appliance_identity_reads_the_persisted_file(client, db_path, identity_file, monkeypatch):
    import main
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "activation-token-1")
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "activation-token-1"})

    monkeypatch.delenv("ANYAICAM_APPLIANCE_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CLOUD_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CREDENTIAL", raising=False)
    monkeypatch.setattr("appliance_activation.ACTIVATION_IDENTITY_FILE", identity_file)

    identity = main.own_appliance_identity()
    assert identity is not None
    assert identity["cloud_id"] == "AIC-1"
    assert identity["appliance_id"] == "appl-1"


# =============================================================== idempotent re-activation, same appliance


def test_reactivating_with_a_fresh_token_for_the_same_cloud_id_succeeds_and_refreshes(client, db_path, identity_file):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "token-one")
    first = client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "token-one"})
    assert first.status_code == 200

    with _db(db_path):
        with connection() as db:
            db.execute(
                "INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                ("tok-2", "appl-1", password_hash("token-two"), (datetime.now() + timedelta(hours=1)).isoformat(), "2026-08-27T00:00:00"),
            )
    second = client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "token-two"})
    assert second.status_code == 200
    assert second.json()["credential"] != first.json()["credential"]

    persisted = appliance_activation.load_persisted_identity()
    assert persisted["credential"] == second.json()["credential"]
    assert persisted["activation_version"] == 2


# =============================================================== a different Cloud ID must not silently overwrite


def test_activating_a_different_cloud_id_is_rejected_and_nothing_is_consumed(client, db_path, identity_file):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "token-one")
            _seed_appliance_with_token(db, "appl-2", "AIC-2", "token-two")
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "token-one"})

    response = client.post("/api/appliance/activate", json={"cloud_id": "AIC-2", "activation_token": "token-two"})
    assert response.status_code == 409

    # The rejected attempt must never have consumed the second
    # appliance's token or minted it a credential.
    with _db(db_path):
        with connection() as db:
            token_row = db.execute("SELECT used_at FROM appliance_activation_tokens WHERE appliance_id='appl-2'").fetchone()
            credential_count = db.execute("SELECT COUNT(*) AS n FROM appliance_credentials WHERE appliance_id='appl-2'").fetchone()["n"]
    assert token_row["used_at"] is None
    assert credential_count == 0
    # And the original identity is completely untouched.
    persisted = appliance_activation.load_persisted_identity()
    assert persisted["cloud_id"] == "AIC-1"
    assert persisted["activation_version"] == 1


# =============================================================== missing credential (never activated)


def test_own_appliance_identity_is_none_before_any_activation(identity_file, monkeypatch):
    import main
    monkeypatch.delenv("ANYAICAM_APPLIANCE_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CLOUD_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CREDENTIAL", raising=False)
    assert main.own_appliance_identity() is None


# =============================================================== corrupt local state -> fail closed, not a crash


def test_activate_endpoint_still_works_if_the_local_identity_file_is_corrupt(client, db_path, identity_file):
    identity_file.parent.mkdir(parents=True, exist_ok=True)
    identity_file.write_text("{not valid json at all", encoding="utf-8")
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "token-one")

    response = client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "token-one"})
    assert response.status_code == 200  # corrupt file reads as "not activated", not a hard failure
    persisted = appliance_activation.load_persisted_identity()
    assert persisted["cloud_id"] == "AIC-1"


# =============================================================== explicit reset -> successful recovery


def test_reset_endpoint_clears_identity_and_a_different_cloud_id_can_then_activate(client, db_path, identity_file, monkeypatch):
    import main
    monkeypatch.setattr(main, "USERS_FILE", db_path.parent / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", db_path.parent / "sessions.json")
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "token-one")
            _seed_appliance_with_token(db, "appl-2", "AIC-2", "token-two")
    client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "token-one"})
    assert appliance_activation.load_persisted_identity()["cloud_id"] == "AIC-1"

    # The reset endpoint lives in main.py (admin-gated), not appliance_cloud.py --
    # exercise the underlying function directly here (already covered via
    # HTTP auth-gating in test_appliance_activation_persistence.py's pure
    # suite); what matters for this test is that reset unblocks a real
    # different-cloud_id activation end to end.
    appliance_activation.reset_persisted_identity()
    assert appliance_activation.load_persisted_identity() is None

    response = client.post("/api/appliance/activate", json={"cloud_id": "AIC-2", "activation_token": "token-two"})
    assert response.status_code == 200
    recovered = appliance_activation.load_persisted_identity()
    assert recovered["cloud_id"] == "AIC-2"
    assert recovered["activation_version"] == 1


# =============================================================== POST /api/operations/appliance-identity/reset (main.app, admin-gated)


@pytest.fixture()
def main_client(db_path, identity_file, tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client, main


def test_reset_endpoint_requires_admin_portal_access(main_client, identity_file):
    client, main = main_client
    main.save_users([{"id": "viewer-1", "email": "viewer@example.test", "role": "viewer", "enabled": True, "camera_ids": []}])
    token = main.create_session("viewer-1")
    appliance_activation.persist_activation(appliance_id="appl-1", cloud_id="AIC-1", credential="cred-1", customer_id="cust-1", site_id="site-1", partner_id="partner-1")

    response = client.post("/api/operations/appliance-identity/reset", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 403
    assert appliance_activation.load_persisted_identity() is not None  # untouched by the denied attempt


def test_reset_endpoint_clears_identity_for_an_authorized_admin(main_client, identity_file):
    client, main = main_client
    main.save_users([{"id": "admin-1", "email": "admin@example.test", "role": "administrator", "enabled": True, "camera_ids": []}])
    token = main.create_session("admin-1")
    appliance_activation.persist_activation(appliance_id="appl-1", cloud_id="AIC-1", credential="cred-1", customer_id="cust-1", site_id="site-1", partner_id="partner-1")

    response = client.post("/api/operations/appliance-identity/reset", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 200
    assert appliance_activation.load_persisted_identity() is None
