"""Windows 1.3.4.1 owner policy R-2 (approved 2026-10-29): administrator access on a CLAIMED appliance.

A cloud-credential administrator may reach the Admin Portal of a claimed Windows appliance only with an explicit,
authorized appliance-local grant AND completed MFA. Until that appliance-local grant + MFA path exists (R-2 design
proposal), every cloud-delegated administrator sign-in on a claimed appliance is refused. These tests pin:
  1. cloud credentials alone (even a live global grant, even a locally enrolled factor) never grant administration;
  2. an absent, revoked or suspended grant fails closed;
  3. no administrator session without MFA can be minted or replayed on a claimed appliance;
  4. the appliance-local emergency /login recovery path still works;
  5. a refused administrator sign-in changes no appliance, camera or recording state.
"""
import pytest

import appliance_identity
import main
import partner_portal
from authz_helpers import (cookies_for, enroll_confirmed_mfa, isolated_app, make_partner_user, partner_session_cookie,
                           table_snapshot)
from partner_db import connection, password_hash

ADMIN = "cloud-admin@anyaicam.test"
PASSWORD = "Sup3rSecret!-cloud-1"
CLOUD_ID = "AIC-R2POLICY1"


def _seed_claimed_appliance():
    now = "2026-10-29T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','P','approved','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) "
                   "VALUES('cust-1','partner-1','C','','c@example.test','active','eligible','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','S',?)", (now,))
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES('appl-1','cust-1','site-1',?,'partner-1',?)", (CLOUD_ID, now))
        db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-1','appl-1',?,?)", (password_hash("appliance-credential"), now))
        db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,created_at) VALUES('cam-1','cust-1','site-1','appl-1','Camera 1',1,?)", (now,))


@pytest.fixture()
def claimed(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        _seed_claimed_appliance()
        monkeypatch.setenv("ANYAICAM_APPLIANCE_ID", "appl-1")
        monkeypatch.setenv("ANYAICAM_APPLIANCE_CLOUD_ID", CLOUD_ID)
        monkeypatch.setenv("ANYAICAM_APPLIANCE_CREDENTIAL", "appliance-credential")
        monkeypatch.setattr(main, "own_appliance_identity", lambda: {"appliance_id": "appl-1", "cloud_id": CLOUD_ID})
        monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
        appliance_identity.reset_cloud_identity_backend_for_tests()
        yield client
        appliance_identity.reset_cloud_identity_backend_for_tests()


def _cloud_admin(scope_type="global", enrolled=False):
    uid = make_partner_user(ADMIN, "administrator", partner_id="partner-1", password=PASSWORD,
                            global_admin=scope_type == "global", partner_admin_grant=scope_type == "partner")
    if enrolled:
        enroll_confirmed_mfa(uid)
    return uid


def _admin_login(client, password=PASSWORD):
    return client.post("/api/portal-login", json={"email": ADMIN, "password": password, "portal": "administrator"}, follow_redirects=False)


def _no_session(response) -> bool:
    return partner_portal.SESSION_COOKIE not in response.cookies and main.SESSION_COOKIE_NAME not in response.cookies


# ------------------------------------------------------------------ 1. cloud credentials alone never grant administration


@pytest.mark.parametrize("scope_type", ["global", "partner"])
@pytest.mark.parametrize("enrolled", [False, True])
def test_cloud_credentials_alone_never_grant_claimed_appliance_administration(claimed, scope_type, enrolled):
    _cloud_admin(scope_type, enrolled)
    response = _admin_login(claimed)
    assert response.status_code == 403, response.text[:200]
    assert _no_session(response)
    with connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM user_sessions WHERE lower(email)=?", (ADMIN,)).fetchone()["n"] == 0
        assert db.execute("SELECT COUNT(*) AS n FROM pending_mfa_logins WHERE lower(email)=?", (ADMIN,)).fetchone()["n"] == 0


# ------------------------------------------------------------------ 2. absent / revoked / suspended fail closed


def test_an_absent_grant_fails_closed(claimed):
    make_partner_user(ADMIN, "administrator", partner_id="partner-1", password=PASSWORD)  # no grant at all
    response = _admin_login(claimed)
    assert response.status_code in (401, 403) and _no_session(response)


def test_a_revoked_grant_fails_closed(claimed):
    uid = _cloud_admin("global")
    with connection() as db:
        db.execute("UPDATE identity_grants SET revoked_at='2026-10-29T01:00:00' WHERE user_id=?", (uid,))
    response = _admin_login(claimed)
    assert response.status_code in (401, 403) and _no_session(response)


def test_a_suspended_account_fails_closed(claimed):
    uid = _cloud_admin("global")
    with connection() as db:
        db.execute("UPDATE partner_users SET account_status='suspended' WHERE id=?", (uid,))
    response = _admin_login(claimed)
    assert response.status_code in (401, 403) and _no_session(response)


# ------------------------------------------------------------------ 3. no unverified or delegated session works


@pytest.mark.parametrize("auth_source,mfa_state", [("cloud_assertion", None), ("cloud_assertion", "verified"), ("password", None)])
def test_no_unverified_or_delegated_administrator_session_reaches_the_admin_portal(claimed, auth_source, mfa_state):
    uid = _cloud_admin("global", enrolled=True)
    token = partner_session_cookie(ADMIN, "administrator", mfa_state=mfa_state, auth_source=auth_source, user_id=uid)
    for path in ("/admin-portal", "/api/operations/identity-grants"):
        response = claimed.get(path, cookies=cookies_for(token), follow_redirects=False)
        reached = response.status_code == 200 and '<p class="eyebrow">Access control</p>' not in response.text
        assert not reached, (auth_source, mfa_state, path, response.status_code)


# ------------------------------------------------------------------ 4. appliance-local emergency recovery is preserved


def test_the_local_emergency_login_still_reaches_the_admin_portal_on_a_claimed_appliance(claimed, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit_log.jsonl")
    main.save_users([{"id": "local-owner", "display_name": "Local owner", "email": "owner@local.test", "role": "administrator",
                      "enabled": True, "password_hash": main.hash_password("Local-Owner-Pass-1"), "site_ids": ["home"], "camera_ids": []}])
    login = claimed.post("/login", data={"email": "owner@local.test", "password": "Local-Owner-Pass-1", "next_url": "/"}, follow_redirects=False)
    assert login.status_code == 303
    token = login.cookies.get(main.SESSION_COOKIE_NAME)
    assert token
    page = claimed.get("/admin-portal", cookies={main.SESSION_COOKIE_NAME: token})
    assert page.status_code == 200 and '<p class="eyebrow">Access control</p>' not in page.text


# ------------------------------------------------------------------ 5. a refusal changes no appliance / camera / recording state


def test_a_refused_cloud_admin_sign_in_changes_no_appliance_camera_or_recording_state(claimed):
    _cloud_admin("global", enrolled=True)
    # The v1/dev cloud backend shares this database and creates its manifest-signing key on first use
    # (identity_signing_keys). Warm it with one refused sign-in, then require the NEXT refusal to change nothing.
    assert _admin_login(claimed).status_code == 403
    before = table_snapshot()
    recordings = sorted(p.name for p in main.RECORDINGS_FOLDER.glob("*")) if main.RECORDINGS_FOLDER.exists() else []
    assert _admin_login(claimed).status_code == 403
    assert table_snapshot() == before  # cameras, appliances, recording settings and grants untouched
    after = sorted(p.name for p in main.RECORDINGS_FOLDER.glob("*")) if main.RECORDINGS_FOLDER.exists() else []
    assert after == recordings


# ------------------------------------------------------------------ Codex PASS-WITH-NOTES follow-ups (2026-10-29)


def test_the_first_refused_sign_in_changes_only_the_dev_cloud_signing_key(claimed):
    # Codex note: assess the FIRST refusal separately. The only permitted change is the v1/dev combined cloud
    # backend creating its manifest-signing key (identity_signing_keys) on first use; nothing appliance-, camera-,
    # grant-, session- or recording-related may change.
    _cloud_admin("global", enrolled=True)
    before = table_snapshot()
    assert _admin_login(claimed).status_code == 403
    after = table_snapshot()
    changed = sorted(table for table in set(before) | set(after) if before.get(table) != after.get(table))
    assert changed in ([], ["identity_signing_keys"]), changed


def test_the_emergency_login_session_has_no_identity_grant_authority(claimed, tmp_path, monkeypatch):
    # D-1 / Codex note: the local emergency /login reaches the Admin Portal for recovery, but it never acquires
    # persistent-grant authority -- it can neither list, create nor revoke identity grants.
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit_log.jsonl")
    uid = _cloud_admin("global")
    with connection() as db:
        grant_id = db.execute("SELECT id FROM identity_grants WHERE user_id=?", (uid,)).fetchone()["id"]
    main.save_users([{"id": "local-owner", "display_name": "Local owner", "email": "owner@local.test", "role": "administrator",
                      "enabled": True, "password_hash": main.hash_password("Local-Owner-Pass-1"), "site_ids": ["home"], "camera_ids": []}])
    login = claimed.post("/login", data={"email": "owner@local.test", "password": "Local-Owner-Pass-1", "next_url": "/"}, follow_redirects=False)
    cookies = {main.SESSION_COOKIE_NAME: login.cookies.get(main.SESSION_COOKIE_NAME)}
    before = table_snapshot()
    assert claimed.get("/api/operations/identity-grants", cookies=cookies).status_code in (401, 403)
    created = claimed.post("/api/operations/identity-grants", cookies=cookies,
                           json={"email": "owner@local.test", "role": "administrator", "scope_type": "global"})
    assert created.status_code in (401, 403)
    assert claimed.post(f"/api/operations/identity-grants/{grant_id}/revoke", cookies=cookies).status_code in (401, 403)
    assert table_snapshot() == before
