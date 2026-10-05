"""Website-first Build Your System purchase flow (owner decision 2026-10-05).

Build Your System -> create an account (or sign in) -> email verification ->
/order-summary (outside the VMS) -> Proceed to Secure Checkout -> Stripe
Checkout -> /order-complete -> Set Up My System -> /customer/setup.

The selection (known plan, whole camera count 1-64, catalog hardware SKU,
relay count, own-PC flag) travels as a validated next= and is kept on the
account until it is paid for, so verifying, refreshing, signing out or
coming back later resumes the same order. Prices and Stripe Price IDs always
come from the server; saving the selection never touches Stripe; only the
verified webhook grants the plan. My subscription stays for existing plan
holders.
"""
import re
from urllib.parse import quote, unquote

import pytest

from database_backend import override_target
from test_direct_onboarding import (PASSWORD, _count, _direct_owner, _login, _one, _signed_in, _signup, _token,  # noqa: F401
                                    site)
from test_customer_downloads import package, storage  # noqa: F401 -- fixtures
from test_pricing_ff_commission import db_path, license_portal, portal  # noqa: F401 -- fixtures

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

PRICES = {"basic_local": "price_srv_basic", "ai_local": "price_srv_ai", "hybrid": "price_srv_hybrid"}
VERIFY_LINK = re.compile(r"/customer/verify-email\?token=[A-Za-z0-9_\-]+(&next=[^\s]+)?")
WEBSITE_QUERY = "plan=hybrid&cameras=8&appliance=AIC-APPLIANCE-RYZEN-STARTER&relays=2"


@pytest.fixture()
def shop(site, monkeypatch):
    import main
    import per_camera_billing as billing
    for plan_key, price_id in PRICES.items():
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], price_id)
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", lambda price_id, cents, interval: None)
    client, captured, mail = site
    keys = []
    real_post = main.stripe_api_post

    def _recording(path, fields, idempotency_key=None):
        if path == "/v1/checkout/sessions":
            keys.append(idempotency_key)
        return real_post(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", _recording)
    sessions = {}
    monkeypatch.setattr(main, "stripe_api_get", lambda path: sessions.get(path.rsplit("/", 1)[-1], {}))
    return client, captured, mail, keys, sessions


def _destination(response):
    if response.status_code == 303:
        return response.headers["location"]
    body = response.json()
    return body.get("destination") or body.get("redirect") or body.get("url")


def _customer_id(db_path, email):
    return _one(db_path, "SELECT id FROM customers WHERE email=?", (email,))["id"]


def _checkouts(captured):
    return [fields for fields in captured if fields.get("mode") == "subscription"]


def _order(client, query=""):
    return client.get("/order-summary" + (f"?{query}" if query else ""))


def _summary(html):
    """(plan label, cameras, monthly) as the order summary states them."""
    plan = re.search(r'id="order-plan"><span>AnyAiCam ([^<]+)</span><span>(\d+) licensed camera', html)
    monthly = re.search(r'id="order-monthly"><span>Monthly subscription</span><span>(\$[\d,.]+)/month', html)
    return (plan.group(1), int(plan.group(2)), monthly.group(1)) if plan and monthly else None


def _new_customer(client, mail, email, query=WEBSITE_QUERY):
    """Website 'Create an account' -> signup -> email link -> first sign-in."""
    next_value = "/order-summary?" + query
    page = client.get(f"/customer-signup?next={quote(next_value, safe='')}")
    assert page.status_code == 200
    response = client.post("/api/customer/direct-signup", json={
        "name": "Bea Builder", "email": email, "password": PASSWORD, "confirm_password": PASSWORD, "next": next_value})
    assert response.status_code == 200
    link = VERIFY_LINK.search([m for m in mail if m["to"] == email][-1]["text"]).group(0)
    verified = client.get(link)
    assert verified.status_code == 200
    sign_in = re.search(r'class="submit" href="(/customer-login\.html[^"]*)"', verified.text).group(1)
    wanted = unquote(sign_in.split("next=", 1)[1]) if "next=" in sign_in else None
    login = _login(client, email, customer_only=True, **({"next": wanted} if wanted else {}))
    assert _signed_in(login)
    return page, link, verified, wanted, _destination(login)


def _checkout(client, plan="hybrid", cameras=8):
    return client.post("/api/v2/customer/subscription/checkout", json={"plan_key": plan, "camera_quantity": cameras, "flow": "order"})


def _pay(db_path, customer_id, plan_key="hybrid", quantity=8, sub_id="sub_build", status="active"):
    """Stripe's subscription as the verified webhook applies it."""
    import per_camera_billing as billing
    import stripe_state
    stripe_state.subscription_payment_reversal = lambda _subscription: None
    with override_target(sqlite_path=str(db_path)):
        return billing._upsert_current({"id": sub_id, "customer": f"cus_test_{customer_id}", "status": status,
                                        "metadata": {"anyaicam_customer_id": customer_id, "anyaicam_billing_version": "2"},
                                        "current_period_end": 1800000000,
                                        "items": {"data": [{"id": "si_build", "quantity": quantity, "price": {"id": PRICES[plan_key]}}]}})


def _activate_appliance(db_path, customer_id):
    import sqlite3
    conn = sqlite3.connect(db_path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(appliances)")]
    site_id = conn.execute("SELECT id FROM sites WHERE customer_id=?", (customer_id,)).fetchone()[0]
    values = {"id": "app-build", "customer_id": customer_id, "site_id": site_id, "activation_status": "activated", "cloud_id": "cloud-build",
              "serial_number": "SN-BUILD", "name": "Build appliance", "status": "online", "created_at": "2026-10-05"}
    values = {key: value for key, value in values.items() if key in columns}
    conn.execute(f"INSERT INTO appliances({','.join(values)}) VALUES({','.join('?' * len(values))})", tuple(values.values()))
    conn.commit()
    conn.close()


def _capacity(db_path, customer_id):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(db_path)):
        return ce.usable_camera_capacity(customer_id)


# ------------------------------------------------------------ new customer journey

def test_new_customer_build_system_to_setup_end_to_end(shop, db_path):
    client, captured, mail, keys, sessions = shop
    page, link, verified, wanted, destination = _new_customer(client, mail, "new@example.test")
    canonical = "/order-summary?plan=hybrid&cameras=8&appliance=AIC-APPLIANCE-RYZEN-STARTER&relays=2"
    # Account creation and email verification carry only the validated order.
    assert f'const signupNext="{canonical}";' in page.text and 'id="signup-selection"' in page.text
    assert link.endswith("&next=" + quote(canonical, safe=""))
    assert "Sign in and continue" in verified.text and wanted == canonical
    # First sign-in returns to the order summary -- not the VMS.
    assert destination == canonical
    order = _order(client, destination.split("?", 1)[1])
    assert order.status_code == 200
    assert _summary(order.text) == ("Hybrid", 8, "$199.92")
    assert '<nav class="nav"' not in order.text and "mobile-nav" not in order.text  # outside the VMS shell
    assert "AnyAiCam Starter appliance · $1,249.99 one-time" in order.text and "× 2" in order.text
    assert "not in this online checkout" in order.text
    customer_id = _customer_id(db_path, "new@example.test")
    assert _checkouts(captured) == [] and _capacity(db_path, customer_id) == 0  # nothing bought yet
    # Proceed to Secure Checkout: the backend creates the session from server prices.
    response = _checkout(client)
    assert response.status_code == 200, response.text
    assert response.json()["checkout_url"].startswith("https://checkout.stripe.test/")
    fields = _checkouts(captured)[-1]
    assert fields["line_items[0][price]"] == PRICES["hybrid"] and fields["line_items[0][quantity]"] == "8"
    assert fields["success_url"] == "https://app.example.test/order-complete?session_id={CHECKOUT_SESSION_ID}"
    assert fields["cancel_url"] == "https://app.example.test/order-summary?checkout=cancelled"
    assert _capacity(db_path, customer_id) == 0  # no entitlement before the webhook
    # Stripe returns before the webhook: activating, no second checkout offered.
    sessions["cs_test_abcdefgh"] = {"status": "complete", "payment_status": "paid", "metadata": {"anyaicam_customer_id": customer_id}}
    waiting = client.get("/order-complete?session_id=cs_test_abcdefgh")
    assert waiting.status_code == 200 and "Activating your AnyAiCam plan" in waiting.text
    assert 'id="order-checkout"' not in waiting.text
    # The verified webhook grants the plan; the customer is handed to setup.
    assert _pay(db_path, customer_id)["status"] == "entitlement_updated"
    done = client.get("/order-complete?session_id=cs_test_abcdefgh").text
    assert "Your AnyAiCam system is ready to set up." in done
    assert '<a class="submit" id="order-setup" href="/customer/setup">Set Up My System</a>' in done
    assert "AnyAiCam Hybrid</span><span>8 licensed cameras" in done and 'id="order-hardware-reminder"' in done
    assert _capacity(db_path, customer_id) == 8
    setup = client.get("/customer/setup")
    assert setup.status_code == 200 and 'id="purchase-progress-banner"' not in setup.text
    # In the VMS before setup finishes: the next step stays visible.
    dashboard = client.get("/dashboard")
    if dashboard.status_code == 200:
        assert "Payment complete</strong> — Set up your AnyAiCam system." in dashboard.text
    _activate_appliance(db_path, customer_id)
    assert 'id="purchase-progress-banner"' not in client.get("/subscription-portal").text
    assert _one(db_path, "SELECT state FROM build_system_intents")["state"] == "setup_complete"
    assert "Your AnyAiCam plan is active" in client.get("/order-complete").text


@pytest.mark.parametrize("plan_key,label,unit", [("basic_local", "Basic Local", 999), ("ai_local", "AI Local", 1499), ("hybrid", "Hybrid", 2499)])
def test_each_plan_and_quantity_is_summarised_from_server_prices(shop, db_path, plan_key, label, unit):
    client, _, mail, _, _ = shop
    _direct_owner(client, mail, f"{plan_key}@example.test")
    for cameras in (1, 5, 8, 16, 32, 64):
        html = _order(client, f"plan={plan_key}&cameras={cameras}").text
        assert _summary(html) == (label, cameras, f"${unit * cameras / 100:,.2f}")
        assert f'data-plan="{plan_key}" data-cameras="{cameras}"' in html


def test_ai_local_five_cameras_matches_the_owner_example(shop, db_path):
    client, _, mail, _, _ = shop
    _direct_owner(client, mail, "example@example.test")
    assert _summary(_order(client, "plan=ai_local&cameras=5").text) == ("AI Local", 5, "$74.95")


@pytest.mark.parametrize("plan_key,cameras", [("basic_local", 1), ("ai_local", 5), ("hybrid", 64)])
def test_each_plan_checks_out_with_its_server_side_price(shop, db_path, plan_key, cameras):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, f"pay-{plan_key}@example.test", f"plan={plan_key}&cameras={cameras}")
    assert _checkout(client, plan_key, cameras).status_code == 200
    fields = _checkouts(captured)[-1]
    assert fields["line_items[0][price]"] == PRICES[plan_key] and fields["line_items[0][quantity]"] == str(cameras)
    assert fields["metadata[anyaicam_plan_key]"] == plan_key and fields["metadata[anyaicam_billing_version]"] == "2"


# ------------------------------------------------------------ existing account

def test_existing_account_signs_in_from_build_system_and_lands_on_the_order_summary(shop, db_path):
    client, captured, mail, _, _ = shop
    _direct_owner(client, mail, "existing@example.test")
    login = _login(client, "existing@example.test", customer_only=True, next="/order-summary?plan=ai_local&cameras=5")
    assert _destination(login) == "/order-summary?plan=ai_local&cameras=5"
    assert _summary(client.get(_destination(login)).text) == ("AI Local", 5, "$74.95")


def test_older_website_links_to_my_subscription_are_sent_to_the_order_summary(shop, db_path):
    client, _, mail, _, _ = shop
    _direct_owner(client, mail, "oldlink@example.test")
    response = client.get("/subscription-portal?plan=ai_local&cameras=5&appliance=AIC-APPLIANCE-RYZEN-STARTER&price=1")
    assert response.status_code == 303
    assert response.headers["location"] == "/order-summary?plan=ai_local&cameras=5&appliance=AIC-APPLIANCE-RYZEN-STARTER"


def test_a_plan_holder_from_build_system_manages_the_plan_in_my_subscription(shop, db_path):
    client, captured, mail, _, _ = shop
    _direct_owner(client, mail, "holder@example.test")
    customer_id = _customer_id(db_path, "holder@example.test")
    _pay(db_path, customer_id, "ai_local", 5, "sub_holder")
    _activate_appliance(db_path, customer_id)
    html = _order(client, "plan=hybrid&cameras=8").text
    assert "You already have an AnyAiCam plan" in html
    assert 'href="/subscription-portal?plan=hybrid&amp;cameras=8"' in html and 'id="order-checkout"' not in html
    # My subscription is unchanged for them: the change form, pre-filled.
    portal = client.get("/subscription-portal?plan=hybrid&cameras=8")
    assert portal.status_code == 200 and 'id="v2-change-button"' in portal.text
    assert 'id="v2-change-quantity" type="number" min="1" max="64" value="8"' in portal.text
    assert _count(db_path, "SELECT COUNT(*) FROM build_system_intents") == 0
    assert _checkout(client).status_code == 409 and _checkouts(captured) == []


def test_normal_signup_without_a_selection_is_unchanged(shop, db_path):
    client, _, mail, _, _ = shop
    page = client.get("/customer-signup")
    assert 'const signupNext="";' in page.text and 'id="signup-selection"' not in page.text
    assert _signup(client, "plain@example.test").status_code == 200
    assert "&next=" not in [m for m in mail if m["to"] == "plain@example.test"][-1]["text"]
    verified = client.get(f"/customer/verify-email?token={_token(mail, 'plain@example.test')}")
    assert 'href="/customer-login.html"' in verified.text
    assert _signed_in(_login(client, "plain@example.test", customer_only=True))
    assert client.get("/customer-account").headers.get("location") == "/customer/setup"  # unchanged landing
    assert _count(db_path, "SELECT COUNT(*) FROM build_system_intents") == 0


# ------------------------------------------------------------ malformed and crafted input

@pytest.mark.parametrize("query", ["plan=enterprise&cameras=5", "plan=HYBRID&cameras=5", "plan=&cameras=5",
                                   "plan=hybrid%00&cameras=5", "plan=hybrid&plan=ai_local&cameras=5"])
def test_a_malformed_plan_shows_the_plan_chooser_not_a_guess(shop, db_path, query):
    client, _, mail, _, _ = shop
    _direct_owner(client, mail, "badplan@example.test")
    html = _order(client, query).text
    assert "Choose your AnyAiCam plan" in html and 'id="order-checkout"' not in html
    assert _count(db_path, "SELECT COUNT(*) FROM build_system_intents") == 0


@pytest.mark.parametrize("cameras", ["0", "65", "1.5", "abc", "-1", "08", "%EF%BC%95", ""])
def test_a_malformed_camera_count_falls_back_to_one(shop, db_path, cameras):
    client, _, mail, _, _ = shop
    _direct_owner(client, mail, "badcount@example.test")
    assert _summary(_order(client, f"plan=ai_local&cameras={cameras}").text) == ("AI Local", 1, "$14.99")


@pytest.mark.parametrize("quantity", [0, 65, 1.5, "8", -1])
def test_checkout_refuses_out_of_range_or_non_integer_quantities(shop, db_path, quantity):
    client, captured, mail, _, _ = shop
    _direct_owner(client, mail, "badqty@example.test")
    response = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "ai_local", "camera_quantity": quantity, "flow": "order"})
    assert response.status_code in (400, 422) and _checkouts(captured) == []


@pytest.mark.parametrize("plan,cameras,appliance,relays,licence,expected", [
    ("hybrid", "8", None, None, None, {"plan": "hybrid", "cameras": 8}),
    ("ai_local", "64", "AIC-APPLIANCE-RYZEN-AAC-FACIAL", "3", None,
     {"plan": "ai_local", "cameras": 64, "appliance": "AIC-APPLIANCE-RYZEN-AAC-FACIAL", "relays": 3}),
    ("basic_local", "1", None, None, "1", {"plan": "basic_local", "cameras": 1, "own_pc": 1}),
    ("hybrid", "0", "AIC-RELAY-NUMATO-3CH", "0", None, {"plan": "hybrid"}),
    ("hybrid", "65", "price_evil", "17", None, {"plan": "hybrid"}),
    ("price_srv_hybrid", " 8", None, "2.5", "x", {}),
    (None, None, None, None, None, {}), (["hybrid"], 8, None, None, None, {}),
])
def test_purchase_selection_keeps_only_catalog_values(plan, cameras, appliance, relays, licence, expected):
    import per_camera_billing as billing
    assert billing.purchase_selection(plan, cameras, appliance, relays, licence) == expected


@pytest.mark.parametrize("crafted,expected", [
    ("/order-summary?plan=hybrid&cameras=8", "/order-summary?plan=hybrid&cameras=8"),
    ("/subscription-portal?plan=hybrid&cameras=8", "/order-summary?plan=hybrid&cameras=8"),
    ("/order-summary?plan=hybrid&cameras=8&price=1&price_id=price_evil", "/order-summary?plan=hybrid&cameras=8"),
    ("/order-summary", "/order-summary"),
    ("//evil.example/order-summary", None), ("https://evil.example/order-summary", None), ("/\\evil.example", None),
    ("/order-summary/../login", None), ("/login?next=/order-summary", None), ("/partner-logout", None), ("/dashboard", None),
    ("/order-complete?session_id=cs_test_x", None), ("/order-summary#x", None), ("/order-summary?\tplan=hybrid", None),
    ("javascript:alert(1)", None), ("", None), (None, None), ("/order-summary?" + "a" * 600, None),
])
def test_a_crafted_next_is_never_carried(crafted, expected):
    import per_camera_billing as billing
    assert billing.purchase_intent_next(crafted) == expected


def test_crafted_next_values_are_dropped_from_signup_email_and_verification(shop, db_path):
    client, _, mail, _, _ = shop
    page = client.get("/customer-signup?next=" + quote("https://evil.example/x", safe=""))
    assert 'const signupNext="";' in page.text and "evil" not in page.text
    client.post("/api/customer/direct-signup", json={
        "name": "Eve", "email": "eve@example.test", "password": PASSWORD, "confirm_password": PASSWORD, "next": "//evil.example/x"})
    text = [m for m in mail if m["to"] == "eve@example.test"][-1]["text"]
    assert "&next=" not in text and "evil" not in text
    verified = client.get(f"/customer/verify-email?token={_token(mail, 'eve@example.test')}&next={quote('/login', safe='')}")
    assert 'href="/customer-login.html"' in verified.text


def test_the_sign_in_page_carries_the_order_to_create_an_account():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parents[1] / "customer-login.html").read_text(encoding="utf-8")
    assert 'id="create-account-link"' in html
    assert r"/^\/(order-summary|subscription-portal)(\?[^\s\\#]*)?$/.test(n))a.href='/customer-signup?next='+encodeURIComponent(n)" in html


def test_signed_out_order_pages_go_to_the_customer_sign_in_with_the_order(shop, monkeypatch):
    import main
    client, _, _, _, _ = shop
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    client.cookies.clear()
    response = client.get("/order-summary?plan=hybrid&cameras=8")
    assert response.status_code == 303 and response.headers["location"].startswith("/customer-login.html?next=")
    assert unquote(response.headers["location"].split("next=", 1)[1]).startswith("/order-summary")


# ------------------------------------------------------------ prices and Price IDs stay server-side

def test_the_browser_cannot_choose_a_price_or_a_price_id(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "price@example.test")
    html = _order(client, "plan=hybrid&cameras=8&price_id=price_evil&unit_amount=1&monthly=1").text
    assert _summary(html) == ("Hybrid", 8, "$199.92")
    assert "price_srv_" not in html and "price_evil" not in html
    for extra in ({"price_id": "price_evil"}, {"unit_amount": 1}, {"success_url": "https://evil.example"}, {"flow": "https://evil.example"}):
        refused = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 8, **extra})
        assert refused.status_code == 422
    assert _checkouts(captured) == []
    columns = set(_one(db_path, "SELECT * FROM build_system_intents"))
    assert not any("price" in column or "amount" in column or "stripe" in column for column in columns)


# ------------------------------------------------------------ resume purchase

def test_refreshing_the_order_summary_keeps_the_order(shop, db_path):
    client, _, mail, _, _ = shop
    _new_customer(client, mail, "refresh@example.test", "plan=ai_local&cameras=16")
    assert _summary(_order(client).text) == ("AI Local", 16, "$239.84")


def test_signing_out_and_back_in_resumes_the_purchase(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "relogin@example.test", "plan=basic_local&cameras=32")
    client.post("/partner-logout")
    client.cookies.clear()
    assert _signed_in(_login(client, "relogin@example.test", customer_only=True))  # no next= this time
    landing = client.get("/customer-account")
    assert landing.status_code == 303 and landing.headers["location"] == "/order-summary"
    assert _summary(_order(client).text) == ("Basic Local", 32, "$319.68")
    assert _checkouts(captured) == []


def test_returning_later_from_another_browser_resumes_the_purchase(shop, db_path):
    client, _, mail, _, _ = shop
    _new_customer(client, mail, "later@example.test", WEBSITE_QUERY)
    client.cookies.clear()  # browser closed; nothing kept client-side
    assert _signed_in(_login(client, "later@example.test", customer_only=True))
    html = _order(client).text
    assert _summary(html) == ("Hybrid", 8, "$199.92") and 'id="order-appliance"' in html


def test_the_order_is_saved_at_verification_before_the_first_sign_in(shop, db_path):
    client, _, mail, _, _ = shop
    _new_customer(client, mail, "verified@example.test", "plan=ai_local&cameras=6&vms_licence=6")
    intent = _one(db_path, "SELECT plan_key,camera_quantity,appliance_sku,relay_modules,own_pc,state FROM build_system_intents")
    assert intent == {"plan_key": "ai_local", "camera_quantity": 6, "appliance_sku": None, "relay_modules": None, "own_pc": 1,
                      "state": "pending_checkout"}


def test_a_customer_who_wanders_into_the_vms_has_a_way_back_to_checkout(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "away@example.test")
    for path in ("/subscription-portal", "/customer/setup"):
        html = client.get(path).text
        assert html.count('id="purchase-progress-banner"') == 1, path
        assert "Setup incomplete</strong> — Complete your subscription to activate your cameras." in html
        assert '<a href="/order-summary">Continue checkout</a>' in html
    assert _checkouts(captured) == [] and _count(db_path, "SELECT COUNT(*) FROM checkout_pending") == 0


# ------------------------------------------------------------ cancelled, failed, duplicate, lost response

def test_a_cancelled_checkout_returns_to_the_same_order_and_grants_nothing(shop, db_path):
    client, _, mail, _, _ = shop
    _new_customer(client, mail, "cancel@example.test")
    assert _checkout(client).status_code == 200
    html = _order(client, "checkout=cancelled").text
    assert "Checkout was cancelled. No payment was taken and nothing was activated." in html
    assert _summary(html) == ("Hybrid", 8, "$199.92")
    assert _capacity(db_path, _customer_id(db_path, "cancel@example.test")) == 0


def test_a_failed_payment_grants_nothing(shop, db_path):
    client, _, mail, _, sessions = shop
    _new_customer(client, mail, "failed@example.test")
    customer_id = _customer_id(db_path, "failed@example.test")
    assert _checkout(client).status_code == 200
    assert _pay(db_path, customer_id, status="incomplete")["status"] == "not_granted"
    sessions["cs_test_failed01"] = {"status": "open", "payment_status": "unpaid", "metadata": {"anyaicam_customer_id": customer_id}}
    html = client.get("/order-complete?session_id=cs_test_failed01").text
    assert "Payment not completed" in html and "nothing was activated" in html
    assert _capacity(db_path, customer_id) == 0
    assert _one(db_path, "SELECT state FROM build_system_intents")["state"] == "pending_checkout"


def test_another_accounts_checkout_session_is_not_shown(shop, db_path):
    client, _, mail, _, sessions = shop
    _new_customer(client, mail, "nosy@example.test")
    sessions["cs_test_otheracct"] = {"status": "complete", "payment_status": "paid", "metadata": {"anyaicam_customer_id": "cust-other"}}
    assert client.get("/order-complete?session_id=cs_test_otheracct").status_code == 404
    assert client.get("/order-complete?session_id=not-a-session").status_code == 404


def test_duplicate_clicks_never_create_a_second_checkout_or_subscription(shop, db_path):
    client, captured, mail, keys, _ = shop
    _new_customer(client, mail, "dupe@example.test")
    customer_id = _customer_id(db_path, "dupe@example.test")
    for _ in range(3):  # refreshes create nothing in Stripe
        _order(client)
    assert _checkouts(captured) == []
    first, second = _checkout(client), _checkout(client)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["checkout_url"] == first.json()["checkout_url"] and len(_checkouts(captured)) == 1
    _pay(db_path, customer_id)
    assert _checkout(client).status_code == 409 and len(_checkouts(captured)) == 1
    _order(client, "plan=basic_local&cameras=1")  # a later website visit does not re-open the purchase
    assert _one(db_path, "SELECT plan_key,camera_quantity,state FROM build_system_intents") == {
        "plan_key": "hybrid", "camera_quantity": 8, "state": "paid_setup_pending"}
    assert _count(db_path, "SELECT COUNT(*) FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer_id,)) == 1


def test_a_lost_stripe_response_is_retried_with_the_same_idempotency_key(shop, db_path, monkeypatch):
    import main
    client, captured, mail, keys, _ = shop
    _new_customer(client, mail, "lost@example.test")
    recording = main.stripe_api_post
    lost = {"done": False}

    def _flaky(path, fields, idempotency_key=None):
        if path == "/v1/checkout/sessions" and not lost["done"]:
            lost["done"] = True
            keys.append(idempotency_key)
            raise TimeoutError("response lost after Stripe created the session")
        return recording(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", _flaky)
    with pytest.raises(TimeoutError):
        _checkout(client)
    monkeypatch.setattr(main, "stripe_api_post", recording)
    retry = _checkout(client)
    assert retry.status_code == 200
    assert len(keys) == 2 and keys[0] == keys[1]  # Stripe returns the same session, never a second one


# ------------------------------------------------------------ viewers and the existing My subscription

def test_a_viewer_cannot_buy(shop, db_path):
    import partner_portal
    client, captured, _, _, _ = shop
    viewer = partner_portal._token("viewer@example.test", "customer_viewer", None, "cust-view", None)
    response = client.get("/order-summary?plan=hybrid&cameras=8", cookies={partner_portal.SESSION_COOKIE: viewer})
    assert response.status_code == 403 and "Only the account owner" in response.text


def test_my_subscription_picker_and_checkout_are_unchanged_for_customers_without_a_build_system_order(shop, db_path):
    client, captured, mail, _, _ = shop
    _direct_owner(client, mail, "portal@example.test")
    html = client.get("/subscription-portal").text
    assert 'id="v2-checkout-button"' in html and 'id="purchase-progress-banner"' not in html
    assert client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "ai_local", "camera_quantity": 2}).status_code == 200
    fields = _checkouts(captured)[-1]
    assert fields["success_url"].startswith("https://app.example.test/subscription-portal?camera_plan_payment=success")
    assert fields["cancel_url"] == "https://app.example.test/subscription-portal?camera_plan_payment=cancelled&plan=ai_local&cameras=2"
