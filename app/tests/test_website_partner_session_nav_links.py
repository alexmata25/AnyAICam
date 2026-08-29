"""Regression coverage for the confirmed-live release blocker: the
"Administration" nav link on partner.html sent an administrator to
https://portal.anyaicam.com/partner?tab=customers, which does not
resolve from an edge appliance at all. website_session()'s
public_navigation/portal_url came from settings.partner_login_url/
customer_login_url, an environment-keyed lookup that resolves to a
fixed public domain -- exactly right for cloud (one real public
domain), wrong for edge (no fixed address; reached via whatever LAN/
Tailscale host the browser is actually using). It also used role_
destination('administrator')'s fixed '/partner?tab=customers', which is
right for a partner-scoped (company-level) administrator but not a
true scope_type='global' administrator, who actually lands on /admin-
portal via the real login flow (see cloud_administrator_bridge() and
portal_login_submit() in main.py).

website_session() now builds every link from the request's own Host
header for edge_production (same pattern as cloud_features.py's
password_reset_request() fix), and checks has_global_administrator_
grant() to pick /admin-portal vs the role's normal destination for the
'administrator' role specifically -- matching where signing in would
actually send that account, without touching login/session
establishment itself.
"""
import dataclasses
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
import website_partner
from cloud_config import Settings
from database_backend import override_target
from partner_db import initialize_database, password_hash


STRONG_SECRET = "a" * 40


def _edge_production(**overrides):
    kwargs = dict(environment="production", runtime_role="edge", app_secrets=[STRONG_SECRET])
    kwargs.update(overrides)
    return Settings(**kwargs)


def _cloud_production(**overrides):
    kwargs = dict(
        environment="production", runtime_role="cloud", app_secrets=[STRONG_SECRET],
        production_partner_url="https://portal.anyaicam.com/partner.html",
        production_customer_url="https://portal.anyaicam.com/customer-login.html",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_website_session.db"


@pytest.fixture()
def http_client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _seed_partner_user(db_path, *, email, role, user_id, partner_id="partner-1"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES(?,?,?,?)", (partner_id, "Partner", "approved", "2026-01-01"))
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, partner_id, email, "User", role, password_hash("x"), 1, "2026-01-01"),
    )
    conn.commit()


def _seed_global_grant(db_path, user_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) VALUES(?,?,?,?,?,?,?,NULL)",
        ("grant-1", user_id, "administrator", "global", None, "2026-01-01", "test"),
    )
    conn.commit()


def test_edge_global_administrator_nav_link_points_to_local_admin_portal(http_client, db_path, monkeypatch):
    _seed_partner_user(db_path, email="admin@example.test", role="administrator", user_id="u-admin")
    _seed_global_grant(db_path, "u-admin")
    monkeypatch.setattr(website_partner, "settings", _edge_production())
    token = partner_portal._token("admin@example.test", "administrator", "partner-1", None, None)

    response = http_client.get("/api/website/partner-session", cookies={partner_portal.SESSION_COOKIE: token}, headers={"host": "100.123.115.65:8000"})

    assert response.status_code == 200
    data = response.json()
    assert data["navigation_label"] == "Administration"
    assert data["portal_url"] == "http://100.123.115.65:8000/admin-portal"
    assert "portal.anyaicam.com" not in data["portal_url"]


def test_edge_partner_scoped_administrator_nav_link_stays_on_partner_view(http_client, db_path, monkeypatch):
    _seed_partner_user(db_path, email="admin2@example.test", role="administrator", user_id="u-admin2")
    # No global grant -- only a partner-scoped one, or none at all; either
    # way this account is not a true platform administrator.
    monkeypatch.setattr(website_partner, "settings", _edge_production())
    token = partner_portal._token("admin2@example.test", "administrator", "partner-1", None, None)

    response = http_client.get("/api/website/partner-session", cookies={partner_portal.SESSION_COOKIE: token}, headers={"host": "192.168.0.165:8000"})

    data = response.json()
    assert data["portal_url"] == "http://192.168.0.165:8000/partner?tab=customers"


def test_edge_uses_whatever_host_this_particular_request_came_from(http_client, db_path, monkeypatch):
    _seed_partner_user(db_path, email="admin3@example.test", role="administrator", user_id="u-admin3")
    _seed_global_grant(db_path, "u-admin3")
    monkeypatch.setattr(website_partner, "settings", _edge_production())
    token = partner_portal._token("admin3@example.test", "administrator", "partner-1", None, None)

    response = http_client.get("/api/website/partner-session", cookies={partner_portal.SESSION_COOKIE: token}, headers={"host": "anyaicam-appliance.local:8000"})

    assert response.json()["portal_url"] == "http://anyaicam-appliance.local:8000/admin-portal"


def test_cloud_production_still_uses_the_fixed_configured_domain(http_client, db_path, monkeypatch):
    # Must stay exactly as before -- proves the edge fix never leaks
    # into a cloud/combined deployment.
    _seed_partner_user(db_path, email="admin4@example.test", role="administrator", user_id="u-admin4")
    _seed_global_grant(db_path, "u-admin4")
    monkeypatch.setattr(website_partner, "settings", _cloud_production())
    token = partner_portal._token("admin4@example.test", "administrator", "partner-1", None, None)

    response = http_client.get("/api/website/partner-session", cookies={partner_portal.SESSION_COOKIE: token}, headers={"host": "attacker.example"})

    data = response.json()
    assert data["portal_url"].startswith("https://portal.anyaicam.com/")
    assert "attacker.example" not in data["portal_url"]


def test_non_administrator_role_is_unaffected(http_client, db_path, monkeypatch):
    _seed_partner_user(db_path, email="owner@example.test", role="partner_owner", user_id="u-owner")
    monkeypatch.setattr(website_partner, "settings", _edge_production())
    token = partner_portal._token("owner@example.test", "partner_owner", "partner-1", None, None)

    response = http_client.get("/api/website/partner-session", cookies={partner_portal.SESSION_COOKIE: token}, headers={"host": "100.123.115.65:8000"})

    data = response.json()
    assert data["navigation_label"] == "Partner Portal"
    assert data["portal_url"] == "http://100.123.115.65:8000/partner?tab=customers"


def test_partner_html_never_overwrites_nav_links_with_an_unauthenticated_error_body():
    """Separate confirmed-live bug found while diagnosing the
    Administration link above: /api/website/partner-session returns a
    401 error body ({"status":"error",...}, no customer_url/partner_url
    keys) for a truly anonymous visitor -- the endpoint's own code has a
    graceful anonymous branch, but a global auth-middleware check blocks
    anonymous callers before ever reaching it (a separate, pre-existing
    gap, not touched here). partner.html's inline JS used to set
    #customer-nav/#partner-nav's href unconditionally from that response
    before checking anything, so a missing customer_url became the
    literal string "undefined" (JS `undefined` assigned to a DOM .href
    property) -- clicking "Customer Login" then landed on
    /login?next=/undefined, the legacy Admin Portal's emergency-recovery
    sign-in, instead of the real customer login. Fixed by guarding the
    JS to leave the correct static default hrefs alone whenever the
    fetched session shape/values aren't actually present -- a pure
    navigation-target fix; no authentication or session code changed."""
    source = (Path(__file__).resolve().parents[1] / "partner.html").read_text(encoding="utf-8")
    assert "typeof session.authenticated==='undefined')return" in source
    assert "if(session.partner_url)document.getElementById('partner-nav').href=session.partner_url" in source
    assert "if(session.customer_url)document.getElementById('customer-nav').href=session.customer_url" in source
