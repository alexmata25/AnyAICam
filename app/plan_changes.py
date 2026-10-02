"""Local -> Hybrid upgrade (2026-10-02).

Owner direction: Local -> Hybrid is an upgrade/migration, not a second base
subscription. The customer ends with ONE active base plan, Hybrid, on the
SAME Stripe subscription -- same Stripe customer, payment method, discount
(Friends & Family coupons stay on the subscription), invoices, attribution
and history. Previously "Upgrade to Hybrid" started a new Hybrid checkout and
left the Local subscription billing: double billing and, until 2026-10-02,
double camera capacity.

How it works: the owner asks; the server checks the account's active Local
plan and its Stripe subscription as Stripe has it NOW (same customer, same
account, still active), checks the Hybrid price against the published
catalog, then changes that subscription's item from the Local price to the
Hybrid price of the same camera tier, and applies Stripe's answer (Hybrid
active, Local superseded on that subscription). The webhook that follows is
idempotent with this.

Owner decision (2026-10-02): the upgrade takes effect immediately, Stripe
prorates the price difference for the rest of the current period and
invoices it now (proration_behavior=always_invoice), and the renewal date is
unchanged (billing_cycle_anchor=unchanged).

payment_behavior=pending_if_incomplete: Stripe applies the new price only if
that immediate proration payment succeeds. If it does not, nothing changes --
the customer stays on Local and is told the payment did not go through. (Not
a grace policy: Hybrid is simply never granted on an unpaid upgrade.)

Repeated or concurrent requests reach Stripe with the same Idempotency-Key
(per subscription and price change), and a request that finds the
subscription already on Hybrid sends nothing: one change, one charge.
"""
from __future__ import annotations

import os
import sys

from fastapi import FastAPI, HTTPException, Request

from partner_db import audit

UPGRADE_PRORATION_BEHAVIOR = "always_invoice"  # owner, 2026-10-02: prorate and invoice now
UPGRADE_BILLING_CYCLE_ANCHOR = "unchanged"     # owner, 2026-10-02: renewal date unchanged
UPGRADE_PAYMENT_BEHAVIOR = "pending_if_incomplete"


def upgrade_available() -> bool:
    """The upgrade is offered (button shown) whenever a Hybrid price exists
    for the customer's tier -- checked by the page; the policy is decided."""
    return True


def _hybrid_tier_for(camera_slots: int):
    from customer_entitlements import PLAN_TIERS
    return next((t for t in PLAN_TIERS if t[0] == "hybrid" and t[4] == camera_slots), None)


def upgrade_to_hybrid(identity: dict) -> dict:
    import customer_entitlements as ce
    import stripe_state
    main = sys.modules.get("main")
    customer_id = identity["customer_id"]
    held = {e["product"]: e for e in ce.get_entitlements_for_customer(customer_id) if e["status"] == "active"}
    if "camera_slots_hybrid" in held:
        return {"status": "already_hybrid", "message": "Your account is already on Hybrid."}
    local = held.get("camera_slots_local")
    if not local:
        raise HTTPException(status_code=409, detail="There is no active Local plan to upgrade.")
    subscription_id = local.get("stripe_subscription_id")
    if not subscription_id or not local.get("stripe_customer_id"):
        raise HTTPException(status_code=409, detail="This plan can't be upgraded online. Please contact AnyAiCam support.")
    tier = _hybrid_tier_for(int(local.get("camera_slot_quantity") or 0))
    if tier is None:
        raise HTTPException(status_code=409, detail="There is no Hybrid plan for this number of cameras.")
    _, tier_label, _, _, camera_slot_maximum, _, env_var, _ = tier
    hybrid_price = os.environ.get(env_var, "").strip()
    if not hybrid_price or main is None:
        raise HTTPException(status_code=503, detail="Hybrid is not available for purchase yet.")
    import pricing_catalog
    main.require_stripe_price_matches_catalog(hybrid_price, pricing_catalog.find_base_plan("hybrid", tier_label)["monthly_cents"], "month")
    try:
        current = stripe_state.current_subscription(subscription_id)
    except stripe_state.RetryableStripeEventError as error:
        raise HTTPException(status_code=502, detail="Could not reach Stripe. Please try again shortly.") from error
    if current is None:
        raise HTTPException(status_code=503, detail="Billing is not available right now.")
    meta_customer = str((current.get("metadata") or {}).get("anyaicam_customer_id") or "") or None
    if str(current.get("customer") or "") != local["stripe_customer_id"] or (meta_customer and meta_customer != customer_id) \
            or stripe_state.bound_elsewhere(local["stripe_customer_id"], customer_id):
        raise HTTPException(status_code=409, detail="This plan's billing record needs attention. Please contact AnyAiCam support.")
    if current.get("status") in ce.SUBSCRIPTION_INACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="Your Local plan has ended, so it can't be upgraded.")
    items = [item for item in ((current.get("items") or {}).get("data") or []) if isinstance(item, dict)]
    item = next((i for i in items if str((i.get("price") or {}).get("id") or "") == str(local.get("stripe_price_id") or "")), None)
    if item is None and len(items) == 1:
        item = items[0]
    if item is None or not item.get("id"):
        raise HTTPException(status_code=409, detail="This plan's billing record needs attention. Please contact AnyAiCam support.")
    if str((item.get("price") or {}).get("id") or "") != hybrid_price:  # a repeated request finds it already moved
        current = main.stripe_api_post(f"/v1/subscriptions/{subscription_id}", [
            ("items[0][id]", str(item["id"])),
            ("items[0][price]", hybrid_price),
            ("proration_behavior", UPGRADE_PRORATION_BEHAVIOR),
            ("billing_cycle_anchor", UPGRADE_BILLING_CYCLE_ANCHOR),
            ("payment_behavior", UPGRADE_PAYMENT_BEHAVIOR),
            ("metadata[anyaicam_customer_id]", customer_id),
            ("metadata[anyaicam_stripe_price_id]", hybrid_price),
            ("metadata[anyaicam_camera_slot_plan_type]", "hybrid"),
            ("metadata[anyaicam_camera_slot_maximum]", str(camera_slot_maximum)),
        ], idempotency_key=f"anyaicam-upgrade-{subscription_id}-{item['id']}-{hybrid_price}")
        moved = [i for i in ((current.get("items") or {}).get("data") or [])
                 if isinstance(i, dict) and str((i.get("price") or {}).get("id") or "") == hybrid_price]
        if not moved or current.get("pending_update"):
            # pending_if_incomplete: the proration payment did not go
            # through, so Stripe kept the subscription on Local.
            audit(identity, "customer.plan_upgrade_payment_incomplete", "customer", customer_id, {"to": "hybrid"})
            raise HTTPException(status_code=402, detail="The payment for the upgrade didn't go through, so your plan is still Local. "
                                                        "Please check your payment method and try again.")
    # Apply what Stripe now says (Hybrid active, Local superseded); the
    # webhook for the same change is idempotent with this.
    ce._sync_subscription_change({"id": f"upgrade:{subscription_id}", "type": "customer.subscription.updated",
                                  "data": {"object": current}}, cancelled=False)
    audit(identity, "customer.plan_upgraded", "customer", customer_id,
          {"from": "local", "to": "hybrid", "camera_slots": camera_slot_maximum,
           "proration_behavior": UPGRADE_PRORATION_BEHAVIOR, "billing_cycle_anchor": UPGRADE_BILLING_CYCLE_ANCHOR})
    return {"status": "upgraded", "message": f"Your plan is now Hybrid ({tier_label} cameras)."}


def register_plan_change_routes(app: FastAPI) -> None:
    from partner_portal import partner_identity

    @app.post("/api/customer/plan/upgrade-to-hybrid")
    def plan_upgrade_to_hybrid(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer sign-in required.")
        if identity.get("role") != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can change the plan.")
        return upgrade_to_hybrid(identity)


# ---------------------------------------------------------------- cancel / resume, scheduled downgrade (owner policy 2026-10-02)
# Cancel: never ends a paid period early -- the subscription is set to end at
# the end of the current paid period (cancel_at_period_end) and can be resumed
# until then. Hybrid -> Local: Hybrid stays through the paid period and Local
# starts at the next renewal (a Stripe subscription schedule: phase 1 Hybrid
# until the period end, phase 2 Local, no proration at the switch); the
# customer can keep Hybrid until then. At the switch, one base plan only
# (customer_entitlements.supersede_other_base_plans). Every action re-reads
# the subscription from Stripe, checks it belongs to this account, sends an
# Idempotency-Key, and applies Stripe's answer; repeats change nothing.

def _owner_base_plan(identity: dict) -> tuple[dict, dict]:
    import customer_entitlements as ce
    import stripe_state
    customer_id = identity["customer_id"]
    plans = [e for e in ce.get_entitlements_for_customer(customer_id)
             if e["product"] in ce.BASE_PLAN_PRODUCTS and e["status"] in ("active", "suspended")]
    plan = next((e for e in plans if e["status"] == "active"), None) or (plans[0] if plans else None)
    if not plan or not plan.get("stripe_subscription_id") or not plan.get("stripe_customer_id"):
        raise HTTPException(status_code=409, detail="There is no plan to change on this account.")
    try:
        current = stripe_state.current_subscription(plan["stripe_subscription_id"])
    except stripe_state.RetryableStripeEventError as error:
        raise HTTPException(status_code=502, detail="Could not reach Stripe. Please try again shortly.") from error
    if current is None:
        raise HTTPException(status_code=503, detail="Billing is not available right now.")
    meta_customer = str((current.get("metadata") or {}).get("anyaicam_customer_id") or "") or None
    if str(current.get("customer") or "") != plan["stripe_customer_id"] or (meta_customer and meta_customer != customer_id) \
            or stripe_state.bound_elsewhere(plan["stripe_customer_id"], customer_id):
        raise HTTPException(status_code=409, detail="This plan's billing record needs attention. Please contact AnyAiCam support.")
    if current.get("status") in ce.SUBSCRIPTION_INACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="This plan has already ended.")
    return plan, current


def _apply(current: dict) -> None:
    import customer_entitlements as ce
    ce._sync_subscription_change({"id": f"plan-change:{current.get('id')}", "type": "customer.subscription.updated",
                                  "data": {"object": current}}, cancelled=False)


def _schedule_id(subscription: dict) -> str:
    schedule = subscription.get("schedule")
    return str((schedule.get("id") if isinstance(schedule, dict) else schedule) or "")


def _release_schedule(current: dict) -> dict:
    """Drop a pending scheduled change (e.g. a downgrade) so the subscription
    simply continues; returns the subscription as Stripe has it now."""
    import stripe_state
    schedule_id = _schedule_id(current)
    if not schedule_id:
        return current
    sys.modules["main"].stripe_api_post(f"/v1/subscription_schedules/{schedule_id}/release", [],
                                        idempotency_key=f"anyaicam-release-{schedule_id}")
    return stripe_state.current_subscription(str(current["id"])) or current


def cancel_at_period_end(identity: dict) -> dict:
    import customer_entitlements as ce
    plan, current = _owner_base_plan(identity)
    if not current.get("cancel_at_period_end"):
        current = _release_schedule(current)  # a scheduled downgrade cannot outlive a cancellation
        current = sys.modules["main"].stripe_api_post(
            f"/v1/subscriptions/{current['id']}", [("cancel_at_period_end", "true")],
            idempotency_key=f"anyaicam-cancel-{current['id']}-{ce._period_end(current) or ''}")
    _record_scheduled_change(plan["id"], None, None, None)
    _apply(current)
    audit(identity, "customer.plan_cancellation_scheduled", "customer", identity["customer_id"])
    return {"status": "cancellation_scheduled", "ends_at": ce._period_end(current),
            "message": "Your plan will end at the end of the current paid period. You can keep it until then."}


def resume_renewal(identity: dict) -> dict:
    import customer_entitlements as ce
    plan, current = _owner_base_plan(identity)
    if current.get("cancel_at_period_end"):
        current = sys.modules["main"].stripe_api_post(
            f"/v1/subscriptions/{current['id']}", [("cancel_at_period_end", "false")],
            idempotency_key=f"anyaicam-resume-{current['id']}-{ce._period_end(current) or ''}")
    _apply(current)
    audit(identity, "customer.plan_renewal_resumed", "customer", identity["customer_id"])
    return {"status": "renewal_resumed", "message": "Your plan will renew as usual."}


def downgrade_to_local(identity: dict) -> dict:
    import customer_entitlements as ce
    import pricing_catalog
    plan, current = _owner_base_plan(identity)
    if plan["product"] != "camera_slots_hybrid":
        raise HTTPException(status_code=409, detail="Only a Hybrid plan can be switched to Local.")
    if current.get("cancel_at_period_end"):
        raise HTTPException(status_code=409, detail="This plan is set to end. Keep it first, then switch to Local.")
    if plan.get("scheduled_change") == "downgrade_to_local" and _schedule_id(current):
        return {"status": "already_scheduled", "effective_at": plan.get("scheduled_change_at"),
                "message": "Your switch to Local is already scheduled."}
    local_tier = next((t for t in ce.PLAN_TIERS if t[0] == "local" and t[4] == int(plan.get("camera_slot_quantity") or 0)), None)
    local_price = os.environ.get(local_tier[6], "").strip() if local_tier else ""
    if not local_price:
        raise HTTPException(status_code=409, detail="There is no Local plan for this number of cameras.")
    main = sys.modules["main"]
    main.require_stripe_price_matches_catalog(local_price, pricing_catalog.find_base_plan("local", local_tier[1])["monthly_cents"], "month")
    items = [i for i in ((current.get("items") or {}).get("data") or []) if isinstance(i, dict)]
    hybrid_price = str((items[0].get("price") or {}).get("id") or "") if items else ""
    period_end = ce._period_end(current)
    if not hybrid_price or not period_end:
        raise HTTPException(status_code=409, detail="This plan's billing record needs attention. Please contact AnyAiCam support.")
    schedule_id = _schedule_id(current)
    if not schedule_id:
        created = main.stripe_api_post("/v1/subscription_schedules", [("from_subscription", str(current["id"]))],
                                       idempotency_key=f"anyaicam-schedule-{current['id']}-{period_end}")
        schedule_id = str(created.get("id") or "")
    phase_start = current.get("current_period_start") or (items[0].get("current_period_start") if items else None)
    fields = [("end_behavior", "release"), ("proration_behavior", "none")]
    if phase_start:
        fields.append(("phases[0][start_date]", str(phase_start)))
    fields += [("phases[0][items][0][price]", hybrid_price), ("phases[0][items][0][quantity]", "1"),
               ("phases[0][end_date]", str(period_end)),
               ("phases[1][items][0][price]", local_price), ("phases[1][items][0][quantity]", "1"),
               ("phases[1][metadata][anyaicam_customer_id]", identity["customer_id"]),
               ("phases[1][metadata][anyaicam_stripe_price_id]", local_price),
               ("phases[1][metadata][anyaicam_camera_slot_plan_type]", "local")]
    main.stripe_api_post(f"/v1/subscription_schedules/{schedule_id}", fields,
                         idempotency_key=f"anyaicam-downgrade-{schedule_id}-{period_end}")
    _record_scheduled_change(plan["id"], "downgrade_to_local", period_end, schedule_id)
    audit(identity, "customer.plan_downgrade_scheduled", "customer", identity["customer_id"], {"to": "local", "effective_at": period_end})
    return {"status": "downgrade_scheduled", "effective_at": period_end,
            "message": "Hybrid stays active until your next renewal; then your plan becomes Local."}


def cancel_downgrade(identity: dict) -> dict:
    plan, current = _owner_base_plan(identity)
    if _schedule_id(current):
        current = _release_schedule(current)
    _record_scheduled_change(plan["id"], None, None, None)
    _apply(current)
    audit(identity, "customer.plan_downgrade_cancelled", "customer", identity["customer_id"])
    return {"status": "downgrade_cancelled", "message": "You're keeping Hybrid."}


def _record_scheduled_change(entitlement_id: str, change: str | None, at: int | None, schedule_id: str | None) -> None:
    from partner_db import connection
    with connection() as db:
        db.execute("UPDATE customer_entitlements SET scheduled_change=?,scheduled_change_at=?,stripe_schedule_id=? WHERE id=?",
                   (change, at, schedule_id, entitlement_id))


def sync_scheduled_change(entitlement: dict, subscription: dict) -> None:
    """Keep the shown scheduled change true to Stripe: no schedule on the
    subscription -> nothing scheduled (released, or already applied at
    renewal); a schedule whose next phase is Local while on Hybrid ->
    'downgrade_to_local' at the period end (also when made in the portal)."""
    import customer_entitlements as ce
    import stripe_state
    schedule_id = _schedule_id(subscription)
    if not schedule_id:
        if entitlement.get("scheduled_change") or entitlement.get("stripe_schedule_id"):
            _record_scheduled_change(entitlement["id"], None, None, None)
        return
    if entitlement.get("stripe_schedule_id") == schedule_id and entitlement.get("scheduled_change"):
        return
    reader = stripe_state._stripe_reader()
    if reader is None:
        return
    try:
        found = reader(f"/v1/subscription_schedules/{schedule_id}")
    except Exception:
        return
    period_end = int(ce._period_end(subscription) or 0)
    upcoming = next((phase for phase in (found or {}).get("phases") or []
                     if period_end and int(phase.get("start_date") or 0) >= period_end), None)
    item = (((upcoming or {}).get("items")) or [{}])[0]
    price = item.get("price")
    price = str((price.get("id") if isinstance(price, dict) else price) or "")
    tier = ce.resolve_tier(price) if price else None
    if tier and tier["product"] == "camera_slots_local" and entitlement.get("product") == "camera_slots_hybrid":
        _record_scheduled_change(entitlement["id"], "downgrade_to_local", int(upcoming.get("start_date")), schedule_id)


def register_plan_management_routes(app: FastAPI) -> None:
    from partner_portal import partner_identity

    def owner(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer sign-in required.")
        if identity.get("role") != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can change the plan.")
        return identity

    @app.post("/api/customer/plan/cancel")
    def plan_cancel(request: Request) -> dict:
        return cancel_at_period_end(owner(request))

    @app.post("/api/customer/plan/resume")
    def plan_resume(request: Request) -> dict:
        return resume_renewal(owner(request))

    @app.post("/api/customer/plan/downgrade-to-local")
    def plan_downgrade(request: Request) -> dict:
        return downgrade_to_local(owner(request))

    @app.post("/api/customer/plan/cancel-downgrade")
    def plan_cancel_downgrade(request: Request) -> dict:
        return cancel_downgrade(owner(request))
