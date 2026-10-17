"""30-day free cloud-storage trial (owner request 2026-10-15).

Administrator-granted, storage plan only, Stripe-native trial (never a 100%
coupon), 30 days counted from the start of the storage subscription (not
from the grant), expires on its own after those 30 days, never charges the customer after
the trial without an explicit paid authorization, one trial per customer
unless an administrator approves a re-grant. Stripe is a test double here:
no Stripe API is ever called.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from database_backend import override_target
from test_pricing_ff_commission import OWNER, _cookie, _make_global_admin, _seed, db_path, portal  # noqa: F401

CUSTOMER = "cust-1"
PRICES = {"basic_local": "price_srv_basic", "ai_local": "price_srv_ai", "hybrid": "price_srv_hybrid"}
TRIAL_KEYS = ("subscription_data[trial_end]", "subscription_data[trial_settings][end_behavior][missing_payment_method]",
              "payment_method_collection", "metadata[anyaicam_storage_trial]", "subscription_data[metadata][anyaicam_storage_trial]",
              "trial_end", "trial_period_days", "subscription_data[trial_period_days]")


@pytest.fixture()
def shop(portal, db_path, monkeypatch):
    import main
    import per_camera_billing as billing
    import stripe_state
    client, captured, _sent = portal
    for plan_key, price_id in PRICES.items():
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], price_id)
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8", "price_lic_8")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_RYZEN_STARTER", "price_hw_starter")
    monkeypatch.setattr(main, "require_stripe_price_matches_catalog", lambda *args: None)
    stripe = {"subscriptions": {}, "invoices": {}, "customers": {}, "updates": [], "deletes": [], "created": []}
    checkout_double = main.stripe_api_post  # the portal fixture's Checkout/customers double

    def post(path, fields, idempotency_key=None):
        if path == "/v1/subscriptions":  # the appliance-activation start
            trialing = any(key == "trial_end" for key, _ in fields)
            sub = {"id": f"sub_new_{len(stripe['created']) + 1}", "status": "trialing" if trialing else "active",
                   "customer": dict(fields)["customer"], "cancel_at_period_end": dict(fields).get("cancel_at_period_end") == "true",
                   "metadata": {key[9:-1]: value for key, value in fields if key.startswith("metadata[")}}
            stripe["created"].append(list(fields))
            stripe["subscriptions"][sub["id"]] = sub
            return sub
        if path.startswith("/v1/subscriptions/"):
            stripe["updates"].append((path, list(fields), idempotency_key))
            sub = stripe["subscriptions"][path.rsplit("/", 1)[1]]
            for key, value in fields:
                if key == "cancel_at_period_end":
                    sub[key] = value == "true"
                elif key == "default_payment_method":
                    sub[key] = value
            return sub
        return checkout_double(path, fields, idempotency_key=idempotency_key)

    def get(path):
        base = path.split("?")[0]
        if base.startswith("/v1/subscriptions/"):
            return stripe["subscriptions"][base.rsplit("/", 1)[1]]
        if base == "/v1/subscriptions":
            return {"data": []}
        if base.startswith("/v1/invoices/"):
            invoice_id = base.rsplit("/", 1)[1]
            return stripe["invoices"].get(invoice_id, {"id": invoice_id, "status": "open", "amount_paid": 0})
        if base.startswith("/v1/customers/"):
            return stripe["customers"].get(base.rsplit("/", 1)[1], {})
        if base == "/v1/payment_methods":
            return {"data": [{"id": "pm_saved_card"}]}
        if base.startswith("/v1/coupons/"):
            from conftest import policy_coupon
            return policy_coupon(base.split("/")[3]) or {}
        raise AssertionError(f"unexpected Stripe read {path}")

    def delete(path):
        stripe["deletes"].append(path)
        sub = stripe["subscriptions"][path.rsplit("/", 1)[1]]
        sub["status"] = "canceled"
        return sub

    monkeypatch.setattr(main, "stripe_api_post", post)
    monkeypatch.setattr(main, "stripe_api_get", get)
    monkeypatch.setattr(main, "stripe_api_delete", delete)
    monkeypatch.setattr(stripe_state, "_stripe_reader", lambda: get)
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _subscription: None)
    _seed(db_path)
    _seed(db_path, customer_id="cust-2", email="two@example.test")
    return client, captured, stripe


def _q(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    found = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return found


def _exec(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _grant(client, admin, customer_id=CUSTOMER, **extra):
    return client.post("/api/admin/storage-trials", json={"customer_id": customer_id, "reason": "Evaluating cloud storage", **extra},
                       cookies=admin)


def _checkout(client, plan_key="hybrid", cameras=4, owner=OWNER):
    return client.post("/api/v2/customer/subscription/checkout", json={"plan_key": plan_key, "camera_quantity": cameras},
                       cookies=_cookie(*owner))


def _trial_subscription(stripe, trial_id, sub_id="sub_trial", customer_id=CUSTOMER, **extra):
    sub = {"id": sub_id, "object": "subscription", "status": "trialing", "customer": f"cus_test_{customer_id}",
           "cancel_at_period_end": False, "current_period_end": 1900000000,
           "metadata": {"anyaicam_customer_id": customer_id, "anyaicam_billing_version": "2", "anyaicam_storage_trial": trial_id},
           "items": {"data": [{"id": "si_trial", "quantity": 4, "price": {"id": PRICES["hybrid"]}}]}, **extra}
    stripe["subscriptions"][sub_id] = sub
    return sub


START = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())


def _start(stripe, db_path, trial_id, sub_id="sub_trial", **extra):
    """The trial subscription is created: the verified webhook starts the countdown."""
    now = int(datetime.now(timezone.utc).timestamp())
    extra.setdefault("cancel_at_period_end", True)
    sub = _trial_subscription(stripe, trial_id, sub_id=sub_id, trial_start=now, trial_end=now + 30 * 86400, **extra)
    with override_target(sqlite_path=str(db_path)):
        _sync(_event("customer.subscription.created", sub))
    assert _trial(db_path)["status"] == "active"
    return sub


def _event(event_type, obj, **data):
    return {"id": f"evt_{event_type}_{obj.get('id')}", "type": event_type, "data": {"object": obj, **data}}


def _sync(event):
    import storage_trial
    return storage_trial.sync_from_stripe_event(event)


def _trial(db_path, customer_id=CUSTOMER):
    return _q(db_path, "SELECT * FROM storage_trials WHERE customer_id=? ORDER BY granted_at DESC", (customer_id,))[0]


def _audit(db_path, action):
    return _q(db_path, "SELECT * FROM audit_logs WHERE action=?", (action,))


# ------------------------------------------------------------------ admin control + tracking

def test_only_a_platform_administrator_can_grant_a_trial_and_it_is_recorded(shop, db_path):
    client, _, _ = shop
    assert _grant(client, {}).status_code in (401, 303)  # not signed in
    assert _grant(client, _cookie(*OWNER)).status_code == 403
    assert _grant(client, _cookie("company-admin@example.test", "administrator")).status_code == 403  # no global grant
    assert _q(db_path, "SELECT * FROM storage_trials") == []
    admin = _make_global_admin(db_path)
    response = _grant(client, admin)
    assert response.status_code == 200, response.text
    trial = _trial(db_path)
    # Reserved, not started: no countdown until the storage subscription exists.
    assert trial["customer_id"] == CUSTOMER and trial["status"] == "granted"
    assert trial["starts_at"] == "" and trial["expires_at"] == ""
    assert response.json()["effective_status"] == "granted"
    assert trial["granted_by"] == "admin@example.test" and trial["reason"] == "Evaluating cloud storage"
    logged = _audit(db_path, "storage_trial.grant")
    assert len(logged) == 1 and logged[0]["actor_email"] == "admin@example.test" and logged[0]["entity_id"] == CUSTOMER
    details = json.loads(logged[0]["details_json"])
    assert details["status"] == "granted" and details["reason"] == "Evaluating cloud storage" and details["lapses_at"]
    # The admin list and page are administrator-only too.
    assert client.get("/api/admin/storage-trials", cookies=_cookie(*OWNER)).status_code == 403
    assert client.get("/api/admin/storage-trials", cookies={}).status_code in (401, 303)
    assert client.get("/api/admin/storage-trials", cookies=admin).json()["trials"][0]["effective_status"] == "granted"
    assert client.get("/admin/storage-trials", cookies=_cookie(*OWNER)).status_code in (403, 303)
    page = client.get("/admin/storage-trials", cookies=admin)
    assert page.status_code == 200 and "When storage starts" in page.text


def test_a_reason_is_required_and_only_eligible_customers_get_a_trial(shop, db_path):
    client, _, _ = shop
    admin = _make_global_admin(db_path)
    assert client.post("/api/admin/storage-trials", json={"customer_id": CUSTOMER, "reason": "  "}, cookies=admin).status_code == 400
    assert _grant(client, admin, customer_id="no-such-customer").status_code == 404
    import per_camera_billing as billing
    paid = _trial_subscription(shop[2], "", sub_id="sub_paid_2", customer_id="cust-2", status="active")
    paid["metadata"].pop("anyaicam_storage_trial")
    with override_target(sqlite_path=str(db_path)):
        assert billing._upsert_current(paid)["status"] == "entitlement_updated"
    assert _grant(client, admin, customer_id="cust-2").status_code == 409  # already a paying plan holder
    assert _q(db_path, "SELECT * FROM storage_trials") == []


# ------------------------------------------------------------------ Stripe fields, storage only

def test_storage_checkout_carries_a_stripe_trial_of_30_days_from_the_storage_start(shop, db_path):
    client, captured, _ = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    trial = _trial(db_path)
    response = _checkout(client)
    assert response.status_code == 200, response.text
    fields = captured[-1]
    trial_end = int(fields["subscription_data[trial_end]"])
    assert abs(trial_end - (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()) < 60
    stored = _trial(db_path)  # provisional end kept; still granted until the subscription exists
    assert stored["status"] == "granted" and int(datetime.fromisoformat(stored["expires_at"]).timestamp()) == trial_end
    assert "line_items[0][price_data][unit_amount]" not in fields  # the plan's own price: $0 comes from the trial, not a price
    assert fields["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == "cancel"
    assert fields["payment_method_collection"] == "if_required"
    assert fields["subscription_data[metadata][anyaicam_storage_trial]"] == trial["id"]
    assert not any(key.startswith("discounts[") for key in fields)  # a trial, never a 100% coupon
    assert fields["line_items[0][price]"] == PRICES["hybrid"] and "line_items[1][price]" not in fields


def test_local_only_plans_get_no_trial(shop, db_path):
    client, captured, _ = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    assert _checkout(client, plan_key="ai_local").status_code == 200
    assert not any(key in captured[-1] for key in TRIAL_KEYS)


def test_hardware_appliance_and_license_orders_are_never_part_of_the_trial(shop, db_path):
    client, _, _ = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    import build_orders
    plan_only, _, _ = build_orders._checkout_fields(CUSTOMER, "owner@example.test", {"plan": "hybrid", "cameras": 4})
    assert dict(plan_only)["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == "cancel"
    own_pc, _, _ = build_orders._checkout_fields(CUSTOMER, "owner@example.test", {"plan": "hybrid", "cameras": 4, "own_pc": True})
    own_pc = dict(own_pc)
    assert own_pc["line_items[1][price]"] == "price_lic_8"  # the license is charged in full
    assert not any(key in own_pc for key in TRIAL_KEYS) and not any(key.startswith("discounts[") for key in own_pc)
    appliance, _, _ = build_orders._checkout_fields(CUSTOMER, "owner@example.test",
                                                    {"plan": "hybrid", "cameras": 4, "appliance": "AIC-APPLIANCE-RYZEN-STARTER"})
    appliance = dict(appliance)
    assert appliance["mode"] == "payment" and appliance["allow_promotion_codes"] == "false"
    assert not any(key in appliance for key in TRIAL_KEYS) and not any(key.startswith("discounts[") for key in appliance)


def test_no_other_checkout_reads_the_trial():
    """Analytics, Talk Down, hardware and license checkouts never consult it."""
    import inspect
    import main
    import hardware_orders
    assert "storage_trial" not in inspect.getsource(main.create_analytics_addon_checkout)
    assert "storage_trial" not in inspect.getsource(hardware_orders)


def _deferred(db_path, customer_id):
    _exec(db_path, "INSERT INTO deferred_storage_plans(customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_payment_method_id,"
                   "checkout_session_id,state,attempt_round,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,0,?,?)",
          (customer_id, "hybrid", 4, f"cus_test_{customer_id}", "pm_appliance_card", f"cs_{customer_id}", "awaiting_activation",
           "2026-10-15", "2026-10-15"))


def test_appliance_storage_start_uses_the_trial_with_no_card_attached(shop, db_path, monkeypatch):
    client, _, stripe = shop
    import build_orders
    monkeypatch.setattr(build_orders, "_appliance_activated", lambda _c: True)
    monkeypatch.setattr(build_orders, "_hardware_still_paid", lambda _p: True)
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    _deferred(db_path, CUSTOMER)
    _deferred(db_path, "cust-2")
    assert build_orders.start_storage_plan(CUSTOMER)["status"] == "started"
    trial_fields = dict(stripe["created"][-1])
    assert trial_fields["trial_settings[end_behavior][missing_payment_method]"] == "cancel"
    assert trial_fields["cancel_at_period_end"] == "true"  # ends at the trial end unless the customer chooses paid
    assert int(trial_fields["trial_end"]) == int(datetime.fromisoformat(_trial(db_path)["expires_at"]).timestamp())
    assert abs(int(trial_fields["trial_end"]) - (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()) < 60
    assert "default_payment_method" not in trial_fields
    started = _trial(db_path)
    assert started["stripe_subscription_id"] == "sub_new_1" and started["status"] == "active"  # countdown started now
    assert abs((datetime.fromisoformat(started["starts_at"]) - datetime.now(timezone.utc)).total_seconds()) < 60
    assert json.loads(_audit(db_path, "storage_trial.start")[0]["details_json"])["source"] == "server"
    # Without a trial the start is exactly as before: the saved card, no trial.
    assert build_orders.start_storage_plan("cust-2")["status"] == "started"
    normal = dict(stripe["created"][-1])
    assert normal["default_payment_method"] == "pm_appliance_card"
    assert not any(key in normal for key in TRIAL_KEYS) and "cancel_at_period_end" not in normal


# ------------------------------------------------------------------ customer protection

def test_a_checkout_trial_subscription_is_set_to_end_at_the_trial_end(shop, db_path):
    client, _, stripe = shop
    import main
    assert "storage_trials" in [name for name, _ in main._stripe_webhook_steps()]
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    trial = _trial(db_path)
    _trial_subscription(stripe, trial["id"], trial_start=START, trial_end=START + 30 * 86400)
    session = {"id": "cs_trial", "subscription": "sub_trial", "metadata": {"anyaicam_storage_trial": trial["id"]}}
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("checkout.session.completed", session))["status"] == "guarded"
        path, fields, key = stripe["updates"][-1]
        assert path == "/v1/subscriptions/sub_trial" and fields == [("cancel_at_period_end", "true")]
        assert key == "anyaicam-storage-trial-guard-sub_trial"
        assert _sync(_event("customer.subscription.created", stripe["subscriptions"]["sub_trial"]))["status"] == "trialing"
    assert len(stripe["updates"]) == 1  # already guarded: nothing more
    stored = _trial(db_path)
    assert stored["stripe_subscription_id"] == "sub_trial" and stored["status"] == "active"
    assert stored["starts_at"] == "2026-10-20T00:00:00+00:00" and stored["expires_at"] == "2026-11-19T00:00:00+00:00"
    assert len(_audit(db_path, "storage_trial.start")) == 1


def test_unrelated_stripe_events_are_ignored_without_a_stripe_call(shop, db_path):
    _, _, stripe = shop
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("customer.subscription.updated", {"id": "sub_other", "metadata": {}}))["status"] == "ignored"
        assert _sync(_event("invoice.paid", {"id": "in_1", "subscription": "sub_other"}))["status"] == "ignored"
    assert stripe["updates"] == [] and stripe["deletes"] == []


# ------------------------------------------------------------------ expiry

def test_the_trial_expires_on_its_own_after_30_days(shop, db_path, monkeypatch):
    client, _, stripe = shop
    import storage_trial
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    _start(stripe, db_path, _trial(db_path)["id"])
    trial = _trial(db_path)
    expires = datetime.fromisoformat(trial["expires_at"])
    real_now = storage_trial._now
    monkeypatch.setattr(storage_trial, "_now", lambda: expires - timedelta(seconds=1))
    with override_target(sqlite_path=str(db_path)):
        assert storage_trial.active_trial(CUSTOMER) is not None
    monkeypatch.setattr(storage_trial, "_now", lambda: expires + timedelta(seconds=1))
    with override_target(sqlite_path=str(db_path)):
        assert storage_trial.active_trial(CUSTOMER) is None
        assert storage_trial.customer_view(CUSTOMER)["status"] == "expired"
        assert storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="hybrid") == []
    assert client.get("/api/v2/customer/storage-trial", cookies=_cookie(*OWNER)).json()["storage_trial"]["status"] == "expired"
    # Stripe ends the subscription at the trial end (cancel_at_period_end): recorded as expired.
    monkeypatch.setattr(storage_trial, "_now", real_now)
    stripe["subscriptions"]["sub_trial"].update(status="canceled", ended_at=int(expires.timestamp()))
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("customer.subscription.deleted", stripe["subscriptions"]["sub_trial"]))["status"] == "expired"
    assert _trial(db_path)["status"] == "expired" and _audit(db_path, "storage_trial.expired")
    assert stripe["updates"] == []  # nothing charged, nothing changed on Stripe


def test_a_storage_start_always_gets_a_fresh_30_days_and_an_unused_grant_lapses(shop, db_path, monkeypatch):
    """Stripe Checkout needs trial_end >= 48 hours ahead: always true now,
    since every storage start gets now + 30 days. A grant unused for 180
    days lapses (derived when read) and then offers nothing."""
    client, _, _ = shop
    import storage_trial
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    granted = datetime.fromisoformat(_trial(db_path)["granted_at"])
    late = granted + timedelta(days=179)
    monkeypatch.setattr(storage_trial, "_now", lambda: late)
    with override_target(sqlite_path=str(db_path)):
        fields = dict(storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="hybrid"))
    assert int(fields["subscription_data[trial_end]"]) == int((late + timedelta(days=30)).timestamp())
    monkeypatch.setattr(storage_trial, "_now", lambda: granted + timedelta(days=180, seconds=1))
    with override_target(sqlite_path=str(db_path)):
        assert storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="hybrid") == []
        assert storage_trial.subscription_trial_fields(CUSTOMER, checkout=False, plan_key="hybrid") == []
        assert storage_trial.customer_view(CUSTOMER)["status"] == "lapsed"


# ------------------------------------------------------------------ cancellation

def test_an_administrator_cancels_a_trial_and_its_free_subscription_ends_without_charge(shop, db_path):
    client, _, stripe = shop
    admin = _make_global_admin(db_path)
    assert _grant(client, admin).status_code == 200
    trial = _trial(db_path)
    _start(stripe, db_path, trial["id"])
    url = f"/api/admin/storage-trials/{trial['id']}/cancel"
    assert client.post(url, json={"reason": "x"}, cookies=_cookie(*OWNER)).status_code == 403
    assert client.post(url, json={}, cookies=admin).status_code == 400
    response = client.post(url, json={"reason": "Customer asked to stop"}, cookies=admin)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert stripe["deletes"] == ["/v1/subscriptions/sub_trial"] and stripe["updates"] == []
    stored = _trial(db_path)
    assert stored["cancelled_by"] == "admin@example.test" and stored["cancel_reason"] == "Customer asked to stop"
    assert json.loads(_audit(db_path, "storage_trial.cancel")[0]["details_json"])["previous_status"] == "active"
    assert client.post(url, json={"reason": "again"}, cookies=admin).status_code == 409
    with override_target(sqlite_path=str(db_path)):  # Stripe's deletion event leaves the record cancelled
        _sync(_event("customer.subscription.deleted", stripe["subscriptions"]["sub_trial"]))
    assert _trial(db_path)["status"] == "cancelled"


def test_cancelling_a_granted_trial_needs_no_stripe_call(shop, db_path):
    client, captured, stripe = shop
    admin = _make_global_admin(db_path)
    assert _grant(client, admin).status_code == 200
    trial = _trial(db_path)
    response = client.post(f"/api/admin/storage-trials/{trial['id']}/cancel", json={"reason": "Granted by mistake"}, cookies=admin)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert stripe["deletes"] == [] and stripe["updates"] == []
    assert json.loads(_audit(db_path, "storage_trial.cancel")[0]["details_json"])["previous_status"] == "granted"
    assert _checkout(client).status_code == 200
    assert not any(key in captured[-1] for key in TRIAL_KEYS)  # a cancelled trial applies to nothing


def test_a_checkout_left_open_when_a_granted_trial_is_cancelled_ends_its_free_subscription(shop, db_path):
    client, captured, stripe = shop
    admin = _make_global_admin(db_path)
    assert _grant(client, admin).status_code == 200
    trial = _trial(db_path)
    assert _checkout(client).status_code == 200 and captured[-1]["subscription_data[trial_end]"]
    assert client.post(f"/api/admin/storage-trials/{trial['id']}/cancel", json={"reason": "Revoked"}, cookies=admin).status_code == 200
    sub = _trial_subscription(stripe, trial["id"])  # the customer completes the open Checkout afterwards
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("customer.subscription.created", sub))["status"] == "cancelled_on_stripe"
    assert stripe["deletes"] == ["/v1/subscriptions/sub_trial"] and _trial(db_path)["status"] == "cancelled"


# ------------------------------------------------------------------ conversion to paid storage

def _trialing_plan(stripe, db_path, trial_id):
    import per_camera_billing as billing
    sub = _start(stripe, db_path, trial_id)
    with override_target(sqlite_path=str(db_path)):
        assert billing._upsert_current(sub)["status"] == "entitlement_updated"
    return sub


def test_conversion_to_paid_storage_needs_the_customers_explicit_authorization(shop, db_path):
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    trial = _trial(db_path)
    sub = _trialing_plan(stripe, db_path, trial["id"])
    with override_target(sqlite_path=str(db_path)):
        import per_camera_billing as billing
        assert billing.entitlement_for_customer(CUSTOMER)["status"] == "active"  # free storage during the trial
    url = "/api/v2/customer/storage-trial/continue-paid"
    assert client.post(url, json={"authorize_paid_storage": True},
                       cookies=_cookie("viewer@example.test", "customer_viewer", CUSTOMER)).status_code == 403
    assert client.post(url, json={}, cookies=_cookie(*OWNER)).status_code == 400
    no_card = client.post(url, json={"authorize_paid_storage": True}, cookies=_cookie(*OWNER))
    assert no_card.status_code == 409 and "payment method" in no_card.json()["detail"]
    assert stripe["updates"] == [] and _trial(db_path)["paid_authorized_at"] is None
    stripe["customers"][sub["customer"]] = {"id": sub["customer"], "invoice_settings": {"default_payment_method": "pm_card_added"}}
    response = client.post(url, json={"authorize_paid_storage": True}, cookies=_cookie(*OWNER))
    assert response.status_code == 200, response.text
    path, fields, _key = stripe["updates"][-1]
    assert path == "/v1/subscriptions/sub_trial"
    assert dict(fields) == {"cancel_at_period_end": "false", "default_payment_method": "pm_card_added"}
    stored = _trial(db_path)
    assert stored["paid_authorized_at"] and stored["paid_authorized_by"] == "owner@example.test"
    assert _audit(db_path, "storage_trial.paid_authorized")
    # An authorized trial is not re-guarded by later trial events.
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("customer.subscription.updated", stripe["subscriptions"]["sub_trial"]))["status"] == "trialing"
    # The trial ends, Stripe bills the first paid month: converted.
    sub.update(status="active", latest_invoice="in_paid_1")
    stripe["invoices"]["in_paid_1"] = {"id": "in_paid_1", "status": "paid", "amount_paid": 9996}
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("customer.subscription.updated", sub))["status"] == "converted"
    stored = _trial(db_path)
    assert stored["status"] == "converted" and stored["converted_at"] and _audit(db_path, "storage_trial.converted")


def test_an_unpaid_post_trial_invoice_is_not_a_conversion(shop, db_path):
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    sub = _trialing_plan(stripe, db_path, _trial(db_path)["id"])
    sub.update(status="active", latest_invoice="in_open_1")
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("customer.subscription.updated", sub))["status"] == "unchanged"
    assert _trial(db_path)["status"] == "active"


def _billed_after_trial(stripe, sub, invoice_id="in_paid_1", **extra):
    sub.update(status="active", latest_invoice=invoice_id, **extra)
    stripe["invoices"][invoice_id] = {"id": invoice_id, "status": "paid", "amount_paid": 9996}
    return sub


def test_a_paid_subscription_nobody_authorized_is_never_recorded_as_a_conversion(shop, db_path):
    """Codex 2026-10-17: Stripe reports the trial subscription active with a paid invoice, but the owner never chose
    paid storage (the guard failed or was undone, or a person changed it in Stripe). Not a conversion: flagged once
    for an administrator, and nothing is changed in Stripe automatically."""
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    sub = _trialing_plan(stripe, db_path, _trial(db_path)["id"])
    _billed_after_trial(stripe, sub, cancel_at_period_end=False)
    writes = (list(stripe["updates"]), list(stripe["deletes"]))
    with override_target(sqlite_path=str(db_path)):
        for event_type in ("customer.subscription.updated", "invoice.paid", "customer.subscription.updated"):
            obj = sub if event_type.startswith("customer.") else {"id": "in_paid_1", "subscription": "sub_trial"}
            assert _sync(_event(event_type, obj))["status"] == "charged_without_authorization"
    stored = _trial(db_path)
    assert stored["status"] == "active" and stored["converted_at"] is None and stored["paid_authorized_at"] is None
    assert _audit(db_path, "storage_trial.converted") == []
    flagged = _audit(db_path, "storage_trial.charged_without_authorization")
    assert len(flagged) == 1 and '"invoice_id": "in_paid_1"' in flagged[0]["details_json"]
    assert (stripe["updates"], stripe["deletes"]) == writes  # no automatic refund/cancel: a person decides
    # A later month's charge is flagged on its own.
    _billed_after_trial(stripe, sub, invoice_id="in_paid_2", cancel_at_period_end=False)
    with override_target(sqlite_path=str(db_path)):
        assert _sync(_event("customer.subscription.updated", sub))["status"] == "charged_without_authorization"
    assert len(_audit(db_path, "storage_trial.charged_without_authorization")) == 2


def test_an_un_cancel_in_stripe_that_arrives_after_the_trial_ended_is_still_the_owners_choice(shop, db_path):
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    sub = _trialing_plan(stripe, db_path, _trial(db_path)["id"])
    _billed_after_trial(stripe, sub, cancel_at_period_end=False)
    with override_target(sqlite_path=str(db_path)):
        result = _sync(_event("customer.subscription.updated", sub, previous_attributes={"cancel_at_period_end": True}))
    assert result["status"] == "converted"
    stored = _trial(db_path)
    assert stored["status"] == "converted" and stored["paid_authorized_by"] == "stripe"
    assert _audit(db_path, "storage_trial.paid_authorized") and _audit(db_path, "storage_trial.charged_without_authorization") == []


def test_a_subscription_still_set_to_end_is_not_an_un_cancel(shop, db_path):
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    sub = _trialing_plan(stripe, db_path, _trial(db_path)["id"])
    _billed_after_trial(stripe, sub, cancel_at_period_end=True)
    with override_target(sqlite_path=str(db_path)):
        result = _sync(_event("customer.subscription.updated", sub, previous_attributes={"cancel_at_period_end": True}))
    assert result["status"] == "charged_without_authorization" and _trial(db_path)["paid_authorized_at"] is None


def test_an_un_cancel_by_a_person_in_stripe_counts_as_authorization_but_nothing_else_does(shop, db_path):
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    trial = _trial(db_path)
    sub = _trialing_plan(stripe, db_path, trial["id"])
    sub["cancel_at_period_end"] = False
    with override_target(sqlite_path=str(db_path)):  # some other update: guarded again
        assert _sync(_event("customer.subscription.updated", sub))["status"] == "guarded"
        sub["cancel_at_period_end"] = False
        result = _sync(_event("customer.subscription.updated", sub, previous_attributes={"cancel_at_period_end": True}))
    assert result["status"] == "paid_authorized_in_stripe"
    assert _trial(db_path)["paid_authorized_by"] == "stripe"


def test_plan_changes_wait_until_the_trial_is_over(shop, db_path):
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    _trialing_plan(stripe, db_path, _trial(db_path)["id"])
    response = client.post("/api/v2/customer/subscription/change", json={"plan_key": "hybrid", "camera_quantity": 20},
                           cookies=_cookie(*OWNER))
    assert response.status_code == 409 and "trial" in response.json()["detail"]
    assert stripe["updates"] == []


def test_the_customer_sees_the_free_trial_on_my_subscription(shop, db_path):
    client, _, stripe = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    _trialing_plan(stripe, db_path, _trial(db_path)["id"])
    import storage_trial
    with override_target(sqlite_path=str(db_path)):
        html = storage_trial.customer_note_html(CUSTOMER, True)
    assert "Free cloud storage trial until" in html and "storage-trial-continue" in html
    assert "will not be charged" in html
    view = client.get("/api/v2/customer/storage-trial", cookies=_cookie(*OWNER)).json()["storage_trial"]
    assert view["status"] == "active" and view["subscription_started"] and not view["paid_after_trial"]


# ------------------------------------------------------------------ one trial per customer

def test_a_second_trial_needs_an_administrator_approved_re_grant(shop, db_path):
    client, _, _ = shop
    admin = _make_global_admin(db_path)
    assert _grant(client, admin).status_code == 200
    assert _grant(client, admin).status_code == 409  # already granted
    assert _grant(client, admin, allow_repeat=True, repeat_reason="x").status_code == 409  # never two at once
    _start(shop[2], db_path, _trial(db_path)["id"])
    assert _grant(client, admin, allow_repeat=True, repeat_reason="x").status_code == 409  # running
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    _exec(db_path, "UPDATE storage_trials SET expires_at=?", (past,))
    refused = _grant(client, admin)
    assert refused.status_code == 409 and "re-grant" in refused.json()["detail"]
    assert _grant(client, admin, allow_repeat=True).status_code == 400  # approval needs its own reason
    regrant = _grant(client, admin, allow_repeat=True, repeat_reason="Owner approved a second evaluation")
    assert regrant.status_code == 200, regrant.text
    trials = _q(db_path, "SELECT * FROM storage_trials WHERE customer_id=? ORDER BY granted_at", (CUSTOMER,))
    assert [t["status"] for t in trials] == ["expired", "granted"]
    assert trials[1]["repeat_of"] == trials[0]["id"] and trials[1]["repeat_approved_by"] == "admin@example.test"
    assert trials[1]["repeat_reason"] == "Owner approved a second evaluation"
    assert _audit(db_path, "storage_trial.regrant")


def test_the_database_allows_only_one_open_trial_per_customer(shop, db_path):
    client, _, _ = shop
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    conn = sqlite3.connect(db_path)
    try:
        for status in ("granted", "active"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO storage_trials(id,customer_id,status,starts_at,expires_at,granted_by,granted_at,reason,updated_at) "
                             "VALUES('dup','cust-1',?,'a','b','c','d','e','f')", (status,))
    finally:
        conn.close()


# ------------------------------------------------------------------ existing billing unchanged

def test_normal_checkout_is_unchanged_without_a_trial(shop, db_path):
    client, captured, _ = shop
    assert _checkout(client, owner=("two@example.test", "customer_owner", "cust-2")).status_code == 200
    fields = captured[-1]
    assert not any(key in fields for key in TRIAL_KEYS)
    assert fields["allow_promotion_codes"] == "true" and fields["metadata[anyaicam_friends_family]"] == "none"


def test_friends_family_is_unchanged_and_applies_after_the_trial(shop, db_path):
    client, captured, _ = shop
    _exec(db_path, "INSERT INTO friends_family_requests(id,customer_id,status,requested_at) VALUES('ff-1','cust-1','approved','2026-10-01')")
    _exec(db_path, "INSERT INTO friends_family_requests(id,customer_id,status,requested_at) VALUES('ff-2','cust-2','approved','2026-10-01')")
    assert _checkout(client, owner=("two@example.test", "customer_owner", "cust-2")).status_code == 200
    without_trial = captured[-1]
    assert without_trial["discounts[0][coupon]"] == "coupon_ff_base" and not any(key in without_trial for key in TRIAL_KEYS)
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    assert _checkout(client).status_code == 200
    with_trial = captured[-1]
    assert with_trial["discounts[0][coupon]"] == "coupon_ff_base"  # the same 50% F&F coupon, for after the trial
    assert with_trial["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == "cancel"
    assert "allow_promotion_codes" not in with_trial


def test_the_single_hook_contract(shop, db_path):
    """subscription_trial_fields is the one hook every storage start uses."""
    client, _, _ = shop
    import storage_trial
    with override_target(sqlite_path=str(db_path)):
        for checkout in (True, False):  # no trial: nothing at all
            assert storage_trial.subscription_trial_fields(CUSTOMER, checkout=checkout, plan_key="hybrid") == []
    assert _grant(client, _make_global_admin(db_path)).status_code == 200
    with override_target(sqlite_path=str(db_path)):
        session = dict(storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="hybrid"))
        direct = dict(storage_trial.subscription_trial_fields(CUSTOMER, checkout=False, plan_key="hybrid"))
        assert storage_trial.subscription_trial_fields(CUSTOMER, checkout=True, plan_key="basic_local") == []
        assert storage_trial.subscription_trial_fields("cust-2", checkout=True, plan_key="hybrid") == []
    assert set(session) == {"subscription_data[trial_end]", "subscription_data[trial_settings][end_behavior][missing_payment_method]",
                            "payment_method_collection", "metadata[anyaicam_storage_trial]",
                            "subscription_data[metadata][anyaicam_storage_trial]"}
    assert set(direct) == {"trial_end", "trial_settings[end_behavior][missing_payment_method]", "cancel_at_period_end",
                           "metadata[anyaicam_storage_trial]"}
    assert session["subscription_data[trial_end]"] == direct["trial_end"]
