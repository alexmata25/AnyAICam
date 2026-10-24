"""D-AUTHZ-2b (Linux 1.2.7.1): administrator access on the cloud needs MFA on
every sign-in path, every existing session and every admin route; the edge
appliance's local sign-ins are unchanged.

Green IDs (GREEN-MFA-ACCEPTANCE-SPEC.md): L-1..L-10, S-1, S-2, S-4, S-5, E-1,
E-2; Z6c/Z6d (V-dup), Z6e (legacy-only), Z6g (env accounts), Z6m (every admin
route refuses a session without MFA). Codex b8c7a7ed D-AUTHZ-2b.
"""
import pytest

import main
import partner_portal
from authz_helpers import (
    admin_route_matrix, audit_actions, cookies_for, enroll_confirmed_mfa, isolated_app, legacy_cookie_from,
    make_partner_user, partner_cookie_from, partner_session_cookie, refused, session_row, table_snapshot, totp_now,
)
from partner_db import connection

PASSWORD = "correct-horse-battery-1"
OWNER = "owner@anyaicam.test"


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=True) as client:
        _seed_customer()
        yield client


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        _seed_customer()
        yield client


def _seed_customer():
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','A','approved','real','2026-01-01')")
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) "
                   "VALUES('cust-a','partner-a','Customer A','a@customers.test','active','real','2026-01-01')")


def _legacy_admin(email=OWNER, role="administrator", password=PASSWORD, user_id="legacy-admin-1"):
    users = [item for item in main.load_users() if item.get("id") != user_id]
    users.append({"id": user_id, "display_name": "Legacy", "email": email, "role": role, "enabled": True,
                  "password_hash": main.hash_password(password), "site_ids": ["home"], "camera_ids": []})
    main.save_users(users)
    return user_id


def _portal_login(client, email=OWNER, password=PASSWORD, portal="administrator"):
    return client.post("/api/portal-login", json={"email": email, "password": password, "portal": portal})


def _actions():
    return [a[1] for a in audit_actions()]


def _denial_reasons():
    import json
    return [json.loads(a[2] or "{}").get("reason") for a in audit_actions() if a[1] == "login.denied"]


# ------------------------------------------------------------------ L-1 / Z6c


def test_enrolled_global_admin_gets_mfa_required_and_no_cookie(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    response = _portal_login(cloud)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "mfa_required"
    assert partner_cookie_from(response) is None and legacy_cookie_from(response) is None
    assert "login.mfa_required" in _actions()


def test_mfa_verify_creates_a_verified_session_that_reaches_the_admin_portal(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    secret = enroll_confirmed_mfa(uid)
    pending = _portal_login(cloud).json()["mfa_pending_token"]
    response = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": totp_now(secret)})
    assert response.status_code == 303, response.text
    token = partner_cookie_from(response)
    row = session_row(token)
    assert row["mfa_state"] == "verified" and row["mfa_verified_at"] and row["mfa_method"] == "totp"
    assert cloud.get("/admin-portal", cookies=cookies_for(token)).status_code == 200
    quota = cloud.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 2}, cookies=cookies_for(token))
    assert quota.status_code == 200, quota.text


# ------------------------------------------------------------------ L-2 / Z6f


def test_unenrolled_global_admin_gets_only_an_enrollment_session(cloud):
    make_partner_user(OWNER, "administrator", global_admin=True)
    response = _portal_login(cloud)
    token = partner_cookie_from(response)
    if token is None:  # L-2(a) variant: refused with "MFA enrollment required"
        assert response.status_code in (401, 403) and "enrol" in response.text.lower()
        return
    assert response.status_code == 303 and response.headers["location"] == "/admin/mfa-setup"
    row = session_row(token)
    assert row["mfa_state"] == "enrollment_only"
    from datetime import datetime
    assert (datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds() <= 15 * 60 + 5
    # Never a full administrator session (Z6f): admin pages/APIs refuse it ...
    assert refused(cloud.get("/admin-portal", cookies=cookies_for(token)))
    assert refused(cloud.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 2}, cookies=cookies_for(token)))
    assert partner_portal.partner_identity(_request(token)) is None
    # ... it reaches only the enrollment surface and logout.
    assert cloud.get("/admin/mfa-setup", cookies=cookies_for(token)).status_code == 200
    assert cloud.get("/api/platform-owner/mfa/status", cookies=cookies_for(token)).status_code == 200
    assert cloud.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(token)).status_code == 200
    logout = cloud.post("/logout", cookies=cookies_for(token))
    assert logout.status_code == 303
    assert session_row(token)["revoked_at"] is not None


def test_enrollment_session_reaches_nothing_else_in_the_admin_namespace(cloud):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_cookie_from(_portal_login(cloud))
    if token is None:
        pytest.skip("L-2(a) refusal variant")
    before = table_snapshot()
    failures = [(m, p, r.status_code) for m, p in admin_route_matrix(include_enrollment=False)
                for r in [cloud.request(m, p, cookies=cookies_for(token), json=None if m == "GET" else {})] if not refused(r)]
    assert not failures, failures
    assert table_snapshot() == before


# ------------------------------------------------------------------ L-3 / Z6d (V-dup)


def test_duplicate_legacy_and_partner_identity_takes_the_partner_path(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    _legacy_admin()
    for portal in ("administrator", None):
        response = cloud.post("/api/portal-login", json={"email": OWNER, "password": PASSWORD, **({"portal": portal} if portal else {})})
        assert response.status_code == 200, (portal, response.status_code, response.text)
        assert response.json()["status"] == "mfa_required"
        assert legacy_cookie_from(response) is None and partner_cookie_from(response) is None


def test_resolve_portal_login_never_selects_the_legacy_system_on_the_cloud():
    decision = main.resolve_portal_login(selected_portal="administrator", legacy_role="administrator", partner_role="administrator",
                                         partner_administrator_scope="global")
    assert decision["system"] == "partner" and decision["destination"] == "/admin-portal"


# ------------------------------------------------------------------ L-4, L-5 / Z6e (V-legacy-only)


def test_legacy_only_admin_is_refused_on_portal_login(cloud):
    _legacy_admin()
    response = _portal_login(cloud)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."
    assert partner_cookie_from(response) is None and legacy_cookie_from(response) is None
    assert "legacy_admin_on_cloud" in _denial_reasons()


@pytest.mark.parametrize("role", ["administrator", "admin", "support_admin", "installer"])
def test_legacy_admin_capable_role_is_refused_on_the_login_form(cloud, role):
    _legacy_admin(role=role)
    response = cloud.post("/login", data={"email": OWNER, "password": PASSWORD, "next_url": "/admin-portal"})
    assert response.status_code == 401
    assert legacy_cookie_from(response) is None
    assert "legacy_admin_on_cloud" in _denial_reasons()


def test_legacy_login_form_refuses_before_checking_the_password(cloud, monkeypatch):
    _legacy_admin()
    called = []
    monkeypatch.setattr(main, "verify_password", lambda *a, **k: called.append(1) or True)
    response = cloud.post("/login", data={"email": OWNER, "password": "wrong", "next_url": "/"})
    assert response.status_code == 401 and not called


def test_non_admin_legacy_role_gets_no_admin_access_on_the_cloud(cloud):
    _legacy_admin(role="viewer")
    response = cloud.post("/login", data={"email": OWNER, "password": PASSWORD, "next_url": "/"})
    cookie = legacy_cookie_from(response)
    if cookie:
        assert refused(cloud.get("/admin-portal", cookies={main.SESSION_COOKIE_NAME: cookie}))
        assert refused(cloud.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 2},
                                  cookies={main.SESSION_COOKIE_NAME: cookie}))


# ------------------------------------------------------------------ S-1 / S-4 / Z6j (sessions at upgrade)


def test_legacy_admin_session_from_before_the_upgrade_is_dead_on_the_cloud(cloud):
    user_id = _legacy_admin()
    token = main.create_session(user_id)
    assert refused(cloud.get("/admin-portal", cookies={main.SESSION_COOKIE_NAME: token}))
    assert refused(cloud.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 2}, cookies={main.SESSION_COOKIE_NAME: token}))
    assert main.authenticated_user(_request_legacy(token)) is None


def test_admin_partner_session_without_an_mfa_marker_is_refused_revoked_and_audited(cloud):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_session_cookie(OWNER, "administrator", mfa_state=None)
    assert refused(cloud.get("/admin-portal", cookies=cookies_for(token)))
    assert partner_portal.partner_identity(_request(token)) is None
    assert session_row(token)["revoked_at"] is not None
    assert "session.invalidated" in _actions()


@pytest.mark.parametrize("mfa_state", [None, "enrollment_only", "reenroll_required"])
def test_every_admin_route_refuses_a_session_without_mfa(cloud, mfa_state):
    """Z6m: the full /admin*, /api/admin/*, /api/operations/* matrix."""
    make_partner_user(OWNER, "administrator", global_admin=True)
    before = table_snapshot()
    failures = []
    for method, path in admin_route_matrix(include_enrollment=False):
        token = partner_session_cookie(OWNER, "administrator", mfa_state=mfa_state)
        response = cloud.request(method, path, cookies=cookies_for(token), json=None if method == "GET" else {"camera_quota": 1})
        if not refused(response):
            failures.append((method, path, response.status_code))
    assert not failures, failures
    assert table_snapshot() == before


def test_partner_scoped_admin_needs_mfa_too(cloud):
    make_partner_user("company-admin@partner.test", "administrator", partner_id="partner-a", partner_admin_grant=True)
    token = partner_session_cookie("company-admin@partner.test", "administrator", mfa_state=None)
    assert partner_portal.partner_identity(_request(token)) is None
    verified = partner_session_cookie("company-admin@partner.test", "administrator")
    assert partner_portal.partner_identity(_request(verified)) is not None


# ------------------------------------------------------------------ S-2


def test_verified_admin_session_ends_on_grant_revoke(cloud):
    import appliance_identity
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    make_partner_user("backup@anyaicam.test", "administrator", global_admin=True)
    token = partner_session_cookie(OWNER, "administrator")
    assert cloud.get("/admin-portal", cookies=cookies_for(token)).status_code == 200
    with connection() as db:
        grant = db.execute("SELECT id FROM identity_grants WHERE user_id=? AND scope_type='global'", (uid,)).fetchone()
        appliance_identity.revoke_grant(db, grant_id=grant["id"])
    assert refused(cloud.get("/admin-portal", cookies=cookies_for(token)))


# ------------------------------------------------------------------ L-6 / L-7 / Z6g


def test_partner_login_never_gives_an_administrator_session(cloud):
    make_partner_user(OWNER, "administrator", global_admin=True)
    response = cloud.post("/api/partner-login", json={"email": OWNER, "password": PASSWORD})
    token = partner_cookie_from(response)
    assert token is None or session_row(token)["mfa_state"] == "verified"
    assert response.status_code in (200, 401, 403)
    if response.status_code == 200:
        assert response.json().get("status") == "mfa_required"


@pytest.mark.parametrize("flag", [{}, {"partner_only": True}, {"customer_only": True}])
def test_partner_login_admin_refusal_holds_for_every_flavour(cloud, flag):
    make_partner_user(OWNER, "administrator", global_admin=True)
    response = cloud.post("/api/partner-login", json={"email": OWNER, "password": PASSWORD, **flag})
    assert partner_cookie_from(response) is None


@pytest.mark.parametrize("role", ["partner_owner", "salesperson", "technician"])
def test_partner_roles_still_sign_in_through_partner_login(cloud, role):
    make_partner_user("staff@partner.test", role, partner_id="partner-a")
    response = cloud.post("/api/partner-login", json={"email": "staff@partner.test", "password": PASSWORD})
    assert response.status_code == 303, response.text
    assert partner_portal.partner_identity(_request(partner_cookie_from(response))) is not None


def test_customer_sign_in_unaffected(cloud):
    make_partner_user("owner@customer.test", "customer_owner", partner_id=None, customer_id="cust-a")
    response = cloud.post("/api/partner-login", json={"email": "owner@customer.test", "password": PASSWORD, "customer_only": True})
    assert response.status_code == 303, response.text
    assert partner_portal.partner_identity(_request(partner_cookie_from(response)))["role"] == "customer_owner"


def test_env_defined_partner_accounts_are_refused_on_the_cloud(cloud, monkeypatch):
    for role in ("administrator", "partner_owner"):
        monkeypatch.setattr(partner_portal, "PORTAL_ACCOUNTS", {"env@anyaicam.test": {"approved": True, "role": role, "password": PASSWORD}})
        response = cloud.post("/api/partner-login", json={"email": "env@anyaicam.test", "password": PASSWORD})
        assert partner_cookie_from(response) is None, role
        assert response.status_code in (401, 403)


def test_env_account_session_from_before_the_upgrade_is_refused_on_the_cloud(cloud):
    token = partner_session_cookie("env@anyaicam.test", "partner_owner", auth_source=None, version=None)
    assert partner_portal.partner_identity(_request(token)) is None


# ------------------------------------------------------------------ L-8


@pytest.mark.parametrize("path", ["/first-admin-setup", "/api/first-admin-setup"])
def test_first_admin_setup_is_404_on_the_cloud(cloud, path):
    response = cloud.get(path) if not path.startswith("/api") else cloud.post(path, json={"email": "x@y.test", "password": "a" * 14})
    assert response.status_code == 404


def test_first_admin_setup_is_404_on_combined(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=True) as client:
        monkeypatch.setattr(main, "RUNTIME_ROLE", "combined")
        monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "combined")
        from dataclasses import replace
        monkeypatch.setattr(partner_portal, "settings", replace(partner_portal.settings, runtime_role="combined"))
        assert client.get("/first-admin-setup").status_code == 404


# ------------------------------------------------------------------ L-10


def test_failed_mfa_codes_count_toward_the_lockout(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    for _ in range(5):
        pending = _portal_login(cloud)
        if pending.status_code == 429:
            break
        assert pending.status_code == 200, pending.text
        bad = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending.json()["mfa_pending_token"], "code": "000000"})
        assert bad.status_code in (400, 429)
    assert _portal_login(cloud).status_code == 429
    import cloud_security
    assert cloud_security.login_blocked(OWNER)


def test_wrong_code_burns_the_pending_token(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    secret = enroll_confirmed_mfa(uid)
    pending = _portal_login(cloud).json()["mfa_pending_token"]
    assert cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": "000000"}).status_code == 400
    again = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": totp_now(secret)})
    assert again.status_code == 400


# ------------------------------------------------------------------ E-1 / E-2 (edge unchanged)


def test_edge_local_emergency_login_still_works(edge):
    _legacy_admin()
    response = edge.post("/login", data={"email": OWNER, "password": PASSWORD, "next_url": "/"})
    assert response.status_code == 303
    token = legacy_cookie_from(response)
    assert token and main.authenticated_user(_request_legacy(token))["role"] == "administrator"


def test_edge_admin_partner_session_needs_no_cloud_mfa(edge):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_session_cookie(OWNER, "administrator", mfa_state=None)
    assert partner_portal.partner_identity(_request(token)) is not None
    assert edge.get("/admin-portal", cookies=cookies_for(token)).status_code == 200


def test_edge_delegated_session_without_a_local_account_keeps_working(edge):
    token = partner_session_cookie("tech@cloud.test", "technician", auth_source="cloud_assertion", version=4, user_id="cloud-user-7")
    assert partner_portal.partner_identity(_request(token)) is not None


def test_edge_local_customer_session_keeps_working_with_the_cloud_version(edge):
    make_partner_user("owner@customer.test", "customer_owner", partner_id=None, customer_id="cust-a")  # local cache row, version 1
    token = partner_session_cookie("owner@customer.test", "customer_owner", auth_source="local_customer", version=9)
    assert partner_portal.partner_identity(_request(token)) is not None


def test_cloud_refuses_edge_only_auth_sources(cloud):
    token = partner_session_cookie("tech@cloud.test", "technician", auth_source="cloud_assertion", version=4, user_id="cloud-user-7")
    assert partner_portal.partner_identity(_request(token)) is None


# ------------------------------------------------------------------ helpers


def _request(token):
    from starlette.requests import Request
    cookie = f"{partner_portal.SESSION_COOKIE}={token}"
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", cookie.encode())], "query_string": b"",
                    "client": ("127.0.0.1", 1), "server": ("portal.test", 443), "scheme": "https"})


def _request_legacy(token):
    from starlette.requests import Request
    cookie = f"{main.SESSION_COOKIE_NAME}={token}"
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", cookie.encode())], "query_string": b"",
                    "client": ("127.0.0.1", 1), "server": ("portal.test", 443), "scheme": "https"})
