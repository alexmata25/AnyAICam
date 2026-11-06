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


def test_a_locked_local_account_is_refused_and_its_lock_is_kept(claimed, backend):
    from datetime import datetime, timedelta
    until = (datetime.now() + timedelta(minutes=10)).isoformat()
    _local_admin(locked_until=until, failed_login_attempts=0)
    spy = backend()
    response = _login(claimed, "administrator", LOCAL_PASSWORD)
    assert response.status_code == 423 and _no_session(response)
    stored = next(user for user in main.load_users() if user["id"] == "local-owner")
    assert stored["locked_until"] == until
    assert len(spy.calls) == 1  # the cloud identity (absent here) was still checked; no local session either way


def test_wrong_local_passwords_lock_the_local_account_as_login_does(claimed, backend):
    _local_admin()
    backend()
    for _ in range(main.MAX_LOGIN_ATTEMPTS):
        assert _login(claimed, "administrator", "wrong-password-1").status_code in (401, 403)
    stored = next(user for user in main.load_users() if user["id"] == "local-owner")
    assert stored["locked_until"]
    locked = _login(claimed, "administrator", LOCAL_PASSWORD)
    # 423 from the local lock, or 429 when the shared SQL login limiter (same failures) answers first; never a session.
    assert locked.status_code in (423, 429) and _no_session(locked)
    # ... and POST /login sees the very same lock.
    login = claimed.post("/login", data={"email": ADMIN, "password": LOCAL_PASSWORD, "next_url": "/"}, follow_redirects=False)
    assert login.status_code == 423
