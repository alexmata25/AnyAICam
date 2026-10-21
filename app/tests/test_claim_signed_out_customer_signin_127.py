"""One-click appliance linking for a SIGNED-OUT customer (MASTER-CHECKLIST-
1.2.7.md section 1a item 3; Green's reading of ee055c05, 2026-10-20).

"Link this appliance" (link_server.py / the Windows setup) opens
<portal>/claim#code=..., which keeps the code in the tab's sessionStorage and
goes to /customer/claim-appliance?return=setup. Signed out, that page sent the
customer to /partner-login ("Partner sign in"): no Create account and no way
back to the claim. It now goes to the CUSTOMER sign-in with a fixed next= back
to the claim; Create account, email confirmation and sign-in all carry it.
Only the two claim paths are ever accepted -- never a caller-supplied target.
"""
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from test_customer_downloads import package, storage  # noqa: F401 -- fixtures
from test_direct_onboarding import PASSWORD, _login, _signup, _token, site  # noqa: F401 -- fixtures
from test_pricing_ff_commission import _stripe_maps, db_path, license_portal, portal  # noqa: F401 -- fixtures

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

CLAIM = "/customer/claim-appliance?return=setup"


def _next_of(location):
    parts = urlsplit(location)
    return parts.path, parse_qs(parts.query).get("next", [None])[0]


def test_link_entry_page_continues_to_the_claim_page(site):
    client, _, _ = site
    client.cookies.clear()
    page = client.get("/claim")
    assert page.status_code == 200
    assert "location.replace('/customer/claim-appliance?return=setup')" in page.text


@pytest.mark.parametrize("query", ["?return=setup", "", "?return=https://evil.example/", "?return=setup&next=//evil.example"])
def test_signed_out_claim_goes_to_customer_sign_in_and_back(site, query):
    """The app's authentication middleware answers first for every signed-out
    /customer/* request (main.py: customer sign-in with the whole path as one
    encoded next=); the route's own fallback (appliance_claims.py) now says the
    same instead of /partner-login. next= is always a same-origin path back to
    the claim page, and sign-in re-validates it (partner_portal)."""
    client, _, _ = site
    client.cookies.clear()
    response = client.get("/customer/claim-appliance" + query, follow_redirects=False)
    assert response.status_code == 303
    path, next_path = _next_of(response.headers["location"])
    assert path == "/customer-login.html"  # not /partner-login
    parts = urlsplit(next_path)
    assert not parts.scheme and not parts.netloc and parts.path == "/customer/claim-appliance"
    if query == "?return=setup":
        assert next_path == CLAIM  # what /claim produces: exactly what Create account accepts


def test_route_fallback_uses_customer_sign_in_with_a_fixed_next():
    """Without the middleware (bare app, or if it is ever bypassed) the route
    itself never builds next= from the request."""
    from starlette.requests import Request
    import main
    endpoint = next(r.endpoint for r in main.app.routes if getattr(r, "path", None) == "/customer/claim-appliance")
    for query, expected in ((b"return=setup", CLAIM), (b"", "/customer/claim-appliance"),
                            (b"return=https://evil.example", "/customer/claim-appliance")):
        request = Request({"type": "http", "method": "GET", "path": "/customer/claim-appliance", "query_string": query,
                           "headers": [], "app": main.app})
        response = endpoint(request)
        assert response.status_code == 303
        assert _next_of(response.headers["location"]) == ("/customer-login.html", expected)


def test_customer_sign_in_offers_create_account_that_keeps_the_claim():
    login_page = open("customer-login.html", encoding="utf-8").read()
    assert 'id="create-account-link"' in login_page and "Create an account" in login_page
    # The page's script points Create account at /customer-signup?next=<claim> only for the two claim paths.
    assert r"/^\/customer\/claim-appliance(\?return=setup)?$/.test(n)" in login_page


def test_sign_up_confirm_and_sign_in_return_to_the_claim(site, db_path):
    client, _, mail = site
    client.cookies.clear()
    signup_page = client.get("/customer-signup?next=" + quote(CLAIM, safe=""))
    assert signup_page.status_code == 200
    assert 'id="signup-claim"' in signup_page.text
    assert f'href="/customer-login.html?next={quote(CLAIM, safe="")}"' in signup_page.text  # Already have an account?
    assert f'const signupNext="{CLAIM}";' in signup_page.text

    response = client.post("/api/customer/direct-signup", json={
        "name": "Lin Owner", "email": "lin@example.test", "password": PASSWORD, "confirm_password": PASSWORD, "next": CLAIM})
    assert response.status_code == 200
    email_text = [m for m in mail if m["to"] == "lin@example.test"][-1]["text"]
    assert "&next=" + quote(CLAIM, safe="") in email_text

    ready = client.get(f"/customer/verify-email?token={_token(mail, 'lin@example.test')}&next={quote(CLAIM, safe='')}")
    assert ready.status_code == 200 and "Your account is ready" in ready.text
    assert "Sign in and link my appliance" in ready.text
    assert f'href="/customer-login.html?next={quote(CLAIM, safe="")}"' in ready.text
    assert "secure checkout" not in ready.text  # not the Build Your System wording

    signed_in = _login(client, "lin@example.test", customer_only=True, next=CLAIM)
    assert signed_in.status_code == 303 and signed_in.headers["location"] == CLAIM
    claim_page = client.get(CLAIM, follow_redirects=False)
    assert claim_page.status_code == 200 and 'id="claim-code-input"' in claim_page.text


@pytest.mark.parametrize("raw", ["//evil.example/customer/claim-appliance", "https://evil.example" + CLAIM,
                                 CLAIM + "&next=//evil.example", "/customer/claim-appliance?return=https://evil.example",
                                 "/customer/setup", "/customer/claim-appliance/../../partner"])
def test_only_the_two_claim_paths_are_carried_through_sign_up(raw):
    import direct_onboarding
    assert direct_onboarding._claim_next(raw) is None
    assert direct_onboarding.signup_next(raw) is None


def test_build_your_system_next_is_unchanged():
    import direct_onboarding
    from per_camera_billing import purchase_intent_next
    for raw in ("/customer/orders", "/order-summary?plan=ai_local&cameras=4"):
        assert direct_onboarding.signup_next(raw) == purchase_intent_next(raw)
