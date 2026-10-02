"""Customer billing management (2026-10-02): the real customer owner's
"Manage billing" action on My subscription.

The only Stripe Customer Portal route used the legacy admin-portal
current_user / billing_account_for_user mapping, which a Customer Portal
session never has. This one is bound to the signed-in customer: partner_
identity, owner role only (household members are refused), and the Stripe
customer id this AnyAiCam account's own subscriptions were created with.
If the account's subscriptions disagree about their Stripe customer the
action is refused rather than guessing which one to open.

Payment-failure, grace and restriction behaviour is deliberately NOT
represented here: those need the owner's policy first.
"""
from __future__ import annotations

import sys

from fastapi import FastAPI, HTTPException, Request

from partner_db import audit, rows


def stripe_customer_ids_for_customer(customer_id: str) -> set[str]:
    ids = {r["stripe_customer_id"] for r in rows(
        "SELECT stripe_customer_id FROM customer_entitlements WHERE customer_id=? AND stripe_customer_id IS NOT NULL AND stripe_customer_id<>''",
        (customer_id,))}
    try:
        ids |= {r["stripe_customer_id"] for r in rows(
            "SELECT stripe_customer_id FROM addon_subscriptions WHERE customer_id=? AND stripe_customer_id IS NOT NULL AND stripe_customer_id<>''",
            (customer_id,))}
    except Exception:
        pass  # a database without add-ons yet
    return ids


def register_customer_billing_routes(app: FastAPI) -> None:
    from partner_portal import partner_identity

    @app.post("/api/customer/billing-portal")
    def customer_billing_portal(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer Portal sign-in required.")
        if identity.get("role") != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can manage billing.")
        ids = stripe_customer_ids_for_customer(identity["customer_id"])
        if not ids:
            raise HTTPException(status_code=404, detail="There is no billing account for this AnyAiCam account yet.")
        if len(ids) > 1:
            raise HTTPException(status_code=409, detail="This account's billing records need attention. Please contact AnyAiCam support.")
        main = sys.modules.get("main")
        if main is None or not getattr(main, "PUBLIC_BASE_URL", ""):
            raise HTTPException(status_code=503, detail="Billing management is not available right now.")
        portal = main.stripe_api_post("/v1/billing_portal/sessions", [
            ("customer", next(iter(ids))),
            ("return_url", f"{main.PUBLIC_BASE_URL}/subscription-portal"),
        ])
        url = str(portal.get("url") or "")
        if not url.startswith("https://"):
            raise HTTPException(status_code=502, detail="Billing management could not be opened. Please try again.")
        audit(identity, "customer.billing_portal_opened", "customer", identity["customer_id"])
        return {"url": url}
