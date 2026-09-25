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


SUPPORT_EMAIL = "amata@anyaicam.com"
SUPPORT_LINK = "https://anyaicam.com/support.html"
SIGN_IN_LINK = "https://app.anyaicam.com"
# Staging draft URLs -- these pages are NOT published live yet (see
# website-pricing-review/staging/policies/); update once the real,
# reviewed policy pages are published.
SHIPPING_POLICY_LINK = "https://anyaicam.com/shipping-policy.html"
RETURN_REFUND_POLICY_LINK = "https://anyaicam.com/hardware-return-refund-policy.html"
CANCELLATION_POLICY_LINK = "https://anyaicam.com/cancellation-policy.html"

_SUPPORT_FOOTER_TEXT = f"Questions? Contact us at {SUPPORT_EMAIL} or visit {SUPPORT_LINK}.\n\n— The AnyAiCam Team"
_SUPPORT_FOOTER_HTML = f'<p>Questions? Contact us at <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a> or visit <a href="{SUPPORT_LINK}">{SUPPORT_LINK}</a>.</p><p>— The AnyAiCam Team</p>'


def _format_amount(amount_cents: int) -> str:
    return f"${amount_cents / 100:,.2f}"


def _format_date(iso_timestamp: str) -> str:
    try:
        return datetime.fromisoformat(iso_timestamp).strftime("%B %d, %Y")
    except (TypeError, ValueError):
        return iso_timestamp or ""


def _hardware_order_email(first_name: str, order: dict) -> tuple[str, str, str]:
    """Order-confirmation email -- sent immediately after a successful
    hardware payment/order record, per this phase's explicit content
    requirements. Deliberately never claims camera slots are active --
    hardware and camera-slot subscriptions remain separate (see this
    module's and hardware_orders.py's own separation contract)."""
    from hardware_fulfillment import PREPARATION_TIMEFRAME_TEXT, generate_order_number
    subject = "Your AnyAiCam hardware order is confirmed"
    quantity = int(order["quantity"] or 1)
    qty_text = f"{quantity} × {order['product_name']}" if quantity != 1 else order["product_name"]
    order_number = generate_order_number(order["id"])
    amount = _format_amount(order["amount_cents"])
    purchase_date = _format_date(order["created_at"])
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam hardware order is confirmed.\n\n"
        f"Order number: {order_number}\n"
        f"Product: {qty_text}\n"
        f"Amount paid: {amount}\n"
        f"Date purchased: {purchase_date}\n\n"
        f"{PREPARATION_TIMEFRAME_TEXT}\n\n"
        f"Before shipment, AnyAiCam prepares, installs and configures the VMS software on your appliance, "
        f"and tests it -- this ensures it's ready to use the moment it arrives.\n\n"
        f"Manage your account: {SIGN_IN_LINK}\n\n"
        f"Please note: purchasing hardware does not activate camera slots on your account. Camera-slot "
        f"service (Local or Hybrid plans) is purchased and managed separately.\n\n"
        f"Shipping, return, and refund terms: {SHIPPING_POLICY_LINK} / {RETURN_REFUND_POLICY_LINK}\n\n"
        f"{_SUPPORT_FOOTER_TEXT}"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam hardware order is confirmed.</p>"
        f"<p><strong>Order number:</strong> {order_number}<br>"
        f"<strong>Product:</strong> {qty_text}<br>"
        f"<strong>Amount paid:</strong> {amount}<br>"
        f"<strong>Date purchased:</strong> {purchase_date}</p>"
        f"<p>{PREPARATION_TIMEFRAME_TEXT}</p>"
        f"<p>Before shipment, AnyAiCam prepares, installs and configures the VMS software on your appliance, "
        f"and tests it — this ensures it's ready to use the moment it arrives.</p>"
        f'<p>Manage your account: <a href="{SIGN_IN_LINK}">{SIGN_IN_LINK}</a></p>'
        f"<p>Please note: purchasing hardware does not activate camera slots on your account. Camera-slot "
        f"service (Local or Hybrid plans) is purchased and managed separately.</p>"
        f'<p>Shipping, return, and refund terms: <a href="{SHIPPING_POLICY_LINK}">Shipping Policy</a> / '
        f'<a href="{RETURN_REFUND_POLICY_LINK}">Hardware Return &amp; Refund Policy</a></p>'
        f"{_SUPPORT_FOOTER_HTML}"
    )
    return subject, text, html


def _hardware_shipped_email(first_name: str, order: dict) -> tuple[str, str, str]:
    from hardware_fulfillment import generate_order_number
    subject = "Your AnyAiCam order has shipped"
    order_number = generate_order_number(order["id"])
    lines_text = [f"Order number: {order_number}", f"Product: {order['product_name']}"]
    lines_html = [f"<strong>Order number:</strong> {order_number}", f"<strong>Product:</strong> {order['product_name']}"]
    if order.get("carrier"):
        lines_text.append(f"Carrier: {order['carrier']}")
        lines_html.append(f"<strong>Carrier:</strong> {order['carrier']}")
    if order.get("tracking_number"):
        lines_text.append(f"Tracking number: {order['tracking_number']}")
        lines_html.append(f"<strong>Tracking number:</strong> {order['tracking_number']}")
    if order.get("tracking_link"):
        lines_text.append(f"Track your shipment: {order['tracking_link']}")
        lines_html.append(f'<strong>Track your shipment:</strong> <a href="{order["tracking_link"]}">{order["tracking_link"]}</a>')
    if order.get("shipped_at"):
        lines_text.append(f"Date shipped: {_format_date(order['shipped_at'])}")
        lines_html.append(f"<strong>Date shipped:</strong> {_format_date(order['shipped_at'])}")
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam order has shipped.\n\n"
        + "\n".join(lines_text) + "\n\n"
        f"When your appliance arrives, sign in to your AnyAiCam account to complete setup and connect your cameras.\n\n"
        f"Sign in: {SIGN_IN_LINK}\n\n"
        f"{_SUPPORT_FOOTER_TEXT}"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam order has shipped.</p>"
        f"<p>{'<br>'.join(lines_html)}</p>"
        f"<p>When your appliance arrives, sign in to your AnyAiCam account to complete setup and connect your cameras.</p>"
        f'<p>Sign in: <a href="{SIGN_IN_LINK}">{SIGN_IN_LINK}</a></p>'
        f"{_SUPPORT_FOOTER_HTML}"
    )
    return subject, text, html


def _hardware_cancellation_email(first_name: str, order: dict) -> tuple[str, str, str]:
    from hardware_fulfillment import generate_order_number
    subject = "Your AnyAiCam cancellation has been received"
    order_number = generate_order_number(order["id"])
    already_shipped = order["fulfillment_status"] == "cancelled" and bool(order.get("shipped_at"))
    shipped_line = "This order had already shipped before cancellation." if already_shipped else "This order had not yet shipped."
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam cancellation request has been received and processed.\n\n"
        f"Order number: {order_number}\n"
        f"Product: {order['product_name']}\n"
        f"Cancellation status: Cancelled\n"
        f"{shipped_line}\n\n"
        f"Next step: our team will review your order and follow up on any applicable refund. Refund amounts, "
        f"if any, are calculated according to our published policies and will be confirmed separately -- we "
        f"are not able to state a specific refund amount in this message.\n\n"
        f"Applicable policy: {CANCELLATION_POLICY_LINK} / {RETURN_REFUND_POLICY_LINK}\n\n"
        f"{_SUPPORT_FOOTER_TEXT}"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam cancellation request has been received and processed.</p>"
        f"<p><strong>Order number:</strong> {order_number}<br>"
        f"<strong>Product:</strong> {order['product_name']}<br>"
        f"<strong>Cancellation status:</strong> Cancelled<br>{shipped_line}</p>"
        f"<p>Next step: our team will review your order and follow up on any applicable refund. Refund amounts, "
        f"if any, are calculated according to our published policies and will be confirmed separately — we "
        f"are not able to state a specific refund amount in this message.</p>"
        f'<p>Applicable policy: <a href="{CANCELLATION_POLICY_LINK}">Cancellation Policy</a> / '
        f'<a href="{RETURN_REFUND_POLICY_LINK}">Hardware Return &amp; Refund Policy</a></p>'
        f"{_SUPPORT_FOOTER_HTML}"
    )
    return subject, text, html


def _return_authorized_email(first_name: str, order: dict, hardware_return: dict) -> tuple[str, str, str]:
    from hardware_fulfillment import generate_order_number
    subject = "Your AnyAiCam return request has been approved"
    order_number = generate_order_number(order["id"])
    reference = hardware_return.get("return_reference") or "(pending assignment)"
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam return request has been approved.\n\n"
        f"Order number: {order_number}\n"
        f"Product: {order['product_name']}\n"
        f"Return authorization number: {reference}\n\n"
        f"Please include all original accessories and packaging where possible. Our support team will follow up "
        f"with return shipping instructions if they were not already provided.\n\n"
        f"Once received, your returned equipment will be inspected. The final refund amount is determined "
        f"according to our published Hardware Return & Refund Policy, including any applicable restocking fee.\n\n"
        f"Policy: {RETURN_REFUND_POLICY_LINK}\n\n"
        f"{_SUPPORT_FOOTER_TEXT}"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam return request has been approved.</p>"
        f"<p><strong>Order number:</strong> {order_number}<br>"
        f"<strong>Product:</strong> {order['product_name']}<br>"
        f"<strong>Return authorization number:</strong> {reference}</p>"
        f"<p>Please include all original accessories and packaging where possible. Our support team will follow up "
        f"with return shipping instructions if they were not already provided.</p>"
        f"<p>Once received, your returned equipment will be inspected. The final refund amount is determined "
        f"according to our published Hardware Return &amp; Refund Policy, including any applicable restocking fee.</p>"
        f'<p>Policy: <a href="{RETURN_REFUND_POLICY_LINK}">{RETURN_REFUND_POLICY_LINK}</a></p>'
        f"{_SUPPORT_FOOTER_HTML}"
    )
    return subject, text, html


def _return_received_email(first_name: str, order: dict) -> tuple[str, str, str]:
    from hardware_fulfillment import generate_order_number
    subject = "We received your AnyAiCam return"
    order_number = generate_order_number(order["id"])
    text = (
        f"Hi {first_name},\n\n"
        f"We've received your returned AnyAiCam equipment.\n\n"
        f"Order number: {order_number}\n"
        f"Product: {order['product_name']}\n\n"
        f"Inspection is now pending. Your final refund amount will be confirmed once inspection is complete -- "
        f"we are not able to confirm a final refund amount yet.\n\n"
        f"{_SUPPORT_FOOTER_TEXT}"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>We've received your returned AnyAiCam equipment.</p>"
        f"<p><strong>Order number:</strong> {order_number}<br><strong>Product:</strong> {order['product_name']}</p>"
        f"<p>Inspection is now pending. Your final refund amount will be confirmed once inspection is complete — "
        f"we are not able to confirm a final refund amount yet.</p>"
        f"{_SUPPORT_FOOTER_HTML}"
    )
    return subject, text, html


def _refund_processed_email(first_name: str, order: dict, hardware_return: dict) -> tuple[str, str, str]:
    from hardware_fulfillment import generate_order_number
    subject = "Your AnyAiCam refund has been processed"
    order_number = generate_order_number(order["id"])
    original = _format_amount(hardware_return["original_amount_cents"])
    fee_cents = hardware_return.get("restocking_fee_cents") or 0
    fee = _format_amount(fee_cents)
    final_amount = _format_amount(hardware_return["approved_refund_cents"])
    refund_date = _format_date(hardware_return.get("refunded_at") or "")
    text = (
        f"Hi {first_name},\n\n"
        f"Your AnyAiCam refund has been processed.\n\n"
        f"Order number: {order_number}\n"
        f"Original purchase amount: {original}\n"
        f"Restocking fee: {fee}\n"
        f"Final refund amount: {final_amount}\n"
        f"Refund date: {refund_date}\n\n"
        f"Bank/card posting times vary by financial institution and can take several business days to appear.\n\n"
        f"{_SUPPORT_FOOTER_TEXT}"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Your AnyAiCam refund has been processed.</p>"
        f"<p><strong>Order number:</strong> {order_number}<br>"
        f"<strong>Original purchase amount:</strong> {original}<br>"
        f"<strong>Restocking fee:</strong> {fee}<br>"
        f"<strong>Final refund amount:</strong> {final_amount}<br>"
        f"<strong>Refund date:</strong> {refund_date}</p>"
        f"<p>Bank/card posting times vary by financial institution and can take several business days to appear.</p>"
        f"{_SUPPORT_FOOTER_HTML}"
    )
    return subject, text, html


def _getting_started_email(first_name: str, camera_slot_summary: Optional[tuple[str, int]]) -> tuple[str, str, str]:
    subject = "Get started with your AnyAiCam system"
    capacity_text = ""
    capacity_html = ""
    if camera_slot_summary:
        plan_label, quantity = camera_slot_summary
        capacity_text = f"Your {plan_label} plan supports up to {quantity} cameras.\n\n"
        capacity_html = f"<p>Your {plan_label} plan supports up to {quantity} cameras.</p>"
    text = (
        f"Hi {first_name},\n\n"
        f"Let's get your AnyAiCam system set up.\n\n"
        f"1. Sign in: {SIGN_IN_LINK}\n"
        f"2. Connect your appliance using the Cloud ID and activation code provided with your device.\n"
        f"3. Add and configure your cameras from the dashboard.\n\n"
        f"{capacity_text}"
        f"{_SUPPORT_FOOTER_TEXT}"
    )
    html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Let's get your AnyAiCam system set up.</p>"
        f'<ol><li>Sign in: <a href="{SIGN_IN_LINK}">{SIGN_IN_LINK}</a></li>'
        f"<li>Connect your appliance using the Cloud ID and activation code provided with your device.</li>"
        f"<li>Add and configure your cameras from the dashboard.</li></ol>"
        f"{capacity_html}"
        f"{_SUPPORT_FOOTER_HTML}"
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
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
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
    subject, text, html = _hardware_order_email(first_name, order)
    return _send_once(
        event_id=event_id, notification_type="hardware_order_confirmation",
        customer_id=order.get("customer_id"), recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"order_id": order["id"], "sku": order["sku"]},
    )


# ------------------------------------------------- fulfillment/return triggers
#
# Unlike notify_from_stripe_event() above, these are triggered by ADMIN
# ACTIONS (marking an order shipped, authorizing a return, ...), not by a
# Stripe webhook delivery -- there is no stripe_event_id to key
# idempotency off of. Each uses a synthetic, deterministic key
# (f"hardware-<action>:<row-id>") in the SAME provisioning_notifications
# table/(_send_once) infrastructure the Stripe-driven path uses -- one
# action (e.g. hardware_fulfillment.mark_shipped()) can only ever
# generate one email, called twice or not, exactly like the webhook path.


def notify_hardware_shipped(order_id: str) -> dict:
    order = row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))
    if not order:
        return {"status": "ignored", "reason": "unknown order"}
    customer = _customer_row(order["customer_id"]) if order.get("customer_id") else None
    first_name = _first_name(customer.get("name")) if customer else "there"
    recipient = (customer.get("email") if customer else None) or ""
    subject, text, html = _hardware_shipped_email(first_name, order)
    return _send_once(
        event_id=f"hardware-shipped:{order_id}", notification_type="hardware_shipped",
        customer_id=order.get("customer_id"), recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"order_id": order_id},
    )


def notify_hardware_cancellation(order_id: str) -> dict:
    order = row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))
    if not order:
        return {"status": "ignored", "reason": "unknown order"}
    customer = _customer_row(order["customer_id"]) if order.get("customer_id") else None
    first_name = _first_name(customer.get("name")) if customer else "there"
    recipient = (customer.get("email") if customer else None) or ""
    subject, text, html = _hardware_cancellation_email(first_name, order)
    return _send_once(
        event_id=f"hardware-cancelled:{order_id}", notification_type="hardware_cancellation",
        customer_id=order.get("customer_id"), recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"order_id": order_id},
    )


def notify_return_authorized(return_id: str) -> dict:
    hardware_return = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not hardware_return:
        return {"status": "ignored", "reason": "unknown return"}
    order = row("SELECT * FROM hardware_orders WHERE id=?", (hardware_return["order_id"],))
    if not order:
        return {"status": "ignored", "reason": "unknown order"}
    customer = _customer_row(hardware_return["customer_id"]) if hardware_return.get("customer_id") else None
    first_name = _first_name(customer.get("name")) if customer else "there"
    recipient = (customer.get("email") if customer else None) or ""
    subject, text, html = _return_authorized_email(first_name, order, hardware_return)
    return _send_once(
        event_id=f"return-authorized:{return_id}", notification_type="return_authorized",
        customer_id=hardware_return.get("customer_id"), recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"return_id": return_id, "order_id": order["id"]},
    )


def notify_return_received(return_id: str) -> dict:
    hardware_return = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not hardware_return:
        return {"status": "ignored", "reason": "unknown return"}
    order = row("SELECT * FROM hardware_orders WHERE id=?", (hardware_return["order_id"],))
    if not order:
        return {"status": "ignored", "reason": "unknown order"}
    customer = _customer_row(hardware_return["customer_id"]) if hardware_return.get("customer_id") else None
    first_name = _first_name(customer.get("name")) if customer else "there"
    recipient = (customer.get("email") if customer else None) or ""
    subject, text, html = _return_received_email(first_name, order)
    return _send_once(
        event_id=f"return-received:{return_id}", notification_type="return_received",
        customer_id=hardware_return.get("customer_id"), recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"return_id": return_id, "order_id": order["id"]},
    )


def notify_refund_processed(return_id: str) -> dict:
    hardware_return = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not hardware_return:
        return {"status": "ignored", "reason": "unknown return"}
    order = row("SELECT * FROM hardware_orders WHERE id=?", (hardware_return["order_id"],))
    if not order:
        return {"status": "ignored", "reason": "unknown order"}
    customer = _customer_row(hardware_return["customer_id"]) if hardware_return.get("customer_id") else None
    first_name = _first_name(customer.get("name")) if customer else "there"
    recipient = (customer.get("email") if customer else None) or ""
    subject, text, html = _refund_processed_email(first_name, order, hardware_return)
    return _send_once(
        event_id=f"refund-processed:{return_id}", notification_type="refund_processed",
        customer_id=hardware_return.get("customer_id"), recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"return_id": return_id, "order_id": order["id"]},
    )


def send_getting_started_email(customer_id: str) -> dict:
    """Standalone, admin/support-triggered (e.g. once a shipped
    appliance's activation is confirmed) -- never fired automatically on
    every entitlement change. Camera capacity is included ONLY if the
    entitlement backend confirms an active Local/Hybrid entitlement for
    this customer right now -- never claimed from hardware purchase
    alone. Idempotency key is per-customer (this message makes sense to
    send at most once per customer in the normal case); call again
    deliberately if a genuine resend is needed -- there is no Stripe
    event or fulfillment action this ties to."""
    customer = _customer_row(customer_id)
    if not customer:
        return {"status": "ignored", "reason": "unknown customer"}
    first_name = _first_name(customer.get("name"))
    recipient = customer.get("email") or ""
    from customer_entitlements import get_entitlements_for_customer
    camera_slot_summary = None
    for entitlement in get_entitlements_for_customer(customer_id):
        if entitlement["status"] == "active" and int(entitlement["camera_slot_quantity"] or 0) > 0:
            camera_slot_summary = (_plan_label(entitlement["product"]), int(entitlement["camera_slot_quantity"]))
            break
    subject, text, html = _getting_started_email(first_name, camera_slot_summary)
    return _send_once(
        event_id=f"getting-started:{customer_id}", notification_type="getting_started",
        customer_id=customer_id, recipient_email=recipient, subject=subject, text=text, html=html,
        metadata={"camera_slot_summary": bool(camera_slot_summary)},
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
