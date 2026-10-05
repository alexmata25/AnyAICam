"""A normal sign-out or an expired session must never land a person on the
local emergency recovery sign-in (/login), 2026-10-05.

Found on portal-staging: a customer who logged out ended on
/login?next=/partner-logout ("Local emergency recovery sign-in"). Cause:
/partner-logout and /partner-login were missing from PUBLIC_PATH_PREFIXES
(/logout was listed), so whenever the request no longer carried a valid
session -- a second click, a second tab, an expired or revoked session --
authentication_middleware intercepted it before the logout handler and its
last-resort branch sent it to /login. /login itself stays reachable directly
for recovery; it is just never an automatic destination. next= values that
point at a sign-in/sign-out/recovery route are no longer honored after
sign-in either.
"""
import pathlib
import re
import sqlite3

import pytest

import main
import partner_portal
from test_cloud_customer_auth_routing import _csrf_headers, _seed_customer, db_path, http_client  # noqa: F401 -- fixtures
from test_logout_redirects import _seed_partner_session

RECOVERY = "Local emergency recovery"
APP = pathlib.Path(__file__).resolve().parents[1]


def _revoked(db_path):
    return sqlite3.connect(db_path).execute("SELECT revoked_at FROM user_sessions WHERE id='sess-1'").fetchone()[0]


# ------------------------------------------------------------ 1. customer logout

@pytest.mark.parametrize("runtime", ["cloud", "edge"])
@pytest.mark.parametrize("path", ["/partner-logout", "/logout"])
def test_customer_logout_never_lands_on_the_recovery_sign_in(http_client, db_path, monkeypatch, runtime, path):
    monkeypatch.setattr(main, "RUNTIME_ROLE", runtime)
    token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    first = http_client.post(path, cookies={partner_portal.SESSION_COOKIE: token}, headers=_csrf_headers(http_client))
    assert first.status_code == 303 and first.headers["location"] == main.CUSTOMER_LOGOUT_DESTINATION
    # The same click again (double click, second tab, back button): the
    # session is already gone, and it still must not reach /login.
    again = http_client.post(path, cookies={partner_portal.SESSION_COOKIE: token}, headers=_csrf_headers(http_client))
    assert again.status_code == 303 and again.headers["location"] == "/customer-login.html"
    customer_login = http_client.get("/customer-login.html")
    assert customer_login.status_code == 200 and RECOVERY not in customer_login.text


# ------------------------------------------------------------ 2. partner / staff logout

@pytest.mark.parametrize("role", ["partner_owner", "administrator", "technician", "salesperson"])
def test_partner_logout_goes_to_the_portal_sign_in_never_recovery(http_client, db_path, monkeypatch, role):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    token = _seed_partner_session(db_path, email=f"{role}@example.test", role=role, partner_id="partner-1")
    first = http_client.post("/partner-logout", cookies={partner_portal.SESSION_COOKIE: token})
    assert first.status_code == 303 and first.headers["location"] == "/partner.html"
    again = http_client.post("/partner-logout", cookies={partner_portal.SESSION_COOKIE: token})
    assert again.status_code == 303 and not again.headers["location"].startswith("/login")
    portal_login = http_client.get("/partner.html")
    assert portal_login.status_code == 200 and RECOVERY not in portal_login.text


@pytest.mark.parametrize("runtime", ["cloud", "edge"])
def test_signed_out_partner_login_page_is_the_normal_partner_sign_in(http_client, monkeypatch, runtime):
    monkeypatch.setattr(main, "RUNTIME_ROLE", runtime)
    response = http_client.get("/partner-login")
    assert response.status_code == 200
    assert "Partner sign in" in response.text and RECOVERY not in response.text


# ------------------------------------------------------------ 3. session expiration

@pytest.mark.parametrize("path", ["/admin-portal", "/operations", "/audit-logs", "/partner", "/settings"])
def test_expired_staff_session_on_the_cloud_goes_to_the_portal_sign_in(http_client, monkeypatch, path):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.get(path)
    assert response.status_code == 303
    assert not response.headers["location"].startswith("/login")
    assert response.headers["location"].startswith(("/partner.html?next=", "/customer-login.html?next="))


@pytest.mark.parametrize("path", ["/customer-account", "/subscription-portal", "/dashboard"])
def test_expired_customer_session_on_the_cloud_goes_to_the_customer_sign_in(http_client, monkeypatch, path):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.get(path)
    assert response.status_code == 303 and response.headers["location"].startswith("/customer-login.html?next=")


def test_an_expired_session_cookie_is_treated_as_signed_out_not_sent_to_recovery(http_client, db_path, monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    sqlite3.connect(db_path).execute("UPDATE user_sessions SET expires_at='2000-01-01T00:00:00' WHERE id='sess-1'").connection.commit()
    page = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: token})
    assert page.status_code == 303 and page.headers["location"].startswith("/customer-login.html")
    logout = http_client.post("/partner-logout", cookies={partner_portal.SESSION_COOKIE: token})
    assert logout.status_code == 303 and logout.headers["location"] == "/customer-login.html"


@pytest.mark.parametrize("path", ["/partner-logout", "/logout"])
def test_visiting_a_logout_url_never_signs_out_and_never_shows_recovery(http_client, db_path, monkeypatch, path):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    anonymous = http_client.get(path)
    assert anonymous.status_code == 303 and anonymous.headers["location"] == "/customer-login.html"
    token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    signed_in = http_client.get(path, cookies={partner_portal.SESSION_COOKIE: token})
    assert signed_in.status_code == 303 and not signed_in.headers["location"].startswith("/login")
    assert _revoked(db_path) is None  # a GET is not a sign-out


# ------------------------------------------------------------ 4. recovery stays reachable

@pytest.mark.parametrize("runtime", ["cloud", "edge"])
def test_the_recovery_sign_in_is_still_reachable_directly(http_client, monkeypatch, runtime):
    monkeypatch.setattr(main, "RUNTIME_ROLE", runtime)
    assert "/login" in main.PUBLIC_PATH_PREFIXES
    response = http_client.get("/login")
    assert response.status_code == 200 and RECOVERY in response.text


# ------------------------------------------------------------ 5. logout clears the session

@pytest.mark.parametrize("path", ["/partner-logout", "/logout"])
def test_logout_revokes_the_session_and_clears_both_cookies(http_client, db_path, path):
    token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    response = http_client.post(path, cookies={partner_portal.SESSION_COOKIE: token}, headers=_csrf_headers(http_client))
    assert response.status_code == 303
    assert _revoked(db_path) is not None
    set_cookie = response.headers.get("set-cookie", "")
    assert partner_portal.SESSION_COOKIE in set_cookie and main.SESSION_COOKIE_NAME in set_cookie
    after = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: token})
    assert after.status_code in (303, 401, 403) and not after.headers.get("location", "").startswith("/login")


# ------------------------------------------------------------ 6. redirect targets

@pytest.mark.parametrize("path,expected", [
    ("/login", True), ("/login?next=/x", True), ("/%6Cogin", True), ("/logout", True), ("/partner-logout", True),
    ("/partner-login", True), ("/customer-login.html", True), ("/partner.html#top", True), ("/forgot-password", True),
    ("/customer-reset-password?token=x", True), ("/dashboard", False), ("/subscription-portal", False),
    ("/customer/setup", False), ("/playback?camera=1&event=2", False), ("/logins-report", False),
])
def test_auth_flow_paths_are_recognized(path, expected):
    assert partner_portal.is_auth_flow_path(path) is expected


def _destination(response):
    if response.status_code == 303:
        return response.headers["location"]
    body = response.json()
    return body.get("destination") or body.get("redirect") or body.get("url")


@pytest.mark.parametrize("crafted", [
    "/login", "/login?next=/partner-logout", "/%6Cogin", "/partner-logout", "/logout", "/partner.html",
    "//evil.example/x", "/\\evil.example", "https://evil.example/phish", "/\tx",
])
def test_a_crafted_next_never_sends_a_signed_in_customer_into_another_auth_flow(http_client, db_path, crafted):
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, email="owner@example.test", password="a long enough test passphrase")
    conn.close()
    response = http_client.post("/api/partner-login", headers=_csrf_headers(http_client), json={
        "email": "owner@example.test", "password": "a long enough test passphrase", "customer_only": True, "next": crafted})
    assert response.status_code in (200, 303)
    destination = _destination(response)
    assert destination != crafted and destination.startswith("/") and not destination.startswith("//")
    assert not partner_portal.is_auth_flow_path(destination)


def test_a_normal_next_is_still_honored(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, email="owner@example.test", password="a long enough test passphrase")
    conn.close()
    response = http_client.post("/api/partner-login", headers=_csrf_headers(http_client), json={
        "email": "owner@example.test", "password": "a long enough test passphrase", "customer_only": True, "next": "/subscription-portal"})
    assert _destination(response) == "/subscription-portal"


@pytest.mark.parametrize("page", ["customer-login.html", "partner.html"])
def test_sign_in_pages_refuse_auth_flow_next_values_with_the_same_list(page):
    html = (APP / page).read_text(encoding="utf-8")
    match = re.search(r"function anyaicamAuthFlowPath\(p\)\{.*?return (\[[^\]]*\])\.some", html)
    assert match, "the sign-in page must define anyaicamAuthFlowPath"
    js_list = [item.strip("'\"") for item in match.group(1).strip("[]").split(",")]
    assert js_list == list(partner_portal.AUTH_FLOW_PATHS)
    # Every next= the page honors goes through the same check.
    assert html.count("anyaicamAuthFlowPath(") >= 2
    assert re.search(r"&&!anyaicamAuthFlowPath\((wanted|n)\)\)\?", html)
