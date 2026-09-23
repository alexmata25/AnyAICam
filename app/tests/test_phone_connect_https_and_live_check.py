"""Regression coverage for GET /phone-connect (2026-09-23):

Two real bugs found in a live review of the customer-facing "Connect
your phone" page on portal-staging.anyaicam.com:

1. The "Phone address" a customer is told to type into their phone came
   from `request.base_url` alone, which reflects the scheme uvicorn
   itself sees -- plain HTTP, since Caddy terminates TLS and proxies
   over the internal docker network, and uvicorn's --proxy-headers only
   trusts X-Forwarded-Proto from 127.0.0.1 by default (Caddy's container
   isn't that). Confirmed live: the page displayed
   "http://portal-staging.anyaicam.com" on a domain that is HTTPS-only
   end-to-end (ANYAICAM_HTTPS_ONLY, ANYAICAM_SECURE_COOKIES, Secure
   cookies already observed on every response). Fixed by preferring
   PUBLIC_BASE_URL (ANYAICAM_PUBLIC_URL) -- the same canonical address
   already used and already health-checked elsewhere in this codebase
   for exactly this "public HTTPS URL" purpose -- before falling back to
   request.base_url. A pure local/self-hosted deployment with neither
   ANYAICAM_PHONE_URL nor ANYAICAM_PUBLIC_URL configured is unaffected.

2. The "Connection checks" panel's "Camera stream" row was a hardcoded,
   non-dynamic literal string naming /static/hls -- the legacy local-HLS
   folder that live_relay_uploader.live_relay_worker() only ever
   populates when RUNTIME_ROLE is "edge" or "combined", never "cloud"
   (this deployment's own RUNTIME_ROLE). The real, current camera-stream
   mechanism (confirmed in live_view_page.py) is the per-session
   /api/customer/cameras/{id}/live/... live-relay/P2P/WireGuard route,
   not that static folder. A customer troubleshooting their phone
   connection had no way to know the displayed check was naming a path
   that can never contain anything on this deployment.

Same fixture idiom as test_customer_subscription_portal.py: real HTTP
through the real app (TestClient(main.app)), a throwaway sqlite DB via
override_target(), and monkeypatch.setattr(main, ...) for module-level
constants computed once at import time (PHONE_ACCESS_URL/PUBLIC_BASE_URL
are read from env vars at import, not per-request).
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_phone_connect.db"


@pytest.fixture()
def http_client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://portal-staging.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _seeded(db_path, customer_id="cust-1"):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, customer_id)
    conn.commit()
    conn.close()


# --------------------------------------------------------- phone address


def test_public_base_url_is_preferred_over_the_raw_request_derived_url(http_client, db_path, monkeypatch):
    """Proves PUBLIC_BASE_URL actually wins the fallback ordering (set to
    a URL that differs from what request.base_url would produce, so the
    two candidates are distinguishable in a test harness where TestClient
    itself always presents as https). In production this is exactly the
    http-vs-https gap: request.base_url reflects the scheme uvicorn saw
    (plain http behind Caddy's un-trusted proxy headers), while
    PUBLIC_BASE_URL is the deployment's real, configured HTTPS address."""
    monkeypatch.setattr(main, "PHONE_ACCESS_URL", "")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.anyaicam.com")
    _seeded(db_path)
    response = http_client.get("/phone-connect", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    assert '<span id="phone-url">https://app.anyaicam.com</span>' in response.text
    assert "https://portal-staging.anyaicam.com</span>" not in response.text


def test_explicit_phone_access_url_still_wins_over_public_base_url(http_client, db_path, monkeypatch):
    """Local/self-hosted deployments set ANYAICAM_PHONE_URL to a LAN or
    Tailscale address specifically because the public cloud URL (if any)
    is not what a phone on the same network should use -- this override
    must keep taking priority."""
    monkeypatch.setattr(main, "PHONE_ACCESS_URL", "http://192.168.1.50:8000")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://portal-staging.anyaicam.com")
    _seeded(db_path)
    response = http_client.get("/phone-connect", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert '<span id="phone-url">http://192.168.1.50:8000</span>' in response.text


def test_pure_local_deployment_with_neither_url_configured_is_unaffected(http_client, db_path, monkeypatch):
    """Neither env var set -- must still fall back to request.base_url,
    exactly as before this fix, with the existing localhost warning
    still doing its job."""
    monkeypatch.setattr(main, "PHONE_ACCESS_URL", "")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "")
    _seeded(db_path)
    response = http_client.get("/phone-connect", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert '<span id="phone-url">https://portal-staging.anyaicam.com</span>' in response.text


# ------------------------------------------------------- connection checks


def test_camera_stream_check_no_longer_names_the_dead_static_hls_path(http_client, db_path):
    _seeded(db_path)
    response = http_client.get("/phone-connect", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "<span>Camera stream</span><strong>/static/hls</strong>" not in html
    assert "<span>Camera stream</span><strong>Live relay session</strong>" in html


def test_other_connection_check_rows_are_unaffected(http_client, db_path, monkeypatch):
    monkeypatch.setattr(main, "SECURE_COOKIES", True)
    monkeypatch.setattr(main, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(main, "VAPID_PRIVATE_KEY", "")
    _seeded(db_path)
    response = http_client.get("/phone-connect", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    html = response.text
    assert "<span>Session cookie</span><strong>Secure</strong>" in html
    assert "<span>Push server</span><strong>Needs VAPID keys</strong>" in html
