"""Replacing a confirmed platform-owner authenticator (2026-10-02).

Re-enrolling used to overwrite the confirmed TOTP secret and clear
confirmed_at, so whoever held an admin session could switch MFA off by
starting an enrollment and abandoning it. Now a replacement needs a current
code from the confirmed factor; the new secret is pending until confirmed,
and the confirmed factor stays in force meanwhile.
"""
import time

import platform_owner
import partner_portal
from partner_db import connection
from test_platform_owner_rbac import (  # noqa: F401 -- fixtures and helpers
    _admin_session,
    _enroll_and_confirm_mfa,
    _grant_global,
    _lookup_user_id,
    _seed_operator,
    http_client,
)

EMAIL = "amata@anyaicam.com"


def _owner(client, db_path):
    _seed_operator(db_path)
    _grant_global(db_path, _admin_session(), client, email=EMAIL)
    cookie = partner_portal._token(EMAIL, "administrator", None, None, None)
    client.cookies.set(partner_portal.SESSION_COOKIE, cookie)
    return cookie


def _code(secret, step_offset=0):
    return platform_owner._totp_code_at(secret, int(time.time() // 30) + step_offset)


def _state():
    with connection() as db:
        user_id = _lookup_user_id(db, EMAIL)
        row = db.execute("SELECT secret_base32,confirmed_at,pending_secret_base32 FROM platform_owner_mfa WHERE user_id=?", (user_id,)).fetchone()
        return dict(row), platform_owner.mfa_is_confirmed(db, user_id=user_id)


def test_an_abandoned_replacement_never_switches_mfa_off(http_client):
    client, db_path = http_client
    cookie = _owner(client, db_path)
    old_secret, _ = _enroll_and_confirm_mfa(client, db_path, cookie)
    started = client.post("/api/platform-owner/mfa/enroll", json={"current_code": _code(old_secret)})
    assert started.status_code == 200
    row, confirmed = _state()
    assert confirmed and row["secret_base32"] == old_secret and row["pending_secret_base32"] == started.json()["secret_base32"]
    # abandoned: the old authenticator still guards the account
    assert platform_owner.verify_totp_code(row["secret_base32"], _code(old_secret))


def test_replacement_requires_a_code_from_the_current_factor(http_client):
    client, db_path = http_client
    cookie = _owner(client, db_path)
    old_secret, _ = _enroll_and_confirm_mfa(client, db_path, cookie)
    assert client.post("/api/platform-owner/mfa/enroll").status_code == 403
    assert client.post("/api/platform-owner/mfa/enroll", json={"current_code": "000000"}).status_code == 403
    row, confirmed = _state()
    assert confirmed and row["secret_base32"] == old_secret and row["pending_secret_base32"] is None


def test_a_confirmed_replacement_takes_over_and_the_old_factor_stops_working(http_client):
    client, db_path = http_client
    cookie = _owner(client, db_path)
    old_secret, _ = _enroll_and_confirm_mfa(client, db_path, cookie)
    new_secret = client.post("/api/platform-owner/mfa/enroll", json={"current_code": _code(old_secret)}).json()["secret_base32"]
    assert client.post("/api/platform-owner/mfa/confirm", json={"code": "000000"}).status_code == 400
    assert _state()[0]["secret_base32"] == old_secret  # a wrong code changes nothing
    done = client.post("/api/platform-owner/mfa/confirm", json={"code": _code(new_secret)})
    assert done.status_code == 200
    row, confirmed = _state()
    assert confirmed and row["secret_base32"] == new_secret and row["pending_secret_base32"] is None


def test_confirm_without_a_pending_replacement_is_refused_once_confirmed(http_client):
    client, db_path = http_client
    cookie = _owner(client, db_path)
    old_secret, _ = _enroll_and_confirm_mfa(client, db_path, cookie)
    assert client.post("/api/platform-owner/mfa/confirm", json={"code": _code(old_secret)}).status_code == 400


def test_first_enrollment_is_unchanged(http_client):
    client, db_path = http_client
    cookie = _owner(client, db_path)
    first = client.post("/api/platform-owner/mfa/enroll")
    assert first.status_code == 200
    second = client.post("/api/platform-owner/mfa/enroll")  # restarting an unconfirmed enrollment needs no code
    assert second.status_code == 200
    assert client.post("/api/platform-owner/mfa/confirm", json={"code": _code(second.json()["secret_base32"])}).status_code == 200
