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
    starts_today = kind != APPLIANCE_ORDER
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
        if quote["kind"] == OWN_PC_ORDER:
            license_line = quote["one_time"][0]
            license_price = _configured(vms_license_for(cameras)["env"], "the VMS software license")
            main.require_stripe_price_matches_catalog(license_price, license_line["cents"], None)
            # A one-time price in a subscription Checkout is charged once, on the first invoice.
            fields += [("line_items[1][price]", license_price), ("line_items[1][quantity]", "1"),
                       ("metadata[anyaicam_vms_license_price_id]", license_price),
                       ("metadata[anyaicam_vms_license_capacity]", str(license_line["capacity"]))]
            # The one-time license is never discounted (Friends & Family: 50%
            # of the recurring plan only). A session-level coupon would cover
            # every line, so the coupon must be verified on Stripe as limited
            # to the plan's Product; otherwise checkout is refused (fail
            # closed). Promotion codes cannot be scoped the same way, so this
            # mixed order does not accept them.
            discount = friends_family.checkout_discount(customer_id, "base")
            if discount["coupon"]:
                try:
                    friends_family.verify_coupon_scope(discount["coupon"], eligible_price_ids=[plan_price],
                                                       excluded_price_ids=[license_price])
                except friends_family.CouponScopeError as error:
                    logger.error("build_order.friends_family_coupon_scope customer_id=%s reason=%s", customer_id, error)
                    raise HTTPException(status_code=503, detail="Your Friends & Family pricing can't be applied to this order "
                                                                "safely right now, so checkout is paused. Please contact AnyAiCam support.")
                fields.append(("discounts[0][coupon]", discount["coupon"]))
            else:
                fields.append(("allow_promotion_codes", "false"))
            fields.append(("metadata[anyaicam_friends_family]", "approved" if discount["friends_family"] else "none"))
        else:
            friends_family.apply_to_checkout_fields(fields, customer_id, "base")
        primary_price, primary_quantity = plan_price, cameras
    fields.append(("customer", checkout_guard.canonical_stripe_customer(customer_id, email=email)))
    return fields, primary_price, primary_quantity


def create_checkout(identity: dict) -> dict:
    import checkout_guard
    import friends_family
    customer_id = identity["customer_id"]
    if billing.has_camera_plan(customer_id) or storage_plan(customer_id):
        raise HTTPException(status_code=409, detail="This account already has a camera plan or a paid order.")
    progress = billing.build_progress(customer_id)
    if not progress or progress["state"] != billing.BUILD_PENDING or not progress.get("plan"):
        raise HTTPException(status_code=409, detail="There is no order to check out. Build your system first.")
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
    result = (_record_appliance_order(event, session, customer_id, plan_key, cameras) if kind == APPLIANCE_ORDER
              else _grant_vms_license(session, customer_id, metadata))
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
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute("INSERT INTO deferred_storage_plans(customer_id,plan_key,camera_quantity,stripe_customer_id,"
                   "stripe_payment_method_id,checkout_session_id,state,attempt_round,attempts,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,0,0,?,?) ON CONFLICT(customer_id) DO NOTHING",
                   (customer_id, plan_key, cameras, stripe_customer, _payment_method(session), str(session.get("id") or ""),
                    STORAGE_AWAITING, now, now))
    return {"status": "appliance_order_recorded", "order_ids": orders}


def _grant_vms_license(session: dict, customer_id: str, metadata: dict) -> dict:
    from customer_entitlements import upsert_entitlement
    from pricing_catalog import VMS_LICENSE_PRODUCT, VMS_LICENSES
    price_id = str(metadata.get("anyaicam_vms_license_price_id") or "")
    try:
        capacity = int(metadata.get("anyaicam_vms_license_capacity") or 0)
    except (TypeError, ValueError):
        capacity = 0
    tier = next((t for t in VMS_LICENSES if t[0] == capacity), None)
    if not tier or not price_id or os.environ.get(tier[2], "").strip() != price_id:
        return {"status": "rejected", "reason": "license metadata does not match the configured license price"}
    entitlement = upsert_entitlement(customer_id=customer_id, product=VMS_LICENSE_PRODUCT, camera_slot_quantity=capacity,
                                     stripe_customer_id=str(session.get("customer") or "") or None,
                                     stripe_checkout_session_id=str(session.get("id") or "") or None,
                                     stripe_price_id=price_id)
    return {"status": "vms_license_granted", "entitlement_id": entitlement.get("id"), "capacity": capacity}


# ------------------------------------------------------------------ activation

def _appliance_activated(customer_id: str) -> bool:
    return bool(row("SELECT id FROM appliances WHERE customer_id=? AND activation_status='activated' LIMIT 1", (customer_id,)))


def _hardware_still_paid(plan: dict) -> bool:
    return bool(row("SELECT id FROM hardware_orders WHERE customer_id=? AND stripe_checkout_session_id=? "
                    "AND sku LIKE 'AIC-APPLIANCE-%' AND status='paid' LIMIT 1",
                    (plan["customer_id"], plan["checkout_session_id"])))


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


def _default_payment_method(plan: dict) -> str | None:
    if plan.get("stripe_payment_method_id"):
        return plan["stripe_payment_method_id"]
    import main
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
    if not _appliance_activated(customer_id):
        return {"status": "ignored", "reason": "appliance not activated"}
    if billing.has_camera_plan(customer_id):
        _finish(customer_id, STORAGE_SUPERSEDED)
        return {"status": "superseded"}
    if not _hardware_still_paid(plan):
        return {"status": "ignored", "reason": "the appliance order is no longer paid (refunded or disputed)"}
    plan = _claim(customer_id, manual=manual)
    if not plan:
        return {"status": "busy_or_done"}
    import main
    import stripe_state
    key = _idempotency_key(plan)
    try:
        subscription = _existing_subscription(plan)
        if not subscription:
            import friends_family
            price_id = billing.price_id_for(plan["plan_key"])
            if not price_id:
                raise RuntimeError(f"no Stripe Price is configured for {plan['plan_key']}")
            main.require_stripe_price_matches_catalog(price_id, billing.PLANS[plan["plan_key"]]["monthly_cents_per_camera"], "month")
            quantity = str(int(plan["camera_quantity"]))
            fields = [("customer", plan["stripe_customer_id"]), ("items[0][price]", price_id), ("items[0][quantity]", quantity),
                      ("off_session", "true"), ("payment_behavior", "error_if_incomplete"),
                      ("metadata[anyaicam_billing_version]", "2"), ("metadata[anyaicam_customer_id]", customer_id),
                      ("metadata[anyaicam_plan_key]", plan["plan_key"]), ("metadata[anyaicam_camera_quantity]", quantity),
                      ("metadata[anyaicam_stripe_price_id]", price_id), ("metadata[anyaicam_storage_start]", key)]
            method = _default_payment_method(plan)
            if method:
                fields.append(("default_payment_method", method))
            coupon = friends_family.checkout_discount(customer_id, "base").get("coupon")
            if coupon:
                fields.append(("discounts[0][coupon]", coupon))
            subscription = main.stripe_api_post("/v1/subscriptions", fields, idempotency_key=key)
    except Exception as error:
        refused = getattr(error, "stripe_outcome", None) == "rejected"
        _finish(customer_id, STORAGE_PAYMENT_FAILED if refused else STORAGE_FAILED, error=str(error)[:300])
        logger.warning("build_order.storage_start_failed customer_id=%s refused=%s", customer_id, refused)
        return {"status": STORAGE_PAYMENT_FAILED if refused else STORAGE_FAILED}
    subscription_id = str(subscription.get("id") or "")
    _finish(customer_id, STORAGE_STARTED, subscription_id=subscription_id)
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
    customer's own retry."""
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
