"""Stripe Checkout return keeps the customer signed in (2026-09-30, found in
the staging Stripe TEST E2E).

After paying on checkout.stripe.com, Stripe sends the browser back to
/customer/setup?camera_plan_payment=success&... (or /subscription-portal?...).
That navigation starts on another site, and the portal session cookie was
SameSite=Strict, so the browser did not send it: the customer landed on the
sign-in page instead of setup. SameSite=Lax is sent on a top-level GET
navigation from another site but still never on a cross-site POST, so CSRF
protection for state-changing requests is unchanged (and the double-submit
anyaicam_csrf check still applies on top).

TestClient does not apply SameSite rules itself, so _browser_sends() models
what a browser does with the real Set-Cookie attributes the app returns."""
import dataclasses
import sqlite3

import pytest

import cloud_security
import main
from partner_portal import SESSION_COOKIE
from test_cloud_customer_auth_routing import _csrf_headers, _seed_customer, db_path, http_client  # noqa: F401

PASSWORD = "correct horse battery staple"
STRIPE_RETURN_URLS = [
    "/customer/setup?camera_plan_payment=success&session_id=cs_test_abc",
    "/customer/setup?analytics_addon_payment=success&session_id=cs_test_abc",
    "/subscription-portal?payment=success&session_id=cs_test_abc",
    "/subscription-portal?vms_license_payment=success&session_id=cs_test_abc",
    "/subscription-portal?hardware_payment=success&session_id=cs_test_abc",
]


def _session_set_cookie(response) -> str:
    headers = [h for h in response.headers.get_list("set-cookie") if h.startswith(f"{SESSION_COOKIE}=")]
    assert len(headers) == 1, response.headers.get_list("set-cookie")
    return headers[0]


def _samesite(set_cookie: str) -> str:
    for part in set_cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name.lower() == "samesite":
            return value.lower()
    return ""


def _browser_sends(set_cookie: str, *, cross_site: bool, method: str) -> bool:
    """RFC 6265bis: Strict is never sent on a cross-site request; Lax only on
    a cross-site top-level navigation with a safe method."""
    if not cross_site:
        return True
    samesite = _samesite(set_cookie)
    if samesite == "strict":
        return False
    if samesite == "lax":
        return method == "GET"
    return samesite == "none"


@pytest.fixture()
def signed_in(http_client, db_path, monkeypatch):  # noqa: F811
    conn = sqlite3.connect(db_path)
    _seed_customer(conn, email="owner@example.test", password=PASSWORD)
    conn.close()
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.post(
        "/api/partner-login",
        json={"email": "owner@example.test", "password": PASSWORD, "customer_only": True},
        headers=_csrf_headers(http_client),
    )
    assert response.status_code in (200, 303)
    return _session_set_cookie(response)


def test_session_cookie_is_lax_httponly(signed_in):
    assert _samesite(signed_in) == "lax"
    assert "httponly" in signed_in.lower()


def test_browser_sends_the_session_on_the_stripe_return_but_never_on_a_cross_site_post(signed_in):
    assert _browser_sends(signed_in, cross_site=True, method="GET")
    assert not _browser_sends(signed_in, cross_site=True, method="POST")


@pytest.mark.parametrize("url", STRIPE_RETURN_URLS)
def test_stripe_return_lands_on_the_page_not_sign_in(http_client, signed_in, url):  # noqa: F811
    # The session cookie is in the jar only because a browser would send it on this navigation.
    assert _browser_sends(signed_in, cross_site=True, method="GET")
    response = http_client.get(url, follow_redirects=False)
    assert response.status_code == 200, response.headers.get("location")
    assert "customer-login" not in str(response.url)


@pytest.mark.parametrize("url", STRIPE_RETURN_URLS[:1])
def test_without_the_session_the_return_still_goes_to_sign_in_and_back(http_client, monkeypatch, url):  # noqa: F811
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.get(url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/customer-login.html?next=")


def test_a_post_carrying_the_session_but_no_csrf_token_is_still_refused(http_client, signed_in, monkeypatch):  # noqa: F811
    # Lax never attaches the session to a cross-site POST (above); even if it
    # were attached, the double-submit CSRF check refuses the request.
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, csrf_enabled=True))
    http_client.cookies.delete("anyaicam_csrf")
    response = http_client.post("/api/customer/setup/progress", json={"step": 2})
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed."
