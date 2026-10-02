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
import copy
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
        keys: list = []  # Idempotency-Key per Stripe write
        declines: set = set()  # subscriptions whose next payment Stripe declines
        price_lookup = main.stripe_api_get  # conftest's fake_stripe_prices: published catalog amounts

        portal_config = {"id": "bpc_policy", "features": {
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "subscription_update": {"enabled": False}}}  # plan changes stay in AnyAiCam (owner, 2026-10-02)
        schedules: dict = {}
        invoices: dict = {}  # invoice id -> what Stripe says now (default: a subscription's first invoice is paid)
        intents: dict = {}  # payment intent id -> what Stripe says now (default: succeeded, not refunded)
        sessions: dict = {}  # Checkout Sessions by id; created once per Idempotency-Key
        sessions_by_key: dict = {}
        customers_by_key: dict = {}
        paid_sessions: set = set()  # sessions Stripe will not expire (already paid)

        def fake_get(path):
            if path.startswith("/v1/prices/"):
                return price_lookup(path)
            if path.startswith("/v1/billing_portal/configurations"):
                return {"data": [portal_config]} if "?" in path else portal_config
            if path.startswith("/v1/subscription_schedules/"):
                return schedules[path.rsplit("/", 1)[1]]
            if path.startswith("/v1/invoice_payments"):
                return {"data": []}
            if path.startswith("/v1/invoices/"):
                invoice_id = path.rsplit("/", 1)[1]
                if invoice_id in invoices:
                    return invoices[invoice_id]
                first = invoice_id.startswith("in_first_")
                return {"id": invoice_id, "object": "invoice", "status": "paid" if first else "open",
                        "amount_paid": 1499 if first else 0, "payment_intent": f"pi_{invoice_id}", "charge": f"ch_{invoice_id}"}
            if path.startswith("/v1/payment_intents/"):
                intent_id = path.rsplit("/", 1)[1].split("?", 1)[0]
                return intents.get(intent_id) or {"id": intent_id, "object": "payment_intent", "status": "succeeded",
                                                  "latest_charge": {"id": f"ch_for_{intent_id}", "amount": 1499,
                                                                    "amount_refunded": 0, "refunded": False}}
            if path.startswith("/v1/subscriptions/"):
                found = stripe.get(path.rsplit("/", 1)[1])
                if found is None:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=502, detail="not found")
                return found
            raise AssertionError(f"unexpected Stripe GET {path}")

        replies: dict = {}  # Idempotency-Key -> Stripe's first answer, replayed for the same key (as Stripe does)

        def fake_post(path, fields, idempotency_key=None):
            posts.append((path, dict(fields)))
            keys.append(idempotency_key)
            if idempotency_key and idempotency_key in replies:
                return copy.deepcopy(replies[idempotency_key])
            answer = stripe_write(path, fields, idempotency_key)
            if idempotency_key:
                replies[idempotency_key] = copy.deepcopy(answer)
            return answer

        def stripe_write(path, fields, idempotency_key):
            if path == "/v1/billing_portal/sessions":
                return {"id": "bps_1", "url": "https://billing.stripe.test/session"}
            if path == "/v1/customers":  # one customer per Idempotency-Key, as Stripe does
                return customers_by_key.setdefault(idempotency_key or f"none-{len(posts)}",
                                                   {"id": f"cus_new_{len(customers_by_key) + 1}", "object": "customer"})
            if path == "/v1/checkout/sessions":
                if idempotency_key in sessions_by_key:  # Stripe replays the original answer
                    return sessions_by_key[idempotency_key]
                session_id = f"cs_{len(sessions) + 1}"
                created = {"id": session_id, "url": f"https://checkout.stripe.test/{session_id}", "status": "open", "fields": dict(fields)}
                sessions[session_id] = created
                if idempotency_key:
                    sessions_by_key[idempotency_key] = created
                return created
            if path.startswith("/v1/checkout/sessions/") and path.endswith("/expire"):
                session_id = path.split("/")[4]
                if session_id in paid_sessions or session_id not in sessions:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=502, detail="Stripe refused: session is complete")
                sessions[session_id]["status"] = "expired"
                return sessions[session_id]
            if path == "/v1/subscription_schedules":  # a schedule taken over from a subscription
                sub = stripe[dict(fields)["from_subscription"]]
                schedule_id = f"sub_sched_{sub['id']}"
                schedules[schedule_id] = {"id": schedule_id, "subscription": sub["id"], "phases": [
                    {"start_date": sub.get("current_period_start") or 0, "end_date": sub["current_period_end"],
                     "items": [{"price": sub["items"]["data"][0]["price"]["id"]}]}]}
                sub["schedule"] = schedule_id
                return schedules[schedule_id]
            if path.startswith("/v1/subscription_schedules/") and path.endswith("/release"):
                schedule_id = path.split("/")[3]
                found = schedules.pop(schedule_id, None)
                if found:
                    stripe[found["subscription"]]["schedule"] = None
                return {"id": schedule_id, "status": "released"}
            if path.startswith("/v1/subscription_schedules/"):
                schedule = schedules[path.rsplit("/", 1)[1]]
                values = dict(fields)
                phases = []
                for n in (0, 1):
                    if f"phases[{n}][items][0][price]" in values:
                        start = values.get(f"phases[{n}][start_date]") or (phases[-1]["end_date"] if phases else 0)
                        phases.append({"start_date": int(start), "end_date": int(values.get(f"phases[{n}][end_date]") or 0),
                                       "items": [{"price": values[f"phases[{n}][items][0][price]"]}]})
                schedule["phases"] = phases
                schedule["fields"] = values
                return schedule
            if path.startswith("/v1/subscriptions/") and "cancel_at" in dict(fields):
                sub = stripe[path.rsplit("/", 1)[1]]
                values = dict(fields)
                sub["cancel_at"] = int(values["cancel_at"]) if values["cancel_at"] else None
                sub["proration_behavior_seen"] = values.get("proration_behavior")
                return sub
            if path.startswith("/v1/subscriptions/") and "cancel_at_period_end" in dict(fields):
                sub = stripe[path.rsplit("/", 1)[1]]
                sub["cancel_at_period_end"] = dict(fields)["cancel_at_period_end"] == "true"
                return sub
            if path.startswith("/v1/subscriptions/"):  # Stripe applies an item price change in place
                sub = stripe[path.rsplit("/", 1)[1]]
                values = dict(fields)
                if sub["id"] in declines and values.get("payment_behavior") == "pending_if_incomplete":
                    return dict(sub, pending_update={"subscription_items": [{"price": values["items[0][price]"]}]})
                item = next(i for i in sub["items"]["data"] if i.get("id") == values["items[0][id]"])
                item["price"] = {"id": values["items[0][price]"]}
                sub["metadata"].update({k[len("metadata["):-1]: v for k, v in values.items() if k.startswith("metadata[")})
                return sub
            return {"id": f"cs_{len(posts)}", "url": f"https://checkout.stripe.test/{len(posts)}"}
        monkeypatch.setattr(main, "stripe_api_get", fake_get)
        monkeypatch.setattr(main, "stripe_api_post", fake_post)
        with TestClient(main.app, follow_redirects=False) as client:
            yield {"client": client, "stripe": stripe, "posts": posts, "path": path, "tmp": tmp_path, "main": main,
                   "keys": keys, "declines": declines, "portal_config": portal_config, "schedules": schedules,
                   "invoices": invoices, "intents": intents, "sessions": sessions, "paid_sessions": paid_sessions,
                   "customers_by_key": customers_by_key}


def _deliver(env, event):
    payload = json.dumps(event).encode()
    stamp = int(time.time())
    signature = hmac.new(SECRET.encode(), str(stamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return env["client"].post("/api/payments/stripe/webhook", content=payload,
                              headers={"stripe-signature": f"t={stamp},v1={signature}", "content-type": "application/json"})


def _subscription(sub_id, customer, status, price=LOCAL_8, metadata_customer="cust-A", period_end=1_893_456_000, cancel_at_end=False):
    return {"id": sub_id, "object": "subscription", "customer": customer, "status": status,
            "current_period_end": period_end, "cancel_at_period_end": cancel_at_end, "latest_invoice": f"in_first_{sub_id}",
            "items": {"data": [{"id": f"si_{sub_id}", "price": {"id": price}, "quantity": 1}]},
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


def test_the_owner_portal_can_be_turned_off(env, monkeypatch):
    monkeypatch.setenv("ANYAICAM_STRIPE_BILLING_PORTAL_ENABLED", "false")
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


# ================================================================ one base plan; Local -> Hybrid is an upgrade

def _local_customer(env):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    assert _deliver(env, _checkout("evt_1")).status_code == 200


def _capacity(env, customer_id="cust-A"):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(env["path"])):
        return ce.total_camera_slots(customer_id), ce.product_mode_for_customer(customer_id)


def test_local_to_hybrid_upgrades_the_same_subscription_to_one_base_plan(env):
    _local_customer(env)
    response = env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A))
    assert response.status_code == 200, response.text
    changes = [p for p in env["posts"] if p[0] == "/v1/subscriptions/sub_A1"]
    assert len(changes) == 1 and changes[0][1]["items[0][id]"] == "si_sub_A1" and changes[0][1]["items[0][price]"] == HYBRID_8
    # Owner, 2026-10-02: immediate, prorated and invoiced now, renewal date unchanged,
    # applied only if that payment succeeds.
    assert changes[0][1]["proration_behavior"] == "always_invoice"
    assert changes[0][1]["billing_cycle_anchor"] == "unchanged"
    assert changes[0][1]["payment_behavior"] == "pending_if_incomplete"
    # Keyed per upgrade attempt (plan_upgrade_attempts): same attempt, same key.
    assert env["keys"][env["posts"].index(changes[0])].startswith(f"anyaicam-upgrade-sub_A1-si_sub_A1-{HYBRID_8}-")
    assert not [p for p in env["posts"] if p[0] == "/v1/checkout/sessions"]  # no second subscription
    assert _plan(env["path"], product="camera_slots_hybrid")["status"] == "active"
    assert _plan(env["path"], product="camera_slots_local")["status"] == "superseded"
    assert _capacity(env) == (8, "hybrid")  # one base plan, never 16
    # The webhook Stripe sends for the same change is harmless, and so is a second click.
    _deliver(env, _sub_event("evt_up", "updated", env["stripe"]["sub_A1"], 9_000))
    again = env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A))
    assert again.status_code == 200 and again.json()["status"] == "already_hybrid"
    assert len([p for p in env["posts"] if p[0] == "/v1/subscriptions/sub_A1"]) == 1
    assert _capacity(env) == (8, "hybrid")


def test_a_declined_upgrade_payment_leaves_the_customer_on_local(env):
    _local_customer(env)
    env["declines"].add("sub_A1")
    response = env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A))
    assert response.status_code == 402 and "still Local" in response.json()["detail"]
    assert _plan(env["path"])["status"] == "active" and _plan(env["path"], product="camera_slots_hybrid") is None
    assert _capacity(env) == (8, "local")


def test_a_try_after_a_confirmed_decline_is_a_new_upgrade_attempt(env):
    """Codex verification of 9a388c6, finding 2: once Stripe confirmed an
    attempt was not applied, the customer's next try is a new attempt with a
    new Idempotency-Key (the same key would only replay the cached decline).
    Simultaneous requests for one attempt share one key:
    test_stripe_billing_verification.py."""
    _local_customer(env)
    env["declines"].add("sub_A1")  # the card keeps failing
    for _ in range(2):
        assert env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A)).status_code == 402
    upgrade_keys = [k for (path, _), k in zip(env["posts"], env["keys"]) if path == "/v1/subscriptions/sub_A1"]
    assert len(upgrade_keys) == 2 and len(set(upgrade_keys)) == 2
    assert _capacity(env) == (8, "local") and not [p for p in env["posts"] if p[0] == "/v1/checkout/sessions"]


def test_the_upgrade_button_uses_the_in_place_upgrade(env):
    _local_customer(env)
    page = env["client"].get("/subscription-portal", cookies=_cookie(*OWNER_A)).text
    assert 'id="subscription-upgrade-button"' in page and "/api/customer/plan/upgrade-to-hybrid" in page
    assert "camera-slots/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan_type:'hybrid'" not in page


def test_hybrid_checkout_while_local_is_active_is_refused_and_points_to_the_upgrade(env):
    _local_customer(env)
    response = env["client"].post("/api/customer/camera-slots/checkout", json={"plan_type": "hybrid", "tier_label": "1-8"},
                                  cookies=_cookie(*OWNER_A))
    assert response.status_code == 409 and "Upgrade to Hybrid" in response.json()["detail"]
    assert not [p for p in env["posts"] if p[0] == "/v1/checkout/sessions"]


@pytest.mark.parametrize("cookie", [("viewer@example.test", "customer_viewer", "cust-A"), ("b@example.test", "customer_owner", "cust-B")])
def test_only_the_accounts_owner_can_upgrade_and_never_another_accounts_subscription(env, cookie):
    _local_customer(env)
    response = env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*cookie))
    assert response.status_code in (403, 409)
    assert not [p for p in env["posts"] if p[0].startswith("/v1/subscriptions/")]
    assert _plan(env["path"])["status"] == "active"


def test_an_upgrade_is_refused_if_stripe_no_longer_matches_the_account(env):
    _local_customer(env)
    env["stripe"]["sub_A1"]["metadata"]["anyaicam_customer_id"] = "cust-B"  # tampered / reassigned in Stripe
    response = env["client"].post("/api/customer/plan/upgrade-to-hybrid", cookies=_cookie(*OWNER_A))
    assert response.status_code == 409 and not [p for p in env["posts"] if p[0].startswith("/v1/subscriptions/")]


def test_a_plan_switch_made_in_stripe_also_leaves_one_base_plan(env):
    _local_customer(env)
    env["stripe"]["sub_A1"]["items"]["data"][0]["price"] = {"id": HYBRID_8}  # e.g. changed in Stripe's portal
    _deliver(env, _sub_event("evt_sw", "updated", env["stripe"]["sub_A1"], 5_000))
    assert _plan(env["path"], product="camera_slots_local")["status"] == "superseded"
    assert _capacity(env) == (8, "hybrid")


def test_two_active_base_plans_never_double_camera_capacity(env):
    import customer_entitlements as ce
    with override_target(sqlite_path=str(env["path"])):  # e.g. an upgrade made before this fix: two subscriptions
        ce.upsert_entitlement(customer_id="cust-A", product="camera_slots_local", camera_slot_quantity=8, stripe_subscription_id="sub_L")
        ce.upsert_entitlement(customer_id="cust-A", product="camera_slots_hybrid", camera_slot_quantity=16, stripe_subscription_id="sub_H")
    assert _capacity(env) == (16, "hybrid")


# ================================================================ My subscription billing facts

@pytest.mark.parametrize("status,cancel_at_end,expect", [
    ("active", False, "Renews on"),
    ("active", True, "will not renew"),
    ("past_due", False, "latest payment for this plan failed"),
])
def test_my_subscription_shows_what_stripe_says_about_the_plan(env, status, cancel_at_end, expect):
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", "active")
    _deliver(env, _checkout("evt_1"))
    env["stripe"]["sub_A1"] = _subscription("sub_A1", "cus_A", status, cancel_at_end=cancel_at_end)
    _deliver(env, _sub_event("evt_2", "updated", env["stripe"]["sub_A1"], 2_000))
    page = env["client"].get("/subscription-portal", cookies=_cookie(*OWNER_A)).text
    assert 'id="plan-billing-note"' in page and expect in page
    assert "sub_A1" not in page and "cus_A" not in page  # no Stripe ids or payment details on the page
