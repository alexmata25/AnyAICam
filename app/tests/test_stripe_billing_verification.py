"""Codex verification of 9a388c6 (2026-10-02): three remaining billing
defects, reproduced fail-first through the real customer APIs and the real
signed webhook route. Stripe is mocked as its current state and, like Stripe,
replays its first answer for a repeated Idempotency-Key. No live Stripe.

1. A paid checkout stays blocking until its entitlement is provisioned.
2. A declined Local -> Hybrid upgrade closes its attempt; the customer's
   retry after fixing the card is a new attempt with a new key.
3. A delayed invoice.paid for an older period never clears grace started by
   a newer failed renewal.
Codex final review of dffe171: an upgrade attempt Stripe applied while
AnyAiCam lost the answer is closed once Stripe's state shows Hybrid, so a
later, legitimate upgrade is a new attempt with a new key.
"""
import sqlite3
import threading
from datetime import timedelta

import pytest

from test_stripe_billing_launch import (  # noqa: F401 -- fixtures and helpers
    HYBRID_8, LOCAL_8, OWNER_A, _checkout, _cookie, _deliver, _plan, _sub_event, _subscription, env,
)
from test_stripe_billing_policies import NOW, _capacity, _customer, _invoice, _sweep
# _buy drives the legacy fixed-capacity checkout, so this file needs the same
# test-only switch test_stripe_billing_remediation.py applies to every test
# that uses it: new legacy purchases are off in production since e6209a7
# (billing v2 only; that default is covered by test_legacy_checkout_guard.py).
from test_stripe_billing_remediation import _at_once, _buy, _count, _legacy_new_purchases_switched_on  # noqa: F401 -- autouse


def _sql(env, statement):
    conn = sqlite3.connect(env["path"])
    conn.execute(statement)
    conn.commit()
    conn.close()


# ================================================================ 1. paid but not yet provisioned

# ================================================================ 2. upgrade attempts

def _upgrade(env):
    return env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A))


def _upgrade_posts(env):
    return [(fields, key) for (path, fields), key in zip(env["posts"], env["keys"])
            if path == "/v1/subscriptions/sub_A1" and "items[0][price]" in fields]


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
