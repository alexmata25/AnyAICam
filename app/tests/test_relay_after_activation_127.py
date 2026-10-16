"""Door-control relays from My subscription after activation (VMS 1.2.7, owner decision 2026-10-16).

The account owner of an activated account orders Numato 3-channel relays (quantity 1-16) through the hardware checkout:
Stripe mode=payment, the catalog price only, a US shipping address, an accessory order recorded only from the verified
webhook. Before activation there is no purchase control and the server refuses (409 activate_first). Appliance
(hardware-first) orders are unchanged. Stripe is the launch suite's fake; nothing reaches Stripe.
"""
import sqlite3
from datetime import datetime

import pytest

from test_stripe_billing_launch import OWNER_A, _cookie, _deliver, env  # noqa: F401 -- fixture

pytestmark = pytest.mark.real_activation
CID = "cust-A"
RELAY_PRICE = "price_hw_relay"


def _exec(env, sql, args=()):
    conn = sqlite3.connect(env["path"])
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _rows(env, sql, args=()):
    conn = sqlite3.connect(env["path"])
    conn.row_factory = sqlite3.Row
    found = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return found


def _activate(env):
    now = datetime.now().isoformat()
    _exec(env, "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
               "VALUES('lic-1',?,'vms_license',8,'active',?,?)", (CID, now, now))


@pytest.fixture()
def relays(env, monkeypatch):
    import hardware_checkout
    import hardware_orders
    import main
    hardware_checkout._create_limiter.events.clear()  # the per-IP checkout limiter is process-wide (as in test_hardware_first_checkout)
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH", RELAY_PRICE)
    monkeypatch.setattr(hardware_orders, "HARDWARE_PRICE_MAP", hardware_orders._load_hardware_price_map())
    previous = main.stripe_api_get

    def get(path):  # what Stripe charged per line, read when a paid order is recorded
        if "/line_items" in path:
            return {"data": [{"price": {"id": RELAY_PRICE}, "amount_total": 44997}]}
        return previous(path)
    monkeypatch.setattr(main, "stripe_api_get", get)
    return env


def _buy(env, body, owner=OWNER_A):
    return env["client"].post("/api/customer/relay-checkout", json=body, cookies=_cookie(*owner))


def _sessions(env):
    return [fields for path, fields in env["posts"] if path == "/v1/checkout/sessions"]


def _portal(env):
    return env["client"].get("/subscription-portal", cookies=_cookie(*OWNER_A)).text


# ------------------------------------------------------------------ before activation

def test_before_activation_the_server_refuses_and_the_portal_offers_no_relay_control(relays):
    response = _buy(relays, {"quantity": 2})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "activate_first"
    assert _sessions(relays) == []
    page = _portal(relays)
    assert 'id="v2-relays-after-activation"' in page and 'id="v2-relay-buy"' not in page


# ------------------------------------------------------------------ after activation

def test_an_activated_owner_orders_relays_at_the_catalog_price_with_shipping(relays):
    _activate(relays)
    page = _portal(relays)
    assert 'id="v2-relay-buy"' in page and "$149.99 each" in page
    response = _buy(relays, {"quantity": 3})
    assert response.status_code == 200, response.text
    [fields] = _sessions(relays)
    assert fields["mode"] == "payment" and fields["line_items[0][price]"] == RELAY_PRICE and fields["line_items[0][quantity]"] == "3"
    assert "line_items[1][price]" not in fields  # relays only: no appliance, nothing recurring
    assert fields["metadata[anyaicam_build_order]"] == "accessory_only"
    assert fields["metadata[anyaicam_hardware_items]"] == "AIC-RELAY-NUMATO-3CH:3"
    assert fields["metadata[anyaicam_customer_id]"] == CID and fields["shipping_address_collection[allowed_countries][0]"] == "US"
    assert fields["success_url"].endswith("/customer/orders?relay_payment=success")
    assert "relay_payment=cancelled" in fields["cancel_url"] and "allow_promotion_codes" in fields


@pytest.mark.parametrize("body", [{"quantity": 0}, {"quantity": 17}, {"quantity": "2"}, {"quantity": True}, {},
                                  {"quantity": 1, "sku": "AIC-APPLIANCE-RYZEN-STARTER"}, {"quantity": 1, "price": "price_evil"}])
def test_quantity_is_bounded_and_the_browser_sets_nothing_else(relays, body):
    _activate(relays)
    assert _buy(relays, body).status_code == 422
    assert _sessions(relays) == []


def test_only_the_account_owner_can_order(relays):
    _activate(relays)
    viewer = ("v@example.test", "customer_viewer", CID)
    assert _buy(relays, {"quantity": 1}, owner=viewer).status_code == 403
    assert relays["client"].post("/api/customer/relay-checkout", json={"quantity": 1}).status_code in (401, 403)  # signed out
    assert _sessions(relays) == []


def test_an_unconfigured_relay_price_shows_not_available_and_refuses(env):
    import hardware_checkout
    hardware_checkout._create_limiter.events.clear()
    _activate(env)
    page = env["client"].get("/subscription-portal", cookies=_cookie(*OWNER_A)).text
    assert 'id="v2-relay-buy"' not in page and "Not available yet" in page
    assert env["client"].post("/api/customer/relay-checkout", json={"quantity": 1}, cookies=_cookie(*OWNER_A)).status_code == 503


# ------------------------------------------------------------------ webhook: only a verified payment records the order

def _paid_event(env, *, payment_status="paid", items="AIC-RELAY-NUMATO-3CH:3", customer=CID, event_id="evt_relay"):
    checkout = _rows(env, "SELECT * FROM hardware_checkouts ORDER BY created_at DESC LIMIT 1")[0]
    return {"id": event_id, "type": "checkout.session.completed", "created": 3_000, "data": {"object": {
        "id": checkout["stripe_checkout_session_id"], "object": "checkout.session", "mode": "payment",
        "payment_status": payment_status, "amount_total": 44997, "customer": "cus_A", "payment_intent": "pi_relay",
        "customer_details": {"email": "a@example.test"},
        "shipping_details": {"name": "A", "address": {"line1": "1 Main", "city": "Houston", "state": "TX", "postal_code": "77001", "country": "US"}},
        "metadata": {"anyaicam_build_order": "accessory_only", "anyaicam_hardware_checkout_id": checkout["id"],
                     "anyaicam_hardware_items": items, "anyaicam_customer_id": customer}}}}


def test_a_paid_relay_order_is_recorded_once_for_the_account(relays):
    _activate(relays)
    assert _buy(relays, {"quantity": 3}).status_code == 200
    event = _paid_event(relays)
    for _ in range(3):  # redelivered: still one order
        assert _deliver(relays, event).status_code == 200
    orders = _rows(relays, "SELECT * FROM hardware_orders WHERE customer_id=?", (CID,))
    assert [(o["sku"], o["quantity"], o["status"]) for o in orders] == [("AIC-RELAY-NUMATO-3CH", 3, "paid")]
    page = relays["client"].get("/customer/orders?relay_payment=success", cookies=_cookie(*OWNER_A)).text
    assert 'id="orders-relay-paid"' in page and "Numato 3-Channel Relay Module" in page


@pytest.mark.parametrize("change", ["unpaid", "appliance_item", "no_account"])
def test_unpaid_or_malformed_relay_sessions_record_nothing(relays, change):
    _activate(relays)
    assert _buy(relays, {"quantity": 1}).status_code == 200
    event = _paid_event(relays, **{"unpaid": {"payment_status": "unpaid"},
                                   "appliance_item": {"items": "AIC-APPLIANCE-RYZEN-STARTER:1"},
                                   "no_account": {"customer": "cust-missing"}}[change])
    assert _deliver(relays, event).status_code == 200
    assert _rows(relays, "SELECT * FROM hardware_orders WHERE status='paid'") == []


def test_a_relay_order_never_counts_as_an_appliance_or_activates_an_account(relays):
    import customer_entitlements
    import per_camera_billing
    # paid relays on an account that is otherwise not activated (e.g. bought before a refund): no licence, no activation
    now = datetime.now().isoformat()
    _exec(relays, "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,status,"
                  "created_at,updated_at) VALUES('hw-r',?,'AIC-RELAY-NUMATO-3CH','Relay','price_hw_relay',2,29998,'paid',?,?)",
          (CID, now, now))
    assert customer_entitlements.appliance_includes_vms_license(CID) is False
    assert per_camera_billing.account_activated(CID) is False
