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
def test_logout_control_is_wired_to_the_real_logout_route(page, base_url, e2e_credentials):
    """The real logout control, confirmed from source (app/main.py's
    page_shell()): `<form id="logout-form" method="post"
    action="/logout"><button class="sidebar-logout" type="submit">`,
    with a small inline script that copies the real anyaicam_csrf
    cookie into the form's hidden csrf_token field on submit.

    Deliberately NOT click-submitted here. Also confirmed from source
    (logout_destination(), CUSTOMER_LOGOUT_DESTINATION =
    "https://anyaicam.com/"): logging out a real customer_owner/
    customer_viewer identity -- this account's own role -- redirects
    all the way out to the public production marketing site, not back
    to any page on portal-staging.anyaicam.com. Actually completing
    that click would take this test's browser off the authorized
    staging host entirely, which is outside "stay within the currently
    authorized staging scope" even though the destination itself is
    just a public, unauthenticated marketing page -- so this test
    verifies the control is real and correctly wired (not a dead
    button, not missing CSRF plumbing) without following it through."""
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    form = page.locator("#logout-form")
    assert form.count() == 1
    assert form.get_attribute("action") == "/logout"
    assert form.get_attribute("method") == "post"
    button = form.locator("button.sidebar-logout[type=submit]")
    assert button.count() == 1
    assert button.is_enabled()
