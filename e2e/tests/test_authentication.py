"""Target: authentication. Real route confirmed from source
(app/customer-login.html): GET /customer-login.html renders the form
(#login, #email, #password, button.submit), which POSTs JSON to
/api/partner-login and redirects on success -- see
test_00_framework_smoke.py for the already-passing unauthenticated half
of this page.

Runs for real once a dedicated e2e staging test-tenant login exists in
e2e/.env (see conftest.py's e2e_credentials fixture and docs/
AUTONOMOUS_VALIDATION_PERMISSIONS.md) -- confirmed live 2026-09-15,
test_valid_login_redirects_to_customer_portal passing against the real
staging login flow.
"""
import pytest
from playwright.sync_api import expect


@pytest.mark.e2e
def test_valid_login_redirects_to_customer_portal(page, base_url, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    assert "/customer-login.html" not in page.url, "expected a redirect away from the login page on success"


@pytest.mark.e2e
def test_invalid_password_shows_inline_error_not_a_redirect(page, base_url, e2e_credentials):
    email, _ = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", "deliberately-wrong-password")
    page.click("form#login button.submit")
    # The login page's own onsubmit handler awaits a fetch() before
    # showing #message -- a bare is_visible() check races that async
    # call (confirmed live 2026-09-15: failed immediately, before the
    # response could ever arrive). expect(...).to_be_visible() polls
    # with Playwright's real auto-wait instead of asserting instantly.
    expect(page.locator("#message")).to_be_visible()
    assert "/customer-login.html" in page.url


@pytest.mark.e2e
def test_logout_returns_to_a_logged_out_state(page, base_url, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    # TODO(first authenticated run): confirm the real logout control's
    # selector on the authenticated portal shell (the public marketing
    # page's own #logout button is a different, unauthenticated-context
    # element -- see customer-login.html's own script).
    pytest.skip("logout control selector not yet confirmed against a real authenticated session")
