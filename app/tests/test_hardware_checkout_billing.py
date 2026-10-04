"""Hardware checkout and hardware refunds (2026-10-04).

1. Price guard: the configured Stripe Price must charge the published
   one-time price for the SKU; the browser only ever names a SKU.
2. One payable Checkout Session per (account, SKU): a double click, a
   concurrent request or a retry after a lost Stripe answer reuses the same
   attempt (same Idempotency-Key, same session); a definite Stripe refusal
   frees it; a paid session blocks until its order is recorded; afterwards a
   new purchase is a new attempt.
3. A fully refunded or disputed hardware payment reverses its order, so the
   VMS license an appliance includes goes with it -- only for that payment;
   a won dispute restores it; replays change nothing.

Signed webhooks through the real route; Stripe is the launch suite's fake.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from database_backend import override_target
from test_stripe_billing_launch import OWNER_A, _cookie, _deliver, env  # noqa: F401 -- fixture

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


def _buy(env, sku=STARTER_SKU, quantity=1, **extra):
    return env["client"].post("/api/payments/hardware-checkout", json={"sku": sku, "quantity": quantity, **extra},
                              cookies=_cookie(*OWNER_A))


def _session_posts(env):
    return [(fields, key) for (path, fields), key in zip(env["posts"], env["keys"]) if path == "/v1/checkout/sessions"]


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


def _rejected(detail="Stripe refused"):
    error = HTTPException(status_code=502, detail=detail)
    error.stripe_outcome = "rejected"
    return error


def _lost():
    error = HTTPException(status_code=502, detail="Could not connect to Stripe.")
    error.stripe_outcome = "uncertain"
    return error


# ============================================================ 1. price guard

def test_the_published_price_creates_a_checkout_with_the_server_side_price(hw):
    response = _buy(hw)
    assert response.status_code == 200, response.text
    [(fields, key)] = _session_posts(hw)
    assert fields["line_items[0][price]"] == STARTER and fields["mode"] == "payment" and fields["line_items[0][quantity]"] == "1"
    assert fields["metadata[anyaicam_hardware_sku]"] == STARTER_SKU and fields["metadata[anyaicam_customer_id]"] == "cust-A"
    assert fields["customer"].startswith("cus_")  # the account's one canonical Stripe customer
    assert key and key.startswith("anyaicam-checkout-")


def test_a_stripe_price_that_does_not_charge_the_published_price_is_refused(hw):
    hw["price_overrides"][STARTER] = {"id": STARTER, "unit_amount": 50, "currency": "usd", "active": True, "recurring": None}
    response = _buy(hw)
    assert response.status_code == 503 and "PRICE_MISMATCH" in response.json()["detail"]
    assert not _session_posts(hw)


def test_a_recurring_or_inactive_price_is_refused_too(hw):
    hw["price_overrides"][STARTER] = {"id": STARTER, "unit_amount": 124999, "currency": "usd", "active": True,
                                      "recurring": {"interval": "month"}}
    assert _buy(hw).status_code == 503
    hw["price_overrides"][STARTER] = {"id": STARTER, "unit_amount": 124999, "currency": "usd", "active": False, "recurring": None}
    import main
    main._VERIFIED_STRIPE_PRICES.clear()
    assert _buy(hw).status_code == 503 and not _session_posts(hw)


def test_an_unknown_sku_is_refused_without_calling_stripe(hw):
    response = _buy(hw, sku="AIC-APPLIANCE-RYZEN-FREE")
    assert response.status_code == 400 and not _session_posts(hw)


def test_client_supplied_prices_and_amounts_are_ignored(hw):
    response = _buy(hw, price_id="price_evil", amount=1, unit_amount=1, stripe_price_id="price_evil",
                    line_items=[{"price": "price_evil"}])
    assert response.status_code == 200
    [(fields, _)] = _session_posts(hw)
    assert fields["line_items[0][price]"] == STARTER and fields["metadata[anyaicam_stripe_price_id]"] == STARTER
    assert "price_evil" not in str(fields) and "amount" not in " ".join(fields)


def test_every_catalog_sku_charges_its_own_published_price(hw):
    import hardware_orders
    for sku, _product, _name, cents, var in hardware_orders.HARDWARE_CATALOG:
        assert _buy(hw, sku=sku).status_code == 200, sku
    sent = {fields["metadata[anyaicam_hardware_sku]"]: fields["line_items[0][price]"] for fields, _ in _session_posts(hw)}
    assert sent == {STARTER_SKU: STARTER, "AIC-APPLIANCE-RYZEN-ENTERPRISE": PRO,
                    "AIC-APPLIANCE-RYZEN-AAC-FACIAL": ENTERPRISE, "AIC-RELAY-NUMATO-3CH": RELAY}


# ============================================================ 2. one payable session

def test_a_double_click_gets_the_same_payable_session(hw):
    first, second = _buy(hw).json(), _buy(hw).json()
    assert first["session_id"] == second["session_id"] and first["checkout_url"] == second["checkout_url"]
    assert len(_session_posts(hw)) == 1  # the second click reused the open session
    assert len(_q(hw, "SELECT * FROM checkout_pending WHERE purchase=?", (f"hardware:{STARTER_SKU}",))) == 1


def test_a_concurrent_request_reuses_the_in_flight_attempt_and_its_key(hw):
    first = _buy(hw).json()
    # As if this request arrived while the first was still waiting for Stripe:
    _exec(hw, "UPDATE checkout_pending SET status='creating',session_id=NULL,checkout_url=NULL")
    second = _buy(hw).json()
    keys = [key for _, key in _session_posts(hw)]
    assert len(keys) == 2 and keys[0] == keys[1]  # the same Idempotency-Key ...
    assert second["session_id"] == first["session_id"]  # ... so Stripe's same session, never a second
    assert len(hw["sessions"]) == 1


def test_a_lost_stripe_answer_is_recovered_by_the_retry_with_the_same_key(hw, monkeypatch):
    main = hw["main"]
    fake = main.stripe_api_post
    lose = {"next": True}

    def answer_lost(path, fields, idempotency_key=None):
        result = fake(path, fields, idempotency_key=idempotency_key)  # Stripe does create the session ...
        if path == "/v1/checkout/sessions" and lose.pop("next", False):
            raise _lost()  # ... but its answer never arrives
        return result
    monkeypatch.setattr(main, "stripe_api_post", answer_lost)
    assert _buy(hw).status_code == 502
    retry = _buy(hw)
    assert retry.status_code == 200
    keys = [key for _, key in _session_posts(hw)]
    assert keys[0] == keys[1] and len(hw["sessions"]) == 1  # one session in Stripe, recovered
    assert retry.json()["session_id"] == next(iter(hw["sessions"]))


def test_a_definite_stripe_refusal_frees_the_attempt(hw, monkeypatch):
    main = hw["main"]
    fake = main.stripe_api_post
    refuse = {"next": True}
    refused_keys = []

    def refusing(path, fields, idempotency_key=None):
        if path == "/v1/checkout/sessions" and refuse.pop("next", False):
            refused_keys.append(idempotency_key)
            raise _rejected()  # nothing was created
        return fake(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", refusing)
    assert _buy(hw).status_code == 502
    assert _q(hw, "SELECT status FROM checkout_pending")[0]["status"] == "abandoned"
    assert _buy(hw).status_code == 200
    [(_, retry_key)] = _session_posts(hw)
    assert refused_keys and retry_key != refused_keys[0]  # a new attempt
    assert len(hw["sessions"]) == 1


def test_a_different_checkout_after_a_lost_answer_waits_while_in_flight_then_proceeds(hw, monkeypatch):
    main = hw["main"]
    fake = main.stripe_api_post
    lose = {"next": True}

    def answer_lost(path, fields, idempotency_key=None):
        result = fake(path, fields, idempotency_key=idempotency_key)
        if path == "/v1/checkout/sessions" and lose.pop("next", False):
            raise _lost()
        return result
    monkeypatch.setattr(main, "stripe_api_post", answer_lost)
    assert _buy(hw).status_code == 502
    assert _buy(hw, quantity=2).status_code == 409  # the lost request may still be in flight
    stalled = (datetime.now() - timedelta(minutes=5)).isoformat()
    _exec(hw, "UPDATE checkout_pending SET updated_at=?", (stalled,))
    changed = _buy(hw, quantity=2)
    assert changed.status_code == 200
    posts = _session_posts(hw)
    assert posts[-1][0]["line_items[0][quantity]"] == "2" and posts[-1][1] != posts[0][1]  # a new attempt, new key


def _paid_event(env, event_id, session_id, *, intent="pi_hw_1", price=STARTER, sku=STARTER_SKU, amount=124999, customer="cust-A"):
    return {"id": event_id, "type": "checkout.session.completed", "created": 2_000, "data": {"object": {
        "id": session_id, "object": "checkout.session", "mode": "payment", "payment_status": "paid",
        "amount_total": amount, "payment_intent": intent, "customer": "cus_A",
        "customer_details": {"email": "a@example.test"},
        "metadata": {"anyaicam_customer_id": customer, "anyaicam_stripe_price_id": price,
                     "anyaicam_hardware_sku": sku, "anyaicam_hardware_quantity": "1"}}}}


def test_a_paid_session_blocks_until_its_order_is_recorded_then_a_new_purchase_is_new(hw, monkeypatch):
    first = _buy(hw).json()
    hw["paid_sessions"].add(first["session_id"])
    import hardware_orders
    real = hardware_orders.sync_hardware_order_from_stripe_event
    monkeypatch.setattr(hardware_orders, "sync_hardware_order_from_stripe_event",
                        lambda event: (_ for _ in ()).throw(RuntimeError("database busy")))
    assert _deliver(hw, _paid_event(hw, "evt_paid_1", first["session_id"])).status_code >= 500
    assert _buy(hw).status_code == 409  # paid, order not yet recorded: no second checkout
    monkeypatch.setattr(hardware_orders, "sync_hardware_order_from_stripe_event", real)
    assert _deliver(hw, _paid_event(hw, "evt_paid_1", first["session_id"])).status_code == 200
    [order] = _q(hw, "SELECT * FROM hardware_orders")
    assert order["status"] == "paid" and order["stripe_payment_intent_id"] == "pi_hw_1" and order["amount_cents"] == 124999
    again = _buy(hw)  # a later, legitimate second purchase
    assert again.status_code == 200 and again.json()["session_id"] != first["session_id"]
    keys = [key for _, key in _session_posts(hw)]
    assert len(set(keys)) == 2


def test_different_skus_are_separate_purchases(hw):
    a, b = _buy(hw).json(), _buy(hw, sku="AIC-RELAY-NUMATO-3CH").json()
    assert a["session_id"] != b["session_id"]


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
