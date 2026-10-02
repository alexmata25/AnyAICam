"""Stripe's current state and Stripe-customer ownership (2026-10-02).

Shared by camera-plan entitlements (customer_entitlements.py) and add-ons
(analytics_entitlements.py), which deliberately never import each other.

Stripe does not deliver webhooks in order and retries them for days; an
event's payload is a snapshot from when it was created. Subscription events
and subscription checkouts therefore apply what Stripe says the subscription
is NOW (current_subscription). If that cannot be read, the event is retried
(RetryableStripeEventError -> the webhook answers 503), never applied from a
possibly stale snapshot. Which statuses count as active is unchanged by this
module; failed-payment behaviour is an owner policy.

A Stripe customer belongs to exactly one AnyAiCam account
(stripe_customer_accounts): one already paying for account A never provisions
account B.
"""
from __future__ import annotations

import logging
import sys
from urllib.parse import quote

from partner_db import rows

logger = logging.getLogger("anyaicam.stripe_state")


class RetryableStripeEventError(Exception):
    """Stripe's current state could not be read: the event is not recorded
    as processed, the webhook answers 503 and Stripe retries it."""


def _stripe_reader():
    """main.stripe_api_get when Stripe is configured; None otherwise (no
    secret key means nothing can be checked out either; events are then
    applied from their payload, as before)."""
    main = sys.modules.get("main")
    if main is None or not getattr(main, "STRIPE_SECRET_KEY", ""):
        return None
    return getattr(main, "stripe_api_get", None)


def current_subscription(subscription_id: str | None) -> dict | None:
    reader = _stripe_reader()
    if reader is None or not subscription_id:
        return None
    try:
        current = reader(f"/v1/subscriptions/{quote(str(subscription_id), safe='')}")
    except Exception as error:
        raise RetryableStripeEventError("could not read the subscription's current state from Stripe") from error
    if not isinstance(current, dict) or str(current.get("id") or "") != str(subscription_id):
        raise RetryableStripeEventError("Stripe returned an unexpected subscription")
    return current


def stripe_customer_accounts(stripe_customer_id: str | None) -> set[str]:
    """Every AnyAiCam account already holding something bought through this
    Stripe customer (camera plans, licenses, add-ons)."""
    if not stripe_customer_id:
        return set()
    accounts = {r["customer_id"] for r in rows(
        "SELECT customer_id FROM customer_entitlements WHERE stripe_customer_id=?", (stripe_customer_id,))}
    try:
        accounts |= {r["customer_id"] for r in rows(
            "SELECT customer_id FROM addon_subscriptions WHERE stripe_customer_id=?", (stripe_customer_id,))}
    except Exception:
        pass  # a database without add-ons yet
    return accounts


def bound_elsewhere(stripe_customer_id: str | None, customer_id: str) -> bool:
    return bool(stripe_customer_accounts(stripe_customer_id) - {customer_id})


def unresolved(kind: str, subscription_id: str | None, stripe_customer_id: str | None) -> dict:
    """An event for no known AnyAiCam account: recorded as ignored (Stripe
    would otherwise retry it for days, and could disable the endpoint), never
    fabricated, and logged for operations. Any later event for the same
    subscription applies Stripe's current state anyway."""
    logger.warning("stripe.unresolved_subscription kind=%s subscription=%s stripe_customer=%s",
                   kind, subscription_id or "-", stripe_customer_id or "-")
    return {"status": "ignored", "reason": "no AnyAiCam account for this subscription"}
