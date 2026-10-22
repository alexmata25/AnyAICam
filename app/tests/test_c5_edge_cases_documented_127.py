"""C5 edge cases, DOCUMENTED (owner, 2026-10-21: "Document and test the remaining C5 edge cases, especially active paid
plans with reversed licenses, add-on purchases, and the legacy checkout endpoint. Do not silently expand billing
behavior.") -- and the four KNOWN GAPS the owner then CORRECTED (2026-10-21: "For C5, I approve narrowly scoped
corrections to: 1. Close the legacy license-only API bypass for refunded/disputed licenses. [...] 2. Prevent Analytics
and Face Access purchases when the account's license is reversed and camera capacity is zero. 3. Prevent paid plan
upgrades and scheduled downgrades in that same state, but preserve cancellation. 4. Prevent refunded/disputed appliance
purchases from retaining camera capacity through an active plan alone. [...] Defer cosmetic Talk Down entitlement
cleanup and the non-bypassing refund replay issue").

Every test pins what the candidate does, so any later change to these paths fails a test and has to be made -- and
its decision recorded -- on purpose. Each case says INTENDED (the C5 design), CORRECTED (owner, 2026-10-21; one shared
predicate per_camera_billing.purchase_reversed = own-PC licence or AnyAiCam appliance purchase refunded/disputed with
nothing else licensing the software; the purchase gates also require usable camera capacity 0,
per_camera_billing.reversed_without_capacity) or DEFERRED (owner decision: documented, not changed).

The account under test ("reversed + running plan"): an own-PC customer whose one-time VMS licence payment is REFUNDED or
DISPUTED while a per-camera plan is still RUNNING. Two real ways to get there, both through the webhook route:
* later-paid-month: order 1 (licence + AI Local x5) fully refunded, then month 2 of the same subscription is paid
  (invoice.paid) -> the plan is active again, the licence stays suspended 'refunded';
* late-checkout: order 1 paid, its plan ended, a My subscription plan checkout opened (allowed then), the licence payment
  disputed, then that checkout paid -> a new active plan (AI Local x5), the licence suspended 'disputed'.
C5 keeps such an account ACTIVATED (its running plan stays manageable) but its plan carries NO software licence:
download refused, licence / usable / signed capacity 0.

Cases (routes in the candidate):
1. Add-ons -- POST /api/customer/analytics/checkout (main.py) + the webhook route:
   CORRECTED (owner item 2): Advanced Analytics / analytics bundles and Face Access are refused (409 licence_reversed,
   no Stripe call) while the account can run 0 cameras; add-ons it already holds are kept (not revoked). Talk Down is
   refused on AI Local as before (included with the plan); Voice Call is not sold on its own: unchanged.
2. Legacy / API-only one-time licence checkout -- POST /api/customer/vms-license/checkout (main.py):
   CORRECTED (owner item 1): refused (409 licence_reversed, no Stripe call) while the own-PC licence is refunded or
   disputed; the refunded row and billing_reversals stay as they are; a refund is bought back through Build Your System
   (R7, which archives the old order), a dispute goes to support. Before activation (plan ended) the route answers 409
   activate_first (INTENDED, C5 gate). RESIDUAL (documented): a licence Checkout Session opened before this correction
   and paid after it is a genuine new payment and is still granted on the same row (the reversal stays in
   billing_reversals). DEFERRED (owner, item 7): a replay of the old reversal does not re-suspend such a new licence.
   Legacy software-tier checkout POST /api/payments/checkout: 503 while its Price is unset; with a Price configured it
   opens a Checkout, whose payment grants no camera capacity (no tier maps its Price).
3. Capacity paths -- POST /api/customer/camera-slots/checkout was removed (404). Build Your System refuses a second
   order while the plan runs (409); /order-summary redirects away; a second plan is refused (409). INTENDED.
4. Plan change -- POST /api/v2/customer/subscription/change (per_camera_billing.py): CORRECTED (owner item 3):
   upgrades and scheduled downgrades are refused (409, no Stripe call); cancel at period end works (INTENDED). There is
   no un-cancel route in AnyAiCam (only Stripe's billing portal), so nothing else changes.
5. Appliance boundary (owner exemption: a PAID AnyAiCam appliance includes the licence): a refunded or disputed appliance
   order no longer exempts the account (INTENDED); a won appliance dispute restores it (INTENDED). CORRECTED (owner
   item 4): an appliance order refunded/disputed WITHOUT any own-PC licence row no longer keeps capacity through the
   active plan alone (licence / usable / signed 0, no download); a won dispute restores it. Controls unchanged: a paid
   appliance, an active licence, a plan-only account.
6. Signed snapshot (feature_entitlements.appliance_snapshot): camera_capacity {0, plan, 0}; talk_down True from the AI
   Local plan, voice_call False. DEFERRED (owner, cosmetic): Talk Down is signed as entitled while no camera may run.

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
from test_r7_repurchase_after_refund_127 import (_capacity, _end_order1, _licence_row, _new_order, _ok, _order1_paid,
                                                 _order2_paid, _plan_row, _r7_ready, _subscription, _superseded)
from test_c5_refunded_licence_no_plan_capacity_127 import (_account, _entitled, _exec, _open_plan_checkout,
                                                           _pay_plan_checkout, _signed)

pytestmark = [pytest.mark.real_activation, pytest.mark.usefixtures("legacy_own_pc_checkout")]  # own-PC orders in their pre-2026-10-22 subscription shape (conftest legacy_own_pc_checkout)


@pytest.fixture(autouse=True)
def _service_activated(monkeypatch):
    """These scenarios are about the licence (C5), not the service-activation rule of 2026-10-22: the customer's PC is
    linked and running, so a plan bought in My subscription may be charged now (test_activation_billing_127 covers the
    refusal before activation)."""
    import build_orders
    monkeypatch.setattr(build_orders, "service_activated", lambda customer_id: True)

ADDON_CHECKOUT = "/api/customer/analytics/checkout"
LICENCE_CHECKOUT = "/api/customer/vms-license/checkout"
LEGACY_CHECKOUT = "/api/payments/checkout"
CHANGE = "/api/v2/customer/subscription/change"
CANCEL = "/api/v2/customer/subscription/cancel"
REVERSED_RUNNING = (True, False, 0, 0, 0)  # activated, no download, licence / usable / signed capacity 0
FULL_PLAN = (True, True, 6, 6, 6)  # a seeded account's AI Local x6 with its licence
PURCHASE_REVERSED = ("Your AnyAiCam software license payment was refunded or is disputed, so this account can run 0 "
                     "cameras. Add-ons and plan changes are unavailable until that is resolved; you can still cancel your "
                     "plan. Contact AnyAiCam support for help.")
LICENCE_REFUNDED = ("Your previous VMS software license payment was refunded, so a license cannot be bought again here. "
                    "Place a new order in Build Your System (/order-summary) once your current camera plan has ended.")
LICENCE_DISPUTED = ("Your previous VMS software license payment is disputed, so a license cannot be bought here. "
                    "Contact AnyAiCam support to resolve the dispute.")


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


def _licence_paid_event(customer_id, stripe_customer, session_id="cs_c5e_licence", intent="pi_c5e_licence"):
    """checkout.session.completed (mode=payment) of a one-time 8-camera licence Checkout Session -- one opened through
    POST /api/customer/vms-license/checkout before the owner's correction (the metadata that route writes)."""
    metadata = {"anyaicam_stripe_price_id": "price_vms_8", "anyaicam_customer_id": customer_id,
                "anyaicam_product_class": "vms_license", "anyaicam_vms_license_capacity": "8"}
    return {"id": f"evt_{session_id}", "type": "checkout.session.completed", "livemode": False, "data": {"object": {
        "id": session_id, "mode": "payment", "payment_status": "paid", "status": "complete", "customer": stripe_customer,
        "payment_intent": intent, "amount_total": 49999, "metadata": metadata}}}


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


def _refused_licence_reversed(response):
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"code": "licence_reversed", "message": PURCHASE_REVERSED}


def _predicates(db_path, customer_id):
    import per_camera_billing
    with override_target(sqlite_path=str(db_path)):
        return (per_camera_billing.purchase_reversed(customer_id), per_camera_billing.reversed_without_capacity(customer_id))


# ------------------------------------------------------------------ 1. add-ons on a reversed + running-plan account

@BUILDERS
def test_1_advanced_analytics_add_on_is_refused_with_zero_camera_capacity(shop, db_path, stripe, builder):
    """CORRECTED (owner item 2; was KNOWN GAP: sold, paid and granted with 0 cameras): My subscription still lists the
    add-on (page unchanged), but the checkout is refused with 409 licence_reversed before any Stripe call, so nothing is
    sold or granted; capacity stays 0."""
    client = shop[0]
    customer_id, ids, stripe_customer, _ = _reversed_running(shop, db_path, stripe, builder, "c5e-addon@example.test")
    page = client.get("/subscription-portal")
    assert page.status_code == 200 and "advanced_analytics" in re.findall(r'data-addon-key="([^"]+)"', page.text)
    before = len(shop[1])
    for addon_key in ("advanced_analytics", "ai_essentials", "ai_professional", "vehicle_intelligence"):
        _refused_licence_reversed(client.post(ADDON_CHECKOUT, json={"addon_key": addon_key, "quantity": 2}))
    assert len(shop[1]) == before  # no Stripe call
    assert _active_analytics(db_path, customer_id) == ([], [])
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING
    assert _signed(db_path, customer_id) == {"camera_slot_quantity": 0, "plan_camera_slots": 5, "vms_license_capacity": 0}


@BUILDERS
def test_1_face_access_is_refused_with_or_without_a_door(shop, db_path, stripe, builder, monkeypatch):
    """CORRECTED (owner item 2; was KNOWN GAP: the per-door checkout opened once a door was set up): every Face Access
    SKU is refused with 409 licence_reversed, door or not, before any Stripe call."""
    client = shop[0]
    customer_id, *_ = _reversed_running(shop, db_path, stripe, builder, "c5e-face@example.test")
    monkeypatch.setenv("ANYAICAM_STRIPE_PRICE_FACE_ACCESS_SMALL", "price_face_small")
    before = len(shop[1])
    _refused_licence_reversed(client.post(ADDON_CHECKOUT, json={"addon_key": "face_access_small"}))
    _camera(db_path, customer_id, door=True)
    for addon_key in ("face_access_small", "face_access_medium", "face_access_large", "facial_recognition"):
        _refused_licence_reversed(client.post(ADDON_CHECKOUT, json={"addon_key": addon_key}))
    assert len(shop[1]) == before
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


def test_1_an_add_on_bought_before_the_reversal_is_kept(shop, db_path, stripe):
    """Owner: existing valid entitlements are not modified. Advanced Analytics bought (and paid) while the licence was
    active stays active after the licence refund and the later paid month; only NEW add-on purchases are refused."""
    client = shop[0]
    customer_id, ids, events, stripe_customer = _order1_paid(shop, db_path, stripe, "c5e-held@example.test")
    opened = client.post(ADDON_CHECKOUT, json={"addon_key": "advanced_analytics", "quantity": 2})
    assert opened.status_code == 200, opened.text
    _ok(client, _addon_paid_event(shop, stripe, customer_id, stripe_customer, "sub_c5e_held"))
    held = (["lpr", "people_counting", "ppe"], ["advanced_analytics"])
    assert _active_analytics(db_path, customer_id) == held
    _ok(client, _refund(ids, "evt_c5e_held_refund"))
    _later_invoice(stripe, ids, "in_race_m2")
    _subscription(stripe, ids, customer_id, stripe_customer, quantity=5, latest_invoice="in_race_m2")
    _ok(client, _invoice_paid(ids, "in_race_m2", "pi_in_race_m2"))
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING
    assert _active_analytics(db_path, customer_id) == held  # kept
    assert _one(db_path, "SELECT status FROM addon_subscriptions WHERE customer_id=? AND addon_key='advanced_analytics'",
                (customer_id,))["status"] == "active"
    before = len(shop[1])
    _refused_licence_reversed(client.post(ADDON_CHECKOUT, json={"addon_key": "vehicle_intelligence"}))
    assert len(shop[1]) == before


# ------------------------------------------------------------------ 2. the legacy / API-only licence checkout

@BUILDERS
def test_2_legacy_licence_checkout_is_refused_for_a_reversed_licence(shop, db_path, stripe, builder):
    """CORRECTED (owner item 1; was KNOWN GAP: a new licence was sold and its webhook overwrote the reversed row in place,
    with no R7 archive): 409 licence_reversed before any Stripe call, for every capacity. The refunded/disputed row and
    its billing_reversals record stay exactly as they were; nothing is archived or superseded."""
    client = shop[0]
    customer_id, ids, stripe_customer, reason = _reversed_running(shop, db_path, stripe, builder, "c5e-lic@example.test")
    old = dict(_licence_row(db_path, customer_id))
    reversals = _one(db_path, "SELECT COUNT(*) AS n FROM billing_reversals")["n"]
    before = len(shop[1])
    for capacity in (8, 16):
        response = client.post(LICENCE_CHECKOUT, json={"capacity": capacity})
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        if reason == "refunded":
            assert detail == {"code": "licence_reversed", "reason": "refunded", "order_url": "/order-summary",
                              "message": LICENCE_REFUNDED}
        else:
            assert detail == {"code": "licence_reversed", "reason": "disputed", "message": LICENCE_DISPUTED}
    assert len(shop[1]) == before  # no Stripe call
    assert dict(_licence_row(db_path, customer_id)) == old
    assert (old["stripe_checkout_session_id"], old["stripe_payment_intent_id"], old["suspended_reason"]) == (
        "cs_test_race0001", "pi_race1", reason)
    assert _one(db_path, "SELECT COUNT(*) AS n FROM billing_reversals")["n"] == reversals
    assert _one(db_path, "SELECT payment_intent FROM billing_reversals WHERE payment_intent='pi_race1'")
    assert _superseded(db_path, customer_id) == []
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING


def test_2_after_the_refusal_the_r7_order_path_restores_licence_and_capacity(shop, db_path, stripe):
    """The repurchase path the refusal points to: once the running plan ends, Build Your System places a NEW order
    (R7 archives order 1) that charges the licence again; paid, licence and capacity return. The legacy route is never
    needed."""
    client = shop[0]
    customer_id, ids, stripe_customer, _ = _reversed_running(shop, db_path, stripe, _later_paid_month, "c5e-r7@example.test")
    assert client.post(LICENCE_CHECKOUT, json={"capacity": 8}).json()["detail"]["code"] == "licence_reversed"
    _end_order1(shop, db_path, stripe, customer_id, ids, {}, stripe_customer)
    _new_order(shop)
    assert len(_superseded(db_path, customer_id)) == 1  # R7 archive of order 1
    _order2_paid(shop, db_path, stripe, customer_id, stripe_customer)
    assert _licence_row(db_path, customer_id)["status"] == "active"
    assert _entitled(db_path, customer_id) == (True, True, 10, 10, 10)
    assert _predicates(db_path, customer_id) == (False, False)


def test_2_legacy_licence_checkout_before_activation_is_refused(shop, db_path, stripe):
    """INTENDED (C5 gate): refunded licence and ended plan -> not activated -> 409 activate_first, no Stripe call.
    The way back is Build Your System (R7, which archives the old order and charges the licence)."""
    customer_id, *_ = _r7_ready(shop, db_path, stripe, "c5e-lic-ended@example.test")
    before = len(shop[1])
    response = shop[0].post(LICENCE_CHECKOUT, json={"capacity": 8})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "activate_first"
    assert len(shop[1]) == before


@BUILDERS
def test_2_a_licence_session_opened_before_the_correction_and_paid_after_it_is_still_granted(shop, db_path, stripe, builder):
    """RESIDUAL, documented (not changed): with the gate, no new licence session can be opened for a reversed account
    (and none while a licence is active: "already covers"), so a licence session paid after a reversal can only be one
    opened before this correction was deployed (Stripe expires Checkout Sessions within 24 hours). It is a genuine new
    payment, so the webhook grants it -- blocking the grant would keep the customer's money and license nothing -- on
    the same row (one per customer+product); the old reversal stays in billing_reversals. DEFERRED (owner item 7): a
    replay of the OLD reversal (new event id) does not suspend that new licence (it names the old PaymentIntent)."""
    client = shop[0]
    customer_id, ids, stripe_customer, reason = _reversed_running(shop, db_path, stripe, builder, "c5e-inflight@example.test")
    reversals = _one(db_path, "SELECT COUNT(*) AS n FROM billing_reversals")["n"]
    _ok(client, _licence_paid_event(customer_id, stripe_customer))
    new = _licence_row(db_path, customer_id)
    assert (new["status"], new["suspended_reason"], new["camera_slot_quantity"]) == ("active", None, 8)
    assert (new["stripe_checkout_session_id"], new["stripe_payment_intent_id"]) == ("cs_c5e_licence", "pi_c5e_licence")
    assert _one(db_path, "SELECT COUNT(*) AS n FROM billing_reversals")["n"] == reversals
    assert _one(db_path, "SELECT payment_intent FROM billing_reversals WHERE payment_intent='pi_race1'")
    assert _entitled(db_path, customer_id) == (True, True, 5, 5, 5)
    replay = _refund(_ids(1), "evt_c5e_old_refund_again") if reason == "refunded" else _dispute(_ids(1), "created", event_id="evt_c5e_old_dp_again")
    _ok(client, replay)
    assert (_licence_row(db_path, customer_id)["status"], _licence_row(db_path, customer_id)["stripe_payment_intent_id"]) == (
        "active", "pi_c5e_licence")
    again = client.post(LICENCE_CHECKOUT, json={"capacity": 8})
    assert again.status_code == 409 and "already covers 8" in again.json()["detail"]


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
def test_4_plan_upgrades_and_downgrades_are_refused_and_cancel_works(shop, db_path, stripe, builder, monkeypatch):
    """CORRECTED (owner item 3; was KNOWN GAP: upgrades charged at once with proration and downgrades scheduled while
    capacity stayed 0): more cameras, AI Local -> Hybrid and a smaller plan are each refused with 409 before any Stripe
    call (the plan row is unchanged). The detail is a plain string because the change button shows data.detail as text.
    Cancel at period end works: INTENDED (the running plan stays manageable)."""
    client = shop[0]
    customer_id, ids, stripe_customer, _ = _reversed_running(shop, db_path, stripe, builder, "c5e-change@example.test")
    page = client.get("/subscription-portal").text
    assert 'id="v2-change-button"' in page and 'id="v2-activate-first"' not in page
    posts = _stripe_writes(monkeypatch, stripe)
    plan_before = dict(_plan_row(db_path, customer_id))
    for change in ({"plan_key": "ai_local", "camera_quantity": 8}, {"plan_key": "hybrid", "camera_quantity": 5},
                   {"plan_key": "ai_local", "camera_quantity": 2}, {"plan_key": "basic_local", "camera_quantity": 5}):
        refused = client.post(CHANGE, json=change)
        assert refused.status_code == 409 and refused.json()["detail"] == PURCHASE_REVERSED, refused.text
    assert posts == []  # no Stripe write (the refusal comes before the subscription is even read)
    assert dict(_plan_row(db_path, customer_id)) == plan_before
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING

    cancel = client.post(CANCEL)
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancellation_scheduled"
    assert [path for path, _ in posts] == [f"/v1/subscriptions/{ids['sub']}"]
    assert posts[-1][1] == {"cancel_at_period_end": "true"}
    assert _entitled(db_path, customer_id) == REVERSED_RUNNING


# ------------------------------------------------------------------ 5. the appliance exemption boundary

def _appliance_order(db_path, customer_id, intent, suffix="", status="paid"):
    _exec(db_path, "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,stripe_checkout_session_id,"
                   "stripe_payment_intent_id,quantity,amount_cents,currency,status,fulfillment_status,created_at,updated_at) "
                   "VALUES(?,?,'AIC-APPLIANCE-RYZEN-STARTER','AnyAiCam Starter','price_hw',?,?,1,124999,'usd',"
                   "?,'delivered','2026-10-01','2026-10-01')",
          (f"hw-{customer_id}{suffix}", customer_id, f"cs_hw_c5e{suffix}", intent, status))


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
    assert _entitled(db_path, "cust-apr") == FULL_PLAN  # paid appliance: exempt (existing C5 test)
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
    assert _entitled(db_path, "cust-apw") == FULL_PLAN


@pytest.mark.parametrize("appliance", ["refunded", "disputed"])
def test_5_reversed_appliance_without_a_licence_row_gives_no_plan_capacity(shop, db_path, appliance):
    """CORRECTED (owner item 4; was KNOWN GAP: full capacity and download through the active plan alone): an appliance
    customer never had a vms_license row; once the appliance payment is refunded or disputed, purchase_reversed holds,
    so the plan carries no licence: licence / usable / signed 0, no download, add-ons and plan changes gated. The
    account stays activated (account_activated is unchanged: an ended plan still counts here, because the R7 new-order
    path is own-PC only)."""
    _account(db_path, "cust-apn")
    _appliance_order(db_path, "cust-apn", "pi_hw_apn")
    assert _entitled(db_path, "cust-apn") == FULL_PLAN and _predicates(db_path, "cust-apn") == (False, False)
    _reverse_appliance(db_path, "pi_hw_apn", appliance)
    import customer_entitlements as ce
    import per_camera_billing
    with override_target(sqlite_path=str(db_path)):
        assert ce.appliance_includes_vms_license("cust-apn") is False
        assert per_camera_billing.own_pc_licence_reversed("cust-apn") is False
    assert _predicates(db_path, "cust-apn") == (True, True)
    assert _entitled(db_path, "cust-apn") == REVERSED_RUNNING
    assert _signed(db_path, "cust-apn") == {"camera_slot_quantity": 0, "plan_camera_slots": 6, "vms_license_capacity": 0}
    _exec(db_path, "UPDATE camera_plan_entitlements_v2 SET status='cancelled',camera_quantity=0 WHERE customer_id='cust-apn'")
    assert _entitled(db_path, "cust-apn") == (True, False, 0, 0, 0)


def test_5_a_won_appliance_dispute_without_a_licence_row_restores_capacity(shop, db_path):
    import billing_status
    _account(db_path, "cust-apd")
    _appliance_order(db_path, "cust-apd", "pi_hw_apd")
    _reverse_appliance(db_path, "pi_hw_apd", "disputed")
    assert _entitled(db_path, "cust-apd") == REVERSED_RUNNING
    with override_target(sqlite_path=str(db_path)):
        assert billing_status.restore_hardware_orders("pi_hw_apd") == 1
    assert _entitled(db_path, "cust-apd") == FULL_PLAN and _predicates(db_path, "cust-apd") == (False, False)


@pytest.mark.parametrize("kind", ["paid-appliance", "paid-appliance-beside-a-refunded-one", "active-licence",
                                  "active-licence-beside-a-refunded-appliance", "plan-only"])
def test_5_controls_paid_appliance_active_licence_and_plan_only_accounts_are_unchanged(shop, db_path, kind):
    """Controls (owner: existing valid entitlements are not modified): a paid appliance order, an active licence row,
    or a plan-only account (never a licence or appliance purchase) keeps full plan capacity and is never gated."""
    customer_id = f"cust-ctl-{kind}"
    _account(db_path, customer_id)
    if kind.startswith("paid-appliance"):
        _appliance_order(db_path, customer_id, "pi_hw_ctl_paid")
    if kind.endswith("refunded-one") or kind.endswith("refunded-appliance"):
        _appliance_order(db_path, customer_id, "pi_hw_ctl_refunded", suffix="-2")
        _reverse_appliance(db_path, "pi_hw_ctl_refunded", "refunded")
    if kind.startswith("active-licence"):
        _exec(db_path, "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,"
                       "updated_at) VALUES(?,?,'vms_license',8,'active','2026-10-01','2026-10-01')", (f"lic-{customer_id}", customer_id))
    assert _predicates(db_path, customer_id) == (False, False)
    assert _entitled(db_path, customer_id) == FULL_PLAN


# ------------------------------------------------------------------ 6. the signed feature / capacity snapshot

@BUILDERS
def test_6_signed_snapshot_of_the_reversed_running_plan_account(shop, db_path, stripe, builder):
    """camera_capacity: usable 0, plan 5, licence 0 (INTENDED, C5). features: talk_down True (AI Local includes
    supported Talk Down, read from the active plan), voice_call False (not part of ordinary Talk Down). DEFERRED
    (cosmetic, owner 2026-10-21): Talk Down is signed as entitled although the appliance may run no camera."""
    import feature_entitlements
    customer_id, *_ = _reversed_running(shop, db_path, stripe, builder, "c5e-snap@example.test")
    with override_target(sqlite_path=str(db_path)):
        snapshot = feature_entitlements.appliance_snapshot({"id": "appl-c5e", "cloud_id": "cloud-c5e", "customer_id": customer_id})
    assert snapshot["camera_capacity"] == {"camera_slot_quantity": 0, "plan_camera_slots": 5, "vms_license_capacity": 0}
    assert snapshot["features"] == {"talk_down": True, "voice_call": False}
    assert snapshot["signature"]
