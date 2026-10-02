"""Owner billing policies approved 2026-10-02, through the real webhook route
and customer APIs (signed webhooks, Stripe mocked as current state; no live
Stripe). See billing_status.py, plan_changes.py and customer_billing.py.

1. Failed payment: 7-day grace, recovery within it, suspension after it,
   restoration by a later payment; replays never move the grace start.
2. Cancellation at the end of the paid period, reversible until then.
3. Customer Portal: owner-only, bound to the account's Stripe customer, and
   only when Stripe's portal configuration matches these policies.
4. Refunds and disputes affect only the purchase they belong to.
5. Hybrid -> Local scheduled for the next renewal, reversible until then;
   one base plan after the switch.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_stripe_billing_launch import (  # noqa: F401 -- fixtures and helpers
    HYBRID_8, LOCAL_8, OWNER_A, _checkout, _cookie, _deliver, _plan, _sub_event, _subscription, env,
)

NOW = datetime.now().replace(microsecond=0)
PERIOD_END = int((NOW + timedelta(days=20)).timestamp())


def _capacity(env, customer_id="cust-A"):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(env["path"])):
        return ce.total_camera_slots(customer_id), ce.product_mode_for_customer(customer_id)


def _sweep(env, at):
    import billing_status
    with override_target(sqlite_path=str(env["path"])):
        return billing_status.sweep_grace(at)


def _customer(env, *, price=LOCAL_8, customer_id="cust-A", stripe_customer="cus_A", sub_id="sub_A1"):
    env["stripe"][sub_id] = _subscription(sub_id, stripe_customer, "active", price=price, metadata_customer=customer_id,
                                          period_end=PERIOD_END)
    assert _deliver(env, _checkout(f"evt_co_{sub_id}", customer_id=customer_id, stripe_customer=stripe_customer,
                                   sub_id=sub_id, price=price)).status_code == 200


def _invoice(event_id, *, sub_id="sub_A1", customer="cus_A", customer_id="cust-A", invoice_id=None, kind="invoice.paid",
             created=None, price=LOCAL_8):
    invoice_id = invoice_id or f"in_{event_id}"
    return {"id": event_id, "type": kind, "created": created or int(NOW.timestamp()), "data": {"object": {
        "id": invoice_id, "object": "invoice", "customer": customer, "subscription": sub_id,
        "amount_paid": 1499 if kind == "invoice.paid" else 0, "amount_due": 1499, "tax": 0, "currency": "usd",
        "charge": f"ch_{invoice_id}", "payment_intent": f"pi_{invoice_id}",
        "subscription_details": {"metadata": {"anyaicam_customer_id": customer_id}},
        "lines": {"data": [{"price": {"id": price}, "amount": 1499}]}}}}


def _refund(event_id, invoice_id, *, amount=1499, refunded=1499):
    return {"id": event_id, "type": "charge.refunded", "data": {"object": {
        "id": f"ch_{invoice_id}", "object": "charge", "payment_intent": f"pi_{invoice_id}", "amount": amount,
        "amount_refunded": refunded, "refunded": refunded >= amount}}}


def _dispute(event_id, invoice_id, kind="charge.dispute.created", status="needs_response"):
    return {"id": event_id, "type": kind, "data": {"object": {
        "id": f"dp_{invoice_id}", "object": "dispute", "charge": f"ch_{invoice_id}", "payment_intent": f"pi_{invoice_id}",
        "status": status}}}


def _page(env):
    return env["client"].get("/subscription-portal", cookies=_cookie(*OWNER_A)).text


# ================================================================ 1. failed payment: 7-day grace

def test_a_failed_renewal_keeps_service_for_7_days_and_tells_the_customer(env):
    _customer(env)
    failed_at = NOW
    _deliver(env, _invoice("evt_f1", kind="invoice.payment_failed", created=int(failed_at.timestamp())))
    _sweep(env, failed_at + timedelta(days=7) - timedelta(seconds=1))  # the last second of grace
    assert _plan(env["path"])["status"] == "active" and _capacity(env) == (8, "local")
    page = _page(env)
    assert "latest payment for this plan failed" in page and "Manage billing" in page


def test_after_7_days_unpaid_the_plan_is_suspended_not_duplicated(env):
    _customer(env)
    _deliver(env, _invoice("evt_f1", kind="invoice.payment_failed", created=int(NOW.timestamp())))
    _sweep(env, NOW + timedelta(days=7))
    plan = _plan(env["path"])
    assert plan["status"] == "suspended" and plan["suspended_reason"] == "payment_failed" and _capacity(env) == (0, "")
    assert "This plan is paused" in _page(env)
    buy = env["client"].post("/api/customer/camera-slots/checkout", json={"plan_type": "local", "tier_label": "1-8"},
                             cookies=_cookie(*OWNER_A))
    assert buy.status_code == 409  # no second subscription while one is held


def test_payment_within_grace_restores_normal_state(env):
    _customer(env)
    _deliver(env, _invoice("evt_f1", kind="invoice.payment_failed", created=int(NOW.timestamp())))
    _deliver(env, _invoice("evt_p2"))  # the retry succeeded on day 3
    _sweep(env, NOW + timedelta(days=30))
    assert _plan(env["path"])["status"] == "active" and _plan(env["path"])["payment_failed_at"] is None


def test_payment_after_suspension_restores_exactly_once(env):
    _customer(env)
    _deliver(env, _invoice("evt_f1", kind="invoice.payment_failed", created=int(NOW.timestamp())))
    _sweep(env, NOW + timedelta(days=8))
    paid = _invoice("evt_p2")
    for _ in range(2):
        _deliver(env, paid)  # replay
    assert _plan(env["path"])["status"] == "active" and _capacity(env) == (8, "local")
    conn = sqlite3.connect(env["path"])
    assert conn.execute("SELECT COUNT(*) FROM customer_entitlements WHERE customer_id='cust-A'").fetchone()[0] == 1
    conn.close()


def test_repeated_failures_never_move_the_grace_start(env):
    _customer(env)
    _deliver(env, _invoice("evt_f1", kind="invoice.payment_failed", created=int(NOW.timestamp())))
    _deliver(env, _invoice("evt_f2", kind="invoice.payment_failed", created=int((NOW + timedelta(days=5)).timestamp())))
    _sweep(env, NOW + timedelta(days=7))
    assert _plan(env["path"])["status"] == "suspended"


def test_a_subscription_update_never_lifts_a_suspension_by_itself(env):
    _customer(env)
    _deliver(env, _invoice("evt_f1", kind="invoice.payment_failed", created=int(NOW.timestamp())))
    _sweep(env, NOW + timedelta(days=8))
    env["stripe"]["sub_A1"]["status"] = "past_due"
    _deliver(env, _sub_event("evt_u", "updated", env["stripe"]["sub_A1"], 9_000))
    assert _plan(env["path"])["status"] == "suspended"


def test_stripe_past_due_status_alone_starts_the_grace(env):
    _customer(env)
    env["stripe"]["sub_A1"]["status"] = "past_due"
    _deliver(env, _sub_event("evt_u", "updated", env["stripe"]["sub_A1"], 9_000))
    assert _plan(env["path"])["payment_failed_at"] is not None and _plan(env["path"])["status"] == "active"


def test_an_add_on_follows_the_same_grace(env, monkeypatch):
    import analytics_entitlements as ae
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_adv")
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    _customer(env)
    env["stripe"]["sub_AD"] = _subscription("sub_AD", "cus_A", "active", price="price_adv")
    _deliver(env, _checkout("evt_ad", sub_id="sub_AD", price="price_adv"))
    _deliver(env, _invoice("evt_af", kind="invoice.payment_failed", sub_id="sub_AD", created=int(NOW.timestamp()), price="price_adv"))
    with override_target(sqlite_path=str(env["path"])):
        assert ae.get_active_analytics_for_customer("cust-A")
    _sweep(env, NOW + timedelta(days=7))
    with override_target(sqlite_path=str(env["path"])):
        assert ae.get_active_analytics_for_customer("cust-A") == []
    _deliver(env, _invoice("evt_ap", sub_id="sub_AD", price="price_adv"))
    with override_target(sqlite_path=str(env["path"])):
        assert ae.get_active_analytics_for_customer("cust-A")
    assert _plan(env["path"])["status"] == "active"  # the camera plan was never touched


# ================================================================ 2. cancellation at period end

def test_cancel_schedules_the_end_and_resume_keeps_the_same_subscription(env):
    _customer(env)
    cancel = env["client"].post("/api/customer/plan/cancel", cookies=_cookie(*OWNER_A))
    assert cancel.status_code == 200 and cancel.json()["ends_at"] == PERIOD_END
    assert env["stripe"]["sub_A1"]["cancel_at_period_end"] is True
    assert _plan(env["path"])["status"] == "active"  # paid period continues
    page = _page(env)
    assert "set to end on" in page and "Keep my plan" in page
    again = env["client"].post("/api/customer/plan/cancel", cookies=_cookie(*OWNER_A))  # a repeat sends nothing new
    assert again.status_code == 200 and len([p for p in env["posts"] if p[1].get("cancel_at_period_end") == "true"]) == 1
    resume = env["client"].post("/api/customer/plan/resume", cookies=_cookie(*OWNER_A))
    assert resume.status_code == 200 and env["stripe"]["sub_A1"]["cancel_at_period_end"] is False
    assert not [p for p in env["posts"] if p[0] == "/v1/checkout/sessions"]
    assert "Renews on" in _page(env)


def test_at_period_end_a_cancelled_plan_ends(env):
    _customer(env)
    env["client"].post("/api/customer/plan/cancel", cookies=_cookie(*OWNER_A))
    env["stripe"]["sub_A1"]["status"] = "canceled"
    _deliver(env, _sub_event("evt_end", "deleted", env["stripe"]["sub_A1"], 9_000))
    assert _plan(env["path"])["status"] == "cancelled" and _capacity(env) == (0, "")


@pytest.mark.parametrize("endpoint", ["cancel", "resume", "downgrade-to-local", "cancel-downgrade"])
@pytest.mark.parametrize("cookie", [("viewer@example.test", "customer_viewer", "cust-A"), ("b@example.test", "customer_owner", "cust-B")])
def test_only_the_owner_changes_their_own_plan(env, endpoint, cookie):
    _customer(env, price=HYBRID_8)
    response = env["client"].post(f"/api/customer/plan/{endpoint}", cookies=_cookie(*cookie))
    assert response.status_code in (403, 409)
    assert not [p for p in env["posts"] if p[0].startswith(("/v1/subscriptions/", "/v1/subscription_schedules"))]


# ================================================================ 3. Customer Portal

def test_the_owner_portal_uses_the_policy_checked_configuration(env):
    _customer(env)
    response = env["client"].post("/api/customer/billing-portal", cookies=_cookie(*OWNER_A))
    assert response.status_code == 200
    session = [p for p in env["posts"] if p[0] == "/v1/billing_portal/sessions"][-1][1]
    assert session["customer"] == "cus_A" and session["configuration"] == "bpc_policy"


@pytest.mark.parametrize("breaks", [
    {"subscription_cancel": {"enabled": True, "mode": "immediately"}},
    {"subscription_update": {"enabled": True, "proration_behavior": "create_prorations",
                             "schedule_at_period_end": {"conditions": [{"type": "decreasing_item_amount"}]}}},
    {"subscription_update": {"enabled": True, "proration_behavior": "always_invoice"}},  # downgrades would be immediate
    {"payment_method_update": {"enabled": False}},
])
def test_a_portal_configured_against_policy_is_refused(env, breaks):
    _customer(env)
    env["portal_config"]["features"].update(breaks)
    response = env["client"].post("/api/customer/billing-portal", cookies=_cookie(*OWNER_A))
    assert response.status_code == 503 and not [p for p in env["posts"] if p[0] == "/v1/billing_portal/sessions"]


# ================================================================ 4. refunds and disputes

def _paid_period(env, event_id="evt_p1", invoice_id="in_p1"):
    _deliver(env, _invoice(event_id, invoice_id=invoice_id))


def test_a_full_refund_of_the_current_period_suspends_that_plan_only(env, monkeypatch):
    import analytics_entitlements as ae
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_adv")
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    _customer(env)
    env["stripe"]["sub_AD"] = _subscription("sub_AD", "cus_A", "active", price="price_adv")
    _deliver(env, _checkout("evt_ad", sub_id="sub_AD", price="price_adv"))
    _paid_period(env)
    for _ in range(2):  # replayed refund
        _deliver(env, _refund("evt_r1", "in_p1"))
    plan = _plan(env["path"])
    assert plan["status"] == "suspended" and plan["suspended_reason"] == "refunded" and _capacity(env) == (0, "")
    with override_target(sqlite_path=str(env["path"])):
        assert ae.get_active_analytics_for_customer("cust-A")  # the add-on was a different purchase


def test_a_partial_refund_leaves_service_unchanged(env):
    _customer(env)
    _paid_period(env)
    _deliver(env, _refund("evt_r1", "in_p1", refunded=500))
    assert _plan(env["path"])["status"] == "active"


def test_refunding_an_older_period_does_not_stop_the_current_paid_one(env):
    _customer(env)
    _paid_period(env, "evt_p1", "in_p1")
    _paid_period(env, "evt_p2", "in_p2")
    _deliver(env, _refund("evt_r1", "in_p1"))
    assert _plan(env["path"])["status"] == "active"


def test_a_late_paid_event_for_a_refunded_payment_never_restores_it(env):
    _customer(env)
    _paid_period(env)
    _deliver(env, _refund("evt_r1", "in_p1"))
    _deliver(env, _invoice("evt_late", invoice_id="in_p1"))  # a different event for the same, refunded payment
    assert _plan(env["path"])["status"] == "suspended"
    _paid_period(env, "evt_p2", "in_p2")  # a genuinely new payment restores it
    assert _plan(env["path"])["status"] == "active"


def test_a_dispute_suspends_and_only_a_won_dispute_restores(env):
    _customer(env)
    _paid_period(env)
    _deliver(env, _dispute("evt_d1", "in_p1"))
    assert _plan(env["path"])["suspended_reason"] == "disputed"
    _deliver(env, _dispute("evt_d2", "in_p1", kind="charge.dispute.closed", status="lost"))
    assert _plan(env["path"])["status"] == "suspended"
    _deliver(env, _dispute("evt_d3", "in_p1", kind="charge.dispute.closed", status="won"))
    assert _plan(env["path"])["status"] == "active"


def test_a_refunded_vms_license_stops_the_installer_download(env, monkeypatch):
    import customer_entitlements as ce
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8", "price_vms_8")
    monkeypatch.setattr(ce, "PRICE_ID_VMS_LICENSE_MAP", ce._load_vms_license_map())
    event = {"id": "evt_lic", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_lic", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "customer": "cus_A",
        "payment_intent": "pi_license", "customer_details": {"email": "a@example.test"},
        "metadata": {"anyaicam_customer_id": "cust-A", "anyaicam_stripe_price_id": "price_vms_8"}}}}
    _deliver(env, event)
    with override_target(sqlite_path=str(env["path"])):
        assert ce.vms_license_capacity("cust-A") == 8
    _deliver(env, {"id": "evt_lr", "type": "charge.refunded", "data": {"object": {
        "id": "ch_license", "object": "charge", "payment_intent": "pi_license", "amount": 4999, "amount_refunded": 4999, "refunded": True}}})
    with override_target(sqlite_path=str(env["path"])):
        assert ce.vms_license_capacity("cust-A") == 0
    assert env["client"].get("/api/customer/downloads/vms-installer", cookies=_cookie(*OWNER_A)).status_code == 403


def test_a_refund_for_another_accounts_payment_never_touches_this_account(env):
    _customer(env)
    _customer(env, customer_id="cust-B", stripe_customer="cus_B", sub_id="sub_B1")
    _deliver(env, _invoice("evt_pb", sub_id="sub_B1", customer="cus_B", customer_id="cust-B", invoice_id="in_b1"))
    _deliver(env, _refund("evt_rb", "in_b1"))
    assert _plan(env["path"], "cust-B")["status"] == "suspended"
    assert _plan(env["path"])["status"] == "active"


# ================================================================ 5. Hybrid -> Local at renewal

def test_downgrade_is_scheduled_for_renewal_and_hybrid_stays_until_then(env):
    _customer(env, price=HYBRID_8)
    response = env["client"].post("/api/customer/plan/downgrade-to-local", cookies=_cookie(*OWNER_A))
    assert response.status_code == 200 and response.json()["effective_at"] == PERIOD_END
    schedule = env["schedules"]["sub_sched_sub_A1"]
    assert schedule["phases"][0]["items"][0]["price"] == HYBRID_8 and schedule["phases"][0]["end_date"] == PERIOD_END
    assert schedule["phases"][1]["items"][0]["price"] == LOCAL_8 and schedule["fields"]["proration_behavior"] == "none"
    assert _capacity(env) == (8, "hybrid") and _plan(env["path"], product="camera_slots_hybrid")["status"] == "active"
    page = _page(env)
    assert "then your plan becomes Local" in page and "Keep Hybrid" in page
    again = env["client"].post("/api/customer/plan/downgrade-to-local", cookies=_cookie(*OWNER_A))
    assert again.json()["status"] == "already_scheduled"
    assert len([p for p in env["posts"] if p[0] == "/v1/subscription_schedules"]) == 1


def test_keeping_hybrid_releases_the_schedule(env):
    _customer(env, price=HYBRID_8)
    env["client"].post("/api/customer/plan/downgrade-to-local", cookies=_cookie(*OWNER_A))
    keep = env["client"].post("/api/customer/plan/cancel-downgrade", cookies=_cookie(*OWNER_A))
    assert keep.status_code == 200 and env["stripe"]["sub_A1"].get("schedule") is None
    hybrid = _plan(env["path"], product="camera_slots_hybrid")
    assert hybrid["status"] == "active" and hybrid["scheduled_change"] is None
    assert "Switch to Local at renewal" in _page(env)


def test_at_renewal_one_local_plan_replaces_hybrid(env):
    _customer(env, price=HYBRID_8)
    env["client"].post("/api/customer/plan/downgrade-to-local", cookies=_cookie(*OWNER_A))
    sub = env["stripe"]["sub_A1"]  # Stripe applies phase 2 at the renewal
    sub["items"]["data"][0]["price"] = {"id": LOCAL_8}
    sub["schedule"] = None
    sub["current_period_end"] = PERIOD_END + 30 * 86400
    for _ in range(2):
        _deliver(env, _sub_event("evt_renew", "updated", sub, 9_000))
    assert _plan(env["path"], product="camera_slots_local")["status"] == "active"
    assert _plan(env["path"], product="camera_slots_hybrid")["status"] == "superseded"
    assert _capacity(env) == (8, "local")


def test_a_local_plan_cannot_be_downgraded(env):
    _customer(env)
    assert env["client"].post("/api/customer/plan/downgrade-to-local", cookies=_cookie(*OWNER_A)).status_code == 409


def test_cancelling_a_plan_with_a_scheduled_downgrade_drops_the_downgrade(env):
    _customer(env, price=HYBRID_8)
    env["client"].post("/api/customer/plan/downgrade-to-local", cookies=_cookie(*OWNER_A))
    env["client"].post("/api/customer/plan/cancel", cookies=_cookie(*OWNER_A))
    assert env["stripe"]["sub_A1"].get("schedule") is None and env["stripe"]["sub_A1"]["cancel_at_period_end"] is True
    assert _plan(env["path"], product="camera_slots_hybrid")["scheduled_change"] is None
