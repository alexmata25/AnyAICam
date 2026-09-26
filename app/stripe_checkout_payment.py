"""When a completed Stripe Checkout Session has actually been paid (2026-09-25).

Checkout Sessions here don't restrict payment_method_types, so Stripe offers
whatever methods the account has enabled -- including delayed ones (bank
debits and similar). For those, `checkout.session.completed` arrives with
payment_status "unpaid" before any money has moved, and the outcome comes
later as `checkout.session.async_payment_succeeded` or
`checkout.session.async_payment_failed`. Granting on the first event gave
away one-time camera slots / analytics and put unpaid hardware orders in the
fulfillment queue (created as fulfillment_status "paid"), with nothing to
undo it if the payment then failed.

Rule shared by customer_entitlements, analytics_entitlements and
hardware_orders: nothing is granted, linked or ordered while a session is
explicitly "unpaid"; the same processing runs on async_payment_succeeded
(its own event id, same session, payment_status "paid"). "paid" and
"no_payment_required" (e.g. a 100% discount) grant immediately, as does a
payload with no payment_status at all (older minimal payloads) -- exactly
the previous behavior for every case that was not a delayed payment.

Stripe only sends the async events to endpoints subscribed to them: the
webhook endpoint must include checkout.session.async_payment_succeeded
(and ..._failed, which needs no action here since nothing was granted).
"""
from __future__ import annotations

CHECKOUT_GRANT_EVENT_TYPES = ("checkout.session.completed", "checkout.session.async_payment_succeeded")


def awaiting_payment(session_obj: dict) -> bool:
    return str((session_obj or {}).get("payment_status") or "").strip().lower() == "unpaid"


def awaiting_payment_result(session_obj: dict) -> dict:
    return {"status": "awaiting_payment", "stripe_checkout_session_id": str((session_obj or {}).get("id") or "") or None}
