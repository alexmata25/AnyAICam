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

  * claim/complete performs the one-time exchange into the existing,
    unmodified persist_activation()/appliance_credentials machinery,
    fails closed on a replayed or expired proof, and the resulting
    credential authenticates successfully against the untouched
    authenticate_appliance() used by every other appliance route
  * no claim_code, claim_proof, or credential value is ever written to
    the log stream, and (this is what actually caught the URL-path
    deviation documented in appliance_claims.py) neither is the full
    request line of any call that used to carry one of those values in
    its URL
  * device_id must be a proper UUIDv4 (security-hardening checkpoint):
    sequential/short/malformed strings and UUIDv1/v3/v5 (same
    8-4-4-4-12 shape, different version/variant nibbles) are all
    rejected by claim/begin -- the regression coverage for the device-
    hijack blocker closed in that pass (see appliance_claims.py's own
    comment on DEVICE_ID_PATTERN and
    docs/non-interactive-activation-phase1-security-hardening-report.md)

Imports appliance_cloud/appliance_claims (which import partner_db,
triggering its import-time schema init) -- redirects to a throwaway
sqlite file via override_target() before that import happens, matching
this project's own documented constraint and every other test file's
established pattern (see test_appliance_updates_latest.py).
"""

import logging
import secrets
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_claims_import.db"):
    import appliance_activation
    import appliance_claims
    import appliance_cloud
    import partner_portal
    from partner_db import connection

# A real device_id must be a UUIDv4 (see appliance_claims.py's own
# DEVICE_ID_PATTERN comment) -- this fixed value stands in for what
# installer/09-identity.sh's `/proc/sys/kernel/random/uuid` would
# actually generate on a real appliance.
VALID_DEVICE_ID = "11111111-1111-4111-8111-111111111111"


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


def _appliance_auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_appliance_claims.db"


@pytest.fixture()
def identity_file(tmp_path, monkeypatch):
    # claim_complete() calls the real, unmodified persist_activation(),
    # which reads/writes ACTIVATION_IDENTITY_FILE -- a real local file
    # (defaults to /app/recordings/appliance_identity.json), not scoped
    # by override_target() (that only covers the SQL database target).
    # Without this, every test here that reaches claim/complete would
    # silently read and mutate whatever real file happens to sit at
    # that default path on the machine running the tests -- exactly
    # the isolation test_appliance_activation_endpoint.py's own
    # matching fixture already established for the equivalent
    # /api/appliance/activate tests. Found via a security-audit
    # verification script hitting a spurious ActivationConflict caused
    # by exactly this leakage; fixed here as the one concrete
    # correctness defect that audit turned up.
    path = tmp_path / "appliance_identity.json"
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", path)
    return path


@pytest.fixture()
def claim_flow_key(monkeypatch):
    # Security-hardening checkpoint: portal_claim_confirm() now fails
    # closed (503) unless ANYAICAM_CLAIM_FLOW_SECRET_KEY is configured
    # -- see appliance_protocol.claim_flow_secret_key()'s own
    # docstring. A fresh, random Fernet key per test, matching
    # test_appliance_updates_latest.py's own signing_key_pair fixture
    # pattern for the equivalent update-signing-key requirement.
    from cryptography.fernet import Fernet
    monkeypatch.setenv("ANYAICAM_CLAIM_FLOW_SECRET_KEY", Fernet.generate_key().decode())


def _shell_stub(title, icon, content, scripts=''):
    # Minimal stand-in for main.py's real page_shell -- just enough
    # structure (title, content, scripts all present in the output) for
    # a test to assert against, matching appliance_cloud's own
    # shell=lambda *a, **k: "" stub philosophy but preserving enough to
    # actually check the new claim page's content (Phase 2A).
    return f'<html><head><title>{title} · {icon}</title></head><body>{content}{scripts}</body></html>'


@pytest.fixture()
def client(db_path, identity_file, claim_flow_key):
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
        appliance_claims.register_appliance_claim_routes(app, shell=_shell_stub)
        with TestClient(app) as test_client:
            yield test_client


def _seeded_customer(db_path, **kwargs):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_customer(db, **kwargs)


def _begin(client, device_id=VALID_DEVICE_ID):
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
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-1", "cust-1", "site-1", VALID_DEVICE_ID.upper(), "2026-09-10T00:00:00"))

    response = _begin(client)

    assert response.status_code == 409


def test_begin_rejects_malformed_device_id(client, db_path):
    _seeded_customer(db_path)

    response = _begin(client, device_id="bad")

    assert response.status_code == 400


def test_begin_accepts_a_real_uuid4_regardless_of_casing(client, db_path):
    # installer/09-identity.sh generates lowercase (kernel uuid), but
    # nothing about the wire format should reject uppercase.
    _seeded_customer(db_path)

    response = _begin(client, device_id=VALID_DEVICE_ID.upper())

    assert response.status_code == 200


# --------------------------------------------------------- device_id hardening:
# regression coverage for the device-hijack blocker (security-hardening
# checkpoint). Each of these device_id values was, before this pass,
# accepted by the old 8-128-char-alphanumeric pattern -- any one of
# them being accepted meant an unauthenticated caller could open a
# claim (and learn its claim_code) for a device_id it merely guessed.
# None of these may ever reach 200.


@pytest.mark.parametrize(
    "device_id",
    [
        pytest.param("AIC-SERIAL-000042", id="sequential_vendor_serial"),
        pytest.param("00000001", id="short_sequential_id"),
        pytest.param("DEVICE042", id="short_predictable_label"),
        pytest.param("11111111-1111-1111-1111-111111111111", id="all_same_digit_not_a_real_uuid_version"),
        pytest.param(str(uuid.uuid1()), id="uuid1_time_based"),
        pytest.param(str(uuid.uuid3(uuid.NAMESPACE_DNS, "anyaicam-appliance")), id="uuid3_namespace_md5"),
        pytest.param(str(uuid.uuid5(uuid.NAMESPACE_DNS, "anyaicam-appliance")), id="uuid5_namespace_sha1"),
        pytest.param("11111111111141118111111111111111", id="uuid4_shape_without_hyphens"),
        pytest.param("11111111-1111-5111-8111-111111111111", id="malformed_uuid_wrong_version_nibble"),
        pytest.param("11111111-1111-4111-c111-111111111111", id="malformed_uuid_wrong_variant_nibble"),
        pytest.param("g1111111-1111-4111-8111-111111111111", id="malformed_uuid_non_hex_character"),
        pytest.param("11111111-1111-4111-8111-11111111111", id="malformed_uuid_too_short"),
        pytest.param("11111111-1111-4111-8111-1111111111111", id="malformed_uuid_too_long"),
        pytest.param("", id="empty_string"),
    ],
)
def test_begin_rejects_non_uuid4_device_ids(client, db_path, device_id):
    _seeded_customer(db_path)

    response = _begin(client, device_id=device_id)

    assert response.status_code == 400
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliance_claims").fetchone()["n"]
    assert count == 0, f"a claim session (and its claim_code) must never be created for a rejected device_id, got {device_id!r}"


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


# --------------------------------------------------------- security-hardening
# checkpoint: claim_proof is encrypted at rest, not stored raw, and an
# abandoned (claimed-but-never-completed) claim is swept to a terminal
# state that clears the recoverable material -- regression coverage for
# both halves of hardening item 2.


def test_claim_proof_is_never_stored_raw_in_the_database(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()
    confirm = _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie())
    assert confirm.status_code == 200
    proof = _status(client, session["claim_session_id"]).json()["claim_proof"]

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            stored = db.execute(
                "SELECT claim_proof_encrypted, claim_proof_plaintext FROM appliance_claims WHERE claim_session_id=?",
                (session["claim_session_id"],),
            ).fetchone()

    assert stored["claim_proof_plaintext"] is None, "the retired plaintext column must never be written by new code"
    assert stored["claim_proof_encrypted"] is not None
    assert proof not in stored["claim_proof_encrypted"], "the raw proof value must not appear inside its own encrypted form"


def test_confirm_fails_closed_when_claim_flow_key_is_unset(client, db_path, monkeypatch):
    monkeypatch.delenv("ANYAICAM_CLAIM_FLOW_SECRET_KEY", raising=False)
    _seeded_customer(db_path)
    session = _begin(client).json()

    response = _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie())

    assert response.status_code == 503
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            status = db.execute(
                "SELECT status FROM appliance_claims WHERE claim_session_id=?", (session["claim_session_id"],)
            ).fetchone()["status"]
    assert status == "pending", "a failed-closed confirm must never leave the claim half-claimed"


def test_abandoned_claimed_row_expires_and_clears_recoverable_proof(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()
    confirm = _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie())
    assert confirm.status_code == 200
    # Simulate the device never coming back to redeem the proof, past
    # its TTL -- the exact scenario the security audit found lingered
    # forever in the original Phase 1 implementation.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "UPDATE appliance_claims SET proof_expires_at=? WHERE claim_session_id=?",
                ("2020-01-01T00:00:00", session["claim_session_id"]),
            )

    status_response = _status(client, session["claim_session_id"]).json()

    assert status_response == {"status": "expired"}
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row_after = db.execute(
                "SELECT status, claim_proof_encrypted, claim_proof_plaintext, claim_proof_hash FROM appliance_claims WHERE claim_session_id=?",
                (session["claim_session_id"],),
            ).fetchone()
    assert row_after["status"] == "expired"
    assert row_after["claim_proof_encrypted"] is None
    assert row_after["claim_proof_plaintext"] is None
    # claim_proof_hash is deliberately left alone -- one-way, needed
    # for audit trail, matching appliance_activation_tokens's own
    # forever-retention of used/expired token hashes.
    assert row_after["claim_proof_hash"] is not None


def test_expired_claimed_row_can_no_longer_be_completed(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()
    confirm = _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie())
    assert confirm.status_code == 200
    proof = _status(client, session["claim_session_id"]).json()["claim_proof"]
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "UPDATE appliance_claims SET proof_expires_at=? WHERE claim_session_id=?",
                ("2020-01-01T00:00:00", session["claim_session_id"]),
            )
    # Touch claim/status once so the lazy-expiry sweep actually runs
    # (matches how a device would naturally discover this).
    _status(client, session["claim_session_id"])

    response = client.post(
        "/api/appliance/claim/complete",
        json={"claim_session_id": session["claim_session_id"], "claim_proof": proof},
    )

    assert response.status_code == 403
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (VALID_DEVICE_ID.upper(),)).fetchone()["n"]
    assert count == 0


# --------------------------------------------------------- Phase 2A: customer claim page


def test_claim_page_redirects_unauthenticated_visitors_to_login(client, db_path):
    response = client.get("/customer/claim-appliance", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/partner-login"


def test_claim_page_rejects_non_customer_owner_roles(client, db_path):
    admin_cookie = partner_portal._token("admin@example.test", "administrator", None, None)

    response = client.get("/customer/claim-appliance", cookies={partner_portal.SESSION_COOKIE: admin_cookie})

    assert response.status_code == 403


def test_claim_page_renders_the_customers_own_sites(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_customer(db)
            _seed_other_customer_site(db)

    response = client.get("/customer/claim-appliance", cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})

    assert response.status_code == 200
    assert "Claim an appliance" in response.text
    assert "site-1" in response.text
    assert "site-other" not in response.text, "must never list a different customer's site"


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
    assert response.json() == {"status": "claimed", "device_id": VALID_DEVICE_ID}


def test_portal_confirm_twice_is_conflict(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()
    cookie = _customer_cookie()
    first = _confirm(client, session["claim_code"], "site-1", cookie=cookie)
    assert first.status_code == 200

    second = _confirm(client, session["claim_code"], "site-1", cookie=cookie)

    assert second.status_code in (403, 404, 409)


# --------------------------------------------------------- claim/complete


def _claim_through_to_proof(client, db_path, device_id=VALID_DEVICE_ID):
    _seeded_customer(db_path)
    session = _begin(client, device_id=device_id).json()
    confirm = _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie())
    assert confirm.status_code == 200
    status = _status(client, session["claim_session_id"]).json()
    return session["claim_session_id"], status["claim_proof"]


def test_complete_produces_activate_shaped_response_and_working_credential(client, db_path):
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)

    response = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"appliance_id", "cloud_id", "credential", "credential_id", "partner_id", "customer_id", "site_id", "message"}
    assert body["cloud_id"] == VALID_DEVICE_ID.upper()
    assert body["customer_id"] == "cust-1"
    assert body["site_id"] == "site-1"

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            appliance = db.execute("SELECT * FROM appliances WHERE id=?", (body["appliance_id"],)).fetchone()
            assert appliance["customer_id"] == "cust-1"
            assert appliance["site_id"] == "site-1"

    # End-to-end: the freshly minted credential authenticates against
    # the existing, untouched authenticate_appliance() boundary exactly
    # like any admin-activated appliance's credential would.
    heartbeat = client.post(
        "/api/appliance/heartbeat",
        headers=_appliance_auth_headers(body["appliance_id"], body["credential"]),
        json={"uptime_seconds": 120, "cpu": 5, "memory": 10},
    )
    assert heartbeat.status_code == 200


def test_complete_retry_recovers_the_same_result_not_a_second_credential(client, db_path):
    # Security-hardening checkpoint, hardening item 3: a retry with the
    # SAME valid claim_session_id+claim_proof after a successful
    # completion (simulating the device never seeing the first
    # response) must recover the identical activation result -- never
    # a 403/409 that strands the device, and never a second credential.
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    first = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})
    assert first.status_code == 200

    second = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})

    assert second.status_code == 200
    assert second.json() == first.json(), "a retry must recover the exact same result, not a different (e.g. rotated) credential"
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            appliance_count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (VALID_DEVICE_ID.upper(),)).fetchone()["n"]
            credential_count = db.execute(
                "SELECT COUNT(*) AS n FROM appliance_credentials WHERE appliance_id=?", (first.json()["appliance_id"],)
            ).fetchone()["n"]
    assert appliance_count == 1, "only one appliance may ever be created for this claim"
    assert credential_count == 1, "only one appliance credential may ever be created for this claim"


def test_complete_retry_fails_closed_with_wrong_proof_even_after_completion(client, db_path):
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    first = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})
    assert first.status_code == 200

    wrong = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": "not-the-real-proof"})

    assert wrong.status_code == 403


def test_complete_retry_after_recovery_window_expires_is_a_clean_conflict(client, db_path):
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    first = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})
    assert first.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "UPDATE appliance_claims SET credential_recovery_expires_at=? WHERE claim_session_id=?",
                ("2020-01-01T00:00:00", claim_session_id),
            )

    retry = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})

    assert retry.status_code == 409
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            appliance_count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (VALID_DEVICE_ID.upper(),)).fetchone()["n"]
            encrypted_after = db.execute(
                "SELECT completed_credential_encrypted FROM appliance_claims WHERE claim_session_id=?", (claim_session_id,)
            ).fetchone()["completed_credential_encrypted"]
    assert appliance_count == 1, "a stale retry must never mint a second credential"
    assert encrypted_after is None, "the recovery ciphertext must be cleared once its own window has passed"


def test_complete_retry_survives_a_simulated_process_restart(client, db_path, identity_file):
    # Recovery material must be durably persisted (the database), not
    # cached in this process's memory -- a fresh FastAPI app/TestClient
    # instance pointed at the SAME database stands in for "a different
    # cloud worker process, or this same process after a restart,
    # handles the retry".
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    first = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})
    assert first.status_code == 200

    with override_target(sqlite_path=str(db_path)):
        fresh_app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(fresh_app, shell=lambda *a, **k: "")
        appliance_claims.register_appliance_claim_routes(fresh_app)
        with TestClient(fresh_app) as fresh_client:
            retry = fresh_client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})

    assert retry.status_code == 200
    assert retry.json() == first.json()


def test_complete_is_multi_worker_safe_under_concurrent_retries(client, db_path, identity_file, monkeypatch):
    # The claim is completed once (sequentially, establishing the
    # 'completed' state a real retry would actually encounter), then
    # twenty concurrent callers -- standing in for the device's own
    # retry racing against several other cloud worker processes
    # handling the same lost-response retry -- all present the SAME
    # valid claim_session_id+claim_proof simultaneously against that
    # already-completed claim. Exactly one appliance and one credential
    # may exist afterward, and every successful (200) response must
    # carry the identical result.
    #
    # threading.Thread does not inherit contextvars, so override_target
    # (a contextvars.ContextVar) is invisible in a naively-spawned
    # thread -- each worker thread below binds it itself, in its own
    # native context, rather than trying to share one contextvars.Context
    # across concurrent threads (which raises "context already entered").
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    original = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})
    assert original.status_code == 200
    # This test isolates multi-worker retry-safety specifically, not
    # activation_limiter's own (separately tested) rate-limit capacity
    # -- raise its limit for this test's duration so 20 concurrent
    # calls (from 20 distinct in-process "workers", all sharing the
    # TestClient's single fixed source IP) aren't partly rejected by a
    # budget this test isn't about.
    appliance_cloud.activation_limiter.events.clear()
    monkeypatch.setattr(appliance_cloud.activation_limiter, "limit", 1000)
    results = []
    lock = threading.Lock()

    def worker():
        with override_target(sqlite_path=str(db_path)):
            worker_app = FastAPI()
            appliance_cloud.register_appliance_cloud_routes(worker_app, shell=lambda *a, **k: "")
            appliance_claims.register_appliance_claim_routes(worker_app)
            with TestClient(worker_app) as worker_client:
                response = worker_client.post(
                    "/api/appliance/claim/complete",
                    json={"claim_session_id": claim_session_id, "claim_proof": claim_proof},
                )
        with lock:
            results.append((response.status_code, response.json()))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    successes = [body for status, body in results if status == 200]
    assert len(successes) == 20, f"every concurrent retry of a valid, already-completed claim must succeed, got statuses {[s for s, _ in results]}"
    assert all(body == original.json() for body in successes), "every concurrent retry must recover the exact original result"
    assert len({body["appliance_id"] for body in successes}) == 1
    assert len({body["credential"] for body in successes}) == 1
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            appliance_count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (VALID_DEVICE_ID.upper(),)).fetchone()["n"]
            credential_count = db.execute(
                "SELECT COUNT(*) AS n FROM appliance_credentials WHERE appliance_id=?", (successes[0]["appliance_id"],)
            ).fetchone()["n"]
    assert appliance_count == 1
    assert credential_count == 1


def test_complete_rejects_expired_proof(client, db_path):
    claim_session_id, claim_proof = _claim_through_to_proof(client, db_path)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("UPDATE appliance_claims SET proof_expires_at=? WHERE claim_session_id=?", ("2020-01-01T00:00:00", claim_session_id))

    response = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": claim_proof})

    assert response.status_code == 403
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM appliances WHERE cloud_id=?", (VALID_DEVICE_ID.upper(),)).fetchone()["n"]
            assert count == 0


def test_complete_rejects_wrong_proof(client, db_path):
    claim_session_id, _ = _claim_through_to_proof(client, db_path)

    response = client.post("/api/appliance/claim/complete", json={"claim_session_id": claim_session_id, "claim_proof": "not-the-real-proof"})

    assert response.status_code == 403


# --------------------------------------------------------- secret hygiene


def test_full_happy_path_never_logs_secrets(client, db_path, caplog):
    caplog.set_level(logging.DEBUG)
    _seeded_customer(db_path)
    session = _begin(client).json()
    confirm = _confirm(client, session["claim_code"], "site-1", cookie=_customer_cookie())
    assert confirm.status_code == 200
    status = _status(client, session["claim_session_id"]).json()
    complete = client.post("/api/appliance/claim/complete", json={"claim_session_id": session["claim_session_id"], "claim_proof": status["claim_proof"]})
    assert complete.status_code == 200
    credential = complete.json()["credential"]

    secrets_that_must_never_be_logged = [session["claim_code"], status["claim_proof"], credential]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in secrets_that_must_never_be_logged:
        assert secret not in log_text
