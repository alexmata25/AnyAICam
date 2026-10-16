"""Hardware-first checkout (owner decision 2026-10-15).

Build Your System -> pay for the hardware (its own Stripe Checkout, server
prices, no subscription, no saved card) -> verified webhook records the paid
order and emails the confirmation -> create the account (or sign in) -> order
status page -> after delivery, a separate subscription checkout. A guest order
is linked to an account only on proof of the order email, never by email
match alone.
"""
import json
import re
import sqlite3
from urllib.parse import quote

import pytest
from fastapi import HTTPException

from database_backend import override_target
from test_direct_onboarding import PASSWORD, _login, _one, _signed_in, site  # noqa: F401 -- fixtures
from test_pricing_ff_commission import db_path, license_portal, portal  # noqa: F401 -- fixtures

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

HW_PRICES = {"ANYAICAM_STRIPE_PRICE_RYZEN_STARTER": "price_hw_starter", "ANYAICAM_STRIPE_PRICE_RYZEN_ENTERPRISE": "price_hw_pro",
             "ANYAICAM_STRIPE_PRICE_RYZEN_AAC_FACIAL": "price_hw_aac", "ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH": "price_hw_relay"}
PLAN_PRICES = {"basic_local": "price_srv_basic", "ai_local": "price_srv_ai", "hybrid": "price_srv_hybrid"}
CATALOG_CENTS = {"price_hw_starter": 124999, "price_hw_pro": 174999, "price_hw_aac": 224999, "price_hw_relay": 14999}
ORDER = {"appliance": "AIC-APPLIANCE-RYZEN-STARTER", "relays": 2, "cameras": 8, "plan": "hybrid"}
ORDER_QUERY = "appliance=AIC-APPLIANCE-RYZEN-STARTER&relays=2&cameras=8&plan=hybrid"
VERIFY_LINK = re.compile(r"/customer/verify-email\?token=[A-Za-z0-9_\-]+(&next=[^\s]+)?")


@pytest.fixture()
def hw(site, license_portal, monkeypatch):
    """The app with Stripe doubles: Checkout Sessions get realistic ids,
    expirations are recorded, and line items / sessions are readable."""
    import main
    import per_camera_billing as billing
    import hardware_checkout
    client, captured, mail = site
    _, _, sent = license_portal  # purchase notifications (order confirmations)
    for env_var, price_id in HW_PRICES.items():
        monkeypatch.setenv(env_var, price_id)
    for plan_key, price_id in PLAN_PRICES.items():
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], price_id)
    checked = []

    def catalog_check(price_id, cents, interval):
        checked.append((price_id, cents, interval))
        if interval is None and CATALOG_CENTS.get(price_id) != cents:
            raise HTTPException(status_code=503, detail="PRICE_MISMATCH")
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", catalog_check)
    hardware_checkout._create_limiter.events.clear()
    state = {"sessions": {}, "expired": [], "keys": []}
    inner_post = main.stripe_api_post

    def post(path, fields, idempotency_key=None):
        if path.endswith("/expire"):
            state["expired"].append(path.split("/")[4])
            return {"id": path.split("/")[4], "status": "expired"}
        if path == "/v1/checkout/sessions" and dict(fields).get("metadata[anyaicam_build_order]") == "hardware_only":
            state["keys"].append(idempotency_key)
            captured.append(dict(fields))
            session_id = f"cs_test_hw{len(state['sessions']) + 1:06d}"
            state["sessions"][session_id] = {"id": session_id, "status": "open", "metadata": {
                k[9:-1]: v for k, v in fields if k.startswith("metadata[")}, "fields": dict(fields)}
            return {"id": session_id, "url": f"https://checkout.stripe.test/{session_id}"}
        return inner_post(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", post)
    inner_get = main.stripe_api_get

    def get(path):
        if path.startswith("/v1/checkout/sessions/"):
            session_id = path.split("/")[4].split("?")[0]
            session = state["sessions"].get(session_id)
            if session and path.split("?")[0].endswith("/line_items"):
                fields = session["fields"]
                data, index = [], 0
                while f"line_items[{index}][price]" in fields:
                    price, quantity = fields[f"line_items[{index}][price]"], int(fields[f"line_items[{index}][quantity]"])
                    data.append({"price": {"id": price}, "quantity": quantity, "amount_total": CATALOG_CENTS[price] * quantity})
                    index += 1
                return {"data": data}
            if session:
                return {k: v for k, v in session.items() if k != "fields"}
        return inner_get(path)
    monkeypatch.setattr(main, "stripe_api_get", get)
    return {"client": client, "captured": captured, "mail": mail, "sent": sent, "stripe": state, "checked": checked}


def _sessions(hw):
    return [fields for fields in hw["captured"] if fields.get("metadata[anyaicam_build_order]") == "hardware_only"]


def _buy(hw, email="buyer@example.test", **overrides):
    return hw["client"].post("/api/hardware-checkout", json={**ORDER, "email": email, **overrides})


def _paid_event(hw, session_id, *, event_type="checkout.session.completed", payment_status="paid", email="buyer@example.test",
                event_id=None):
    session = hw["stripe"]["sessions"][session_id]
    session["status"] = "complete"
    obj = {"id": session_id, "object": "checkout.session", "mode": "payment", "payment_status": payment_status, "status": "complete",
           "customer": "cus_guest_1", "payment_intent": f"pi_{session_id}", "amount_total": 154997,
           "customer_details": {"email": email, "phone": "+13465550100"},
           "collected_information": {"shipping_details": {"name": "Bea Buyer", "address": {
               "line1": "1 Main St", "city": "Houston", "state": "TX", "postal_code": "77002", "country": "US"}}},
           "metadata": dict(session["metadata"])}
    return {"id": event_id or f"evt_{session_id}_{event_type}", "type": event_type, "livemode": False, "data": {"object": obj}}


def _record(db_path, event):
    import hardware_checkout
    with override_target(sqlite_path=str(db_path)):
        return hardware_checkout.sync_from_stripe_event(event)


def _orders(db_path, session_id):
    return sqlite3.connect(db_path).execute(
        "SELECT sku,quantity,status,fulfillment_status,amount_cents,customer_id FROM hardware_orders "
        "WHERE stripe_checkout_session_id=? ORDER BY sku", (session_id,)).fetchall()


def _signup_and_verify(hw, email, next_path=None):
    client, mail = hw["client"], hw["mail"]
    body = {"name": "Bea Buyer", "email": email, "password": PASSWORD, "confirm_password": PASSWORD}
    if next_path:
        body["next"] = next_path
    assert client.post("/api/customer/direct-signup", json=body).status_code == 200
    link = VERIFY_LINK.search([m for m in mail if m["to"] == email][-1]["text"]).group(0)
    return client.get(link)


def _customer_id(db_path, email):
    return _one(db_path, "SELECT id FROM customers WHERE email=?", (email,))["id"]


# ------------------------------------------------------------ the checkout page and session

def test_the_hardware_page_prices_from_the_server_and_says_the_subscription_comes_later(hw):
    page = hw["client"].get(f"/hardware-checkout?{ORDER_QUERY}")
    assert page.status_code == 200
    html = page.text
    assert "AnyAiCam Starter</span><span>$1,249.99</span>" in html
    assert "Numato 3-Channel Relay Module × 2</span><span>$299.98</span>" in html
    assert 'id="hw-total"><span>Due today</span><span>$1,549.97</span>' in html
    assert ("Your AnyAiCam hardware is purchased today. Your camera subscription will be activated separately after your "
            "equipment arrives and you complete setup.") in html
    assert "not charged today" in html and "$199.92" not in html  # the plan is not part of this order
    assert 'id="hw-email"' in html and "Pay for hardware" in html


@pytest.mark.parametrize("query", ["", "appliance=AIC-RELAY-NUMATO-3CH", "appliance=NOPE", "appliance=AIC-APPLIANCE-RYZEN-STARTER&relays=17"])
def test_no_appliance_no_hardware_checkout(hw, query):
    assert hw["client"].get(f"/hardware-checkout?{query}").status_code == 400


def test_guest_checkout_is_a_one_time_hardware_payment_with_shipping(hw):
    response = _buy(hw)
    assert response.status_code == 200, response.text
    assert response.json()["checkout_url"] == "https://checkout.stripe.test/cs_test_hw000001"
    fields = _sessions(hw)[-1]
    assert fields["mode"] == "payment"
    assert (fields["line_items[0][price]"], fields["line_items[0][quantity]"]) == ("price_hw_starter", "1")
    assert (fields["line_items[1][price]"], fields["line_items[1][quantity]"]) == ("price_hw_relay", "2")
    assert "line_items[2][price]" not in fields
    # Nothing recurring: no plan price, no subscription, no card saved for later charges.
    assert not set(PLAN_PRICES.values()) & set(fields.values())
    assert not any(key.startswith("subscription_data") or "setup_future_usage" in key for key in fields)
    assert fields["shipping_address_collection[allowed_countries][0]"] == "US"
    assert fields["phone_number_collection[enabled]"] == "true"
    assert (fields["customer_email"], fields["customer_creation"]) == ("buyer@example.test", "always")
    assert "customer" not in fields and "metadata[anyaicam_customer_id]" not in fields
    assert fields["metadata[anyaicam_hardware_items]"] == "AIC-APPLIANCE-RYZEN-STARTER:1,AIC-RELAY-NUMATO-3CH:2"
    assert (fields["metadata[anyaicam_intended_plan]"], fields["metadata[anyaicam_intended_cameras]"]) == ("hybrid", "8")
    assert fields["success_url"] == "https://app.example.test/hardware-order/complete?session_id={CHECKOUT_SESSION_ID}"
    assert fields["cancel_url"] == f"https://app.example.test/hardware-checkout?{ORDER_QUERY}&checkout=cancelled"
    assert fields["allow_promotion_codes"] == "false"
    assert hw["stripe"]["keys"][-1].startswith("anyaicam-hardware-checkout-")
    assert ("price_hw_starter", 124999, None) in hw["checked"] and ("price_hw_relay", 14999, None) in hw["checked"]


def test_a_test_price_or_missing_price_refuses_checkout(hw, monkeypatch):
    monkeypatch.setitem(CATALOG_CENTS, "price_hw_starter", 50)  # a $0.50 staging price behind the Price ID
    refused = _buy(hw)
    assert refused.status_code == 503 and refused.json()["detail"] == "PRICE_MISMATCH"
    monkeypatch.setitem(CATALOG_CENTS, "price_hw_starter", 124999)
    monkeypatch.delenv("ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH")
    missing = _buy(hw)
    assert missing.status_code == 503 and "not available yet" in missing.json()["detail"]
    assert _sessions(hw) == []


@pytest.mark.parametrize("body", [
    {**ORDER, "email": "buyer@example.test", "price": 1},                       # the browser cannot send a price
    {**ORDER, "email": "buyer@example.test", "appliance": "AIC-RELAY-NUMATO-3CH"},
    {**ORDER, "email": "buyer@example.test", "relays": 17},
    {**ORDER, "email": "not-an-email"},
    {**ORDER},
])
def test_the_browser_sends_only_valid_choices(hw, body):
    assert hw["client"].post("/api/hardware-checkout", json=body).status_code == 422
    assert _sessions(hw) == []


def test_a_new_checkout_for_the_same_email_closes_the_unpaid_one(hw):
    assert _buy(hw).status_code == 200 and _buy(hw).status_code == 200
    assert hw["stripe"]["expired"] == ["cs_test_hw000001"]
    assert _buy(hw, email="someone-else@example.test").status_code == 200
    assert hw["stripe"]["expired"] == ["cs_test_hw000001"]


def test_checkout_creation_is_rate_limited(hw):
    codes = [_buy(hw, email=f"r{n}@example.test").status_code for n in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


# ------------------------------------------------------------ payment confirmation

def test_only_the_verified_paid_webhook_records_the_order_once_and_emails_once(hw, db_path):
    _buy(hw)
    session_id = "cs_test_hw000001"
    # Back from Stripe (session complete) before the webhook: the page waits, nothing is recorded.
    hw["stripe"]["sessions"][session_id]["status"] = "complete"
    waiting = hw["client"].get(f"/hardware-order/complete?session_id={session_id}")
    assert waiting.status_code == 200 and "Confirming your order" in waiting.text
    assert _orders(db_path, session_id) == []
    event = _paid_event(hw, session_id)
    assert _record(db_path, event)["status"] == "hardware_order_recorded"
    assert _record(db_path, event)["status"] == "hardware_order_recorded"  # redelivery
    assert _orders(db_path, session_id) == [("AIC-APPLIANCE-RYZEN-STARTER", 1, "paid", "paid", 124999, None),
                                            ("AIC-RELAY-NUMATO-3CH", 2, "paid", "paid", 29998, None)]
    checkout = _one(db_path, "SELECT * FROM hardware_checkouts WHERE stripe_checkout_session_id=?", (session_id,))
    assert checkout["status"] == "paid" and checkout["customer_id"] is None and checkout["amount_total_cents"] == 154997
    assert json.loads(checkout["shipping_json"])["city"] == "Houston"
    confirmations = [m for m in hw["sent"] if m["subject"].endswith("Your AnyAiCam hardware order is confirmed")]
    assert len(confirmations) == 1 and confirmations[0]["to"] == "buyer@example.test"
    assert confirmations[0]["type"] == "hardware_order_confirmation"
    assert "Your AnyAiCam hardware is purchased today." in confirmations[0]["text"]
    assert f"/hardware-order/complete?session_id={session_id}" in confirmations[0]["text"]
    done = hw["client"].get(f"/hardware-order/complete?session_id={session_id}").text
    assert "Payment complete · Order confirmed" in done and "AnyAiCam Starter" in done
    assert 'id="hw-signup"' in done and "/customer-signup?next=%2Fcustomer%2Forders" in done
    assert len([m for m in hw["sent"] if m["subject"].endswith("hardware order is confirmed")]) == 1  # no resend
    # Nothing recurring was created anywhere.
    assert _one(db_path, "SELECT COUNT(*) AS n FROM deferred_storage_plans")["n"] == 0


def test_an_unpaid_or_delayed_payment_is_not_an_order_until_it_clears(hw, db_path):
    _buy(hw)
    session_id = "cs_test_hw000001"
    assert _record(db_path, _paid_event(hw, session_id, payment_status="unpaid"))["status"] == "not_granted"
    assert _orders(db_path, session_id) == []
    assert _record(db_path, _paid_event(hw, session_id, event_type="checkout.session.async_payment_succeeded"))["status"] == \
        "hardware_order_recorded"
    assert len(_orders(db_path, session_id)) == 2


def test_failed_and_expired_checkouts_create_nothing(hw, db_path):
    _buy(hw)
    _buy(hw, email="other@example.test")
    assert _record(db_path, _paid_event(hw, "cs_test_hw000001", event_type="checkout.session.async_payment_failed",
                                        payment_status="unpaid"))["status"] == "closed"
    assert _record(db_path, _paid_event(hw, "cs_test_hw000002", event_type="checkout.session.expired",
                                        payment_status="unpaid"))["status"] == "closed"
    assert _one(db_path, "SELECT COUNT(*) AS n FROM hardware_orders")["n"] == 0
    states = dict(sqlite3.connect(db_path).execute("SELECT stripe_checkout_session_id,status FROM hardware_checkouts").fetchall())
    assert states == {"cs_test_hw000001": "payment_failed", "cs_test_hw000002": "expired"}
    hw["stripe"]["sessions"]["cs_test_hw000002"]["status"] = "expired"
    page = hw["client"].get("/hardware-order/complete?session_id=cs_test_hw000002").text
    assert "Payment not completed" in page


def test_a_forged_or_unknown_session_is_not_shown(hw, db_path):
    assert hw["client"].get("/hardware-order/complete?session_id=cs_test_unknown0001").status_code == 404
    assert hw["client"].get("/hardware-order/complete?session_id=../../etc").status_code == 404


def test_the_real_webhook_route_runs_the_hardware_step(hw, db_path):
    _buy(hw)
    event = _paid_event(hw, "cs_test_hw000001", event_id="evt_route_hw")
    response = hw["client"].post("/api/payments/stripe/webhook", content=json.dumps(event),
                                 headers={"Stripe-Signature": "t=1,v1=test", "Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    assert len(_orders(db_path, "cs_test_hw000001")) == 2
    assert _one(db_path, "SELECT status FROM stripe_webhook_steps WHERE event_id='evt_route_hw' AND step='hardware_checkout'")["status"] == "completed"


# ------------------------------------------------------------ account and linking

def test_a_guest_creates_the_account_with_the_order_email_and_sees_the_order(hw, db_path):
    _buy(hw)
    _record(db_path, _paid_event(hw, "cs_test_hw000001"))
    verified = _signup_and_verify(hw, "buyer@example.test", next_path="/customer/orders")
    assert "Sign in to see your hardware order" in verified.text and "next=%2Fcustomer%2Forders" in verified.text
    customer_id = _customer_id(db_path, "buyer@example.test")
    assert {row[5] for row in _orders(db_path, "cs_test_hw000001")} == {customer_id}
    assert _signed_in(_login(hw["client"], "buyer@example.test", customer_only=True))
    orders = hw["client"].get("/customer/orders").text
    assert ("Your AnyAiCam system has been ordered. Your equipment is being prepared for shipment. You do not need to "
            "activate or pay for your camera subscription yet. Return here when your equipment arrives.") in orders
    assert orders.count('class="hw-status">Paid<') == 2
    # No camera plan was started, and the account can buy one later in its own checkout.
    import per_camera_billing as billing
    with override_target(sqlite_path=str(db_path)):
        assert not billing.has_camera_plan(customer_id)
    assert _one(db_path, "SELECT COUNT(*) AS n FROM deferred_storage_plans")["n"] == 0


def test_an_order_is_never_linked_to_an_existing_account_by_email_alone(hw, db_path):
    _signup_and_verify(hw, "owner@example.test")
    customer_id = _customer_id(db_path, "owner@example.test")
    hw["client"].cookies.clear()
    _buy(hw, email="owner@example.test")  # someone pays as a guest with that address
    _record(db_path, _paid_event(hw, "cs_test_hw000001", email="owner@example.test"))
    assert {row[5] for row in _orders(db_path, "cs_test_hw000001")} == {None}
    page = hw["client"].get("/hardware-order/complete?session_id=cs_test_hw000001").text
    assert 'id="hw-sign-in-to-link"' in page and "next=%2Fhardware-order%2Fcomplete%3Fsession_id%3Dcs_test_hw000001" in page
    # Signing in to that account from the order's own page (proof: the session id) links this order.
    assert _signed_in(_login(hw["client"], "owner@example.test", customer_only=True))
    assert 'id="orders-none"' in hw["client"].get("/customer/orders").text  # the orders page alone does not link
    assert {row[5] for row in _orders(db_path, "cs_test_hw000001")} == {None}
    linked = hw["client"].get("/hardware-order/complete?session_id=cs_test_hw000001").text
    assert 'id="hw-view-order"' in linked
    assert {row[5] for row in _orders(db_path, "cs_test_hw000001")} == {customer_id}


def test_another_account_cannot_claim_a_guest_order(hw, db_path):
    _buy(hw, email="buyer@example.test")
    _record(db_path, _paid_event(hw, "cs_test_hw000001"))
    _signup_and_verify(hw, "stranger@example.test")
    assert _signed_in(_login(hw["client"], "stranger@example.test", customer_only=True))
    page = hw["client"].get("/hardware-order/complete?session_id=cs_test_hw000001").text
    assert 'id="hw-other-account"' in page
    assert {row[5] for row in _orders(db_path, "cs_test_hw000001")} == {None}


def test_a_signed_in_owner_buys_into_the_account(hw, db_path):
    _signup_and_verify(hw, "member@example.test")
    customer_id = _customer_id(db_path, "member@example.test")
    assert _signed_in(_login(hw["client"], "member@example.test", customer_only=True))
    page = hw["client"].get(f"/hardware-checkout?{ORDER_QUERY}").text
    assert "Ordering as member@example.test" in page
    assert hw["client"].post("/api/hardware-checkout", json={**ORDER, "email": "ignored@example.test"}).status_code == 200
    fields = _sessions(hw)[-1]
    assert fields["metadata[anyaicam_customer_id]"] == customer_id and fields["customer"].startswith("cus_test_")
    assert "customer_email" not in fields
    _record(db_path, _paid_event(hw, "cs_test_hw000001", email="member@example.test"))
    assert {row[5] for row in _orders(db_path, "cs_test_hw000001")} == {customer_id}
    assert 'id="hw-view-order"' in hw["client"].get("/hardware-order/complete?session_id=cs_test_hw000001").text


# ------------------------------------------------------------ delivery, then the subscription

def test_order_status_follows_fulfillment_and_the_plan_is_offered_after_delivery(hw, db_path):
    _buy(hw)
    _record(db_path, _paid_event(hw, "cs_test_hw000001"))
    _signup_and_verify(hw, "buyer@example.test", next_path="/customer/orders")
    assert _signed_in(_login(hw["client"], "buyer@example.test", customer_only=True))
    for fulfillment, label in (("preparing", "Preparing"), ("testing", "Preparing"), ("shipped", "Shipped"), ("delivered", "Delivered")):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE hardware_orders SET fulfillment_status=? WHERE sku LIKE 'AIC-APPLIANCE-%'", (fulfillment,))
        conn.commit()
        conn.close()
        html = hw["client"].get("/customer/orders").text
        assert f'data-status="{label}"' in html, fulfillment
        assert ('id="orders-choose-plan"' in html) == (label == "Delivered"), fulfillment
    # The separate subscription checkout is available to this account.
    response = hw["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 4})
    assert response.status_code == 200, response.text
    subscription = [f for f in hw["captured"] if f.get("mode") == "subscription"][-1]
    assert subscription["line_items[0][quantity]"] == "4"


def test_old_order_summary_links_with_an_appliance_go_to_the_hardware_checkout(hw, db_path):
    _signup_and_verify(hw, "old@example.test")
    assert _signed_in(_login(hw["client"], "old@example.test", customer_only=True))
    response = hw["client"].get(f"/order-summary?plan=hybrid&cameras=8&appliance=AIC-APPLIANCE-RYZEN-STARTER&relays=2")
    assert response.status_code == 303 and response.headers["location"] == f"/hardware-checkout?{ORDER_QUERY}"
    own_pc = hw["client"].get("/order-summary?plan=ai_local&cameras=6&vms_licence=6")  # software-only path unchanged
    assert own_pc.status_code == 200 and 'id="order-checkout"' in own_pc.text


def test_orders_page_requires_sign_in():
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app, follow_redirects=False) as client:
        response = client.get("/customer/orders")
    assert response.status_code == 303
    assert response.headers["location"] in ("/customer-login.html?next=%2Fcustomer%2Forders", "/customer-login.html?next=/customer/orders")


@pytest.mark.parametrize("raw,expected", [("/customer/orders", "/customer/orders"), ("/customer/orders?x=1", None),
                                          ("//evil/customer/orders", None), ("/customer/orders/../admin", None)])
def test_only_the_exact_orders_path_is_carried_through_signup(raw, expected):
    import per_camera_billing as billing
    assert billing.purchase_intent_next(raw) == expected


def test_my_subscription_checkout_errors_show_next_to_the_button(hw, db_path):
    _signup_and_verify(hw, "plan@example.test")
    assert _signed_in(_login(hw["client"], "plan@example.test", customer_only=True))
    html = hw["client"].get("/subscription-portal").text
    button = html.index('id="v2-checkout-button"')
    assert 0 < html.index('id="v2-checkout-message"') - button < 200
    assert "addEventListener('pageshow'" in html and "Array.isArray(d)" in html and "(error '+response.status+')" in html


def test_a_guest_without_an_account_still_gets_the_shipping_email(hw, db_path):
    _buy(hw)
    _record(db_path, _paid_event(hw, "cs_test_hw000001"))
    order_id = _one(db_path, "SELECT id FROM hardware_orders WHERE sku='AIC-APPLIANCE-RYZEN-STARTER'")["id"]
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET fulfillment_status='shipped',carrier='UPS',tracking_number='1Z999' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    import purchase_notifications
    with override_target(sqlite_path=str(db_path)):
        assert purchase_notifications.notify_hardware_shipped(order_id)["status"] == "sent"
    shipped = [m for m in hw["sent"] if m["type"] == "hardware_shipped"]
    assert len(shipped) == 1 and shipped[0]["to"] == "buyer@example.test"


# ------------------------------------------------------------ 1.2.7 journey review (2026-10-15): My subscription while hardware is pending

def _hardware_owner_with_paid_order(hw, db_path, email="pending@example.test"):
    _buy(hw, email=email)
    _record(db_path, _paid_event(hw, "cs_test_hw000001", email=email))
    _signup_and_verify(hw, email, next_path="/customer/orders")
    assert _signed_in(_login(hw["client"], email, customer_only=True))
    return _one(db_path, "SELECT id FROM customers WHERE email=?", (email,))["id"]


def test_my_subscription_says_no_plan_is_needed_while_the_hardware_is_on_its_way(hw, db_path):
    _hardware_owner_with_paid_order(hw, db_path)
    html = hw["client"].get("/subscription-portal").text
    assert "You don&#x27;t need to choose or pay for a camera plan until your equipment arrives" in html \
        or "You don't need to choose or pay for a camera plan until your equipment arrives" in html
    assert 'href="/customer/orders"' in html
    # Starting early stays possible but is folded away and says when billing starts.
    assert '<details id="v2-checkout-early"><summary>Start my camera plan now' in html and "billing starts when you complete checkout" in html
    assert "<h2>Choose a per-camera plan</h2>" not in html
    assert "scrollIntoView({block:'start'})" not in html  # no jump to the picker


def test_the_plan_picker_is_prefilled_from_the_hardware_order(hw, db_path):
    _hardware_owner_with_paid_order(hw, db_path, "prefill@example.test")
    html = hw["client"].get("/subscription-portal").text
    assert '<option value="hybrid" selected>' in html and 'id="v2-quantity" type="number" min="1" max="64" value="8"' in html
    assert "Selected in Build Your System: Hybrid · 8 cameras" in html


def test_after_delivery_the_normal_plan_picker_returns(hw, db_path):
    _hardware_owner_with_paid_order(hw, db_path, "arrived@example.test")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET fulfillment_status='delivered' WHERE sku LIKE 'AIC-APPLIANCE-%'")
    conn.commit()
    conn.close()
    html = hw["client"].get("/subscription-portal").text
    assert "<h2>Choose a per-camera plan</h2>" in html and 'id="v2-checkout-early"' not in html
    assert "until your equipment arrives" not in html
    assert '<option value="hybrid" selected>' in html


def test_the_prefill_never_overrides_the_customers_own_choice(hw, db_path):
    _hardware_owner_with_paid_order(hw, db_path, "ownchoice@example.test")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hardware_orders SET fulfillment_status='delivered' WHERE sku LIKE 'AIC-APPLIANCE-%'")
    conn.commit()
    conn.close()
    assert '<option value="hybrid" selected>' in hw["client"].get("/subscription-portal").text  # pre-filled from the order
    response = hw["client"].post("/api/v2/customer/subscription/checkout", json={"plan_key": "ai_local", "camera_quantity": 2})
    assert response.status_code == 200, response.text
    fields = [f for f in hw["captured"] if f.get("mode") == "subscription"][-1]
    assert (fields["line_items[0][price]"], fields["line_items[0][quantity]"]) == ("price_srv_ai", "2")  # what the customer chose


def test_own_pc_customers_are_told_appliance_or_pc(db_path, monkeypatch):
    from test_per_camera_billing_v2 import _v2_page
    billing = _v2_page(db_path, monkeypatch, quantity=6)
    html = billing.customer_portal_page({"customer_id": "cust-v2", "role": "customer_owner"}, billing.entitlement_for_customer("cust-v2"))
    assert 'id="v2-setup-next"' in html  # active plan, nothing set up yet
    assert "Connect your AnyAiCam appliance or PC and add your cameras" in html
