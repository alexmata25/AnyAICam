"""POST /api/portal-login on a CLAIMED Windows appliance, after the independent Codex review of R0 77070be
(2026-11-06, P1). Owner policy 2026-10-28: the appliance keeps the b8 selection (the local administrator wins when
both identities validate; no selection with both is refused). Owner policy R-2 (2026-10-29): cloud credentials alone
never grant claimed-appliance administration. These tests pin, at the HTTP route:
  1. explicit Administrator + a valid local administrator -> a local recovery session only, the cloud never consulted
     (so an upstream outage can't block it), no partner session, no identity-grant authority, labelled in the audit;
  2. omitted or invalid selection -> refused, no session, no cloud call;
  3. cloud credentials alone -> the R-2 403 (and the outage 503), no session;
  4. the users.json lockout of POST /login applies here too.
"""
import json

import pytest

import appliance_identity
import main
import partner_portal
from authz_helpers import table_snapshot
from partner_db import connection
from test_windows_1341_r2_claimed_appliance_admin_policy import (  # noqa: F401
    ADMIN, PASSWORD, _cloud_admin, _isolated_recordings, _no_session, claimed)

LOCAL_PASSWORD = "Local-Owner-Pass-1"


def _local_admin(email=ADMIN, password=LOCAL_PASSWORD, **extra):
    main.save_users([dict({"id": "local-owner", "display_name": "Local owner", "email": email, "role": "administrator",
                           "enabled": True, "password_hash": main.hash_password(password), "site_ids": ["home"],
                           "camera_ids": []}, **extra)])


class _Backend:
    """Stands in for the cloud identity backend: records calls; optionally unavailable."""

    def __init__(self, real, unavailable=False):
        self.real, self.unavailable, self.calls = real, unavailable, []

    def authenticate_operator(self, **kwargs):
        self.calls.append(kwargs)
        if self.unavailable:
            raise appliance_identity.CloudIdentityUnavailable("synthetic outage")
        return self.real.authenticate_operator(**kwargs)

    def public_keys(self):
        return self.real.public_keys()


@pytest.fixture()
def backend(monkeypatch):
    def install(unavailable=False):
        spy = _Backend(appliance_identity.get_cloud_identity_backend(), unavailable)
        monkeypatch.setattr(appliance_identity, "get_cloud_identity_backend", lambda: spy)
        return spy
    return install


def _login(client, portal, password):
    body = {"email": ADMIN, "password": password}
    if portal is not ...:
        body["portal"] = portal
    return client.post("/api/portal-login", json=body, follow_redirects=False)


def _admin_sessions():
    with connection() as db:
        return db.execute("SELECT COUNT(*) AS n FROM user_sessions WHERE lower(email)=?", (ADMIN,)).fetchone()["n"]


# ------------------------------------------------------------------ 1. explicit Administrator, local identity valid


@pytest.mark.parametrize("unavailable", [False, True])
def test_explicit_administrator_with_both_identities_gets_only_a_local_recovery_session(claimed, backend, tmp_path,
                                                                                        monkeypatch, unavailable):
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit_log.jsonl")
    _cloud_admin("global", enrolled=True)
    _local_admin(password=PASSWORD)  # the same email AND password in both systems
    spy = backend(unavailable)
    response = _login(claimed, "administrator", PASSWORD)
    assert response.status_code == 303 and response.headers["location"] == "/admin-portal"
    assert spy.calls == []  # the cloud is never consulted, so an outage can't block local recovery
    assert partner_portal.SESSION_COOKIE not in response.cookies
    token = response.cookies.get(main.SESSION_COOKIE_NAME)
    assert token
    assert _admin_sessions() == 0  # no partner/cloud session row
    audit = [json.loads(line) for line in (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any("Local emergency recovery sign-in" in entry.get("detail", "") and "portal login" in entry.get("detail", "")
               for entry in audit)
    cookies = {main.SESSION_COOKIE_NAME: token}
    page = claimed.get("/admin-portal", cookies=cookies)
    assert page.status_code == 200 and '<p class="eyebrow">Access control</p>' not in page.text


def test_the_selector_local_session_has_no_identity_grant_authority(claimed, backend, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit_log.jsonl")
    uid = _cloud_admin("global", enrolled=True)
    with connection() as db:
        grant_id = db.execute("SELECT id FROM identity_grants WHERE user_id=?", (uid,)).fetchone()["id"]
    _local_admin(password=PASSWORD)
    backend()
    token = _login(claimed, "administrator", PASSWORD).cookies.get(main.SESSION_COOKIE_NAME)
    assert token
    cookies = {main.SESSION_COOKIE_NAME: token}
    before = table_snapshot()
    assert claimed.get("/api/operations/identity-grants", cookies=cookies).status_code in (401, 403)
    assert claimed.post("/api/operations/identity-grants", cookies=cookies,
                        json={"email": ADMIN, "role": "administrator", "scope_type": "global"}).status_code in (401, 403)
    assert claimed.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies=cookies).status_code in (401, 403)
    assert table_snapshot() == before


# ------------------------------------------------------------------ 2. omitted or invalid selection


@pytest.mark.parametrize("portal", [..., None, "", "owner", "ADMINISTRATOR"])
def test_an_omitted_or_invalid_selection_is_refused_without_a_session_or_cloud_call(claimed, backend, portal):
    _cloud_admin("global", enrolled=True)
    _local_admin(password=PASSWORD)
    spy = backend()
    response = _login(claimed, portal, PASSWORD)
    assert response.status_code == 403 and "portal" in response.json()["detail"].lower()
    assert _no_session(response)
    assert spy.calls == []
    assert _admin_sessions() == 0


# ------------------------------------------------------------------ 3. cloud credentials alone (R-2 unchanged)


def test_cloud_credentials_alone_are_still_refused(claimed, backend):
    _cloud_admin("global", enrolled=True)
    _local_admin(email="someone-else@local.test")
    spy = backend()
    response = _login(claimed, "administrator", PASSWORD)
    assert response.status_code == 403 and _no_session(response)
    assert len(spy.calls) == 1  # a cloud-only sign-in is still checked against the cloud, then refused (R-2)
    assert _admin_sessions() == 0


def test_cloud_credentials_alone_during_an_outage_get_503_and_no_session(claimed, backend):
    _cloud_admin("global", enrolled=True)
    backend(unavailable=True)
    response = _login(claimed, "administrator", PASSWORD)
    assert response.status_code == 503 and _no_session(response)


def test_a_local_password_that_differs_is_not_a_local_identity(claimed, backend):
    # Cloud password typed, local account has another password: the local identity did not validate, so this is the
    # cloud-only case and stays refused.
    _cloud_admin("global", enrolled=True)
    _local_admin(password=LOCAL_PASSWORD)
    response = _login(claimed, "administrator", PASSWORD)
    assert response.status_code == 403 and _no_session(response)


# ------------------------------------------------------------------ 4. users.json lockout parity with POST /login


def _stored(user_id="local-owner"):
    return next(user for user in main.load_users() if user["id"] == user_id)


def _sql_lock(email=ADMIN, minutes=10):
    """Seeds the shared SQL sign-in limiter's own lock directly (independent of the users.json lock)."""
    from datetime import datetime, timedelta
    with connection() as db:
        db.execute("INSERT INTO account_lockouts(email,attempts,locked_until,last_attempt_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(email) DO UPDATE SET locked_until=excluded.locked_until",
                   (email, 5, (datetime.now() + timedelta(minutes=minutes)).isoformat(), datetime.now().isoformat()))


def _pending_mfa():
    with connection() as db:
        return db.execute("SELECT COUNT(*) AS n FROM pending_mfa_logins WHERE lower(email)=?", (ADMIN,)).fetchone()["n"]


def test_a_locked_local_account_is_refused_before_any_cloud_call_and_its_lock_is_kept(claimed, backend):
    # Codex R1 re-review: an explicit Administrator attempt on a locked eligible local account is answered 423 after
    # the SQL gate, before delegation, so neither a delegated denial nor an outage can mask the lock.
    from datetime import datetime, timedelta
    until = (datetime.now() + timedelta(minutes=10)).isoformat()
    _cloud_admin("global", enrolled=True)
    _local_admin(password=PASSWORD, locked_until=until, failed_login_attempts=0)
    for unavailable in (False, True):
        spy = backend(unavailable)
        response = _login(claimed, "administrator", PASSWORD)
        assert response.status_code == 423 and _no_session(response)
        assert spy.calls == []
        assert _stored()["locked_until"] == until and _stored()["failed_login_attempts"] == 0
    assert _admin_sessions() == 0 and _pending_mfa() == 0


def test_wrong_local_passwords_lock_the_local_account_as_login_does(claimed, backend, monkeypatch):
    # The users.json threshold on its own: the SQL limiter is held clear so it cannot answer first.
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)
    _local_admin()
    backend()
    for strike in range(1, main.MAX_LOGIN_ATTEMPTS):
        assert _login(claimed, "administrator", "wrong-password-1").status_code in (401, 403)  # no identity: refused
        assert _stored()["failed_login_attempts"] == strike and not _stored()["locked_until"]
    assert _login(claimed, "administrator", "wrong-password-1").status_code in (401, 403)
    assert _stored()["locked_until"] and _stored()["failed_login_attempts"] == 0
    locked = _login(claimed, "administrator", LOCAL_PASSWORD)
    assert locked.status_code == 423 and _no_session(locked)
    # ... and POST /login sees the very same lock.
    login = claimed.post("/login", data={"email": ADMIN, "password": LOCAL_PASSWORD, "next_url": "/"}, follow_redirects=False)
    assert login.status_code == 423


def test_the_sql_limiter_threshold_alone_answers_429_without_touching_the_local_account(claimed, backend):
    # The SQL limiter on its own: it counts this route's refusals and then answers 429 first, with no local strike.
    _local_admin(email="someone-else@local.test")
    _cloud_admin("global", enrolled=True)
    backend()
    import cloud_security
    for _ in range(cloud_security.settings.login_attempt_limit):
        assert _login(claimed, "administrator", "wrong-password-1").status_code in (401, 403)
    blocked = _login(claimed, "administrator", PASSWORD)
    assert blocked.status_code == 429 and _no_session(blocked)
    assert _stored()["failed_login_attempts"] == 0 and not _stored()["locked_until"]


# ------------------------------------------------------------------ 5. Codex R1 re-review: precedence and eligibility


@pytest.mark.parametrize("sql_locked,local_locked,expected", [(True, False, 429), (False, True, 423), (True, True, 429)])
def test_lock_precedence_is_deterministic(claimed, backend, sql_locked, local_locked, expected):
    from datetime import datetime, timedelta
    until = (datetime.now() + timedelta(minutes=10)).isoformat() if local_locked else None
    _local_admin(locked_until=until, failed_login_attempts=0)
    if sql_locked:
        _sql_lock()
    spy = backend()
    before = _stored()
    response = _login(claimed, "administrator", LOCAL_PASSWORD)
    assert response.status_code == expected and _no_session(response)
    assert spy.calls == [] and _admin_sessions() == 0 and _pending_mfa() == 0
    assert _stored() == before  # neither lock nor counter changed by a refused attempt


@pytest.mark.parametrize("portal", [..., None, "", "owner"])
@pytest.mark.parametrize("identities", ["local_only", "dual"])
def test_the_selection_is_refused_before_any_password_check_or_lock_change(claimed, backend, monkeypatch, portal,
                                                                          identities):
    from datetime import datetime, timedelta
    if identities == "dual":
        _cloud_admin("global", enrolled=True)
    _local_admin(password=PASSWORD, failed_login_attempts=2,
                 locked_until=(datetime.now() + timedelta(minutes=10)).isoformat())
    checks = []
    real_verify = main.verify_password
    monkeypatch.setattr(main, "verify_password", lambda *args: checks.append(args) or real_verify(*args))
    spy = backend()
    users_before = main.USERS_FILE.read_bytes()
    response = _login(claimed, portal, PASSWORD)
    assert response.status_code == 403 and "portal selector" in response.json()["detail"].lower()
    assert _no_session(response) and checks == [] and spy.calls == []
    assert main.USERS_FILE.read_bytes() == users_before
    assert _admin_sessions() == 0 and _pending_mfa() == 0


@pytest.mark.parametrize("role", ["viewer", "operator", "customer_owner"])
def test_an_ineligible_local_account_is_never_verified_counted_or_locked_here(claimed, backend, monkeypatch, role):
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)
    _local_admin(role=role)
    backend()
    for _ in range(main.MAX_LOGIN_ATTEMPTS + 1):
        assert _login(claimed, "administrator", "wrong-password-1").status_code in (401, 403)
    assert _stored()["failed_login_attempts"] == 0 and not _stored()["locked_until"]
    # its own correct password still works at POST /login
    login = claimed.post("/login", data={"email": ADMIN, "password": LOCAL_PASSWORD, "next_url": "/"}, follow_redirects=False)
    assert login.status_code == 303


def test_an_ineligible_locked_local_account_is_not_reported_locked_here(claimed, backend):
    from datetime import datetime, timedelta
    until = (datetime.now() + timedelta(minutes=10)).isoformat()
    _local_admin(role="viewer", locked_until=until)
    backend()
    response = _login(claimed, "administrator", LOCAL_PASSWORD)
    assert response.status_code in (401, 403) and _no_session(response)
    assert _stored()["locked_until"] == until


def test_a_wrong_local_password_during_a_delegated_outage_is_counted(claimed, backend):
    _local_admin()
    backend(unavailable=True)
    response = _login(claimed, "administrator", "wrong-password-1")
    assert response.status_code == 503 and _no_session(response)
    assert _stored()["failed_login_attempts"] == 1


def test_partner_and_technician_selections_never_touch_the_local_account(claimed, backend):
    _local_admin()
    backend()
    for portal in ("partner", "technician"):
        assert _no_session(_login(claimed, portal, "wrong-password-1"))
    assert _stored()["failed_login_attempts"] == 0 and not _stored()["locked_until"]


@pytest.mark.parametrize("offset", ["+00:00", "-06:00", "+09:30"])
def test_an_offset_aware_lock_is_honoured_by_both_routes_without_an_error(claimed, backend, offset):
    from datetime import datetime, timedelta, timezone
    zone = timezone(timedelta(hours=int(offset[:3]), minutes=int(offset[0] + offset[4:])))
    _local_admin(locked_until=(datetime.now(zone) + timedelta(minutes=10)).isoformat())
    backend()
    assert _login(claimed, "administrator", LOCAL_PASSWORD).status_code == 423
    login = claimed.post("/login", data={"email": ADMIN, "password": LOCAL_PASSWORD, "next_url": "/"}, follow_redirects=False)
    assert login.status_code == 423
    _local_admin(locked_until=(datetime.now(zone) - timedelta(minutes=1)).isoformat())  # expired: no lock
    response = _login(claimed, "administrator", LOCAL_PASSWORD)
    assert response.status_code == 303 and response.headers["location"] == "/admin-portal"
    assert _stored()["locked_until"] is None and _stored()["failed_login_attempts"] == 0


def test_a_naive_lock_keeps_its_local_wall_clock_meaning():
    from datetime import datetime, timedelta
    assert main._legacy_lock_active((datetime.now() + timedelta(minutes=5)).isoformat()) is not None
    assert main._legacy_lock_active((datetime.now() - timedelta(minutes=5)).isoformat()) is None
    assert main._legacy_lock_time({"locked_until": "not-a-time"}) is None


def test_concurrent_strikes_on_both_routes_are_never_lost(claimed, monkeypatch):
    # The shared helper serializes users.json read-modify-write, so N concurrent strikes leave exactly N.
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(main, "MAX_LOGIN_ATTEMPTS", 1000)
    _local_admin()
    real_load = main.load_users

    def slow_load():
        users = real_load()
        import time
        time.sleep(0.005)  # widen the read-modify-write window an unserialized writer would lose increments in
        return users
    monkeypatch.setattr(main, "load_users", slow_load)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: main._count_legacy_password_failure("local-owner"), range(40)))
    monkeypatch.setattr(main, "load_users", real_load)
    assert _stored()["failed_login_attempts"] == 40


# ------------------------------------------------------------------ 6. Codex R1 revision review P2 (owner 2026-11-06)
# A VALID cloud password, refused by R-2, is not a local password failure: it never uses the local administrator's
# lockout budget. It is counted by the shared SQL limiter instead, so guesses through this route stay bounded, and
# POST /login keeps local emergency recovery.


def _sql_attempts(email=ADMIN):
    with connection() as db:
        row = db.execute("SELECT attempts FROM account_lockouts WHERE email=?", (email,)).fetchone()
    return row["attempts"] if row else 0


def test_a_valid_cloud_password_refused_by_r2_never_uses_the_local_lockout_budget(claimed, backend, tmp_path,
                                                                                    monkeypatch):
    import cloud_security
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit_log.jsonl")
    _cloud_admin("global", enrolled=True)
    _local_admin(password=LOCAL_PASSWORD)
    spy = backend()
    limit = cloud_security.settings.login_attempt_limit
    assert limit >= main.MAX_LOGIN_ATTEMPTS  # counted locally, these refusals would have locked the local account
    for attempt in range(1, limit + 1):
        response = _login(claimed, "administrator", PASSWORD)  # the cloud password: R-2 refusal
        assert response.status_code == 403 and _no_session(response)
        assert "verifiable MFA" in response.json()["detail"]
        assert _sql_attempts() == attempt  # counted once per refusal by the shared limiter
        assert _stored()["failed_login_attempts"] == 0 and not _stored()["locked_until"]
    assert len(spy.calls) == limit
    blocked = _login(claimed, "administrator", PASSWORD)  # the shared limiter now answers first
    assert blocked.status_code == 429 and _no_session(blocked) and len(spy.calls) == limit
    assert _stored()["failed_login_attempts"] == 0 and not _stored()["locked_until"]
    assert _admin_sessions() == 0 and _pending_mfa() == 0
    audit = [json.loads(line) for line in (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum("validated as this email's cloud identity" in entry.get("detail", "") for entry in audit) == limit
    # Local emergency recovery is untouched: POST /login with the local password still signs in.
    login = claimed.post("/login", data={"email": ADMIN, "password": LOCAL_PASSWORD, "next_url": "/"}, follow_redirects=False)
    assert login.status_code == 303


def test_a_password_valid_for_neither_identity_still_counts_a_local_strike(claimed, backend):
    _cloud_admin("global", enrolled=True)
    _local_admin(password=LOCAL_PASSWORD)
    backend()
    response = _login(claimed, "administrator", "wrong-for-both-1")
    assert response.status_code in (401, 403) and _no_session(response)
    assert _stored()["failed_login_attempts"] == 1
    assert _sql_attempts() == 1  # the generic refusal's own SQL count, exactly once


def test_wrong_passwords_for_both_identities_still_lock_the_local_account(claimed, backend, monkeypatch):
    import cloud_security
    monkeypatch.setattr(cloud_security, "record_login_failure", lambda email: None)  # local threshold on its own
    _cloud_admin("global", enrolled=True)
    _local_admin(password=LOCAL_PASSWORD)
    backend()
    for _ in range(main.MAX_LOGIN_ATTEMPTS):
        assert _login(claimed, "administrator", "wrong-for-both-1").status_code in (401, 403)
    assert _stored()["locked_until"] and _stored()["failed_login_attempts"] == 0
    assert _login(claimed, "administrator", LOCAL_PASSWORD).status_code == 423


def test_cloud_password_refusals_then_the_local_password_still_recovers_through_the_selector(claimed, backend):
    # Below the shared limit, a dual-identity user who typed the cloud password can still recover locally here.
    import cloud_security
    _cloud_admin("global", enrolled=True)
    _local_admin(password=LOCAL_PASSWORD)
    spy = backend()
    for _ in range(cloud_security.settings.login_attempt_limit - 1):
        assert _login(claimed, "administrator", PASSWORD).status_code == 403
    calls = len(spy.calls)
    response = _login(claimed, "administrator", LOCAL_PASSWORD)
    assert response.status_code == 303 and response.headers["location"] == "/admin-portal"
    assert len(spy.calls) == calls  # the local shortcut: no cloud call
    assert _stored()["failed_login_attempts"] == 0 and _sql_attempts() == 0  # success clears the shared limiter


def test_a_wrong_local_password_during_an_outage_is_still_counted_locally(claimed, backend):
    # The cloud never validated anything during an outage, so this stays a local password failure.
    _cloud_admin("global", enrolled=True)
    _local_admin(password=LOCAL_PASSWORD)
    backend(unavailable=True)
    response = _login(claimed, "administrator", PASSWORD)
    assert response.status_code == 503 and _no_session(response)
    assert _stored()["failed_login_attempts"] == 1
