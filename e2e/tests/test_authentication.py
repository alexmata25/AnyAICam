"""Target: authentication. Real route confirmed from source
(app/customer-login.html): GET /customer-login.html renders the form
(#login, #email, #password, button.submit), which POSTs JSON to
/api/partner-login and redirects on success -- see
test_00_framework_smoke.py for the already-passing unauthenticated half
of this page.

Skipped until a dedicated e2e staging test-tenant login exists (see
conftest.py's e2e_credentials fixture and docs/
AUTONOMOUS_VALIDATION_PERMISSIONS.md) -- these are real, intended
scenarios, not placeholders to be rewritten later, just not yet
runnable.
"""
import pytest


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
    message = page.locator("#message")
    assert message.is_visible()
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
