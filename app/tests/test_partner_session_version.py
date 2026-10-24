"""D-AUTHZ-1 residual (Linux 1.2.7.1): a Partner Portal session is bound to the
account's authorization_version, role and status at sign-in, and an
administrator route also needs a LIVE global administrator grant on the cloud.

Green IDs: U-A1-1 (version/role/status re-check), U-A1-2 (every admin route
with a VALID body -> 401/403, no DB change, no audit except a denial), Z8d'
(pre-revoke session AND fresh sign-in after the revoke, camera-quota with a
valid body), Z8f (route matrix), Z8g (role change / password reset).
Codex b8c7a7ed D-AUTHZ-1 residual; coordinator evidence 2026-10-24 (a fresh
partner-login after the revoke still reached /api/admin/*).
"""
import json

import pytest

import appliance_identity
import main
import partner_portal
from authz_helpers import (
    admin_route_matrix, audit_actions, cookies_for, current_version, isolated_app, make_partner_user,
    max_audit_id, partner_cookie_from, partner_session_cookie, refused, session_row, table_snapshot,
)
from partner_db import connection

DENIAL_ACTIONS = {"session.invalidated", "login.denied", "access.denied", "login.failed", "login.blocked"}
PASSWORD = "correct-horse-battery-1"


@pytest.fixture(params=["cloud", "edge"])
def profile(request):
    return request.param


@pytest.fixture()
def client(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=(profile == "cloud")) as test_client:
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','A','approved','real','2026-01-01')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) "
                       "VALUES('cust-a','partner-a','Customer A','a@customers.test','active','real','2026-01-01')")
        # Another live platform administrator, so revoking the one under test is
        # never "the last administrator".
        make_partner_user("backup-admin@anyaicam.test", "administrator", global_admin=True)
        yield test_client


def _entitlements():
    with connection() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM customer_entitlements ORDER BY id").fetchall()]


def _quota_audits():
    with connection() as db:
        return db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='customer.camera_quota_changed'").fetchone()["n"]


def _revoke_global(email):
    with connection() as db:
        grant = db.execute("SELECT g.id FROM identity_grants g JOIN partner_users u ON u.id=g.user_id "
                           "WHERE lower(u.email)=? AND g.scope_type='global' AND g.revoked_at IS NULL", (email,)).fetchone()
        appliance_identity.revoke_grant(db, grant_id=grant["id"])


# ------------------------------------------------------------------ U-A1-1


def test_session_refused_once_the_authorization_version_moves(client):
    make_partner_user("owner@partner.test", "partner_owner", partner_id="partner-a")
    token = partner_session_cookie("owner@partner.test", "partner_owner")
    assert partner_portal.partner_identity(_request(token)) is not None
    with connection() as db:
        appliance_identity.bump_authorization_version(db, "u-owner-at-partner-test")
    assert partner_portal.partner_identity(_request(token)) is None


def test_session_refused_when_the_account_role_changed_without_a_version_bump(client):
    uid = make_partner_user("ops@partner.test", "administrator", partner_id="partner-a", global_admin=True)
    token = partner_session_cookie("ops@partner.test", "administrator")
    assert partner_portal.partner_identity(_request(token)) is not None
    with connection() as db:
        db.execute("UPDATE partner_users SET role='partner_owner' WHERE id=?", (uid,))
    assert partner_portal.partner_identity(_request(token)) is None


@pytest.mark.parametrize("status_sql", ["approved=0", "account_status='suspended'", "account_status='revoked'"])
def test_session_refused_when_the_account_is_no_longer_active(client, status_sql):
    uid = make_partner_user("tech@partner.test", "technician", partner_id="partner-a")
    token = partner_session_cookie("tech@partner.test", "technician")
    with connection() as db:
        db.execute(f"UPDATE partner_users SET {status_sql} WHERE id=?", (uid,))
    assert partner_portal.partner_identity(_request(token)) is None


def test_session_with_no_recorded_version_is_refused(client):
    make_partner_user("sales@partner.test", "salesperson", partner_id="partner-a")
    token = partner_session_cookie("sales@partner.test", "salesperson", version=None)
    assert partner_portal.partner_identity(_request(token)) is None


def test_administrator_token_without_a_session_row_is_refused(client):
    make_partner_user("ops@partner.test", "administrator", partner_id="partner-a", global_admin=True)
    bare = partner_portal._token("ops@partner.test", "administrator", "partner-a", None, None)
    assert partner_portal.partner_identity(_request(bare)) is None
    response = client.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 3}, cookies=cookies_for(bare))
    assert refused(response), response.status_code


def test_token_naming_another_accounts_session_is_refused(client):
    make_partner_user("owner@partner.test", "partner_owner", partner_id="partner-a")
    make_partner_user("victim@partner.test", "partner_owner", partner_id="partner-a")
    token = partner_session_cookie("victim@partner.test", "partner_owner")
    session_id = session_row(token)["id"]
    forged = partner_portal._token("owner@partner.test", "partner_owner", "partner-a", None, session_id)
    assert partner_portal.partner_identity(_request(forged)) is None


def test_revoking_a_grant_revokes_that_users_sessions_in_the_same_transaction(client):
    make_partner_user("ops@partner.test", "administrator", partner_id="partner-a", global_admin=True)
    token = partner_session_cookie("ops@partner.test", "administrator")
    _revoke_global("ops@partner.test")
    assert session_row(token)["revoked_at"] is not None


def test_bumping_the_version_revokes_open_sessions(client):
    uid = make_partner_user("owner@partner.test", "partner_owner", partner_id="partner-a")
    token = partner_session_cookie("owner@partner.test", "partner_owner")
    with connection() as db:
        appliance_identity.bump_authorization_version(db, uid)
    assert session_row(token)["revoked_at"] is not None


def test_partner_login_records_the_current_version_and_auth_source(client):
    make_partner_user("owner@partner.test", "partner_owner", partner_id="partner-a")
    response = client.post("/api/partner-login", json={"email": "owner@partner.test", "password": PASSWORD})
    assert response.status_code == 303, response.text
    row = session_row(partner_cookie_from(response))
    assert row["authorization_version_at_login"] == current_version("owner@partner.test")
    assert row["auth_source"] == "password"


def test_password_change_keeps_this_session_and_refreshes_its_version(client):
    make_partner_user("owner@partner.test", "partner_owner", partner_id="partner-a")
    token = partner_session_cookie("owner@partner.test", "partner_owner")
    other = partner_session_cookie("owner@partner.test", "partner_owner")
    response = client.post("/api/partner/activate-account", json={"password": "another-long-password-2", "current_password": PASSWORD, "accept_terms": True},
                           cookies=cookies_for(token))
    assert response.status_code == 200, response.text
    assert partner_portal.partner_identity(_request(token)) is not None  # this session follows the new version
    assert partner_portal.partner_identity(_request(other)) is None      # every other session ends (Z8g)


def test_password_reset_ends_an_existing_admin_session(client):
    import cloud_security
    uid = make_partner_user("ops@partner.test", "administrator", partner_id="partner-a", global_admin=True)
    token = partner_session_cookie("ops@partner.test", "administrator")
    raw = cloud_security.create_password_reset(uid, "ops@partner.test")
    assert cloud_security.consume_password_reset(raw, "a-brand-new-password-9")
    assert partner_portal.partner_identity(_request(token)) is None
    # A reset never marks MFA as verified (L-9): the next administrator sign-in
    # still goes through MFA / enrollment on the cloud.
    if main.RUNTIME_ROLE == "cloud":
        response = client.post("/api/portal-login", json={"email": "ops@partner.test", "password": "a-brand-new-password-9", "portal": "administrator"})
        cookie = partner_cookie_from(response)
        assert cookie is None or session_row(cookie)["mfa_state"] != "verified"


# ------------------------------------------------------------------ Z8d'


def test_pre_revoke_admin_session_cannot_change_a_camera_quota_with_a_valid_body(client):
    make_partner_user("ops@partner.test", "administrator", partner_id="partner-a", global_admin=True)
    token = partner_session_cookie("ops@partner.test", "administrator")
    before_rows, before_audits = _entitlements(), _quota_audits()
    _revoke_global("ops@partner.test")
    response = client.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 1}, cookies=cookies_for(token))
    assert refused(response), (response.status_code, response.text)
    assert _entitlements() == before_rows
    assert _quota_audits() == before_audits


def test_fresh_sign_in_after_the_revoke_gets_no_administrator_access(client, profile):
    make_partner_user("ops@partner.test", "administrator", partner_id="partner-a", global_admin=True)
    _revoke_global("ops@partner.test")
    before_rows, before_audits = _entitlements(), _quota_audits()
    for path, body in (("/api/partner-login", {"email": "ops@partner.test", "password": PASSWORD}),
                       ("/api/portal-login", {"email": "ops@partner.test", "password": PASSWORD, "portal": "administrator"})):
        response = client.post(path, json=body)
        cookie = partner_cookie_from(response)
        if profile == "cloud":
            # Refused at sign-in: no administrator session at all.
            assert response.status_code in (401, 403), (path, response.status_code, response.text)
            assert cookie is None
        if cookie:
            quota = client.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 1}, cookies=cookies_for(cookie))
            if profile == "cloud":
                assert refused(quota), (path, quota.status_code)
    if profile == "cloud":
        # The cloud-profile rule (coordinator 2026-10-24); an edge appliance keeps
        # its local administrator behaviour (E-1/E-2).
        assert _entitlements() == before_rows
        assert _quota_audits() == before_audits
        denials = [a for a in audit_actions() if a[1] == "login.denied"]
        assert denials and all("ops@partner.test" not in (a[3] or "") for a in denials)


def test_partner_scoped_administrator_cannot_use_admin_routes_on_the_cloud(client, profile):
    if profile != "cloud":
        pytest.skip("cloud-profile rule")
    make_partner_user("company-admin@partner.test", "administrator", partner_id="partner-a", partner_admin_grant=True)
    token = partner_session_cookie("company-admin@partner.test", "administrator")
    before_rows = _entitlements()
    response = client.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 1}, cookies=cookies_for(token))
    assert response.status_code == 403, response.text
    assert _entitlements() == before_rows


# ------------------------------------------------------------------ U-A1-2 / Z8f matrix


def _matrix_body(path):
    return {"camera_quota": 1} if path.endswith("/camera-quota") else {}


def _run_matrix(client, token):
    before = table_snapshot()
    first_audit = max_audit_id()
    failures = []
    for method, path in admin_route_matrix():
        response = client.request(method, path, cookies=cookies_for(token), json=None if method == "GET" else _matrix_body(path))
        if not refused(response):
            failures.append((method, path, response.status_code))
    assert not failures, failures
    assert table_snapshot() == before
    unexpected = [a for a in audit_actions(first_audit) if a[1] not in DENIAL_ACTIONS]
    assert not unexpected, unexpected


def test_every_admin_route_refuses_a_pre_revoke_session(client):
    make_partner_user("ops@partner.test", "administrator", partner_id="partner-a", global_admin=True)
    token = partner_session_cookie("ops@partner.test", "administrator")
    _revoke_global("ops@partner.test")
    _run_matrix(client, token)


def test_every_admin_route_refuses_an_administrator_without_a_global_grant_on_the_cloud(client, profile):
    if profile != "cloud":
        pytest.skip("cloud-profile rule")
    # role administrator, current version, MFA-verified -- but no live global grant
    make_partner_user("ops@partner.test", "administrator", partner_id="partner-a")
    token = partner_session_cookie("ops@partner.test", "administrator")
    _run_matrix(client, token)


def test_mfa_setup_page_refuses_a_full_admin_session_without_a_live_grant(client, profile):
    if profile != "cloud":
        pytest.skip("cloud-profile rule")
    # This page is intentionally exempted from middleware's admin-namespace
    # grant gate for enrollment-only sessions. A normal MFA-verified session
    # must still have a live admin grant before the page is served.
    make_partner_user("ops@partner.test", "administrator", partner_id="partner-a")
    token = partner_session_cookie("ops@partner.test", "administrator")
    response = client.get("/admin/mfa-setup", cookies=cookies_for(token))
    assert response.status_code == 403


def test_matrix_covers_the_known_admin_surface():
    paths = {path for _method, path in admin_route_matrix()}
    for expected in ("/api/admin/customers/cust-a/camera-quota", "/admin-portal", "/api/operations/identity-grants"):
        assert expected in paths


# ------------------------------------------------------------------ helpers


def _request(token):
    from starlette.requests import Request
    cookie = f"{partner_portal.SESSION_COOKIE}={token}"
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", cookie.encode())], "query_string": b"",
             "client": ("127.0.0.1", 1), "server": ("portal.test", 443), "scheme": "https"}
    return Request(scope)
