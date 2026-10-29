"""Admin access: login, lockout, password change, reset, recovery and the
"never permanently locked out" guarantees (2026-09-25).

Complements test_password_reset_lifecycle.py (reset expiry, enumeration,
login after reset) rather than repeating it. New defects this file pins:

- The last live global administrator grant could be revoked through
  /api/operations/identity-grants/{id}/revoke -- nobody could then reach
  the Admin Portal, and break-glass recovery deliberately only restores an
  EXISTING global grant, so only database surgery could undo it.
- The appliance's local user API could disable, demote or delete the last
  enabled user able to manage users (local-admin only had its own
  disable/delete checks, not a role-change or last-manager check).
- /api/partner/activate-account replaced the password of ANY signed-in
  account without the current password -- a stolen session cookie was
  enough to take an account over. Now only a forced first-password change
  (must_change_password=1) may skip it.
"""
import dataclasses
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import cloud_features
import cloud_security
import main
import platform_owner
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash, verify_password

ADMIN_EMAIL = "owner@anyaicam.test"
ADMIN_PASSWORD = "original-admin-password-1"


@pytest.fixture(autouse=True)
def _isolated_recordings(tmp_path, monkeypatch):
    # Codex note on 108a132: never read or write the shared recordings root (C:\app\recordings) -- every
    # recordings-derived path (audit log, alerts, AAC faces/events, identity file, email previews...) goes to tmp_path.
    from recordings_isolation import isolate_recordings
    return isolate_recordings(monkeypatch, tmp_path)


def test_recordings_derived_paths_are_isolated_for_this_module(_isolated_recordings):
    from recordings_isolation import assert_isolated
    assert_isolated(_isolated_recordings)


class _CapturingEmailService:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.sent.append({"to": to, "text": text})
        return {"id": secrets.token_hex(6), "status": "preview"}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_admin_access_lockout.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    cloud_features._password_reset_email_limiter.events.clear()
    cloud_features._password_reset_ip_limiter.events.clear()
    cloud_features._password_reset_complete_ip_limiter.events.clear()
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _seed_admin(db_path, *, user_id="u-owner", email=ADMIN_EMAIL, password=ADMIN_PASSWORD, must_change=0, grant=True, encoded=None):
    now = "2026-09-01T00:00:00"
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('anyaicam-primary','AnyAiCam','approved','real',?)", (now,))
            db.execute(
                "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at,must_change_password,account_status) "
                "VALUES(?,?,?,?,?,?,1,?,?,'active')",
                (user_id, "anyaicam-primary", email, "Owner", "administrator", encoded or password_hash(password), now, must_change),
            )
            if grant:
                from appliance_identity import create_grant
                return create_grant(db, user_id=user_id, role="administrator", scope_type="global", scope_id=None, granted_by="test", now=now)
    return None


def _login(client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
    return client.post("/api/partner-login", json={"email": email, "password": password}, follow_redirects=False)


def _stored_hash(db_path, user_id="u-owner"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT password_hash FROM partner_users WHERE id=?", (user_id,)).fetchone()["password_hash"]


def _all_audit_text(db_path):
    conn = sqlite3.connect(db_path)
    text = " ".join(" ".join(str(v) for v in row) for row in conn.execute("SELECT * FROM audit_logs").fetchall())
    conn.close()
    return text


# ------------------------------------------------------------------ login + lockout


def test_break_glass_recovers_the_owner_even_while_the_account_is_locked_out(client, db_path, monkeypatch):
    _seed_admin(db_path)
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, login_attempt_limit=2))
    for _ in range(2):
        _login(client, password="wrong-password-000")
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            token = platform_owner.create_break_glass_token(db, email=ADMIN_EMAIL, reason="locked out", created_by="host-operator")
    redeemed = client.post("/api/platform-owner/break-glass-recover", json={"email": ADMIN_EMAIL, "token": token}, follow_redirects=False)
    assert redeemed.status_code == 303
    assert token not in _all_audit_text(db_path)


# ------------------------------------------------------------------ password change


# ------------------------------------------------------------------ never permanently locked out


def _local_admin_session(role="administrator"):
    # The acting Admin Portal session is the platform owner itself (a live
    # global administrator, 2026-10-02 -- identity grants require one).
    main.save_users([{"id": "admin-1", "email": ADMIN_EMAIL, "role": role, "enabled": True, "camera_ids": []}])
    return main.create_session("admin-1")


def _manager_and_plain_roles():
    managers = sorted(role for role, perms in main.ROLE_PERMISSIONS.items() if "manage_users" in perms)
    plain = sorted(role for role, perms in main.ROLE_PERMISSIONS.items() if "manage_users" not in perms)
    return managers, plain


def test_removes_last_user_manager_logic():
    managers, plain = _manager_and_plain_roles()
    manager, other = managers[0], plain[0]
    only = [{"id": "a", "role": manager, "enabled": True}, {"id": "b", "role": other, "enabled": True}]
    assert main.removes_last_user_manager(only, only[0], {"enabled": False})
    assert main.removes_last_user_manager(only, only[0], {"role": other})
    assert not main.removes_last_user_manager(only, only[0], {"display_name": "x"})
    assert not main.removes_last_user_manager(only, only[1], {"enabled": False})  # not a manager
    two = only + [{"id": "c", "role": manager, "enabled": True}]
    assert not main.removes_last_user_manager(two, two[0], {"enabled": False})
    disabled_backup = only + [{"id": "c", "role": manager, "enabled": False}]
    assert main.removes_last_user_manager(disabled_backup, disabled_backup[0], {"enabled": False})


def test_the_local_user_api_refuses_to_remove_the_last_user_manager(client):
    managers, plain = _manager_and_plain_roles()
    main.save_users([
        {"id": "admin-1", "email": "admin@example.test", "role": managers[0], "enabled": True, "camera_ids": []},
        {"id": "viewer-1", "email": "viewer@example.test", "role": plain[0], "enabled": True, "camera_ids": []},
    ])
    token = main.create_session("admin-1")
    cookies = {main.SESSION_COOKIE_NAME: token}
    for change in ({"enabled": False}, {"role": plain[0]}):
        response = client.put("/api/users/admin-1", json=change, cookies=cookies)
        assert response.json()["message"] == main.LAST_USER_MANAGER_MESSAGE, change
    assert client.delete("/api/users/admin-1", cookies=cookies).json()["message"] == main.LAST_USER_MANAGER_MESSAGE
    stored = next(user for user in main.load_users() if user["id"] == "admin-1")
    assert stored["enabled"] is True and stored["role"] == managers[0]


# ------------------------------------------------------------------ existing-user compatibility


def test_a_reset_link_issued_in_the_older_bare_token_format_still_works(client, db_path):
    _seed_admin(db_path)
    raw = secrets.token_urlsafe(32)
    now = datetime.now()
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute(
                "INSERT INTO password_reset_tokens(id,user_id,email,token_hash,expires_at,used_at,created_at) VALUES(?,?,?,?,?,NULL,?)",
                ("legacy-1", "u-owner", ADMIN_EMAIL, password_hash(raw), (now + timedelta(minutes=30)).isoformat(), now.isoformat()),
            )
    assert client.post("/api/password-reset/complete", json={"token": raw, "password": "legacy-link-password-1"}).status_code == 200
    assert raw not in _all_audit_text(db_path)


# ------------------------------------------------------------------ Windows 1.3.4.1 (13415ef): administrator sign-in is MFA-only
# 13415ef refuses administrators at /api/partner-login on every profile and requires an MFA-verified global-admin
# session for identity grants. The 9 cases below replace the earlier partner-login / legacy-session versions of the
# same properties (R-1, Blue 910a1db, Codex PASS-WITH-NOTES); the 4 cases above are unchanged.
from authz_helpers import (cookies_for, enroll_confirmed_mfa, isolated_app, make_partner_user, partner_cookie_from,  # noqa: E402
                           partner_session_cookie, session_row, totp_now)

MFA_ADMIN = "owner@anyaicam.test"
MFA_PASSWORD = "original-admin-password-1"


@pytest.fixture()
def mfa_edge(tmp_path, monkeypatch):
    cloud_features._password_reset_email_limiter.events.clear()
    cloud_features._password_reset_ip_limiter.events.clear()
    cloud_features._password_reset_complete_ip_limiter.events.clear()
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        yield client


def _mfa_admin(password=MFA_PASSWORD, must_change=0):
    uid = make_partner_user(MFA_ADMIN, "administrator", global_admin=True, password=password)
    if must_change:
        with connection() as db:
            db.execute("UPDATE partner_users SET must_change_password=1 WHERE id=?", (uid,))
    return uid


def _mfa_portal_login(client, password=MFA_PASSWORD):
    return client.post("/api/portal-login", json={"email": MFA_ADMIN, "password": password, "portal": "administrator"})


def _mfa_password_accepted(response) -> bool:
    """The password was right: an MFA challenge (200 mfa_required) or an enrollment-only session (303)."""
    if response.status_code == 200:
        return response.json().get("status") == "mfa_required"
    return response.status_code == 303


def _mfa_hash(uid):
    with connection() as db:
        return db.execute("SELECT password_hash FROM partner_users WHERE id=?", (uid,)).fetchone()["password_hash"]


def _mfa_verified_session(email=MFA_ADMIN):
    return cookies_for(partner_session_cookie(email, "administrator", mfa_state="verified", auth_source="mfa"))


def _mfa_global_grant_id(uid):
    with connection() as db:
        return db.execute("SELECT id FROM identity_grants WHERE user_id=? AND scope_type='global' AND revoked_at IS NULL", (uid,)).fetchone()["id"]


# ------------------------------------------------------------------ login + lockout (was 3 tests)


def test_admin_portal_login_accepts_the_right_password_and_refuses_a_wrong_one(mfa_edge):
    _mfa_admin()
    assert _mfa_password_accepted(_mfa_portal_login(mfa_edge))
    assert _mfa_portal_login(mfa_edge, password="not-the-password-000").status_code in (401, 403)


def test_lockout_blocks_even_the_right_password_but_only_for_a_bounded_window(mfa_edge, monkeypatch):
    _mfa_admin()
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, login_attempt_limit=3))
    for _ in range(3):
        _mfa_portal_login(mfa_edge, password="wrong-password-000")
    assert _mfa_portal_login(mfa_edge).status_code == 429
    with connection() as db:
        db.execute("UPDATE account_lockouts SET locked_until=? WHERE email=?", ((datetime.now() - timedelta(seconds=1)).isoformat(), MFA_ADMIN))
    assert _mfa_password_accepted(_mfa_portal_login(mfa_edge))


def test_a_password_reset_recovers_a_locked_out_admin_immediately(mfa_edge, monkeypatch):
    uid = _mfa_admin()
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, login_attempt_limit=2))
    for _ in range(2):
        _mfa_portal_login(mfa_edge, password="wrong-password-000")
    assert _mfa_portal_login(mfa_edge).status_code == 429
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    mfa_edge.post("/api/password-reset/request", json={"email": MFA_ADMIN})
    assert capturing.sent, "no reset email was sent"
    token = capturing.sent[0]["text"].split("token=")[1].split()[0]
    assert mfa_edge.post("/api/password-reset/complete", json={"token": token, "password": "recovered-admin-pass-2"}).status_code == 200
    assert _mfa_password_accepted(_mfa_portal_login(mfa_edge, password="recovered-admin-pass-2"))  # lockout cleared by the reset
    for bad in (token, "abc.def", ""):
        assert mfa_edge.post("/api/password-reset/complete", json={"token": bad, "password": "another-password-345"}).status_code == 400
    assert verify_password("recovered-admin-pass-2", _mfa_hash(uid))


# ------------------------------------------------------------------ password change (was 3 tests)


def test_a_forced_first_password_change_works_through_mfa_enrollment_without_the_temporary_password(mfa_edge):
    uid = _mfa_admin(password="temporary-issued-pass-9", must_change=1)
    first = _mfa_portal_login(mfa_edge, password="temporary-issued-pass-9")
    assert first.status_code == 303, first.text[:200]
    enrollment = partner_cookie_from(first)
    assert session_row(enrollment)["mfa_state"] == "enrollment_only"
    # An enrollment-only session cannot change the password yet.
    assert mfa_edge.post("/api/partner/activate-account", json={"password": "my-own-new-password-1", "accept_terms": True},
                     cookies=cookies_for(enrollment)).status_code in (401, 403)
    started = mfa_edge.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(enrollment))
    assert started.status_code == 200, started.text[:200]
    secret = started.json()["secret_base32"]
    assert mfa_edge.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(secret)}, cookies=cookies_for(enrollment)).status_code == 200
    challenge = _mfa_portal_login(mfa_edge, password="temporary-issued-pass-9")
    assert challenge.status_code == 200 and challenge.json()["status"] == "mfa_required", challenge.text[:200]
    verified = mfa_edge.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": challenge.json()["mfa_pending_token"], "code": totp_now(secret)})
    assert verified.status_code == 303, verified.text[:200]
    token = partner_cookie_from(verified)
    assert session_row(token)["mfa_state"] == "verified"
    changed = mfa_edge.post("/api/partner/activate-account", json={"password": "my-own-new-password-1", "accept_terms": True}, cookies=cookies_for(token))
    assert changed.status_code == 200, changed.text[:200]
    assert verify_password("my-own-new-password-1", _mfa_hash(uid))


def test_a_voluntary_change_requires_the_current_password(mfa_edge):
    uid = _mfa_admin()
    cookies = _mfa_verified_session()
    before = _mfa_hash(uid)
    for attempt in ({}, {"current_password": "not-my-password-00"}):
        refused = mfa_edge.post("/api/partner/activate-account", json={"password": "hijacker-password-99", "accept_terms": True, **attempt}, cookies=cookies)
        assert refused.status_code == 403
    assert _mfa_hash(uid) == before
    ok = mfa_edge.post("/api/partner/activate-account", json={"password": "rotated-admin-pass-77", "current_password": MFA_PASSWORD, "accept_terms": True}, cookies=cookies)
    assert ok.status_code == 200 and verify_password("rotated-admin-pass-77", _mfa_hash(uid))


def test_the_change_password_page_asks_for_the_current_password_when_not_forced(mfa_edge):
    _mfa_admin()
    page = mfa_edge.get("/change-password", cookies=_mfa_verified_session())
    assert page.status_code == 200 and 'id="current-password"' in page.text


# ------------------------------------------------------------------ never permanently locked out (was 2 tests)


def test_the_last_platform_administrator_grant_cannot_be_revoked(mfa_edge):
    uid = _mfa_admin()
    grant_id = _mfa_global_grant_id(uid)
    refused = mfa_edge.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies=_mfa_verified_session())
    assert refused.status_code == 409 and "last active platform administrator" in refused.json()["detail"]
    make_partner_user("second-admin@anyaicam.test", "administrator", global_admin=True, password=MFA_PASSWORD)
    allowed = mfa_edge.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies=_mfa_verified_session())
    assert allowed.status_code == 200 and allowed.json()["status"] == "revoked"


def test_a_suspended_admin_does_not_count_as_the_remaining_administrator(mfa_edge):
    uid = _mfa_admin()
    second = make_partner_user("second-admin@anyaicam.test", "administrator", global_admin=True, password=MFA_PASSWORD)
    with connection() as db:
        db.execute("UPDATE partner_users SET account_status='suspended' WHERE id=?", (second,))
    assert mfa_edge.post(f"/api/operations/identity-grants/{_mfa_global_grant_id(uid)}/revoke", cookies=_mfa_verified_session()).status_code == 409


# ------------------------------------------------------------------ existing-user compatibility (was 1 test)


def test_an_existing_hash_with_older_parameters_still_logs_in(mfa_edge):
    uid = _mfa_admin()
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", MFA_PASSWORD.encode(), salt, 100000)
    with connection() as db:
        db.execute("UPDATE partner_users SET password_hash=? WHERE id=?", (f"pbkdf2_sha256$100000${salt.hex()}${digest.hex()}", uid))
    assert _mfa_password_accepted(_mfa_portal_login(mfa_edge))
