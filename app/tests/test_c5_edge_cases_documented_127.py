"""C5 edge cases, DOCUMENTED (owner, 2026-10-21: "Document and test the remaining C5 edge cases, especially active paid
plans with reversed licenses, add-on purchases, and the legacy checkout endpoint. Do not silently expand billing
behavior.").

TESTS ONLY. Nothing here changes billing: every test pins what the integrated candidate (C5 c1d1bb7b + customer fixes,
dedc2a35) does TODAY, so any later change to these paths fails a test and has to be made -- and its decision recorded --
on purpose.

Each case says whether today's behaviour is INTENDED (the C5 design: per_camera_billing.own_pc_licence_reversed,
account_activated, vms_license_capacity) or a KNOWN GAP (owner decision). Known gaps are asserted as they behave now,
not as xfail: a pinned value fails on ANY drift (also a partial or accidental one), while xfail(strict) would only notice
the one outcome somebody guessed as the fix. When the owner decides a gap, the test that pins it is updated together
with the code.

The account under test ("reversed + running plan"): an own-PC customer whose one-time VMS licence payment is REFUNDED or
DISPUTED while a per-camera plan is still RUNNING. Two real ways to get there, both through the webhook route:
* later-paid-month: order 1 (licence + AI Local x5) fully refunded, then month 2 of the same subscription is paid
  (invoice.paid) -> the plan is active again, the licence stays suspended 'refunded';
* late-checkout: order 1 paid, its plan ended, a My subscription plan checkout opened (allowed then), the licence payment
  disputed, then that checkout paid -> a new active plan (AI Local x5), the licence suspended 'disputed'.
C5 keeps such an account ACTIVATED (its running plan stays manageable) but its plan carries NO software licence:
download refused, licence / usable / signed capacity 0.

Cases (routes in the candidate):
1. Add-ons -- POST /api/customer/analytics/checkout (main.py:114407) + the webhook route:
   KNOWN GAP: Advanced Analytics is sold, paid and granted (and assignable to an existing camera row) although the
   account can run 0 cameras. Face Access reaches checkout once a door is set up. Talk Down is refused on AI Local as
   before (included with the plan); Voice Call is not sold on its own (part of the Talk Down add-on).
2. Legacy / API-only one-time licence checkout -- POST /api/customer/vms-license/checkout (main.py:114611):
   KNOWN GAP: it sells a NEW licence to the reversed account (it counts only ACTIVE licence rows); the paid webhook
   (customer_entitlements._sync_checkout_completed -> upsert_entitlement, one row per customer+product) OVERWRITES the
   refunded/disputed row in place (status, reason, Checkout Session and PaymentIntent replaced), writes NO R7 archive
   (audit_logs build_order.superseded_after_refund), and the running plan's capacity comes back. The reversal itself
   stays in billing_reversals; a replay of the old reversal does not suspend the new licence. Before activation (plan
   ended) the route answers 409 activate_first (INTENDED, C5 gate).
   Legacy software-tier checkout POST /api/payments/checkout (main.py:113591): 503 while its Price is unset; with a
   Price configured it opens a Checkout, whose payment grants no camera capacity (no tier maps its Price).
3. Capacity paths -- POST /api/customer/camera-slots/checkout was removed (404). Build Your System
   (POST /api/v2/customer/build-order/checkout, build_orders.py:719) refuses a second order while the plan runs (409);
   /order-summary redirects away; a second plan is refused (409). INTENDED: no plan-only capacity path.
4. Plan change -- POST /api/v2/customer/subscription/change (per_camera_billing.py:1612): KNOWN GAP: no activation or
   licence check; upgrades are charged at once (Stripe proration) and scheduled downgrades accepted, while capacity stays
   0. Cancel (per_camera_billing.py:1619) works: INTENDED (the running plan stays manageable).
5. Appliance boundary (owner exemption: a PAID AnyAiCam appliance includes the licence): a refunded or disputed appliance
   order no longer exempts the account, so a refunded own-PC licence row again blocks plan capacity (INTENDED); a won
   appliance dispute restores it (INTENDED). KNOWN GAP: an appliance order refunded/disputed WITHOUT any own-PC licence
   row falls back to the billing-v2 rule "an active plan carries the licence" -- full capacity and download.
6. Signed snapshot (feature_entitlements.appliance_snapshot): camera_capacity {0, plan, 0}; talk_down True from the AI
   Local plan, voice_call False. KNOWN GAP (cosmetic): Talk Down is signed as entitled while no camera may run.

Real routes and real billing code; Stripe is the fake of test_refund_before_record_race_127.py.
"""
import copy
import re

import pytest

from database_backend import override_target
from test_direct_onboarding import _login, _one, _signed_in  # noqa: F401
from test_hybrid_build_system_flow import (PRICES, _checkout, _checkouts, _paid_checkout_event,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)
from test_refund_before_record_race_127 import (_access, _configure, _dispute, _ids, _invoice_paid, _later_invoice,  # noqa: F401
                                                _post, _refund, _sub_event, stripe)
from test_r7_repurchase_after_refund_127 import (_capacity, _end_order1, _licence_row, _ok, _order1_paid, _plan_row,
                                                 _r7_ready, _subscription, _superseded)
from test_c5_refunded_licence_no_plan_capacity_127 import (_account, _entitled, _exec, _open_plan_checkout,
                                                           _pay_plan_checkout, _signed)

pytestmark = [pytest.mark.real_activation]

ADDON_CHECKOUT = "/api/customer/analytics/checkout"
LICENCE_CHECKOUT = "/api/customer/vms-license/checkout"
LEGACY_CHECKOUT = "/api/payments/checkout"
CHANGE = "/api/v2/customer/subscription/change"
CANCEL = "/api/v2/customer/subscription/cancel"
REVERSED_RUNNING = (True, False, 0, 0, 0)  # activated, no download, licence / usable / signed capacity 0


# ------------------------------------------------------------------ the account under test

def _later_paid_month(shop, db_path, stripe, email):
    """Order 1 fully refunded, then month 2 of its subscription paid (webhook route): plan active, licence 'refunded'."""
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, email)
    _ok(shop[0], _refund(ids, "evt_c5e_refund"))
    _later_invoice(stripe, ids, "in_race_m2")
    _subscription(stripe, ids, customer_id, stripe_customer, quantity=5, latest_invoice="in_race_m2")
    _ok(shop[0], _invoice_paid(ids, "in_race_m2", "pi_in_race_m2"))
    return customer_id, ids, stripe_customer, "refunded"


def _late_checkout(shop, db_path, stripe, email):
    """Order 1 paid and ended; a plan checkout opened; the licence payment disputed; then the checkout paid."""
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, email)
    _end_order1(shop, db_path, stripe, customer_id, ids, events, stripe_customer)
    assert _open_plan_checkout(shop).status_code == 200
    _ok(shop[0], _dispute(ids, "created", event_id="evt_c5e_dp"))
    ids3, _ = _pay_plan_checkout(shop, db_path, stripe, customer_id, stripe_customer, 3)
    return customer_id, ids3, stripe_customer, "disputed"


BUILDERS = pytest.mark.parametrize("builder", [_later_paid_month, _late_checkout], ids=["later-paid-month", "late-checkout"])


def _reversed_running(shop, db_path, stripe, builder, email):
    customer_id, ids, stripe_customer, reason = builder(shop, db_path, stripe, email)
    plan, licence = _plan_row(db_path, customer_id), _licence_row(db_path, customer_id)
    assert (plan["status"], plan["camera_quantity"], plan["plan_key"]) == ("active", 5, "ai_local")
    assert (licence["status"], licence["suspended_reason"]) == ("suspended", reason)
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING
    return customer_id, ids, stripe_customer, reason


def _metadata(fields):
    return {key[len("metadata["):-1]: value for key, value in fields.items() if key.startswith("metadata[")}


def _addon_paid_event(shop, stripe, customer_id, stripe_customer, sub_id):
    """checkout.session.completed for the latest add-on Checkout, with Stripe's subscription for it (first invoice paid)."""
    fields = _checkouts(shop[1])[-1]
    price = fields["line_items[0][price]"]
    stripe["now"]["subscriptions"][sub_id] = {
        "id": sub_id, "object": "subscription", "status": "active", "customer": stripe_customer,
        "latest_invoice": f"in_first_{sub_id}", "current_period_end": 1800000000,
        "metadata": {"anyaicam_customer_id": customer_id, "anyaicam_product_class": "addon",
                     "anyaicam_addon_key": fields["metadata[anyaicam_addon_key]"], "anyaicam_stripe_price_id": price},
        "items": {"data": [{"id": f"si_{sub_id}", "quantity": int(fields["line_items[0][quantity]"]), "price": {"id": price}}]}}
    return {"id": f"evt_{sub_id}", "type": "checkout.session.completed", "livemode": False, "data": {"object": {
        "id": f"cs_{sub_id}", "mode": "subscription", "payment_status": "paid", "status": "complete",
        "customer": stripe_customer, "subscription": sub_id, "metadata": _metadata(fields)}}}


def _licence_paid_event(shop, stripe_customer, session_id="cs_c5e_licence", intent="pi_c5e_licence"):
    """checkout.session.completed for the latest one-time licence Checkout (mode=payment)."""
    fields = [item for item in shop[1] if item.get("mode") == "payment"][-1]
    return {"id": f"evt_{session_id}", "type": "checkout.session.completed", "livemode": False, "data": {"object": {
        "id": session_id, "mode": "payment", "payment_status": "paid", "status": "complete", "customer": stripe_customer,
        "payment_intent": intent, "amount_total": 49999, "metadata": _metadata(fields)}}}


def _camera(db_path, customer_id, camera_id="cam-c5e", door=False):
    """A camera row of the account (e.g. one set up before the reversal)."""
    _exec(db_path, "INSERT INTO cameras(id,customer_id,site_id,name,camera_number,created_at,door_access_enabled) "
                   "SELECT ?,?,id,'Front door',1,'2026-10-01',? FROM sites WHERE customer_id=? LIMIT 1",
          (camera_id, customer_id, 1 if door else 0, customer_id))


def _active_analytics(db_path, customer_id):
    import analytics_entitlements as ae
    with override_target(sqlite_path=str(db_path)):
        return ae.get_active_analytics_for_customer(customer_id), ae.active_addon_keys(customer_id)


def _stripe_writes(monkeypatch, stripe):
    """Stripe's answer to a plan change, a scheduled change and a cancel: the subscription becomes what was asked."""
    import main
    inner, posts = main.stripe_api_post, []

    def fake(path, fields, idempotency_key=None):
        values = dict(fields)
        posts.append((path, values))
        if path.startswith("/v1/subscriptions/"):
            sub = stripe["now"]["subscriptions"][path.split("/")[3]]
            if "items[0][quantity]" in values:
                sub["items"]["data"][0].update(quantity=int(values["items[0][quantity]"]), price={"id": values["items[0][price]"]})
            if values.get("cancel_at_period_end") == "true":
                sub["cancel_at_period_end"] = True
            return copy.deepcopy(sub)
        if path.startswith("/v1/subscription_schedules"):
            return {"id": "sub_sched_c5e"}
        return inner(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", fake)
    return posts


# ------------------------------------------------------------------ 1. add-ons on a reversed + running-plan account

@BUILDERS
def test_1_advanced_analytics_add_on_is_sold_and_granted_with_zero_camera_capacity(shop, db_path, stripe, builder):
    """KNOWN GAP (owner decision): require_activated passes (the running plan keeps the account activated) and no
    add-on check looks at the licence, so My subscription offers Advanced Analytics, the checkout opens at the server
    price, the paid webhook grants its analytics and they can be assigned to an existing camera row -- while the signed
    capacity stays 0, so the appliance may run no camera to use them on. Billed monthly per camera (quantity 2 here)."""
    client = shop[0]
    customer_id, ids, stripe_customer, _ = _reversed_running(shop, db_path, stripe, builder, "c5e-addon@example.test")
    page = client.get("/subscription-portal")
    assert page.status_code == 200 and "advanced_analytics" in re.findall(r'data-addon-key="([^"]+)"', page.text)
    before = len(_checkouts(shop[1]))
    response = client.post(ADDON_CHECKOUT, json={"addon_key": "advanced_analytics", "quantity": 2})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "complete" and len(_checkouts(shop[1])) == before + 1
    fields = _checkouts(shop[1])[-1]
    assert (fields["mode"], fields["line_items[0][price]"], fields["line_items[0][quantity]"]) == ("subscription", "price_adv", "2")
    assert not any("vms" in str(value).lower() for value in fields.values())  # no licence line, no licence wording
    _ok(client, _addon_paid_event(shop, stripe, customer_id, stripe_customer, "sub_c5e_addon"))
    assert _active_analytics(db_path, customer_id) == (["lpr", "people_counting", "ppe"], ["advanced_analytics"])
    _camera(db_path, customer_id)
    import customer_analytics_panel as cap
    from partner_db import connection
    with override_target(sqlite_path=str(db_path)), connection() as db:
        cap.assign_entitlement(db, "cam-c5e", "lpr", now="2026-10-21T00:00:00")  # accepted (no capacity check)
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING  # the add-on restores nothing
    assert _signed(db_path, customer_id) == {"camera_slot_quantity": 0, "plan_camera_slots": 5, "vms_license_capacity": 0}


@BUILDERS
def test_1_face_access_reaches_checkout_once_a_door_is_set_up(shop, db_path, stripe, builder, monkeypatch):
    """KNOWN GAP (same as Advanced Analytics): with a test-mode Face Access Price and a door camera, the per-door
    checkout opens for the reversed account. Without a door it is refused for that reason only (400, no Stripe call)."""
    client = shop[0]
    customer_id, *_ = _reversed_running(shop, db_path, stripe, builder, "c5e-face@example.test")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_FACE_ACCESS_SMALL", "price_face_small")
    before = len(_checkouts(shop[1]))
    refused = client.post(ADDON_CHECKOUT, json={"addon_key": "face_access_small"})
    assert refused.status_code == 400 and "billed per door" in refused.json()["detail"]
    assert len(_checkouts(shop[1])) == before
    _camera(db_path, customer_id, door=True)
    response = client.post(ADDON_CHECKOUT, json={"addon_key": "face_access_small"})
    assert response.status_code == 200, response.text
    fields = _checkouts(shop[1])[-1]
    assert (fields["line_items[0][price]"], fields["line_items[0][quantity]"]) == ("price_face_small", "1")
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING


@BUILDERS
def test_1_talk_down_is_refused_on_ai_local_and_voice_call_is_not_sold_alone(shop, db_path, stripe, builder):
    """INTENDED (unchanged rule, not C5): Talk Down is included with AI Local / Hybrid, so the add-on is refused (409,
    no Stripe call). AAC Voice Call has no SKU of its own (it is part of the Talk Down add-on): 400 unknown add-on."""
    client = shop[0]
    customer_id, *_ = _reversed_running(shop, db_path, stripe, builder, "c5e-talk@example.test")
    before = len(_checkouts(shop[1]))
    talk_down = client.post(ADDON_CHECKOUT, json={"addon_key": "talk_down"})
    assert talk_down.status_code == 409 and talk_down.json()["detail"] == "Talk Down is included with your plan on supported cameras."
    voice_call = client.post(ADDON_CHECKOUT, json={"addon_key": "voice_call"})
    assert voice_call.status_code == 400 and voice_call.json()["detail"] == "Unknown analytics add-on."
    assert len(_checkouts(shop[1])) == before


# ------------------------------------------------------------------ 2. the legacy / API-only licence checkout

@BUILDERS
def test_2_legacy_licence_checkout_sells_a_new_licence_that_overwrites_the_reversed_row(shop, db_path, stripe, builder):
    """KNOWN GAP (owner decision): POST /api/customer/vms-license/checkout (not linked from My subscription's v2 page;
    API-only) counts only ACTIVE licence rows, so the reversed account may buy a new one-time licence (8 cameras here).
    Paid, the webhook updates the SAME customer_entitlements row in place: status active, reason cleared, the old
    Checkout Session and PaymentIntent replaced by the new ones. No R7 archive is written (R7's audit copy exists only on
    the Build Your System path). The reversal stays recorded in billing_reversals. Capacity returns: licence min(plan 5,
    licence 8) -> the running plan's 5 cameras, download allowed. A replay of the OLD reversal (new event id) does not
    suspend the new licence (it names the old PaymentIntent)."""
    client = shop[0]
    customer_id, ids, stripe_customer, reason = _reversed_running(shop, db_path, stripe, builder, "c5e-lic@example.test")
    old = dict(_licence_row(db_path, customer_id))
    reversals_before = _one(db_path, "SELECT COUNT(*) AS n FROM billing_reversals")["n"]
    response = client.post(LICENCE_CHECKOUT, json={"capacity": 8})
    assert response.status_code == 200, response.text
    assert (response.json()["capacity"], response.json()["billing_type"]) == (8, "one_time")
    fields = [item for item in shop[1] if item.get("mode") == "payment"][-1]
    assert (fields["line_items[0][price]"], fields["metadata[anyaicam_product_class]"]) == ("price_vms_8", "vms_license")
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING  # an open checkout grants nothing

    _ok(client, _licence_paid_event(shop, stripe_customer))
    new = _licence_row(db_path, customer_id)
    assert new["id"] == old["id"]  # the same row, overwritten
    assert (new["status"], new["suspended_reason"], new["camera_slot_quantity"]) == ("active", None, 8)
    assert (new["stripe_checkout_session_id"], new["stripe_payment_intent_id"]) == ("cs_c5e_licence", "pi_c5e_licence")
    assert (old["stripe_checkout_session_id"], old["stripe_payment_intent_id"], old["suspended_reason"]) == (
        "cs_test_race0001", "pi_race1", reason)  # what the row said before -- now only in billing_reversals / Stripe
    assert _superseded(db_path, customer_id) == []  # no R7 archive
    assert _one(db_path, "SELECT COUNT(*) AS n FROM billing_reversals")["n"] == reversals_before
    assert _one(db_path, "SELECT payment_intent FROM billing_reversals WHERE payment_intent='pi_race1'")
    assert _entitled(db_path, customer_id) == (True, True, 5, 5, 5)

    replay = _refund(_ids(1), "evt_c5e_old_refund_again") if reason == "refunded" else _dispute(_ids(1), "created", event_id="evt_c5e_old_dp_again")
    _ok(client, replay)
    assert (_licence_row(db_path, customer_id)["status"], _licence_row(db_path, customer_id)["stripe_payment_intent_id"]) == (
        "active", "pi_c5e_licence")
    # A second licence is refused as before.
    again = client.post(LICENCE_CHECKOUT, json={"capacity": 8})
    assert again.status_code == 409 and "already covers 8" in again.json()["detail"]


def test_2_legacy_licence_checkout_before_activation_is_refused(shop, db_path, stripe):
    """INTENDED (C5 gate): refunded licence and ended plan -> not activated -> 409 activate_first, no Stripe call.
    The way back is Build Your System (R7, which archives the old order and charges the licence)."""
    customer_id, *_ = _r7_ready(shop, db_path, stripe, "c5e-lic-ended@example.test")
    before = len(shop[1])
    response = shop[0].post(LICENCE_CHECKOUT, json={"capacity": 8})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "activate_first"
    assert len(shop[1]) == before


@BUILDERS
def test_2_legacy_software_tier_checkout_grants_no_capacity(shop, db_path, stripe, builder, monkeypatch):
    """POST /api/payments/checkout (pre-billing-v2 starter/professional/enterprise software tiers): 503 while its
    Price is unset (ANYAICAM_STRIPE_PRICE_STARTER, read at import). If a Price were configured it opens a subscription
    Checkout for the reversed account (only require_activated guards it); its payment grants no camera capacity,
    because no camera-slot tier maps that Price. Recorded so the route is not mistaken for a licence path."""
    import main
    client = shop[0]
    customer_id, ids, stripe_customer, _ = _reversed_running(shop, db_path, stripe, builder, "c5e-legacy@example.test")
    monkeypatch.setattr(main, "STRIPE_PRICE_STARTER", "")
    unset = client.post(LEGACY_CHECKOUT, json={"plan": "starter", "quantity": 1})
    assert unset.status_code == 503 and unset.json()["detail"] == "Stripe price is not configured for starter."
    monkeypatch.setattr(main, "STRIPE_PRICE_STARTER", "price_c5e_starter")
    opened = client.post(LEGACY_CHECKOUT, json={"plan": "starter", "quantity": 1})
    assert opened.status_code == 200, opened.text
    fields = _checkouts(shop[1])[-1]
    assert (fields["line_items[0][price]"], fields["metadata[anyaicam_customer_id]"]) == ("price_c5e_starter", customer_id)
    stripe["now"]["subscriptions"]["sub_c5e_starter"] = {
        "id": "sub_c5e_starter", "object": "subscription", "status": "active", "customer": stripe_customer,
        "latest_invoice": "in_first_sub_c5e_starter", "metadata": {"anyaicam_customer_id": customer_id},
        "items": {"data": [{"id": "si_c5e_starter", "quantity": 1, "price": {"id": "price_c5e_starter"}}]}}
    _ok(client, {"id": "evt_c5e_starter", "type": "checkout.session.completed", "livemode": False, "data": {"object": {
        "id": "cs_c5e_starter", "mode": "subscription", "payment_status": "paid", "status": "complete",
        "customer": stripe_customer, "subscription": "sub_c5e_starter", "metadata": _metadata(fields)}}})
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING
    assert _licence_row(db_path, customer_id)["status"] == "suspended"


# ------------------------------------------------------------------ 3. camera-slot / capacity paths

@BUILDERS
def test_3_no_capacity_path_is_open_to_the_reversed_account(shop, db_path, stripe, builder):
    """INTENDED: the fixed camera-slot checkout was removed (404); Build Your System refuses a second order while the
    plan runs (409, R7 only supersedes an ENDED order); the order summary sends the customer away (303) with no
    checkout; a second plan beside the running one is refused (409). No Stripe call on any of them."""
    client = shop[0]
    customer_id, *_ = _reversed_running(shop, db_path, stripe, builder, "c5e-cap@example.test")
    before = len(shop[1])
    assert client.post("/api/customer/camera-slots/checkout", json={}).status_code == 404
    build = _checkout(client)
    assert build.status_code == 409 and build.json()["detail"] == "This account already has a camera plan or a paid order."
    summary = client.get("/order-summary?plan=ai_local&cameras=10&vms_licence=10")
    assert summary.status_code == 303 and 'id="order-checkout"' not in summary.text
    assert _open_plan_checkout(shop).status_code == 409
    assert len(shop[1]) == before
    assert _superseded(db_path, customer_id) == []
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING


# ------------------------------------------------------------------ 4. plan change on the running plan

@BUILDERS
def test_4_plan_changes_are_charged_and_capacity_stays_zero(shop, db_path, stripe, builder, monkeypatch):
    """KNOWN GAP (owner decision): /api/v2/customer/subscription/change checks neither activation nor the licence.
    More cameras (5 -> 8) and AI Local -> Hybrid are applied at once with Stripe proration (always_invoice), a smaller
    plan is scheduled for renewal -- the plan grows, the customer pays more, usable / signed capacity stays 0 and
    nothing on the change tells the customer why. Cancel at period end works: INTENDED (C5: the running plan stays
    manageable)."""
    client = shop[0]
    customer_id, ids, stripe_customer, _ = _reversed_running(shop, db_path, stripe, builder, "c5e-change@example.test")
    page = client.get("/subscription-portal").text
    assert 'id="v2-change-button"' in page and 'id="v2-activate-first"' not in page
    posts = _stripe_writes(monkeypatch, stripe)

    more = client.post(CHANGE, json={"plan_key": "ai_local", "camera_quantity": 8})
    assert more.status_code == 200 and more.json()["status"] == "updated", more.text
    path, write = posts[-1]
    assert path == f"/v1/subscriptions/{ids['sub']}"
    assert (write["items[0][quantity]"], write["proration_behavior"]) == ("8", "always_invoice")
    assert _plan_row(db_path, customer_id)["camera_quantity"] == 8
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING
    assert _capacity(db_path, customer_id)["plan_camera_slots"] == 8

    hybrid = client.post(CHANGE, json={"plan_key": "hybrid", "camera_quantity": 8})
    assert hybrid.status_code == 200 and hybrid.json()["status"] == "updated"
    assert posts[-1][1]["items[0][price]"] == PRICES["hybrid"] and _plan_row(db_path, customer_id)["plan_key"] == "hybrid"
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING

    fewer = client.post(CHANGE, json={"plan_key": "hybrid", "camera_quantity": 2})
    assert fewer.status_code == 200 and fewer.json()["status"] == "scheduled"
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING
    assert not any("vms" in str(value).lower() for _, write in posts for value in write.values())  # never a licence line

    cancel = client.post(CANCEL)
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancellation_scheduled"
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING


# ------------------------------------------------------------------ 5. the appliance exemption boundary

def _appliance_order(db_path, customer_id, intent):
    _exec(db_path, "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,stripe_checkout_session_id,"
                   "stripe_payment_intent_id,quantity,amount_cents,currency,status,fulfillment_status,created_at,updated_at) "
                   "VALUES(?,?,'AIC-APPLIANCE-RYZEN-STARTER','AnyAiCam Starter','price_hw','cs_hw_c5e',?,1,124999,'usd',"
                   "'paid','delivered','2026-10-01','2026-10-01')", (f"hw-{customer_id}", customer_id, intent))


def _reverse_appliance(db_path, intent, reason):
    """The real billing_status step a refund / dispute of the appliance payment runs."""
    import billing_status
    with override_target(sqlite_path=str(db_path)):
        assert billing_status.reverse_hardware_orders(intent, reason) == 1


@pytest.mark.parametrize("appliance", ["refunded", "disputed"])
@pytest.mark.parametrize("licence", ["refunded", "disputed"])
def test_5_reversed_appliance_and_reversed_licence_give_no_plan_capacity(shop, db_path, appliance, licence):
    """INTENDED: once the appliance order is refunded or disputed it no longer includes the licence
    (appliance_includes_vms_license counts PAID orders only), so the reversed own-PC licence row applies: running plan,
    capacity 0, no download; with the plan ended the account is not activated."""
    _account(db_path, "cust-apr", licence=licence)
    _appliance_order(db_path, "cust-apr", "pi_hw_apr")
    assert _entitled(db_path, "cust-apr") == (True, True, 6, 6, 6)  # paid appliance: exempt (existing C5 test)
    _reverse_appliance(db_path, "pi_hw_apr", appliance)
    assert _entitled(db_path, "cust-apr") == REVERSED_RUNNING
    _exec(db_path, "UPDATE camera_plan_entitlements_v2 SET status='cancelled',camera_quantity=0 WHERE customer_id='cust-apr'")
    assert _entitled(db_path, "cust-apr") == (False, False, 0, 0, 0)


def test_5_a_won_appliance_dispute_restores_the_exemption(shop, db_path):
    """INTENDED: billing_status.restore_hardware_orders (won dispute) makes the order paid again -> exempt again."""
    import billing_status
    _account(db_path, "cust-apw", licence="refunded")
    _appliance_order(db_path, "cust-apw", "pi_hw_apw")
    _reverse_appliance(db_path, "pi_hw_apw", "disputed")
    assert _entitled(db_path, "cust-apw") == REVERSED_RUNNING
    with override_target(sqlite_path=str(db_path)):
        assert billing_status.restore_hardware_orders("pi_hw_apw") == 1
    assert _entitled(db_path, "cust-apw") == (True, True, 6, 6, 6)


@pytest.mark.parametrize("appliance", ["refunded", "disputed"])
def test_5_reversed_appliance_without_a_licence_row_keeps_full_plan_capacity(shop, db_path, appliance):
    """KNOWN GAP (owner decision; outside C5, which keys on an own-PC licence ROW): an appliance customer never had a
    vms_license row, so after the appliance payment is refunded or disputed own_pc_licence_reversed is False and the
    billing-v2 rule "an active per-camera plan carries the software licence" gives full capacity and download."""
    _account(db_path, "cust-apn")
    _appliance_order(db_path, "cust-apn", "pi_hw_apn")
    _reverse_appliance(db_path, "pi_hw_apn", appliance)
    import customer_entitlements as ce
    import per_camera_billing
    with override_target(sqlite_path=str(db_path)):
        assert ce.appliance_includes_vms_license("cust-apn") is False
        assert per_camera_billing.own_pc_licence_reversed("cust-apn") is False
    assert _entitled(db_path, "cust-apn") == (True, True, 6, 6, 6)


# ------------------------------------------------------------------ 6. the signed feature / capacity snapshot

@BUILDERS
def test_6_signed_snapshot_of_the_reversed_running_plan_account(shop, db_path, stripe, builder):
    """camera_capacity: usable 0, plan 5, licence 0 (INTENDED, C5). features: talk_down True (AI Local includes
    supported Talk Down, read from the active plan), voice_call False (not part of ordinary Talk Down). KNOWN GAP
    (cosmetic, owner decision): Talk Down is signed as entitled although the appliance may run no camera."""
    import feature_entitlements
    customer_id, *_ = _reversed_running(shop, db_path, stripe, builder, "c5e-snap@example.test")
    with override_target(sqlite_path=str(db_path)):
        snapshot = feature_entitlements.appliance_snapshot({"id": "appl-c5e", "cloud_id": "cloud-c5e", "customer_id": customer_id})
    assert snapshot["camera_capacity"] == {"camera_slot_quantity": 0, "plan_camera_slots": 5, "vms_license_capacity": 0}
    assert snapshot["features"] == {"talk_down": True, "voice_call": False}
    assert snapshot["signature"]
