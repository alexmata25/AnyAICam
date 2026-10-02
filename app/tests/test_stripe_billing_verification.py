"""Codex verification of 9a388c6 (2026-10-02): three remaining billing
defects, reproduced fail-first through the real customer APIs and the real
signed webhook route. Stripe is mocked as its current state and, like Stripe,
replays its first answer for a repeated Idempotency-Key. No live Stripe.

1. A paid checkout stays blocking until its entitlement is provisioned.
2. A declined Local -> Hybrid upgrade closes its attempt; the customer's
   retry after fixing the card is a new attempt with a new key.
3. A delayed invoice.paid for an older period never clears grace started by
   a newer failed renewal.
"""
import sqlite3
import threading
from datetime import timedelta

from test_stripe_billing_launch import (  # noqa: F401 -- fixtures and helpers
    HYBRID_8, LOCAL_8, OWNER_A, _checkout, _cookie, _deliver, _plan, _sub_event, _subscription, env,
)
from test_stripe_billing_policies import NOW, _capacity, _customer, _invoice, _sweep
from test_stripe_billing_remediation import _at_once, _buy, _count


def _sql(env, statement):
    conn = sqlite3.connect(env["path"])
    conn.execute(statement)
    conn.commit()
    conn.close()


# ================================================================ 1. paid but not yet provisioned

def test_a_paid_checkout_blocks_another_base_checkout_until_it_is_provisioned(env, monkeypatch):
    import customer_entitlements as ce
    session = _buy(env).json()
    customer = env["sessions"][session["session_id"]]["fields"]["customer"]
    env["stripe"]["sub_A1"] = _subscription("sub_A1", customer, "active")
    env["paid_sessions"].add(session["session_id"])  # the customer paid
    completed = _checkout("evt_paid", stripe_customer=customer)
    completed["data"]["object"]["id"] = session["session_id"]

    provision = ce.sync_entitlement_from_stripe_event

    def unavailable(event):
        raise RuntimeError("entitlement store briefly unavailable")
    monkeypatch.setattr(ce, "sync_entitlement_from_stripe_event", unavailable)
    assert _deliver(env, completed).status_code == 503  # Stripe will retry
    assert _deliver(env, completed).status_code == 503  # a retry, still failing; the checkout step runs again
    for plan_type in ("local", "hybrid", "local"):  # the customer tries again before the webhook recovers
        assert _buy(env, plan_type).status_code == 409
    _sql(env, "UPDATE checkout_pending SET expires_at=0")  # even long after the session's own lifetime
    assert _buy(env, "local").status_code == 409 and _buy(env, "hybrid").status_code == 409
    assert len(env["sessions"]) == 1  # no second Checkout Session, so no second subscription

    monkeypatch.setattr(ce, "sync_entitlement_from_stripe_event", provision)
    for _ in range(2):
        assert _deliver(env, completed).status_code == 200  # recovery, then a harmless replay
    assert _count(env, "SELECT COUNT(*) FROM customer_entitlements WHERE customer_id='cust-A'") == 1
    assert _capacity(env) == (8, "local")
    assert _count(env, "SELECT COUNT(*) FROM checkout_pending WHERE status='completed'") == 1
    assert _buy(env, "hybrid").status_code == 409 and len(env["sessions"]) == 1  # Hybrid is the in-place upgrade


# ================================================================ 2. upgrade attempts

def _upgrade(env):
    return env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A))


def _upgrade_posts(env):
    return [(fields, key) for (path, fields), key in zip(env["posts"], env["keys"])
            if path == "/v1/subscriptions/sub_A1" and "items[0][price]" in fields]


def test_a_declined_upgrade_can_be_retried_with_a_new_attempt_after_the_card_is_fixed(env):
    _customer(env)  # Local on sub_A1
    period_end = env["stripe"]["sub_A1"]["current_period_end"]
    env["declines"].add("sub_A1")  # the proration payment fails
    declined = _upgrade(env)
    assert declined.status_code == 402
    assert env["stripe"]["sub_A1"]["items"]["data"][0]["price"]["id"] == LOCAL_8 and _capacity(env) == (8, "local")
    env["declines"].discard("sub_A1")  # the customer fixes the payment method and tries again
    upgraded = _upgrade(env)
    assert upgraded.status_code == 200, upgraded.text
    (first, first_key), (second, second_key) = _upgrade_posts(env)
    assert first_key != second_key  # a new attempt, not Stripe's cached decline
    for fields in (first, second):
        assert fields["proration_behavior"] == "always_invoice" and fields["billing_cycle_anchor"] == "unchanged"
    sub = env["stripe"]["sub_A1"]
    assert sub["items"]["data"][0]["price"]["id"] == HYBRID_8 and sub["current_period_end"] == period_end
    assert not [p for p in env["posts"] if p[0] == "/v1/checkout/sessions"]  # never a second subscription
    assert _capacity(env) == (8, "hybrid")
    assert _upgrade(env).json()["status"] == "already_hybrid"


def test_simultaneous_requests_for_one_upgrade_attempt_share_one_key(env, monkeypatch):
    _customer(env)
    main = env["main"]
    send = main.stripe_api_post
    arrived = threading.Barrier(5, timeout=10)

    def all_at_once(path, fields, idempotency_key=None):
        if path == "/v1/subscriptions/sub_A1" and "items[0][price]" in dict(fields):
            arrived.wait()  # every request has opened its attempt before Stripe answers any
        return send(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", all_at_once)
    responses = _at_once(5, lambda n: _upgrade(env))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    keys = {key for _, key in _upgrade_posts(env)}
    assert len(_upgrade_posts(env)) == 5 and len(keys) == 1  # one attempt: Stripe applies it once
    assert _capacity(env) == (8, "hybrid")


# ================================================================ 3. old invoice.paid vs newer failed renewal

def test_a_delayed_paid_event_for_an_older_period_never_ends_grace_for_a_newer_failure(env):
    _customer(env)
    _deliver(env, _invoice("evt_old_paid", invoice_id="in_old"))  # an older period, paid
    _deliver(env, _invoice("evt_new_failed", invoice_id="in_new", kind="invoice.payment_failed",
                           created=int(NOW.timestamp())))  # the newer renewal fails: grace starts
    started = _plan(env["path"])["payment_failed_at"]
    assert started is not None
    for n in range(2):  # the older paid event arrives late, and again
        _deliver(env, _invoice(f"evt_old_paid_late_{n}", invoice_id="in_old"))
    env["stripe"]["sub_A1"]["status"] = "active"
    _deliver(env, _sub_event("evt_sub_touch", "updated", env["stripe"]["sub_A1"], 5_000))  # nor does an unrelated update
    assert _plan(env["path"])["payment_failed_at"] == started  # grace still running from the same start
    _sweep(env, NOW + timedelta(days=7))
    assert _plan(env["path"])["status"] == "suspended"  # and it really ends after 7 days

    env["invoices"]["in_new"] = {"id": "in_new", "status": "paid", "amount_paid": 1499}  # the failed renewal is paid
    for n in range(2):
        _deliver(env, _invoice(f"evt_new_paid_{n}", invoice_id="in_new"))
    plan = _plan(env["path"])
    assert plan["status"] == "active" and plan["payment_failed_at"] is None and _capacity(env) == (8, "local")
    assert _count(env, "SELECT COUNT(*) FROM customer_entitlements WHERE customer_id='cust-A'") == 1


def test_grace_clears_when_the_failed_renewal_is_paid_within_the_7_days(env):
    _customer(env)
    _deliver(env, _invoice("evt_failed", invoice_id="in_r1", kind="invoice.payment_failed"))
    env["invoices"]["in_r1"] = {"id": "in_r1", "status": "paid", "amount_paid": 1499}
    _deliver(env, _invoice("evt_paid", invoice_id="in_r1"))
    assert _plan(env["path"])["payment_failed_at"] is None
    _sweep(env, NOW + timedelta(days=30))
    assert _capacity(env) == (8, "local")
