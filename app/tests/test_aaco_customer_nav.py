"""AACO in the customer VMS navigation (2026-10-05).

The full AACO workspace (/aaco, aaco_web.py) had no navigation entry; only the
floating Ask AACO button reached it. It is now a sidebar item between
Investigate and My subscription and a phone-navigation item after
Investigate, for customer owners/viewers, hidden when the account turned
AACO off (aaco_settings). It reuses the existing /aaco workspace; the
floating button is unchanged.
"""
import re

import pytest

import main
import partner_portal
from database_backend import override_target
from test_aaco_floating_widget import _admin_cookie, _owner_cookie, _viewer_cookie, client, db_path  # noqa: F401 -- fixtures

SIDEBAR = re.compile(r'<nav class="nav" aria-label="Primary">(.*?)</nav>', re.S)
MOBILE = re.compile(r'<nav class="mobile-nav" aria-label="Mobile">(.*?)</nav>', re.S)


def _get(client, path, cookie):
    return client.get(path, cookies={partner_portal.SESSION_COOKIE: cookie})


def _hrefs(html, pattern):
    match = pattern.search(html)
    assert match, "navigation not found"
    return re.findall(r'href="([^"]+)"', match.group(1))


def _turn_aaco_off(db_path):
    import aaco_settings
    with override_target(sqlite_path=str(db_path)):
        aaco_settings.save("cust-a", {"enabled": False}, {"email": "owner-a@example.test", "role": "customer_owner"})


# ------------------------------------------------------------ 1 + 6: sidebar (desktop)

@pytest.mark.parametrize("cookie", [_owner_cookie, _viewer_cookie])
def test_aaco_is_in_the_customer_sidebar_between_investigate_and_my_subscription(client, cookie):
    html = _get(client, "/dashboard", cookie()).text
    hrefs = _hrefs(html, SIDEBAR)
    assert hrefs.count("/aaco") == 1
    assert hrefs.index("/investigate") < hrefs.index("/aaco") < hrefs.index("/subscription-portal")
    assert re.search(r'<a [^>]*href="/aaco"[^>]*>.*?AACO', SIDEBAR.search(html).group(1), re.S)


def test_administrators_get_no_aaco_link(client):
    response = _get(client, "/dashboard", _admin_cookie())
    assert 'href="/aaco"' not in response.text


# ------------------------------------------------------------ 2: clicking opens the real workspace

@pytest.mark.parametrize("cookie", [_owner_cookie, _viewer_cookie])
def test_the_aaco_entry_opens_the_existing_workspace_and_is_marked_active(client, cookie):
    response = _get(client, "/aaco", cookie())
    assert response.status_code == 200
    html = response.text
    assert "/api/aaco/command" in html  # the real AACO workspace, not a second implementation
    assert re.search(r'<a class="[^"]*active[^"]*" href="/aaco"', SIDEBAR.search(html).group(1))
    assert 'data-aaco-embed="aaco-float"' not in html  # no floating bubble on the workspace itself


# ------------------------------------------------------------ 7: phone navigation

@pytest.mark.parametrize("cookie", [_owner_cookie, _viewer_cookie])
def test_aaco_is_in_the_phone_navigation_right_after_investigate(client, cookie):
    hrefs = _hrefs(_get(client, "/dashboard", cookie()).text, MOBILE)
    assert hrefs.count("/aaco") == 1
    assert hrefs.index("/aaco") == hrefs.index("/investigate") + 1


# ------------------------------------------------------------ 3: AACO turned off

def test_turned_off_aaco_has_no_navigation_entries_and_the_workspace_explains(client, db_path):
    _turn_aaco_off(db_path)
    html = _get(client, "/dashboard", _owner_cookie()).text
    assert "/aaco" not in _hrefs(html, SIDEBAR) and "/aaco" not in _hrefs(html, MOBILE)
    assert 'data-aaco-embed="aaco-float"' not in html  # existing behavior: no floating button either
    workspace = _get(client, "/aaco", _owner_cookie())
    assert workspace.status_code == 200 and "AACO is turned off for this account" in workspace.text


def test_a_settings_read_failure_keeps_the_entries_like_the_floating_button(client, monkeypatch):
    import aaco_settings

    def broken(_customer_id):
        raise RuntimeError("settings store unavailable")

    monkeypatch.setattr(aaco_settings, "load", broken)
    html = _get(client, "/dashboard", _owner_cookie()).text
    assert "/aaco" in _hrefs(html, SIDEBAR) and "/aaco" in _hrefs(html, MOBILE)
    assert 'data-aaco-embed="aaco-float"' in html


# ------------------------------------------------------------ 4: authentication

def test_signed_out_aaco_requires_customer_sign_in(client, monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    page = client.get("/aaco", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/customer-login.html?next=")
    command = client.post("/api/aaco/command", json={"text": "show events today"})
    assert command.status_code in (401, 403)


def test_an_administrator_cannot_open_the_customer_workspace(client):
    response = _get(client, "/aaco", _admin_cookie())
    assert response.status_code in (401, 403) or "Customer" in response.text


# ------------------------------------------------------------ 5: floating Ask AACO unchanged

@pytest.mark.parametrize("path", ["/dashboard", "/customer-live", "/playback", "/investigate", "/alerts"])
def test_the_floating_ask_aaco_button_is_still_on_customer_pages(client, path):
    html = _get(client, path, _owner_cookie()).text
    assert html.count('data-aaco-embed="aaco-float"') == 1
    assert 'id="aaco-float-toggle"' in html and html.count("/api/aaco/command") == 1
