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
