"""Regression coverage for the Windows edge Admin Portal bridge MFA bypass."""
from __future__ import annotations

import pytest

import main
import partner_portal
from authz_helpers import (
    cookies_for,
    enroll_confirmed_mfa,
    isolated_app,
    make_partner_user,
    partner_cookie_from,
    partner_session_cookie,
    table_snapshot,
    totp_now,
)
from partner_db import connection

PASSWORD = "correct-horse-battery-1"
ADMIN_EMAIL = "edge-admin@example.test"
OPS_READ_PATHS = (
    "/api/operations/identity-grants",
    "/api/operations/diagnostics",
    "/api/operations/deployment",
    "/operations/identity-grants",
    "/admin-portal",
)


@pytest.fixture(params=[False, True], ids=["edge", "cloud"])
def app_client(request, tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=bool(request.param)) as client:
        yield client


def _seed_global_admin(email=ADMIN_EMAIL, *, factor=False):
    user_id = make_partner_user(email, "administrator", password=PASSWORD, global_admin=True)
    if factor:
        enroll_confirmed_mfa(user_id)
    return user_id


def _assert_no_admin_access(client, cookies):
    before = table_snapshot()
    responses = [client.get(path, cookies=cookies) for path in OPS_READ_PATHS]
    def denied(response):
        if response.status_code in (401, 403):
            return True
        return (response.status_code in (302, 303, 307, 308)
                and not response.headers.get("location", "").startswith(("/admin", "/operations", "/api/admin")))

    assert all(denied(response) for response in responses), [
        (path, response.status_code, response.text[:120]) for path, response in zip(OPS_READ_PATHS, responses)
    ]
    mutation = client.post(
        "/api/operations/identity-grants",
        cookies=cookies,
        json={"email": "attacker-created@example.test", "role": "administrator", "scope_type": "global"},
    )
    assert mutation.status_code in (401, 403)
    assert table_snapshot() == before


@pytest.mark.parametrize("factor_enrolled", [False, True], ids=["no-factor", "factor-not-verified"])
def test_partner_login_refuses_administrator_on_every_profile(app_client, factor_enrolled):
    client = app_client
    _seed_global_admin(factor=factor_enrolled)
    before = table_snapshot()

    response = client.post("/api/partner-login", json={"email": ADMIN_EMAIL, "password": PASSWORD})

    assert response.status_code == 403, response.text
    assert partner_cookie_from(response) is None
    assert table_snapshot() == before
    assert client.get("/api/operations/identity-grants", cookies=response.cookies).status_code in (401, 403)


def test_pre_upgrade_password_session_without_mfa_is_denied(app_client):
    client = app_client
    _seed_global_admin()
    token = partner_session_cookie(ADMIN_EMAIL, "administrator", mfa_state=None, auth_source="password")
    _assert_no_admin_access(client, cookies_for(token))


def test_pre_upgrade_cloud_delegated_session_without_mfa_is_denied(app_client):
    client = app_client
    _seed_global_admin()
    token = partner_session_cookie(ADMIN_EMAIL, "administrator", mfa_state=None, auth_source="cloud_assertion")
    _assert_no_admin_access(client, cookies_for(token))


def test_global_grant_revocation_denies_existing_verified_session(app_client):
    client = app_client
    user_id = _seed_global_admin()
    token = partner_session_cookie(ADMIN_EMAIL, "administrator", mfa_state="verified", auth_source="mfa")
    with connection() as db:
        db.execute("UPDATE identity_grants SET revoked_at='2026-10-28T00:00:00' WHERE user_id=? AND scope_type='global'", (user_id,))
    _assert_no_admin_access(client, cookies_for(token))


def test_stale_authorization_version_and_revoked_session_are_denied(app_client):
    client = app_client
    user_id = _seed_global_admin()
    stale = partner_session_cookie(ADMIN_EMAIL, "administrator", mfa_state="verified", auth_source="mfa")
    with connection() as db:
        db.execute("UPDATE user_sessions SET authorization_version_at_login=-1 WHERE user_id=?", (user_id,))
    _assert_no_admin_access(client, cookies_for(stale))

    fresh = partner_session_cookie(ADMIN_EMAIL, "administrator", mfa_state="verified", auth_source="mfa")
    with connection() as db:
        db.execute("UPDATE user_sessions SET revoked_at='2026-10-28T00:00:00' WHERE user_id=? AND revoked_at IS NULL", (user_id,))
    _assert_no_admin_access(client, cookies_for(fresh))


def test_cross_account_session_binding_cannot_borrow_another_email_grant(app_client):
    client = app_client
    account_a = "a@example.test"
    account_b = "b@example.test"
    user_a = make_partner_user(account_a, "administrator", partner_id=None, global_admin=True)
    user_b = make_partner_user(account_b, "administrator", partner_id=None, global_admin=False)
    token_b = partner_session_cookie(account_b, "administrator", mfa_state="verified", auth_source="mfa")
    session_id = partner_portal._decode(token_b)["session_id"]
    with connection() as db:
        # Simulate a stale/malformed pre-upgrade binding: the signed cookie and
        # session email identify A, while the authoritative user_id names B.
        db.execute("UPDATE user_sessions SET email=? WHERE id=?", (account_a, session_id))
    borrowed = partner_portal._token(account_a, "administrator", None, None, session_id)
    assert user_a != user_b
    _assert_no_admin_access(client, cookies_for(borrowed))


def test_emergency_login_remains_narrow_and_cannot_manage_identity_grants(edge_client):
    client = edge_client
    main.save_users([{
        "id": "local-emergency-admin", "email": "admin@local", "role": "administrator", "enabled": True,
        "password_hash": main.hash_password(PASSWORD), "site_ids": ["home"], "camera_ids": [],
    }])
    login = client.post("/login", data={"email": "admin@local", "password": PASSWORD})
    assert login.status_code == 303
    assert client.get("/admin-portal", cookies=login.cookies).status_code == 200
    before = table_snapshot()
    assert client.get("/api/operations/identity-grants", cookies=login.cookies).status_code in (401, 403)
    assert client.post("/api/operations/identity-grants", cookies=login.cookies,
                       json={"email": "attacker-created@example.test", "role": "administrator", "scope_type": "global"}).status_code in (401, 403)
    assert table_snapshot() == before


@pytest.fixture()
def edge_client(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        yield client


def test_verified_global_admin_mfa_flow_still_opens_admin_portal_and_grants_api(edge_client):
    client = edge_client
    user_id = _seed_global_admin(factor=True)
    login = client.post("/api/portal-login", json={"email": ADMIN_EMAIL, "password": PASSWORD, "portal": "administrator"})
    assert login.status_code == 200 and login.json()["status"] == "mfa_required", login.text
    verified = client.post("/api/platform-owner/mfa/verify", json={
        "mfa_pending_token": login.json()["mfa_pending_token"],
        "code": totp_now(_mfa_secret(user_id)),
    })
    assert verified.status_code == 303, verified.text
    assert partner_cookie_from(verified)
    assert client.get("/admin-portal", cookies=verified.cookies).status_code == 200
    assert client.get("/api/operations/identity-grants", cookies=verified.cookies).status_code == 200


def _mfa_secret(user_id):
    with connection() as db:
        return db.execute("SELECT secret_base32 FROM platform_owner_mfa WHERE user_id=?", (user_id,)).fetchone()["secret_base32"]
