"""Windows 1.3.4.1 MFA security correction (D-AUTHZ-2b port).

Linux 1.2.7.1 ends an administrator Partner Portal session without the
server-side MFA marker on the cloud profile only. The administrator-only
routes guarded by partner_portal.require_partner_access(request,
{'administrator'}) -- appliance_cloud's /api/admin/* (People Counting, recording
modes, camera quota, cloud/storage policy, activation tokens, revoke, relay
pilot, capacity transition), cloud_features, website_partner._require_global_admin
and everything built on it, partner pricing -- and the Test Lab grant routes are
registered on an edge (Windows) install too, where such a session was accepted.
These tests pin the corrected rule on BOTH profiles:

  - an administrator session that did not pass MFA (no marker, enrollment-only,
    re-enrollment) is refused with 403 and changes nothing;
  - an MFA-verified administrator still reaches the route (the existing role
    and global-grant requirements are unchanged);
  - non-administrator roles stay refused;
  - on the edge the refused session itself stays valid for the surfaces Linux
    E-2 keeps open (/admin-portal), and is not revoked;
  - a real MFA sign-in replaces the unverified session with a verified one
    that reaches the route.
"""
import json

import pytest

import partner_portal
from authz_helpers import (
    audit_actions, cookies_for, enroll_confirmed_mfa, isolated_app, make_partner_user, partner_cookie_from,
    partner_session_cookie, refused, session_row, table_snapshot, totp_now,
)
from partner_db import connection

OWNER = "owner@anyaicam.test"
PASSWORD = "correct-horse-battery-1"
UNVERIFIED_STATES = [None, "enrollment_only", "reenroll_required"]


def _expected_refusal(mfa_state, *, cloud: bool) -> int:
    """403 = a full administrator session that reached the route without MFA (the
    corrected rule). 401 = main.app's authentication middleware already found no
    full identity: an MFA (re-)enrollment-only session, a session-less token, or
    (cloud, D-AUTHZ-2b) an unmarked session revoked on sight."""
    return 403 if mfa_state is None and not cloud else 401

# (method, path, json body) -- one request per administrator-only route family.
ADMIN_ONLY_ROUTES = [
    ("POST", "/api/admin/cameras/cam-1/people-counting", {"people_counting_enabled": True}),
    ("POST", "/api/admin/cameras/cam-1/cloud-recording-mode", {"cloud_recording_mode": "event"}),
    ("POST", "/api/admin/cameras/cam-1/local-recording-mode", {"local_recording_mode": "event"}),
    ("POST", "/api/admin/customers/cust-a/camera-quota", {"camera_quota": 4}),
    ("POST", "/api/admin/customers/cust-a/cloud-policy", {"cloud_enabled": True}),
    ("POST", "/api/admin/customers/cust-a/storage-policy", {"storage_days": 30}),
    ("POST", "/api/admin/appliances/appl-1/activation-token", {"expires_hours": 24}),
    ("POST", "/api/admin/appliances/appl-1/capacity-identity-transition", {}),
    ("POST", "/api/admin/appliances/appl-1/revoke", {}),
    ("POST", "/api/admin/appliances/appl-1/live-relay-pilot", {"enabled": True}),
    ("GET", "/api/admin/deployment-verification", None),
    ("GET", "/api/admin/audit-export", None),
    ("GET", "/api/admin/data-retention", None),
    ("PUT", "/api/admin/data-retention", {"media": 30}),
    ("GET", "/api/admin/partner-applications", None),
    ("PUT", "/api/admin/partner-applications/app-1", {"status": "approved"}),
    ("PUT", "/api/admin/partner-pricing", {}),
    ("POST", "/api/admin/sales-attribution", {"customer_id": "cust-a", "salesperson": "x"}),
    ("POST", "/api/admin/software-releases", {}),
    ("POST", "/api/admin/storage-trials", {}),
    ("POST", "/api/admin/hardware-orders/order-1/fulfillment", {"status": "shipped"}),
    ("POST", "/api/admin/friends-family/req-1/decision", {"decision": "approve"}),
    ("POST", "/api/admin/test-lab/grants", {}),
    ("POST", "/api/admin/test-lab/grants/tlg-1/revoke", {}),
]


def _seed():
    now = "2026-10-28T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-a','A','approved','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) "
                   "VALUES('cust-a','partner-a','Customer A','a@customers.test','active','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-a','Site',?)", (now,))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-a','site-1','AIC-TEST0001',?)", (now,))
        db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,created_at) "
                   "VALUES('cam-1','cust-a','site-1','appl-1','Camera 1',1,?)", (now,))


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        _seed()
        yield client


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=True) as client:
        _seed()
        yield client


def _people_counting(client, token):
    return client.post("/api/admin/cameras/cam-1/people-counting", cookies=cookies_for(token), json={"people_counting_enabled": True})


def _entitled() -> bool:
    with connection() as db:
        return bool(db.execute("SELECT people_counting_enabled FROM cameras WHERE id='cam-1'").fetchone()["people_counting_enabled"])


def _mfa_denials() -> int:
    return sum(1 for _, action, details, _ in audit_actions()
               if action == "access.denied" and json.loads(details or "{}").get("reason") == "admin_without_mfa")


# ----------------------------------------------------------- the reported defect


@pytest.mark.parametrize("mfa_state", UNVERIFIED_STATES)
def test_edge_people_counting_refuses_admin_without_verified_mfa(edge, mfa_state):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_session_cookie(OWNER, "administrator", mfa_state=mfa_state)
    response = _people_counting(edge, token)
    assert response.status_code == _expected_refusal(mfa_state, cloud=False), response.text
    assert not _entitled()


def test_edge_people_counting_allows_mfa_verified_admin(edge):
    make_partner_user(OWNER, "administrator", global_admin=True)
    response = _people_counting(edge, partner_session_cookie(OWNER, "administrator", mfa_state="verified"))
    assert response.status_code == 200, response.text
    assert response.json() == {"camera_id": "cam-1", "people_counting_enabled": True}
    assert _entitled()


@pytest.mark.parametrize("role", ["partner_owner", "salesperson", "technician", "customer_owner", "customer_viewer"])
def test_edge_people_counting_refuses_non_administrators(edge, role):
    if role.startswith("customer_"):
        make_partner_user(f"{role}@example.test", role, partner_id=None, customer_id="cust-a")
    else:
        make_partner_user(f"{role}@example.test", role, partner_id="partner-a")
    token = partner_session_cookie(f"{role}@example.test", role)
    assert _people_counting(edge, token).status_code == 403
    assert not _entitled()


def test_edge_people_counting_refuses_session_less_admin_token(edge):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_portal._token(OWNER, "administrator")
    assert _people_counting(edge, token).status_code == 401
    assert not _entitled()


# ----------------------------------------------------------- neighbouring routes


@pytest.mark.parametrize("mfa_state", UNVERIFIED_STATES)
def test_edge_every_admin_only_route_refuses_admin_without_verified_mfa(edge, mfa_state):
    make_partner_user(OWNER, "administrator", global_admin=True)
    before = table_snapshot()
    failures = []
    for method, path, body in ADMIN_ONLY_ROUTES:
        token = partner_session_cookie(OWNER, "administrator", mfa_state=mfa_state)
        response = edge.request(method, path, cookies=cookies_for(token), json=body)
        if response.status_code != _expected_refusal(mfa_state, cloud=False):
            failures.append((method, path, response.status_code))
    assert not failures, failures
    assert table_snapshot() == before


@pytest.mark.parametrize("mfa_state", UNVERIFIED_STATES)
def test_cloud_every_admin_only_route_refuses_admin_without_verified_mfa(tmp_path, monkeypatch, mfa_state):
    with isolated_app(tmp_path, monkeypatch, cloud=True) as client:
        _seed()
        make_partner_user(OWNER, "administrator", global_admin=True)
        before = table_snapshot()
        failures = []
        for method, path, body in ADMIN_ONLY_ROUTES:
            token = partner_session_cookie(OWNER, "administrator", mfa_state=mfa_state)
            response = client.request(method, path, cookies=cookies_for(token), json=body)
            if response.status_code != _expected_refusal(mfa_state, cloud=True):
                failures.append((method, path, response.status_code))
        assert not failures, failures
        assert table_snapshot() == before


def test_edge_admin_only_refusal_is_audited_and_does_not_revoke_the_session(edge):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_session_cookie(OWNER, "administrator", mfa_state=None)
    assert _people_counting(edge, token).status_code == 403
    assert _mfa_denials() == 1
    assert session_row(token)["revoked_at"] is None
    # Linux E-2 is unchanged: the edge Admin Portal itself still opens for it.
    assert edge.get("/admin-portal", cookies=cookies_for(token)).status_code == 200


def test_edge_mfa_verified_admin_keeps_neighbouring_routes(edge):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_session_cookie(OWNER, "administrator", mfa_state="verified")
    quota = edge.post("/api/admin/customers/cust-a/camera-quota", cookies=cookies_for(token), json={"camera_quota": 4})
    assert quota.status_code == 200, quota.text
    retention = edge.get("/api/admin/data-retention", cookies=cookies_for(token))
    assert retention.status_code == 200, retention.text
    assert _mfa_denials() == 0


# ----------------------------------------------------------- cloud profile (unchanged D-AUTHZ-2b)


@pytest.mark.parametrize("mfa_state", UNVERIFIED_STATES)
def test_cloud_people_counting_refuses_admin_without_verified_mfa(cloud, mfa_state):
    make_partner_user(OWNER, "administrator", global_admin=True)
    token = partner_session_cookie(OWNER, "administrator", mfa_state=mfa_state)
    assert _people_counting(cloud, token).status_code == _expected_refusal(mfa_state, cloud=True)
    assert not _entitled()
    if mfa_state is None:
        assert session_row(token)["revoked_at"] is not None  # D-AUTHZ-2b: revoked on sight on the cloud


def test_cloud_people_counting_allows_mfa_verified_global_admin(cloud):
    make_partner_user(OWNER, "administrator", global_admin=True)
    assert _people_counting(cloud, partner_session_cookie(OWNER, "administrator")).status_code == 200
    assert _entitled()


def test_cloud_verified_partner_scoped_admin_still_needs_the_global_grant(cloud):
    make_partner_user("company-admin@partner.test", "administrator", partner_id="partner-a", partner_admin_grant=True)
    token = partner_session_cookie("company-admin@partner.test", "administrator", mfa_state="verified")
    assert _people_counting(cloud, token).status_code == 403
    assert not _entitled()


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_delegated_admin_cannot_reuse_local_mfa_by_email_or_user_id(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed()
        uid = make_partner_user(OWNER, "administrator", global_admin=True)
        enroll_confirmed_mfa(uid)
        delegated = partner_session_cookie(OWNER, "administrator", mfa_state="verified", auth_source="cloud_assertion", user_id=uid)
        before = table_snapshot()
        response = _people_counting(client, delegated)
        assert response.status_code == 403
        assert table_snapshot() == before


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_partner_scoped_admin_is_refused_by_all_global_only_routes_without_mutation(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed()
        email = "company-admin@partner.test"
        make_partner_user(email, "administrator", partner_id="partner-a", partner_admin_grant=True)
        token = partner_session_cookie(email, "administrator", mfa_state="verified")
        before = table_snapshot()
        failures = []
        for method, path, body in ADMIN_ONLY_ROUTES:
            response = client.request(method, path, cookies=cookies_for(token), json=body)
            if response.status_code != 403:
                failures.append((method, path, response.status_code, response.text[:160]))
        assert not failures, failures
        assert table_snapshot() == before


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_revoked_global_grant_is_rechecked_by_every_admin_only_route(tmp_path, monkeypatch, profile):
    import appliance_identity

    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed()
        uid = make_partner_user(OWNER, "administrator", global_admin=True)
        token = partner_session_cookie(OWNER, "administrator", mfa_state="verified")
        assert _people_counting(client, token).status_code == 200
        with connection() as db:
            grant = db.execute("SELECT id FROM identity_grants WHERE user_id=? AND scope_type='global' AND revoked_at IS NULL", (uid,)).fetchone()
            # Model a grant revoked by an external authority before local
            # session reconciliation has bumped authorization_version.
            db.execute("UPDATE identity_grants SET revoked_at='2026-10-28T12:00:00' WHERE id=?", (grant["id"],))
        after_revoke = table_snapshot()
        failures = []
        for method, path, body in ADMIN_ONLY_ROUTES:
            response = client.request(method, path, cookies=cookies_for(token), json=body)
            if response.status_code != 403:
                failures.append((method, path, response.status_code))
        assert not failures, failures
        assert table_snapshot() == after_revoke
        assert session_row(token)["revoked_at"] is None


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_partner_admin_grant_must_match_account_partner_and_live_mfa(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed()
        email = "mismatched-admin@partner.test"
        uid = make_partner_user(email, "administrator", partner_id="partner-a", partner_admin_grant=True)
        with connection() as db:
            db.execute("UPDATE identity_grants SET scope_id='partner-b' WHERE user_id=? AND scope_type='partner'", (uid,))
        token = partner_session_cookie(email, "administrator", mfa_state="verified")
        response = client.get("/api/partner/customers", cookies=cookies_for(token))
        assert response.status_code == 403
        assert not _entitled()


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_partner_admin_mfa_enrollment_is_limited_until_verified_then_stays_tenant_scoped(tmp_path, monkeypatch, profile):
    import json
    import main

    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-b','B','approved','real','2026-10-28')")
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-b','partner-b','Customer B','b@customers.test','active','real','2026-10-28')")
        customer_file = tmp_path / "partner_customers.json"
        customer_file.write_text(json.dumps([
            {"id": "cust-a", "partner_id": "partner-a", "name": "Customer A"},
            {"id": "cust-b", "partner_id": "partner-b", "name": "Customer B"},
        ]), encoding="utf-8")
        monkeypatch.setattr(main, "PARTNER_CUSTOMERS_FILE", customer_file)
        email = "company-admin@partner.test"
        uid = make_partner_user(email, "administrator", partner_id="partner-a", partner_admin_grant=True, password=PASSWORD)
        login = client.post("/api/portal-login", json={"email": email, "password": PASSWORD, "portal": "administrator"})
        assert login.status_code == 303, login.text
        enrollment = partner_cookie_from(login)
        assert enrollment and session_row(enrollment)["mfa_state"] == "enrollment_only"
        assert client.get("/api/partner/customers", cookies=cookies_for(enrollment)).status_code in (401, 403)
        assert client.post("/api/admin/customers/cust-a/camera-quota", cookies=cookies_for(enrollment), json={"camera_quota": 4}).status_code in (401, 403)

        started = client.post("/api/platform-owner/mfa/enroll", json={}, cookies=cookies_for(enrollment))
        assert started.status_code == 200, started.text
        confirmed = client.post("/api/platform-owner/mfa/confirm", json={"code": totp_now(started.json()["secret_base32"])}, cookies=cookies_for(enrollment))
        assert confirmed.status_code == 200 and confirmed.json()["sign_in_required"] is True
        login = client.post("/api/portal-login", json={"email": email, "password": PASSWORD, "portal": "administrator"})
        assert login.status_code == 200 and login.json()["status"] == "mfa_required"
        verified = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": login.json()["mfa_pending_token"], "code": totp_now(started.json()["secret_base32"])})
        assert verified.status_code == 303, verified.text
        token = partner_cookie_from(verified)
        assert session_row(token)["mfa_state"] == "verified"
        customers = client.get("/api/partner/customers", cookies=cookies_for(token))
        assert customers.status_code == 200, customers.text
        assert {row["id"] for row in customers.json()["customers"]} == {"cust-a"}
        assert client.post("/api/admin/customers/cust-a/camera-quota", cookies=cookies_for(token), json={"camera_quota": 4}).status_code == 403
        assert not _entitled()


# ----------------------------------------------------------- platform owner / session replacement


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_mfa_sign_in_replaces_an_unverified_session_and_reaches_the_route(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed()
        uid = make_partner_user(OWNER, "administrator", global_admin=True, password=PASSWORD)
        secret = enroll_confirmed_mfa(uid)
        old = partner_session_cookie(OWNER, "administrator", mfa_state=None)
        assert _people_counting(client, old).status_code == _expected_refusal(None, cloud=profile == "cloud")
        login = client.post("/api/portal-login", json={"email": OWNER, "password": PASSWORD, "portal": "administrator"})
        assert login.status_code == 200, login.text
        assert login.json()["status"] == "mfa_required"
        assert partner_cookie_from(login) is None
        verify = client.post("/api/platform-owner/mfa/verify",
                             json={"mfa_pending_token": login.json()["mfa_pending_token"], "code": totp_now(secret)})
        assert verify.status_code == 303, verify.text
        new = partner_cookie_from(verify)
        assert session_row(new)["mfa_state"] == "verified" and session_row(new)["auth_source"] == "mfa"
        assert _people_counting(client, new).status_code == 200
        assert _entitled()
        # The unverified session never gains MFA by the other session's sign-in.
        assert refused(client.post("/api/admin/customers/cust-a/camera-quota", cookies=cookies_for(old), json={"camera_quota": 2}))


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_wrong_mfa_code_yields_no_session_and_no_change(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed()
        uid = make_partner_user(OWNER, "administrator", global_admin=True, password=PASSWORD)
        enroll_confirmed_mfa(uid)
        login = client.post("/api/portal-login", json={"email": OWNER, "password": PASSWORD, "portal": "administrator"})
        verify = client.post("/api/platform-owner/mfa/verify", json={"mfa_pending_token": login.json()["mfa_pending_token"], "code": "000000"})
        assert verify.status_code == 400
        assert partner_cookie_from(verify) is None
        assert not _entitled()
