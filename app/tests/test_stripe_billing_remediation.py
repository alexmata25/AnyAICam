"""Codex audit of billing commit 3f5b9c4 (2026-10-02): the nine findings,
reproduced fail-first and adversarially through the real customer APIs and
the real signed webhook route (Stripe mocked as its current state; no live
Stripe).

Launch blockers
1. Simultaneous base-plan checkouts never produce two subscriptions.
2. Every checkout uses the account's one canonical Stripe customer.
3. A delayed event for an old subscription never alters a newer one.
4. A refund/dispute that arrives before the grant is honoured by the grant.
5. A subscription whose first payment is incomplete gets no service.
Fix before launch
6. A late invoice.payment_failed for a paid invoice never restarts grace.
7. Commissions only for an invoice whose customer, subscription and
   metadata belong to one account.
8. A failed renewal is emailed to the owner once per invoice (outbox).
9. Nothing is applied when Stripe's current state cannot be read.
All earlier owner billing policies stay as tested in
test_stripe_billing_policies.py / test_stripe_billing_launch.py.
"""
import contextvars
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from database_backend import override_target
from test_stripe_billing_launch import (  # noqa: F401 -- fixtures and helpers
    HYBRID_8, LOCAL_8, OWNER_A, _checkout, _cookie, _deliver, _plan, _sub_event, _subscription, env,
)
from test_stripe_billing_policies import NOW, PERIOD_END, _capacity, _customer, _dispute, _invoice, _refund, _sweep


@pytest.fixture(autouse=True)
def _legacy_new_purchases_switched_on(monkeypatch):
    """These tests drive the legacy fixed-capacity checkout to exercise the
    machinery it shares with grandfathered accounts (one payable session,
    canonical Stripe customer, price guard, F&F coupons, webhook grant).
    In production new legacy purchases are off (billing v2, 2026-10-05);
    that default is covered by test_legacy_checkout_guard.py."""
    import main
    monkeypatch.setattr(main, "LEGACY_CAMERA_SLOT_NEW_PURCHASES", True)

OWNER_B = ("b@example.test", "customer_owner", "cust-B")


def _db(env):
    conn = sqlite3.connect(env["path"])
    conn.row_factory = sqlite3.Row
    return conn


def _count(env, sql, args=()):
    conn = _db(env)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def _buy(env, plan_type="local", owner=OWNER_A):
    return env["client"].post("/api/customer/camera-slots/checkout", json={"plan_type": plan_type, "tier_label": "1-8"},
                              cookies=_cookie(*owner))


def _session_fields(env):
    return [p[1] for p in env["posts"] if p[0] == "/v1/checkout/sessions"]


def _open_sessions(env):
    return [s for s in env["sessions"].values() if s["status"] == "open"]


def _at_once(count, fn):
    """Run fn(n) for n in range(count) on separate threads released together,
    each in a copy of this test's context (its database)."""
    barrier = threading.Barrier(count)
    contexts = [contextvars.copy_context() for _ in range(count)]

    def run(n):
        barrier.wait()
        return fn(n)
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(lambda n: contexts[n].run(run, n), range(count)))


@pytest.fixture()
def mail(monkeypatch):
    import purchase_notifications
    sent, failing = [], []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            if failing:
                raise OSError("smtp unavailable")
            sent.append({"type": message_type, "to": to, "subject": subject, "text": text, "metadata": metadata or {}})
            return {"status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    return {"sent": sent, "failing": failing}


# ================================================================ 1. one payable checkout per purchase

def test_simultaneous_same_plan_checkouts_create_exactly_one_stripe_session(env):
    responses = _at_once(8, lambda n: _buy(env))
    ok = [r.json() for r in responses if r.status_code == 200]
    assert ok and all(r.status_code in (200, 409) for r in responses)
    assert len(env["sessions"]) == 1  # one Checkout Session -> at most one subscription
    assert {o["checkout_url"] for o in ok} == {next(iter(env["sessions"].values()))["url"]}


def test_simultaneous_local_and_hybrid_leave_only_one_payable_session(env):
    responses = _at_once(6, lambda n: _buy(env, "local" if n % 2 else "hybrid"))
    assert all(r.status_code in (200, 409) for r in responses)
    assert len(_open_sessions(env)) == 1


def test_racing_claims_at_the_database_create_one_session(env):
    """The claim itself, without the HTTP layer serialising anything."""
    import checkout_guard
    fields = [("mode", "subscription"), ("line_items[0][price]", LOCAL_8), ("customer", "cus_A")]
    results = _at_once(12, lambda n: _try(lambda: checkout_guard.create_session("cust-A", "base", price_id=LOCAL_8,
                                                                                 quantity=1, fields=fields)))
    created = {r["id"] for r in results if isinstance(r, dict)}
    assert len(env["sessions"]) == 1 and created == set(env["sessions"])


def _try(fn):
    try:
        return fn()
    except Exception as error:  # 409 while another request holds the claim
        return error


def test_switching_plan_expires_the_open_session_first_and_a_paid_one_is_never_replaced(env):
    first = _buy(env, "local").json()
    again = _buy(env, "local").json()
    assert again["checkout_url"] == first["checkout_url"] and len(env["sessions"]) == 1  # same purchase: same session
    hybrid = _buy(env, "hybrid").json()
    assert env["sessions"][first["session_id"]]["status"] == "expired"  # the Local session can no longer be paid
    assert [s["id"] for s in _open_sessions(env)] == [hybrid["session_id"]]
    env["paid_sessions"].add(hybrid["session_id"])  # the customer pays the Hybrid session...
    refused = _buy(env, "local")  # ...and tries Local in another tab before the webhook arrives
    assert refused.status_code == 409 and len(env["sessions"]) == 2


def test_an_expired_claim_or_a_failed_stripe_call_can_be_retried(env, monkeypatch):
    first = _buy(env).json()
    conn = _db(env)
    conn.execute("UPDATE checkout_pending SET expires_at=0")  # Stripe expired the session long ago
    conn.commit()
    conn.close()
    second = _buy(env).json()
    assert second["session_id"] != first["session_id"]
    conn = _db(env)
    conn.execute("UPDATE checkout_pending SET expires_at=0")
    conn.commit()
    conn.close()
    main = env["main"]
    real = main.stripe_api_post

    def flaky(path, fields, idempotency_key=None):
        if path == "/v1/checkout/sessions":
            from fastapi import HTTPException
            raise HTTPException(status_code=502, detail="Stripe unavailable")
        return real(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", flaky)
    assert _buy(env).status_code == 502
    monkeypatch.setattr(main, "stripe_api_post", real)
    assert _buy(env).status_code == 200  # the failed attempt did not leave the purchase blocked


def test_a_completed_purchase_grants_once_however_often_stripe_replays_it(env):
    session = _buy(env).json()
    env["stripe"]["sub_A1"] = _subscription("sub_A1", env["sessions"][session["session_id"]]["fields"]["customer"], "active")
    completed = _checkout("evt_paid", stripe_customer=env["stripe"]["sub_A1"]["customer"])
    completed["data"]["object"]["id"] = session["session_id"]
    paid = _invoice("evt_inv_paid", customer=env["stripe"]["sub_A1"]["customer"], invoice_id="in_first_sub_A1")
    for _ in range(3):
        assert _deliver(env, completed).status_code == 200
        assert _deliver(env, paid).status_code == 200
    assert _count(env, "SELECT COUNT(*) FROM customer_entitlements WHERE customer_id='cust-A'") == 1
    assert _capacity(env) == (8, "local")
    assert _count(env, "SELECT COUNT(*) FROM subscription_payments WHERE customer_id='cust-A'") == 1
    assert _count(env, "SELECT COUNT(*) FROM checkout_pending WHERE status='completed'") == 1
    assert _buy(env, "hybrid").status_code == 409  # one base plan: Hybrid is the in-place upgrade


# ================================================================ 2. one canonical Stripe customer

def test_every_checkout_reuses_the_accounts_one_stripe_customer(env, monkeypatch, fake_stripe_prices):
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", "price_talk")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8", "price_vms_8")
    import analytics_entitlements as ae
    import customer_entitlements as ce
    import pricing_catalog
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    monkeypatch.setattr(ce, "PRICE_ID_VMS_LICENSE_MAP", ce._load_vms_license_map())
    monkeypatch.setattr(pricing_catalog, "find_vms_license", lambda capacity: {
        "capacity": 8, "stripe_price_id": "price_vms_8", "price_env_var": "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8", "one_time_cents": 4999})
    monkeypatch.setattr(env["main"], "require_stripe_price_matches_catalog", lambda *a, **k: None)
    client, cookie = env["client"], _cookie(*OWNER_A)
    assert _buy(env).status_code == 200
    assert client.post("/api/customer/analytics/checkout", json={"addon_key": "talk_down", "quantity": 1}, cookies=cookie).status_code == 200
    assert client.post("/api/customer/vms-license/checkout", json={"capacity": 8}, cookies=cookie).status_code == 200
    customers = {f.get("customer") for f in _session_fields(env)}
    assert len(customers) == 1 and next(iter(customers)).startswith("cus_")
    assert not any("customer_email" in f for f in _session_fields(env))
    assert len(env["customers_by_key"]) == 1  # Stripe was asked to create exactly one customer


def test_an_account_that_already_pays_keeps_its_stripe_customer_and_its_portal(env):
    _customer(env)  # cust-A already pays through cus_A
    env["stripe"]["sub_A1"]["cancel_at_period_end"] = False
    response = env["client"].post("/api/customer/plan/cancel", cookies=_cookie(*OWNER_A))
    assert response.status_code == 200
    conn = _db(env)
    conn.execute("UPDATE customer_entitlements SET status='cancelled',camera_slot_quantity=0")  # later: buys again
    conn.commit()
    conn.close()
    assert _buy(env).status_code == 200
    assert _session_fields(env)[-1]["customer"] == "cus_A" and not env["customers_by_key"]
    portal = env["client"].post("/api/customer/billing-portal", cookies=_cookie(*OWNER_A))
    assert portal.status_code == 200
    assert [p for p in env["posts"] if p[0] == "/v1/billing_portal/sessions"][-1][1]["customer"] == "cus_A"


def test_two_accounts_never_share_a_stripe_customer(env):
    assert _buy(env, owner=OWNER_A).status_code == 200
    assert _buy(env, owner=OWNER_B).status_code == 200
    a, b = (f["customer"] for f in _session_fields(env))
    assert a != b
    conn = _db(env)
    conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_customer_id,created_at,updated_at) "
                 "VALUES('stolen','cust-B','camera_slots_local',8,'cancelled',?,'2026-01-01','2026-01-01')", (a,))
    conn.execute("DELETE FROM stripe_customer_bindings WHERE customer_id='cust-B'")
    conn.execute("UPDATE checkout_pending SET status='expired'")
    conn.commit()
    conn.close()
    assert _buy(env, owner=OWNER_B).status_code == 409  # A's Stripe customer is never used for B


# ================================================================ 3. old subscription events vs a resubscription

def _resubscribed(env):
    _customer(env)  # sub_A1
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "canceled", period_end=PERIOD_END)
    _deliver(env, _sub_event("evt_old_end", "deleted", env["stripe"]["sub_A1"], 5_000))
    env["stripe"]["sub_A2"] = _subscription("sub_A2", "cus_A", "active", period_end=PERIOD_END)
    assert _deliver(env, _checkout("evt_new", sub_id="sub_A2")).status_code == 200
    plan = _plan(env["path"])
    assert plan["status"] == "active" and plan["stripe_subscription_id"] == "sub_A2"


@pytest.mark.parametrize("stale", ["deleted", "updated", "checkout"])
def test_a_delayed_event_for_the_old_subscription_never_touches_the_new_one(env, stale):
    _resubscribed(env)
    if stale == "checkout":
        event = _checkout("evt_old_checkout_late", sub_id="sub_A1")
    else:
        event = _sub_event(f"evt_old_{stale}_late", stale, _subscription("sub_A1", "cus_A", "active"), 1_000)
    assert _deliver(env, event).status_code == 200
    plan = _plan(env["path"])
    assert plan["status"] == "active" and plan["stripe_subscription_id"] == "sub_A2" and _capacity(env) == (8, "local")


def test_a_delayed_event_for_an_old_add_on_subscription_never_touches_the_new_one(env, monkeypatch):
    import analytics_entitlements as ae
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", "price_talk")
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    _customer(env)
    env["stripe"]["sub_T1"] = _subscription("sub_T1", "cus_A", "active", price="price_talk")
    _deliver(env, _checkout("evt_t1", sub_id="sub_T1", price="price_talk"))
    env["stripe"]["sub_T1"]["status"] = "canceled"
    _deliver(env, _sub_event("evt_t1_end", "deleted", env["stripe"]["sub_T1"], 5_000))
    env["stripe"]["sub_T2"] = _subscription("sub_T2", "cus_A", "active", price="price_talk")
    _deliver(env, _checkout("evt_t2", sub_id="sub_T2", price="price_talk"))
    _deliver(env, _sub_event("evt_t1_late", "deleted", dict(env["stripe"]["sub_T1"]), 1_000))
    with override_target(sqlite_path=str(env["path"])):
        assert "talk_down" in ae.get_active_analytics_for_customer("cust-A")
        assert ae.row("SELECT stripe_subscription_id FROM addon_subscriptions WHERE addon_key='talk_down'")["stripe_subscription_id"] == "sub_T2"


# ================================================================ 4. refund / dispute before the grant

def test_a_refund_before_the_checkout_event_means_no_service_for_that_payment(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active", period_end=PERIOD_END)
    for _ in range(2):
        assert _deliver(env, _refund("evt_early_refund", "in_first_sub_A1")).status_code == 200
    assert _deliver(env, _checkout("evt_late_checkout")).status_code == 200
    plan = _plan(env["path"])
    assert plan["status"] == "suspended" and plan["suspended_reason"] == "refunded" and _capacity(env)[0] == 0


def test_a_dispute_before_the_checkout_event_suspends_and_a_won_dispute_restores(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active", period_end=PERIOD_END)
    _deliver(env, _dispute("evt_early_dispute", "in_first_sub_A1"))
    _deliver(env, _checkout("evt_late_checkout"))
    assert _plan(env["path"])["suspended_reason"] == "disputed" and _capacity(env)[0] == 0
    _deliver(env, _invoice("evt_first_paid", invoice_id="in_first_sub_A1"))  # the payment record arrives too
    assert _capacity(env)[0] == 0  # a disputed payment never restores service by itself
    for n in range(2):
        _deliver(env, _dispute(f"evt_won_{n}", "in_first_sub_A1", kind="charge.dispute.closed", status="won"))
    assert _plan(env["path"])["status"] == "active" and _capacity(env) == (8, "local")


def test_stripe_saying_the_payment_was_refunded_is_enough_even_before_the_refund_event(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active", period_end=PERIOD_END)
    env["intents"]["pi_in_first_sub_A1"] = {"id": "pi_in_first_sub_A1", "latest_charge": {
        "id": "ch_x", "amount": 1499, "amount_refunded": 1499, "refunded": True}}
    _deliver(env, _checkout("evt_checkout"))
    assert _plan(env["path"])["status"] == "suspended" and _capacity(env)[0] == 0


def test_a_partial_refund_before_the_grant_leaves_service_on(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active", period_end=PERIOD_END)
    _deliver(env, _refund("evt_partial", "in_first_sub_A1", refunded=100))
    _deliver(env, _checkout("evt_checkout"))
    assert _capacity(env) == (8, "local")


def _license_checkout(event_id, intent="pi_license"):
    return {"id": event_id, "type": "checkout.session.completed", "data": {"object": {
        "id": f"cs_{event_id}", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "customer": "cus_A",
        "payment_intent": intent, "amount_total": 4999, "customer_details": {"email": "a@example.test"},
        "metadata": {"anyaicam_customer_id": "cust-A", "anyaicam_stripe_price_id": "price_vms_8"}}}}


def test_a_refunded_vms_license_payment_is_never_granted(env, monkeypatch):
    import customer_entitlements as ce
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8", "price_vms_8")
    monkeypatch.setattr(ce, "PRICE_ID_VMS_LICENSE_MAP", ce._load_vms_license_map())
    _deliver(env, {"id": "evt_lic_refund", "type": "charge.refunded", "data": {"object": {
        "id": "ch_license", "object": "charge", "payment_intent": "pi_license", "amount": 4999, "amount_refunded": 4999, "refunded": True}}})
    _deliver(env, _license_checkout("evt_lic"))
    with override_target(sqlite_path=str(env["path"])):
        assert ce.vms_license_capacity("cust-A") == 0


def test_when_the_payment_cannot_be_checked_the_grant_waits_and_retries(env, monkeypatch):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active", period_end=PERIOD_END)
    main = env["main"]
    real = main.stripe_api_get

    def down(path):
        if path.startswith("/v1/payment_intents/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=502, detail="Stripe unavailable")
        return real(path)
    monkeypatch.setattr(main, "stripe_api_get", down)
    assert _deliver(env, _checkout("evt_checkout")).status_code == 503  # Stripe will retry
    assert _plan(env["path"]) is None
    monkeypatch.setattr(main, "stripe_api_get", real)
    assert _deliver(env, _checkout("evt_checkout")).status_code == 200  # the retry grants
    assert _capacity(env) == (8, "local")


# ================================================================ 5. incomplete first payment

def test_an_incomplete_subscription_gets_no_service_until_its_first_payment_completes(env, mail):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "incomplete", period_end=PERIOD_END)
    _deliver(env, _checkout("evt_checkout"))
    _deliver(env, _sub_event("evt_created", "updated", env["stripe"]["sub_A1"], 1_100))
    _deliver(env, _invoice("evt_first_failed", invoice_id="in_first_sub_A1", kind="invoice.payment_failed"))
    assert _capacity(env)[0] == 0 and not [p for p in [_plan(env["path"])] if p and p["status"] == "active"]
    env["stripe"]["sub_A1"]["status"] = "active"  # the customer completes the payment
    _deliver(env, _sub_event("evt_now_active", "updated", env["stripe"]["sub_A1"], 1_200))
    assert _plan(env["path"])["status"] == "active" and _capacity(env) == (8, "local")


def test_an_incomplete_subscription_that_expires_never_gets_service(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "incomplete", period_end=PERIOD_END)
    _deliver(env, _checkout("evt_checkout"))
    env["stripe"]["sub_A1"]["status"] = "incomplete_expired"
    _deliver(env, _sub_event("evt_expired", "updated", env["stripe"]["sub_A1"], 1_300))
    assert _capacity(env)[0] == 0


def test_an_incomplete_add_on_grants_no_feature(env, monkeypatch):
    import analytics_entitlements as ae
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", "price_talk")
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    env["stripe"]["sub_T"] = _subscription("sub_T", "cus_A", "incomplete", price="price_talk")
    _deliver(env, _checkout("evt_t", sub_id="sub_T", price="price_talk"))
    _deliver(env, _sub_event("evt_t_upd", "updated", env["stripe"]["sub_T"], 1_100))
    with override_target(sqlite_path=str(env["path"])):
        assert ae.get_active_analytics_for_customer("cust-A") == []


# ================================================================ 6. late failure after payment

def test_a_late_failure_for_an_invoice_already_paid_never_restarts_grace(env):
    _customer(env)
    _deliver(env, _invoice("evt_r2_paid", invoice_id="in_r2"))
    for n in range(2):
        _deliver(env, _invoice(f"evt_r2_failed_late_{n}", invoice_id="in_r2", kind="invoice.payment_failed"))
    assert _plan(env["path"])["payment_failed_at"] is None
    _sweep(env, NOW + timedelta(days=30))
    assert _capacity(env) == (8, "local")


def test_stripe_saying_the_invoice_is_paid_wins_even_before_the_paid_event(env):
    _customer(env)
    env["invoices"]["in_r3"] = {"id": "in_r3", "status": "paid", "amount_paid": 1499}
    _deliver(env, _invoice("evt_r3_failed", invoice_id="in_r3", kind="invoice.payment_failed"))
    assert _plan(env["path"])["payment_failed_at"] is None


def test_failure_then_payment_then_a_replayed_failure_stays_recovered(env):
    _customer(env)
    _deliver(env, _invoice("evt_f", invoice_id="in_r4", kind="invoice.payment_failed"))
    assert _plan(env["path"])["payment_failed_at"] is not None
    env["invoices"]["in_r4"] = {"id": "in_r4", "status": "paid", "amount_paid": 1499}
    _deliver(env, _invoice("evt_p", invoice_id="in_r4"))
    _deliver(env, _invoice("evt_f_retry_attempt", invoice_id="in_r4", kind="invoice.payment_failed"))
    assert _plan(env["path"])["payment_failed_at"] is None
    _deliver(env, _invoice("evt_next_fails", invoice_id="in_r5", kind="invoice.payment_failed"))  # a genuine new failure
    assert _plan(env["path"])["payment_failed_at"] is not None


# ================================================================ 7. commission ownership

def test_an_invoice_naming_another_account_changes_neither_ledger(env):
    _customer(env)  # cust-A pays through cus_A / sub_A1
    _customer(env, customer_id="cust-B", stripe_customer="cus_B", sub_id="sub_B1")  # cust-B has a salesperson
    forged = _invoice("evt_forged", customer="cus_A", sub_id="sub_A1", customer_id="cust-B", invoice_id="in_forged")
    assert _deliver(env, forged).status_code == 200
    assert _count(env, "SELECT COUNT(*) FROM subscription_payments") == 0
    assert _count(env, "SELECT COUNT(*) FROM commission_ledger") == 0


def test_an_invoice_whose_subscription_belongs_to_another_stripe_customer_is_refused(env):
    _customer(env)
    _customer(env, customer_id="cust-B", stripe_customer="cus_B", sub_id="sub_B1")
    crossed = _invoice("evt_crossed", customer="cus_B", sub_id="sub_A1", customer_id="cust-B", invoice_id="in_crossed")
    _deliver(env, crossed)
    assert _count(env, "SELECT COUNT(*) FROM subscription_payments") == 0
    genuine = _invoice("evt_genuine", customer="cus_B", sub_id="sub_B1", customer_id="cust-B", invoice_id="in_genuine")
    _deliver(env, genuine)
    assert _count(env, "SELECT COUNT(*) FROM subscription_payments WHERE customer_id='cust-B'") == 1


# ================================================================ 8. failed-renewal email

def test_a_failed_renewal_is_emailed_once_per_invoice(env, mail):
    _customer(env)
    for n in range(3):  # Stripe retries the charge and resends events
        _deliver(env, _invoice(f"evt_fail_{n}", invoice_id="in_r6", kind="invoice.payment_failed"))
    emails = [m for m in mail["sent"] if m["type"] == "payment_failed"]
    assert len(emails) == 1 and emails[0]["to"] == "a@example.test"
    assert "payment" in emails[0]["subject"].lower() and "/subscription-portal" in emails[0]["text"]
    assert "staging" not in (emails[0]["subject"] + emails[0]["text"]).lower()


def test_an_undelivered_failure_email_is_retried_by_the_billing_worker(env, mail):
    import purchase_notifications
    _customer(env)
    mail["failing"].append(True)
    _deliver(env, _invoice("evt_fail", invoice_id="in_r7", kind="invoice.payment_failed"))
    assert not [m for m in mail["sent"] if m["type"] == "payment_failed"]
    mail["failing"].clear()
    with override_target(sqlite_path=str(env["path"])):
        assert purchase_notifications.retry_payment_failed_notifications() == 1
        assert purchase_notifications.retry_payment_failed_notifications() == 0
    assert len([m for m in mail["sent"] if m["type"] == "payment_failed"]) == 1


def test_no_failure_email_for_an_invoice_that_was_paid(env, mail):
    _customer(env)
    _deliver(env, _invoice("evt_paid", invoice_id="in_r8"))
    _deliver(env, _invoice("evt_fail_late", invoice_id="in_r8", kind="invoice.payment_failed"))
    assert not [m for m in mail["sent"] if m["type"] == "payment_failed"]


# ================================================================ 9. fail closed

def test_without_a_stripe_key_subscription_events_are_retried_not_applied(env, monkeypatch):
    _customer(env)
    monkeypatch.setattr(env["main"], "STRIPE_SECRET_KEY", "")
    stale = _sub_event("evt_snapshot_cancel", "deleted", _subscription("sub_A1", "cus_A", "canceled"), 9_000)
    assert _deliver(env, stale).status_code == 503
    assert _plan(env["path"])["status"] == "active"
    monkeypatch.setattr(env["main"], "STRIPE_SECRET_KEY", "sk_test_launch")
    assert _deliver(env, stale).status_code == 200  # Stripe says the subscription is still active
    assert _plan(env["path"])["status"] == "active"


def test_an_unreadable_subscription_is_retried_and_its_snapshot_never_applied(env, monkeypatch):
    _customer(env)
    real = env["main"].stripe_api_get

    def down(path):
        if path.startswith("/v1/subscriptions/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=502, detail="Stripe unavailable")
        return real(path)
    monkeypatch.setattr(env["main"], "stripe_api_get", down)
    for kind in ("deleted", "updated"):
        snapshot = _sub_event(f"evt_{kind}", kind, _subscription("sub_A1", "cus_A", "canceled"), 9_000)
        assert _deliver(env, snapshot).status_code == 503
    assert _plan(env["path"])["status"] == "active" and _capacity(env) == (8, "local")


def test_without_a_stripe_key_an_add_on_checkout_grants_nothing(env, monkeypatch):
    import analytics_entitlements as ae
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", "price_talk")
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    monkeypatch.setattr(env["main"], "STRIPE_SECRET_KEY", "")
    env["stripe"]["sub_T"] = _subscription("sub_T", "cus_A", "active", price="price_talk")
    assert _deliver(env, _checkout("evt_t", sub_id="sub_T", price="price_talk")).status_code == 503
    with override_target(sqlite_path=str(env["path"])):
        assert ae.get_active_analytics_for_customer("cust-A") == []
