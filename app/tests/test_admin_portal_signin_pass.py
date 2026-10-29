"""Admin portal completion pass (2026-09-26): sign-in on the cloud itself.

The cloud portal (RUNTIME_ROLE=cloud, no own appliance identity) checks
Administrator sign-ins directly against partner_db. Before this pass that
path never looked at identity_grants, so a live global administrator grant
landed on the Partner Portal and the platform-owner MFA gate could never
trigger there. platform_owner's MFA API also had no page calling it, the
login page could not complete an MFA sign-in, and a wrong MFA code rolled
back the pending login's single-use claim (unlimited guesses per password
check). These tests pin each of those fixes on the real HTTP routes.
"""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import appliance_identity
import main
import partner_portal
import platform_owner
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash

PASSWORD = "Adm1n-pass-for-tests"


@pytest.fixture()
def cloud_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    # Staging/production shape: the cloud itself, never an activated appliance.
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    for name in ("ANYAICAM_APPLIANCE_ID", "ANYAICAM_APPLIANCE_CLOUD_ID", "ANYAICAM_APPLIANCE_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    db_path = tmp_path / "admin_signin.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as client:
            yield client
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _seed_admin(email, *, scope_type="global", scope_id=None, partner_id="partner-1"):
    with connection() as db:
        now = "2026-09-26T00:00:00"
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, "Partner", "approved", "real", now))
        user_id = "u-" + email.split("@")[0]
        db.execute(
            "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, partner_id, email, "Admin", "administrator", password_hash(PASSWORD), 1, now),
        )
        if scope_type:
            appliance_identity.create_grant(db, user_id=user_id, role="administrator", scope_type=scope_type, scope_id=scope_id, granted_by="test")
    return user_id


def _login(client, email, portal="administrator"):
    return client.post("/api/portal-login", json={"email": email, "password": PASSWORD, "portal": portal}, follow_redirects=False)


def _session(email):
    """Linux 1.2.7.1 (D-AUTHZ-2b, S-2): a real session bound to the account's
    current authorization_version and MFA-verified, as /mfa/verify issues it --
    a session-less administrator token is refused ('no_session')."""
    from authz_helpers import partner_session_cookie
    return {partner_portal.SESSION_COOKIE: partner_session_cookie(email, "administrator")}


def _enrollment_session(client, email):
    """Linux 1.2.7.1 (GREEN-MFA-ACCEPTANCE-SPEC.md L-2(a)): an un-enrolled
    administrator's password sign-in on the cloud yields only an
    enrollment-only session, sent to /admin/mfa-setup."""
    response = _login(client, email)
    assert response.status_code == 303 and response.headers["location"] == "/admin/mfa-setup"
    return {partner_portal.SESSION_COOKIE: response.cookies[partner_portal.SESSION_COOKIE]}


def _enable_mfa(client, email, cookies=None):
    # EN-1: enrollment happens from the enrollment-only session the sign-in gave.
    cookies = cookies or _enrollment_session(client, email)
    secret = client.post("/api/platform-owner/mfa/enroll", cookies=cookies).json()["secret_base32"]
    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    codes = client.post("/api/platform-owner/mfa/confirm", json={"code": code}, cookies=cookies).json()["recovery_codes"]
    return secret, codes


def _mfa_sign_in(client, email, secret):
    """L-1: password, then the TOTP second step; returns the MFA-verified cookie and where it was sent."""
    pending = _login(client, email).json()["mfa_pending_token"]
    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    verified = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": code}, follow_redirects=False)
    assert verified.status_code == 303
    return {partner_portal.SESSION_COOKIE: verified.cookies[partner_portal.SESSION_COOKIE]}, verified.headers["location"]


# ------------------------------------------------------------ cloud login routing


def test_cloud_global_administrator_lands_on_the_admin_portal(cloud_client):
    _seed_admin("platform@anyaicam.test")
    # Linux 1.2.7.1 (GREEN-MFA-ACCEPTANCE-SPEC.md §2 L-2(a)): an UN-enrolled global
    # administrator's password sign-in no longer opens /admin-portal; it gets an
    # enrollment-only session sent to /admin/mfa-setup that cannot reach the portal.
    response = _login(cloud_client, "platform@anyaicam.test")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/mfa-setup"
    enrollment = {partner_portal.SESSION_COOKIE: response.cookies[partner_portal.SESSION_COOKIE]}
    blocked = cloud_client.get("/admin-portal", cookies=enrollment, follow_redirects=False)
    assert "Customer and billing overview" not in blocked.text
    # After enrolling (EN-1) the global administrator signs in with MFA (L-1/EN-4)
    # and lands on the Admin Portal.
    secret, _codes = _enable_mfa(cloud_client, "platform@anyaicam.test", enrollment)
    cookies, location = _mfa_sign_in(cloud_client, "platform@anyaicam.test", secret)
    assert location == "/admin-portal"
    page = cloud_client.get("/admin-portal", cookies=cookies)
    assert page.status_code == 200
    assert "Customer and billing overview" in page.text


def test_cloud_partner_scoped_administrator_still_stays_on_the_partner_portal(cloud_client):
    _seed_admin("company-admin@anyaicam.test", scope_type="partner", scope_id="partner-1")
    # Linux 1.2.7.1 (spec §2 L-2/L-6, §5 S-2: never an administrator session without
    # MFA on the cloud): every administrator scope signs in through MFA, so the
    # first password sign-in is enrollment-only (/admin/mfa-setup) ...
    response = _login(cloud_client, "company-admin@anyaicam.test")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/mfa-setup"
    # ... and after enrolling, the partner-scoped administrator still lands on the
    # Partner Portal, never /admin-portal.
    secret, _codes = _enable_mfa(cloud_client, "company-admin@anyaicam.test",
                                 {partner_portal.SESSION_COOKIE: response.cookies[partner_portal.SESSION_COOKIE]})
    _cookies, location = _mfa_sign_in(cloud_client, "company-admin@anyaicam.test", secret)
    assert location == "/partner?tab=customers"


def test_cloud_administrator_without_any_grant_stays_on_the_partner_portal(cloud_client):
    _seed_admin("legacy-admin@anyaicam.test", scope_type=None)
    # Linux 1.2.7.1 (CORRECTIVE-1.2.7.1.md "Fix scope widened", coordinator 2026-10-24
    # Green Z8d'): on the cloud an administrator with no live grant is refused at
    # sign-in (generic credential error, no session) instead of reaching /partner.
    response = _login(cloud_client, "legacy-admin@anyaicam.test")
    assert response.status_code == 401
    assert partner_portal.SESSION_COOKIE not in response.cookies
    assert "location" not in response.headers


def test_revoked_global_grant_no_longer_routes_to_the_admin_portal(cloud_client):
    user_id = _seed_admin("revoked@anyaicam.test")
    with connection() as db:
        db.execute("UPDATE identity_grants SET revoked_at=? WHERE user_id=?", ("2026-09-26T01:00:00", user_id))
    # Linux 1.2.7.1 (CORRECTIVE-1.2.7.1.md "a revoked admin is refused at login as
    # administrator"): no session at all, rather than a Partner Portal one.
    response = _login(cloud_client, "revoked@anyaicam.test")
    assert response.status_code == 401
    assert partner_portal.SESSION_COOKIE not in response.cookies
    assert "location" not in response.headers


def test_wrong_password_is_still_rejected(cloud_client):
    _seed_admin("platform@anyaicam.test")
    response = cloud_client.post("/api/portal-login", json={"email": "platform@anyaicam.test", "password": "wrong", "portal": "administrator"}, follow_redirects=False)
    assert response.status_code == 401


# ------------------------------------------------------------ MFA on the cloud


def test_cloud_global_administrator_with_mfa_must_complete_the_second_step(cloud_client):
    _seed_admin("platform@anyaicam.test")
    secret, _codes = _enable_mfa(cloud_client, "platform@anyaicam.test")

    login = _login(cloud_client, "platform@anyaicam.test")
    assert login.status_code == 200
    body = login.json()
    assert body["status"] == "mfa_required"
    assert partner_portal.SESSION_COOKIE not in login.cookies

    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    verified = cloud_client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": body["mfa_pending_token"], "code": code}, follow_redirects=False)
    assert verified.status_code == 303
    assert verified.headers["location"] == "/admin-portal"
    assert partner_portal.SESSION_COOKIE in verified.cookies


def test_a_wrong_mfa_code_spends_the_pending_login(cloud_client):
    _seed_admin("platform@anyaicam.test")
    secret, _codes = _enable_mfa(cloud_client, "platform@anyaicam.test")
    token = _login(cloud_client, "platform@anyaicam.test").json()["mfa_pending_token"]

    wrong = cloud_client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": token, "code": "000000"}, follow_redirects=False)
    assert wrong.status_code == 400
    assert "Sign in again" in wrong.json()["detail"]

    code = platform_owner._totp_code_at(secret, int(time.time() // 30))
    retry = cloud_client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": token, "code": code}, follow_redirects=False)
    assert retry.status_code == 400  # the same pending login can't be guessed at again
    assert partner_portal.SESSION_COOKIE not in retry.cookies
    with connection() as db:
        failures = db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='mfa_login_failed'").fetchone()["n"]
    assert failures == 1


def test_recovery_code_signs_in_once(cloud_client):
    _seed_admin("platform@anyaicam.test")
    _secret, codes = _enable_mfa(cloud_client, "platform@anyaicam.test")
    first = _login(cloud_client, "platform@anyaicam.test").json()["mfa_pending_token"]
    assert cloud_client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": first, "code": codes[0]}, follow_redirects=False).status_code == 303
    second = _login(cloud_client, "platform@anyaicam.test").json()["mfa_pending_token"]
    assert cloud_client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": second, "code": codes[0]}, follow_redirects=False).status_code == 400


# ------------------------------------------------------------ MFA status + card


def test_mfa_status_reports_state_without_secrets(cloud_client):
    _seed_admin("platform@anyaicam.test")
    # Linux 1.2.7.1: before enrolling, the only session the cloud gives is the
    # enrollment-only one (spec L-2(a)); /mfa/status now also names the session
    # kind ("enrollment_only" / "full", platform_owner.py:475-476) so the setup page
    # can tell them apart. Still no secret and no code in the response.
    enrollment = _enrollment_session(cloud_client, "platform@anyaicam.test")
    before = cloud_client.get("/api/platform-owner/mfa/status", cookies=enrollment).json()
    assert before == {"mfa_enabled": False, "recovery_codes_remaining": 0, "session": "enrollment_only"}
    secret, _codes = _enable_mfa(cloud_client, "platform@anyaicam.test", enrollment)
    cookies, _location = _mfa_sign_in(cloud_client, "platform@anyaicam.test", secret)
    status = cloud_client.get("/api/platform-owner/mfa/status", cookies=cookies).json()
    assert status == {"mfa_enabled": True, "recovery_codes_remaining": 10, "session": "full"}
    assert secret not in str(status) and secret not in str(before)


def test_a_partner_scoped_administrator_can_enroll_mfa_but_gains_no_global_reach(cloud_client):
    """Owner 2026-10-28 (EN-1 updated): partner-scoped administrators may enroll MFA. Enrolling must not widen what
    they can reach: still no Admin Portal and no administrator API."""
    from authz_helpers import admin_route_matrix, refused, table_snapshot
    _seed_admin("company-admin@anyaicam.test", scope_type="partner", scope_id="partner-1")
    assert cloud_client.get("/api/platform-owner/mfa/status").status_code == 401  # no session at all
    _enable_mfa(cloud_client, "company-admin@anyaicam.test")  # through the real enrollment-only sign-in session
    verified = _session("company-admin@anyaicam.test")
    status = cloud_client.get("/api/platform-owner/mfa/status", cookies=verified)
    assert status.status_code == 200 and status.json()["mfa_enabled"] is True
    assert refused(cloud_client.get("/admin-portal", cookies=verified, follow_redirects=False))
    # Codex review 50f614eb note 5: every method of every administrator route, not only GET, and nothing written.
    matrix = admin_route_matrix(include_enrollment=False)
    assert {method for method, _path in matrix} >= {"GET", "POST"}
    before = table_snapshot()
    allowed = []
    for method, path in matrix:
        response = cloud_client.request(method, path, cookies=verified, follow_redirects=False,
                                        json=None if method == "GET" else {"camera_quota": 1})
        if not refused(response):
            allowed.append((method, path, response.status_code))
    assert allowed == [], allowed
    assert table_snapshot() == before


def test_admin_portal_renders_the_sign_in_security_card(cloud_client):
    _seed_admin("platform@anyaicam.test")
    # Linux 1.2.7.1 (spec L-1/L-2): /admin-portal needs an MFA-verified session, so
    # the administrator enrolls and signs in with the second step first.
    secret, _codes = _enable_mfa(cloud_client, "platform@anyaicam.test")
    cookies, _location = _mfa_sign_in(cloud_client, "platform@anyaicam.test", secret)
    page = cloud_client.get("/admin-portal", cookies=cookies).text
    assert 'id="admin-signin-security"' in page
    for route in ("/api/platform-owner/mfa/status", "/api/platform-owner/mfa/enroll", "/api/platform-owner/mfa/confirm", "/api/platform-owner/recovery-codes/regenerate"):
        assert route in page
    # the recovery-code list joins with a JS newline escape, not a raw line break
    assert "codes.join('\\n')" in page


# ------------------------------------------------------------ login page


def test_login_page_is_revalidated_and_can_finish_an_mfa_sign_in(cloud_client):
    response = cloud_client.get("/partner.html")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert 'id="mfa"' in response.text
    assert "/api/platform-owner/mfa/verify" in response.text
    assert "mfa_required" in response.text
    assert ".form[hidden]{display:none}" in response.text
    assert cloud_client.get("/customer-login.html").headers["cache-control"] == "no-cache"


def test_login_page_script_parses():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = (Path(main.__file__).with_name("partner.html")).read_text(encoding="utf-8")
    scripts = [chunk.split("</script>")[0] for chunk in html.split("<script>")[1:]]
    for script in scripts:
        result = subprocess.run([node, "--check", "-"], input=script, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr


# ------------------------------------------------------------ forgot / reset pages


@pytest.mark.parametrize("path", ["/forgot-password", "/reset-password?token=abc"])
def test_public_password_pages_show_no_app_chrome_or_license_details(cloud_client, path):
    response = cloud_client.get(path)
    assert response.status_code == 200
    text = response.text
    assert 'class="sidebar"' not in text and 'class="nav"' not in text and "mobile-nav" not in text
    assert "License attention" not in text and "license permits" not in text
    assert "email-preview" not in text and "Local development" not in text
    assert 'href="/partner.html"' in text  # back to the portal sign-in, not the app
    assert "/api/password-reset/" in text


def test_reset_page_escapes_the_token_and_is_not_cached(cloud_client):
    response = cloud_client.get('/reset-password?token=x"onmouseover="alert(1)')
    assert 'value="x&quot;onmouseover=&quot;alert(1)"' in response.text
    assert 'onmouseover="alert(1)"' not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_forgot_password_request_is_generic_for_unknown_and_known_accounts(cloud_client):
    _seed_admin("platform@anyaicam.test")
    csrf = cloud_client.get("/forgot-password").cookies.get("anyaicam_csrf") or cloud_client.cookies.get("anyaicam_csrf")
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    known = cloud_client.post("/api/password-reset/request", json={"email": "platform@anyaicam.test"}, headers=headers)
    unknown = cloud_client.post("/api/password-reset/request", json={"email": "nobody@anyaicam.test"}, headers=headers)
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
