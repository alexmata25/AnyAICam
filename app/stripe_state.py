"""Stripe's current state and Stripe-customer ownership (2026-10-02).

Shared by camera-plan entitlements (customer_entitlements.py) and add-ons
(analytics_entitlements.py), which deliberately never import each other.

Stripe does not deliver webhooks in order and retries them for days; an
event's payload is a snapshot from when it was created. Subscription events
and subscription checkouts therefore apply what Stripe says the subscription
is NOW (current_subscription). If that cannot be read -- including when no
Stripe secret key is configured -- the event is retried
(RetryableStripeEventError -> the webhook answers 503), never applied from a
possibly stale snapshot (Codex audit of 3f5b9c4, finding 9). Which statuses
count as active is unchanged by this module; failed-payment behaviour is an
owner policy.

A Stripe customer belongs to exactly one AnyAiCam account
(stripe_customer_accounts): one already paying for account A never provisions
account B. An account's canonical Stripe customer is bound once
(stripe_customer_bindings, checkout_guard.py) and reused by every checkout.

An entitlement row bound to one subscription is never altered by an event for
a different subscription unless that one has ended (conflicting_subscription,
finding 3), and service is never granted for a payment that was fully
refunded or disputed before the grant (payment_reversal, finding 4).
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


# A subscription Stripe has ENDED (never past_due/unpaid: those get the
# 7-day grace, billing_status.py).
ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})


def _stripe_reader():
    """main.stripe_api_get when Stripe is configured, else None."""
    main = sys.modules.get("main")
    if main is None or not getattr(main, "STRIPE_SECRET_KEY", ""):
        return None
    return getattr(main, "stripe_api_get", None)


def stripe_get(path: str, what: str) -> dict:
    """A read of Stripe's current state, or RetryableStripeEventError. With
    no secret key nothing can be confirmed, so nothing is applied."""
    reader = _stripe_reader()
    if reader is None:
        raise RetryableStripeEventError(f"Stripe is not configured here, so {what} cannot be confirmed")
    try:
        found = reader(path)
    except Exception as error:
        raise RetryableStripeEventError(f"could not read {what} from Stripe") from error
    if not isinstance(found, dict):
        raise RetryableStripeEventError(f"Stripe returned an unexpected answer for {what}")
    return found


def current_subscription(subscription_id: str | None) -> dict | None:
    """Stripe's current subscription; None only when there is no id."""
    if not subscription_id:
        return None
    current = stripe_get(f"/v1/subscriptions/{quote(str(subscription_id), safe='')}", "the subscription's current state")
    if str(current.get("id") or "") != str(subscription_id):
        raise RetryableStripeEventError("Stripe returned an unexpected subscription")
    return current


def conflicting_subscription(bound_subscription_id: str | None, subscription: dict) -> str | None:
    """Why an event for `subscription` must not alter a row already bound to
    another subscription (None: it may). A delayed event for an old, ended
    subscription never touches the account's newer one; a live subscription
    takes over a row only once the subscription it is bound to has ended."""
    event_subscription_id = str(subscription.get("id") or "")
    if not bound_subscription_id or not event_subscription_id or bound_subscription_id == event_subscription_id:
        return None
    if subscription.get("status") in ENDED_STATUSES:
        return "an event for an older subscription; the account's current subscription is unaffected"
    bound = current_subscription(bound_subscription_id)
    if bound is not None and bound.get("status") not in ENDED_STATUSES:
        logger.warning("stripe.two_live_subscriptions bound=%s event=%s", bound_subscription_id, event_subscription_id)
        return "the account already has another live subscription for this"
    return None


def _id(value) -> str:
    return str((value.get("id") if isinstance(value, dict) else value) or "")


def _full_refund(charge: dict) -> bool:
    amount = int(charge.get("amount") or 0)
    return bool(charge.get("refunded")) or (amount > 0 and int(charge.get("amount_refunded") or 0) >= amount)


def payment_reversal(*, payment_intent: str | None, invoice_id: str | None = None, charge_id: str | None = None) -> str | None:
    """'refunded' / 'disputed' when this payment was fully refunded or is
    disputed: from AnyAiCam's durable reversal record (a refund or dispute
    event that arrived before the grant) and from Stripe's own charge. A
    partial refund is no reversal (owner policy). Stripe unreadable ->
    RetryableStripeEventError."""
    import billing_status
    recorded = billing_status.reversal_reason(charge_id or "", payment_intent or "", invoice_id or "")
    if recorded:
        return recorded
    if not payment_intent:
        return None
    intent = stripe_get(f"/v1/payment_intents/{quote(payment_intent, safe='')}?expand[]=latest_charge", "the payment")
    charge = intent.get("latest_charge")
    if isinstance(charge, dict) and _full_refund(charge):
        billing_status.record_reversal(_id(charge), payment_intent, invoice_id or "", "refunded")
        return "refunded"
    return None


def subscription_payment_reversal(subscription: dict) -> str | None:
    """payment_reversal for the payment covering a subscription's current
    period (its latest invoice). A charged invoice whose payment cannot be
    identified is retried, never assumed fine."""
    invoice_id = _id(subscription.get("latest_invoice"))
    if not invoice_id:
        raise RetryableStripeEventError("the subscription's payment could not be identified")
    invoice = subscription["latest_invoice"] if isinstance(subscription.get("latest_invoice"), dict) else None
    invoice = invoice or stripe_get(f"/v1/invoices/{quote(invoice_id, safe='')}", "the subscription's invoice")
    import billing_status
    import sales_commissions
    if int(invoice.get("amount_paid") or 0) <= 0:
        return billing_status.reversal_reason("", "", invoice_id)  # nothing was charged (e.g. a 100% discount)
    intent = sales_commissions._invoice_payment_intent(invoice)
    if not intent and not invoice.get("charge"):
        raise RetryableStripeEventError("the subscription's payment could not be identified")
    return payment_reversal(payment_intent=intent, invoice_id=invoice_id, charge_id=_id(invoice.get("charge")))


def stripe_customer_accounts(stripe_customer_id: str | None) -> set[str]:
    """Every AnyAiCam account already holding something bought through this
    Stripe customer (camera plans, licenses, add-ons)."""
    if not stripe_customer_id:
        return set()
    accounts = {r["customer_id"] for r in rows(
        "SELECT customer_id FROM customer_entitlements WHERE stripe_customer_id=?", (stripe_customer_id,))}
    try:
        accounts |= {r["customer_id"] for r in rows(
            "SELECT customer_id FROM stripe_customer_bindings WHERE stripe_customer_id=?", (stripe_customer_id,))}
    except Exception:
        pass  # a database before canonical bindings
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
