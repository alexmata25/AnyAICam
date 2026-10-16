"""Per-camera subscription lifecycle end to end (2026-10-08), offline.

One account goes through the whole life of a per-camera plan: checkout,
more cameras, a plan upgrade, fewer cameras at renewal, a failed renewal
with grace, suspension and recovery, refund, dispute, and cancellation.
Every customer action goes through the real /api/v2/customer/subscription
routes and every Stripe event through the real signed webhook route
(test_stripe_billing_launch's fake Stripe; no live Stripe). After each step
the entitlement, the usable camera capacity and the product mode must be
exactly what was paid for, at the universal per-camera prices
(Basic Local $9.99, AI Local $14.99, Hybrid $24.99 per camera per month).
"""
from datetime import datetime, timedelta
import sqlite3
import time

import pytest

from test_stripe_billing_launch import OWNER_A, _cookie, _deliver, env  # noqa: F401  (env is a fixture)

PRICES = {"basic_local": ("price_v2_basic", 999), "ai_local": ("price_v2_ai", 1499), "hybrid": ("price_v2_hybrid", 2499)}
SUB = "sub_v2_life"
PERIOD_END = 1_893_456_000


@pytest.fixture()
def v2(env, monkeypatch, fake_stripe_prices):  # noqa: F811
    import main
    import per_camera_billing as billing
    for plan_key, (price_id, cents) in PRICES.items():
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], price_id)
        fake_stripe_prices[price_id] = {"id": price_id, "unit_amount": cents, "currency": "usd", "active": True,
                                        "recurring": {"interval": "month"}}
    stripe_post = main.stripe_api_post

    def post(path, fields, idempotency_key=None):
        # The launch fake changes an item's price in place; Stripe also applies its quantity.
        answer = stripe_post(path, fields, idempotency_key=idempotency_key)
        values = dict(fields)
        if (path.startswith("/v1/subscriptions/") and "items[0][quantity]" in values
                and not answer.get("pending_update")):
            sub = env["stripe"][path.rsplit("/", 1)[1]]
            next(i for i in sub["items"]["data"] if i["id"] == values["items[0][id]"])["quantity"] = int(values["items[0][quantity]"])
        return answer
    monkeypatch.setattr(main, "stripe_api_post", post)
    env["client"].cookies.update(_cookie(*OWNER_A))
    return env


def _state(env, customer_id="cust-A"):
    import customer_entitlements as ce
    import per_camera_billing as billing
    entitlement = billing.entitlement_for_customer(customer_id) or {}
    return {"plan": entitlement.get("plan_key"), "status": entitlement.get("status"),
            "quantity": entitlement.get("camera_quantity"), "capacity": ce.usable_camera_capacity(customer_id),
            "mode": ce.product_mode_for_customer(customer_id)}


def _stripe_sub(env, plan_key, quantity, *, customer, status="active"):
    price_id = PRICES[plan_key][0]
    sub = env["stripe"].setdefault(SUB, {"id": SUB, "object": "subscription", "customer": customer,
                                         "current_period_end": PERIOD_END, "cancel_at_period_end": False,
                                         "latest_invoice": f"in_first_{SUB}", "schedule": None,
                                         "items": {"data": [{"id": "si_v2_life", "price": {"id": price_id}, "quantity": quantity}]},
                                         "metadata": {}})
    sub["status"] = status
    sub["items"]["data"][0].update(price={"id": price_id}, quantity=quantity)
    sub["metadata"].update({"anyaicam_customer_id": "cust-A", "anyaicam_billing_version": "2",
                            "anyaicam_plan_key": plan_key, "anyaicam_stripe_price_id": price_id})
    return sub


def _event(event_id, kind, obj):
    return {"id": event_id, "type": kind, "created": int(time.time()), "data": {"object": obj}}


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _invoice(invoice_id, sub, plan_key, quantity, *, billing_reason="subscription_cycle"):
    price_id, cents = PRICES[plan_key]
    return {"id": invoice_id, "object": "invoice", "customer": sub["customer"], "subscription": SUB,
            "billing_reason": billing_reason, "amount_paid": cents * quantity, "tax": 0, "currency": "usd",
            "period_start": int(time.time()), "charge": f"ch_{invoice_id}", "payment_intent": f"pi_{invoice_id}",
            "subscription_details": {"metadata": {"anyaicam_customer_id": "cust-A"}},
            "lines": {"data": [{"price": {"id": price_id}, "amount": cents * quantity, "quantity": quantity}]}}


def _checkout_to_active(env, plan_key="ai_local", quantity=4):
    started = _ok(env["client"].post("/api/v2/customer/subscription/checkout",
                                     json={"plan_key": plan_key, "camera_quantity": quantity}))
    fields = env["sessions"][started["session_id"]]["fields"]
    assert fields["line_items[0][price]"] == PRICES[plan_key][0]
    assert fields["line_items[0][quantity]"] == str(quantity)
    assert fields["metadata[anyaicam_billing_version]"] == "2"
    assert _state(env)["capacity"] == 0  # nothing is granted before Stripe confirms payment
    sub = _stripe_sub(env, plan_key, quantity, customer=fields["customer"])
    session = {"id": started["session_id"], "object": "checkout.session", "mode": "subscription", "payment_status": "paid",
               "customer": sub["customer"], "subscription": SUB, "client_reference_id": "cust-A",
               "metadata": {k[len("metadata["):-1]: v for k, v in fields.items() if k.startswith("metadata[")}}
    env["paid_sessions"].add(started["session_id"])
    assert _deliver(env, _event("evt_checkout", "checkout.session.completed", session)).status_code == 200
    _deliver(env, _event("evt_paid_first", "invoice.paid", _invoice("in_first_" + SUB, sub, plan_key, quantity,
                                                                  billing_reason="subscription_create")))
    return sub


def test_the_catalog_is_the_universal_per_camera_prices_and_no_fixed_tiers(v2):
    catalog = _ok(v2["client"].get("/api/v2/pricing/catalog"))
    plans = {p["plan_key"]: p for p in catalog["plans"]}
    assert {key: plans[key]["monthly_per_camera_amount"] for key in plans} == {"basic_local": 9.99, "ai_local": 14.99, "hybrid": 24.99}
    assert "base_plans" not in catalog
    text = str(catalog)
    for retired in ("1_8", "9_16", "17_32", "33_64"):
        assert f"local_{retired}" not in text and f"hybrid_{retired}" not in text


def test_a_per_camera_plan_through_its_whole_stripe_lifecycle(v2):
    import billing_status
    env = v2
    client = env["client"]

    # 1. Checkout: 4 AI Local cameras, granted once Stripe confirms payment.
    sub = _checkout_to_active(env, "ai_local", 4)
    assert _state(env) == {"plan": "ai_local", "status": "active", "quantity": 4, "capacity": 4, "mode": "local"}
    assert _deliver(env, _event("evt_checkout", "checkout.session.completed", {})).status_code == 200  # replay
    assert _state(env)["capacity"] == 4

    # 2. More cameras: charged now (prorated), capacity grows immediately.
    changed = _ok(client.post("/api/v2/customer/subscription/change", json={"plan_key": "ai_local", "camera_quantity": 6}))
    assert changed["status"] == "updated"
    write = dict(env["posts"][-1][1])
    assert (write["items[0][price]"], write["items[0][quantity]"], write["proration_behavior"],
            write["payment_behavior"]) == ("price_v2_ai", "6", "always_invoice", "pending_if_incomplete")
    assert _state(env)["capacity"] == 6
    _deliver(env, _event("evt_more", "customer.subscription.updated", sub))
    assert _state(env) == {"plan": "ai_local", "status": "active", "quantity": 6, "capacity": 6, "mode": "local"}

    # 3. Upgrade to Hybrid: immediate, cloud mode from now on.
    assert _ok(client.post("/api/v2/customer/subscription/change",
                           json={"plan_key": "hybrid", "camera_quantity": 6}))["status"] == "updated"
    assert dict(env["posts"][-1][1])["items[0][price]"] == "price_v2_hybrid"
    assert _state(env) == {"plan": "hybrid", "status": "active", "quantity": 6, "capacity": 6, "mode": "hybrid"}

    # 4. Fewer cameras: scheduled for renewal; the paid period keeps 6.
    scheduled = _ok(client.post("/api/v2/customer/subscription/change", json={"plan_key": "hybrid", "camera_quantity": 2}))
    assert scheduled["status"] == "scheduled" and scheduled["effective_at"] == PERIOD_END
    phases = env["schedules"][sub["schedule"]]["fields"]
    assert (phases["phases[0][items][0][quantity]"], phases["phases[1][items][0][quantity]"],
            phases["phases[1][items][0][price]"], phases["proration_behavior"]) == ("6", "2", "price_v2_hybrid", "none")
    assert _state(env)["capacity"] == 6
    # Renewal: Stripe moves to the next phase and releases the schedule.
    sub["items"]["data"][0]["quantity"] = 2
    sub["schedule"] = None
    _deliver(env, _event("evt_renewed", "customer.subscription.updated", sub))
    assert _state(env) == {"plan": "hybrid", "status": "active", "quantity": 2, "capacity": 2, "mode": "hybrid"}
    import per_camera_billing
    assert per_camera_billing.entitlement_for_customer("cust-A")["scheduled_plan_key"] is None

    # 5. Failed renewal: 7 days' grace with service, then suspension; payment restores it.
    failed = _invoice("in_renew_1", sub, "hybrid", 2)
    failed["amount_paid"] = 0
    env["invoices"]["in_renew_1"] = dict(failed, status="open")
    sub["status"] = "past_due"
    _deliver(env, _event("evt_fail", "invoice.payment_failed", failed))
    _deliver(env, _event("evt_past_due", "customer.subscription.updated", sub))
    assert _state(env)["capacity"] == 2  # grace
    billing_status.sweep_grace(datetime.now() + timedelta(days=8))
    assert _state(env) == {"plan": "hybrid", "status": "suspended", "quantity": 2, "capacity": 0, "mode": ""}
    env["invoices"]["in_renew_1"] = dict(_invoice("in_renew_1", sub, "hybrid", 2), status="paid")
    sub["status"] = "active"
    _deliver(env, _event("evt_recovered", "invoice.paid", _invoice("in_renew_1", sub, "hybrid", 2)))
    assert _state(env) == {"plan": "hybrid", "status": "active", "quantity": 2, "capacity": 2, "mode": "hybrid"}

    # 6. A full refund of the current period suspends service; a replayed paid invoice never restores it.
    refund = {"id": "ch_in_renew_1", "object": "charge", "amount": 4998, "amount_refunded": 4998, "refunded": True,
              "payment_intent": "pi_in_renew_1", "invoice": "in_renew_1"}
    _deliver(env, _event("evt_refund", "charge.refunded", refund))
    assert _state(env)["status"] == "suspended" and _state(env)["capacity"] == 0
    _deliver(env, _event("evt_recovered_replay", "invoice.paid", _invoice("in_renew_1", sub, "hybrid", 2)))
    assert _state(env)["capacity"] == 0

    # 7. Cancel at period end: service continues until Stripe deletes the subscription.
    cancelled = _ok(client.post("/api/v2/customer/subscription/cancel"))
    assert cancelled["status"] == "cancellation_scheduled" and cancelled["effective_at"] == PERIOD_END
    assert env["stripe"][SUB]["cancel_at_period_end"] is True
    sub["status"] = "canceled"
    _deliver(env, _event("evt_deleted", "customer.subscription.deleted", sub))
    state = _state(env)
    assert (state["status"], state["quantity"], state["capacity"], state["mode"]) == ("cancelled", 0, 0, "")
    with sqlite3.connect(env["path"]) as db:  # history kept
        assert db.execute("SELECT COUNT(*) FROM camera_plan_entitlements_v2 WHERE customer_id='cust-A'").fetchone()[0] == 1


def test_cancel_at_period_end_keeps_paid_service_until_stripe_ends_it(v2):
    env = v2
    sub = _checkout_to_active(env, "basic_local", 3)
    assert _state(env) == {"plan": "basic_local", "status": "active", "quantity": 3, "capacity": 3, "mode": "local"}
    first = _ok(env["client"].post("/api/v2/customer/subscription/cancel"))
    assert first["status"] == "cancellation_scheduled"
    assert _ok(env["client"].post("/api/v2/customer/subscription/cancel"))["status"] == "already_scheduled"
    assert _state(env)["capacity"] == 3
    assert [path for path, _ in env["posts"]].count(f"/v1/subscriptions/{SUB}") == 1
    sub["status"] = "canceled"
    _deliver(env, _event("evt_deleted", "customer.subscription.deleted", sub))
    assert _state(env)["capacity"] == 0


def test_a_dispute_suspends_the_plan_and_a_won_dispute_restores_it(v2):
    env = v2
    sub = _checkout_to_active(env, "ai_local", 5)
    dispute = {"id": "dp_1", "object": "dispute", "charge": f"ch_in_first_{SUB}", "payment_intent": f"pi_in_first_{SUB}",
               "status": "needs_response"}
    _deliver(env, _event("evt_dispute", "charge.dispute.created", dispute))
    assert _state(env)["status"] == "suspended" and _state(env)["capacity"] == 0
    _deliver(env, _event("evt_dispute_won", "charge.dispute.closed", dict(dispute, status="won")))
    assert _state(env) == {"plan": "ai_local", "status": "active", "quantity": 5, "capacity": 5, "mode": "local"}
    assert sub["status"] == "active"


def test_a_declined_upgrade_payment_keeps_the_paid_plan(v2):
    env = v2
    _checkout_to_active(env, "ai_local", 2)
    env["declines"].add(SUB)
    response = env["client"].post("/api/v2/customer/subscription/change", json={"plan_key": "hybrid", "camera_quantity": 4})
    assert response.status_code == 402, response.text
    assert _state(env) == {"plan": "ai_local", "status": "active", "quantity": 2, "capacity": 2, "mode": "local"}


def test_e1_plan_changes_never_charge_the_one_time_license_again(v2):
    """Owner decision E1 (2026-10-16): an own-PC customer pays the one-time VMS license once, in the first checkout;
    later plan and camera-count changes only modify the recurring plan item."""
    import customer_entitlements as ce
    env = v2
    _checkout_to_active(env, "basic_local", 1)
    ce.upsert_entitlement(customer_id="cust-A", product="vms_license", camera_slot_quantity=8, status="active",
                          stripe_customer_id="cus_A", stripe_subscription_id=None, stripe_checkout_session_id="cs_own_pc")
    before = len(env["posts"])
    for plan_key, cameras in (("basic_local", 4), ("ai_local", 4), ("hybrid", 6), ("hybrid", 2)):
        _ok(env["client"].post("/api/v2/customer/subscription/change", json={"plan_key": plan_key, "camera_quantity": cameras}))
    writes = [dict(fields) for _, fields in env["posts"][before:]]
    assert writes, "the changes reached Stripe"
    for write in writes:
        assert not any(key.startswith("line_items") for key in write), write
        assert not any("vms" in str(value).lower() or "license" in str(value).lower() for value in write.values()), write
        assert set(k for k in write if k.startswith("items[")) <= {"items[0][id]", "items[0][price]", "items[0][quantity]"}
    with sqlite3.connect(env["path"]) as db:  # still the one license row, untouched
        assert db.execute("SELECT camera_slot_quantity,status FROM customer_entitlements WHERE customer_id='cust-A' "
                          "AND product='vms_license'").fetchall() == [(8, "active")]
