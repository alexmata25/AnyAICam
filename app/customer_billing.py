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

Owner policy (2026-10-02): the portal is on for account owners, for changing
the card, upgrading and downgrading the plan, and cancelling/resuming renewal.
Before a session opens, the Stripe portal configuration is checked against
the approved billing policies (configuration_problems()); a mismatch refuses
the portal rather than letting Stripe apply a different policy:
- payment method update enabled;
- cancellation enabled, at the end of the paid period (never immediate);
- plan changes enabled, upgrades prorated and invoiced immediately
  (proration_behavior=always_invoice), downgrades scheduled for the end of the
  period (schedule_at_period_end on decreasing_item_amount).
ANYAICAM_STRIPE_PORTAL_CONFIGURATION_ID names the configuration (else Stripe's
default is checked). ANYAICAM_STRIPE_BILLING_PORTAL_ENABLED=false turns the
portal off.
"""
from __future__ import annotations

import os
import sys

from fastapi import FastAPI, HTTPException, Request

from partner_db import audit

PORTAL_ENV = "ANYAICAM_STRIPE_BILLING_PORTAL_ENABLED"


def portal_enabled() -> bool:
    return os.environ.get(PORTAL_ENV, "true").strip().lower() != "false"


CONFIGURATION_ENV = "ANYAICAM_STRIPE_PORTAL_CONFIGURATION_ID"


def configuration_problems(configuration: dict) -> list[str]:
    """What in this Stripe portal configuration contradicts the approved
    billing policies (empty list = matches)."""
    features = (configuration or {}).get("features") or {}
    problems = []
    if not (features.get("payment_method_update") or {}).get("enabled"):
        problems.append("payment method update is not enabled")
    cancel = features.get("subscription_cancel") or {}
    if not cancel.get("enabled"):
        problems.append("cancellation is not enabled")
    elif cancel.get("mode") != "at_period_end":
        problems.append("cancellation is not at the end of the period")
    update = features.get("subscription_update") or {}
    if not update.get("enabled"):
        problems.append("plan changes are not enabled")
    else:
        if update.get("proration_behavior") != "always_invoice":
            problems.append("upgrades are not prorated and invoiced immediately")
        conditions = ((update.get("schedule_at_period_end") or {}).get("conditions")) or []
        if not any(isinstance(c, dict) and c.get("type") == "decreasing_item_amount" for c in conditions):
            problems.append("downgrades are not scheduled for the end of the period")
    return problems


def _portal_configuration(main) -> dict:
    configured = os.environ.get(CONFIGURATION_ENV, "").strip()
    if configured:
        return main.stripe_api_get(f"/v1/billing_portal/configurations/{configured}")
    listed = main.stripe_api_get("/v1/billing_portal/configurations?is_default=true&limit=1")
    data = (listed or {}).get("data") or []
    return data[0] if data else {}


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
        try:
            configuration = _portal_configuration(main)
        except HTTPException:
            raise HTTPException(status_code=503, detail="Billing management is not available right now.")
        problems = configuration_problems(configuration)
        if problems:
            main.structured_log("stripe.portal_configuration_mismatch", level="error", problems=problems)
            raise HTTPException(status_code=503, detail="Billing management is being set up. Please try again later or contact AnyAiCam support.")
        fields = [("customer", next(iter(ids))), ("return_url", f"{main.PUBLIC_BASE_URL}/subscription-portal")]
        if configuration.get("id"):
            fields.append(("configuration", str(configuration["id"])))
        portal = main.stripe_api_post("/v1/billing_portal/sessions", fields)
        url = str(portal.get("url") or "")
        if not url.startswith("https://"):
            raise HTTPException(status_code=502, detail="Billing management could not be opened. Please try again.")
        audit(identity, "customer.billing_portal_opened", "customer", identity["customer_id"])
        return {"url": url}
