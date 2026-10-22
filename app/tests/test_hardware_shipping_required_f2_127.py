"""F2 (Red Stripe/VMS inspection, 2026-10-21): every way to buy AnyAiCam hardware collects a shipping address.

Hardware is sold only through the hardware checkout (hardware_checkout.py: /hardware-checkout for appliances, the relay
checkout from My subscription), whose Stripe Checkout collects the US shipping address and phone. The two older paths
that charged live hardware Prices without a shipping address are refused before any Stripe call:
* the appliance kind of POST /api/v2/customer/build-order/checkout (a selection saved before hardware-first) -> 409,
  pointing to /hardware-checkout;
* POST /api/payments/hardware-checkout -> 410 (retired).
Own-PC and plan-only Build Your System orders (software and service, nothing shipped) are unchanged.
"""
import pytest

from test_build_order_billing import APPLIANCE_RELAYS
from test_hardware_delivery_flow import APPLIANCE, OWN_PC
from test_hybrid_build_system_flow import (PRICES, _checkout, _checkouts, _new_customer, db_path, license_portal,  # noqa: F401
                                           package, portal, shop, site, storage)
from test_stripe_billing_launch import OWNER_A, _cookie, env  # noqa: F401 -- fixture

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

HARDWARE_SKUS = ["AIC-APPLIANCE-RYZEN-STARTER", "AIC-APPLIANCE-RYZEN-ENTERPRISE", "AIC-APPLIANCE-RYZEN-AAC-FACIAL",
                 "AIC-RELAY-NUMATO-3CH"]


# ------------------------------------------------------------------ Build Your System: appliance kind refused

@pytest.mark.parametrize("query", [APPLIANCE, APPLIANCE_RELAYS])
def test_a_saved_appliance_selection_is_sent_to_the_hardware_checkout_and_nothing_is_charged(shop, query):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "saved-appliance@example.test", query)
    response = _checkout(client)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "shipping address" in detail and "/hardware-checkout?appliance=AIC-APPLIANCE-RYZEN-STARTER" in detail
    assert _checkouts(captured) == []  # no Stripe Checkout Session was created


def test_the_order_summary_still_redirects_appliance_links_to_the_hardware_checkout(shop):
    client, _, mail, _, _ = shop
    _new_customer(client, mail, "redirect@example.test", APPLIANCE)
    response = client.get("/order-summary?plan=hybrid&cameras=8&appliance=AIC-APPLIANCE-RYZEN-STARTER", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/hardware-checkout?")


def test_own_pc_build_orders_still_check_out(shop):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "own-pc@example.test", OWN_PC)
    response = _checkout(client)
    assert response.status_code == 200, response.text
    fields = _checkouts(captured)[-1]
    # Owner billing rule (2026-10-22): the licence now, the plan (its Price saved) from activation.
    assert fields["mode"] == "payment" and fields["metadata[anyaicam_build_order]"] == "own_pc"
    assert fields["line_items[0][price]"] == "price_vms_8" and fields["metadata[anyaicam_plan_price_id]"] == PRICES["ai_local"]


def test_plan_only_build_orders_still_check_out(shop):
    client, captured, mail, _, _ = shop
    _new_customer(client, mail, "plan-only@example.test", "plan=hybrid&cameras=4")
    response = _checkout(client)
    assert response.status_code == 200, response.text
    fields = _checkouts(captured)[-1]
    # A selection without hardware is an own-PC order (licence now, plan from activation, owner rule 2026-10-22).
    assert fields["mode"] == "payment" and fields["metadata[anyaicam_plan_price_id]"] == PRICES["hybrid"]


# ------------------------------------------------------------------ the retired hardware checkout

def _stripe_sessions(env):
    return [path for path, _ in env["posts"] if path == "/v1/checkout/sessions"]


@pytest.mark.parametrize("sku", HARDWARE_SKUS)
def test_the_retired_hardware_checkout_refuses_every_sku_before_stripe(env, monkeypatch, sku):
    import hardware_orders
    for _, _, _, _, env_var in hardware_orders.HARDWARE_CATALOG:
        monkeypatch.setenv(env_var, "price_hw_configured")  # even fully configured, nothing is sold here
    response = env["client"].post("/api/payments/hardware-checkout", json={"sku": sku, "quantity": 1}, cookies=_cookie(*OWNER_A))
    assert response.status_code == 410
    assert "/hardware-checkout" in response.json()["detail"] and "shipping address" in response.json()["detail"]
    assert _stripe_sessions(env) == []


@pytest.mark.parametrize("body", [{}, {"sku": "AIC-APPLIANCE-RYZEN-STARTER"}, {"price_id": "price_evil"}])
def test_the_retired_hardware_checkout_refuses_anyone_whatever_the_body(env, body):
    signed_in = env["client"].post("/api/payments/hardware-checkout", json=body, cookies=_cookie(*OWNER_A))
    assert signed_in.status_code == 410
    signed_out = env["client"].post("/api/payments/hardware-checkout", json=body, cookies={})
    assert signed_out.status_code in (401, 410)  # the app's sign-in gate may answer first; never a checkout
    assert _stripe_sessions(env) == []


def test_hardware_is_never_discounted_on_the_supported_checkout():
    import inspect
    import hardware_checkout
    source = inspect.getsource(hardware_checkout.create_checkout)
    assert "friends_family" not in source and "discounts[" not in source
    assert '("allow_promotion_codes", "false")' in source


# ------------------------------------------------------------------ the supported hardware checkout requires shipping

@pytest.mark.parametrize("selection", [{"appliance": "AIC-APPLIANCE-RYZEN-STARTER"},
                                       {"appliance": "AIC-APPLIANCE-RYZEN-STARTER", "relays": 2},
                                       {"relays": 3}])
def test_every_supported_hardware_session_collects_shipping(shop, db_path, selection, monkeypatch):
    import hardware_checkout
    import hardware_orders
    import main
    from database_backend import override_target
    for sku, _, _, _, env_var in hardware_orders.HARDWARE_CATALOG:
        monkeypatch.setenv(env_var, f"price_{sku.lower()}")
    sent = []
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", lambda *args: None)
    monkeypatch.setattr(main, "stripe_api_post", lambda path, fields, idempotency_key=None: sent.append(dict(fields)) or
                        {"id": "cs_test_ship01", "url": "https://checkout.stripe.test/cs_test_ship01"})
    monkeypatch.setattr(hardware_checkout, "_expire_previous", lambda email, keep: None)
    identity = {"customer_id": "cust-ship", "role": "customer_owner"} if "appliance" not in selection else None
    import checkout_guard
    monkeypatch.setattr(checkout_guard, "canonical_stripe_customer", lambda customer_id, email=None: "cus_ship")
    with override_target(sqlite_path=str(db_path)):
        hardware_checkout.create_checkout(selection, email="ship@example.test", identity=identity)
    [fields] = sent
    assert fields["mode"] == "payment"
    assert fields["shipping_address_collection[allowed_countries][0]"] == "US"
    assert fields["phone_number_collection[enabled]"] == "true"
