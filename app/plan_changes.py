"""Stripe-side reconciliation for base camera plans held in
customer_entitlements (webhook helpers).

2026-10-08 (owner decision): the fixed 8/16/32/64-camera Local/Hybrid
subscription tiers were removed; camera plans are per camera (billing v2,
per_camera_billing.py, which has its own change and cancel routes). The
customer-facing Local -> Hybrid upgrade, downgrade, cancel and resume
actions of those fixed-tier plans (/api/customer/plan/*) were removed with
them. What stays is what the Stripe webhook calls for a base plan's
subscription, whoever changed it: closing upgrade attempts Stripe has
applied, ending/resuming add-ons with the base plan, and recording a
scheduled change.
"""
from __future__ import annotations

import sys
from datetime import datetime


def reconcile_upgrade_attempts(subscription: dict) -> int:
    """Close every open upgrade attempt whose requested price Stripe now
    shows on this subscription (applied, whether or not AnyAiCam saw the
    answer to the request that applied it)."""
    from partner_db import connection
    subscription_id = str(subscription.get("id") or "")
    if not subscription_id or subscription.get("pending_update"):
        return 0
    prices = sorted({str((i.get("price") or {}).get("id") or "") for i in ((subscription.get("items") or {}).get("data") or [])
                     if isinstance(i, dict)} - {""})
    if not prices:
        return 0
    with connection() as db:
        return db.execute(
            f"UPDATE plan_upgrade_attempts SET status='applied',open_slot=NULL,updated_at=? WHERE stripe_subscription_id=? "
            f"AND open_slot='open' AND stripe_price_id IN ({','.join('?' * len(prices))})",
            (datetime.now().isoformat(), subscription_id, *prices)).rowcount


def _schedule_id(subscription: dict) -> str:
    schedule = subscription.get("schedule")
    return str((schedule.get("id") if isinstance(schedule, dict) else schedule) or "")


# Owner decisions 2026-10-02: when the base plan is cancelled, its add-ons end
# with it -- never billing after the base plan ends -- with no credit for
# unused add-on days. An add-on whose own paid period ends on or before the
# base plan's end simply ends then (cancel_at_period_end: it never renews for
# a final partial period); one paid beyond the base end stops on the base end
# date (cancel_at, proration_behavior=none). Resuming the base plan resumes
# exactly the add-ons its cancellation ended.

def align_addons_with_base(customer_id: str, base: dict) -> list[str]:
    import customer_entitlements as ce
    import stripe_state
    from partner_db import connection, rows
    main = sys.modules.get("main")
    base_end = ce._period_end(base)
    base_customer = str(base.get("customer") or "")
    if main is None or not base_end or not base_customer:
        return []
    changed = []
    for addon in rows("SELECT * FROM addon_subscriptions WHERE customer_id=? AND stripe_customer_id=? AND status IN ('active','suspended') "
                      "AND stripe_subscription_id IS NOT NULL", (customer_id, base_customer)):
        current = stripe_state.current_subscription(addon["stripe_subscription_id"])
        if not current or str(current.get("customer") or "") != base_customer \
                or str((current.get("metadata") or {}).get("anyaicam_customer_id") or customer_id) != customer_id \
                or current.get("status") in ce.SUBSCRIPTION_INACTIVE_STATUSES:
            continue
        addon_end = ce._period_end(current)
        if addon_end and addon_end <= base_end:
            if not current.get("cancel_at_period_end"):
                main.stripe_api_post(f"/v1/subscriptions/{current['id']}", [("cancel_at_period_end", "true")],
                                     idempotency_key=f"anyaicam-addon-end-{current['id']}-{base_end}")
        elif int(current.get("cancel_at") or 0) != int(base_end):
            main.stripe_api_post(f"/v1/subscriptions/{current['id']}", [("cancel_at", str(base_end)), ("proration_behavior", "none")],
                                 idempotency_key=f"anyaicam-addon-cancel-at-{current['id']}-{base_end}")
        with connection() as db:
            db.execute("UPDATE addon_subscriptions SET ended_with_base=1 WHERE id=?", (addon["id"],))
        changed.append(addon["id"])
    return changed


def resume_addons_with_base(customer_id: str, base: dict) -> list[str]:
    import stripe_state
    from partner_db import connection, rows
    main = sys.modules.get("main")
    base_customer = str(base.get("customer") or "")
    if main is None or not base_customer:
        return []
    resumed = []
    for addon in rows("SELECT * FROM addon_subscriptions WHERE customer_id=? AND stripe_customer_id=? AND ended_with_base=1",
                      (customer_id, base_customer)):
        current = stripe_state.current_subscription(addon["stripe_subscription_id"])
        if current and str(current.get("customer") or "") == base_customer:
            if current.get("cancel_at_period_end"):
                main.stripe_api_post(f"/v1/subscriptions/{current['id']}", [("cancel_at_period_end", "false")],
                                     idempotency_key=f"anyaicam-addon-resume-{current['id']}-{current.get('current_period_end') or ''}")
            elif current.get("cancel_at"):
                main.stripe_api_post(f"/v1/subscriptions/{current['id']}", [("cancel_at", "")],
                                     idempotency_key=f"anyaicam-addon-resume-at-{current['id']}-{current.get('cancel_at')}")
        with connection() as db:
            db.execute("UPDATE addon_subscriptions SET ended_with_base=NULL WHERE id=?", (addon["id"],))
        resumed.append(addon["id"])
    return resumed


def sync_addons_with_base(entitlement: dict, subscription: dict) -> None:
    """The base plan's renewal state, wherever it was changed (AnyAiCam or
    the Customer Portal): set to end -> its add-ons end with it; renewal
    resumed -> the add-ons it ended resume."""
    import customer_entitlements as ce
    if entitlement.get("product") not in ce.BASE_PLAN_PRODUCTS or entitlement.get("status") not in ("active", "suspended"):
        return
    if subscription.get("cancel_at_period_end"):
        align_addons_with_base(entitlement["customer_id"], subscription)
    else:
        resume_addons_with_base(entitlement["customer_id"], subscription)


def _record_scheduled_change(entitlement_id: str, change: str | None, at: int | None, schedule_id: str | None) -> None:
    from partner_db import connection
    with connection() as db:
        db.execute("UPDATE customer_entitlements SET scheduled_change=?,scheduled_change_at=?,stripe_schedule_id=? WHERE id=?",
                   (change, at, schedule_id, entitlement_id))


def sync_scheduled_change(entitlement: dict, subscription: dict) -> None:
    """Keep the shown scheduled change true to Stripe: no schedule on the
    subscription -> nothing scheduled (released, or already applied at
    renewal); a schedule whose next phase is Local while on Hybrid ->
    'downgrade_to_local' at the period end (also when made in the portal)."""
    import customer_entitlements as ce
    import stripe_state
    schedule_id = _schedule_id(subscription)
    if not schedule_id:
        if entitlement.get("scheduled_change") or entitlement.get("stripe_schedule_id"):
            _record_scheduled_change(entitlement["id"], None, None, None)
        return
    if entitlement.get("stripe_schedule_id") == schedule_id and entitlement.get("scheduled_change"):
        return
    reader = stripe_state._stripe_reader()
    if reader is None:
        return
    try:
        found = reader(f"/v1/subscription_schedules/{schedule_id}")
    except Exception:
        return
    period_end = int(ce._period_end(subscription) or 0)
    upcoming = next((phase for phase in (found or {}).get("phases") or []
                     if period_end and int(phase.get("start_date") or 0) >= period_end), None)
    item = (((upcoming or {}).get("items")) or [{}])[0]
    price = item.get("price")
    price = str((price.get("id") if isinstance(price, dict) else price) or "")
    tier = ce.resolve_tier(price) if price else None
    if tier and tier["product"] == "camera_slots_local" and entitlement.get("product") == "camera_slots_hybrid":
        _record_scheduled_change(entitlement["id"], "downgrade_to_local", int(upcoming.get("start_date")), schedule_id)
