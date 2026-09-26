"""Regression coverage for a genuine gap found live during Samsung
validation: POST /api/partner/appliances/{id}/activation-token (the
"Regenerate activation token" admin action) only ever wrote appliances.
activation_token_hash -- a column POST /api/appliance/activate (the real
activation endpoint the appliance-agent's anyaicam-setup CLI actually
calls, appliance_cloud.py) never reads at all. That endpoint instead
accepts any appliance_activation_tokens row for the appliance that is
still unused, unrevoked, and unexpired. So "regenerating" a token
produced a value that could never activate anything, while the
original, still-valid row from onboarding time remained silently
usable forever -- the opposite of what an operator asking to invalidate
a lost token and issue a new one needs.

regenerate_activation_token() (partner_workspace.py) now revokes every
still-usable appliance_activation_tokens row for that appliance and
inserts a fresh one, so the returned token is the one the real
activation endpoint will actually accept, and the previous token
genuinely stops working.
"""
import secrets
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_activation_token_regen.db"):
    import appliance_cloud
    import partner_portal
    import partner_workspace
    from partner_db import connection, password_hash


def _seed_appliance_with_token(db, appliance_id, cloud_id, token, partner_id="partner-1", customer_id="cust-1", site_id="site-1"):
    now = "2026-08-27T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Partner", "approved", "real", now))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, "Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,partner_id,cloud_id,created_at) VALUES(?,?,?,?,?,?)", (appliance_id, customer_id, site_id, partner_id, cloud_id, now))
    db.execute(
        "INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
        (secrets.token_hex(4), appliance_id, password_hash(token), (datetime.now() + timedelta(hours=24)).isoformat(), now),
    )


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_activation_token_regen.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    import appliance_activation
    # Isolates POST /api/appliance/activate's persist_activation() step
    # (run as part of a successful activation, see appliance_cloud.py)
    # from the real default identity-file path -- same isolation
    # test_appliance_activation_endpoint.py's own fixture uses.
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", tmp_path / "appliance_identity.json")
    appliance_cloud.activation_limiter.events.clear()
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        partner_workspace.register_partner_workspace_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _db(db_path):
    return override_target(sqlite_path=str(db_path))


def _partner_owner_cookie():
    return {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "partner_owner", "partner-1", None, None)}


def _customer_owner_cookie():
    return {partner_portal.SESSION_COOKIE: partner_portal._token("customer@example.test", "customer_owner", "partner-1", "cust-1", None)}


def test_regenerated_token_actually_activates_the_appliance(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "original-token")

    response = client.post("/api/partner/appliances/appl-1/activation-token", cookies=_partner_owner_cookie())
    assert response.status_code == 200
    new_token = response.json()["activation_token"]

    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": new_token})
    assert activate.status_code == 200, activate.text


def test_the_previous_token_no_longer_activates_after_regeneration(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "original-token")

    regen = client.post("/api/partner/appliances/appl-1/activation-token", cookies=_partner_owner_cookie())
    assert regen.status_code == 200

    stale = client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": "original-token"})
    assert stale.status_code == 403
    assert "invalid" in stale.json()["detail"].lower()


def test_regenerating_twice_only_the_latest_token_works(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "original-token")

    client.post("/api/partner/appliances/appl-1/activation-token", cookies=_partner_owner_cookie())
    second = client.post("/api/partner/appliances/appl-1/activation-token", cookies=_partner_owner_cookie())
    second_token = second.json()["activation_token"]

    activate = client.post("/api/appliance/activate", json={"cloud_id": "AIC-1", "activation_token": second_token})
    assert activate.status_code == 200, activate.text


def test_unknown_appliance_id_returns_404(client, db_path):
    with _db(db_path):
        pass  # no appliance seeded at all
    response = client.post("/api/partner/appliances/does-not-exist/activation-token", cookies=_partner_owner_cookie())
    assert response.status_code == 404


def test_a_role_without_appliance_assign_is_denied(client, db_path):
    with _db(db_path):
        with connection() as db:
            _seed_appliance_with_token(db, "appl-1", "AIC-1", "original-token")

    response = client.post("/api/partner/appliances/appl-1/activation-token", cookies=_customer_owner_cookie())
    assert response.status_code == 403
