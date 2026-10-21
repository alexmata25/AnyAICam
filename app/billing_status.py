"""Paid-service state of camera plans, VMS licenses and add-ons
(2026-10-02, owner billing policies approved 2026-10-02).

1. Failed payment -> 7-day grace. When a renewal payment fails (Stripe sends
   invoice.payment_failed and/or the subscription becomes past_due/unpaid),
   service continues for GRACE_PERIOD; the customer sees the problem on My
   subscription and can update the card in the Customer Portal. A payment
   that succeeds in that window restores normal state (no new entitlement,
   nothing duplicated). Unpaid after the window: the entitlement is
   'suspended' (reason payment_failed) -- not cancelled, so a later
   successful payment restores exactly what was paid for. A subscription
   Stripe ends (canceled / incomplete_expired) is cancelled as before.
2. Refunds and disputes affect only the purchase they belong to: a FULL
   refund or a dispute of the payment covering the CURRENT period of a
   subscription (or of a one-time license) suspends that entitlement (reason
   refunded / disputed). A refunded or disputed OLDER period does not stop
   the currently paid period. A later successful payment restores refunded
   service; a dispute closed in AnyAiCam's favour (Stripe 'won': the payment
   stands) restores disputed service. Commission reversal is unchanged
   (sales_commissions.py).
Every transition is a state set keyed by Stripe ids, so replayed or
duplicated events change nothing further.

Ordering (Codex audit of 3f5b9c4): each invoice's payment state is monotonic
(billing_invoice_states) -- once paid, a delayed invoice.payment_failed for
it never starts grace again (finding 6); the invoice's state in Stripe is
read before grace starts. A refund or dispute recorded before the purchase
was granted is consulted when it is granted (stripe_state.payment_reversal,
finding 4). A failed renewal is also emailed to the customer, once per
invoice (purchase_notifications.notify_payment_failed, finding 8).
A paid invoice clears grace only when it resolves the outstanding failure:
no other renewal invoice of the subscription is still unpaid in Stripe and
Stripe no longer reports the subscription past_due/unpaid -- a delayed
invoice.paid for an older period never ends grace started by a newer failed
renewal (Codex verification of 9a388c6, finding 3). Camera plans keep their camera
count while suspended (counted only when active), so restoring is exact.

Not decided here (owner): partial refunds -- a partially refunded payment
leaves service unchanged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from partner_db import connection, row, rows, transaction

logger = logging.getLogger("anyaicam.billing_status")

GRACE_PERIOD = timedelta(days=7)
GRACE_STRIPE_STATUSES = {"past_due", "unpaid"}
HEALTHY_STRIPE_STATUSES = {"active", "trialing"}
RESTORED_BY_PAYMENT = ("payment_failed", "refunded")
REVERSALS_DDL = ("CREATE TABLE IF NOT EXISTS billing_reversals(id INTEGER PRIMARY KEY AUTOINCREMENT,charge_id TEXT,payment_intent TEXT,"
                 "invoice_id TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL)")
INVOICE_STATES_DDL = ("CREATE TABLE IF NOT EXISTS billing_invoice_states(invoice_id TEXT PRIMARY KEY,subscription_id TEXT,"
                      "state TEXT NOT NULL,updated_at TEXT NOT NULL)")
# 'paid' (or 'void': nothing is owed) is final for an invoice.
SETTLED_INVOICE_STATES = ("paid", "void")


def _now() -> datetime:
    return datetime.now()


def _stamp(value) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(int(value)).isoformat()
    return _now().isoformat()


# ------------------------------------------------------------------ rows

def _plan_rows(subscription_id: str) -> list[dict]:
    # A one-time VMS licence is never a subscription's plan row: a renewal or recovery never restores it, even if a
    # licence row ever carried a subscription id (RC10b hardening, Red F1 follow-up analysis item 2).
    return rows("SELECT * FROM customer_entitlements WHERE stripe_subscription_id=? AND status IN ('active','suspended') "
                "AND product<>'vms_license'", (subscription_id,))


def _addon_rows(subscription_id: str) -> list[dict]:
    try:
        return rows("SELECT * FROM addon_subscriptions WHERE stripe_subscription_id=? AND status IN ('active','suspended')",
                    (subscription_id,))
    except Exception:
        return []


def _set_plan(entitlement_id: str, status: str, reason: str | None) -> None:
    with connection() as db:
        db.execute("UPDATE customer_entitlements SET status=?,suspended_reason=?,updated_at=? WHERE id=?",
                   (status, reason, _now().isoformat(), entitlement_id))


def _set_addon(addon: dict, status: str, reason: str | None) -> None:
    import analytics_entitlements
    analytics_entitlements.apply_addon_state(addon, status=status, suspended_reason=reason)


def _suspend(subscription_id: str, reason: str) -> int:
    changed = 0
    for plan in _plan_rows(subscription_id):
        if plan["status"] == "active":
            _set_plan(plan["id"], "suspended", reason)
            changed += 1
    for addon in _addon_rows(subscription_id):
        if addon["status"] == "active":
            _set_addon(addon, "suspended", reason)
            changed += 1
    try:
        import per_camera_billing
        changed += per_camera_billing._suspend(subscription_id, reason)
    except Exception:
        pass  # retain compatibility with databases before billing v2
    if changed:
        logger.info("billing.suspended subscription=%s reason=%s rows=%s", subscription_id, reason, changed)
        from purchase_notifications import notify_service_suspended
        notify_service_suspended(subscription_id, reason)  # never raises; once per episode
    return changed


def _restore(subscription_id: str, reasons: tuple[str, ...]) -> int:
    changed = 0
    for plan in _plan_rows(subscription_id):
        if plan["status"] == "suspended" and plan.get("suspended_reason") in reasons:
            _set_plan(plan["id"], "active", None)
            changed += 1
    for addon in _addon_rows(subscription_id):
        if addon["status"] == "suspended" and addon.get("suspended_reason") in reasons:
            _set_addon(addon, "active", None)
            changed += 1
    try:
        import per_camera_billing
        changed += per_camera_billing._restore(subscription_id, reasons)
    except Exception:
        pass
    return changed


# ------------------------------------------------------------------ 1. failed payment and grace

def mark_payment_failed(subscription_id: str, *, at=None) -> None:
    """The first failure starts the grace period; repeats do not move it."""
    stamp = _stamp(at)
    with connection() as db:
        db.execute("UPDATE customer_entitlements SET payment_failed_at=? WHERE stripe_subscription_id=? AND payment_failed_at IS NULL "
                   "AND status IN ('active','suspended')", (stamp, subscription_id))
        try:
            db.execute("UPDATE addon_subscriptions SET payment_failed_at=? WHERE stripe_subscription_id=? AND payment_failed_at IS NULL "
                       "AND status IN ('active','suspended')", (stamp, subscription_id))
        except Exception:
            pass
    try:
        import per_camera_billing
        per_camera_billing._set_subscription_payment_failed(subscription_id, at)
    except Exception:
        pass


def payment_recovered(subscription_id: str) -> int:
    with connection() as db:
        db.execute("UPDATE customer_entitlements SET payment_failed_at=NULL WHERE stripe_subscription_id=?", (subscription_id,))
        try:
            db.execute("UPDATE addon_subscriptions SET payment_failed_at=NULL WHERE stripe_subscription_id=?", (subscription_id,))
        except Exception:
            pass
    changed = _restore(subscription_id, RESTORED_BY_PAYMENT)
    try:
        import per_camera_billing
        changed += per_camera_billing._resume_paid(subscription_id)
    except Exception:
        pass
    if changed:
        from purchase_notifications import notify_service_restored
        notify_service_restored(subscription_id)  # never raises
    return changed


def sweep_grace(now: datetime | None = None, *, customer_id: str | None = None) -> int:
    """Suspend whatever has been unpaid for longer than the grace period.
    Run periodically and before entitlements are read, so access never
    outlives the grace period just because no event arrived."""
    cutoff = ((now or _now()) - GRACE_PERIOD).isoformat()
    scope, args = ("", ()) if customer_id is None else (" AND customer_id=?", (customer_id,))
    changed = 0
    try:
        for plan in rows("SELECT id FROM customer_entitlements WHERE status='active' AND payment_failed_at IS NOT NULL "
                         f"AND payment_failed_at<=?{scope}", (cutoff, *args)):
            _set_plan(plan["id"], "suspended", "payment_failed")
            changed += 1
        v2_scope, v2_args = ("", ()) if customer_id is None else (" AND customer_id=?", (customer_id,))
        v2_rows = rows("SELECT id,stripe_subscription_id FROM camera_plan_entitlements_v2 WHERE status='active' AND payment_failed_at IS NOT NULL "
                       f"AND payment_failed_at<=?{v2_scope}", (cutoff, *v2_args))
        for plan in v2_rows:
            changed += _suspend(str(plan["stripe_subscription_id"]), "payment_failed")
        for addon in rows("SELECT * FROM addon_subscriptions WHERE status='active' AND payment_failed_at IS NOT NULL "
                          f"AND payment_failed_at<=?{scope}", (cutoff, *args)):
            _set_addon(addon, "suspended", "payment_failed")
            changed += 1
    except Exception as error:  # a database before these columns exist
        logger.debug("billing.sweep_skipped error=%s", type(error).__name__)
        return 0
    if changed:
        logger.info("billing.grace_expired rows=%s", changed)
    return changed


def grace_ends_at(payment_failed_at: str | None) -> datetime | None:
    if not payment_failed_at:
        return None
    try:
        return datetime.fromisoformat(payment_failed_at) + GRACE_PERIOD
    except ValueError:
        return None


def _outstanding_failure(subscription_id: str, *, paid_invoice_id: str = "", subscription: dict | None = None) -> bool:
    """True while a failed renewal of this subscription is still unpaid:
    another invoice recorded as failed that Stripe has not settled, or Stripe
    still reporting the subscription past_due/unpaid. Read from Stripe;
    unreadable -> RetryableStripeEventError (the event is retried)."""
    import stripe_state
    from urllib.parse import quote
    with connection() as db:
        db.execute(INVOICE_STATES_DDL)
        failed = [r["invoice_id"] for r in db.execute(
            "SELECT invoice_id FROM billing_invoice_states WHERE subscription_id=? AND state='failed' AND invoice_id<>?",
            (subscription_id, paid_invoice_id)).fetchall()]
    for invoice_id in failed:
        current = stripe_state.stripe_get(f"/v1/invoices/{quote(invoice_id, safe='')}", "a failed renewal's payment state")
        status = str(current.get("status") or "")
        if status not in SETTLED_INVOICE_STATES:
            return True
        _set_invoice_state(invoice_id, subscription_id, status)
    subscription = subscription or stripe_state.current_subscription(subscription_id)
    return str((subscription or {}).get("status") or "") in GRACE_STRIPE_STATUSES


def apply_subscription_status(subscription: dict) -> None:
    """Stripe's current status for a subscription (reconciliation)."""
    subscription_id = str(subscription.get("id") or "")
    status = str(subscription.get("status") or "")
    if not subscription_id:
        return
    if status in GRACE_STRIPE_STATUSES:
        mark_payment_failed(subscription_id)
        sweep_grace()
    elif status in HEALTHY_STRIPE_STATUSES and not _outstanding_failure(subscription_id, subscription=subscription):
        payment_recovered(subscription_id)


# ------------------------------------------------------------------ 2. refunds and disputes

def _payment_for_charge(charge_id: str, intent: str, invoice_id: str) -> dict | None:
    """The locally recorded subscription payment of this charge. Local records only (RC10b): when there is none, the
    caller asks Stripe through _stripe_invoice_for_payment, which never mistakes an unreadable Stripe for 'no invoice'."""
    for column, value in (("id", invoice_id), ("stripe_charge_id", charge_id), ("stripe_payment_intent_id", intent)):
        if value:
            found = row(f"SELECT * FROM subscription_payments WHERE {column}=?", (value,))
            if found:
                return found
    return None


def _covers_current_period(payment: dict) -> bool:
    latest = row("SELECT id FROM subscription_payments WHERE stripe_subscription_id=? ORDER BY period_start DESC, paid_at DESC LIMIT 1",
                 (payment["stripe_subscription_id"],))
    return bool(latest) and latest["id"] == payment["id"]


def _charge_parts(event: dict) -> tuple[str, str, str, bool]:
    obj = (event.get("data") or {}).get("object") or {}
    if obj.get("object") == "charge" or event.get("type") == "charge.refunded":
        amount = int(obj.get("amount") or 0)
        full = bool(obj.get("refunded")) or (amount > 0 and int(obj.get("amount_refunded") or 0) >= amount)
        return str(obj.get("id") or ""), str(obj.get("payment_intent") or ""), str(obj.get("invoice") or ""), full
    return str(obj.get("charge") or ""), str(obj.get("payment_intent") or ""), "", True  # a dispute covers the charge


def _record_reversal(charge_id: str, intent: str, invoice_id: str, reason: str) -> None:
    """Remembered so a paid-invoice event for this same payment, however late
    it arrives, never restores refunded/disputed service."""
    with connection() as db:
        db.execute(REVERSALS_DDL)
        if not db.execute("SELECT 1 FROM billing_reversals WHERE COALESCE(charge_id,'')=? AND COALESCE(payment_intent,'')=? AND reason=?",
                          (charge_id, intent, reason)).fetchone():
            db.execute("INSERT INTO billing_reversals(charge_id,payment_intent,invoice_id,reason,created_at) VALUES(?,?,?,?,?)",
                       (charge_id or None, intent or None, invoice_id or None, reason, _now().isoformat()))
        elif invoice_id:
            # The invoice was identified later (current Stripe API versions: the charge does not name it). Kept, so a
            # purchase granted later from that invoice sees the reversal (F1, reversal_for_invoice).
            db.execute("UPDATE billing_reversals SET invoice_id=? WHERE COALESCE(charge_id,'')=? AND COALESCE(payment_intent,'')=? "
                       "AND reason=? AND invoice_id IS NULL", (invoice_id, charge_id, intent, reason))


def reversal_reason(charge_id: str, intent: str, invoice_id: str) -> str | None:
    """'refunded' / 'disputed' if this payment was reversed (a won dispute no
    longer counts), else None."""
    with connection() as db:
        db.execute(REVERSALS_DDL)
        for column, value in (("charge_id", charge_id), ("payment_intent", intent), ("invoice_id", invoice_id)):
            if value:
                found = db.execute(f"SELECT reason FROM billing_reversals WHERE {column}=? AND reason IN ('refunded','disputed') "
                                   "ORDER BY CASE reason WHEN 'refunded' THEN 0 ELSE 1 END LIMIT 1", (value,)).fetchone()
                if found:
                    return found["reason"]
    return None


def reversal_for_invoice(invoice_id: str) -> dict | None:
    """{'reason', 'payment_intent'} if the payment of this invoice was fully refunded or is disputed (a won dispute
    no longer counts), else None. Local record only: used when a purchase is granted after its reversal arrived."""
    if not invoice_id:
        return None
    with connection() as db:
        db.execute(REVERSALS_DDL)
        found = db.execute("SELECT reason,payment_intent FROM billing_reversals WHERE invoice_id=? AND reason IN ('refunded','disputed') "
                           "ORDER BY CASE reason WHEN 'refunded' THEN 0 ELSE 1 END LIMIT 1", (invoice_id,)).fetchone()
    return {"reason": found["reason"], "payment_intent": found["payment_intent"]} if found else None


def _is_reversed(charge_id: str, intent: str, invoice_id: str) -> bool:
    return reversal_reason(charge_id, intent, invoice_id) is not None


def record_reversal(charge_id: str, intent: str, invoice_id: str, reason: str) -> None:
    with transaction() as db:
        lock_payment(db, payment_keys(charge_id, intent, invoice_id))
        _record_reversal(charge_id, intent, invoice_id, reason)


# ------------------------------------------------------------------ per-invoice payment state (finding 6)

def invoice_state(invoice_id: str) -> str | None:
    with connection() as db:
        db.execute(INVOICE_STATES_DDL)
        found = db.execute("SELECT state FROM billing_invoice_states WHERE invoice_id=?", (invoice_id,)).fetchone()
    return found["state"] if found else None


def _set_invoice_state(invoice_id: str, subscription_id: str, state: str) -> bool:
    """Monotonic: a settled invoice never goes back to failed. True if the
    state was written."""
    with connection() as db:
        db.execute(INVOICE_STATES_DDL)
        return bool(db.execute(
            "INSERT INTO billing_invoice_states(invoice_id,subscription_id,state,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(invoice_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at "
            "WHERE billing_invoice_states.state NOT IN ('paid','void')",
            (invoice_id, subscription_id, state, _now().isoformat())).rowcount)


def _invoice_payment_failed(invoice: dict, subscription_id: str, at) -> dict:
    invoice_id = str(invoice.get("id") or "")
    if not invoice_id:
        return {"status": "ignored", "reason": "invoice has no id"}
    if str(invoice.get("billing_reason") or "") == "subscription_update":
        # A declined plan-change proration (Local -> Hybrid upgrade with
        # payment_behavior=pending_if_incomplete): Stripe keeps the
        # subscription on its paid plan and holds the change as
        # pending_update, then voids this invoice when the change expires --
        # without a subscription.updated. It is not an unpaid period, so it
        # never starts grace, is never recorded as an unpaid renewal and
        # never sends a payment-failed email. If Stripe did make the
        # subscription past_due, customer.subscription.updated starts grace
        # through apply_subscription_status. (Stripe TEST-mode evidence,
        # 2026-10-03: tests/test_stripe_testmode_declined_upgrade.py.)
        return {"status": "ignored", "reason": "a declined plan-change payment: the paid plan is unchanged"}
    if invoice_state(invoice_id) in SETTLED_INVOICE_STATES:
        return {"status": "ignored", "reason": "this invoice has already been paid"}
    import stripe_state
    from urllib.parse import quote
    current = stripe_state.stripe_get(f"/v1/invoices/{quote(invoice_id, safe='')}", "the invoice's payment state")
    if str(current.get("status") or "") in SETTLED_INVOICE_STATES:
        _set_invoice_state(invoice_id, subscription_id, str(current["status"]))
        return {"status": "ignored", "reason": "this invoice has already been paid"}
    if not _set_invoice_state(invoice_id, subscription_id, "failed"):
        return {"status": "ignored", "reason": "this invoice has already been paid"}
    mark_payment_failed(subscription_id, at=at)
    return {"status": "grace_started", "subscription": subscription_id, "invoice": invoice_id}


def _stripe_invoice_for_payment(intent: str, invoice_id: str = "") -> dict | None:
    """A refund or dispute of a payment AnyAiCam has no local record of yet: ask Stripe which invoice it paid, and that
    invoice's subscription and Stripe customer -> {'invoice_id', 'subscription_id', 'stripe_customer_id'}.
    None ONLY when Stripe answers conclusively that this PaymentIntent paid no invoice (a one-time payment). An
    unreadable or unexpected answer raises RetryableStripeEventError, so the event is retried instead of being
    acknowledged as unrelated (RC10b: before, every Stripe error here read as 'no invoice')."""
    import stripe_state
    from stripe_state import RetryableStripeEventError
    from urllib.parse import quote
    if not invoice_id:
        if not intent:
            return None
        listed = stripe_state.stripe_get(
            f"/v1/invoice_payments?payment[type]=payment_intent&payment[payment_intent]={quote(intent, safe='')}&limit=1",
            "the invoice this payment paid")
        data = listed.get("data")
        if not isinstance(data, list):
            raise RetryableStripeEventError("Stripe returned an unexpected answer for the invoice this payment paid")
        if not data:
            return None
        invoice_id = stripe_state._id((data[0] or {}).get("invoice"))
        if not invoice_id:
            raise RetryableStripeEventError("the invoice this payment paid could not be identified")
    invoice = stripe_state.stripe_get(f"/v1/invoices/{quote(invoice_id, safe='')}", "the invoice this payment paid")
    if invoice.get("id") and str(invoice["id"]) != invoice_id:
        raise RetryableStripeEventError("Stripe returned an unexpected invoice")
    return {"invoice_id": invoice_id, "subscription_id": _invoice_subscription(invoice),
            "stripe_customer_id": stripe_state._id(invoice.get("customer"))}


def invoice_payment(invoice_id: str) -> tuple[str, str]:
    """(PaymentIntent, charge) that paid this invoice, read from Stripe; ('', '') when the invoice charged nothing.
    Used when a one-time licence bought on a subscription's first invoice is granted (build_orders._grant_vms_license),
    so the licence row records its payment from the start. Unreadable -> RetryableStripeEventError."""
    import stripe_state
    from stripe_state import RetryableStripeEventError
    from urllib.parse import quote
    if not invoice_id:
        return "", ""
    invoice = stripe_state.stripe_get(f"/v1/invoices/{quote(invoice_id, safe='')}", "the invoice of this order")
    if int(invoice.get("amount_paid") or 0) <= 0:
        return "", ""
    charge = stripe_state._id(invoice.get("charge"))
    intent = stripe_state._id(invoice.get("payment_intent"))
    for item in ((invoice.get("payments") or {}).get("data") or []):
        intent = intent or stripe_state._id((item.get("payment") or {}).get("payment_intent"))
    if not intent:
        listed = stripe_state.stripe_get(f"/v1/invoice_payments?invoice={quote(invoice_id, safe='')}&limit=5",
                                         "the payment of this order's invoice")
        for item in listed.get("data") or []:
            if item.get("status", "paid") == "paid":
                intent = intent or stripe_state._id((item.get("payment") or {}).get("payment_intent"))
    if not intent and not charge:
        raise RetryableStripeEventError("the payment of this order's invoice could not be identified")
    return intent, charge


def reverse_hardware_orders(intent: str, reason: str) -> int:
    """A refunded or disputed hardware purchase (2026-10-04): its PAID orders
    become 'refunded'/'disputed', so an appliance order no longer includes the
    VMS license (customer_entitlements.appliance_includes_vms_license counts
    paid appliance orders only). Only that payment's orders change -- other
    appliances and standalone licenses are untouched -- and a replay finds
    nothing left to change."""
    if not intent:
        return 0
    with connection() as db:
        changed = db.execute("UPDATE hardware_orders SET status=?,updated_at=? WHERE stripe_payment_intent_id=? AND status='paid'",
                             (reason, datetime.now().isoformat(), intent)).rowcount
    if changed or not row("SELECT id FROM hardware_orders WHERE stripe_payment_intent_id IS NULL AND status='paid' LIMIT 1"):
        return changed
    # An order recorded before its PaymentIntent was kept: ask Stripe which
    # Checkout Session this payment belongs to (retried by the webhook if
    # Stripe cannot answer).
    import stripe_state
    from urllib.parse import quote
    found = stripe_state.stripe_get(f"/v1/checkout/sessions?payment_intent={quote(intent, safe='')}&limit=1",
                                    "the Checkout Session of this payment")
    session_ids = [str(item.get("id") or "") for item in (found.get("data") or []) if isinstance(item, dict)]
    with connection() as db:
        for session_id in filter(None, session_ids):
            changed += db.execute("UPDATE hardware_orders SET status=?,stripe_payment_intent_id=?,updated_at=? "
                                  "WHERE stripe_checkout_session_id=? AND status='paid'",
                                  (reason, intent, datetime.now().isoformat(), session_id)).rowcount
    return changed


def restore_hardware_orders(intent: str) -> int:
    """A won dispute: the payment stands, so the order is paid again."""
    if not intent:
        return 0
    with connection() as db:
        return db.execute("UPDATE hardware_orders SET status='paid',updated_at=? WHERE stripe_payment_intent_id=? AND status='disputed'",
                          (datetime.now().isoformat(), intent)).rowcount


# ------------------------------------------------------------------ payment serialization (Green 2026-10-18, post-RC5)
#
# Codex review of 7080e193: a refund/dispute could be recorded (its own transaction) between the sign-in attachment's
# "not reversed" check and its commit, so a refunded purchase could still get an active entitlement under PostgreSQL
# READ COMMITTED. Every path that reverses, restores or attaches a purchase now takes the same per-payment lock
# (payment_locks rows keyed by payment intent / charge / invoice, in sorted order) inside the transaction that does
# its work: whichever commits first, the other then sees it.

_LOCK_ORDER = {"ch": 0, "pi": 1, "in": 2}


def payment_keys(charge_id: str = "", intent: str = "", invoice_id: str = "") -> list[str]:
    return sorted({f"{prefix}:{value}" for prefix, value in (("ch", charge_id), ("pi", intent), ("in", invoice_id)) if value})


def lock_payment(db, keys: list[str]) -> None:
    """Take the per-payment lock(s) inside the caller's transaction (SQLite: BEGIN IMMEDIATE already serializes all
    writers; PostgreSQL: a row lock held until commit)."""
    from database_backend import backend
    now = _now().isoformat()
    # One global order -- charge, then PaymentIntent, then invoice (RC10b) -- for every caller, including the invoice
    # key a reversal adds later in its transaction (_lock_invoice) and a licence grant that holds a PaymentIntent and an
    # invoice: no two transactions ever wait on each other's keys (PostgreSQL deadlock-free).
    for key in sorted(set(keys), key=lambda k: (_LOCK_ORDER.get(k.split(":", 1)[0], len(_LOCK_ORDER)), k)):
        db.execute("INSERT INTO payment_locks(key,created_at) VALUES(?,?) ON CONFLICT(key) DO NOTHING", (key, now))
        if backend() == "postgresql":
            db.execute("SELECT key FROM payment_locks WHERE key=? FOR UPDATE", (key,))


def _restore_disputed_pending(db, charge_id: str, intent: str) -> int:
    """A won dispute makes a pending purchase that attachment had set aside as 'disputed' claimable again (Codex:
    dispute-won recovery). Refund-reversed purchases stay terminal."""
    restored = 0
    for column, value in (("charge_id", charge_id), ("payment_intent", intent)):
        if not value:
            continue
        for item in db.execute(f"SELECT * FROM pending_reversals WHERE {column}=? AND reason='disputed'", (value,)).fetchall():
            table = item["source_table"]
            if table not in ("pending_customer_links", "pending_hardware_order_links", "pending_analytics_links"):
                continue
            restored += db.execute(f"UPDATE {table} SET status='pending',claim_token=NULL,claimed_at=NULL "
                                   "WHERE id=? AND status='reversed'", (item["source_id"],)).rowcount
            db.execute("DELETE FROM pending_reversals WHERE id=?", (item["id"],))
    return restored


# ------------------------------------------------------------------ own-PC licence on a subscription's first invoice (F1)
#
# An own-PC Build Your System order (build_orders._grant_vms_license) is one subscription Checkout: the plan plus the
# one-time VMS software licence, both charged on the subscription's FIRST invoice. The licence is a one-time purchase of
# that payment, so -- like a standalone licence -- a full refund or a dispute of that payment suspends it (whether or not
# that invoice is still the current period), and only a won dispute restores it; the plan follows the subscription rules
# above. The licence row records its Checkout Session; the payment that paid for it is bound to the row the first time a
# reversal of that subscription's first payment arrives (the Session names its first invoice), and from then on the row is
# found by its PaymentIntent exactly like a standalone licence.

def _is_first_payment(payment: dict) -> bool:
    first = row("SELECT id FROM subscription_payments WHERE stripe_subscription_id=? ORDER BY period_start ASC, paid_at ASC LIMIT 1",
                (payment["stripe_subscription_id"],))
    return bool(first) and first["id"] == payment["id"]


def _lock_invoice(invoice_id: str) -> None:
    """Also serialize with a licence grant for this invoice (build_orders._grant_vms_license takes this key)."""
    if invoice_id:
        with connection() as db:  # joins the caller's transaction
            lock_payment(db, payment_keys(invoice_id=invoice_id))


def _bind_initial_licence(invoice_id: str, subscription_id: str, intent: str, customer_ids: set) -> None:
    """Record the PaymentIntent of this subscription's first invoice on an own-PC licence bought with it (granted
    before the row kept its payment). Read from Stripe: unreadable -> RetryableStripeEventError (the event is retried)."""
    import stripe_state
    from urllib.parse import quote
    for customer_id in sorted(filter(None, customer_ids)):
        for licence in rows("SELECT id,stripe_checkout_session_id FROM customer_entitlements WHERE customer_id=? AND product='vms_license' "
                            "AND stripe_payment_intent_id IS NULL AND stripe_checkout_session_id IS NOT NULL", (customer_id,)):
            session = stripe_state.stripe_get(f"/v1/checkout/sessions/{quote(str(licence['stripe_checkout_session_id']), safe='')}",
                                              "the licence's Checkout Session")
            if stripe_state._id(session.get("subscription")) == subscription_id and stripe_state._id(session.get("invoice")) == invoice_id:
                with connection() as db:
                    db.execute("UPDATE customer_entitlements SET stripe_payment_intent_id=?,updated_at=? WHERE id=? "
                               "AND stripe_payment_intent_id IS NULL", (intent, _now().isoformat(), licence["id"]))


def _reverse_initial_licence(invoice_id: str, subscription_id: str, intent: str, reason: str, *, first: bool,
                             customer_ids: set) -> int:
    """Suspend the one-time licence paid by this (subscription) payment; 0 if it paid for none."""
    if not intent:
        return 0
    if first and invoice_id and subscription_id:
        customer_ids = set(customer_ids) | {r["customer_id"] for r in rows(
            "SELECT customer_id FROM camera_plan_entitlements_v2 WHERE stripe_subscription_id=?", (subscription_id,))}
        _bind_initial_licence(invoice_id, subscription_id, intent, customer_ids)
    changed = 0
    for licence in rows("SELECT id FROM customer_entitlements WHERE stripe_payment_intent_id=? AND product='vms_license' AND status='active'",
                        (intent,)):
        _set_plan(licence["id"], "suspended", reason)
        changed += 1
    if changed:
        logger.info("billing.initial_licence_suspended subscription=%s reason=%s rows=%s", subscription_id, reason, changed)
    return changed


def _restore_disputed_licences(intent: str) -> int:
    """A won dispute: the one-time licence(s) paid by this payment are active again (refunds stay terminal)."""
    restored = 0
    for license_row in rows("SELECT * FROM customer_entitlements WHERE stripe_payment_intent_id=? AND status='suspended' "
                            "AND suspended_reason='disputed'", (intent,)):
        _set_plan(license_row["id"], "active", None)
        restored += 1
    return restored


def _reverse_purchase(event: dict, reason: str) -> dict:
    charge_id, intent, invoice_id, full = _charge_parts(event)
    if not full:
        return {"status": "ignored", "reason": "partial refund: service unchanged (owner decision pending)"}
    with transaction() as db:  # the reversal record and every suspension commit together, under the payment lock
        lock_payment(db, payment_keys(charge_id, intent, invoice_id))
        return _reverse_purchase_locked(charge_id, intent, invoice_id, reason)


def _hardware_payment(intent: str) -> bool:
    """A hardware order paid with this PaymentIntent: a one-time payment (mode=payment), never a subscription invoice."""
    if not intent:
        return False
    try:
        return bool(row("SELECT id FROM hardware_orders WHERE stripe_payment_intent_id=? LIMIT 1", (intent,)))
    except Exception:
        return False


def _reverse_purchase_locked(charge_id: str, intent: str, invoice_id: str, reason: str) -> dict:
    # The reversal is recorded first, by charge AND PaymentIntent (and invoice when known), whatever is found below: a
    # purchase granted later from this payment is granted suspended (build_orders._grant_vms_license checks the invoice,
    # the PaymentIntent and the charge; stripe_state.payment_reversal the PaymentIntent and the charge). A replay finds
    # the same row (one per charge, PaymentIntent and reason).
    payment = _payment_for_charge(charge_id, intent, invoice_id)
    _record_reversal(charge_id, intent, (payment or {}).get("id") or invoice_id, reason)
    found = None
    if payment is None and not _hardware_payment(intent):
        # RC10b (Codex review of RC10 99a19591): no local record of this payment yet -- the refund or dispute may have
        # arrived before invoice.paid and the plan were recorded. Stripe says which invoice, subscription and Stripe
        # customer it belongs to. An unreadable Stripe raises (the event is retried; this transaction, including the
        # record above, rolls back) instead of acknowledging the reversal as unrelated.
        found = _stripe_invoice_for_payment(intent, invoice_id)
        if found:
            # Kept with its invoice even when nothing is granted yet, so a licence granted from that invoice later is
            # granted suspended (F1, build_orders._grant_vms_license takes the same invoice lock).
            _lock_invoice(found["invoice_id"])
            _record_reversal(charge_id, intent, found["invoice_id"], reason)
            payment = row("SELECT * FROM subscription_payments WHERE id=?", (found["invoice_id"],))
    if payment is None and found and found["subscription_id"]:
        subscription_id = found["subscription_id"]
        # Not recorded yet locally: the payment is treated as possibly the first one. The accounts are those of the
        # invoice's Stripe customer (a granted licence row records it) and of the subscription's plan; the bind still
        # checks the licence's Checkout Session names this subscription and invoice, so another account's licence is
        # never bound.
        import stripe_state
        accounts = stripe_state.stripe_customer_accounts(found["stripe_customer_id"])
        licences = _reverse_initial_licence(found["invoice_id"], subscription_id, intent, reason, first=True,
                                            customer_ids=accounts)
        if _plan_rows(subscription_id) or _addon_rows(subscription_id) or row(
                "SELECT id FROM camera_plan_entitlements_v2 WHERE stripe_subscription_id=?", (subscription_id,)):
            return {"status": "suspended", "rows": _suspend(subscription_id, reason) + licences}
        if licences:
            return {"status": "suspended", "rows": licences}
        if intent and row("SELECT id FROM customer_entitlements WHERE stripe_payment_intent_id=? AND product='vms_license'", (intent,)):
            return {"status": "ignored", "reason": "already applied to the licence bought with this payment"}
        # Nothing granted from this invoice yet: the durable record above is applied when it is (2xx, not retried).
        logger.warning("billing.reversal_recorded_before_purchase invoice=%s subscription=%s reason=%s",
                       found["invoice_id"], subscription_id, reason)
        return {"status": "recorded", "reason": "nothing granted from this payment yet; applied when it is granted",
                "invoice": found["invoice_id"]}
    if payment and payment.get("stripe_subscription_id"):
        _lock_invoice(str(payment["id"]))
        # F1: the one-time own-PC licence bought on this payment goes with it, whatever the period rules say for the plan.
        licences = _reverse_initial_licence(str(payment["id"]), str(payment["stripe_subscription_id"]),
                                            intent or str(payment.get("stripe_payment_intent_id") or ""), reason,
                                            first=_is_first_payment(payment), customer_ids={payment.get("customer_id")})
        if not _covers_current_period(payment):
            if licences:
                return {"status": "suspended", "rows": licences, "reason": "an earlier period: only its one-time licence"}
            return {"status": "ignored", "reason": "an earlier period; the current period is paid"}
        return {"status": "suspended", "rows": _suspend(payment["stripe_subscription_id"], reason) + licences}
    if intent:  # a one-time purchase (VMS license, hardware)
        changed = 0
        for license_row in rows("SELECT * FROM customer_entitlements WHERE stripe_payment_intent_id=? AND status='active'", (intent,)):
            _set_plan(license_row["id"], "suspended", reason)
            changed += 1
        hardware = reverse_hardware_orders(intent, reason)
        if changed or hardware:
            return {"status": "suspended", "rows": changed, "hardware_orders": hardware}
    return {"status": "ignored", "reason": "no AnyAiCam purchase for this charge"}


def _dispute_closed(event: dict) -> dict:
    obj = (event.get("data") or {}).get("object") or {}
    if str(obj.get("status") or "") != "won":
        return {"status": "ignored", "reason": "dispute not won: service stays suspended"}
    charge_id, intent = str(obj.get("charge") or ""), str(obj.get("payment_intent") or "")
    with transaction() as db:
        lock_payment(db, payment_keys(charge_id, intent))
        result = _dispute_won_locked(charge_id, intent)
        result["pending_reinstated"] = _restore_disputed_pending(db, charge_id, intent)
    return result


def _disputed_invoice(charge_id: str, intent: str) -> str:
    """The invoice a dispute of this payment was recorded with (the dispute event found it), else ''."""
    with connection() as db:
        db.execute(REVERSALS_DDL)
        for column, value in (("charge_id", charge_id), ("payment_intent", intent)):
            if value:
                found = db.execute(f"SELECT invoice_id FROM billing_reversals WHERE {column}=? AND reason='disputed' "
                                   "AND invoice_id IS NOT NULL LIMIT 1", (value,)).fetchone()
                if found:
                    return str(found["invoice_id"])
    return ""


def _dispute_won_locked(charge_id: str, intent: str) -> dict:
    disputed_invoice = _disputed_invoice(charge_id, intent)
    payment = _payment_for_charge(charge_id, intent, disputed_invoice)
    subscription_id = ""
    if payment is None and not _hardware_payment(intent):
        # RC10b: no local payment record (e.g. the dispute opened before invoice.paid was recorded): the invoice and
        # subscription it paid, from Stripe (as when the dispute opened); unreadable -> retried, never skipped.
        found = _stripe_invoice_for_payment(intent, disputed_invoice)
        if found:
            _lock_invoice(found["invoice_id"])
            payment = row("SELECT * FROM subscription_payments WHERE id=?", (found["invoice_id"],))
            subscription_id = found["subscription_id"] if payment is None else ""
    with connection() as db:  # the payment stands: no longer a reversal
        db.execute(REVERSALS_DDL)
        for column, value in (("charge_id", charge_id), ("payment_intent", intent)):
            if value:
                db.execute(f"UPDATE billing_reversals SET reason='dispute_won' WHERE {column}=? AND reason='disputed'", (value,))
    restored = 0
    if payment is None and subscription_id:
        restored = _restore(subscription_id, ("disputed",))
        if restored:
            from purchase_notifications import notify_service_restored
            notify_service_restored(subscription_id)  # never raises
        restored += _restore_disputed_licences(intent) if intent else 0
    elif payment and payment.get("stripe_subscription_id"):
        restored = _restore(payment["stripe_subscription_id"], ("disputed",))
        if restored:
            from purchase_notifications import notify_service_restored
            notify_service_restored(payment["stripe_subscription_id"])  # never raises
        # F1: an own-PC licence bought on this (first) payment is restored with it.
        restored += _restore_disputed_licences(intent or str(payment.get("stripe_payment_intent_id") or ""))
    elif intent:
        restored += _restore_disputed_licences(intent)
        restored += restore_hardware_orders(intent)
    return {"status": "restored" if restored else "ignored", "rows": restored}


# ------------------------------------------------------------------ webhook step

def _invoice_subscription(invoice: dict) -> str:
    direct = invoice.get("subscription")
    if direct:
        return str(direct.get("id") if isinstance(direct, dict) else direct)
    parent = ((invoice.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    return str(parent or "")


def sync_from_stripe_event(event: dict) -> dict:
    event_type = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    if event_type == "invoice.payment_failed":
        subscription_id = _invoice_subscription(obj)
        if subscription_id:
            return _invoice_payment_failed(obj, subscription_id, event.get("created"))
        return {"status": "ignored"}
    if event_type in ("invoice.paid", "invoice.payment_succeeded"):
        subscription_id = _invoice_subscription(obj)
        if not subscription_id:
            return {"status": "ignored"}
        import sales_commissions
        intent = sales_commissions._invoice_payment_intent(obj) or ""
        if obj.get("id"):
            _set_invoice_state(str(obj["id"]), subscription_id, "paid")
        if _is_reversed(str(obj.get("charge") or ""), intent, str(obj.get("id") or "")):
            return {"status": "ignored", "reason": "this payment was refunded or disputed"}
        if _outstanding_failure(subscription_id, paid_invoice_id=str(obj.get("id") or "")):
            return {"status": "ignored", "reason": "a later renewal is still unpaid: grace continues"}
        return {"status": "recovered", "rows": payment_recovered(subscription_id)}
    if event_type in ("customer.subscription.updated", "customer.subscription.created"):
        from stripe_state import current_subscription
        current = current_subscription(str(obj.get("id") or "") or None) or obj
        apply_subscription_status(current)
        return {"status": "reconciled"}
    if event_type == "charge.refunded":
        return _reverse_purchase(event, "refunded")
    if event_type == "charge.dispute.created":
        return _reverse_purchase(event, "disputed")
    if event_type == "charge.dispute.closed":
        return _dispute_closed(event)
    return {"status": "ignored"}
