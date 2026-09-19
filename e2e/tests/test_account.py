"""Target: the "Account" link every other customer page's topbar
carries (Investigate, Playback, Events -- href="/customer-account").
Real route confirmed from source: NOT in app/main.py (where every
other customer-portal page in this suite lives) but in
app/partner_workspace.py's customer_account() -- a legacy "Your
cameras" mini-dashboard from an earlier portal generation, still live
and still linked to from the current pages. Read-only here: the one
form on this page ("Sign out", POST /partner-logout) is deliberately
never submitted, matching this suite's established logout-button
policy (see test_authentication.py's own skipped logout test).
"""
import pytest


@pytest.fixture
def logged_in_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    return page


@pytest.mark.e2e
def test_account_link_is_present_on_investigate_and_navigates_successfully(logged_in_page):
    """Proves the real, live link every customer page's topbar shares
    isn't a dead/broken route -- not just that its href string looks
    well-formed."""
    page = logged_in_page
    page.goto("/investigate")
    page.wait_for_selector("#investigation-query")
    account_link = page.locator('a.ghost-button[href="/customer-account"]')
    assert account_link.count() == 1
    response = page.goto("/customer-account")
    assert response.status < 400, f"expected /customer-account to load successfully, got HTTP {response.status}"
    assert "AnyAiCam" in page.title()
