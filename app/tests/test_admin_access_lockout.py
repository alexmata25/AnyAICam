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


def test_admin_login_success_and_wrong_password_failure(client, db_path):
    _seed_admin(db_path)
    assert _login(client).status_code in (200, 303)
    wrong = _login(client, password="not-the-password-000")
    assert wrong.status_code == 403 and "incorrect" in wrong.json()["detail"]


def test_lockout_blocks_even_the_right_password_but_only_for_a_bounded_window(client, db_path, monkeypatch):
    _seed_admin(db_path)
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, login_attempt_limit=3))
    for _ in range(3):
        _login(client, password="wrong-password-000")
    assert _login(client).status_code == 429  # locked: correct password refused too
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:  # the window elapses
            db.execute("UPDATE account_lockouts SET locked_until=? WHERE email=?", ((datetime.now() - timedelta(seconds=1)).isoformat(), ADMIN_EMAIL))
    assert _login(client).status_code in (200, 303)


def test_a_password_reset_recovers_a_locked_out_admin_immediately(client, db_path, monkeypatch):
    _seed_admin(db_path)
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, login_attempt_limit=2))
    for _ in range(2):
        _login(client, password="wrong-password-000")
    assert _login(client).status_code == 429
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    client.post("/api/password-reset/request", json={"email": ADMIN_EMAIL})
    token = capturing.sent[0]["text"].split("token=")[1].split()[0]
    assert client.post("/api/password-reset/complete", json={"token": token, "password": "recovered-admin-pass-2"}).status_code == 200
    assert _login(client, password="recovered-admin-pass-2").status_code in (200, 303)  # lockout cleared by the reset
    # A reset token is single-use; an unknown one is refused the same way.
    for bad in (token, "abc.def", ""):
        refused = client.post("/api/password-reset/complete", json={"token": bad, "password": "another-password-345"})
        assert refused.status_code == 400
    assert verify_password("recovered-admin-pass-2", _stored_hash(db_path))


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


def test_a_forced_first_password_change_does_not_need_the_temporary_password(client, db_path):
    _seed_admin(db_path, password="temporary-issued-pass-9", must_change=1)
    _login(client, password="temporary-issued-pass-9")
    response = client.post("/api/partner/activate-account", json={"password": "my-own-new-password-1", "accept_terms": True})
    assert response.status_code == 200
    assert verify_password("my-own-new-password-1", _stored_hash(db_path))


def test_a_voluntary_change_requires_the_current_password(client, db_path):
    _seed_admin(db_path)
    _login(client)
    before = _stored_hash(db_path)
    for attempt in ({}, {"current_password": "not-my-password-00"}):
        refused = client.post("/api/partner/activate-account", json={"password": "hijacker-password-99", "accept_terms": True, **attempt})
        assert refused.status_code == 403
    assert _stored_hash(db_path) == before  # a signed-in session alone cannot take the account over
    ok = client.post("/api/partner/activate-account", json={"password": "rotated-admin-pass-77", "current_password": ADMIN_PASSWORD, "accept_terms": True})
    assert ok.status_code == 200 and verify_password("rotated-admin-pass-77", _stored_hash(db_path))
    text = _all_audit_text(db_path)
    assert "password.change_denied" in text
    for secret in (ADMIN_PASSWORD, "hijacker-password-99", "not-my-password-00", "rotated-admin-pass-77"):
        assert secret not in text


def test_the_change_password_page_asks_for_the_current_password_only_when_not_forced(client, db_path):
    _seed_admin(db_path)
    _login(client)
    assert 'id="current-password"' in client.get("/change-password").text


# ------------------------------------------------------------------ never permanently locked out


def _local_admin_session(role="administrator"):
    main.save_users([{"id": "admin-1", "email": "admin@example.test", "role": role, "enabled": True, "camera_ids": []}])
    return main.create_session("admin-1")


def test_the_last_platform_administrator_grant_cannot_be_revoked(client, db_path):
    grant_id = _seed_admin(db_path)
    token = _local_admin_session()
    refused = client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: token})
    assert refused.status_code == 409 and "last active platform administrator" in refused.json()["detail"]
    _seed_admin(db_path, user_id="u-second", email="second-admin@anyaicam.test")
    allowed = client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: token})
    assert allowed.status_code == 200 and allowed.json()["status"] == "revoked"


def test_a_suspended_admin_does_not_count_as_the_remaining_administrator(client, db_path):
    grant_id = _seed_admin(db_path)
    _seed_admin(db_path, user_id="u-second", email="second-admin@anyaicam.test")
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("UPDATE partner_users SET account_status='suspended' WHERE id='u-second'")
    token = _local_admin_session()
    assert client.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies={main.SESSION_COOKIE_NAME: token}).status_code == 409


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


def test_an_existing_hash_with_older_parameters_still_logs_in(client, db_path):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", ADMIN_PASSWORD.encode(), salt, 100000)
    _seed_admin(db_path, encoded=f"pbkdf2_sha256$100000${salt.hex()}${digest.hex()}")
    assert _login(client).status_code in (200, 303)


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
