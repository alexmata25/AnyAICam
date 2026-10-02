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

What happens to the money for the rest of the current period is the owner's
proration decision, not decided here: ANYAICAM_STRIPE_UPGRADE_PRORATION must
be set to a Stripe proration_behavior (create_prorations, always_invoice or
none). Until it is, the upgrade is refused and its button is not shown.
"""
from __future__ import annotations

import os
import sys

from fastapi import FastAPI, HTTPException, Request

from partner_db import audit

PRORATION_ENV = "ANYAICAM_STRIPE_UPGRADE_PRORATION"
PRORATION_CHOICES = ("create_prorations", "always_invoice", "none")


def upgrade_proration() -> str | None:
    value = os.environ.get(PRORATION_ENV, "").strip().lower()
    return value if value in PRORATION_CHOICES else None


def upgrade_available() -> bool:
    return upgrade_proration() is not None


def _hybrid_tier_for(camera_slots: int):
    from customer_entitlements import PLAN_TIERS
    return next((t for t in PLAN_TIERS if t[0] == "hybrid" and t[4] == camera_slots), None)


def upgrade_to_hybrid(identity: dict) -> dict:
    import customer_entitlements as ce
    import stripe_state
    main = sys.modules.get("main")
    customer_id = identity["customer_id"]
    proration = upgrade_proration()
    if proration is None:
        raise HTTPException(status_code=503, detail="Upgrading to Hybrid online is not available yet. Please contact AnyAiCam support.")
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
            ("proration_behavior", proration),
            ("metadata[anyaicam_customer_id]", customer_id),
            ("metadata[anyaicam_stripe_price_id]", hybrid_price),
            ("metadata[anyaicam_camera_slot_plan_type]", "hybrid"),
            ("metadata[anyaicam_camera_slot_maximum]", str(camera_slot_maximum)),
        ])
    # Apply what Stripe now says (Hybrid active, Local superseded); the
    # webhook for the same change is idempotent with this.
    ce._sync_subscription_change({"id": f"upgrade:{subscription_id}", "type": "customer.subscription.updated",
                                  "data": {"object": current}}, cancelled=False)
    audit(identity, "customer.plan_upgraded", "customer", customer_id,
          {"from": "local", "to": "hybrid", "camera_slots": camera_slot_maximum, "proration_behavior": proration})
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
