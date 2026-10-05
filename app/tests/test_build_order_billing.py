"""Build Your System orders paid online (owner decisions 2026-10-05).

Appliance: the hardware is charged at checkout and the card saved; the
recurring storage plan is not charged before the appliance is activated, and
activation starts it exactly once, whatever the retries, duplicates or lost
answers. Own PC: the one-time VMS license and the plan are charged at
checkout. Prices and Stripe Price IDs are server-side only and the verified
webhook stays authoritative. No phone ordering anywhere in the funnel.
"""
import inspect
import sqlite3

import pytest

from database_backend import override_target
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hardware_delivery_flow import APPLIANCE, OWN_PC, _hardware_order, _paid  # noqa: F401
from test_hybrid_build_system_flow import (PRICES, _activate_appliance, _checkout, _checkouts, _customer_id, _new_customer,  # noqa: F401
                                           _order, _paid_checkout_event, _pay, _record_build_order,
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

APPLIANCE_RELAYS = APPLIANCE + "&relays=2"


class _Refused(Exception):
    stripe_outcome = "rejected"


class _Lost(Exception):
    stripe_outcome = "uncertain"


@pytest.fixture()
def stripe_subscriptions(monkeypatch):
    """Stripe's subscription API for the activation start: records every
    create (with its Idempotency-Key), can refuse or lose answers, and lists
    what it created for the adoption check."""
    import main
    state = {"created": [], "keys": [], "fail": [], "made": {}}
    checkout_post, checkout_get = main.stripe_api_post, main.stripe_api_get  # the shop's Checkout double

    def post(path, fields, idempotency_key=None):
        if path != "/v1/subscriptions":
            return checkout_post(path, fields, idempotency_key=idempotency_key)
        state["keys"].append(idempotency_key)
        if idempotency_key in state["made"]:  # Stripe replays an Idempotency-Key's first answer
            return state["made"][idempotency_key]
        failure = state["fail"].pop(0) if state["fail"] else None
        if isinstance(failure, _Refused):
            raise failure
        subscription = {"id": f"sub_storage_{len(state['made']) + 1}", "status": "active",
                        "metadata": {key[9:-1]: value for key, value in fields if key.startswith("metadata[")}}
        state["made"][idempotency_key] = subscription
        state["created"].append(dict(fields))
        if isinstance(failure, _Lost):
            raise failure  # Stripe made it; the answer never arrived
        return subscription

    def get(path):
        if path.startswith("/v1/subscriptions?"):
            return {"data": list(state["made"].values())}
        if path.startswith("/v1/payment_methods?"):
            return {"data": [{"id": "pm_saved_card"}]}
        return checkout_get(path)
    monkeypatch.setattr(main, "stripe_api_post", post)
    monkeypatch.setattr(main, "stripe_api_get", get)
    return state


def _start(db_path, customer_id, **kwargs):
    import build_orders
    with override_target(sqlite_path=str(db_path)):
        return build_orders.start_storage_plan(customer_id, **kwargs)


def _activate(db_path, customer_id):
    import build_orders
    _activate_appliance(db_path, customer_id)
    with override_target(sqlite_path=str(db_path)):
        return build_orders.on_appliance_activated(customer_id)


def _worker(db_path):
    import build_orders
    with override_target(sqlite_path=str(db_path)):
        return build_orders.retry_storage_starts()


def _storage(db_path, customer_id):
    return _one(db_path, "SELECT * FROM deferred_storage_plans WHERE customer_id=?", (customer_id,))


def _capacity(db_path, customer_id):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(db_path)):
        return ce.usable_camera_capacity(customer_id)


# ------------------------------------------------------------ appliance: hardware today, storage at activation

def test_the_appliance_and_relays_are_charged_at_checkout_with_the_card_saved(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "hw@example.test", APPLIANCE_RELAYS)
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    assert fields["mode"] == "payment"
    assert (fields["line_items[0][price]"], fields["line_items[0][quantity]"]) == ("price_hw_starter", "1")
    assert (fields["line_items[1][price]"], fields["line_items[1][quantity]"]) == ("price_hw_relay", "2")
    assert "line_items[2][price]" not in fields
    assert fields["payment_intent_data[setup_future_usage]"] == "off_session"
    assert fields["metadata[anyaicam_build_order]"] == "appliance"
    assert fields["metadata[anyaicam_hardware_items]"] == "AIC-APPLIANCE-RYZEN-STARTER:1,AIC-RELAY-NUMATO-3CH:2"
    assert fields["customer"].startswith("cus_test_")  # the account's one Stripe customer, so the card is saved there


def test_the_webhook_records_the_hardware_orders_and_no_storage_is_charged_before_activation(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, sessions = shop
    _new_customer(client, mail, "orders@example.test", APPLIANCE_RELAYS)
    customer_id = _customer_id(db_path, "orders@example.test")
    assert _checkout(client).status_code == 200
    event = _paid_checkout_event(captured, customer_id)
    assert _record_build_order(db_path, event)["status"] == "appliance_order_recorded"
    assert _record_build_order(db_path, event)["status"] == "appliance_order_recorded"  # redelivery: same rows
    orders = sqlite3.connect(db_path).execute("SELECT sku,quantity,status,fulfillment_status,amount_cents FROM hardware_orders "
                                              "WHERE customer_id=? ORDER BY sku", (customer_id,)).fetchall()
    assert orders == [("AIC-APPLIANCE-RYZEN-STARTER", 1, "paid", "paid", 124999), ("AIC-RELAY-NUMATO-3CH", 2, "paid", "paid", 29998)]
    storage = _storage(db_path, customer_id)
    assert (storage["plan_key"], storage["camera_quantity"], storage["state"]) == ("hybrid", 8, "awaiting_activation")
    # Nothing recurring before activation: no subscription, no capacity, the worker waits.
    assert _worker(db_path) == 0 and stripe_subscriptions["created"] == []
    assert _capacity(db_path, customer_id) == 0
    assert _start(db_path, customer_id)["reason"] == "appliance not activated"


def test_activation_starts_the_storage_plan_exactly_once(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "once@example.test", APPLIANCE, captured)
    assert _activate(db_path, customer_id)["status"] == "started"
    import build_orders
    with override_target(sqlite_path=str(db_path)):
        assert build_orders.on_appliance_activated(customer_id)["status"] == "ignored"  # a duplicate activation event
    assert _worker(db_path) == 0
    assert len(stripe_subscriptions["created"]) == 1
    fields = dict(stripe_subscriptions["created"][0])
    assert fields["items[0][price]"] == PRICES["hybrid"] and fields["items[0][quantity]"] == "8"
    assert fields["customer"].startswith("cus_test_") and fields["default_payment_method"] == "pm_saved_card"
    assert fields["off_session"] == "true" and fields["payment_behavior"] == "error_if_incomplete"
    assert fields["metadata[anyaicam_billing_version]"] == "2" and fields["metadata[anyaicam_customer_id]"] == customer_id
    storage = _storage(db_path, customer_id)
    assert storage["state"] == "started" and storage["stripe_subscription_id"] == "sub_storage_1"


def test_a_lost_answer_is_retried_with_the_same_key_and_never_makes_a_second_subscription(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "lost@example.test", APPLIANCE, captured)
    stripe_subscriptions["fail"].append(_Lost("timeout"))
    assert _activate(db_path, customer_id)["status"] == "failed"
    assert _storage(db_path, customer_id)["state"] == "failed"
    assert _worker(db_path) == 1  # the worker finds Stripe's subscription for this order and adopts it
    assert _worker(db_path) == 0
    assert len(stripe_subscriptions["made"]) == 1 and len(stripe_subscriptions["created"]) == 1
    assert _storage(db_path, customer_id)["stripe_subscription_id"] == "sub_storage_1"


def test_a_retry_after_stripes_idempotency_window_is_still_a_single_subscription(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "window@example.test", APPLIANCE, captured)
    stripe_subscriptions["fail"].append(_Lost("timeout"))
    _activate(db_path, customer_id)
    made = dict(stripe_subscriptions["made"])
    stripe_subscriptions["made"].clear()  # Stripe forgot the key (over 24 hours later)...
    stripe_subscriptions["made"].update({"other-key": list(made.values())[0]})  # ...but the subscription exists
    assert _worker(db_path) == 1 and len(stripe_subscriptions["created"]) == 1


def test_a_start_already_running_is_not_started_twice(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "busy@example.test", APPLIANCE, captured)
    _activate_appliance(db_path, customer_id)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE deferred_storage_plans SET state='starting' WHERE customer_id=?", (customer_id,))
    conn.commit()
    conn.close()
    assert _start(db_path, customer_id)["status"] == "busy_or_done" and stripe_subscriptions["created"] == []


def test_a_declined_card_waits_for_the_customer_and_the_retry_is_a_new_request(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "declined@example.test", APPLIANCE, captured)
    stripe_subscriptions["fail"].append(_Refused("card_declined"))
    assert _activate(db_path, customer_id)["status"] == "payment_failed"
    assert _worker(db_path) == 0  # not retried automatically against a declined card
    html = client.get("/order-complete").text
    assert "Your payment method was declined." in html and 'id="order-storage-retry"' in html
    dashboard = client.get("/dashboard")
    if dashboard.status_code == 200:
        assert "Storage plan not started" in dashboard.text
    retry = client.post("/api/v2/customer/storage/start")
    assert retry.status_code == 200 and retry.json()["status"] == "started"
    assert len(stripe_subscriptions["created"]) == 1
    assert len(set(stripe_subscriptions["keys"])) == 2  # the retry after a refusal is a new Stripe request
    assert client.post("/api/v2/customer/storage/start").status_code == 409  # nothing left to retry


def test_a_refunded_appliance_order_never_starts_the_storage_plan(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "refund@example.test", APPLIANCE, captured)
    _hardware_order(db_path, customer_id, status="refunded")
    assert _activate(db_path, customer_id)["status"] == "ignored" and stripe_subscriptions["created"] == []


def test_an_account_that_already_has_a_plan_is_not_charged_twice(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "twice@example.test", APPLIANCE, captured)
    _pay(db_path, customer_id, "hybrid", 8, "sub_elsewhere")
    assert _activate(db_path, customer_id)["status"] == "superseded" and stripe_subscriptions["created"] == []


def test_an_appliance_customer_cannot_buy_the_plan_again_before_activation(shop, db_path):
    client, captured, mail, _, _ = shop
    _paid(client, mail, db_path, "nobuy@example.test", APPLIANCE, captured)
    refused = client.post("/api/v2/customer/subscription/checkout", json={"plan_key": "hybrid", "camera_quantity": 8})
    assert refused.status_code == 409 and "starts when you activate" in refused.json()["detail"]
    assert _checkout(client).status_code == 409
    page = client.get("/subscription-portal").text
    assert 'id="v2-checkout-button"' not in page and "Your storage plan starts when you activate the appliance." in page
    assert len(_checkouts(captured)) == 1


# ------------------------------------------------------------ own PC: license + plan at checkout

@pytest.mark.parametrize("cameras,license_price", [(5, "price_vms_8"), (8, "price_vms_8"), (16, "price_vms_16"),
                                                   (17, "price_vms_32"), (64, "price_vms_64")])
def test_own_pc_charges_the_smallest_license_once_and_starts_the_plan_at_checkout(shop, db_path, cameras, license_price):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, f"pc{cameras}@example.test", f"plan=ai_local&cameras={cameras}&vms_licence={cameras}")
    assert _checkout(client).status_code == 200
    fields = _checkouts(captured)[-1]
    assert fields["mode"] == "subscription"
    assert (fields["line_items[0][price]"], fields["line_items[0][quantity]"]) == (PRICES["ai_local"], str(cameras))
    assert (fields["line_items[1][price]"], fields["line_items[1][quantity]"]) == (license_price, "1")
    assert list(fields.values()).count(license_price) == 2  # the line and its metadata: charged once
    assert "payment_intent_data[setup_future_usage]" not in fields


def test_own_pc_webhook_grants_the_license_once(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "license@example.test", OWN_PC)
    customer_id = _customer_id(db_path, "license@example.test")
    assert _checkout(client).status_code == 200
    event = _paid_checkout_event(captured, customer_id)
    for _ in range(2):  # redelivery
        assert _record_build_order(db_path, event)["status"] == "vms_license_granted"
    rows = sqlite3.connect(db_path).execute("SELECT camera_slot_quantity,status,stripe_price_id FROM customer_entitlements "
                                            "WHERE customer_id=? AND product='vms_license'", (customer_id,)).fetchall()
    assert rows == [(8, "active", "price_vms_8")]
    assert _storage(db_path, customer_id) is None  # nothing deferred: the plan started at checkout


def test_license_metadata_that_does_not_match_the_configured_price_is_rejected(shop, db_path):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "tamper@example.test", OWN_PC)
    customer_id = _customer_id(db_path, "tamper@example.test")
    assert _checkout(client).status_code == 200
    event = _paid_checkout_event(captured, customer_id)
    event["data"]["object"]["metadata"]["anyaicam_vms_license_capacity"] = "64"
    assert _record_build_order(db_path, event)["status"] == "rejected"


# ------------------------------------------------------------ server-side prices, verified webhook

@pytest.mark.parametrize("body", [{"price_id": "price_evil"}, {"unit_amount": 1}, {"plan_key": "hybrid", "camera_quantity": 64},
                                  {"line_items": [{"price": "price_evil"}]}])
def test_the_browser_cannot_send_prices_price_ids_or_products(shop, db_path, body):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "evil@example.test", APPLIANCE)
    response = client.post("/api/v2/customer/build-order/checkout", json=body)
    assert response.status_code == 422 and _checkouts(captured) == []


def test_a_missing_hardware_price_is_reported_and_creates_nothing(shop, db_path, monkeypatch):
    client, captured, mail, _, _ = shop
    monkeypatch.delenv("ANYAICAM_STRIPE_PRICE_RYZEN_STARTER")
    _new_customer(client, mail, "noprice@example.test", APPLIANCE)
    response = _checkout(client)
    assert response.status_code == 503 and "not available yet" in response.json()["detail"] and _checkouts(captured) == []


@pytest.mark.parametrize("change", [{"payment_status": "unpaid"}, {"mode": "subscription"},
                                    {"metadata": {"anyaicam_plan_key": "enterprise"}},
                                    {"metadata": {"anyaicam_hardware_items": "AIC-RELAY-NUMATO-3CH:1"}},
                                    {"metadata": {"anyaicam_camera_quantity": "65"}}])
def test_only_a_verified_paid_valid_order_is_recorded(shop, db_path, change):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "verify@example.test", APPLIANCE)
    customer_id = _customer_id(db_path, "verify@example.test")
    assert _checkout(client).status_code == 200
    event = _paid_checkout_event(captured, customer_id)
    session = event["data"]["object"]
    for key, value in change.items():
        if key == "metadata":
            session["metadata"].update(value)
        else:
            session[key] = value
    assert _record_build_order(db_path, event)["status"] in ("not_granted", "rejected")
    assert _storage(db_path, customer_id) is None
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM hardware_orders WHERE customer_id=?", (customer_id,)).fetchone()[0] == 0


def test_returning_from_stripe_grants_nothing_until_the_webhook(shop, db_path):
    client, captured, mail, _, sessions = shop
    _new_customer(client, mail, "return@example.test", APPLIANCE)
    customer_id = _customer_id(db_path, "return@example.test")
    assert _checkout(client).status_code == 200
    sessions["cs_test_return01"] = {"status": "complete", "payment_status": "paid", "metadata": {"anyaicam_customer_id": customer_id}}
    html = client.get("/order-complete?session_id=cs_test_return01").text
    assert "Activating your AnyAiCam plan" in html and "being prepared" not in html
    assert _storage(db_path, customer_id) is None


def test_build_orders_run_before_commissions_and_block_the_checkout_claim_until_done():
    import checkout_guard
    import main
    names = [name for name, _ in main._stripe_webhook_steps()]
    assert names.index("hardware_orders") < names.index("build_orders") < names.index("sales_commissions")
    source = inspect.getsource(checkout_guard._provisioned)
    assert '"build_orders"' in source and "anyaicam_build_order" in source


def test_appliance_activation_starts_the_storage_plan():
    import appliance_cloud
    source = inspect.getsource(appliance_cloud.register_appliance_cloud_routes)
    assert "on_appliance_activated(appliance.get('customer_id'))" in source
    import main
    assert "retry_storage_starts" in inspect.getsource(main._billing_grace_worker)


# ------------------------------------------------------------ no phone ordering, resume

def test_no_phone_ordering_anywhere_in_the_funnel(shop, db_path):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "nophone@example.test", APPLIANCE_RELAYS, captured)
    pages = [client.get("/order-complete").text, client.get("/subscription-portal").text]
    client.cookies.clear()
    _new_customer(client, mail, "nophone2@example.test", APPLIANCE_RELAYS)
    pages.append(_order(client).text)
    for html in pages:
        for phrase in ("by phone", "call (346)", "arranged with AnyAiCam", "not in this online checkout"):
            assert phrase not in html
    import order_funnel
    assert "by phone" not in inspect.getsource(order_funnel)


def test_a_returning_appliance_customer_resumes_at_the_order_status(shop, db_path):
    client, captured, mail, _, _ = shop
    _paid(client, mail, db_path, "resume@example.test", APPLIANCE, captured)
    client.cookies.clear()
    assert _signed_in(_login(client, "resume@example.test", customer_only=True))
    assert client.get("/customer-account").headers["location"] == "/order-complete"
    assert "Your AnyAiCam system is being prepared." in client.get("/order-complete").text
