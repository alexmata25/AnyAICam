"""Cloud side of customer sign-in on the customer's own appliance (VMS 1.2.6,
local_customer_access.py), as a minimal patch on the live cloud build:

- POST /api/appliance/{cloud_id}/authenticate-operator accepts portal
  "customer" for a customer_owner of THIS appliance's customer only;
- the new bucket never widens administrator/partner/technician sign-in;
- an unknown or de-activated appliance answers 404, never a 500 that the
  appliance would treat as an outage (and fall back to its offline path);
- a password reset bumps authorization_version (appliance sessions and
  offline appliance passwords follow it through the signed manifest).

Same isolation pattern as test_appliance_identity_integration.py.
"""
import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_cloud_customer_portal_compat.db"):
    import appliance_cloud
    import appliance_identity
    from partner_db import connection, password_hash

PASSWORD = "Sup3rSecret!-customer"


def _seed_appliance(db, appliance_id, cloud_id, credential, partner_id="partner-1", customer_id="cust-1", site_id="site-1"):
    now = "2026-10-14T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (partner_id, partner_id, "approved", "real", now))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, partner_id, customer_id, f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, partner_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (secrets.token_hex(4), appliance_id, password_hash(credential), now))


def _seed_user(db, user_id, email, role, *, partner_id="partner-1", grant_role=None, scope_type="customer", scope_id="cust-1"):
    db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,1,?)",
               (user_id, partner_id, email, "User", role, password_hash(PASSWORD), "2026-10-14T00:00:00"))
    if grant_role:
        appliance_identity.create_grant(db, user_id=user_id, role=grant_role, scope_type=scope_type, scope_id=scope_id, granted_by="test")


def _headers(appliance_id="appl-1", credential="cred-1"):
    return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer {credential}"}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "cloud_customer_portal.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _seed(db_path, *users):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_appliance(db, "appl-2", "AIC-2", "cred-2", partner_id="partner-2", customer_id="cust-2", site_id="site-2")
            for user in users:
                _seed_user(db, *user[:3], **user[3])


def _sign_in(client, email, portal, cloud_id="AIC-1", headers=None):
    return client.post(f"/api/appliance/{cloud_id}/authenticate-operator",
                       json={"email": email, "password": PASSWORD, "portal": portal}, headers=headers or _headers())


def test_the_customers_owner_gets_a_verifiable_customer_assertion(client, db_path):
    _seed(db_path, ("u-owner", "owner@cust-1.test", "customer_owner", {"grant_role": "customer_owner"}))
    response = _sign_in(client, "owner@cust-1.test", "customer")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["assertion"]["role"] == "customer_owner"
    keys = client.get("/api/appliance/signing-keys").json()["keys"]
    appliance_identity.verify_assertion(body, expected_cloud_id="AIC-1", public_keys=keys)  # does not raise


def test_a_household_viewer_is_refused_on_the_customer_portal(client, db_path):
    _seed(db_path, ("u-viewer", "viewer@cust-1.test", "customer_viewer", {"grant_role": "customer_viewer"}))
    assert _sign_in(client, "viewer@cust-1.test", "customer").json() == {"status": "denied", "reason": "not_authorized_for_selected_portal"}


def test_another_customers_owner_has_no_access_to_this_appliance(client, db_path):
    _seed(db_path, ("u-other", "owner@cust-2.test", "customer_owner", {"grant_role": "customer_owner", "partner_id": "partner-2", "scope_id": "cust-2"}))
    assert _sign_in(client, "owner@cust-2.test", "customer").json() == {"status": "denied", "reason": "not_authorized_for_this_appliance"}


def test_a_wrong_password_is_still_invalid(client, db_path):
    _seed(db_path, ("u-owner", "owner@cust-1.test", "customer_owner", {"grant_role": "customer_owner"}))
    response = client.post("/api/appliance/AIC-1/authenticate-operator",
                           json={"email": "owner@cust-1.test", "password": "wrong", "portal": "customer"}, headers=_headers())
    assert response.json() == {"status": "denied", "reason": "invalid"}


@pytest.mark.parametrize("portal", ["administrator", "partner", "technician"])
def test_a_customer_owner_never_passes_an_operator_portal(client, db_path, portal):
    _seed(db_path, ("u-owner", "owner@cust-1.test", "customer_owner", {"grant_role": "customer_owner"}))
    assert _sign_in(client, "owner@cust-1.test", portal).json() == {"status": "denied", "reason": "not_authorized_for_selected_portal"}


def test_operator_portals_are_unchanged_and_cannot_use_the_customer_portal(client, db_path):
    _seed(db_path,
          ("u-admin", "admin@anyaicam.test", "administrator", {"grant_role": "administrator", "scope_type": "global", "scope_id": None}),
          ("u-partner", "owner@partner-1.test", "partner_owner", {"grant_role": "partner_owner", "scope_type": "partner", "scope_id": "partner-1"}),
          ("u-tech", "tech@partner-1.test", "technician", {"grant_role": "technician", "scope_type": "site", "scope_id": "site-1"}))
    for email, portal, role in (("admin@anyaicam.test", "administrator", "administrator"),
                                ("owner@partner-1.test", "partner", "partner_owner"),
                                ("tech@partner-1.test", "technician", "technician")):
        ok = _sign_in(client, email, portal).json()
        assert ok["status"] == "ok" and ok["assertion"]["role"] == role, (email, ok)
        assert _sign_in(client, email, "customer").json()["status"] == "denied", email


def test_an_unknown_portal_is_still_refused(client, db_path):
    _seed(db_path, ("u-owner", "owner@cust-1.test", "customer_owner", {"grant_role": "customer_owner"}))
    assert _sign_in(client, "owner@cust-1.test", "household").json()["status"] == "denied"


def test_a_de_activated_appliance_gets_404_not_500(client, db_path, monkeypatch):
    _seed(db_path, ("u-owner", "owner@cust-1.test", "customer_owner", {"grant_role": "customer_owner"}))
    backend = appliance_identity.get_cloud_identity_backend()

    def no_appliance(**_kwargs):
        raise ValueError("No activated appliance with cloud_id='AIC-1'.")

    monkeypatch.setattr(backend, "authenticate_operator", no_appliance)
    monkeypatch.setattr(backend, "fetch_manifest", no_appliance)
    signed_in = _sign_in(client, "owner@cust-1.test", "customer")
    assert signed_in.status_code == 404 and signed_in.json()["detail"] == "This appliance is not activated."
    manifest = client.get("/api/appliance/AIC-1/identity-manifest", headers=_headers())
    assert manifest.status_code == 404 and manifest.json()["detail"] == "This appliance is not activated."


def test_the_manifest_carries_the_owner_with_its_authorization_version(client, db_path):
    _seed(db_path, ("u-owner", "owner@cust-1.test", "customer_owner", {"grant_role": "customer_owner"}))
    body = client.get("/api/appliance/AIC-1/identity-manifest", headers=_headers()).json()
    owner = next(i for i in body["manifest"]["identities"] if i["user_id"] == "u-owner") if "manifest" in body else \
        next(i for i in body["identities"] if i["user_id"] == "u-owner")
    assert "authorization_version" in owner


def test_a_password_reset_bumps_the_authorization_version(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import cloud_security
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1")
            _seed_user(db, "u-owner", "owner@cust-1.test", "customer_owner", grant_role="customer_owner")
            before = db.execute("SELECT authorization_version FROM partner_users WHERE id='u-owner'").fetchone()[0]
        token = cloud_security.create_password_reset("u-owner", "owner@cust-1.test")
        assert cloud_security.consume_password_reset(token, "a-brand-new-password-42")
        with connection() as db:
            after = db.execute("SELECT authorization_version FROM partner_users WHERE id='u-owner'").fetchone()[0]
    assert after == before + 1
