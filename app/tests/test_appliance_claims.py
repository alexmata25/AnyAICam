"""Phase 1 of the non-interactive/self-service appliance claim flow --
see docs/non-interactive-activation-phase1-plan.md and
appliance_claims.py's own module docstring for the full design this
proves. This commit adds coverage for the portal-facing lookup/confirm
routes on top of the previous commit's device-facing claim/begin and
claim/status:

  * claim/begin issues a session+code, and is idempotent (resumes the
    same session) while one is already pending for a device
  * claim/begin refuses a device_id that is already provisioned or
    malformed
  * claim/status never distinguishes "wrong id" from "expired" (no
    session-id enumeration oracle), and lazily expires a stale pending
    claim
  * the portal lookup/confirm routes enforce the exact same
    partner_identity()+customer_owner+appliance.self.link boundary the
    existing POST /api/customer/appliances/link route already uses,
    including the site-ownership check, and claim/status starts
    returning claim_proof once a claim is confirmed

claim/complete tests are added to this same file in the next commit,
once that route exists (see the Phase 1 plan doc's 6-commit
implementation order).

Imports appliance_cloud/appliance_claims (which import partner_db,
triggering its import-time schema init) -- redirects to a throwaway
sqlite file via override_target() before that import happens, matching
this project's own documented constraint and every other test file's
established pattern (see test_appliance_updates_latest.py).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_claims_import.db"):
    import appliance_claims
    import appliance_cloud
    import partner_portal
    from partner_db import connection


def _seed_customer(db, customer_id="cust-1", site_id="site-1", partner_id="partner-1", email="owner@example.test"):
    now = "2026-09-10T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, "Test Customer", email, "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))


def _seed_other_customer_site(db, site_id="site-other"):
    now = "2026-09-10T00:00:00"
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-other", "partner-1", "Other Customer", "other@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, "cust-other", "Other Site", now))


def _customer_cookie(email="owner@example.test", customer_id="cust-1", partner_id=None):
    return partner_portal._token(email, "customer_owner", partner_id, customer_id)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_appliance_claims.db"


@pytest.fixture()
def client(db_path):
    # Shared module-level RateLimiter singletons, never reset by
    # database/target isolation -- see test_appliance_activation_
    # endpoint.py's matching fixture comment.
    appliance_cloud.activation_limiter.events.clear()
    appliance_claims.claim_begin_limiter.events.clear()
    appliance_claims.claim_status_limiter.events.clear()
    appliance_claims.claim_portal_limiter.events.clear()
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        appliance_claims.register_appliance_claim_routes(app)
        with TestClient(app) as test_client:
            yield test_client


def _seeded_customer(db_path, **kwargs):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_customer(db, **kwargs)


def _begin(client, device_id="AIC-DEVICE-0001"):
    return client.post("/api/appliance/claim/begin", json={"device_id": device_id})


def _status(client, claim_session_id):
    return client.post("/api/appliance/claim/status", json={"claim_session_id": claim_session_id})


def _lookup(client, claim_code, cookie=None):
    cookies = {partner_portal.SESSION_COOKIE: cookie} if cookie else {}
    return client.post("/api/portal/claims/lookup", cookies=cookies, json={"claim_code": claim_code})


def _confirm(client, claim_code, site_id, cookie=None):
    cookies = {partner_portal.SESSION_COOKIE: cookie} if cookie else {}
    return client.post("/api/portal/claims/confirm", cookies=cookies, json={"claim_code": claim_code, "site_id": site_id})


# --------------------------------------------------------- claim/begin


def test_begin_creates_pending_session(client, db_path):
    _seeded_customer(db_path)

    response = _begin(client)

    assert response.status_code == 200
    body = response.json()
    assert body["claim_session_id"]
    assert body["claim_code"] and len(body["claim_code"]) == 8
    assert body["resumed"] is False


def test_begin_is_idempotent_while_pending(client, db_path):
    _seeded_customer(db_path)

    first = _begin(client).json()
    second = _begin(client).json()

    assert second["claim_session_id"] == first["claim_session_id"]
    assert second["resumed"] is True


def test_begin_rejects_already_provisioned_device(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_customer(db)
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-1", "cust-1", "site-1", "AIC-DEVICE-0001", "2026-09-10T00:00:00"))

    response = _begin(client)

    assert response.status_code == 409


def test_begin_rejects_malformed_device_id(client, db_path):
    _seeded_customer(db_path)

    response = _begin(client, device_id="bad")

    assert response.status_code == 400


# --------------------------------------------------------- claim/status


def test_status_unknown_session_reports_expired_not_404(client, db_path):
    _seeded_customer(db_path)

    response = _status(client, "does-not-exist")

    assert response.status_code == 200
    assert response.json() == {"status": "expired"}


def test_status_is_pending_before_confirm(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()

    response = _status(client, session["claim_session_id"])

    assert response.json()["status"] == "pending"


def test_status_lazily_expires_stale_pending_claim(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    session = _begin(client).json()
    monkeypatch.setattr(appliance_claims, "CLAIM_SESSION_TTL_MINUTES", -1)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("UPDATE appliance_claims SET expires_at=? WHERE claim_session_id=?", ("2020-01-01T00:00:00", session["claim_session_id"]))

    first = _status(client, session["claim_session_id"]).json()
    second = _status(client, session["claim_session_id"]).json()

    assert first["status"] == "expired"
    assert second["status"] == "expired"


def test_status_returns_proof_repeatedly_once_claimed(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()
    assert _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie()).status_code == 200

    first = _status(client, session["claim_session_id"]).json()
    second = _status(client, session["claim_session_id"]).json()

    assert first["status"] == "claimed" and first["claim_proof"]
    assert second["claim_proof"] == first["claim_proof"]


# --------------------------------------------------------- portal lookup/confirm


def test_portal_lookup_requires_customer_owner_role(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()

    response = _lookup(client, session["claim_code"])

    assert response.status_code == 403


def test_portal_lookup_wrong_code_is_404(client, db_path):
    _seeded_customer(db_path)
    _begin(client)

    response = _lookup(client, "WRONGCOD", cookie=_customer_cookie())

    assert response.status_code == 404


def test_portal_confirm_rejects_site_owned_by_a_different_customer(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_customer(db)
            _seed_other_customer_site(db)
    session = _begin(client).json()

    response = _confirm(client, session["claim_code"], "site-other", cookie=_customer_cookie())

    assert response.status_code == 403


def test_portal_confirm_succeeds_for_own_site(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()

    response = _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie())

    assert response.status_code == 200
    assert response.json() == {"status": "claimed", "device_id": "AIC-DEVICE-0001"}


def test_portal_confirm_twice_is_conflict(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()
    cookie = _customer_cookie()
    first = _confirm(client, session["claim_code"], "site-1", cookie=cookie)
    assert first.status_code == 200

    second = _confirm(client, session["claim_code"], "site-1", cookie=cookie)

    assert second.status_code in (403, 404, 409)
