"""Build Your System orders paid online (owner decisions, 2026-10-05).

Three kinds of order, all built server-side from the account's saved Build
Your System selection (build_system_intents) -- the browser sends nothing
that sets a product, price or Stripe Price ID:

* appliance -- one Stripe Checkout (mode=payment) charges the appliance (and
  any relay modules) today and saves the card for later off-session use
  (payment_intent_data[setup_future_usage]=off_session). The recurring
  storage/service plan does NOT start at checkout: the verified webhook
  records the hardware orders and a storage plan awaiting activation
  (deferred_storage_plans). When the customer activates the appliance the
  plan is created on Stripe with the saved card -- durably and idempotently
  (one claimed row per account, a fixed Idempotency-Key per attempt round,
  and adoption of a subscription Stripe already made for this order), so a
  repeated activation, a retry or a lost answer never creates a second
  subscription or charge. The plan's entitlement is still granted only by
  billing v2 from Stripe's own subscription state.
  Activation is a token activation (activation_status='activated') or, since
  2026-10-11, a claim-code appliance that really enrolled (appliances.
  activated_at, recorded by appliance_claims.record_claim_activation()).
  Starts pending for appliances claimed before that are HELD for an explicit,
  audited owner/admin decision (STORAGE_HELD, /admin/storage-starts) and are
  never charged automatically.
* own_pc -- one Stripe Checkout (mode=subscription): the per-camera plan
  starts today, plus the one-time VMS software license for the smallest
  license tier that covers the camera count, as a one-time line on the
  first invoice. The verified webhook grants the license (vms_license).
* plan -- no hardware chosen: the plan alone, starting today.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request

import per_camera_billing as billing
from partner_db import connection, row, rows

logger = logging.getLogger("anyaicam.build_orders")

APPLIANCE_ORDER, OWN_PC_ORDER, PLAN_ORDER = "appliance", "own_pc", "plan"
STORAGE_AWAITING = "awaiting_activation"
STORAGE_STARTING = "starting"
STORAGE_STARTED = "started"
STORAGE_FAILED = "failed"                # transient: retried by the billing worker
STORAGE_PAYMENT_FAILED = "payment_failed"  # Stripe refused (e.g. card declined): the customer retries
STORAGE_SUPERSEDED = "superseded"        # the account got a plan another way; nothing to start
# Held for an explicit owner/admin decision (db_migrations.hold_pre_cutover_
# claimed_storage_starts): never started by activation, a claim or the worker;
# only admin_start_held_storage() releases it.
STORAGE_HELD = "owner_review"
SERVICE_ACTIVATION_FIRST = {"code": "activate_service_first",
                            "message": "Your monthly plan starts when your AnyAiCam system is activated. Install AnyAiCam (or "
                                       "set up your AnyAiCam appliance) and link it to your account first; nothing is charged "
                                       "for the plan before then."}
STARTING_STALE_SECONDS = 300
RELAY_SKU = "AIC-RELAY-NUMATO-3CH"


class RetryableBuildOrderError(Exception):
    """A Stripe read failed while recording a paid order: the webhook step
    fails so Stripe redelivers the event."""


# ------------------------------------------------------------------ catalog

def _hardware_catalog() -> dict:
    import hardware_orders
    return {sku: {"product": product, "name": name, "cents": cents, "env": env}
            for sku, product, name, cents, env in hardware_orders.HARDWARE_CATALOG}


def vms_license_for(cameras: int) -> dict | None:
    """The smallest one-time VMS license tier that covers this many cameras."""
    from pricing_catalog import VMS_LICENSES
    for capacity, cents, env in sorted(VMS_LICENSES):
        if capacity >= int(cameras):
            return {"capacity": capacity, "cents": cents, "env": env}
    return None


def order_kind(progress: dict) -> str:
    if progress.get("appliance"):
        return APPLIANCE_ORDER
    if progress.get("own_pc"):
        return OWN_PC_ORDER
    return PLAN_ORDER


def _configured(env_var: str, label: str) -> str:
    price_id = os.environ.get(env_var, "").strip()
    if not price_id:
        raise HTTPException(status_code=503, detail=f"Online ordering for {label} is not available yet.")
    return price_id


def order_quote(progress: dict) -> dict:
    """What the order costs, from the server catalog only (cents)."""
    plan = billing.PLANS[progress["plan"]]
    cameras = int(progress.get("cameras") or billing.MIN_CAMERA_QUANTITY)
    monthly = int(plan["monthly_cents_per_camera"]) * cameras
    kind = order_kind(progress)
    one_time = []
    if kind == APPLIANCE_ORDER:
        catalog = _hardware_catalog()
        item = catalog[progress["appliance"]]
        one_time.append({"sku": progress["appliance"], "name": f"{item['name']} appliance", "quantity": 1, "cents": item["cents"]})
        if progress.get("relays") and RELAY_SKU in catalog:
            relay = catalog[RELAY_SKU]
            one_time.append({"sku": RELAY_SKU, "name": relay["name"], "quantity": int(progress["relays"]), "cents": relay["cents"]})
    elif kind == OWN_PC_ORDER:
        license_tier = vms_license_for(cameras)
        if license_tier:
            one_time.append({"sku": f"vms_license_{license_tier['capacity']}", "capacity": license_tier["capacity"],
                             "name": f"AnyAiCam VMS software license (up to {license_tier['capacity']} cameras)",
                             "quantity": 1, "cents": license_tier["cents"]})
    one_time_total = sum(line["cents"] * line["quantity"] for line in one_time)
    # Owner billing rule (2026-10-22): appliance AND own-PC orders charge only the one-time items at purchase; the
    # monthly plan (saved with its price) starts at service activation. Only a plan-only order starts today.
    starts_today = kind == PLAN_ORDER
    return {"kind": kind, "cameras": cameras, "monthly_cents": monthly, "one_time": one_time,
            "due_today_cents": one_time_total + (monthly if starts_today else 0), "plan_starts_today": starts_today}


# ------------------------------------------------------------------ checkout

def _checkout_fields(customer_id: str, email: str | None, progress: dict) -> tuple[list, str, int]:
    import main
    import friends_family
    import checkout_guard
    plan_key = progress["plan"]
    quote = order_quote(progress)
    cameras = quote["cameras"]
    base = main.PUBLIC_BASE_URL
    if not base:
        raise HTTPException(status_code=503, detail="ANYAICAM_PUBLIC_URL is required for Stripe Checkout.")
    common = [
        ("success_url", f"{base}{billing.ORDER_COMPLETE_PATH}?session_id={{CHECKOUT_SESSION_ID}}"),
        ("cancel_url", f"{base}{billing.ORDER_SUMMARY_PATH}?checkout=cancelled"),
        ("client_reference_id", customer_id),
        ("metadata[anyaicam_customer_id]", customer_id),
        ("metadata[anyaicam_build_order]", quote["kind"]),
        ("metadata[anyaicam_plan_key]", plan_key),
        ("metadata[anyaicam_camera_quantity]", str(cameras)),
    ]
    if quote["kind"] == APPLIANCE_ORDER:
        # F2 (2026-10-21): create_checkout() no longer opens this kind (no shipping address was collected). Kept as
        # the exact shape of appliance orders paid before then, which the webhook below still records and starts.
        catalog = _hardware_catalog()
        fields = [("mode", "payment"), *common,
                  # Saves the card on the account's Stripe customer for the
                  # storage plan that starts at activation.
                  ("payment_intent_data[setup_future_usage]", "off_session"),
                  ("payment_intent_data[metadata][anyaicam_customer_id]", customer_id),
                  ("payment_intent_data[metadata][anyaicam_build_order]", APPLIANCE_ORDER)]
        items, prices = [], []
        for index, line in enumerate(quote["one_time"]):
            entry = catalog[line["sku"]]
            price_id = _configured(entry["env"], entry["name"])
            main.require_stripe_price_matches_catalog(price_id, entry["cents"], None)
            fields += [(f"line_items[{index}][price]", price_id), (f"line_items[{index}][quantity]", str(line["quantity"]))]
            items.append(f"{line['sku']}:{line['quantity']}")
            prices.append(price_id)
        fields += [("metadata[anyaicam_hardware_items]", ",".join(items)),
                   ("allow_promotion_codes", "false")]  # as the existing hardware checkout
        primary_price, primary_quantity = prices[0], 1
    elif quote["kind"] == OWN_PC_ORDER:
        # Owner billing rule (2026-10-22): only the one-time VMS licence is charged now (mode=payment). The monthly plan
        # and its approved Price are saved with the order (metadata -> deferred_storage_plans) and start, with the card
        # saved here, when the customer's AnyAiCam service is activated (start_storage_plan). The licence is never
        # discounted; a Friends & Family discount applies to the plan when it starts.
        plan = billing.PLANS[plan_key]
        plan_price = billing.price_id_for(plan_key)
        if not plan_price:
            raise HTTPException(status_code=503, detail=f"No Stripe Price is configured for {plan_key}.")
        main.require_stripe_price_matches_catalog(plan_price, plan["monthly_cents_per_camera"], "month")
        license_line = quote["one_time"][0]
        license_price = _configured(vms_license_for(cameras)["env"], "the VMS software license")
        main.require_stripe_price_matches_catalog(license_price, license_line["cents"], None)
        fields = [("mode", "payment"), *common,
                  ("line_items[0][price]", license_price), ("line_items[0][quantity]", "1"),
                  ("payment_intent_data[setup_future_usage]", "off_session"),
                  ("payment_intent_data[metadata][anyaicam_customer_id]", customer_id),
                  ("payment_intent_data[metadata][anyaicam_build_order]", OWN_PC_ORDER),
                  ("metadata[anyaicam_vms_license_price_id]", license_price),
                  ("metadata[anyaicam_vms_license_capacity]", str(license_line["capacity"])),
                  ("metadata[anyaicam_plan_price_id]", plan_price),
                  ("metadata[anyaicam_plan_monthly_cents]", str(int(plan["monthly_cents_per_camera"]))),
                  ("allow_promotion_codes", "false")]
        primary_price, primary_quantity = license_price, 1
    else:
        plan = billing.PLANS[plan_key]
        plan_price = billing.price_id_for(plan_key)
        if not plan_price:
            raise HTTPException(status_code=503, detail=f"No Stripe Price is configured for {plan_key}.")
        main.require_stripe_price_matches_catalog(plan_price, plan["monthly_cents_per_camera"], "month")
        fields = [("mode", "subscription"), *common,
                  ("line_items[0][price]", plan_price), ("line_items[0][quantity]", str(cameras)),
                  ("metadata[anyaicam_billing_version]", "2"), ("metadata[anyaicam_stripe_price_id]", plan_price),
                  ("subscription_data[metadata][anyaicam_customer_id]", customer_id),
                  ("subscription_data[metadata][anyaicam_stripe_price_id]", plan_price),
                  ("subscription_data[metadata][anyaicam_billing_version]", "2"),
                  ("subscription_data[metadata][anyaicam_plan_key]", plan_key),
                  ("subscription_data[metadata][anyaicam_camera_quantity]", str(cameras))]
        friends_family.apply_to_checkout_fields(fields, customer_id, "base")
        # 30-day free storage trial (storage_trial.py): storage-only
        # subscription Checkout; nothing is added without an active trial.
        import storage_trial
        fields += storage_trial.subscription_trial_fields(customer_id, checkout=True, plan_key=plan_key)
        primary_price, primary_quantity = plan_price, cameras
    fields.append(("customer", checkout_guard.canonical_stripe_customer(customer_id, email=email)))
    return fields, primary_price, primary_quantity


def create_checkout(identity: dict) -> dict:
    import checkout_guard
    import friends_family
    customer_id = identity["customer_id"]
    if billing.has_camera_plan(customer_id) or live_storage_plan(customer_id):
        raise HTTPException(status_code=409, detail="This account already has a camera plan or a paid order.")
    # R7 (2026-10-21): an order page opened before the refunded order's subscription ended checks out a new order too.
    billing.supersede_refunded_order(customer_id)
    progress = billing.build_progress(customer_id)
    if not progress or progress["state"] != billing.BUILD_PENDING or not progress.get("plan"):
        raise HTTPException(status_code=409, detail="There is no order to check out. Build your system first.")
    if order_kind(progress) == PLAN_ORDER and not service_activated(customer_id):
        # Owner billing rule (2026-10-22): a monthly plan is charged only once the AnyAiCam service is activated.
        raise HTTPException(status_code=409, detail=SERVICE_ACTIVATION_FIRST)
    if order_kind(progress) == APPLIANCE_ORDER:
        # F2 (2026-10-21): appliances are sold only through the hardware checkout (hardware_checkout.py), which
        # collects the shipping address. This older appliance checkout collected none, so it is refused here even
        # for a selection saved before hardware-first (the order summary page already redirects such selections).
        import hardware_checkout
        raise HTTPException(status_code=409, detail="AnyAiCam appliances are ordered through the hardware checkout, which collects "
                                                    "your shipping address. Continue at "
                                                    f"{hardware_checkout.CHECKOUT_PATH}?{hardware_checkout.selection_query(progress)}")
    try:
        fields, price_id, quantity = _checkout_fields(customer_id, identity.get("email"), progress)
    except friends_family.CheckoutHeld as held:
        raise HTTPException(status_code=409, detail=str(held))
    except RuntimeError as missing:  # approved, but the coupon is not configured: never charge full price silently
        raise HTTPException(status_code=503, detail=str(missing))
    session = checkout_guard.create_session(customer_id, "base", price_id=price_id, quantity=quantity, fields=fields)
    if not session.get("id") or not session.get("url"):
        raise HTTPException(status_code=502, detail="Stripe did not return a Checkout Session URL.")
    return {"status": "complete", "checkout_url": session["url"], "session_id": session["id"]}


# ------------------------------------------------------------------ webhook step

def storage_plan(customer_id: str) -> dict | None:
    try:
        return row("SELECT * FROM deferred_storage_plans WHERE customer_id=?", (customer_id,))
    except Exception:
        return None


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _order_still_paid(plan: dict) -> bool:
    """Owner billing rule 5 (2026-10-22): the purchase this plan was saved with is still valid -- paid, never refunded,
    no open dispute. Appliance: its order is still 'paid'. Own PC: the licence bought by that Checkout is active and its
    payment carries no reversal. Checked before a start is claimed and again right before each Stripe request."""
    if (plan.get("order_kind") or APPLIANCE_ORDER) != OWN_PC_ORDER:
        return _hardware_still_paid(plan)
    licence = row("SELECT status,stripe_payment_intent_id FROM customer_entitlements WHERE customer_id=? AND product='vms_license' "
                  "AND stripe_checkout_session_id=?", (plan["customer_id"], plan["checkout_session_id"]))
    if not licence or licence["status"] != "active":
        return False
    import billing_status
    return billing_status.reversal_reason("", str(licence.get("stripe_payment_intent_id") or ""), "") is None


def _deferred_alive(plan: dict | None) -> bool:
    """A saved plan that may still start or has started: not superseded, and either started (a subscription exists) or
    its purchase still valid. A plan whose purchase was refunded or disputed before it started is not alive: it never
    starts, it does not block a new order, and a new order replaces it."""
    if not plan or plan.get("state") == STORAGE_SUPERSEDED:
        return False
    if plan.get("stripe_subscription_id") or plan.get("state") in (STORAGE_STARTED, STORAGE_STARTING):
        return True
    return _order_still_paid(plan)


def live_storage_plan(customer_id: str) -> dict | None:
    """The account's saved plan that still blocks a second plan or order and counts as a paid order. An appliance plan
    keeps its behaviour from before 2026-10-22 (any row that is not superseded; it never starts while its order is
    refunded or disputed, start_storage_plan); an own-PC plan whose licence was refunded or disputed before it started
    does not (owner billing rule: the customer may order again, R7)."""
    plan = storage_plan(customer_id)
    if not plan or plan.get("state") == STORAGE_SUPERSEDED:
        return None
    if (plan.get("order_kind") or APPLIANCE_ORDER) != OWN_PC_ORDER:
        return plan
    return plan if _deferred_alive(plan) else None


def record_deferred_plan(customer_id: str, *, order_kind: str, plan_key: str, cameras: int, stripe_customer_id: str | None,
                         payment_method_id: str | None, checkout_session_id: str, price_id: str | None = None,
                         monthly_cents: int | None = None) -> str:
    """Owner billing rule 2 (2026-10-22): save the plan, camera count and approved monthly Price chosen with a purchase.
    It starts only at service activation (start_storage_plan). One row per account: a redelivered event for the same
    Checkout changes nothing; a new order replaces a saved plan only when that plan is no longer alive (its purchase was
    refunded or disputed and it never started -- the old row is kept in audit_logs). Never for an account that already
    has a running plan. Returns 'recorded', 'replaced', 'unchanged' or 'skipped'."""
    import json
    from partner_db import transaction
    if not customer_id or plan_key not in billing.PLANS or not checkout_session_id:
        return "skipped"
    if not billing.MIN_CAMERA_QUANTITY <= int(cameras or 0) <= billing.MAX_CAMERA_QUANTITY:
        return "skipped"
    if billing.has_camera_plan(customer_id):
        return "skipped"
    if not price_id:  # an order without a saved Price: the plan's current Price at purchase time
        price_id = billing.price_id_for(plan_key)
        monthly_cents = int(billing.PLANS[plan_key]["monthly_cents_per_camera"]) if price_id else None
    now = datetime.now().isoformat()
    values = (plan_key, int(cameras), stripe_customer_id, payment_method_id, checkout_session_id, price_id, monthly_cents, order_kind)
    with transaction() as db:
        existing = db.execute("SELECT * FROM deferred_storage_plans WHERE customer_id=?", (customer_id,)).fetchone()
        existing = dict(existing) if existing else None
        if existing and existing.get("checkout_session_id") == checkout_session_id:
            return "unchanged"
        if existing and _deferred_alive(existing):
            logger.warning("build_order.deferred_plan_kept customer_id=%s existing_session=%s new_session=%s",
                           customer_id, existing.get("checkout_session_id"), checkout_session_id)
            return "unchanged"
        if existing:
            db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", ("", "system", "deferred_plan.replaced_after_reversal", "deferred_storage_plan",
                                                 customer_id, json.dumps({"previous": existing}, default=str, sort_keys=True), now))
            db.execute("UPDATE deferred_storage_plans SET plan_key=?,camera_quantity=?,stripe_customer_id=?,stripe_payment_method_id=?,"
                       "checkout_session_id=?,stripe_price_id=?,monthly_cents_per_camera=?,order_kind=?,state=?,attempt_round=0,"
                       "attempts=0,stripe_subscription_id=NULL,last_error=NULL,started_at=NULL,hold_reason=NULL,held_at=NULL,"
                       "owner_approved_at=NULL,owner_approved_by=NULL,created_at=?,updated_at=? WHERE customer_id=?",
                       (*values, STORAGE_AWAITING, now, now, customer_id))
            return "replaced"
        db.execute("INSERT INTO deferred_storage_plans(customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_payment_method_id,"
                   "checkout_session_id,stripe_price_id,monthly_cents_per_camera,order_kind,state,attempt_round,attempts,created_at,"
                   "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,0,?,?) ON CONFLICT(customer_id) DO NOTHING",
                   (customer_id, *values, STORAGE_AWAITING, now, now))
    return "recorded"


class SavedPriceError(RuntimeError):
    """The monthly Price saved at purchase is no longer a Price of that plan at that amount."""


def _locked_price(plan: dict) -> tuple[str, int]:
    """Owner billing rule 2: the plan starts at the Price saved at purchase -- still accepted only while it is this plan's
    current or grandfathered (_PREVIOUS) Price at exactly the saved amount; otherwise SavedPriceError (the start is held
    for an owner decision, never re-priced silently). A row saved before the price lock uses the plan's current Price."""
    stored = str(plan.get("stripe_price_id") or "")
    if not stored:
        price_id = billing.price_id_for(plan["plan_key"])
        if not price_id:
            raise RuntimeError(f"no Stripe Price is configured for {plan['plan_key']}")
        return price_id, int(billing.PLANS[plan["plan_key"]]["monthly_cents_per_camera"])
    resolved = billing.resolve_price(stored) or {}
    cents = int(plan.get("monthly_cents_per_camera") or 0)
    if resolved.get("plan_key") != plan["plan_key"] or int(resolved.get("monthly_cents_per_camera") or -1) != cents:
        raise SavedPriceError(f"the {plan['plan_key']} Price saved at purchase ({stored}, {cents} cents) is no longer configured")
    return stored, cents


def _line_amounts(session: dict) -> dict:
    """price id -> amount Stripe charged for that line (after discounts)."""
    import main
    try:
        listing = main.stripe_api_get(f"/v1/checkout/sessions/{session['id']}/line_items?limit=20")
    except Exception as error:
        raise RetryableBuildOrderError("checkout line items unavailable") from error
    amounts = {}
    for item in listing.get("data") or []:
        price = (item.get("price") or {}).get("id")
        if price and item.get("amount_total") is not None:
            amounts[price] = int(item["amount_total"])
    return amounts


def _payment_method(session: dict) -> str | None:
    import main
    intent = session.get("payment_intent")
    intent_id = str((intent.get("id") if isinstance(intent, dict) else intent) or "")
    if not intent_id:
        return None
    try:
        found = main.stripe_api_get(f"/v1/payment_intents/{intent_id}")
    except Exception:
        return None  # resolved from the customer's saved cards at activation
    method = found.get("payment_method")
    return str((method.get("id") if isinstance(method, dict) else method) or "") or None


def sync_from_stripe_event(event: dict) -> dict:
    """Webhook step for Build Your System orders (after hardware_orders,
    before sales_commissions). Only a verified, paid checkout counts."""
    event_type = str(event.get("type") or "")
    if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        return {"status": "ignored"}
    session = (event.get("data") or {}).get("object") or {}
    metadata = session.get("metadata") or {}
    kind = str(metadata.get("anyaicam_build_order") or "")
    customer_id = str(metadata.get("anyaicam_customer_id") or "")
    if kind not in {APPLIANCE_ORDER, OWN_PC_ORDER} or not customer_id:
        return {"status": "ignored"}
    if session.get("payment_status") not in {"paid", "no_payment_required"}:
        return {"status": "not_granted", "reason": "checkout payment is incomplete"}
    if not row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        return {"status": "ignored", "reason": "unknown account"}
    plan_key = str(metadata.get("anyaicam_plan_key") or "")
    try:
        cameras = int(metadata.get("anyaicam_camera_quantity") or 0)
    except (TypeError, ValueError):
        cameras = 0
    if plan_key not in billing.PLANS or not billing.MIN_CAMERA_QUANTITY <= cameras <= billing.MAX_CAMERA_QUANTITY:
        return {"status": "rejected", "reason": "order metadata is not a valid plan and camera count"}
    if kind == APPLIANCE_ORDER:
        result = _record_appliance_order(event, session, customer_id, plan_key, cameras)
    else:
        result = _grant_vms_license(session, customer_id, metadata)
        if str(session.get("mode") or "") == "payment" and result.get("status") == "vms_license_granted":
            # Owner billing rule (2026-10-22): the plan chosen with the licence is saved with its approved Price and
            # starts at service activation -- at once when this account's AnyAiCam is already activated.
            result["deferred_plan"] = record_deferred_plan(
                customer_id, order_kind=OWN_PC_ORDER, plan_key=plan_key, cameras=cameras,
                stripe_customer_id=str(session.get("customer") or "") or None, payment_method_id=_payment_method(session),
                checkout_session_id=str(session.get("id") or ""),
                price_id=str(metadata.get("anyaicam_plan_price_id") or "") or None,
                monthly_cents=_int_or_none(metadata.get("anyaicam_plan_monthly_cents")))
            result["plan_start"] = on_appliance_activated(customer_id).get("status")
    try:  # the order confirmation (outbox, retried by the worker); never fails the step
        import order_funnel
        import stripe_mode
        order_funnel.send_order_confirmation(customer_id, test_mode=stripe_mode.is_test_mode(event=event))
    except Exception:
        logger.exception("build_order.confirmation_not_queued")
    return result


def _record_appliance_order(event: dict, session: dict, customer_id: str, plan_key: str, cameras: int) -> dict:
    import hardware_orders
    if str(session.get("mode") or "") != "payment":
        return {"status": "rejected", "reason": "an appliance order is a one-time payment"}
    catalog = _hardware_catalog()
    items = []
    for part in str((session.get("metadata") or {}).get("anyaicam_hardware_items") or "").split(","):
        sku, _, quantity = part.partition(":")
        if sku in catalog and quantity.isdigit() and int(quantity) >= 1:
            items.append((sku, int(quantity)))
    if not items or not items[0][0].startswith("AIC-APPLIANCE-"):
        return {"status": "rejected", "reason": "no appliance in this order"}
    amounts = _line_amounts(session)
    intent = session.get("payment_intent")
    intent_id = str((intent.get("id") if isinstance(intent, dict) else intent) or "") or None
    stripe_customer = str(session.get("customer") or "") or None
    orders = []
    for sku, quantity in items:
        entry = catalog[sku]
        price_id = os.environ.get(entry["env"], "").strip()
        charged = amounts.get(price_id)
        order = hardware_orders.upsert_order(
            customer_id=customer_id, sku=sku, product_name=entry["name"], stripe_price_id=price_id or "unconfigured",
            quantity=quantity, amount_cents=charged if charged is not None else entry["cents"] * quantity,
            stripe_checkout_session_id=str(session.get("id") or "") or None, stripe_payment_intent_id=intent_id,
            stripe_customer_id=stripe_customer, status="paid")
        orders.append(order["id"])
    record_deferred_plan(customer_id, order_kind=APPLIANCE_ORDER, plan_key=plan_key, cameras=cameras,
                         stripe_customer_id=stripe_customer, payment_method_id=_payment_method(session),
                         checkout_session_id=str(session.get("id") or ""))
    return {"status": "appliance_order_recorded", "order_ids": orders}


def _grant_vms_license(session: dict, customer_id: str, metadata: dict) -> dict:
    from customer_entitlements import upsert_entitlement
    from pricing_catalog import VMS_LICENSE_PRODUCT, VMS_LICENSES
    price_id = str(metadata.get("anyaicam_vms_license_price_id") or "")
    try:
        capacity = int(metadata.get("anyaicam_vms_license_capacity") or 0)
    except (TypeError, ValueError):
        capacity = 0
    from pricing_catalog import vms_license_price_ids
    tier = next((t for t in VMS_LICENSES if t[0] == capacity), None)
    # The tier's current or an earlier Price (2026-10-17 repricing: a checkout opened before the switch still grants).
    if not tier or not price_id or price_id not in vms_license_price_ids(tier[2]):
        return {"status": "rejected", "reason": "license metadata does not match the configured license price"}
    # F1 (2026-10-21): the licence is paid on the subscription's first invoice. A full refund or dispute of that
    # payment suspends it (billing_status._reverse_initial_licence); one recorded BEFORE this grant is honoured here,
    # under the same per-payment lock, and a redelivered checkout event never re-activates a reversed licence.
    import billing_status
    import stripe_state
    from partner_db import transaction
    invoice = session.get("invoice")
    invoice_id = str((invoice.get("id") if isinstance(invoice, dict) else invoice) or "")
    session_id = str(session.get("id") or "") or None
    # RC10b (Codex review of RC10 99a19591): the payment (PaymentIntent and charge) of the first invoice, read before the
    # lock, so a reversal recorded by PaymentIntent or charge only -- e.g. before this fix, when Stripe could not name
    # its invoice -- is honoured too, and the licence row records its payment from the start (a later refund finds it
    # without a Checkout Session lookup). Stripe unreadable: granted on the invoice check alone; a refund of this
    # payment is always recorded with its invoice now (billing_status._stripe_invoice_for_payment retries otherwise)
    # and binds the licence through its Checkout Session.
    intent, charge = "", ""
    if str(session.get("mode") or "") == "payment":
        # Owner billing rule (2026-10-22): the licence alone, paid by this Checkout's PaymentIntent (no invoice).
        paid_by = session.get("payment_intent")
        intent = str((paid_by.get("id") if isinstance(paid_by, dict) else paid_by) or "")
    else:
        try:
            intent, charge = billing_status.invoice_payment(invoice_id)
        except stripe_state.RetryableStripeEventError as error:
            logger.warning("build_order.licence_payment_unread session=%s invoice=%s error=%s", session_id, invoice_id, error)
    with transaction() as db:
        # The same per-payment keys a refund or dispute of this payment takes (billing_status.lock_payment orders them
        # globally): whichever commits first, the other sees it.
        billing_status.lock_payment(db, billing_status.payment_keys(charge, intent, invoice_id))
        reason = billing_status.reversal_reason(charge, intent, invoice_id)
        invoice_reversal = billing_status.reversal_for_invoice(invoice_id) or {}
        existing = row("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (customer_id, VMS_LICENSE_PRODUCT))
        same_order = bool(existing and session_id and existing.get("stripe_checkout_session_id") == session_id)
        reversed_before = bool(same_order and existing.get("status") == "suspended"
                               and existing.get("suspended_reason") in ("refunded", "disputed"))
        if reason and existing and not same_order and existing.get("status") == "active":
            # A late event for an older, reversed order never suspends the licence another (paid) order holds.
            return {"status": "vms_license_not_granted", "entitlement_id": existing.get("id"),
                    "reason": f"the payment was {reason}; the account's licence belongs to another order"}
        entitlement = upsert_entitlement(customer_id=customer_id, product=VMS_LICENSE_PRODUCT, camera_slot_quantity=capacity,
                                         status="suspended" if (reason or reversed_before) else "active",
                                         stripe_customer_id=str(session.get("customer") or "") or None,
                                         stripe_checkout_session_id=session_id,
                                         stripe_price_id=price_id)
        paid_with = intent or invoice_reversal.get("payment_intent") or None
        if same_order:
            db.execute("UPDATE customer_entitlements SET stripe_payment_intent_id=COALESCE(?,stripe_payment_intent_id),"
                       "suspended_reason=COALESCE(?,suspended_reason) WHERE id=?", (paid_with, reason, entitlement["id"]))
        else:
            # A new order (first grant, or a second own-PC order after an earlier one): the row records THIS order's
            # payment and reason, never the previous order's PaymentIntent (which would hide this order from a refund).
            db.execute("UPDATE customer_entitlements SET stripe_payment_intent_id=?,suspended_reason=? WHERE id=?",
                       (paid_with, reason, entitlement["id"]))
    if reason or reversed_before:
        return {"status": "vms_license_not_granted", "entitlement_id": entitlement.get("id"),
                "reason": f"the payment was {reason or existing['suspended_reason']}"}
    return {"status": "vms_license_granted", "entitlement_id": entitlement.get("id"), "capacity": capacity}


# ------------------------------------------------------------------ activation

def _appliance_activated(customer_id: str) -> bool:
    """Activated for billing: an appliance that went through an activation
    event -- token activation (activation_status='activated',
    appliance_cloud.activate()), or, after claim-flow activation shipped, the
    claim flow's recorded activation (activated_at on a row claim/complete
    marked activated_via='claim', appliance_claims.record_claim_activation())
    -- AND is still in service right now by appliance_readiness (the single
    source of truth: not revoked or released, a working credential; a
    claimed one also really enrolled). Revocation leaves activation_status
    and activated_at as they were, so neither alone is enough (Codex review
    of fb3c50f). An appliance claimed before that change has no marker and
    never counts here: its account's start waits for an explicit owner/admin
    decision instead (STORAGE_HELD)."""
    from appliance_readiness import appliance_activated
    candidates = rows("SELECT id,activation_status,state FROM appliances WHERE customer_id=? AND (activation_status='activated' OR "
                      "(activated_via='claim' AND activated_at IS NOT NULL))", (customer_id,))
    return any(appliance_activated(candidate) for candidate in candidates)


def service_activated(customer_id: str) -> bool:
    """Owner billing rule (2026-10-22): the account's AnyAiCam service is activated -- an appliance or own PC of the
    account that is activated and in service (appliance_readiness, which also counts appliances claimed before the
    2026-10-11 activation marker; Red review M2). Gates purchases the customer makes themselves (My subscription, a
    plan-only order). Automatic off-session starts keep the stricter _appliance_activated."""
    from appliance_readiness import customer_has_activated_appliance
    return customer_has_activated_appliance(customer_id)


def _start_allowed(customer_id: str, plan: dict) -> bool:
    """May this account's storage start reach Stripe now? An activation
    event on an appliance still in service (_appliance_activated), or an
    owner/admin approval (admin_start_held_storage) while an appliance on the
    account is still in service (appliance_readiness). Checked before the
    start is claimed and again right before each Stripe request."""
    if _appliance_activated(customer_id):
        return True
    if plan.get("owner_approved_at"):
        from appliance_readiness import customer_has_activated_appliance
        return customer_has_activated_appliance(customer_id)
    return False


def _hardware_still_paid(plan: dict) -> bool:
    order = row("SELECT id,stripe_payment_intent_id FROM hardware_orders WHERE customer_id=? AND stripe_checkout_session_id=? "
                "AND sku LIKE 'AIC-APPLIANCE-%' AND status='paid' LIMIT 1", (plan["customer_id"], plan["checkout_session_id"]))
    if not order:
        return False
    # Owner billing rule 5 (Red review H1): a refund or dispute recorded before the order itself was recorded leaves the
    # order 'paid' (nothing to suspend yet); its payment's reversal record still counts.
    import billing_status
    return billing_status.reversal_reason("", str(order.get("stripe_payment_intent_id") or ""), "") is None


def _idempotency_key(plan: dict) -> str:
    return f"anyaicam-storage-start-{plan['customer_id']}-{plan['checkout_session_id']}-{int(plan['attempt_round'] or 0)}"


def _claim(customer_id: str, *, manual: bool) -> dict | None:
    """Atomically take the start for this account; None if another start
    is running, it already started, or it is not startable now."""
    states = (STORAGE_AWAITING, STORAGE_FAILED) + ((STORAGE_PAYMENT_FAILED,) if manual else ())
    stale = (datetime.now() - timedelta(seconds=STARTING_STALE_SECONDS)).isoformat()
    now = datetime.now().isoformat()
    placeholders = ",".join("?" * len(states))
    with connection() as db:
        # A manual retry after a declined card is a new Stripe request (new key).
        if manual:
            db.execute("UPDATE deferred_storage_plans SET attempt_round=attempt_round+1 WHERE customer_id=? AND state=?",
                       (customer_id, STORAGE_PAYMENT_FAILED))
        claimed = db.execute(
            f"UPDATE deferred_storage_plans SET state=?,attempts=attempts+1,last_error=NULL,updated_at=? WHERE customer_id=? "
            f"AND (state IN ({placeholders}) OR (state=? AND updated_at<?))",
            (STORAGE_STARTING, now, customer_id, *states, STORAGE_STARTING, stale)).rowcount
    return storage_plan(customer_id) if claimed == 1 else None


def _finish(customer_id: str, state: str, *, subscription_id: str | None = None, error: str | None = None) -> None:
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute("UPDATE deferred_storage_plans SET state=?,stripe_subscription_id=COALESCE(?,stripe_subscription_id),"
                   "last_error=?,started_at=CASE WHEN ?='started' THEN ? ELSE started_at END,updated_at=? WHERE customer_id=?",
                   (state, subscription_id, error, state, now, now, customer_id))


def _existing_subscription(plan: dict) -> dict | None:
    """A subscription Stripe already made for this order (a lost answer)."""
    import main
    listing = main.stripe_api_get(f"/v1/subscriptions?customer={plan['stripe_customer_id']}&status=all&limit=100")
    prefix = f"anyaicam-storage-start-{plan['customer_id']}-{plan['checkout_session_id']}-"
    for subscription in listing.get("data") or []:
        marker = str((subscription.get("metadata") or {}).get("anyaicam_storage_start") or "")
        if marker.startswith(prefix) and subscription.get("status") not in ("canceled", "incomplete_expired"):
            return subscription
    return None


def _default_payment_method(plan: dict, *, manual: bool = False) -> str | None:
    import main
    if manual:
        # Red review M1: after a declined first payment the customer updates the card in Manage billing (the Stripe
        # customer's default); the retry uses that card, not the one saved at purchase.
        try:
            customer = main.stripe_api_get(f"/v1/customers/{plan['stripe_customer_id']}")
            chosen = (customer.get("invoice_settings") or {}).get("default_payment_method")
            chosen = chosen.get("id") if isinstance(chosen, dict) else chosen
            if chosen:
                return str(chosen)
        except Exception:
            logger.info("build_order.default_card_unread customer=%s", plan.get("stripe_customer_id"))
    if plan.get("stripe_payment_method_id"):
        return plan["stripe_payment_method_id"]
    listing = main.stripe_api_get(f"/v1/payment_methods?customer={plan['stripe_customer_id']}&type=card&limit=1")
    found = (listing.get("data") or [None])[0]
    return str(found["id"]) if found and found.get("id") else None


def start_storage_plan(customer_id: str, *, manual: bool = False) -> dict:
    """Start the appliance customer's storage plan on Stripe once the
    appliance is activated. Exactly one subscription per order, whatever
    the number of calls (activation, worker retries, duplicate events)."""
    plan = storage_plan(customer_id)
    if not plan or plan["state"] in (STORAGE_STARTED, STORAGE_SUPERSEDED):
        return {"status": "ignored", "reason": "nothing to start"}
    if plan["state"] == STORAGE_HELD:
        return {"status": "ignored", "reason": "held for an owner decision"}
    # An owner/admin-approved start (admin_start_held_storage) is its own
    # activation signal, so a transient failure after approval is retried --
    # but, like every start, only while an appliance is still in service.
    if not _start_allowed(customer_id, plan):
        return {"status": "ignored", "reason": "appliance not activated"}
    if billing.has_camera_plan(customer_id):
        _finish(customer_id, STORAGE_SUPERSEDED)
        return {"status": "superseded"}
    if not _order_still_paid(plan):
        return {"status": "ignored", "reason": "the purchase is no longer paid (refunded or disputed)"}
    plan = _claim(customer_id, manual=manual)
    if not plan:
        return {"status": "busy_or_done"}
    import main
    import stripe_state
    key = _idempotency_key(plan)

    def still_allowed() -> bool:
        # Re-checked right before each Stripe request: the appliance may have
        # been revoked or released since the start was claimed.
        if not _start_allowed(customer_id, plan):
            _finish(customer_id, STORAGE_AWAITING, error="no appliance in service")
        elif not _order_still_paid(plan):  # owner rule 5: refunded or disputed since the start was claimed
            _finish(customer_id, STORAGE_AWAITING, error="the purchase is no longer paid (refunded or disputed)")
        else:
            return True
        logger.warning("build_order.storage_start_skipped_not_in_service customer_id=%s", customer_id)
        return False
    if not still_allowed():
        return {"status": "ignored", "reason": "appliance not activated"}
    try:
        subscription = _existing_subscription(plan)
        if not subscription:
            import friends_family
            try:
                price_id, cents = _locked_price(plan)
            except SavedPriceError as error:
                _finish(customer_id, STORAGE_HELD, error=str(error)[:300])
                logger.error("build_order.storage_start_held_price customer_id=%s reason=%s", customer_id, error)
                return {"status": STORAGE_HELD, "reason": "the saved plan price is no longer configured"}
            try:
                main.require_stripe_price_matches_catalog(price_id, cents, "month")
            except HTTPException as refused_price:  # Red review L2: e.g. the saved Price was archived
                _finish(customer_id, STORAGE_HELD, error=f"saved price refused by the catalog check: {refused_price.detail}"[:300])
                logger.error("build_order.storage_start_held_catalog customer_id=%s", customer_id)
                return {"status": STORAGE_HELD, "reason": "the saved plan price failed the catalog check"}
            quantity = str(int(plan["camera_quantity"]))
            fields = [("customer", plan["stripe_customer_id"]), ("items[0][price]", price_id), ("items[0][quantity]", quantity),
                      ("off_session", "true"), ("payment_behavior", "error_if_incomplete"),
                      ("metadata[anyaicam_billing_version]", "2"), ("metadata[anyaicam_customer_id]", customer_id),
                      ("metadata[anyaicam_plan_key]", plan["plan_key"]), ("metadata[anyaicam_camera_quantity]", quantity),
                      ("metadata[anyaicam_stripe_price_id]", price_id), ("metadata[anyaicam_storage_start]", key)]
            import storage_trial
            trial = storage_trial.subscription_trial_fields(customer_id, checkout=False, plan_key=plan["plan_key"])
            # A free-trial subscription gets no card attached: it ends at the
            # trial end unless the customer explicitly chooses paid storage.
            method = None if trial else _default_payment_method(plan, manual=manual)
            if method:
                fields.append(("default_payment_method", method))
            fields += trial
            coupon = friends_family.checkout_discount(customer_id, "base").get("coupon")
            if coupon:
                fields.append(("discounts[0][coupon]", coupon))
            if not still_allowed():
                return {"status": "ignored", "reason": "appliance not activated"}
            subscription = main.stripe_api_post("/v1/subscriptions", fields, idempotency_key=key)
    except Exception as error:
        refused = getattr(error, "stripe_outcome", None) == "rejected"
        _finish(customer_id, STORAGE_PAYMENT_FAILED if refused else STORAGE_FAILED, error=str(error)[:300])
        logger.warning("build_order.storage_start_failed customer_id=%s refused=%s", customer_id, refused)
        return {"status": STORAGE_PAYMENT_FAILED if refused else STORAGE_FAILED}
    subscription_id = str(subscription.get("id") or "")
    _finish(customer_id, STORAGE_STARTED, subscription_id=subscription_id)
    if not _order_still_paid(plan):  # a refund/dispute recorded while Stripe created the subscription (Red review L1)
        logger.error("build_order.storage_started_after_reversal customer_id=%s subscription=%s", customer_id, subscription_id)
        import json
        now = datetime.now().isoformat()
        with connection() as db:
            db.execute("UPDATE deferred_storage_plans SET hold_reason=?,held_at=? WHERE customer_id=?",
                       ("started while its purchase was refunded or disputed: owner review", now, customer_id))
            db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", ("", "system", "deferred_plan.started_after_reversal", "deferred_storage_plan",
                                                 customer_id, json.dumps({"subscription_id": subscription_id}), now))
    trial_id = str((subscription.get("metadata") or {}).get("anyaicam_storage_trial") or "")
    if trial_id:
        import storage_trial
        storage_trial.link_subscription(trial_id, customer_id, subscription_id)
    try:  # the webhook applies it too; this only makes capacity appear at once
        current = stripe_state.current_subscription(subscription_id)
        if current:
            billing._upsert_current(current)
    except Exception:
        logger.info("build_order.storage_entitlement_waits_for_webhook customer_id=%s", customer_id)
    return {"status": STORAGE_STARTED, "subscription_id": subscription_id}


def on_appliance_activated(customer_id: str | None) -> dict:
    """Called after an appliance activation; never raises (the activation
    itself must succeed -- a failed start is retried by the worker)."""
    if not customer_id:
        return {"status": "ignored"}
    try:
        return start_storage_plan(str(customer_id))
    except Exception:
        logger.exception("build_order.storage_start_error")
        return {"status": "error"}


def retry_storage_starts(limit: int = 50) -> int:
    """Billing-worker pass: start plans for activated appliances whose start
    has not happened or failed transiently. Declined cards wait for the
    customer's own retry. Held plans (STORAGE_HELD) are never selected.
    First records the activation of any claim-marked appliance that has
    enrolled since (its heartbeat normally does this at once)."""
    try:
        import appliance_claims
        appliance_claims.record_pending_claim_activations(limit)
    except Exception:
        logger.exception("build_order.claim_activation_sweep_failed")
    stale = (datetime.now() - timedelta(seconds=STARTING_STALE_SECONDS)).isoformat()
    started = 0
    for plan in rows("SELECT customer_id FROM deferred_storage_plans WHERE state IN (?,?) OR (state=? AND updated_at<?) LIMIT ?",
                     (STORAGE_AWAITING, STORAGE_FAILED, STORAGE_STARTING, stale, limit)):
        try:
            if start_storage_plan(plan["customer_id"]).get("status") == STORAGE_STARTED:
                started += 1
        except Exception:
            continue
    return started


# ------------------------------------------------------------------ owner decision

def held_storage_starts() -> list[dict]:
    """Storage starts waiting for an owner/admin decision (STORAGE_HELD), and
    approved ones that have not started yet, with what the decision needs:
    the order, the claimed appliances and whether one has really enrolled."""
    from appliance_readiness import customer_has_activated_appliance
    found = rows("SELECT p.*, c.name AS customer_name, c.email AS customer_email FROM deferred_storage_plans p "
                 "LEFT JOIN customers c ON c.id=p.customer_id WHERE p.state=? OR (p.owner_approved_at IS NOT NULL AND p.state!=?) "
                 "ORDER BY p.created_at", (STORAGE_HELD, STORAGE_STARTED))
    out = []
    for plan in found:
        appliances = rows("SELECT a.id,a.cloud_id,a.activation_status,a.activated_via,a.last_check_in,cl.completed_at AS claimed_at "
                          "FROM appliances a LEFT JOIN appliance_claims cl ON cl.appliance_id=a.id AND cl.status='completed' "
                          "WHERE a.customer_id=? ORDER BY a.created_at", (plan["customer_id"],))
        out.append({
            "customer_id": plan["customer_id"], "customer_name": plan.get("customer_name"),
            "customer_email": plan.get("customer_email"), "plan_key": plan["plan_key"],
            "camera_quantity": int(plan["camera_quantity"]), "state": plan["state"],
            "hold_reason": plan.get("hold_reason"), "held_at": plan.get("held_at"),
            "owner_approved_at": plan.get("owner_approved_at"), "owner_approved_by": plan.get("owner_approved_by"),
            "checkout_session_id": plan["checkout_session_id"], "ordered_at": plan["created_at"],
            "hardware_still_paid": _order_still_paid(plan),
            "appliance_enrolled": customer_has_activated_appliance(plan["customer_id"]),
            "appliances": appliances,
        })
    return out


def _audit_storage(db, actor: dict, action: str, customer_id: str, details: dict) -> None:
    import json
    db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
               "VALUES(?,?,?,?,?,?,?)", (actor.get("email", ""), actor.get("role", ""), action, "deferred_storage_plan",
                                          customer_id, json.dumps(details), datetime.now().isoformat()))


def admin_start_held_storage(customer_id: str, actor: dict) -> dict:
    """The owner/admin's explicit 'start storage now' for one account's held
    order (/admin/storage-starts). The approval and its audit record are one
    transaction; the start itself is the same idempotent path activation
    uses (one claimed row, fixed Idempotency-Key per round, adoption of a
    subscription Stripe already made), so repeating the action never makes a
    second subscription, and a transient Stripe error is retried by the
    billing worker. Only an order whose appliance has really enrolled."""
    from appliance_readiness import customer_has_activated_appliance
    plan = storage_plan(customer_id)
    if not plan:
        raise HTTPException(status_code=404, detail="No storage plan is recorded for this account.")
    if plan["state"] == STORAGE_STARTED:
        return {"status": "unchanged", "state": STORAGE_STARTED, "subscription_id": plan.get("stripe_subscription_id")}
    if plan["state"] != STORAGE_HELD and not plan.get("owner_approved_at"):
        raise HTTPException(status_code=409, detail="This storage plan is not waiting for an owner decision.")
    if not _order_still_paid(plan):
        raise HTTPException(status_code=409, detail="The appliance order is no longer paid (refunded or disputed).")
    if not customer_has_activated_appliance(customer_id):
        raise HTTPException(status_code=409, detail="No appliance on this account has finished enrolling yet.")
    now = datetime.now().isoformat()
    with connection() as db:
        approved = db.execute("UPDATE deferred_storage_plans SET state=?,owner_approved_at=?,owner_approved_by=?,updated_at=? "
                              "WHERE customer_id=? AND state=?",
                              (STORAGE_AWAITING, now, actor.get("email", ""), now, customer_id, STORAGE_HELD)).rowcount
        if approved == 1:
            _audit_storage(db, actor, "storage_start.owner_approved", customer_id,
                           {"plan_key": plan["plan_key"], "camera_quantity": int(plan["camera_quantity"]),
                            "checkout_session_id": plan["checkout_session_id"], "hold_reason": plan.get("hold_reason")})
    current = storage_plan(customer_id) or plan
    if not current.get("owner_approved_at"):
        raise HTTPException(status_code=409, detail="This storage plan is not waiting for an owner decision.")
    result = start_storage_plan(customer_id, manual=current["state"] == STORAGE_PAYMENT_FAILED)
    with connection() as db:
        _audit_storage(db, actor, "storage_start.admin_start", customer_id, {"result": result.get("status")})
    latest = storage_plan(customer_id) or current
    return {"status": result.get("status"), "state": latest["state"], "subscription_id": latest.get("stripe_subscription_id")}


def register_build_order_routes(app: FastAPI) -> None:
    from partner_portal import partner_identity

    def owner(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        return identity

    @app.post("/api/v2/customer/build-order/checkout")
    async def build_order_checkout(request: Request) -> dict:
        # The order is the account's saved Build Your System selection; the
        # browser sends nothing that could set a product, price or Price ID.
        body = await request.body()
        if body.strip() not in (b"", b"{}"):
            raise HTTPException(status_code=422, detail="This request takes no parameters.")
        return create_checkout(owner(request))

    @app.post("/api/v2/customer/storage/start")
    def retry_storage_start(request: Request) -> dict:
        identity = owner(request)
        plan = storage_plan(identity["customer_id"])
        if not plan or plan["state"] not in (STORAGE_PAYMENT_FAILED, STORAGE_FAILED):
            raise HTTPException(status_code=409, detail="There is no storage plan waiting to be retried.")
        result = start_storage_plan(identity["customer_id"], manual=True)
        if result.get("status") != STORAGE_STARTED:
            raise HTTPException(status_code=402, detail="The storage plan could not be started. Update your payment method in "
                                                        "Manage billing on My subscription, then try again.")
        return {"status": "started", "message": "Your storage plan has started."}
