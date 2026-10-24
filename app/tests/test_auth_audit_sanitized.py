"""Sanitized authentication monitoring (Linux 1.2.7.1, D-AUTHZ-2b).

Green IDs: M-1 (the audit events exist), M-2 (failed MFA attempts per account,
burst alert at 5 failures in 15 minutes), EN-3 / Z6k (the TOTP secret, recovery
codes, tokens and passwords never appear in audit rows or logs).
"""
import json
import logging

import pytest

import main
import platform_owner
from authz_helpers import (
    audit_actions, cookies_for, enroll_confirmed_mfa, isolated_app, make_partner_user, partner_cookie_from, totp_now,
)
from partner_db import connection

PASSWORD = "correct-horse-battery-1"
OWNER = "owner@anyaicam.test"


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=True) as client:
        yield client


def _login(client, email=OWNER, password=PASSWORD):
    return client.post("/api/portal-login", json={"email": email, "password": password, "portal": "administrator"})


def test_auth_audit_masks_the_email_and_drops_secret_fields(cloud):
    import admin_auth_policy
    admin_auth_policy.auth_audit("login.denied", actor_email="Someone.Long@Example.com", actor_role="administrator",
                                 reason="legacy_admin_on_cloud",
                                 details={"password": "p4ss", "code": "123456", "token": "tok", "secret": "S3CR", "recovery_codes": ["A"], "count": 2})
    row = audit_actions()[-1]
    assert row[1] == "login.denied"
    assert "someone.long@example.com" not in (row[3] or "").lower()
    details = json.loads(row[2])
    assert details == {"reason": "legacy_admin_on_cloud", "count": 2}
    assert admin_auth_policy.mask_email("someone.long@example.com") != "someone.long@example.com"


def test_full_flow_leaves_no_secret_in_audit_or_logs(cloud, caplog):
    caplog.set_level(logging.DEBUG)
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    # enrollment-only sign-in, enroll, confirm
    enrollment = partner_cookie_from(_login(cloud))
    enroll = cloud.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(enrollment)).json()
    secret = enroll["secret_base32"]
    codes = cloud.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(secret)}, cookies=cookies_for(enrollment)).json()["recovery_codes"]
    # a failed code, a TOTP sign-in, a recovery-code sign-in
    pending = _login(cloud).json()["mfa_pending_token"]
    cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending, "code": "000000"})
    pending2 = _login(cloud).json()["mfa_pending_token"]
    ok = cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending2, "code": totp_now(secret)})
    session = partner_cookie_from(ok)
    pending3 = _login(cloud).json()["mfa_pending_token"]
    cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": pending3, "code": codes[0]})
    # break-glass
    with connection() as db:
        raw = platform_owner.create_break_glass_token(db, email=OWNER, reason="test", created_by="ops@host")
    cloud.post("/api/platform-owner/break-glass-recover", json={"email": OWNER, "token": raw})
    # legacy admin denial
    main.save_users([{"id": "l1", "email": "legacy@anyaicam.test", "role": "administrator", "enabled": True,
                      "password_hash": main.hash_password("legacy-password-1")}])
    _login(cloud, "legacy@anyaicam.test", "legacy-password-1")

    actions = {a[1] for a in audit_actions()}
    for expected in ("login.mfa_required", "mfa_login_verify", "mfa_login_failed", "mfa_enroll_started", "mfa_enroll_confirmed",
                     "recovery_code_used", "break_glass_redeemed", "login.denied"):
        assert expected in actions, (expected, sorted(actions))

    with connection() as db:
        audit_blob = json.dumps([dict(r) for r in db.execute("SELECT * FROM audit_logs").fetchall()])
    haystacks = {"audit": audit_blob, "logs": caplog.text}
    secrets_to_find = [secret, *codes, raw, pending, pending2, pending3, PASSWORD, "legacy-password-1", session or "\x00"]
    for name, haystack in haystacks.items():
        for value in secrets_to_find:
            assert value not in haystack, (name, value[:4])


def test_failed_mfa_attempts_are_counted_per_account_and_bursts_alert(cloud, caplog):
    import admin_auth_policy
    uid = make_partner_user(OWNER, "administrator", global_admin=True)
    enroll_confirmed_mfa(uid)
    caplog.set_level(logging.WARNING)
    for _ in range(admin_auth_policy.MFA_FAILURE_BURST_THRESHOLD):
        response = _login(cloud)
        if response.status_code != 200:
            break
        cloud.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": response.json()["mfa_pending_token"], "code": "000000"})
    assert admin_auth_policy.failed_mfa_attempts_24h(uid) >= admin_auth_policy.MFA_FAILURE_BURST_THRESHOLD - 1
    assert admin_auth_policy.MFA_FAILURE_BURST_THRESHOLD == 5 and admin_auth_policy.MFA_FAILURE_BURST_WINDOW_MINUTES == 15
    assert "security.mfa_failure_burst" in caplog.text or "mfa_failure_burst" in {a[1] for a in audit_actions()}
