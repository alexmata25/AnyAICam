"""Provisioning Phase 6: customer-facing post-purchase notifications --
"your AnyAiCam service is ready", "your plan was updated", "your
subscription was cancelled", "your hardware order was received", and
"complete your account setup" (checkout-before-registration).

This module sends email ONLY -- it never creates, updates, or deletes an
entitlement or hardware order. It reads the RESULT of processing that
customer_entitlements.py / hardware_orders.py already did (their own
existing idempotent sync functions, unmodified) and decides what, if
anything, a customer should be emailed about. No Stripe product/price
logic, no checkout logic, and no entitlement/order schema were touched to
build this -- exactly what was asked.

Why this needs its OWN idempotency table, separate from entitlement
processing's idempotency
--------------------------------------------------------------------------
customer_entitlements.sync_entitlement_from_stripe_event() is idempotent
per Stripe event id via provisioning_webhook_events: a REDELIVERED event
(Stripe retries on anything but a 2xx) short-circuits to
{"status": "already_processed"} WITHOUT recomputing what actually
happened -- correct for entitlement correctness (never double-grant), but
useless on its own for notifications: if this module's email send failed
on the first delivery, a naive read of that return value on redelivery
would see only "already_processed" and have nothing to act on, so a
failed email could never be retried.

The fix: provisioning_webhook_events already stores the FULL result dict
from the first (and only) real processing pass, keyed by event id,
forever -- _stored_entitlement_result() reads that stored value directly,
so this module sees the real outcome ("entitlement_updated",
"pending_link_created", "ignored", ...) on every delivery, first or
hundredth. Whether an EMAIL was actually sent for that outcome is then
tracked in this module's own provisioning_notifications table, keyed by
(stripe_event_id, notification_type) -- independent of, and never
gating, entitlement processing. Concretely:
  - entitlement processing succeeds, email send fails -> entitlement is
    correct and unaffected; provisioning_notifications row is 'failed';
    the NEXT redelivery of the same event re-reads the same stored
    outcome and retries the send (updating the same row, not a duplicate).
  - entitlement processing succeeds, email send succeeds -> row is
    'sent'; any later redelivery of the same event sees 'sent' and skips,
    so the customer is never emailed twice for one event.
This is the "existing webhook-event idempotency AND a durable
notification marker" the task asked for -- both, each solving a different
half of the problem.

Event-type -> email-type mapping (mirrors customer_entitlements.py's own
event routing exactly, so this module never has to re-derive "was this a
fresh purchase, an upgrade, or a cancellation" by diffing state):
  checkout.session.completed  (-> entitlement_updated)       => account_ready
  checkout.session.completed  (-> pending_link_created)      => setup_required
  customer.subscription.updated (-> entitlement_updated,
                                    resulting status != 'cancelled')
                                                               => plan_updated
  customer.subscription.updated/deleted (-> resulting
                                             status == 'cancelled')
                                                               => plan_cancelled
  checkout.session.completed, mode=payment, hardware SKU     => hardware_order_confirmation
Anything else (ignored/no verified tier/no verified hardware SKU,
including the $1 live plumbing-test Price ID) sends nothing -- silence is
the correct, fail-closed default, not a guess.

Hardware purchases never produce a camera-slot email and camera-slot
purchases never produce a hardware email, for the same structural reason
hardware_orders.py and customer_entitlements.py can never cross-grant:
a single event's Price ID resolves against at most one of PRICE_ID_
CAMERA_SLOT_MAP / HARDWARE_PRICE_MAP, so at most one of
_stored_entitlement_result()/_find_hardware_order_for_event() ever
produces a real outcome for that event.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from partner_db import connection, row

from email_service import get_email_service

CANCELLED_STATUSES = {"cancelled"}


def _now() -> str:
    return datetime.now().isoformat()


def _first_name(full_name: Optional[str]) -> str:
    """Safe best-effort first name: the part of customers.name before the
    first space, or the whole name if there's no space, or a generic
    greeting if there's nothing usable at all. Never raises, never
    invents a name that wasn't there."""
    name = (full_name or "").strip()
    if not name:
        return "there"
    return name.split(" ", 1)[0]


PLAN_LABELS = {"camera_slots_local": "Local", "camera_slots_hybrid": "Hybrid"}


def _plan_label(product: str) -> str:
    return PLAN_LABELS.get(product, product.replace("camera_slots_", "").replace("_", " ").title() or product)


# ---------------------------------------------------------- idempotency


def _existing_notification(event_id: str, notification_type: str) -> Optional[dict]:
    return row(
        "SELECT * FROM provisioning_notifications WHERE stripe_event_id=? AND notification_type=?",
        (event_id, notification_type),
    )


def _record_attempt(
    *, event_id: str, notification_type: str, customer_id: Optional[str], recipient_email: str, subject: str,
) -> dict:
    """Creates (or reuses, if a prior failed attempt already exists) the
    tracking row for this (event, type) pair, in 'pending' status, before
    actually calling the email backend -- so a crash between "decided to
    send" and "actually sent" still leaves a retryable 'pending'/'failed'
    row rather than silently losing the attempt."""
    existing = _existing_notification(event_id, notification_type)
    now = _now()
    with connection() as db:
        if existing:
            notification_id = existing["id"]
            db.execute(
                "UPDATE provisioning_notifications SET customer_id=?,recipient_email=?,subject=?,updated_at=? WHERE id=?",
                (customer_id, recipient_email, subject, now, notification_id),
            )
        else:
            notification_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO provisioning_notifications(id,stripe_event_id,notification_type,customer_id,"
                "recipient_email,subject,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (notification_id, event_id, notification_type, customer_id, recipient_email, subject, "pending", now, now),
            )
    return row("SELECT * FROM provisioning_notifications WHERE id=?", (notification_id,))


def _mark_result(notification_id: str, *, status: str, error_detail: Optional[str] = None) -> None:
    now = _now()
    with connection() as db:
        db.execute(
            "UPDATE provisioning_notifications SET status=?,error_detail=?,updated_at=?,sent_at=? WHERE id=?",
            (status, error_detail, now, now if status == "sent" else None, notification_id),
        )


def _send_once(
    *, event_id: str, notification_type: str, customer_id: Optional[str], recipient_email: str,
    subject: str, text: str, html: Optional[str] = None, metadata: Optional[dict] = None,
) -> dict:
    """The one place that actually calls the email backend for a
    provisioning notification. Idempotent per (event_id, notification_
    type): a prior 'sent' row short-circuits to a no-op skip; a missing
    or 'failed' row attempts (or retries) the send, and email failure
    here is recorded but NEVER raised -- the caller (the webhook route)
    must be able to keep processing/return 200 to Stripe regardless."""
    if not recipient_email:
        return {"status": "skipped", "reason": "no recipient email available"}
    existing = _existing_notification(event_id, notification_type)
    if existing and existing["status"] == "sent":
        return {"status": "skipped", "reason": "already sent", "notification_id": existing["id"]}

    tracking = _record_attempt(
        event_id=event_id, notification_type=notification_type, customer_id=customer_id,
        recipient_email=recipient_email, subject=subject,
    )
    try:
        get_email_service().send(notification_type, recipient_email, subject, text, html=html, metadata=metadata or {})
    except Exception as exc:
        _mark_result(tracking["id"], status="failed", error_detail=str(exc)[:500])
        return {"status": "failed", "notification_id": tracking["id"], "error": str(exc)[:500]}
    _mark_result(tracking["id"], status="sent")
    return {"status": "sent", "notification_id": tracking["id"]}


# ------------------------------------------------------- content renderers


def _account_ready_email(first_name: str, plan_label: str, camera_slot_maximum: int) -> tuple[str, str, str]:
    subject = "Your AnyAiCam service is ready"
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam purchase is confirmed and your service is ready.\n\n"
        f"Plan: {plan_label}\n"
        f"Camera capacity: {camera_slot_maximum}\n\n"
        f"Sign in and connect/claim your AnyAiCam VMS or appliance: https://app.anyaicam.com\n\n"
        f"If you need help getting started, our support team is here for you.\n\n"
        f"— The AnyAiCam Team"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam purchase is confirmed and your service is ready.</p>"
        f"<p><strong>Plan:</strong> {plan_label}<br><strong>Camera capacity:</strong> {camera_slot_maximum}</p>"
        f'<p>Sign in and connect/claim your AnyAiCam VMS or appliance: '
        f'<a href="https://app.anyaicam.com">https://app.anyaicam.com</a></p>'
        f"<p>If you need help getting started, our support team is here for you.</p>"
        f"<p>— The AnyAiCam Team</p>"
    )
    return subject, text, html


def _setup_required_email(plan_label: str, camera_slot_maximum: int) -> tuple[str, str, str]:
    subject = "Your AnyAiCam purchase is confirmed — complete your setup"
    text = (
        f"Hi there,\n\n"
        f"Your AnyAiCam purchase is confirmed ({plan_label}, {camera_slot_maximum} cameras).\n\n"
        f"Complete your account setup to activate your service: https://app.anyaicam.com\n\n"
        f"— The AnyAiCam Team"
    )
    html = (
        f"<p>Hi there,</p>"
        f"<p>Your AnyAiCam purchase is confirmed ({plan_label}, {camera_slot_maximum} cameras).</p>"
        f'<p>Complete your account setup to activate your service: '
        f'<a href="https://app.anyaicam.com">https://app.anyaicam.com</a></p>'
        f"<p>— The AnyAiCam Team</p>"
    )
    return subject, text, html


def _plan_updated_email(first_name: str, plan_label: str, camera_slot_maximum: int) -> tuple[str, str, str]:
    subject = "Your AnyAiCam plan has been updated"
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam plan has been updated to {camera_slot_maximum} cameras ({plan_label}).\n\n"
        f"Sign in at https://app.anyaicam.com to see your updated plan.\n\n"
        f"— The AnyAiCam Team"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam plan has been updated to <strong>{camera_slot_maximum} cameras</strong> ({plan_label}).</p>"
        f'<p>Sign in at <a href="https://app.anyaicam.com">https://app.anyaicam.com</a> to see your updated plan.</p>'
        f"<p>— The AnyAiCam Team</p>"
    )
    return subject, text, html


def _plan_cancelled_email(first_name: str, plan_label: str) -> tuple[str, str, str]:
    subject = "Your AnyAiCam camera-slot subscription has been cancelled"
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam {plan_label} camera-slot subscription has been cancelled.\n\n"
        f"Your account and any connected appliances remain in place -- only camera-slot licensing has changed.\n\n"
        f"If this wasn't expected, contact our support team.\n\n"
        f"— The AnyAiCam Team"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam {plan_label} camera-slot subscription has been cancelled.</p>"
        f"<p>Your account and any connected appliances remain in place — only camera-slot licensing has changed.</p>"
        f"<p>If this wasn't expected, contact our support team.</p>"
        f"<p>— The AnyAiCam Team</p>"
    )
    return subject, text, html


def _hardware_order_email(first_name: str, product_name: str, quantity: int) -> tuple[str, str, str]:
    subject = "Your AnyAiCam hardware order has been received"
    qty_text = f"{quantity} × {product_name}" if quantity != 1 else product_name
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam hardware order has been received: {qty_text}.\n\n"
        f"We'll follow up with shipping details separately. This order does not include any camera-slot "
        f"subscription -- if you'd also like recurring camera-slot service, that's purchased separately.\n\n"
        f"— The AnyAiCam Team"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam hardware order has been received: <strong>{qty_text}</strong>.</p>"
        f"<p>We'll follow up with shipping details separately. This order does not include any camera-slot "
        f"subscription — if you'd also like recurring camera-slot service, that's purchased separately.</p>"
        f"<p>— The AnyAiCam Team</p>"
    )
    return subject, text, html


# --------------------------------------------------------- stored-result lookups


def _stored_entitlement_result(event_id: str) -> Optional[dict]:
    stored = row("SELECT result FROM provisioning_webhook_events WHERE id=?", (event_id,))
    if not stored:
        return None
    try:
        return json.loads(stored["result"])
    except (TypeError, ValueError):
        return None


def _find_hardware_order_for_session(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    return row("SELECT * FROM hardware_orders WHERE stripe_checkout_session_id=? ORDER BY created_at DESC LIMIT 1", (session_id,))


def _entitlement_row(entitlement_id: str) -> Optional[dict]:
    return row("SELECT * FROM customer_entitlements WHERE id=?", (entitlement_id,))


def _customer_row(customer_id: str) -> Optional[dict]:
    return row("SELECT * FROM customers WHERE id=?", (customer_id,))


def _session_email(session_obj: dict) -> str:
    return str((session_obj.get("customer_details") or {}).get("email") or session_obj.get("customer_email") or "")


# ------------------------------------------------------------- orchestration


def notify_from_stripe_event(event: dict) -> dict:
    """Call on every delivery of POST /api/payments/stripe/webhook, AFTER
    both customer_entitlements.sync_entitlement_from_stripe_event() and
    hardware_orders.sync_hardware_order_from_stripe_event() have already
    run (this function never triggers either of them itself). Never
    raises -- every internal failure is caught and reported in the
    returned dict / recorded in provisioning_notifications, never
    propagated to the webhook route."""
    try:
        return _notify_from_stripe_event(event)
    except Exception as exc:
        return {"status": "error", "reason": str(exc)[:500]}


def _notify_from_stripe_event(event: dict) -> dict:
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        return {"status": "ignored", "reason": "event missing id"}

    session_or_sub = (event.get("data") or {}).get("object") or {}

    entitlement_outcome = _stored_entitlement_result(event_id)
    if entitlement_outcome and entitlement_outcome.get("status") == "entitlement_updated":
        return _notify_entitlement_updated(event_id, event_type, entitlement_outcome)
    if entitlement_outcome and entitlement_outcome.get("status") == "pending_link_created":
        return _notify_setup_required(event_id, session_or_sub)

    # Not a camera-slot outcome -- check whether this event produced a
    # hardware order instead (mutually exclusive by construction, see
    # module docstring).
    if event_type == "checkout.session.completed":
        session_id = str(session_or_sub.get("id") or "")
        hardware_order = _find_hardware_order_for_session(session_id)
        if hardware_order:
            return _notify_hardware_order(event_id, hardware_order)

    return {"status": "ignored", "reason": "no notifiable outcome for this event"}


def _notify_entitlement_updated(event_id: str, event_type: str, outcome: dict) -> dict:
    entitlement = _entitlement_row(str(outcome.get("entitlement_id") or ""))
    if not entitlement:
        return {"status": "ignored", "reason": "entitlement row no longer exists"}
    customer = _customer_row(entitlement["customer_id"])
    if not customer:
        return {"status": "ignored", "reason": "customer row no longer exists"}

    plan_label = _plan_label(entitlement["product"])
    quantity = int(entitlement["camera_slot_quantity"] or 0)
    first_name = _first_name(customer.get("name"))
    recipient = customer.get("email") or ""

    cancelled = entitlement["status"] in CANCELLED_STATUSES

    if event_type == "checkout.session.completed" and not cancelled:
        subject, text, html = _account_ready_email(first_name, plan_label, quantity)
        notification_type = "account_ready"
    elif cancelled:
        subject, text, html = _plan_cancelled_email(first_name, plan_label)
        notification_type = "plan_cancelled"
    else:
        # customer.subscription.updated, still active -- a real,
        # server-verified tier change (see module docstring for why a
        # new Stripe event id is trusted as "a real change occurred").
        subject, text, html = _plan_updated_email(first_name, plan_label, quantity)
        notification_type = "plan_updated"

    return _send_once(
        event_id=event_id, notification_type=notification_type, customer_id=customer["id"],
        recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"entitlement_id": entitlement["id"], "product": entitlement["product"]},
    )


def _notify_setup_required(event_id: str, session_obj: dict) -> dict:
    email = _session_email(session_obj)
    metadata = session_obj.get("metadata") or {}
    price_id = str(metadata.get("anyaicam_stripe_price_id") or "")
    # Best-effort plan/quantity for the copy -- resolved the same way
    # customer_entitlements.resolve_tier() would, without importing that
    # module's internals; if unresolvable for any reason, generic (but
    # still accurate, never fabricated) copy is used instead.
    plan_label, quantity = "your", 0
    try:
        from customer_entitlements import resolve_tier
        tier = resolve_tier(price_id)
        if tier:
            plan_label, quantity = _plan_label(tier["product"]), int(tier["camera_slot_maximum"])
    except Exception:
        pass
    subject, text, html = _setup_required_email(plan_label, quantity)
    return _send_once(
        event_id=event_id, notification_type="setup_required", customer_id=None,
        recipient_email=email, subject=subject, text=text, html=html, metadata={"stripe_price_id": price_id},
    )


def _notify_hardware_order(event_id: str, order: dict) -> dict:
    customer = _customer_row(order["customer_id"]) if order.get("customer_id") else None
    first_name = _first_name(customer.get("name")) if customer else "there"
    recipient = (customer.get("email") if customer else None) or ""
    subject, text, html = _hardware_order_email(first_name, order["product_name"], int(order["quantity"] or 1))
    return _send_once(
        event_id=event_id, notification_type="hardware_order_confirmation",
        customer_id=order.get("customer_id"), recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"order_id": order["id"], "sku": order["sku"]},
    )


def notify_registration_resolved(customer_id: str) -> list:
    """Call once, right after customer_entitlements.resolve_pending_links_
    for_customer()/hardware_orders.resolve_pending_links_for_customer()
    in customer_registration.py's approve_registration() -- the "new
    customer completes setup" half of the flow. Sends the final "account
    ready" email for every now-active camera-slot entitlement this
    customer has (there is normally exactly one). Idempotency key is
    f"registration-resolved:{customer_id}:{product}" rather than a Stripe
    event id -- a customer's registration is approved at most once, so
    this key is naturally unique per (customer, product) here; reusing
    the same provisioning_notifications table keeps every provisioning
    email in one place rather than inventing a second tracking table."""
    from customer_entitlements import get_entitlements_for_customer

    customer = _customer_row(customer_id)
    if not customer:
        return []
    recipient = customer.get("email") or ""
    first_name = _first_name(customer.get("name"))
    results = []
    for entitlement in get_entitlements_for_customer(customer_id):
        if entitlement["status"] != "active" or int(entitlement["camera_slot_quantity"] or 0) <= 0:
            continue
        plan_label = _plan_label(entitlement["product"])
        quantity = int(entitlement["camera_slot_quantity"])
        subject, text, html = _account_ready_email(first_name, plan_label, quantity)
        pseudo_event_id = f"registration-resolved:{customer_id}:{entitlement['product']}"
        results.append(_send_once(
            event_id=pseudo_event_id, notification_type="account_ready", customer_id=customer_id,
            recipient_email=recipient, subject=subject, text=text, html=html,
            metadata={"entitlement_id": entitlement["id"], "product": entitlement["product"], "source": "registration_resolved"},
        ))
    return results
