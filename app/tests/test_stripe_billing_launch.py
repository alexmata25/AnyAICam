"""Stripe/billing launch hardening (2026-10-02), adversarial.

Every webhook here is signed with a real test secret and goes through the
real /api/payments/stripe/webhook route (no signature bypass). Stripe's
API is mocked: `stripe` holds what Stripe would answer *now* for each
subscription (authoritative current state), and every write is captured.
No live Stripe, no real products/prices/customers.

Covers: stale/out-of-order events, cross-tenant Stripe ids, mismatched
customer/subscription, unresolvable events (retried, not lost), replayed
webhooks, duplicate checkout, Billing Portal authorization, and
direct-vs-partner attribution.
"""
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target
from partner_db import connection, initialize_database

SECRET = "whsec_test_launch"
LOCAL_8, HYBRID_8 = "price_local_8", "price_hybrid_8"


@pytest.fixture()
def env(tmp_path, monkeypatch, fake_stripe_prices):
    path = tmp_path / "billing.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('anyaicam-primary','AnyAiCam','approved','real','2026-01-01')")
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','Installer Co','approved','real','2026-01-01')")
            for cid, partner, email, channel, created_by in (
                    ("cust-A", "anyaicam-primary", "a@example.test", "direct", "direct-signup"),
                    ("cust-B", "partner-1", "b@example.test", None, "sales@example.test")):
                db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at,created_by,onboarding_channel) "
                           "VALUES(?,?,?,?,'active','real','2026-01-01',?,?)", (cid, partner, cid, email, created_by, channel))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) "
                       "VALUES('sales-1','partner-1','sales@example.test','Sam','salesperson','x',1,'2026-01-01')")
        import customer_entitlements as ce
        import main
        monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", SECRET)
        monkeypatch.setattr(main, "STRIPE_SECRET_KEY", "sk_test_launch")
        monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
        for name in ("USERS_FILE", "SESSIONS_FILE", "PAYMENT_WEBHOOK_EVENTS_FILE", "PAYMENT_SESSIONS_FILE", "BILLING_ACCOUNTS_FILE"):
            monkeypatch.setattr(main, name, tmp_path / f"{name.lower()}.json")
        monkeypatch.setenv("ANYAICAM_STRIPE_BILLING_PORTAL_ENABLED", "true")  # off by default: owner decision on portal capabilities
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_LOCAL_1_8", LOCAL_8)
        monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", HYBRID_8)
        monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", ce._load_price_tier_map())
        stripe: dict = {}
        posts: list = []
        price_lookup = main.stripe_api_get  # conftest's fake_stripe_prices: published catalog amounts

        def fake_get(path):
            if path.startswith("/v1/prices/"):
                return price_lookup(path)
            if path.startswith("/v1/subscriptions/"):
                found = stripe.get(path.rsplit("/", 1)[1])
                if found is None:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=502, detail="not found")
                return found
            raise AssertionError(f"unexpected Stripe GET {path}")

        def fake_post(path, fields):
            posts.append((path, dict(fields)))
            if path == "/v1/billing_portal/sessions":
                return {"id": "bps_1", "url": "https://billing.stripe.test/session"}
            return {"id": f"cs_{len(posts)}", "url": f"https://checkout.stripe.test/{len(posts)}"}
        monkeypatch.setattr(main, "stripe_api_get", fake_get)
        monkeypatch.setattr(main, "stripe_api_post", fake_post)
        with TestClient(main.app, follow_redirects=False) as client:
            yield {"client": client, "stripe": stripe, "posts": posts, "path": path, "tmp": tmp_path, "main": main}


def _deliver(env, event):
    payload = json.dumps(event).encode()
    stamp = int(time.time())
    signature = hmac.new(SECRET.encode(), str(stamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return env["client"].post("/api/payments/stripe/webhook", content=payload,
                              headers={"stripe-signature": f"t={stamp},v1={signature}", "content-type": "application/json"})


def _subscription(sub_id, customer, status, price=LOCAL_8, metadata_customer="cust-A"):
    return {"id": sub_id, "object": "subscription", "customer": customer, "status": status,
            "items": {"data": [{"price": {"id": price}, "quantity": 1}]},
            "metadata": {"anyaicam_customer_id": metadata_customer, "anyaicam_stripe_price_id": price}}


def _checkout(event_id, *, customer_id="cust-A", stripe_customer="cus_A", sub_id="sub_A1", price=LOCAL_8, created=1_000):
    return {"id": event_id, "type": "checkout.session.completed", "created": created, "data": {"object": {
        "id": f"cs_{event_id}", "object": "checkout.session", "mode": "subscription", "payment_status": "paid",
        "customer": stripe_customer, "subscription": sub_id, "customer_details": {"email": "a@example.test"},
        "metadata": {"anyaicam_customer_id": customer_id, "anyaicam_stripe_price_id": price}}}}


def _sub_event(event_id, kind, subscription, created):
    return {"id": event_id, "type": f"customer.subscription.{kind}", "created": created, "data": {"object": subscription}}


def _plan(path, customer_id="cust-A", product="camera_slots_local"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    found = conn.execute("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (customer_id, product)).fetchone()
    conn.close()
    return dict(found) if found else None


def _cookie(email, role, customer_id):
    import partner_portal
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, None, customer_id, None)}


OWNER_A = ("a@example.test", "customer_owner", "cust-A")


# ================================================================ stale / out-of-order events

def test_a_delayed_older_active_update_never_revives_a_cancelled_subscription(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    assert _deliver(env, _checkout("evt_1", created=1_000)).status_code == 200
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "canceled")
    _deliver(env, _sub_event("evt_3", "deleted", env["stripe"]["sub_A1"], 3_000))
    assert _plan(env["path"])["status"] == "cancelled"
    stale = _sub_event("evt_2", "updated", _subscription("sub_A1", "cus_A", "active"), 2_000)  # older, delivered last
    assert _deliver(env, stale).status_code == 200
    assert _plan(env["path"])["status"] == "cancelled"  # Stripe says canceled now


def test_a_delayed_checkout_after_cancellation_grants_nothing(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "canceled")
    _deliver(env, _sub_event("evt_9", "deleted", env["stripe"]["sub_A1"], 9_000))
    _deliver(env, _checkout("evt_1", created=1_000))
    plan = _plan(env["path"])
    assert plan is None or (plan["status"] == "cancelled" and plan["camera_slot_quantity"] == 0)


def test_events_in_any_order_converge_on_stripes_current_state(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active", price=LOCAL_8)
    _deliver(env, _sub_event("evt_5", "updated", _subscription("sub_A1", "cus_A", "past_due"), 5_000))  # before its checkout
    _deliver(env, _checkout("evt_1", created=1_000))
    plan = _plan(env["path"])
    assert plan["status"] == "active" and plan["camera_slot_quantity"] == 8


# ================================================================ cross-tenant / mismatched ids

def test_a_stripe_customer_belonging_to_account_a_never_provisions_account_b(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    env["stripe"]["sub_X"] = _subscription("sub_X", "cus_A", "active", metadata_customer="cust-B")
    _deliver(env, _checkout("evt_2", customer_id="cust-B", stripe_customer="cus_A", sub_id="sub_X"))
    assert _plan(env["path"], "cust-B") is None


def test_a_subscription_whose_customer_and_metadata_disagree_changes_neither_account(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    hostile = _subscription("sub_A1", "cus_A", "canceled", metadata_customer="cust-B")
    env["stripe"]["sub_A1"] = hostile
    _deliver(env, _sub_event("evt_2", "deleted", hostile, 2_000))
    assert _plan(env["path"])["status"] == "active" and _plan(env["path"], "cust-B") is None


def test_an_event_for_no_known_account_fabricates_nothing(env):
    """Our flows create the account before any checkout, so such an event is
    a deleted account or unrelated Stripe activity: recorded as ignored and
    logged -- never fabricated, and never retried for days (which could get
    the endpoint disabled). Any later event applies Stripe's current state."""
    lost = _subscription("sub_Z", "cus_Z", "active", metadata_customer="cust-not-yet")
    env["stripe"]["sub_Z"] = lost
    assert _deliver(env, _sub_event("evt_z", "updated", lost, 1_000)).status_code == 200
    conn = sqlite3.connect(env["path"])
    assert conn.execute("SELECT COUNT(*) FROM customer_entitlements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
    conn.close()


def test_when_stripe_cannot_be_read_the_event_is_retried_not_applied_from_its_snapshot(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    del env["stripe"]["sub_A1"]  # Stripe unreachable / not answering for this subscription
    response = _deliver(env, _sub_event("evt_2", "deleted", _subscription("sub_A1", "cus_A", "canceled"), 2_000))
    assert response.status_code == 503  # Stripe retries it
    assert _plan(env["path"])["status"] == "active"  # nothing applied from the snapshot
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "canceled")
    assert _deliver(env, _sub_event("evt_2", "deleted", _subscription("sub_A1", "cus_A", "canceled"), 2_000)).status_code == 200
    assert _plan(env["path"])["status"] == "cancelled"


# ================================================================ replay / duplicates

def test_replayed_webhooks_never_duplicate_anything(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    event = _checkout("evt_1")
    for _ in range(3):
        assert _deliver(env, event).status_code == 200
    again = dict(event, id="evt_1b")  # Stripe re-sent the same session as a new event
    _deliver(env, again)
    conn = sqlite3.connect(env["path"])
    assert conn.execute("SELECT COUNT(*) FROM customer_entitlements WHERE customer_id='cust-A'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
    conn.close()


def test_a_second_checkout_for_a_plan_already_held_is_refused(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    before = len(env["posts"])
    response = env["client"].post("/api/customer/camera-slots/checkout", json={"plan_type": "local", "tier_label": "1-8"},
                                  cookies=_cookie(*OWNER_A))
    assert response.status_code == 409 and len(env["posts"]) == before  # no second Stripe subscription


# ================================================================ Billing Portal

def _legacy_primary_account(env):
    (env["tmp"] / "billing_accounts_file.json").write_text(json.dumps({"primary": {
        "id": "primary", "billing_email": "ops@anyaicam.test", "external_customer_id": "cus_PRIMARY"}}))


def test_a_customer_can_never_open_another_accounts_billing_portal(env):
    _legacy_primary_account(env)
    response = env["client"].post("/api/payments/customer-portal", json={"return_path": "/subscription-portal"},
                                  cookies=_cookie(*OWNER_A))
    assert not [p for p in env["posts"] if p[0] == "/v1/billing_portal/sessions"], response.text
    assert response.status_code in (401, 403)


def test_the_owner_portal_is_bound_to_their_own_stripe_customer(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    response = env["client"].post("/api/customer/billing-portal", cookies=_cookie(*OWNER_A))
    assert response.status_code == 200 and response.json()["url"].startswith("https://")
    sessions = [p for p in env["posts"] if p[0] == "/v1/billing_portal/sessions"]
    assert sessions and sessions[-1][1]["customer"] == "cus_A"


@pytest.mark.parametrize("cookie,status", [
    (None, (401, 403)),
    (("viewer@example.test", "customer_viewer", "cust-A"), (403,)),
    (("b@example.test", "customer_owner", "cust-B"), (404,)),  # no billing account of their own; never A's
])
def test_portal_access_is_owner_only_and_never_cross_tenant(env, cookie, status):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    response = env["client"].post("/api/customer/billing-portal", cookies=_cookie(*cookie) if cookie else None)
    assert response.status_code in status
    assert not [p for p in env["posts"] if p[0] == "/v1/billing_portal/sessions"]


# ================================================================ direct vs partner attribution

def _invoice(event_id, customer_id, sub_id, stripe_customer):
    return {"id": event_id, "type": "invoice.paid", "created": 5_000, "data": {"object": {
        "id": f"in_{event_id}", "object": "invoice", "customer": stripe_customer, "subscription": sub_id,
        "amount_paid": 1499, "tax": 0, "currency": "usd", "charge": f"ch_{event_id}", "payment_intent": f"pi_{event_id}",
        "subscription_details": {"metadata": {"anyaicam_customer_id": customer_id}},
        "lines": {"data": [{"price": {"id": LOCAL_8}, "amount": 1499}]}}}}


def test_direct_customers_earn_nobody_a_commission_and_partner_customers_still_do(env):
    import sales_commissions
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    env["stripe"]["sub_B1"] = _subscription("sub_B1", "cus_B", "active", metadata_customer="cust-B")
    _deliver(env, _checkout("evt_1"))
    _deliver(env, _checkout("evt_2", customer_id="cust-B", stripe_customer="cus_B", sub_id="sub_B1"))
    for event in (_invoice("evt_i1", "cust-A", "sub_A1", "cus_A"), _invoice("evt_i2", "cust-B", "sub_B1", "cus_B")):
        _deliver(env, event)
        _deliver(env, event)  # replay
    with override_target(sqlite_path=str(env["path"])):
        entries = sales_commissions.ledger_for()
    assert {e["customer_id"] for e in entries} == {"cust-B"}
    assert len([e for e in entries if e["kind"] == "activation"]) == 1  # replay never doubles


def test_the_owner_portal_is_off_until_the_owner_turns_it_on(env, monkeypatch):
    monkeypatch.delenv("ANYAICAM_STRIPE_BILLING_PORTAL_ENABLED")
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    assert env["client"].post("/api/customer/billing-portal", cookies=_cookie(*OWNER_A)).status_code == 404
    assert not [p for p in env["posts"] if p[0] == "/v1/billing_portal/sessions"]


# ================================================================ add-ons follow the same rules

def _addon_env(env, monkeypatch):
    import analytics_entitlements as ae
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_adv")
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())


def _addon_status(path, customer_id):
    conn = sqlite3.connect(path)
    found = conn.execute("SELECT status FROM addon_subscriptions WHERE customer_id=?", (customer_id,)).fetchone()
    conn.close()
    return found[0] if found else None


def test_a_late_add_on_update_never_revives_a_cancelled_add_on(env, monkeypatch):
    _addon_env(env, monkeypatch)
    env["stripe"]["sub_AD"] = _subscription("sub_AD", "cus_A", "active", price="price_adv")
    _deliver(env, _checkout("evt_ad1", sub_id="sub_AD", price="price_adv"))
    assert _addon_status(env["path"], "cust-A") == "active"
    env["stripe"]["sub_AD"] = _subscription("sub_AD", "cus_A", "canceled", price="price_adv")
    _deliver(env, _sub_event("evt_ad3", "deleted", env["stripe"]["sub_AD"], 3_000))
    _deliver(env, _sub_event("evt_ad2", "updated", _subscription("sub_AD", "cus_A", "active", price="price_adv"), 2_000))
    assert _addon_status(env["path"], "cust-A") == "cancelled"


def test_an_add_on_paid_by_account_as_stripe_customer_never_provisions_b(env, monkeypatch):
    _addon_env(env, monkeypatch)
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    env["stripe"]["sub_ADB"] = _subscription("sub_ADB", "cus_A", "active", price="price_adv", metadata_customer="cust-B")
    _deliver(env, _checkout("evt_ad", customer_id="cust-B", stripe_customer="cus_A", sub_id="sub_ADB", price="price_adv"))
    assert _addon_status(env["path"], "cust-B") is None
