"""Regression coverage for platform_owner.py (2026-09-23): the bootstrap
grant helper, TOTP/recovery-code/break-glass recovery primitives, and the
login-time MFA gate this module adds on top of the pre-existing, already-
tested identity_grants(role='administrator', scope_type='global') tier
(see that module's own docstring, and test_cloud_administrator_bridge.py/
test_bootstrap_admin_identity_grant.py for the pre-existing mechanism this
one builds on rather than replaces).

Camera-count-agnostic where relevant: this feature is account/grant-
shaped, not camera-shaped, so fleet size isn't a first-class axis here --
tenant-isolation tests instead vary customer/partner counts.
"""
import time

import pytest
from fastapi.testclient import TestClient

import appliance_identity
import main
import partner_portal
import platform_owner
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash


@pytest.fixture()
def http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    # The scope_type='global' -> /admin-portal routing this module's MFA
    # hook depends on is ONLY evaluated on the cloud-delegated-appliance
    # login path (own_appliance_identity() truthy) -- resolve_portal_
    # login() never even looks at identity_grants on the direct
    # partner_db path, by design (see main.py's own portal_login_
    # submit() docstring). So, like test_cloud_administrator_bridge.py's
    # own fixture, this instance must be configured as an activated
    # appliance for these tests to exercise the real routing at all --
    # env vars "win outright" over any stray persisted appliance_
    # identity.json left on this machine from unrelated testing (see
    # own_appliance_identity()'s own docstring), so this also
    # neutralizes that same cross-test-file contamination risk.
    monkeypatch.setenv("ANYAICAM_APPLIANCE_ID", "appl-platform-owner")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CLOUD_ID", "AIC-PLATFORM-OWNER")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CREDENTIAL", "platform-owner-credential")
    db_path = tmp_path / "test_platform_owner.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        with connection() as db:
            now = "2026-09-23T00:00:00"
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", now))
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Customer", "", "cust1@example.test", "active", "eligible", "real", now))
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Site", now))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)", ("appl-platform-owner", "cust-1", "site-1", "AIC-PLATFORM-OWNER", "partner-1", now))
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-platform-owner", "appl-platform-owner", password_hash("platform-owner-credential"), now))
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as test_client:
            yield test_client, db_path
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _seed_operator(db_path, email="amata@anyaicam.com", partner_id="partner-1"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            now = "2026-09-23T00:00:00"
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Partner", "approved", "real", now))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)", (f"u-{email}", partner_id, email, "Operator", "administrator", password_hash("x"), 1, now))
    return f"u-{email}"


def _admin_session():
    main.save_users([{"id": "admin-1", "email": "admin@local", "role": "administrator", "enabled": True, "camera_ids": []}])
    return main.create_session("admin-1")


def _grant(db_path, admin_token, client, *, email, role, scope_type, scope_id=None):
    payload = {"email": email, "role": role, "scope_type": scope_type}
    if scope_id:
        payload["scope_id"] = scope_id
    response = client.post("/api/operations/identity-grants", json=payload, cookies={main.SESSION_COOKIE_NAME: admin_token})
    assert response.status_code == 200, response.text
    return response.json()["grant_id"]


def _grant_global(db_path, admin_token, client, *, email):
    return _grant(db_path, admin_token, client, email=email, role="administrator", scope_type="global")


# ------------------------------------------------------- provision_platform_owner


def test_provision_platform_owner_is_a_noop_for_empty_email(http_client):
    _client, db_path = http_client
    with connection() as db:
        assert appliance_identity.provision_platform_owner(db, email="", granted_by="test") is None


def test_provision_platform_owner_is_a_noop_when_no_such_account_exists_yet(http_client):
    _client, db_path = http_client
    with connection() as db:
        assert appliance_identity.provision_platform_owner(db, email="nobody@anyaicam.com", granted_by="test") is None
        assert not appliance_identity.has_global_administrator_grant(db, email="nobody@anyaicam.com")


def test_provision_platform_owner_grants_an_existing_account(http_client):
    _client, db_path = http_client
    _seed_operator(db_path)
    with connection() as db:
        grant_id = appliance_identity.provision_platform_owner(db, email="amata@anyaicam.com", granted_by="test")
        assert grant_id is not None
        assert appliance_identity.has_global_administrator_grant(db, email="amata@anyaicam.com")


def test_provision_platform_owner_is_idempotent(http_client):
    _client, db_path = http_client
    _seed_operator(db_path)
    with connection() as db:
        first = appliance_identity.provision_platform_owner(db, email="amata@anyaicam.com", granted_by="test")
        second = appliance_identity.provision_platform_owner(db, email="amata@anyaicam.com", granted_by="test")
        assert first is not None
        assert second is None  # already granted -- does nothing, no duplicate grant row
        rows = db.execute("SELECT COUNT(*) AS n FROM identity_grants WHERE user_id=(SELECT id FROM partner_users WHERE email='amata@anyaicam.com')").fetchone()
        assert rows["n"] == 1


def test_provision_platform_owner_writes_an_audit_row(http_client):
    _client, db_path = http_client
    _seed_operator(db_path)
    with connection() as db:
        appliance_identity.provision_platform_owner(db, email="amata@anyaicam.com", granted_by="test-bootstrap")
        row = db.execute("SELECT actor_email,action FROM audit_logs WHERE action='grant' ORDER BY id DESC LIMIT 1").fetchone()
        assert row["actor_email"] == "test-bootstrap"


def test_env_var_driven_bootstrap_is_opt_in_and_off_by_default(http_client, monkeypatch):
    """initialize_database() itself calls provision_platform_owner() via
    ANYAICAM_PLATFORM_OWNER_EMAIL -- unset (the default for every
    deployment that hasn't opted in) must grant nothing to anyone."""
    _client, db_path = http_client
    _seed_operator(db_path)
    monkeypatch.delenv("ANYAICAM_PLATFORM_OWNER_EMAIL", raising=False)
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            assert not appliance_identity.has_global_administrator_grant(db, email="amata@anyaicam.com")


def test_env_var_driven_bootstrap_grants_the_configured_account(http_client, monkeypatch):
    _client, db_path = http_client
    _seed_operator(db_path)
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "amata@anyaicam.com")
    with override_target(sqlite_path=str(db_path)):
        initialize_database()  # idempotent -- re-running here must not error or duplicate
        with connection() as db:
            assert appliance_identity.has_global_administrator_grant(db, email="amata@anyaicam.com")


# ------------------------------------------------------------------------ TOTP


def test_totp_correct_code_verifies():
    secret = platform_owner.generate_totp_secret()
    now = time.time()
    code = platform_owner._totp_code_at(secret, int(now // 30))
    assert platform_owner.verify_totp_code(secret, code, now=now)


def test_totp_wrong_code_is_rejected():
    secret = platform_owner.generate_totp_secret()
    assert not platform_owner.verify_totp_code(secret, "000000", now=time.time())


def test_totp_tolerates_one_step_of_clock_skew():
    secret = platform_owner.generate_totp_secret()
    now = time.time()
    code = platform_owner._totp_code_at(secret, int(now // 30) - 1)
    assert platform_owner.verify_totp_code(secret, code, now=now, window=1)


def test_totp_rejects_beyond_the_configured_window():
    secret = platform_owner.generate_totp_secret()
    now = time.time()
    code = platform_owner._totp_code_at(secret, int(now // 30) - 5)
    assert not platform_owner.verify_totp_code(secret, code, now=now, window=1)


def test_totp_malformed_code_is_rejected_not_error():
    secret = platform_owner.generate_totp_secret()
    assert not platform_owner.verify_totp_code(secret, "not-a-code", now=time.time())
    assert not platform_owner.verify_totp_code(secret, "12345", now=time.time())  # too short


# ----------------------------------------------------------------- recovery codes


def test_recovery_codes_are_single_use(http_client):
    _client, db_path = http_client
    user_id = _seed_operator(db_path)
    with connection() as db:
        codes = platform_owner.generate_recovery_codes(db, user_id=user_id)
        assert len(codes) == 10
        assert platform_owner.consume_recovery_code(db, user_id=user_id, code=codes[0])
        assert not platform_owner.consume_recovery_code(db, user_id=user_id, code=codes[0])  # already used


def test_recovery_codes_regeneration_invalidates_the_old_set(http_client):
    _client, db_path = http_client
    user_id = _seed_operator(db_path)
    with connection() as db:
        old_codes = platform_owner.generate_recovery_codes(db, user_id=user_id)
        platform_owner.generate_recovery_codes(db, user_id=user_id)
        assert not platform_owner.consume_recovery_code(db, user_id=user_id, code=old_codes[0])


def test_recovery_code_for_one_user_does_not_work_for_another(http_client):
    _client, db_path = http_client
    user_a = _seed_operator(db_path, email="a@anyaicam.com")
    user_b = _seed_operator(db_path, email="b@anyaicam.com")
    with connection() as db:
        codes_a = platform_owner.generate_recovery_codes(db, user_id=user_a)
        assert not platform_owner.consume_recovery_code(db, user_id=user_b, code=codes_a[0])


def _concurrent_claims(db_path, n, claim_fn):
    """Run `claim_fn` (taking one open `db` connection) from `n` real OS
    threads at nearly the same instant (a Barrier holds every thread at
    the starting line until all have arrived), each on its own fresh
    connection scoped to `db_path` via its own override_target() --
    threading does not propagate contextvars into new threads, so each
    thread must re-enter the override itself, not rely on the caller's
    already-active context. Returns the list of per-thread results."""
    import threading

    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(index):
        with override_target(sqlite_path=str(db_path)):
            with connection() as db:
                barrier.wait()
                results[index] = claim_fn(db)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_recovery_code_single_use_holds_under_real_concurrent_redemption(http_client):
    """2026-09-23 review finding: the pre-fix consume_recovery_code() did
    a plain SELECT-then-UPDATE with no atomic claim -- two threads could
    both read "unused" before either wrote "used", double-spending one
    code. Fires two real OS threads at the same code at (as close to)
    the same instant as a Barrier can arrange; exactly one must
    succeed, never both, never neither."""
    _client, db_path = http_client
    user_id = _seed_operator(db_path)
    with connection() as db:
        codes = platform_owner.generate_recovery_codes(db, user_id=user_id)
    code = codes[0]

    results = _concurrent_claims(db_path, 2, lambda db: platform_owner.consume_recovery_code(db, user_id=user_id, code=code))
    assert sorted(results) == [False, True]

    with connection() as db:
        assert not platform_owner.consume_recovery_code(db, user_id=user_id, code=code)


# --------------------------------------------------------------- MFA enrollment


def test_mfa_enroll_requires_a_real_global_grant_session(http_client):
    client, db_path = http_client
    _seed_operator(db_path)  # administrator role, but NO grant yet
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    response = client.post("/api/platform-owner/mfa/enroll", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert response.status_code == 403


def test_mfa_enroll_and_confirm_full_flow(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)

    enroll = client.post("/api/platform-owner/mfa/enroll", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert enroll.status_code == 200
    secret = enroll.json()["secret_base32"]
    assert "otpauth://totp/" in enroll.json()["provisioning_uri"]

    with connection() as db:
        assert not platform_owner.mfa_is_confirmed(db, user_id=_lookup_user_id(db, "amata@anyaicam.com"))

    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    confirm = client.post("/api/platform-owner/mfa/confirm", json={"code": code}, cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert confirm.status_code == 200
    assert len(confirm.json()["recovery_codes"]) == 10

    with connection() as db:
        assert platform_owner.mfa_is_confirmed(db, user_id=_lookup_user_id(db, "amata@anyaicam.com"))


def test_mfa_confirm_with_wrong_code_does_not_confirm(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    client.post("/api/platform-owner/mfa/enroll", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    confirm = client.post("/api/platform-owner/mfa/confirm", json={"code": "000000"}, cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert confirm.status_code == 400
    with connection() as db:
        assert not platform_owner.mfa_is_confirmed(db, user_id=_lookup_user_id(db, "amata@anyaicam.com"))


def _lookup_user_id(db, email):
    return db.execute("SELECT id FROM partner_users WHERE email=?", (email,)).fetchone()["id"]


def _enroll_and_confirm_mfa(client, db_path, session_cookie):
    enroll = client.post("/api/platform-owner/mfa/enroll", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    secret = enroll.json()["secret_base32"]
    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    confirmed = client.post("/api/platform-owner/mfa/confirm", json={"code": code}, cookies={partner_portal.SESSION_COOKIE: session_cookie})
    return secret, confirmed.json()["recovery_codes"]


# ------------------------------------------------------------ login-time MFA gate


def test_login_without_mfa_confirmed_establishes_session_directly_unaffected(http_client):
    """The whole point: an account with NO MFA enrolled logs in exactly
    as before this module existed -- proves the hook is inert until
    MFA is actually confirmed."""
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/admin-portal"


def test_login_with_mfa_confirmed_requires_a_second_step(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    secret, _codes = _enroll_and_confirm_mfa(client, db_path, session_cookie)

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    assert login.status_code == 200
    body = login.json()
    assert body["status"] == "mfa_required"
    assert "mfa_pending_token" in body
    # No session cookie was set -- the login is genuinely not complete yet.
    assert partner_portal.SESSION_COOKIE not in login.cookies


def test_mfa_verify_with_correct_totp_completes_the_login(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    secret, _codes = _enroll_and_confirm_mfa(client, db_path, session_cookie)

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    pending_token = login.json()["mfa_pending_token"]

    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    verify = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending_token, "code": code}, follow_redirects=False)
    assert verify.status_code == 303
    assert verify.headers["location"] == "/admin-portal"
    new_session = verify.cookies[partner_portal.SESSION_COOKIE]

    page = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: new_session})
    assert page.status_code == 200


def test_mfa_verify_with_a_recovery_code_completes_the_login(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    _secret, codes = _enroll_and_confirm_mfa(client, db_path, session_cookie)

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    pending_token = login.json()["mfa_pending_token"]

    verify = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending_token, "code": codes[0]}, follow_redirects=False)
    assert verify.status_code == 303
    # The recovery code is now spent -- reusing it (a fresh pending login) must fail.
    login2 = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    pending_token2 = login2.json()["mfa_pending_token"]
    verify2 = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending_token2, "code": codes[0]}, follow_redirects=False)
    assert verify2.status_code == 400


def test_mfa_verify_with_wrong_code_does_not_establish_a_session(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    _enroll_and_confirm_mfa(client, db_path, session_cookie)

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    pending_token = login.json()["mfa_pending_token"]

    verify = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending_token, "code": "000000"}, follow_redirects=False)
    assert verify.status_code == 400
    assert partner_portal.SESSION_COOKIE not in verify.cookies


def test_mfa_pending_token_is_single_use(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    secret, _codes = _enroll_and_confirm_mfa(client, db_path, session_cookie)

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    pending_token = login.json()["mfa_pending_token"]
    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending_token, "code": code}, follow_redirects=False)

    replay = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending_token, "code": code}, follow_redirects=False)
    assert replay.status_code == 400


def test_pending_mfa_login_single_use_holds_under_real_concurrent_completion(http_client):
    """Same review finding, same fix, applied here: _consume_pending_mfa_
    login()'s prior SELECT-then-UPDATE (no rowcount check) let two
    concurrent completions of one pending login both read "unused"
    before either wrote."""
    _client, db_path = http_client
    user_id = _seed_operator(db_path)
    with connection() as db:
        token = platform_owner.create_pending_mfa_login(
            db, user_id=user_id, destination="/admin-portal", email="amata@anyaicam.com", role="administrator", authorization_version_at_login=1,
        )

    results = _concurrent_claims(db_path, 2, lambda db: platform_owner._consume_pending_mfa_login(db, token=token))
    outcomes = [bool(r) for r in results]
    assert sorted(outcomes) == [False, True]


def test_mfa_gate_never_applies_to_a_partner_scoped_administrator(http_client):
    """The hook's own scoping condition: destination must be exactly
    /admin-portal (a live global grant) -- a partner-scoped
    administrator (or any other role) is never routed through it, MFA
    enrolled or not."""
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    client.post("/api/operations/identity-grants", json={"email": "amata@anyaicam.com", "role": "administrator", "scope_type": "partner", "scope_id": "partner-1"}, cookies={main.SESSION_COOKIE_NAME: admin_token})

    login = client.post("/api/portal-login", json={"email": "amata@anyaicam.com", "password": "x", "portal": "administrator"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/partner?tab=customers"  # never mfa_required, never /admin-portal


# ----------------------------------------------------------------------- break-glass


def test_break_glass_token_requires_an_existing_live_grant(http_client):
    _client, db_path = http_client
    _seed_operator(db_path)  # no grant yet
    with connection() as db:
        token = platform_owner.create_break_glass_token(db, email="amata@anyaicam.com", reason="test", created_by="ops@anyaicam.com")
        assert token is None


def test_break_glass_full_recovery_flow(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")

    with connection() as db:
        token = platform_owner.create_break_glass_token(db, email="amata@anyaicam.com", reason="lost password and MFA device", created_by="ops@anyaicam.com")
    assert token

    redeem = client.post("/api/platform-owner/break-glass-recover", json={"email": "amata@anyaicam.com", "token": token}, follow_redirects=False)
    assert redeem.status_code == 303
    assert redeem.headers["location"] == "/admin-portal"
    session_cookie = redeem.cookies[partner_portal.SESSION_COOKIE]

    page = client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert page.status_code == 200


def test_break_glass_token_is_single_use(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    with connection() as db:
        token = platform_owner.create_break_glass_token(db, email="amata@anyaicam.com", reason="test", created_by="ops@anyaicam.com")

    client.post("/api/platform-owner/break-glass-recover", json={"email": "amata@anyaicam.com", "token": token}, follow_redirects=False)
    replay = client.post("/api/platform-owner/break-glass-recover", json={"email": "amata@anyaicam.com", "token": token}, follow_redirects=False)
    assert replay.status_code == 400


def test_break_glass_token_single_use_holds_under_real_concurrent_redemption(http_client):
    """Same review finding, same fix, applied to the single most
    sensitive credential in this module: _redeem_break_glass_token()'s
    prior SELECT-then-UPDATE (no rowcount check) let two concurrent
    redemptions of one break-glass token both read "unused" before
    either wrote."""
    _client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, _client, email="amata@anyaicam.com")
    with connection() as db:
        token = platform_owner.create_break_glass_token(db, email="amata@anyaicam.com", reason="test", created_by="ops@anyaicam.com")

    results = _concurrent_claims(db_path, 2, lambda db: platform_owner._redeem_break_glass_token(db, email="amata@anyaicam.com", token=token))
    outcomes = [bool(r) for r in results]
    assert sorted(outcomes) == [False, True]


def test_break_glass_recovery_does_not_bypass_a_since_revoked_grant(http_client):
    """The user's own explicit requirement: recovery must not bypass
    tenant/permission authorization once authenticated. A token created
    while the grant was live, then the grant gets revoked before the
    token is redeemed, must fail closed -- exactly like an ordinary
    session would on its next request (cloud_administrator_bridge()'s
    own live re-check)."""
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    grant_id = _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")

    with connection() as db:
        token = platform_owner.create_break_glass_token(db, email="amata@anyaicam.com", reason="test", created_by="ops@anyaicam.com")

    client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: admin_token})

    redeem = client.post("/api/platform-owner/break-glass-recover", json={"email": "amata@anyaicam.com", "token": token}, follow_redirects=False)
    assert redeem.status_code == 403


def test_break_glass_creation_is_audited(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    with connection() as db:
        platform_owner.create_break_glass_token(db, email="amata@anyaicam.com", reason="audit-test-reason", created_by="ops@anyaicam.com")
        row = db.execute("SELECT actor_email,action FROM audit_logs WHERE action='break_glass_create' ORDER BY id DESC LIMIT 1").fetchone()
        assert row["actor_email"] == "ops@anyaicam.com"


def test_break_glass_redemption_is_also_audited(http_client):
    client, db_path = http_client
    _seed_operator(db_path)
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="amata@anyaicam.com")
    with connection() as db:
        token = platform_owner.create_break_glass_token(db, email="amata@anyaicam.com", reason="test", created_by="ops@anyaicam.com")
    client.post("/api/platform-owner/break-glass-recover", json={"email": "amata@anyaicam.com", "token": token}, follow_redirects=False)
    with connection() as db:
        row = db.execute("SELECT actor_email,action FROM audit_logs WHERE action='break_glass_redeem' ORDER BY id DESC LIMIT 1").fetchone()
        assert row["actor_email"] == "amata@anyaicam.com"


def test_break_glass_route_is_the_only_web_reachable_break_glass_surface():
    """create_break_glass_token() must never be reachable as an HTTP
    route -- it's a plain function, callable only via direct script/
    shell access. Sanity check against a route-string leak."""
    from platform_owner import register_platform_owner_routes

    class _FakeApp:
        def __init__(self):
            self.routes = []

        def post(self, path, *a, **kw):
            self.routes.append(path)
            return lambda fn: fn

        def get(self, path, *a, **kw):
            self.routes.append(path)
            return lambda fn: fn

    fake_app = _FakeApp()
    register_platform_owner_routes(fake_app, shell=None)
    assert "/api/platform-owner/break-glass-create" not in fake_app.routes
    assert not any("create" in route and "break-glass" in route for route in fake_app.routes)


# --------------------------------------------------- isolation / regression checks


def test_granting_one_account_does_not_grant_a_second_unrelated_account(http_client):
    client, db_path = http_client
    _seed_operator(db_path, email="a@anyaicam.com")
    _seed_operator(db_path, email="b@anyaicam.com", partner_id="partner-2")
    admin_token = _admin_session()
    _grant_global(db_path, admin_token, client, email="a@anyaicam.com")
    with connection() as db:
        assert appliance_identity.has_global_administrator_grant(db, email="a@anyaicam.com")
        assert not appliance_identity.has_global_administrator_grant(db, email="b@anyaicam.com")


def test_ordinary_partner_owner_login_completely_unaffected_by_this_module(http_client):
    client, db_path = http_client
    _seed_operator(db_path, email="owner@example.test")
    admin_token = _admin_session()
    _grant(db_path, admin_token, client, email="owner@example.test", role="partner_owner", scope_type="partner", scope_id="partner-1")
    login = client.post("/api/portal-login", json={"email": "owner@example.test", "password": "x", "portal": "partner"}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/partner?tab=customers"


def test_customer_session_still_cannot_reach_platform_owner_routes(http_client):
    client, _db_path = http_client
    session_cookie = partner_portal._token("customer@example.test", "customer_owner", None, "cust-1", None)
    response = client.post("/api/platform-owner/mfa/enroll", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert response.status_code == 403


def test_anonymous_request_cannot_reach_platform_owner_routes(http_client):
    """A completely cookie-less request is intercepted before this
    module's own route handler even runs, by the same generic /api/
    auth-required middleware every other unauthenticated API route in
    this app is already subject to (401, not this module's own 403 --
    that 403 is what a real-but-ungranted session gets instead, proven
    by test_mfa_enroll_requires_a_real_global_grant_session above)."""
    client, _db_path = http_client
    response = client.post("/api/platform-owner/mfa/enroll")
    assert response.status_code == 401


def test_route_level_check_is_independent_of_any_nav_visibility(http_client):
    """Direct proof of the user's own explicit requirement: hitting the
    route with a bare client (no browser, no rendered nav at all) still
    correctly enforces authorization -- nav visibility was never in the
    request path to begin with."""
    client, db_path = http_client
    _seed_operator(db_path)
    # role='administrator' in partner_db, but no live global grant --
    # exactly today's real amata@anyaicam.com starting state before
    # provisioning, and the exact case that used to 403 on legacy
    # Admin-Portal-gated pages while the sidebar showed them anyway.
    session_cookie = partner_portal._token("amata@anyaicam.com", "administrator", None, None, None)
    response = client.post("/api/platform-owner/mfa/enroll", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert response.status_code == 403
