"""D-AUTHZ-2b enrollment and recovery (Linux 1.2.7.1, cloud profile).

Green IDs: EN-1, EN-2, EN-4, RC-1 (recovery code once; regenerate needs a
verified session AND a current code), RC-2 (break-glass: host-minted only,
single use, forces re-enrollment, throttled), RC-3 (no email/skip bypass),
L-2/T-2 (an un-enrolled owner is never locked out), mfa_verify version re-check.
"""
import io
from contextlib import redirect_stdout

import pytest

import main
import partner_portal
import platform_owner
from authz_helpers import (
    cookies_for, current_version, enroll_confirmed_mfa, isolated_app, make_partner_user, partner_cookie_from,
    partner_session_cookie, refused, session_row, totp_now,
)
from partner_db import connection

PASSWORD = "correct-horse-battery-1"
OWNER = "owner@anyaicam.test"


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=True) as client:
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','A','approved','real','2026-01-01')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) "
                       "VALUES('cust-a','partner-a','Customer A','a@customers.test','active','real','2026-01-01')")
        yield client


def _login(client):
    return client.post("/api/portal-login", json={"email": OWNER, "password": PASSWORD, "portal": "administrator"})


def _enrollment_session(client):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_cookie_from(_login(client))
    assert token, "an un-enrolled administrator must get an enrollment-only session (L-2a, T-2)"
    return token


# ------------------------------------------------------------------ EN-1 / EN-4 / (h)


def test_enrollment_from_the_enrollment_session_then_fresh_sign_in(cloud):
    token = _enrollment_session(cloud)
    version_before = current_version(OWNER)
    enroll = cloud.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(token))
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret_base32"]
    assert enroll.json()["provisioning_uri"].startswith("otpauth://totp/")
    confirm = cloud.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(secret)}, cookies=cookies_for(token))
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert len(body["recovery_codes"]) == 10
    assert body.get("sign_in_required") is True
    # The enrollment session ends; the version moved; a fresh L-1 sign-in follows.
    assert session_row(token)["revoked_at"] is not None
    assert current_version(OWNER) > version_before
    again = _login(cloud)
    assert again.status_code == 200 and again.json()["status"] == "mfa_required"
    with connection() as db:
        stored = [row["code_hash"] for row in db.execute("SELECT code_hash FROM platform_owner_recovery_codes").fetchall()]
    assert stored and all(code not in stored for code in body["recovery_codes"])  # hashed at rest (EN-3)


def test_enrollment_only_session_cannot_regenerate_recovery_codes(cloud):
    token = _enrollment_session(cloud)
    response = cloud.post("/api/platform-owner/recovery-codes/regenerate", json={}, cookies=cookies_for(token))
    assert response.status_code in (400, 401, 403)


# ------------------------------------------------------------------ EN-2


def test_replacing_a_confirmed_factor_needs_a_current_code_and_an_abandoned_replacement_keeps_mfa(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    secret = enroll_confirmed_mfa(uid)
    token = partner_session_cookie(OWNER, "administrator")
    assert cloud.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(token)).status_code == 403
    started = cloud.post("/api/platform-owner/mfa/enroll", json={"current_code": totp_now(secret)}, cookies=cookies_for(token))
    assert started.status_code == 200
    # abandoned: the confirmed factor is still required at sign-in
    assert _login(cloud).json()["status"] == "mfa_required"
    with connection() as db:
        assert platform_owner.mfa_is_confirmed(db, user_id=uid)


def test_confirming_a_replacement_keeps_this_session_and_ends_the_others(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    secret = enroll_confirmed_mfa(uid)
    token = partner_session_cookie(OWNER, "administrator")
    other = partner_session_cookie(OWNER, "administrator")
    new_secret = cloud.post("/api/platform-owner/mfa/enroll", json={"current_code": totp_now(secret)}, cookies=cookies_for(token)).json()["secret_base32"]
    confirm = cloud.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(new_secret)}, cookies=cookies_for(token))
    assert confirm.status_code == 200, confirm.text
    assert cloud.get("/admin-portal", cookies=cookies_for(token)).status_code == 200
    assert refused(cloud.get("/admin-portal", cookies=cookies_for(other)))


# ------------------------------------------------------------------ RC-1


def test_recovery_code_signs_in_once(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    with connection() as db:
        codes = platform_owner.generate_recovery_codes(db, user_id=uid)
    pending = _login(cloud).json()["mfa_pending_token"]
    first = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": codes[0]})
    assert first.status_code == 303
    assert session_row(partner_cookie_from(first))["mfa_method"] == "recovery_code"
    pending = _login(cloud).json()["mfa_pending_token"]
    reuse = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": codes[0]})
    assert reuse.status_code == 400


def test_regenerate_needs_a_verified_session_and_a_current_code(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    secret = enroll_confirmed_mfa(uid)
    token = partner_session_cookie(OWNER, "administrator")
    assert cloud.post("/api/platform-owner/recovery-codes/regenerate", json={}, cookies=cookies_for(token)).status_code in (400, 403)
    assert cloud.post("/api/platform-owner/recovery-codes/regenerate", json={"current_code": "000000"}, cookies=cookies_for(token)).status_code in (400, 403)
    ok = cloud.post("/api/platform-owner/recovery-codes/regenerate", json={"current_code": totp_now(secret)}, cookies=cookies_for(token))
    assert ok.status_code == 200 and len(ok.json()["recovery_codes"]) == 10


# ------------------------------------------------------------------ mfa_verify re-checks


def test_mfa_verify_refuses_when_the_version_moved_after_the_password_step(cloud):
    import appliance_identity
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    secret = enroll_confirmed_mfa(uid)
    pending = _login(cloud).json()["mfa_pending_token"]
    with connection() as db:
        appliance_identity.bump_authorization_version(db, uid)
    response = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": totp_now(secret)})
    assert response.status_code in (400, 403)
    assert partner_cookie_from(response) is None


def test_mfa_verify_refuses_a_locked_account(cloud):
    import cloud_security
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    secret = enroll_confirmed_mfa(uid)
    pending = _login(cloud).json()["mfa_pending_token"]
    for _ in range(6):
        cloud_security.record_login_failure(OWNER)
    response = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": totp_now(secret)})
    assert response.status_code == 429
    assert partner_cookie_from(response) is None


# ------------------------------------------------------------------ RC-2 break-glass


def _mint(email=OWNER):
    with connection() as db:
        return platform_owner.create_break_glass_token(db, email=email, reason="lost phone", created_by="operator@host")


def test_break_glass_gives_one_reenroll_session_and_forces_reenrollment(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    old = partner_session_cookie(OWNER, "administrator")
    version_before = current_version(OWNER)
    raw = _mint()
    response = cloud.post("/api/platform-owner/break-glass-recover", json={"email": OWNER, "token": raw})
    assert response.status_code == 303, response.text
    token = partner_cookie_from(response)
    row = session_row(token)
    assert row["mfa_state"] == "reenroll_required"
    assert current_version(OWNER) > version_before
    assert refused(cloud.get("/admin-portal", cookies=cookies_for(old)))  # every older session ends
    # Nothing else before re-enrollment ...
    assert refused(cloud.get("/admin-portal", cookies=cookies_for(token)))
    assert refused(cloud.post("/api/admin/customers/cust-a/camera-quota", json={"camera_quota": 1}, cookies=cookies_for(token)))
    # ... and re-enrollment works without the lost factor.
    enroll = cloud.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(token))
    assert enroll.status_code == 200, enroll.text
    confirm = cloud.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(enroll.json()["secret_base32"])}, cookies=cookies_for(token))
    assert confirm.status_code == 200 and confirm.json().get("sign_in_required") is True
    # single use
    assert cloud.post("/api/platform-owner/break-glass-recover", json={"email": OWNER, "token": raw}).status_code == 400


def test_break_glass_only_for_a_live_global_grant(cloud):
    make_partner_user("company-admin@partner.test", "administrator", partner_id="partner-a", partner_admin_grant=True)
    assert _mint("company-admin@partner.test") is None


def test_break_glass_redemption_is_throttled(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    statuses = [cloud.post("/api/platform-owner/break-glass-recover", json={"email": OWNER, "token": f"wrong-{i}"}).status_code for i in range(8)]
    assert statuses[-1] == 429, statuses
    raw = _mint()
    assert cloud.post("/api/platform-owner/break-glass-recover", json={"email": OWNER, "token": raw}).status_code == 429


def test_no_http_route_mints_a_break_glass_token():
    minting = [route.path for route in main.app.routes if "break" in getattr(route, "path", "") and route.path != "/api/platform-owner/break-glass-recover"]
    assert minting == []


def test_host_cli_mints_a_token_once(cloud):
    make_partner_user(OWNER, "administrator", global_admin=True)
    out = io.StringIO()
    with redirect_stdout(out):
        status = platform_owner.cli_main(["mint-break-glass", "--email", OWNER, "--reason", "lost phone", "--operator", "ops@host"])
    assert status == 0
    raw = out.getvalue().strip().splitlines()[-1]
    assert cloud.post("/api/platform-owner/break-glass-recover", json={"email": OWNER, "token": raw}).status_code == 303
    with redirect_stdout(io.StringIO()):
        assert platform_owner.cli_main(["mint-break-glass", "--email", "nobody@anyaicam.test", "--reason", "x", "--operator", "ops@host"]) != 0


# ------------------------------------------------------------------ RC-3


def test_no_email_or_skip_path_bypasses_mfa(cloud):
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    for extra in ({"skip_mfa": True}, {"mfa": "skip"}, {"mfa_via_email": True}):
        response = cloud.post("/api/portal-login", json={"email": OWNER, "password": PASSWORD, "portal": "administrator", **extra})
        assert response.status_code == 200 and response.json()["status"] == "mfa_required"
    mfa_paths = sorted({route.path for route in main.app.routes if "mfa" in getattr(route, "path", "")})
    assert all("email" not in path and "skip" not in path for path in mfa_paths), mfa_paths


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_global_admin_can_audit_reset_partner_admin_factor_without_granting_privilege(tmp_path, monkeypatch, profile):
    import json

    from authz_helpers import audit_actions, table_snapshot

    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','A','approved','real','2026-01-01')")
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-a','partner-a','Customer A','a@customers.test','active','real','2026-01-01')")

        target_email = "company-admin@partner.test"
        target_id = make_partner_user(target_email, "administrator", partner_id="partner-a", partner_admin_grant=True, password=PASSWORD)
        target_secret = enroll_confirmed_mfa(target_id)
        target_existing_session = partner_session_cookie(target_email, "administrator", mfa_state="verified")
        pending = _login_target(client, target_email).json()["mfa_pending_token"]
        with connection() as db:
            recovery_codes = platform_owner.generate_recovery_codes(db, user_id=target_id)

        global_email = "global-owner@anyaicam.test"
        global_id = make_partner_user(global_email, "administrator", partner_id="partner-a", global_admin=True, password=PASSWORD)
        global_secret = enroll_confirmed_mfa(global_id)
        global_unverified = partner_session_cookie(global_email, "administrator", mfa_state=None)

        scoped_email = "other-company-admin@partner.test"
        make_partner_user(scoped_email, "administrator", partner_id="partner-a", partner_admin_grant=True, password=PASSWORD)
        scoped_verified = partner_session_cookie(scoped_email, "administrator", mfa_state="verified")

        route = f"/api/platform-owner/partner-administrators/{target_id}/mfa-reset"
        body = {"partner_id": "partner-a"}
        protected_before = table_snapshot()
        assert client.post(route, cookies=cookies_for(global_unverified), json=body).status_code in (401, 403)
        assert client.post(route, cookies=cookies_for(scoped_verified), json=body).status_code == 403
        assert table_snapshot() == protected_before

        global_login = client.post("/api/portal-login", json={"email": global_email, "password": PASSWORD, "portal": "administrator"})
        assert global_login.status_code == 200 and global_login.json()["status"] == "mfa_required"
        global_verify = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": global_login.json()["mfa_pending_token"], "code": totp_now(global_secret)})
        assert global_verify.status_code == 303, global_verify.text
        global_token = partner_cookie_from(global_verify)

        reset = client.post(route, cookies=cookies_for(global_token), json=body)
        assert reset.status_code == 200, reset.text
        assert reset.json()["status"] == "reset"
        assert session_row(target_existing_session)["revoked_at"] is not None
        with connection() as db:
            assert db.execute("SELECT 1 FROM platform_owner_mfa WHERE user_id=?", (target_id,)).fetchone() is None
            assert db.execute("SELECT COUNT(*) AS n FROM platform_owner_recovery_codes WHERE user_id=?", (target_id,)).fetchone()["n"] == 0
            pending_row = db.execute("SELECT used_at FROM pending_mfa_logins WHERE id=?", (pending,)).fetchone()
            assert pending_row and pending_row["used_at"]
            grants_after = [tuple(row) for row in db.execute("SELECT role,scope_type,scope_id,revoked_at FROM identity_grants WHERE user_id=?", (target_id,)).fetchall()]
        assert grants_after == [("administrator", "partner", "partner-a", None)]

        audit = [entry for entry in audit_actions() if entry[1] == "partner_admin_mfa_reset"]
        assert audit
        details = json.loads(audit[-1][2])
        assert details["partner_id"] == "partner-a" and details["initiated_by_user_id"] == global_id

        # The target must prove password control; the response is enrollment-only,
        # old operations remain blocked, and no global grant was created.
        recovery_login = _login_target(client, target_email)
        assert recovery_login.status_code == 303
        enrollment = partner_cookie_from(recovery_login)
        assert enrollment and session_row(enrollment)["mfa_state"] == "enrollment_only"
        assert client.post("/api/admin/customers/cust-a/camera-quota", cookies=cookies_for(enrollment), json={"camera_quota": 4}).status_code in (401, 403)
        with connection() as db:
            assert db.execute("SELECT 1 FROM identity_grants WHERE user_id=? AND scope_type='global' AND revoked_at IS NULL", (target_id,)).fetchone() is None

        started = client.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(enrollment))
        assert started.status_code == 200
        confirmed = client.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(started.json()["secret_base32"])}, cookies=cookies_for(enrollment))
        assert confirmed.status_code == 200 and confirmed.json()["sign_in_required"] is True
        second_login = _login_target(client, target_email)
        assert second_login.status_code == 200 and second_login.json()["status"] == "mfa_required"
        second_verify = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": second_login.json()["mfa_pending_token"], "code": totp_now(started.json()["secret_base32"])})
        assert second_verify.status_code == 303


def _login_target(client, email):
    return client.post("/api/portal-login", json={"email": email, "password": PASSWORD, "portal": "administrator"})


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_delegated_admin_cannot_enroll_or_recover_by_matching_local_email(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        uid = make_partner_user(OWNER, "administrator", global_admin=True)
        enroll_confirmed_mfa(uid)
        delegated = partner_session_cookie(OWNER, "administrator", mfa_state="verified", auth_source="cloud_assertion", user_id=uid)
        before = current_version(OWNER)
        assert client.get("/api/platform-owner/mfa/status", cookies=cookies_for(delegated)).status_code == 403
        assert client.post(f"/api/platform-owner/partner-administrators/{uid}/mfa-reset", cookies=cookies_for(delegated), json={"partner_id": "partner-a"}).status_code == 403
        assert current_version(OWNER) == before
