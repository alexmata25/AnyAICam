"""Provisioning Phase 8: hardware RETURN and refund workflow, separate
from hardware_fulfillment.py's pre-shipment order lifecycle.

Lifecycle (hardware_returns.status):
    return_requested -> return_authorized -> return_in_transit
        -> return_received -> inspection
        -> refund_approved / refund_adjusted / refund_denied
        -> refunded

This module NEVER issues a real refund (no Stripe refund API call
anywhere in this file) -- approve_refund() only records an admin's
explicit approval of a CALCULATED amount, and finalize_refund() only
records that a refund was carried out through whatever separate,
explicitly-authorized process actually executes it. That is a deliberate
scope boundary for this phase, not an oversight: "do not issue any real
refunds" was an explicit instruction.

Restocking fee -- NOT invented
--------------------------------
ANYAICAM_HARDWARE_RESTOCKING_PERCENT and ANYAICAM_HARDWARE_RETURN_WINDOW_
DAYS are both read from the environment with NO fallback default. Unset
means exactly that: no restocking-fee policy or return-window policy has
been approved yet. calculate_refund() below is fully usable in that
state -- it returns a $0.00 restocking fee (the original amount in full)
and an explicit `configured: False` flag an admin UI/report must show,
so "no fee is being applied" is never silently indistinguishable from "a
$0 fee was deliberately approved." Once Alejandro approves a real
percentage, setting the env var is the only change needed here -- no
code change, no invented number in the meantime.

The restocking fee is deliberately the ONE named customer-facing
deduction this module ever computes. No "processing fee"/"bank fee"/
"Stripe fee" is invented anywhere in this file -- see the module's
refund_breakdown() docstring for where actual processor costs, if ever
tracked, would live (recorded separately, never surfaced to the
customer, never subtracted from their refund without a separately
approved, legally-reviewed policy).

Separation from camera-slot entitlements
-------------------------------------------
Nothing in this module ever imports or calls customer_entitlements.py.
A hardware return/refund can never change camera_slot_quantity for any
product, in either direction -- proven in test_hardware_returns.py.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Optional

from partner_db import connection, row, rows

RETURN_STATES = [
    "return_requested", "return_authorized", "return_in_transit", "return_received",
    "inspection", "refund_approved", "refund_adjusted", "refund_denied", "refunded",
]

# Transitions allowed FROM each state (a dict of sets) -- fail closed on
# anything not explicitly listed here, rather than allowing an arbitrary
# jump (e.g. straight from return_requested to refunded, skipping
# inspection entirely -- exactly what the task explicitly disallows).
_ALLOWED_NEXT = {
    "return_requested": {"return_authorized", "refund_denied"},
    "return_authorized": {"return_in_transit", "refund_denied"},
    "return_in_transit": {"return_received"},
    "return_received": {"inspection"},
    "inspection": {"refund_approved", "refund_adjusted", "refund_denied"},
    "refund_approved": {"refunded"},
    "refund_adjusted": {"refunded"},
    "refund_denied": set(),  # terminal
    "refunded": set(),  # terminal
}


def _now() -> str:
    return datetime.now().isoformat()


def restocking_fee_percent() -> Optional[float]:
    """None means "not configured" -- never a guessed default. See
    module docstring."""
    raw = os.environ.get("ANYAICAM_HARDWARE_RESTOCKING_PERCENT", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return max(0.0, min(100.0, value))


def return_window_days() -> Optional[int]:
    """None means "not configured" -- request_return() below does not
    reject a request just because this is unset; it simply can't yet
    flag a request as outside an approved window. See module docstring."""
    raw = os.environ.get("ANYAICAM_HARDWARE_RETURN_WINDOW_DAYS", "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def calculate_refund(original_amount_cents: int) -> dict:
    """Pure calculation, callable at any time (e.g. to preview a refund
    before any return has even been requested) -- never mutates
    anything. `configured` distinguishes a deliberately-approved $0 fee
    (impossible today, since no percent has ever been approved) from
    "no policy exists yet, so nothing is being deducted."

    Deliberately does NOT subtract shipping, tax, or any payment-
    processor cost -- see the module docstring; only a restocking fee
    is ever computed here, and only when explicitly configured."""
    original_amount_cents = max(0, int(original_amount_cents))
    percent = restocking_fee_percent()
    if percent is None:
        return {
            "original_amount_cents": original_amount_cents,
            "restocking_fee_cents": 0,
            "restocking_fee_percent": None,
            "final_refund_cents": original_amount_cents,
            "configured": False,
        }
    fee_cents = round(original_amount_cents * percent / 100)
    fee_cents = min(fee_cents, original_amount_cents)  # a refund can never go negative
    return {
        "original_amount_cents": original_amount_cents,
        "restocking_fee_cents": fee_cents,
        "restocking_fee_percent": percent,
        "final_refund_cents": original_amount_cents - fee_cents,
        "configured": True,
    }


def _transition(return_id: str, new_status: str, **fields) -> dict:
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    allowed = _ALLOWED_NEXT.get(existing["status"], set())
    if new_status not in allowed:
        raise ValueError(f"Cannot move return status from '{existing['status']}' to '{new_status}'.")
    now = _now()
    set_clause = ",".join(f"{key}=?" for key in fields) + ("," if fields else "")
    with connection() as db:
        db.execute(
            f"UPDATE hardware_returns SET status=?,{set_clause}updated_at=? WHERE id=?",
            (new_status, *fields.values(), now, return_id),
        )
    return row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))


def request_return(order_id: str) -> dict:
    """The customer-initiated start of the return workflow -- only valid
    for an order that has actually SHIPPED (an unshipped order is
    cancelled outright via hardware_fulfillment.mark_cancelled(), never
    routed through this return process at all -- the mirror-image of
    that function's own guard)."""
    order = row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))
    if not order:
        raise ValueError(f"Unknown hardware order {order_id!r}")
    if order["fulfillment_status"] not in ("shipped", "delivered"):
        raise ValueError(
            f"Order {order_id!r} has not shipped (status={order['fulfillment_status']!r}) -- "
            "cancel it directly instead of requesting a return."
        )
    return_id = uuid.uuid4().hex
    now = _now()
    with connection() as db:
        db.execute(
            "INSERT INTO hardware_returns(id,order_id,customer_id,sku,status,requested_at,original_amount_cents,refund_status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (return_id, order_id, order.get("customer_id"), order["sku"], "return_requested", now, order["amount_cents"], "not_started", now, now),
        )
    return row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))


def authorize_return(return_id: str, *, return_reference: str) -> dict:
    result = _transition(return_id, "return_authorized", return_reference=return_reference, authorized_at=_now())
    from purchase_notifications import notify_return_authorized
    notify_return_authorized(return_id)
    return result


def deny_return(return_id: str) -> dict:
    return _transition(return_id, "refund_denied")


def mark_return_shipped_back(return_id: str) -> dict:
    return _transition(return_id, "return_in_transit")


def mark_return_received(return_id: str, *, condition_notes: str = "", accessories_included: str = "", damage_notes: str = "") -> dict:
    result = _transition(
        return_id, "return_received",
        received_at=_now(), condition_notes=condition_notes,
        accessories_included=accessories_included, damage_notes=damage_notes,
    )
    from purchase_notifications import notify_return_received
    notify_return_received(return_id)
    return result


def begin_inspection(return_id: str) -> dict:
    return _transition(return_id, "inspection")


def record_inspection_and_calculate_refund(return_id: str, *, serial_number: str = "", inspected_by: str) -> dict:
    """Moves 'return_received' -> 'inspection' and records the
    CALCULATED (not yet approved) restocking-fee preview via
    calculate_refund() -- an admin must still separately call
    approve_refund() (below) with explicit confirmation before this is
    final. Any manual adjustment for damage/missing accessories beyond
    the standard restocking fee is an admin decision made when calling
    approve_refund() (its override_final_refund_cents), never decided
    here."""
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    calculation = calculate_refund(existing["original_amount_cents"])
    return _transition(
        return_id, "inspection",
        serial_number=serial_number, inspected_at=_now(), inspected_by=inspected_by,
        restocking_fee_cents=calculation["restocking_fee_cents"],
        restocking_fee_percent_applied=calculation["restocking_fee_percent"],
    )


def preview_refund(return_id: str) -> dict:
    """Read-only: what approve_refund() WOULD record if called right
    now. This is the "display the calculated refund amount to an
    administrator BEFORE executing a refund" requirement -- callable any
    number of times, changes nothing."""
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    return calculate_refund(existing["original_amount_cents"])


def approve_refund(return_id: str, *, approved_by: str, override_final_refund_cents: Optional[int] = None) -> dict:
    """Requires an explicit admin identity (`approved_by`) -- there is
    no code path that reaches 'refund_approved'/'refund_adjusted'
    without one. `override_final_refund_cents` exists ONLY for a
    documented manual adjustment (e.g. inspection found damage) -- when
    omitted, the standard calculate_refund() figure is used unchanged.
    Still does not call Stripe or move any money -- see module
    docstring."""
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    if not approved_by:
        raise ValueError("approve_refund() requires an explicit admin identity (approved_by).")
    calculation = calculate_refund(existing["original_amount_cents"])
    final_cents = calculation["final_refund_cents"] if override_final_refund_cents is None else max(0, int(override_final_refund_cents))
    status = "refund_adjusted" if override_final_refund_cents is not None else "refund_approved"
    return _transition(
        return_id, status,
        approved_refund_cents=final_cents,
        restocking_fee_cents=calculation["restocking_fee_cents"],
        restocking_fee_percent_applied=calculation["restocking_fee_percent"],
        refund_status="approved",
        refund_approved_by=approved_by, refund_approved_at=_now(),
    )


def finalize_refund(return_id: str) -> dict:
    """Records that a refund was ACTUALLY carried out -- via whatever
    separate, explicitly-authorized real-Stripe-refund process the
    business runs; this function performs no such call itself. Sends the
    refund-processed email."""
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    if existing["status"] not in ("refund_approved", "refund_adjusted"):
        raise ValueError(f"Cannot finalize a refund in status '{existing['status']}' -- must be approved or adjusted first.")
    result = _transition(return_id, "refunded", refund_status="refunded", refunded_at=_now())
    from purchase_notifications import notify_refund_processed
    notify_refund_processed(return_id)
    return result


def get_return(return_id: str) -> Optional[dict]:
    return row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))


def get_returns_for_order(order_id: str) -> list:
    return rows("SELECT * FROM hardware_returns WHERE order_id=? ORDER BY created_at DESC", (order_id,))
