"""Customer billing management (2026-10-02): the customer owner's "Manage
billing" action, opening Stripe's Billing Portal for THIS account only.

The only existing portal route (/api/payments/customer-portal) is the
legacy appliance-admin tool: it maps a legacy login to a billing account and
falls back to the shared 'primary' account, so a customer session reaching it
opened another account's Stripe portal. That route is now admin-only; this
one is bound to the signed-in customer:

- the customer session (partner_identity), owner only -- household members
  and viewers are refused;
- the Stripe customer id this AnyAiCam account's own purchases were made
  with (entitlements and add-ons); none -> 404, more than one -> 409 (never
  guess which to open);
- nothing about payment details, tokens or the Stripe secret is returned or
  logged -- only the portal URL Stripe issues.

What customers can do inside the portal (update card, see invoices, cancel,
switch plan) is configured in Stripe and is an owner decision; until it is
made, this is off unless ANYAICAM_STRIPE_BILLING_PORTAL_ENABLED=true.
"""
from __future__ import annotations

import os
import sys

from fastapi import FastAPI, HTTPException, Request

from partner_db import audit

PORTAL_ENV = "ANYAICAM_STRIPE_BILLING_PORTAL_ENABLED"


def portal_enabled() -> bool:
    return os.environ.get(PORTAL_ENV, "").strip().lower() == "true"


def stripe_customer_ids_for_customer(customer_id: str) -> set[str]:
    from partner_db import rows
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
            raise HTTPException(status_code=403, detail="Customer sign-in required.")
        if identity.get("role") != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can manage billing.")
        if not portal_enabled():
            raise HTTPException(status_code=404, detail="Online billing management is not available yet.")
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
