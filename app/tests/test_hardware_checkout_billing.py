"""Hardware checkout and hardware refunds (2026-10-04).

1-2. The original one-SKU hardware checkout route (price guard, one payable
   session) is retired (F2, 2026-10-21): it collected no shipping address.
3. A fully refunded or disputed hardware payment reverses its order, so the
   VMS license an appliance includes goes with it -- only for that payment;
   a won dispute restores it; replays change nothing.

Signed webhooks through the real route; Stripe is the launch suite's fake.
"""
import sqlite3

import pytest

from database_backend import override_target
from test_stripe_billing_launch import _deliver, env  # noqa: F401 -- fixture

STARTER, PRO, ENTERPRISE, RELAY = "price_hw_starter", "price_hw_pro", "price_hw_enterprise", "price_hw_relay"
STARTER_SKU = "AIC-APPLIANCE-RYZEN-STARTER"


@pytest.fixture()
def hw(env, monkeypatch, fake_stripe_prices):
    import hardware_orders
    for var, price in (("ANYAICAM_STRIPE_PRICE_RYZEN_STARTER", STARTER), ("ANYAICAM_STRIPE_PRICE_RYZEN_ENTERPRISE", PRO),
                       ("ANYAICAM_STRIPE_PRICE_RYZEN_AAC_FACIAL", ENTERPRISE), ("ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH", RELAY)):
        monkeypatch.setenv(var, price)
    monkeypatch.setattr(hardware_orders, "HARDWARE_PRICE_MAP", hardware_orders._load_hardware_price_map())
    env["price_overrides"] = fake_stripe_prices
    return env


def _q(env, sql, args=()):
    conn = sqlite3.connect(env["path"])
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _exec(env, sql, args=()):
    conn = sqlite3.connect(env["path"])
    conn.execute(sql, args)
    conn.commit()
    conn.close()


# ============================================================ 1-2. the retired checkout route

# The route itself (POST /api/payments/hardware-checkout) charged hardware without a shipping address and is retired
# (F2, 2026-10-21: test_hardware_shipping_required_f2_127.py). Hardware is bought through hardware_checkout.py
# (test_hardware_first_checkout.py, test_relay_after_activation_127.py); orders already paid through the old route
# are still refunded and disputed as below.


def _paid_event(env, event_id, session_id, *, intent="pi_hw_1", price=STARTER, sku=STARTER_SKU, amount=124999, customer="cust-A"):
    return {"id": event_id, "type": "checkout.session.completed", "created": 2_000, "data": {"object": {
        "id": session_id, "object": "checkout.session", "mode": "payment", "payment_status": "paid",
        "amount_total": amount, "payment_intent": intent, "customer": "cus_A",
        "customer_details": {"email": "a@example.test"},
        "metadata": {"anyaicam_customer_id": customer, "anyaicam_stripe_price_id": price,
                     "anyaicam_hardware_sku": sku, "anyaicam_hardware_quantity": "1"}}}}


# ============================================================ 3. refunds and disputes

def _seed_plan(env, customer="cust-A", slots=8):
    _exec(env, "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) "
               "VALUES(?,?,'camera_slots_local',?,'active','2026-01-01','2026-01-01')", (f"plan-{customer}", customer, slots))


def _license(env, customer="cust-A"):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(env["path"])):
        return ce.appliance_includes_vms_license(customer), ce.vms_license_capacity(customer)


def _refund(event_id, intent, *, amount=124999, refunded=None):
    refunded = amount if refunded is None else refunded
    return {"id": event_id, "type": "charge.refunded", "created": 3_000, "data": {"object": {
        "id": f"ch_{intent}", "object": "charge", "payment_intent": intent, "amount": amount,
        "amount_refunded": refunded, "refunded": refunded >= amount}}}


def _dispute(event_id, intent, kind, status="needs_response"):
    return {"id": event_id, "type": f"charge.dispute.{kind}", "created": 3_000, "data": {"object": {
        "id": f"dp_{intent}", "object": "dispute", "charge": f"ch_{intent}", "payment_intent": intent, "status": status}}}


def _paid_appliance(env, intent="pi_hw_1", session="cs_hw_1", event="evt_hw_1", customer="cust-A"):
    assert _deliver(env, _paid_event(env, event, session, intent=intent, customer=customer)).status_code == 200


def test_a_full_refund_reverses_the_order_and_the_included_license_and_replays_change_nothing(hw):
    _seed_plan(hw)
    _paid_appliance(hw)
    assert _license(hw) == (True, 8)
    for _ in range(2):
        assert _deliver(hw, _refund("evt_refund_1", "pi_hw_1")).status_code == 200
    [order] = _q(hw, "SELECT * FROM hardware_orders")
    assert order["status"] == "refunded"
    assert _license(hw) == (False, 0)
    # A replayed checkout event never revives a refunded order.
    _paid_appliance(hw)
    assert _q(hw, "SELECT status FROM hardware_orders")[0]["status"] == "refunded" and _license(hw) == (False, 0)


def test_a_partial_refund_changes_nothing(hw):
    _seed_plan(hw)
    _paid_appliance(hw)
    assert _deliver(hw, _refund("evt_partial", "pi_hw_1", refunded=20000)).status_code == 200
    assert _q(hw, "SELECT status FROM hardware_orders")[0]["status"] == "paid" and _license(hw) == (True, 8)


def test_a_dispute_suspends_the_order_a_won_dispute_restores_it_a_lost_one_does_not(hw):
    _seed_plan(hw)
    _paid_appliance(hw)
    assert _deliver(hw, _dispute("evt_dp_open", "pi_hw_1", "created")).status_code == 200
    assert _q(hw, "SELECT status FROM hardware_orders")[0]["status"] == "disputed" and _license(hw) == (False, 0)
    assert _deliver(hw, _dispute("evt_dp_won", "pi_hw_1", "closed", status="won")).status_code == 200
    assert _q(hw, "SELECT status FROM hardware_orders")[0]["status"] == "paid" and _license(hw) == (True, 8)
    assert _deliver(hw, _dispute("evt_dp_open_2", "pi_hw_1", "created")).status_code == 200
    assert _deliver(hw, _dispute("evt_dp_lost", "pi_hw_1", "closed", status="lost")).status_code == 200
    assert _q(hw, "SELECT status FROM hardware_orders")[0]["status"] == "disputed" and _license(hw) == (False, 0)


def test_only_the_refunded_payment_changes_other_appliances_and_licenses_are_untouched(hw):
    _seed_plan(hw)
    _seed_plan(hw, customer="cust-B")
    _paid_appliance(hw, intent="pi_hw_1", session="cs_hw_1", event="evt_hw_1")
    _paid_appliance(hw, intent="pi_hw_2", session="cs_hw_2", event="evt_hw_2")
    _paid_appliance(hw, intent="pi_hw_b", session="cs_hw_b", event="evt_hw_b", customer="cust-B")
    _exec(hw, "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_payment_intent_id,created_at,updated_at) "
              "VALUES('lic-A','cust-A','vms_license',16,'active','pi_license_A','2026-01-01','2026-01-01')")
    assert _deliver(hw, _refund("evt_refund_1", "pi_hw_1")).status_code == 200
    states = {r["stripe_payment_intent_id"]: r["status"] for r in _q(hw, "SELECT * FROM hardware_orders")}
    assert states == {"pi_hw_1": "refunded", "pi_hw_2": "paid", "pi_hw_b": "paid"}
    assert _license(hw) == (True, 16)  # still an appliance customer, and the standalone license stands
    assert _q(hw, "SELECT status FROM customer_entitlements WHERE id='lic-A'")[0]["status"] == "active"
    assert _license(hw, "cust-B") == (True, 8)


def test_an_order_recorded_before_its_payment_intent_was_kept_is_found_through_stripe(hw, monkeypatch):
    _seed_plan(hw)
    _paid_appliance(hw)
    _exec(hw, "UPDATE hardware_orders SET stripe_payment_intent_id=NULL")  # as recorded by an earlier build
    main = hw["main"]
    fake_get = main.stripe_api_get

    def sessions_by_intent(path):
        if path.startswith("/v1/checkout/sessions?payment_intent=pi_hw_1"):
            return {"object": "list", "data": [{"id": "cs_hw_1", "object": "checkout.session"}]}
        return fake_get(path)
    monkeypatch.setattr(main, "stripe_api_get", sessions_by_intent)
    assert _deliver(hw, _refund("evt_refund_old", "pi_hw_1")).status_code == 200
    [order] = _q(hw, "SELECT * FROM hardware_orders")
    assert order["status"] == "refunded" and order["stripe_payment_intent_id"] == "pi_hw_1"
    assert _license(hw) == (False, 0)
