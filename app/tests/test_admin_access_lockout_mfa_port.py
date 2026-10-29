"""R-1 PROPOSAL (Blue, 2026-10-29) -- test_admin_access_lockout.py ported to the Windows 1.3.4.1 MFA sign-in.

13415ef refuses administrators at /api/partner-login and requires an MFA-verified global-admin session for
identity-grant operations, so 9 tests in test_admin_access_lockout.py fail before reaching their subject. This
file pins the SAME properties through the paths an administrator may use now:
  /api/portal-login -> MFA challenge (enrolled) or enrollment-only session (not enrolled)
  -> /api/platform-owner/mfa/verify -> MFA-verified session.
It is a proposal for Codex (owner of the correction); it does not edit or replace Codex's file.
"""
import dataclasses
import hashlib
import secrets
from datetime import datetime, timedelta

import pytest

import cloud_features
import cloud_security
from authz_helpers import (cookies_for, enroll_confirmed_mfa, isolated_app, make_partner_user, partner_cookie_from,
                           partner_session_cookie, session_row, totp_now)
from partner_db import connection, verify_password

ADMIN = "owner@anyaicam.test"
PASSWORD = "original-admin-password-1"


class _CapturingEmailService:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
        self.sent.append({"to": to, "text": text})
        return {"id": secrets.token_hex(6), "status": "preview"}


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    cloud_features._password_reset_email_limiter.events.clear()
    cloud_features._password_reset_ip_limiter.events.clear()
    cloud_features._password_reset_complete_ip_limiter.events.clear()
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        yield client


def _admin(password=PASSWORD, must_change=0):
    uid = make_partner_user(ADMIN, "administrator", global_admin=True, password=password)
    if must_change:
        with connection() as db:
            db.execute("UPDATE partner_users SET must_change_password=1 WHERE id=?", (uid,))
    return uid


def _portal_login(client, password=PASSWORD):
    return client.post("/api/portal-login", json={"email": ADMIN, "password": password, "portal": "administrator"})


def _password_accepted(response) -> bool:
    """The password was right: an MFA challenge (200 mfa_required) or an enrollment-only session (303)."""
    if response.status_code == 200:
        return response.json().get("status") == "mfa_required"
    return response.status_code == 303


def _hash(uid):
    with connection() as db:
        return db.execute("SELECT password_hash FROM partner_users WHERE id=?", (uid,)).fetchone()["password_hash"]


def _verified_session(email=ADMIN):
    return cookies_for(partner_session_cookie(email, "administrator", mfa_state="verified", auth_source="mfa"))


def _global_grant_id(uid):
    with connection() as db:
        return db.execute("SELECT id FROM identity_grants WHERE user_id=? AND scope_type='global' AND revoked_at IS NULL", (uid,)).fetchone()["id"]


# ------------------------------------------------------------------ login + lockout (was 3 tests)


def test_admin_portal_login_accepts_the_right_password_and_refuses_a_wrong_one(edge):
    _admin()
    assert _password_accepted(_portal_login(edge))
    assert _portal_login(edge, password="not-the-password-000").status_code in (401, 403)


def test_lockout_blocks_even_the_right_password_but_only_for_a_bounded_window(edge, monkeypatch):
    _admin()
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, login_attempt_limit=3))
    for _ in range(3):
        _portal_login(edge, password="wrong-password-000")
    assert _portal_login(edge).status_code == 429
    with connection() as db:
        db.execute("UPDATE account_lockouts SET locked_until=? WHERE email=?", ((datetime.now() - timedelta(seconds=1)).isoformat(), ADMIN))
    assert _password_accepted(_portal_login(edge))


def test_a_password_reset_recovers_a_locked_out_admin_immediately(edge, monkeypatch):
    uid = _admin()
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, login_attempt_limit=2))
    for _ in range(2):
        _portal_login(edge, password="wrong-password-000")
    assert _portal_login(edge).status_code == 429
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    edge.post("/api/password-reset/request", json={"email": ADMIN})
    assert capturing.sent, "no reset email was sent"
    token = capturing.sent[0]["text"].split("token=")[1].split()[0]
    assert edge.post("/api/password-reset/complete", json={"token": token, "password": "recovered-admin-pass-2"}).status_code == 200
    assert _password_accepted(_portal_login(edge, password="recovered-admin-pass-2"))  # lockout cleared by the reset
    for bad in (token, "abc.def", ""):
        assert edge.post("/api/password-reset/complete", json={"token": bad, "password": "another-password-345"}).status_code == 400
    assert verify_password("recovered-admin-pass-2", _hash(uid))


# ------------------------------------------------------------------ password change (was 3 tests)


def test_a_forced_first_password_change_works_through_mfa_enrollment_without_the_temporary_password(edge):
    uid = _admin(password="temporary-issued-pass-9", must_change=1)
    first = _portal_login(edge, password="temporary-issued-pass-9")
    assert first.status_code == 303, first.text[:200]
    enrollment = partner_cookie_from(first)
    assert session_row(enrollment)["mfa_state"] == "enrollment_only"
    # An enrollment-only session cannot change the password yet.
    assert edge.post("/api/partner/activate-account", json={"password": "my-own-new-password-1", "accept_terms": True},
                     cookies=cookies_for(enrollment)).status_code in (401, 403)
    started = edge.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(enrollment))
    assert started.status_code == 200, started.text[:200]
    secret = started.json()["secret_base32"]
    assert edge.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(secret)}, cookies=cookies_for(enrollment)).status_code == 200
    challenge = _portal_login(edge, password="temporary-issued-pass-9")
    assert challenge.status_code == 200 and challenge.json()["status"] == "mfa_required", challenge.text[:200]
    verified = edge.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": challenge.json()["mfa_pending_token"], "code": totp_now(secret)})
    assert verified.status_code == 303, verified.text[:200]
    token = partner_cookie_from(verified)
    assert session_row(token)["mfa_state"] == "verified"
    changed = edge.post("/api/partner/activate-account", json={"password": "my-own-new-password-1", "accept_terms": True}, cookies=cookies_for(token))
    assert changed.status_code == 200, changed.text[:200]
    assert verify_password("my-own-new-password-1", _hash(uid))


def test_a_voluntary_change_requires_the_current_password(edge):
    uid = _admin()
    cookies = _verified_session()
    before = _hash(uid)
    for attempt in ({}, {"current_password": "not-my-password-00"}):
        refused = edge.post("/api/partner/activate-account", json={"password": "hijacker-password-99", "accept_terms": True, **attempt}, cookies=cookies)
        assert refused.status_code == 403
    assert _hash(uid) == before
    ok = edge.post("/api/partner/activate-account", json={"password": "rotated-admin-pass-77", "current_password": PASSWORD, "accept_terms": True}, cookies=cookies)
    assert ok.status_code == 200 and verify_password("rotated-admin-pass-77", _hash(uid))


def test_the_change_password_page_asks_for_the_current_password_when_not_forced(edge):
    _admin()
    page = edge.get("/change-password", cookies=_verified_session())
    assert page.status_code == 200 and 'id="current-password"' in page.text


# ------------------------------------------------------------------ never permanently locked out (was 2 tests)


def test_the_last_platform_administrator_grant_cannot_be_revoked(edge):
    uid = _admin()
    grant_id = _global_grant_id(uid)
    refused = edge.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies=_verified_session())
    assert refused.status_code == 409 and "last active platform administrator" in refused.json()["detail"]
    make_partner_user("second-admin@anyaicam.test", "administrator", global_admin=True, password=PASSWORD)
    allowed = edge.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies=_verified_session())
    assert allowed.status_code == 200 and allowed.json()["status"] == "revoked"


def test_a_suspended_admin_does_not_count_as_the_remaining_administrator(edge):
    uid = _admin()
    second = make_partner_user("second-admin@anyaicam.test", "administrator", global_admin=True, password=PASSWORD)
    with connection() as db:
        db.execute("UPDATE partner_users SET account_status='suspended' WHERE id=?", (second,))
    assert edge.post(f"/api/operations/identity-grants/{_global_grant_id(uid)}/revoke", cookies=_verified_session()).status_code == 409


# ------------------------------------------------------------------ existing-user compatibility (was 1 test)


def test_an_existing_hash_with_older_parameters_still_logs_in(edge):
    uid = _admin()
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 100000)
    with connection() as db:
        db.execute("UPDATE partner_users SET password_hash=? WHERE id=?", (f"pbkdf2_sha256$100000${salt.hex()}${digest.hex()}", uid))
    assert _password_accepted(_portal_login(edge))
