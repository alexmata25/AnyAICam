"""Regression coverage for the confirmed-live release blocker: password-
reset links were always built from settings.password_reset_url, which
defaults to http://localhost:8000/reset-password. That's correct for a
cloud deployment with one real public domain, but an edge appliance has
no such fixed address (LAN IP, Tailscale IP, mDNS name -- whatever DHCP/
Tailscale happens to assign) -- every reset link pointed at "localhost"
regardless of which real address the requester's browser was actually
using, forcing error-prone manual URL editing (swap in the real host,
keep the ~43-character token intact by hand) before every single reset.
Repeated "token invalid/expired" reports on Samsung traced back to
exactly this, with tokens independently confirmed valid, unused, and
unexpired in the database every time.

password_reset_request() (cloud_features.py) now builds the link from
the request's own Host header for edge_production specifically --
already trusted (TrustedHostMiddleware accepts any host for
edge_production) and guaranteed to be an address that actually works
for whoever just submitted the request. Cloud/combined production is
unaffected and keeps using the fixed configured URL, since reflecting
an arbitrary Host header into an outbound email link would be a real
host-header-injection risk for a deployment that has one correct answer.

Also covers the independent reflected-XSS gap found while tracing this:
/reset-password's hidden token field was inserted into an HTML attribute
unescaped (its customer-facing twin already escaped it correctly).
"""
import dataclasses
import sqlite3

import pytest
from fastapi.testclient import TestClient

import cloud_features
import main
from cloud_config import Settings
from database_backend import override_target
from partner_db import initialize_database, password_hash


STRONG_SECRET = "a" * 40


class _CapturingEmailService:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.sent.append({"type": message_type, "to": to, "subject": subject, "text": text})
        return {"id": "test-message-id", "status": "preview"}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_password_reset.db"


@pytest.fixture()
def http_client(db_path):
    # Recovery request throttling is process-local by design.  Each client
    # fixture represents a fresh application test boundary, so do not leak
    # earlier test requests into this independent scenario.
    cloud_features._password_reset_email_limiter.events.clear()
    cloud_features._password_reset_ip_limiter.events.clear()
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _seed_admin(db_path, email="admin@example.test"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES(?,?,?,?)", ("partner-1", "Partner", "approved", "2026-01-01"))
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("admin-1", "partner-1", email, "Admin", "administrator", password_hash("old-password-123"), 1, "2026-01-01"),
    )
    conn.commit()


def _seed_customer(db_path, email="customer@example.test", must_change_password=1):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES(?,?,?,?)", ("partner-1", "Partner", "approved", "2026-01-01"))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", ("cust-1", "partner-1", "Customer", email, "active", "2026-01-01"))
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,must_change_password) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("cust-user-1", "partner-1", email, "Customer", "customer_owner", password_hash("temp-password-123"), 1, "cust-1", "2026-01-01", must_change_password),
    )
    conn.commit()


def _request_and_extract_token(http_client, capturing, email):
    http_client.post("/api/password-reset/request", json={"email": email})
    text = capturing.sent[-1]["text"]
    return text.split("token=")[1].strip()


def _edge_production():
    return Settings(environment="production", runtime_role="edge", app_secrets=[STRONG_SECRET])


def _cloud_production():
    return Settings(
        environment="production", runtime_role="cloud", app_secrets=[STRONG_SECRET],
        password_reset_url="https://portal.anyaicam.com/reset-password",
        allowed_origins=["https://portal.anyaicam.com"],
    )


def test_edge_production_uses_the_requests_own_host(http_client, db_path, monkeypatch):
    _seed_admin(db_path)
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _edge_production())

    response = http_client.post(
        "/api/password-reset/request", json={"email": "admin@example.test"},
        headers={"host": "100.123.115.65:8000"},
    )

    assert response.status_code == 200
    assert len(capturing.sent) == 1
    assert "http://100.123.115.65:8000/reset-password?token=" in capturing.sent[0]["text"]
    assert "localhost" not in capturing.sent[0]["text"]


def test_edge_production_uses_whatever_host_this_particular_request_came_from(http_client, db_path, monkeypatch):
    # Different request, different address (e.g. LAN instead of Tailscale)
    # -- proves this isn't a single hard-coded address, it reflects
    # whatever the requester is actually using.
    _seed_admin(db_path)
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _edge_production())

    http_client.post(
        "/api/password-reset/request", json={"email": "admin@example.test"},
        headers={"host": "192.168.0.165:8000"},
    )

    assert "http://192.168.0.165:8000/reset-password?token=" in capturing.sent[0]["text"]


def test_cloud_production_still_uses_the_fixed_configured_url(http_client, db_path, monkeypatch):
    # Must stay exactly as before -- reflecting an arbitrary Host header
    # into a cloud deployment's reset email would be a real host-header-
    # injection risk, and cloud has one correct, fixed public domain.
    _seed_admin(db_path)
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    http_client.post(
        "/api/password-reset/request", json={"email": "admin@example.test"},
        headers={"host": "attacker.example"},
    )

    assert "https://portal.anyaicam.com/reset-password?token=" in capturing.sent[0]["text"]
    assert "attacker.example" not in capturing.sent[0]["text"]


def test_reset_page_html_escapes_the_token_query_parameter(http_client):
    # Independent reflected-XSS gap found while tracing this: the raw
    # ?token= value must never reach the page unescaped.
    response = http_client.get('/reset-password?token=x"onmouseover="alert(1)')
    assert response.status_code == 200
    assert 'onmouseover="alert(1)"' not in response.text
    assert "&quot;" in response.text or "&#34;" in response.text


# ------------------------------------------- role-aware post-reset destination
#
# Regression for a real staging report: a customer_owner account's
# successful password reset always redirected to /partner-login
# regardless of role. Independent of that page's own reachability bug
# (see test_provisioning_api.py's PUBLIC_PATH_PREFIXES coverage for the
# analogous /api/provisioning/refresh fix), /partner-login is simply the
# wrong destination for a customer account -- its login form has no
# "customer" portal option, and /partner.html's own text says customer
# accounts cannot use it. Customers must land on /customer-login.html;
# every other role lands on /partner.html (the confirmed-public,
# actively-used partner/admin/technician entry point).


def test_successful_reset_for_a_customer_account_returns_the_customer_login_destination(http_client, db_path, monkeypatch):
    _seed_customer(db_path, email="customer@example.test")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    token = _request_and_extract_token(http_client, capturing, "customer@example.test")
    response = http_client.post("/api/password-reset/complete", json={"token": token, "password": "brand-new-password-123"})

    assert response.status_code == 200
    assert response.json()["destination"] == "/customer-login.html"


def test_successful_reset_for_a_non_customer_account_returns_the_partner_login_destination(http_client, db_path, monkeypatch):
    _seed_admin(db_path, email="admin@example.test")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    token = _request_and_extract_token(http_client, capturing, "admin@example.test")
    response = http_client.post("/api/password-reset/complete", json={"token": token, "password": "brand-new-password-123"})

    assert response.status_code == 200
    assert response.json()["destination"] == "/partner.html"


def test_successful_reset_clears_must_change_password(http_client, db_path, monkeypatch):
    """The account just set a real password of its own choosing through
    this exact flow -- must_change_password (meant for an unopened
    partner-issued temporary password) must not force it through a
    second "create your permanent password" step right after logging
    in."""
    _seed_customer(db_path, email="customer@example.test", must_change_password=1)
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    token = _request_and_extract_token(http_client, capturing, "customer@example.test")
    http_client.post("/api/password-reset/complete", json={"token": token, "password": "brand-new-password-123"})

    with override_target(sqlite_path=db_path):
        row = sqlite3.connect(db_path).execute("SELECT must_change_password FROM partner_users WHERE email='customer@example.test'").fetchone()
    assert row[0] == 0


def test_an_invalid_or_expired_token_still_fails_exactly_as_before(http_client, db_path, monkeypatch):
    _seed_customer(db_path, email="customer@example.test")
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    response = http_client.post("/api/password-reset/complete", json={"token": "not-a-real-token", "password": "brand-new-password-123"})

    assert response.status_code == 400
    assert "destination" not in response.json()


def test_reset_page_has_a_show_hide_password_toggle_defaulting_to_masked(http_client):
    # UI usability addition, unrelated to the link-host/XSS fixes above:
    # matches the only existing instance of this pattern in the app
    # (partner.html's sign-in password field) -- same ids/classes/toggle
    # behavior, applied to this page's password field.
    response = http_client.get("/reset-password?token=abc123")
    assert response.status_code == 200
    text = response.text
    assert 'id="reset-password" type="password"' in text  # masked by default
    assert 'id="show-password" class="show-password" type="button">Show<' in text
    assert "input.type==='password'?'text':'password'" in text
    assert "button.textContent=input.type==='password'?'Show':'Hide'" in text
