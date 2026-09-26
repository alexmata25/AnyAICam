"""Admin portal completion pass (2026-09-26): business_portal.py's legacy
routes (account-management data, setup, sites, branding, appliances) had no
permission check at all -- any signed-in account, including a customer,
could read the stored account-management record (with its plaintext
temporary password) or overwrite the white-label branding. They now require
the same Administrator permission as the rest of the Admin Portal."""
import pytest
from fastapi.testclient import TestClient

import appliance_identity
import business_portal
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash

ROUTES = [
    ("GET", "/api/account-management", None),
    ("POST", "/api/setup/complete", {"customer_name": "X", "email": "x@example.test"}),
    ("POST", "/api/sites", {"name": "Annex"}),
    ("POST", "/api/branding", {"company_name": "Hijacked"}),
    ("GET", "/appliances", None),
    ("GET", "/branding", None),
    ("GET", "/setup-legacy", None),
    ("GET", "/pricing-legacy", None),
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(business_portal, "DATA_FILE", tmp_path / "account_management.json")
    for name in ("ANYAICAM_APPLIANCE_ID", "ANYAICAM_APPLIANCE_CLOUD_ID", "ANYAICAM_APPLIANCE_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    with override_target(sqlite_path=tmp_path / "bp.db"):
        initialize_database()
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "P", "approved", "real", "2026-09-26"))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       ("u-admin", "partner-1", "platform@anyaicam.test", "A", "administrator", password_hash("x-password-123"), 1, "2026-09-26"))
            appliance_identity.create_grant(db, user_id="u-admin", role="administrator", scope_type="global", scope_id=None, granted_by="test")
        with TestClient(main.app) as test_client:
            yield test_client


def _customer():
    return {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)}


def _admin():
    return {partner_portal.SESSION_COOKIE: partner_portal._token("platform@anyaicam.test", "administrator", None, None, None)}


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_customers_and_anonymous_visitors_are_refused(client, method, path, body):
    for cookies in ({}, _customer()):
        response = client.request(method, path, json=body, cookies=cookies, follow_redirects=False)
        assert response.status_code in (302, 303, 401, 403), (path, cookies, response.status_code)


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_platform_administrator_still_reaches_them(client, method, path, body):
    response = client.request(method, path, json=body, cookies=_admin(), follow_redirects=False)
    assert response.status_code == 200, (path, response.status_code, response.text[:200])


def test_branding_values_are_escaped(client):
    client.post("/api/branding", json={"company_name": '"><script>alert(1)</script>', "support": "<b>x</b>"}, cookies=_admin())
    page = client.get("/branding", cookies=_admin()).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<b>x</b>" not in page


def test_branding_only_accepts_known_fields(client):
    client.post("/api/branding", json={"company_name": "Demo", "evil": "x", "primary_color": "#112233"}, cookies=_admin())
    stored = business_portal.load_data()["branding"]
    assert stored["company_name"] == "Demo" and stored["primary_color"] == "#112233"
    assert "evil" not in stored
