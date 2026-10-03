"""Stripe TEST-mode evidence (2026-10-03): a declined Local -> Hybrid upgrade
must not put the customer's fully paid Local plan into payment-failure grace.

Reproduced in Stripe test mode (account acct_1U87e9GllhK80H2n, livemode=false)
with AnyAiCam's exact upgrade request (proration_behavior=always_invoice,
billing_cycle_anchor=unchanged, payment_behavior=pending_if_incomplete) and a
card that declines: Stripe keeps the subscription active on Local, holds the
Hybrid change in pending_update, and sends invoice.payment_failed for the
PRORATION invoice (billing_reason=subscription_update). When the pending
update expires (or a retry replaces it), Stripe voids that invoice and sends
customer.subscription.pending_update_expired + invoice.voided -- events
AnyAiCam neither subscribes to nor handles -- and no subscription.updated.

Before the fix, that invoice.payment_failed started the 7-day grace period:
nothing cleared it until the next renewal was paid (~30 days), so the sweep
suspended the paid Local plan after 7 days, and the customer was emailed that
a payment had failed. The payloads below are the real test-mode events,
trimmed to the fields AnyAiCam reads; only the AnyAiCam customer id and the
Price ids are mapped to the ones this test environment configures.
"""
import sqlite3
from datetime import timedelta

from test_stripe_billing_launch import (  # noqa: F401 -- fixtures and helpers
    HYBRID_8, LOCAL_8, _deliver, _plan, env,
)
from test_stripe_billing_policies import NOW, _capacity, _customer, _sweep

CUS = "cus_VN71xt1ImrtFYz"
SUB = "sub_1UMMneGllhK80H2nYBoDBMnC"
ITEM = "si_VN71YHPJGwjYhW"
PRORATION = "in_1UMMoAGllhK80H2n3M5aGkEc"


def _pending_subscription():
    """customer.subscription.updated evt_1UMMoDGllhK80H2nuFBFYt7R (real)."""
    return {"id": SUB, "object": "subscription", "customer": CUS, "status": "active", "billing_cycle_anchor": 1791009900,
            "cancel_at_period_end": False, "latest_invoice": PRORATION,
            "metadata": {"anyaicam_camera_slot_maximum": "8", "anyaicam_camera_slot_plan_type": "local",
                         "anyaicam_customer_id": "cust-A", "anyaicam_stripe_price_id": LOCAL_8},
            "items": {"data": [{"id": ITEM, "price": {"id": LOCAL_8}, "quantity": 1,
                                "current_period_end": int((NOW + timedelta(days=30)).timestamp())}]},
            "pending_update": {"expires_at": 1791092700, "subscription_items": [{"id": ITEM, "price": {"id": HYBRID_8}}]}}


def _proration_invoice(status, billing_reason="subscription_update"):
    """The invoice in invoice.payment_failed evt_1UMMoDGllhK80H2nXXZH5jFt (real)."""
    return {"id": PRORATION, "object": "invoice", "customer": CUS, "status": status, "billing_reason": billing_reason,
            "amount_due": 1000, "amount_paid": 0, "amount_remaining": 1000, "attempt_count": 1,
            "collection_method": "charge_automatically", "currency": "usd",
            "parent": {"type": "subscription_details", "subscription_details": {"subscription": SUB, "metadata": {
                "anyaicam_customer_id": "cust-A", "anyaicam_stripe_price_id": HYBRID_8,
                "anyaicam_camera_slot_plan_type": "hybrid", "anyaicam_camera_slot_maximum": "8"}}}}


def _event(event_id, kind, obj, created):
    return {"id": event_id, "type": kind, "created": created, "livemode": False, "data": {"object": obj}}


def _local_customer_with_declined_upgrade(env):
    _customer(env, customer_id="cust-A", stripe_customer=CUS, sub_id=SUB)
    assert _capacity(env) == (8, "local")
    env["stripe"][SUB] = _pending_subscription()  # what Stripe says now
    env["invoices"][PRORATION] = _proration_invoice("open")
    created = int(NOW.timestamp())
    assert _deliver(env, _event("evt_1UMMoDGllhK80H2nuFBFYt7R", "customer.subscription.updated",
                                _pending_subscription(), created)).status_code == 200
    assert _deliver(env, _event("evt_1UMMoDGllhK80H2nXXZH5jFt", "invoice.payment_failed",
                                _proration_invoice("open"), created + 1)).status_code == 200


def _emails(env):
    conn = sqlite3.connect(env["path"])
    try:
        return conn.execute("SELECT COUNT(*) FROM provisioning_notifications WHERE notification_type='payment_failed'").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _invoice_state(env, invoice_id):
    conn = sqlite3.connect(env["path"])
    try:
        found = conn.execute("SELECT state FROM billing_invoice_states WHERE invoice_id=?", (invoice_id,)).fetchone()
        return found[0] if found else None
    finally:
        conn.close()


def test_a_declined_upgrade_proration_does_not_start_grace_on_the_paid_local_plan(env):
    _local_customer_with_declined_upgrade(env)
    # No "your payment failed" email for a plan that is paid: that email only
    # ever sends for an invoice recorded as 'failed'.
    assert _invoice_state(env, PRORATION) != "failed"
    import purchase_notifications
    assert purchase_notifications.notify_payment_failed(PRORATION)["status"] == "ignored"
    assert _emails(env) == 0
    plan = _plan(env["path"])
    assert plan["status"] == "active" and plan["payment_failed_at"] is None


def test_the_paid_local_plan_is_not_suspended_after_the_declined_upgrade_expires(env):
    _local_customer_with_declined_upgrade(env)
    # The pending update expires: Stripe voids the proration invoice and sends
    # events AnyAiCam does not act on (the real ones, replayed).
    env["stripe"][SUB] = dict(_pending_subscription(), pending_update=None)
    env["invoices"][PRORATION] = _proration_invoice("void")
    created = int(NOW.timestamp()) + 60
    for event_id, kind, obj in (("evt_1UMMotGllhK80H2nIKQt9WxZ", "customer.subscription.pending_update_expired", env["stripe"][SUB]),
                                ("evt_1UMMouGllhK80H2nmawM23cr", "invoice.voided", _proration_invoice("void"))):
        assert _deliver(env, _event(event_id, kind, obj, created)).status_code == 200
    assert _sweep(env, NOW + timedelta(days=8)) == 0
    assert _plan(env["path"])["status"] == "active"
    assert _capacity(env) == (8, "local")


def test_a_replayed_declined_upgrade_event_changes_nothing(env):
    _local_customer_with_declined_upgrade(env)
    again = _deliver(env, _event("evt_1UMMoDGllhK80H2nXXZH5jFt", "invoice.payment_failed", _proration_invoice("open"),
                                 int(NOW.timestamp()) + 1))
    assert again.status_code == 200
    assert _plan(env["path"])["payment_failed_at"] is None and _capacity(env) == (8, "local")


def test_a_failed_renewal_still_starts_grace_exactly_as_before(env):
    """Control: only the declined plan-change proration is exempt."""
    _customer(env, customer_id="cust-A", stripe_customer=CUS, sub_id=SUB)
    renewal = dict(_proration_invoice("open", billing_reason="subscription_cycle"), id="in_renewal_cycle")
    env["invoices"]["in_renewal_cycle"] = renewal
    assert _deliver(env, _event("evt_renewal_failed", "invoice.payment_failed", renewal,
                                int(NOW.timestamp()))).status_code == 200
    assert _plan(env["path"])["payment_failed_at"] is not None
    assert _invoice_state(env, "in_renewal_cycle") == "failed"  # so the renewal-failure email is still due
    assert _sweep(env, NOW + timedelta(days=8)) == 1
    assert _plan(env["path"])["status"] == "suspended"


def test_a_subscription_stripe_makes_past_due_still_starts_grace_after_a_declined_upgrade(env):
    """Control: if Stripe itself reports the subscription past_due, grace starts as before."""
    _local_customer_with_declined_upgrade(env)
    past_due = dict(_pending_subscription(), status="past_due")
    env["stripe"][SUB] = past_due
    assert _deliver(env, _event("evt_past_due", "customer.subscription.updated", past_due,
                                int(NOW.timestamp()) + 120)).status_code == 200
    assert _plan(env["path"])["payment_failed_at"] is not None
    assert _sweep(env, NOW + timedelta(days=8)) == 1


# ---------------------------------------------------------------- real-object replays of the successful upgrade
#
# Customer A in the same test-mode run: sub_1UMMhSGllhK80H2n5CMZghZE, item
# si_VN6vPAlmoRBFJW, Local 8 -> Hybrid 8 in place, proration invoice
# in_1UMMj6GllhK80H2npxo0Vw2y paid immediately ($10.00), billing_cycle_anchor
# 1791009900 and renewal 1793688300 unchanged. Real Stripe objects of this API
# version carry the renewal date only on the item (no top-level
# current_period_end), unlike the synthetic fixtures elsewhere.

SUB_A = "sub_1UMMhSGllhK80H2n5CMZghZE"
CUS_A = "cus_VN6uPS12uMh4c3"
ITEM_A = "si_VN6vPAlmoRBFJW"
RENEWAL_A = 1793688300


def _real_subscription_a(price):
    plan = "hybrid" if price == HYBRID_8 else "local"
    return {"id": SUB_A, "object": "subscription", "customer": CUS_A, "status": "active", "billing_cycle_anchor": 1791009900,
            "cancel_at_period_end": False, "pending_update": None, "latest_invoice": "in_1UMMj6GllhK80H2npxo0Vw2y",
            "metadata": {"anyaicam_customer_id": "cust-A", "anyaicam_stripe_price_id": price,
                         "anyaicam_camera_slot_plan_type": plan, "anyaicam_camera_slot_maximum": "8"},
            "items": {"data": [{"id": ITEM_A, "price": {"id": price}, "quantity": 1, "current_period_start": 1791009900,
                                "current_period_end": RENEWAL_A}]}}


def _proration_paid():
    return {"id": "in_1UMMj6GllhK80H2npxo0Vw2y", "object": "invoice", "customer": CUS_A, "status": "paid",
            "billing_reason": "subscription_update", "amount_due": 1000, "amount_paid": 1000, "currency": "usd",
            "parent": {"type": "subscription_details", "subscription_details": {"subscription": SUB_A,
                                                                                "metadata": {"anyaicam_customer_id": "cust-A"}}}}


def _upgrade(env):
    from test_stripe_billing_launch import OWNER_A, _cookie
    return env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A))


def _rows(env, product):
    conn = sqlite3.connect(env["path"])
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM customer_entitlements WHERE customer_id='cust-A' AND product=?",
                                              (product,)).fetchall()]
    finally:
        conn.close()


def _upgrade_posts(env):
    return [key for (path, fields), key in zip(env["posts"], env["keys"]) if path == f"/v1/subscriptions/{SUB_A}"
            and "items[0][price]" in fields]


def _apply_like_stripe(env, monkeypatch, *, lose_answer=False):
    """Stripe's real answer: the same subscription and item, now on Hybrid."""
    from fastapi import HTTPException
    main = env["main"]
    send = main.stripe_api_post

    def post(path, fields, idempotency_key=None):
        if path == f"/v1/subscriptions/{SUB_A}" and "items[0][price]" in dict(fields):
            env["posts"].append((path, dict(fields)))
            env["keys"].append(idempotency_key)
            fields = dict(fields)
            assert (fields["items[0][id]"], fields["proration_behavior"], fields["billing_cycle_anchor"],
                    fields["payment_behavior"]) == (ITEM_A, "always_invoice", "unchanged", "pending_if_incomplete")
            env["stripe"][SUB_A] = _real_subscription_a(HYBRID_8)
            if lose_answer:
                raise HTTPException(status_code=502, detail="connection reset")
            return _real_subscription_a(HYBRID_8)
        return send(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", post)


def _seed_a(env):
    _customer(env, customer_id="cust-A", stripe_customer=CUS_A, sub_id=SUB_A)
    env["stripe"][SUB_A] = _real_subscription_a(LOCAL_8)
    assert _capacity(env) == (8, "local")


def test_real_successful_upgrade_gives_exactly_one_hybrid_plan_on_the_same_subscription(env, monkeypatch):
    _seed_a(env)
    _apply_like_stripe(env, monkeypatch)
    response = _upgrade(env)
    assert response.status_code == 200, response.text
    assert _capacity(env) == (8, "hybrid")
    [hybrid] = [r for r in _rows(env, "camera_slots_hybrid") if r["status"] == "active"]
    assert hybrid["stripe_subscription_id"] == SUB_A
    assert not [r for r in _rows(env, "camera_slots_local") if r["status"] == "active"]
    assert len(_upgrade_posts(env)) == 1
    assert not [p for p in env["posts"] if p[0] == "/v1/checkout/sessions"]  # never a second subscription
    # The real webhooks for the same change, delivered twice each: nothing duplicated, no grace.
    created = int(NOW.timestamp()) + 5
    for _ in range(2):
        assert _deliver(env, _event("evt_1UMMj9GllhK80H2nI9uJkBeR", "customer.subscription.updated",
                                    _real_subscription_a(HYBRID_8), created)).status_code == 200
        assert _deliver(env, _event("evt_1UMMj9GllhK80H2ncqpKZYPC", "invoice.paid", _proration_paid(), created)).status_code == 200
    assert len([r for r in _rows(env, "camera_slots_hybrid") if r["status"] == "active"]) == 1
    assert _plan(env["path"], product="camera_slots_hybrid")["payment_failed_at"] is None
    assert _capacity(env) == (8, "hybrid")


def test_real_upgrade_whose_answer_was_lost_is_recovered_without_a_second_charge(env, monkeypatch):
    _seed_a(env)
    _apply_like_stripe(env, monkeypatch, lose_answer=True)
    assert _upgrade(env).status_code == 502  # Stripe applied it; AnyAiCam never saw the answer
    _apply_like_stripe(env, monkeypatch)
    retry = _upgrade(env)  # Stripe already shows Hybrid: nothing is sent again
    assert retry.status_code == 200, retry.text
    assert len(_upgrade_posts(env)) == 1  # one Stripe update, one proration charge
    conn = sqlite3.connect(env["path"])
    assert conn.execute("SELECT COUNT(*) FROM plan_upgrade_attempts WHERE open_slot='open'").fetchone()[0] == 0
    conn.close()
    assert _capacity(env) == (8, "hybrid")
    assert len([r for r in _rows(env, "camera_slots_hybrid") if r["status"] == "active"]) == 1
