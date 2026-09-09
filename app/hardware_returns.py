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

Restocking fee -- approved values, staging only
--------------------------------------------------
ANYAICAM_HARDWARE_RESTOCKING_PERCENT (15) and ANYAICAM_HARDWARE_RETURN_
WINDOW_DAYS (30) are now approved and set in deploy/.env.staging.example
-- STAGING ONLY; neither has been carried into a live/production env
file, and neither should be until the customer-facing policy has
completed legal review and Alejandro has separately approved publishing
it. Both are still read from the environment with NO code-level default
(see restocking_fee_percent()/return_window_days()) -- an environment
that doesn't set them still gets `configured: False` / "no window
enforced" behavior, exactly as before this pass, never a hard-coded 15
or 30 baked into this module itself.

Approved exception: no restocking fee applies when the hardware was
defective due to AnyAiCam or arrived damaged -- see calculate_refund()'s
`waive_restocking_fee` parameter and record_inspection_and_calculate_
refund()'s `defective_or_damaged_on_arrival` flag, an explicit admin
determination made at inspection time, never inferred from free-text
notes.

The restocking fee is deliberately the ONE named customer-facing
deduction this module ever computes. No "processing fee"/"bank fee"/
"Stripe fee" is invented anywhere in this file -- actual processor
costs, if ever tracked, would live recorded separately, never surfaced
to the customer, never subtracted from their refund without a
separately approved, legally-reviewed policy.

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


def calculate_refund(original_amount_cents: int, *, waive_restocking_fee: bool = False) -> dict:
    """Pure calculation, callable at any time (e.g. to preview a refund
    before any return has even been requested) -- never mutates
    anything. `configured` distinguishes a deliberately-approved fee
    percent from "no policy exists yet, so nothing is being deducted."

    `waive_restocking_fee` is the approved defective/damaged-on-arrival
    exception: when the hardware was defective due to AnyAiCam or
    arrived damaged, no restocking fee applies, full stop -- regardless
    of what percent is configured. Set this from a return's own
    recorded is_defective_or_damaged_on_arrival flag (see record_
    inspection_and_calculate_refund()), never guessed here. `fee_waived`
    in the result distinguishes "waived for cause" from "no policy
    configured yet" (`configured=False`) -- both produce a $0 fee, but
    for different, both-explicit reasons; a caller/report must never
    conflate the two.

    Deliberately does NOT subtract shipping, tax, or any payment-
    processor cost -- see the module docstring; only a restocking fee
    is ever computed here, and only when explicitly configured and not
    waived."""
    original_amount_cents = max(0, int(original_amount_cents))
    percent = restocking_fee_percent()
    if waive_restocking_fee:
        return {
            "original_amount_cents": original_amount_cents,
            "restocking_fee_cents": 0,
            "restocking_fee_percent": percent,  # what WOULD have applied, for audit/transparency
            "final_refund_cents": original_amount_cents,
            "configured": percent is not None,
            "fee_waived": True,
        }
    if percent is None:
        return {
            "original_amount_cents": original_amount_cents,
            "restocking_fee_cents": 0,
            "restocking_fee_percent": None,
            "final_refund_cents": original_amount_cents,
            "configured": False,
            "fee_waived": False,
        }
    # round-half-to-even (banker's rounding) on whole cents -- Python's
    # built-in round() on an already-integer-valued float division,
    # currency-safe: the operand here is always a whole number of cents
    # divided/multiplied by a plain percentage, never floating-point
    # dollars, so no accumulated fractional-cent drift is possible.
    fee_cents = round(original_amount_cents * percent / 100)
    fee_cents = min(fee_cents, original_amount_cents)  # a refund can never go negative
    return {
        "original_amount_cents": original_amount_cents,
        "restocking_fee_cents": fee_cents,
        "restocking_fee_percent": percent,
        "final_refund_cents": original_amount_cents - fee_cents,
        "configured": True,
        "fee_waived": False,
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
    that function's own guard).

    Enforces the approved return window (ANYAICAM_HARDWARE_RETURN_
    WINDOW_DAYS) when configured, measured from delivery if known,
    otherwise from shipment -- the customer's own clock for "when did I
    get this" starts at delivery when that's known. Unconfigured means
    exactly what return_window_days() documents: no automatic rejection
    is possible yet, so every request reaches the (still fully gated by
    inspection/admin-approval) workflow regardless of age."""
    order = row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))
    if not order:
        raise ValueError(f"Unknown hardware order {order_id!r}")
    if order["fulfillment_status"] not in ("shipped", "delivered"):
        raise ValueError(
            f"Order {order_id!r} has not shipped (status={order['fulfillment_status']!r}) -- "
            "cancel it directly instead of requesting a return."
        )
    window_days = return_window_days()
    if window_days is not None:
        reference = order.get("delivered_at") or order.get("shipped_at")
        if reference:
            try:
                elapsed_days = (datetime.now() - datetime.fromisoformat(reference)).days
            except (TypeError, ValueError):
                elapsed_days = None
            if elapsed_days is not None and elapsed_days > window_days:
                raise ValueError(
                    f"The {window_days}-day return window for order {order_id!r} has passed "
                    f"({elapsed_days} days since {'delivery' if order.get('delivered_at') else 'shipment'})."
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


def record_inspection_and_calculate_refund(
    return_id: str, *, serial_number: str = "", inspected_by: str, defective_or_damaged_on_arrival: bool = False,
) -> dict:
    """Moves 'return_received' -> 'inspection' and records the
    CALCULATED (not yet approved) restocking-fee preview via
    calculate_refund() -- an admin must still separately call
    approve_refund() (below) with explicit confirmation before this is
    final.

    `defective_or_damaged_on_arrival` is the admin's explicit,
    inspection-time determination of the approved fee-waiver exception
    (defective due to AnyAiCam, or arrived damaged) -- recorded on the
    return row so every later step (preview_refund(), approve_refund())
    applies it consistently without needing to be told again. Any OTHER
    manual adjustment (e.g. missing accessories) remains an admin
    decision made when calling approve_refund() (its override_final_
    refund_cents), never decided here."""
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    calculation = calculate_refund(existing["original_amount_cents"], waive_restocking_fee=defective_or_damaged_on_arrival)
    return _transition(
        return_id, "inspection",
        serial_number=serial_number, inspected_at=_now(), inspected_by=inspected_by,
        is_defective_or_damaged_on_arrival=1 if defective_or_damaged_on_arrival else 0,
        restocking_fee_cents=calculation["restocking_fee_cents"],
        restocking_fee_percent_applied=calculation["restocking_fee_percent"],
    )


def preview_refund(return_id: str) -> dict:
    """Read-only: what approve_refund() WOULD record if called right
    now, honoring the return's own recorded defective/damaged-on-arrival
    flag (set at inspection time) automatically. This is the "display
    the calculated refund amount to an administrator BEFORE executing a
    refund" requirement -- callable any number of times, changes
    nothing."""
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    return calculate_refund(existing["original_amount_cents"], waive_restocking_fee=bool(existing["is_defective_or_damaged_on_arrival"]))


def approve_refund(return_id: str, *, approved_by: str, override_final_refund_cents: Optional[int] = None) -> dict:
    """Requires an explicit admin identity (`approved_by`) -- there is
    no code path that reaches 'refund_approved'/'refund_adjusted'
    without one. Automatically honors the return's recorded defective/
    damaged-on-arrival flag (waives the restocking fee) -- an admin does
    not need to re-assert it here. `override_final_refund_cents` exists
    ONLY for a documented manual adjustment beyond that (e.g. missing
    accessories) -- when omitted, the standard calculate_refund() figure
    is used unchanged. Still does not call Stripe or move any money --
    see module docstring."""
    existing = row("SELECT * FROM hardware_returns WHERE id=?", (return_id,))
    if not existing:
        raise ValueError(f"Unknown return {return_id!r}")
    if not approved_by:
        raise ValueError("approve_refund() requires an explicit admin identity (approved_by).")
    calculation = calculate_refund(existing["original_amount_cents"], waive_restocking_fee=bool(existing["is_defective_or_damaged_on_arrival"]))
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
