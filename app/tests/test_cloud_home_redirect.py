"""The cloud portal's "/" (2026-10-01): it is the appliance's own live wall
(camera1..N HLS on the host). Signed in on staging, a customer with no
cameras -- reached from Phone access's "Open cameras" -- saw duplicated
"Camera 1..8" tiles that could never connect. On the cloud, "/" now sends a
customer to their real Live page and any other identity to its portal; the
edge appliance's own page is unchanged."""
import pytest
from fastapi.testclient import TestClient

import appliance_identity
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database

NOW = "2026-10-01T00:00:00"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    with override_target(sqlite_path=tmp_path / "home.db"):
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('c1','p1','C','c1@example.test','active','real',?)", (NOW,))
            for user, role, customer in (("owner", "customer_owner", "c1"), ("viewer", "customer_viewer", "c1"), ("sales", "salesperson", None)):
                db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (user, "p1", f"{user}@example.test", user, role, "x", 1, customer, NOW, "active", "all"))
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _as(client, user, role, customer):
    client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token(f"{user}@example.test", role, None, customer, None))
    return client


@pytest.mark.parametrize("user,role", [("owner", "customer_owner"), ("viewer", "customer_viewer")])
def test_cloud_customer_goes_to_their_live_page(client, monkeypatch, user, role):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = _as(client, user, role, "c1").get("/")
    assert response.status_code == 303 and response.headers["location"] == "/customer-live"


def test_cloud_partner_identity_goes_to_its_portal(client, monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = _as(client, "sales", "salesperson", None).get("/")
    assert response.status_code == 303 and response.headers["location"] == "/partner"


def test_cloud_signed_out_still_goes_to_the_customer_sign_in(client, monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = client.get("/")
    assert response.status_code == 303 and response.headers["location"] == "/customer-login.html"


def test_the_cloud_never_renders_the_appliance_camera_wall(client, monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    for user, role, customer in (("owner", "customer_owner", "c1"), ("sales", "salesperson", None)):
        assert 'id="camera1"' not in _as(client, user, role, customer).get("/").text


def test_the_edge_appliance_home_is_unchanged(monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")

    class _Request:
        cookies = {}
        headers = {}

    page = main.home(_Request())  # the appliance's own live wall, rendered as before
    assert isinstance(page, str) and page.startswith("<!doctype html>") and "My cameras" in page
