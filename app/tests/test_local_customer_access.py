"""Local customer access on a claimed appliance (local_customer_access.py, 1.2.6 D-G6).

A real "cloud" database (appliance_identity.authenticate_operator / build_manifest,
real Ed25519 signing) answers the appliance's requests in-process; the appliance
side runs against its own database, trusts only the cloud's public key, and is
switched online/offline per test. No network, no real accounts.
"""
import dataclasses
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import appliance_identity
import cloud_config
import cloud_security
import local_customer_access as lca
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash

CLOUD_ID = "AIC-LOCAL0001"
OWNER_EMAIL, OWNER_PW = "owner@example.test", "Cloud-Owner-Pass-1"
APPLIANCE_PW = "Appliance-Pass-2"


class FakeCloud:
    def __init__(self, path):
        self.path, self.online = path, True
        with override_target(sqlite_path=path):
            initialize_database()
            with connection() as db:
                now = "2026-10-14T00:00:00"
                db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (now,))
                for cid in ("cust-1", "cust-2"):
                    db.execute("INSERT INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) "
                               "VALUES(?,'p1','C','',?,'active','eligible','real',?)", (cid, f"{cid}@example.test", now))
                db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','S',?)", (now,))
                db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES('appl-1','cust-1','site-1',?,'p1',?)",
                           (CLOUD_ID, now))
                self.user("u-owner", OWNER_EMAIL, OWNER_PW, "customer_owner", "cust-1", db)
                self.user("u-viewer", "viewer@example.test", "Viewer-Pass-123", "customer_viewer", "cust-1", db)
                self.user("u-other", "other@example.test", "Other-Pass-123", "customer_owner", "cust-2", db)
                appliance_identity.ensure_signing_key(db)
                self.keys = appliance_identity.active_public_keys(db)

    @staticmethod
    def user(uid, email, pw, role, cid, db):
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                   "VALUES(?,NULL,?,?,?,?,1,?,?)", (uid, email, email, role, password_hash(pw), cid, "2026-10-14"))
        appliance_identity.create_grant(db, user_id=uid, role=role, scope_type="customer", scope_id=cid, granted_by="test")

    def sql(self, statement, args=()):
        with override_target(sqlite_path=self.path):
            with connection() as db:
                db.execute(statement, args)

    def request(self, method, path, identity, payload=None):
        if not self.online:
            raise lca.CloudUnavailable("offline (test)")
        with override_target(sqlite_path=self.path):
            with connection() as db:
                if path.endswith("/authenticate-operator"):
                    return appliance_identity.authenticate_operator(db, email=payload["email"], password=payload["password"],
                                                                    portal=payload["portal"], cloud_id=CLOUD_ID)
                if path.endswith("/identity-manifest"):
                    return appliance_identity.build_manifest(db, cloud_id=CLOUD_ID)
        raise AssertionError(path)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cloud = FakeCloud(tmp_path / "cloud.db")
    identity = {"appliance_id": "appl-1", "cloud_id": CLOUD_ID, "credential": "c", "customer_id": "cust-1", "site_id": "site-1", "partner_id": "p1"}
    monkeypatch.setattr(lca, "_request", cloud.request)
    monkeypatch.setattr(lca, "_trusted_keys", lambda: dict(cloud.keys))
    monkeypatch.setattr(lca, "appliance_identity", lambda: dict(identity))
    with override_target(sqlite_path=tmp_path / "edge.db"):
        initialize_database()
        yield cloud, identity


def _edge_rows(sql, args=()):
    with connection() as db:
        return [dict(r) for r in db.execute(sql, args)]


# ------------------------------------------------------------------ cloud contract
def test_cloud_customer_bucket_is_owner_only(env):
    cloud, identity = env
    with override_target(sqlite_path=cloud.path):
        with connection() as db:
            owner = appliance_identity.authenticate_operator(db, email=OWNER_EMAIL, password=OWNER_PW, portal="customer", cloud_id=CLOUD_ID)
            viewer = appliance_identity.authenticate_operator(db, email="viewer@example.test", password="Viewer-Pass-123", portal="customer", cloud_id=CLOUD_ID)
            other = appliance_identity.authenticate_operator(db, email="other@example.test", password="Other-Pass-123", portal="customer", cloud_id=CLOUD_ID)
            tech = appliance_identity.authenticate_operator(db, email=OWNER_EMAIL, password=OWNER_PW, portal="technician", cloud_id=CLOUD_ID)
    assert owner["status"] == "ok" and owner["assertion"]["role"] == "customer_owner"
    assert viewer == {"status": "denied", "reason": "not_authorized_for_selected_portal"}
    assert other == {"status": "denied", "reason": "not_authorized_for_this_appliance"}
    assert tech["status"] == "denied"  # a customer never gets an operator portal


def test_password_reset_bumps_authorization_version(env):
    cloud, _ = env
    with override_target(sqlite_path=cloud.path):
        raw = cloud_security.create_password_reset("u-owner", OWNER_EMAIL)
        raw = raw if isinstance(raw, str) else raw.get("token") or raw.get("raw")
        before = _edge_rows("SELECT authorization_version v FROM partner_users WHERE id='u-owner'")[0]["v"]
        assert cloud_security.consume_password_reset(raw, "Brand-New-Pass-9")
        after = _edge_rows("SELECT authorization_version v FROM partner_users WHERE id='u-owner'")[0]["v"]
    assert after == before + 1


# ------------------------------------------------------------------ online sign-in
def test_online_sign_in_is_cloud_delegated_and_stores_no_password(env):
    result = lca.sign_in(OWNER_EMAIL, OWNER_PW)
    assert result["mode"] == "online" and result["role"] == "customer_owner"
    assert result["user"] == {"id": "u-owner", "email": OWNER_EMAIL, "customer_id": "cust-1", "partner_id": None}
    assert _edge_rows("SELECT * FROM local_customer_credentials") == []
    # The customer pages need a local account row: it is a cache with an unusable hash.
    from partner_db import authenticate_detailed, verify_password
    cached = _edge_rows("SELECT * FROM partner_users WHERE lower(email)=?", (OWNER_EMAIL,))
    assert [(c["id"], c["role"], c["customer_id"]) for c in cached] == [("u-owner", "customer_owner", "cust-1")]
    assert not verify_password(OWNER_PW, cached[0]["password_hash"])
    assert authenticate_detailed(OWNER_EMAIL, OWNER_PW)[0] is None


@pytest.mark.parametrize("email,password,reason", [
    (OWNER_EMAIL, "wrong-password-123", "invalid"),
    ("viewer@example.test", "Viewer-Pass-123", "not_authorized_for_selected_portal"),
    ("other@example.test", "Other-Pass-123", "not_authorized_for_this_appliance"),
])
def test_unauthorized_online_sign_in_is_refused(env, email, password, reason):
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(email, password)
    assert denied.value.reason == reason


def test_an_assertion_signed_by_an_untrusted_key_is_refused(env, monkeypatch):
    other = Ed25519 = __import__("cryptography.hazmat.primitives.asymmetric.ed25519", fromlist=["Ed25519PrivateKey"]).Ed25519PrivateKey.generate()
    import base64
    monkeypatch.setattr(lca, "_trusted_keys", lambda: {"rogue": base64.b64encode(other.public_key().public_bytes_raw()).decode()})
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, OWNER_PW)
    assert denied.value.reason == "invalid_assertion"


def test_an_assertion_for_another_customer_is_refused(env, monkeypatch):
    cloud, identity = env
    monkeypatch.setattr(lca, "appliance_identity", lambda: dict(identity, customer_id="cust-2"))
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, OWNER_PW)
    assert denied.value.reason == "not_owner"


def test_unclaimed_appliance_refuses_customer_sign_in(env, monkeypatch):
    monkeypatch.setattr(lca, "appliance_identity", lambda: None)
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, OWNER_PW)
    assert denied.value.reason == "not_claimed"


# ------------------------------------------------------------------ first enrollment
OWNER_SESSION = {"role": "customer_owner", "customer_id": "cust-1", "email": OWNER_EMAIL}


def test_enrollment_needs_the_cloud_password_and_a_different_appliance_password(env):
    cloud, _ = env
    with pytest.raises(lca.AccessDenied) as same:
        lca.enroll(OWNER_SESSION, OWNER_PW, OWNER_PW, user_id="u-owner")
    assert same.value.reason == "same_as_cloud"
    with pytest.raises(lca.AccessDenied) as short:
        lca.enroll(OWNER_SESSION, OWNER_PW, "short", user_id="u-owner")
    assert short.value.reason == "too_short"
    with pytest.raises(lca.AccessDenied) as wrong:
        lca.enroll(OWNER_SESSION, "not-the-cloud-password", APPLIANCE_PW, user_id="u-owner")
    assert wrong.value.reason == "invalid"
    with pytest.raises(lca.AccessDenied) as viewer:
        lca.enroll(dict(OWNER_SESSION, role="customer_viewer"), OWNER_PW, APPLIANCE_PW, user_id="u-viewer")
    assert viewer.value.reason == "not_owner"
    cloud.online = False
    with pytest.raises(lca.AccessDenied) as offline:
        lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    assert offline.value.reason == "cloud_unavailable"
    assert _edge_rows("SELECT * FROM local_customer_credentials") == []


def test_enrollment_stores_only_a_hash_bound_to_user_customer_and_version(env):
    assert lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")["status"] == "enrolled"
    row = _edge_rows("SELECT * FROM local_customer_credentials")[0]
    cloud, _ = env
    with override_target(sqlite_path=cloud.path):
        version = _edge_rows("SELECT authorization_version v FROM partner_users WHERE id='u-owner'")[0]["v"]
    assert (row["user_id"], row["customer_id"], row["role"], row["authorization_version"]) == ("u-owner", "cust-1", "customer_owner", version)
    blob = repr(_edge_rows("SELECT * FROM local_customer_credentials"))
    assert APPLIANCE_PW not in blob and OWNER_PW not in blob
    assert lca.status("u-owner")["enrolled"] and lca.status("u-owner")["offline_ready"]


# ------------------------------------------------------------------ offline sign-in
def test_offline_sign_in_with_the_appliance_password(env):
    cloud, _ = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    cloud.online = False
    assert lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)["mode"] == "offline"
    answers = set()
    for email, password in ((OWNER_EMAIL, OWNER_PW), (OWNER_EMAIL, "guess-guess-guess"), ("viewer@example.test", "Viewer-Pass-123"),
                            ("nobody@example.test", "Whatever-123")):
        with pytest.raises(lca.AccessDenied) as denied:
            lca.sign_in(email, password)  # the cloud password never works offline: it is not stored
        answers.add((denied.value.status, denied.value.detail, denied.value.reason))
    assert answers == {(503, lca.OFFLINE_REFUSED, "invalid_offline")}  # no account enumeration offline


def test_offline_sign_in_without_enrollment_is_refused(env):
    cloud, _ = env
    cloud.online = False
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, OWNER_PW)
    assert denied.value.status == 503 and denied.value.reason == "invalid_offline"


def test_offline_sign_in_stops_after_the_offline_grace_window(env, monkeypatch):
    cloud, _ = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    old = (datetime.now() - timedelta(hours=73)).isoformat()
    with connection() as db:
        db.execute("UPDATE local_customer_credentials SET last_verified_at=?", (old,))
    cloud.online = False
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)
    assert denied.value.reason == "offline_grace_expired"
    cloud.online = True  # a verified manifest re-confirms access and restores offline readiness
    lca.reconcile(lca.fetch_verified_manifest(lca.appliance_identity()), lca.appliance_identity())
    cloud.online = False
    assert lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)["mode"] == "offline"


def test_the_appliance_password_also_works_online_only_while_the_manifest_authorizes(env):
    cloud, _ = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    assert lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)["mode"] == "appliance_password"
    cloud.sql("UPDATE identity_grants SET revoked_at='2026-10-14' WHERE user_id='u-owner'")
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)
    assert denied.value.reason == "no_longer_authorized"


# ------------------------------------------------------------------ revocation and continuity
def test_reconcile_revokes_password_and_sessions_when_access_changes(env):
    cloud, identity = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    with connection() as db:
        db.execute("INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,last_seen_at,expires_at) "
                   "VALUES('s1','u-owner',?,'customer_owner','cookie','now','now','2999-01-01')", (OWNER_EMAIL,))
    cloud.sql("UPDATE partner_users SET authorization_version=authorization_version+1 WHERE id='u-owner'")  # e.g. a password reset
    assert lca.reconcile(lca.fetch_verified_manifest(identity), identity)["revoked"] == 1
    assert _edge_rows("SELECT revoked_reason FROM local_customer_credentials")[0]["revoked_reason"] == "authorization_changed"
    assert _edge_rows("SELECT revoked_at FROM user_sessions WHERE id='s1'")[0]["revoked_at"]
    cloud.online = False
    with pytest.raises(lca.AccessDenied):
        lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)
    assert lca.status("u-owner")["revoked_reason"] == "authorization_changed"


def test_online_sign_in_after_an_authorization_change_turns_the_old_appliance_password_off(env):
    cloud, _ = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    cloud.sql("UPDATE partner_users SET authorization_version=authorization_version+1 WHERE id='u-owner'")
    assert lca.sign_in(OWNER_EMAIL, OWNER_PW)["mode"] == "online"
    assert lca.status("u-owner")["revoked_reason"] == "authorization_changed"
    # recovery: the owner sets a new appliance password while online
    lca.enroll(OWNER_SESSION, OWNER_PW, "Appliance-Pass-3!", user_id="u-owner")
    cloud.online = False
    assert lca.sign_in(OWNER_EMAIL, "Appliance-Pass-3!")["mode"] == "offline"


def test_reclaim_to_another_customer_invalidates_the_appliance_password(env, monkeypatch):
    cloud, identity = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    monkeypatch.setattr(lca, "appliance_identity", lambda: dict(identity, customer_id="cust-2"))
    cloud.online = False
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)
    assert denied.value.reason == "invalid_offline"  # not usable: bound to the previous customer


def test_owner_can_turn_appliance_sign_in_off(env):
    cloud, _ = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    assert lca.disable(OWNER_SESSION, user_id="u-owner") == {"status": "disabled"}
    cloud.online = False
    with pytest.raises(lca.AccessDenied):
        lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)


# ------------------------------------------------------------------ HTTP: the appliance's real sign-in route
@pytest.fixture()
def edge_client(env, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    edge_settings = dataclasses.replace(cloud_config.settings, runtime_role="edge")
    monkeypatch.setattr(partner_portal, "settings", edge_settings)
    monkeypatch.setattr(cloud_config, "settings", edge_settings)
    monkeypatch.setattr(partner_portal, "_claimed_appliance", lambda: True)
    with TestClient(main.app, follow_redirects=False) as client:
        yield client, env[0]


def _login(client, email, password):
    from token_security import sign
    token = sign("csrf", 28800)
    client.cookies.set("anyaicam_csrf", token)
    return client.post("/api/partner-login", json={"email": email, "password": password, "customer_only": True},
                       headers={"X-CSRF-Token": token})


def test_http_owner_signs_in_online_then_offline_and_reaches_appliance_sign_in(edge_client):
    client, cloud = edge_client
    response = _login(client, OWNER_EMAIL, OWNER_PW)
    assert response.status_code == 303, response.text
    first = client.get("/api/customer/appliance-sign-in")
    assert first.status_code == 200 and first.json()["enrolled"] is False, (first.status_code, first.text, response.headers.get("location"))
    from token_security import sign
    token = client.cookies.get("anyaicam_csrf")
    saved = client.post("/api/customer/appliance-sign-in", json={"cloud_password": OWNER_PW, "new_password": APPLIANCE_PW},
                        headers={"X-CSRF-Token": token})
    assert saved.status_code == 200, saved.text
    assert client.get("/customer/appliance-sign-in").status_code == 200
    client.cookies.clear()
    cloud.online = False
    assert _login(client, OWNER_EMAIL, APPLIANCE_PW).status_code == 303
    assert client.get("/api/customer/appliance-sign-in").json()["enrolled"] is True


def test_http_refusals_and_lockout(edge_client):
    client, cloud = edge_client
    assert _login(client, "viewer@example.test", "Viewer-Pass-123").status_code == 403
    cloud.online = False
    first = _login(client, OWNER_EMAIL, OWNER_PW)
    assert first.status_code == 503 and "appliance password" in first.json()["detail"]
    client.cookies.clear()
    assert client.get("/api/customer/appliance-sign-in").status_code in (401, 403)  # no session, no access


def test_revocation_disables_the_cached_local_account(env):
    cloud, identity = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    lca.sign_in(OWNER_EMAIL, OWNER_PW)
    cloud.sql("UPDATE identity_grants SET revoked_at='2026-10-14' WHERE user_id='u-owner'")
    lca.reconcile(lca.fetch_verified_manifest(identity), identity)
    row = _edge_rows("SELECT approved,account_status FROM partner_users WHERE id='u-owner'")[0]
    assert (row["approved"], row["account_status"]) == (0, "revoked")


def test_an_operator_account_with_the_same_email_is_never_converted(env):
    with connection() as db:
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) "
                   "VALUES('local-admin',NULL,?,'Admin','administrator',?,1,'now')", (OWNER_EMAIL, password_hash("Local-Admin-Pass-1")))
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, OWNER_PW)
    assert denied.value.reason == "operator_email_conflict"
    assert _edge_rows("SELECT role FROM partner_users WHERE id='local-admin'")[0]["role"] == "administrator"


def test_a_revoked_grant_is_reported_as_no_longer_authorized(env):
    cloud, identity = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    cloud.sql("UPDATE identity_grants SET revoked_at='2026-10-14' WHERE user_id='u-owner'")
    lca.reconcile(lca.fetch_verified_manifest(identity), identity)
    assert lca.status("u-owner")["revoked_reason"] == "no_longer_authorized"



# ------------------------------------------------------------------ Red review of c912efe
def test_m1_a_verified_manifest_for_another_customer_revokes_everything(env):
    cloud, identity = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    with connection() as db:
        db.execute("INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) "
                   "VALUES('s-on','u-owner',?,'customer_owner','cookie','now','now','2999-01-01',99)", (OWNER_EMAIL,))
    cloud.sql("UPDATE appliances SET customer_id='cust-2' WHERE id='appl-1'")  # the cloud reassigned the appliance
    with pytest.raises(lca.Reassigned):
        lca.fetch_verified_manifest(identity)
    with pytest.raises(lca.AccessDenied) as denied:
        lca.sign_in(OWNER_EMAIL, APPLIANCE_PW)  # never falls back to the offline grace window
    assert denied.value.reason in ("appliance_reassigned", "not_owner")
    lca.revoke_all("appliance_reassigned")  # what the reconcile worker does on Reassigned
    assert lca.status("u-owner")["enrolled"] is False
    assert _edge_rows("SELECT revoked_at FROM user_sessions WHERE id='s-on'")[0]["revoked_at"]


def test_m1_the_cloud_answering_404_is_a_revocation_not_an_outage(env, monkeypatch):
    import io, urllib.error, urllib.request
    cloud, identity = env
    monkeypatch.setenv("ANYAICAM_CLOUD_URL", "http://cloud.invalid")

    def not_activated(*a, **k):
        raise urllib.error.HTTPError("http://cloud.invalid", 404, "Not Found", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(urllib.request, "urlopen", not_activated)
    with pytest.raises(lca.Reassigned):
        _REAL_REQUEST("GET", "/x", identity)


def test_m2_online_sessions_without_an_appliance_password_are_reconciled(env):
    cloud, identity = env
    lca.sign_in(OWNER_EMAIL, OWNER_PW)  # online, no appliance password
    with override_target(sqlite_path=cloud.path):
        version = _edge_rows("SELECT authorization_version v FROM partner_users WHERE id='u-owner'")[0]["v"]
    with connection() as db:
        db.execute("INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,last_seen_at,expires_at,authorization_version_at_login) "
                   "VALUES('s-online','u-owner',?,'customer_owner','cookie','now','now','2999-01-01',?)", (OWNER_EMAIL, version))
    assert lca.reconcile(lca.fetch_verified_manifest(identity), identity)["sessions_revoked_for"] == 0
    cloud.sql("UPDATE identity_grants SET revoked_at='2026-10-14' WHERE user_id='u-owner'")
    assert lca.reconcile(lca.fetch_verified_manifest(identity), identity)["sessions_revoked_for"] == 1
    assert _edge_rows("SELECT revoked_at FROM user_sessions WHERE id='s-online'")[0]["revoked_at"]
    assert _edge_rows("SELECT account_status FROM partner_users WHERE id='u-owner'")[0]["account_status"] == "revoked"


def test_a_tampered_or_expired_manifest_is_rejected_and_revokes_nothing(env, monkeypatch):
    cloud, identity = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    good = cloud.request

    def tampered(method, path, ident, payload=None):
        body = good(method, path, ident, payload)
        if path.endswith("/identity-manifest"):
            body = dict(body, identities=[])  # signature no longer matches
        return body
    monkeypatch.setattr(lca, "_request", tampered)
    with pytest.raises(lca.CloudUnavailable):
        lca.fetch_verified_manifest(identity)

    def expired(method, path, ident, payload=None):
        if path.endswith("/identity-manifest"):
            with override_target(sqlite_path=cloud.path):
                with connection() as db:
                    return appliance_identity.build_manifest(db, cloud_id=CLOUD_ID, ttl_minutes=-1)
        return good(method, path, ident, payload)
    monkeypatch.setattr(lca, "_request", expired)
    with pytest.raises(lca.CloudUnavailable):
        lca.fetch_verified_manifest(identity)
    assert lca.status("u-owner")["enrolled"] is True


def test_revoked_and_disabled_hashes_are_overwritten(env):
    from partner_db import verify_password
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    lca.disable(OWNER_SESSION, user_id="u-owner")
    assert not verify_password(APPLIANCE_PW, _edge_rows("SELECT password_hash FROM local_customer_credentials")[0]["password_hash"])


def test_offline_sign_in_runs_exactly_one_password_check(env, monkeypatch):
    import partner_db
    cloud, _ = env
    lca.enroll(OWNER_SESSION, OWNER_PW, APPLIANCE_PW, user_id="u-owner")
    cloud.online = False
    calls = []
    real = partner_db.verify_password
    monkeypatch.setattr(partner_db, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    for email, pw in ((OWNER_EMAIL, "wrong-guess-123"), ("nobody@example.test", "x-y-z-123456")):
        calls.clear()
        with pytest.raises(lca.AccessDenied):
            lca.sign_in(email, pw)
        assert len(calls) == 1


def test_a_cloud_password_change_also_bumps_the_authorization_version(env, monkeypatch, tmp_path):
    cloud, _ = env
    from token_security import sign
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    with override_target(sqlite_path=cloud.path):
        before = _edge_rows("SELECT authorization_version v FROM partner_users WHERE id='u-owner'")[0]["v"]
        with TestClient(main.app) as client:
            token = partner_portal._token(OWNER_EMAIL, "customer_owner", None, "cust-1", None)
            csrf = sign("csrf", 28800)
            client.cookies.set(partner_portal.SESSION_COOKIE, token)
            client.cookies.set("anyaicam_csrf", csrf)
            response = client.post("/api/partner/activate-account", headers={"X-CSRF-Token": csrf},
                                   json={"password": "Changed-Cloud-Pass-77", "current_password": OWNER_PW})
            assert response.status_code == 200, response.text
        after = _edge_rows("SELECT authorization_version v FROM partner_users WHERE id='u-owner'")[0]["v"]
    assert after == before + 1


def test_http_lockout_trips_to_429_including_appliance_password_setup(edge_client):
    client, cloud = edge_client
    statuses = [_login(client, OWNER_EMAIL, f"wrong-password-{i}").status_code for i in range(7)]
    assert statuses[0] == 403 and 429 in statuses
    from cloud_security import clear_login_failures
    clear_login_failures(OWNER_EMAIL)
    assert _login(client, OWNER_EMAIL, OWNER_PW).status_code == 303
    token = client.cookies.get("anyaicam_csrf")
    codes = [client.post("/api/customer/appliance-sign-in", json={"cloud_password": f"bad-cloud-{i}", "new_password": APPLIANCE_PW},
                         headers={"X-CSRF-Token": token}).status_code for i in range(7)]
    assert codes[0] == 403 and codes[-1] == 429


def test_http_new_posts_require_csrf_when_enabled(edge_client, monkeypatch):
    client, cloud = edge_client
    assert _login(client, OWNER_EMAIL, OWNER_PW).status_code == 303
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, csrf_enabled=True))
    for path in ("/api/customer/appliance-sign-in", "/api/customer/appliance-sign-in/disable"):
        assert client.post(path, json={"cloud_password": OWNER_PW, "new_password": APPLIANCE_PW}).status_code == 403


_REAL_REQUEST = lca._request
