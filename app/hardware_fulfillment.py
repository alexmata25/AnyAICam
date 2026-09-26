"""Provisioning Phase 8: hardware order FULFILLMENT lifecycle.

AnyAiCam does not stock large quantities of Ryzen appliances -- a
purchase triggers AnyAiCam to source, receive, install/configure the VMS
software onto, test, package, and ship the specific unit. This module
models that lifecycle on top of hardware_orders.py's existing order
record (hardware_orders.fulfillment_status), and is the ONLY place that
decides which lifecycle transitions are customer-email-worthy.

Lifecycle (hardware_orders.fulfillment_status):
    paid -> preparing -> configuring -> testing -> ready_to_ship
         -> shipped -> delivered
    (or, from any pre-shipped state) -> cancelled

Not every step sends an email -- see FULFILLMENT_STATES below and the
task's own explicit minimum: order confirmed, shipped, cancelled, return
received, refund processed. advance_fulfillment_status() accepts any
valid transition silently; only mark_shipped() and mark_cancelled() also
trigger a customer email, via purchase_notifications.py's existing
send-once infrastructure (own idempotency key per order, independent of
the Stripe-event-keyed notifications the checkout/webhook flow uses --
these are ADMIN-triggered actions, not Stripe events).

Preparation timeframe: HARDWARE_PREPARATION_MAX_DAYS, approved at 14
days per the task's explicit instruction -- ships with that real
default, not a placeholder, unlike the restocking fee / return window in
hardware_returns.py (both still awaiting Alejandro's decision, see that
module's docstring).

Never touches customer_entitlements or camera-slot logic in any way --
see the module docstring of purchase_notifications.py and hardware_
orders.py for the same, now-familiar separation contract. A hardware
order shipping, being cancelled, or being returned/refunded can never
change a customer's camera-slot count.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Optional

from partner_db import connection, row

HARDWARE_PREPARATION_MAX_DAYS = int(os.environ.get("ANYAICAM_HARDWARE_PREPARATION_MAX_DAYS", "14") or "14")

PREPARATION_TIMEFRAME_TEXT = f"Please allow up to {HARDWARE_PREPARATION_MAX_DAYS} days for preparation and shipment."

# Ordered lifecycle -- index order matters for advance_fulfillment_status()'s
# "no going backward" guard. 'cancelled' is a terminal side-branch, not a
# position in this sequence -- reachable from any pre-shipped state, and
# nothing is ever reachable FROM it.
FULFILLMENT_STATES = ["paid", "preparing", "configuring", "testing", "ready_to_ship", "shipped", "delivered"]
CANCELLABLE_STATES = {"paid", "preparing", "configuring", "testing", "ready_to_ship"}
EMAIL_WORTHY_TRANSITIONS = {"shipped", "cancelled"}  # 'paid' (order confirmation) is sent at order-creation time, not here


def _now() -> str:
    return datetime.now().isoformat()


def generate_order_number(order_id: str) -> str:
    """Human-friendly order number for customer-facing text -- derived
    deterministically from the order's own id, never a second random
    value that would need its own uniqueness check."""
    return f"AIC-HW-{order_id[:8].upper()}"


def advance_fulfillment_status(order_id: str, new_status: str) -> dict:
    """Moves an order forward in FULFILLMENT_STATES, or terminates it
    into 'cancelled' from any cancellable state. Refuses to move
    backward or to skip from/into 'cancelled' incorrectly -- fail closed
    on an invalid transition rather than silently accepting it."""
    order = row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))
    if not order:
        raise ValueError(f"Unknown hardware order {order_id!r}")
    current = order["fulfillment_status"] or "paid"

    if new_status == "cancelled":
        if current not in CANCELLABLE_STATES:
            raise ValueError(f"Cannot cancel an order already '{current}'.")
    elif new_status in FULFILLMENT_STATES:
        if current == "cancelled":
            raise ValueError("Cannot advance a cancelled order.")
        if current not in FULFILLMENT_STATES or FULFILLMENT_STATES.index(new_status) <= FULFILLMENT_STATES.index(current):
            raise ValueError(f"Cannot move fulfillment status from '{current}' to '{new_status}' (not forward).")
    else:
        raise ValueError(f"Unknown fulfillment status {new_status!r}")

    now = _now()
    with connection() as db:
        db.execute("UPDATE hardware_orders SET fulfillment_status=?,updated_at=? WHERE id=?", (new_status, now, order_id))
        if new_status == "cancelled":
            db.execute("UPDATE hardware_orders SET cancelled_at=? WHERE id=?", (now, order_id))
        if new_status == "delivered":
            db.execute("UPDATE hardware_orders SET delivered_at=? WHERE id=?", (now, order_id))
    return row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))


def mark_shipped(order_id: str, *, carrier: str, tracking_number: str, tracking_link: Optional[str] = None) -> dict:
    """Advances to 'shipped', records carrier/tracking, and sends the
    shipping email (send-once per order, see purchase_notifications.
    notify_hardware_shipped())."""
    now = _now()
    with connection() as db:
        db.execute(
            "UPDATE hardware_orders SET fulfillment_status='shipped',carrier=?,tracking_number=?,tracking_link=?,shipped_at=?,updated_at=? WHERE id=?",
            (carrier, tracking_number, tracking_link, now, now, order_id),
        )
    order = row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))
    if not order:
        raise ValueError(f"Unknown hardware order {order_id!r}")
    from purchase_notifications import notify_hardware_shipped
    notify_hardware_shipped(order_id)
    return order


def mark_cancelled(order_id: str) -> dict:
    """Cancels an order NOT YET SHIPPED and sends the cancellation-
    received email. A 'shipped' or 'delivered' order must go through
    hardware_returns.py's return workflow instead -- see that module's
    request_return(), which explicitly refuses to accept a return
    request for an order that was never shipped, the mirror-image
    refusal of this function's CANCELLABLE_STATES guard."""
    order = advance_fulfillment_status(order_id, "cancelled")
    from purchase_notifications import notify_hardware_cancellation
    notify_hardware_cancellation(order_id)
    return order
