"""Versioned, per-camera subscription billing.

This module is additive beside customer_entitlements' fixed-capacity legacy
catalog. Stripe Price IDs are resolved only from the server environment and
every entitlement write is based on Stripe's current subscription state.
"""
from __future__ import annotations

import json
import os
import secrets
import string
import sys
import uuid
from datetime import datetime
from html import escape
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, conint

from partner_db import connection, row

MAX_CAMERA_QUANTITY = 64
MIN_CAMERA_QUANTITY = 1
PRICE_ENV = {
    "basic_local": "ANYAICAM_STRIPE_PRICE_BASIC_LOCAL",
    "ai_local": "ANYAICAM_STRIPE_PRICE_AI_LOCAL",
    "hybrid": "ANYAICAM_STRIPE_PRICE_HYBRID",
}
PLANS = {
    "basic_local": {
        "display_name": "Basic Local", "monthly_cents_per_camera": 999,
        "local_retention_days": 2, "cloud_event_storage": False,
        "cloud_event_retention_days": None, "mode": "local", "commission_percent": 0,
        "resolution_classes": ["standard", "high_resolution"],
        "features": ["local_vms", "local_recording", "local_playback", "basic_motion_events",
                     "standard_camera_management", "remote_viewing_where_supported"],
    },
    "ai_local": {
        "display_name": "AI Local", "monthly_cents_per_camera": 1499,
        "local_retention_days": 7, "cloud_event_storage": False,
        "cloud_event_retention_days": None, "mode": "local", "commission_percent": 20,
        "resolution_classes": ["standard", "high_resolution"],
        "features": ["local_vms", "local_recording", "local_playback", "basic_motion_events",
                     "standard_camera_management", "remote_viewing_where_supported", "person_vehicle_detection",
                     "smart_motion", "ai_event_search_filtering", "intelligent_notifications",
                     "core_aaco_retrieval", "supported_talk_down"],
    },
    "hybrid": {
        "display_name": "Hybrid", "monthly_cents_per_camera": 2499,
        "local_retention_days": 7, "cloud_event_storage": True,
        "cloud_event_retention_days": 14, "mode": "hybrid", "commission_percent": 20,
        "resolution_classes": ["standard", "high_resolution"],
        "features": ["local_vms", "local_recording", "local_playback", "basic_motion_events",
                     "standard_camera_management", "remote_viewing_where_supported", "person_vehicle_detection",
                     "smart_motion", "ai_event_search_filtering", "intelligent_notifications",
                     "core_aaco_retrieval", "supported_talk_down", "hybrid_cloud_services",
                     "remote_cloud_services", "cloud_event_protection"],
    },
}
FEATURE_LABELS = {
    "local_vms": "Local VMS", "local_recording": "Local recording", "local_playback": "Local playback",
    "basic_motion_events": "Basic motion and events", "standard_camera_management": "Camera management",
    "remote_viewing_where_supported": "Remote viewing where supported", "person_vehicle_detection": "Person and vehicle detection",
    "smart_motion": "Smart Motion", "ai_event_search_filtering": "AI event search and filters",
    "intelligent_notifications": "Intelligent notifications", "core_aaco_retrieval": "Core AACO video and event retrieval",
    "supported_talk_down": "Talk Down / two-way audio on supported cameras", "hybrid_cloud_services": "Hybrid cloud services",
    "remote_cloud_services": "Remote cloud services", "cloud_event_protection": "Cloud event protection",
}


class CheckoutRequest(BaseModel):
    plan_key: str
    camera_quantity: conint(strict=True, ge=MIN_CAMERA_QUANTITY, le=MAX_CAMERA_QUANTITY)

    class Config:
        extra = "forbid"


class ChangeRequest(BaseModel):
    plan_key: str
    camera_quantity: conint(strict=True, ge=MIN_CAMERA_QUANTITY, le=MAX_CAMERA_QUANTITY)

    class Config:
        extra = "forbid"


def _now() -> str:
    return datetime.now().isoformat()


def price_id_for(plan_key: str) -> Optional[str]:
    env = PRICE_ENV.get(plan_key)
    return os.environ.get(env, "").strip() or None if env else None


def _configured_prices() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for key, plan in PLANS.items():
        price_id = price_id_for(key)
        if price_id:
            if price_id in found:
                raise ValueError("A Stripe Price ID is configured for more than one per-camera plan.")
            found[price_id] = {**plan, "plan_key": key, "product": "camera_plan_v2", "price_id": price_id}
    return found


def resolve_price(price_id: str) -> Optional[dict]:
    try:
        plan = _configured_prices().get(str(price_id or "").strip())
    except ValueError:
        return None
    if not plan:
        return None
    plan["product_class"] = "base_v2_noncommissionable" if plan["commission_percent"] == 0 else "base_v2_commissionable"
    return dict(plan)


def public_catalog() -> dict:
    return {
        "catalog_version": 2,
        "currency": "USD",
        "billing_interval": "month",
        "quantity": {"minimum": MIN_CAMERA_QUANTITY, "maximum": MAX_CAMERA_QUANTITY},
        "resolution_classes": [
            {"key": "standard", "display_name": "Standard", "maximum_megapixels": 4},
            {"key": "high_resolution", "display_name": "High Resolution", "minimum_megapixels": 5, "maximum_megapixels": 8},
        ],
        "continuous_cloud_recording_included": False,
        "plans": [
            {"plan_key": key, "display_name": plan["display_name"],
             "monthly_per_camera_amount": plan["monthly_cents_per_camera"] / 100,
             "billing_interval": "month", "included_features": [
                 {"key": feature, "label": FEATURE_LABELS[feature]} for feature in plan["features"]
             ], "local_retention_days": plan["local_retention_days"],
             # Both resolution classes are included at the same per-camera
             # subscription price. Resolution affects local storage sizing.
             "resolution_classes": list(plan["resolution_classes"]),
             "cloud_event_storage": plan["cloud_event_storage"],
             "cloud_event_retention_days": plan["cloud_event_retention_days"],
             "quantity": {"minimum": MIN_CAMERA_QUANTITY, "maximum": MAX_CAMERA_QUANTITY}}
            for key, plan in PLANS.items()
        ],
    }


def _table_exists_query(customer_id: str) -> dict | None:
    return row("SELECT * FROM camera_plan_entitlements_v2 WHERE customer_id=?", (customer_id,))


def entitlement_for_customer(customer_id: str) -> dict | None:
    try:
        return _table_exists_query(customer_id)
    except Exception:
        return None


def _entitlement_status(plan: dict) -> str:
    return str(plan.get("status") or "")


def entitlement_payload(entitlement: dict | None) -> dict | None:
    if not entitlement:
        return None
    plan = PLANS.get(entitlement.get("plan_key"))
    if not plan:
        return None
    quantity = int(entitlement.get("camera_quantity") or 0)
    return {
        "plan_key": entitlement["plan_key"], "display_name": plan["display_name"],
        "status": entitlement.get("status"), "stripe_status": entitlement.get("stripe_status"),
        "camera_quantity": quantity, "monthly_per_camera_amount": plan["monthly_cents_per_camera"] / 100,
        "monthly_base_total": plan["monthly_cents_per_camera"] * quantity / 100,
        "local_retention_days": int(entitlement.get("local_retention_days") or plan["local_retention_days"]),
        "cloud_event_storage": bool(entitlement.get("cloud_event_storage")),
        "cloud_event_retention_days": entitlement.get("cloud_event_retention_days"),
        "resolution_classes": list(plan["resolution_classes"]),
        "included_features": [{"key": feature, "label": FEATURE_LABELS[feature]} for feature in plan["features"]],
        "billing_period_end": entitlement.get("current_period_end"),
        "cancel_at_period_end": bool(entitlement.get("cancel_at_period_end")),
        "scheduled_change": ({"plan_key": entitlement.get("scheduled_plan_key"),
                               "camera_quantity": entitlement.get("scheduled_camera_quantity"),
                               "effective_at": entitlement.get("scheduled_at")}
                              if entitlement.get("scheduled_plan_key") else None),
        "suspended_reason": entitlement.get("suspended_reason"),
    }


def _stripe_item(subscription: dict) -> dict | None:
    matches = []
    for item in ((subscription.get("items") or {}).get("data") or []):
        if not isinstance(item, dict):
            continue
        price = item.get("price") or {}
        price_id = str(price.get("id") if isinstance(price, dict) else price or "")
        plan = resolve_price(price_id)
        if plan:
            matches.append({"item": item, "plan": plan})
    return matches[0] if len(matches) == 1 else None


def _period_end(subscription: dict) -> int | None:
    value = subscription.get("current_period_end")
    items = ((subscription.get("items") or {}).get("data") or [])
    if not value and items and isinstance(items[0], dict):
        value = items[0].get("current_period_end")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _customer_id_for_subscription(subscription: dict) -> str | None:
    metadata = subscription.get("metadata") or {}
    customer_id = str(metadata.get("anyaicam_customer_id") or "")
    if customer_id and row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        return customer_id
    stripe_customer_id = str(subscription.get("customer") or "")
    if stripe_customer_id:
        binding = row("SELECT customer_id FROM stripe_customer_bindings WHERE stripe_customer_id=?", (stripe_customer_id,))
        return str(binding["customer_id"]) if binding else None
    return None


def _upsert_current(subscription: dict, *, session: dict | None = None) -> dict:
    import customer_entitlements as ce
    import stripe_state
    sub_id = str(subscription.get("id") or "")
    stripe_customer_id = str(subscription.get("customer") or "")
    current_item = _stripe_item(subscription)
    if not sub_id or not stripe_customer_id:
        return {"status": "ignored", "reason": "missing subscription/customer"}
    existing_by_sub = row("SELECT * FROM camera_plan_entitlements_v2 WHERE stripe_subscription_id=?", (sub_id,))
    customer_id = _customer_id_for_subscription(subscription)
    if not customer_id:
        return {"status": "ignored", "reason": "subscription owner is unresolved"}
    metadata_customer = str((subscription.get("metadata") or {}).get("anyaicam_customer_id") or "")
    if metadata_customer and metadata_customer != customer_id:
        return {"status": "rejected", "reason": "subscription metadata and Stripe customer binding disagree"}
    if stripe_state.bound_elsewhere(stripe_customer_id, customer_id):
        return {"status": "rejected", "reason": "Stripe customer belongs to another account"}
    existing = existing_by_sub or entitlement_for_customer(customer_id)
    if existing and existing.get("stripe_subscription_id") != sub_id:
        conflict = stripe_state.conflicting_subscription(existing.get("stripe_subscription_id"), subscription)
        if conflict:
            return {"status": "ignored", "reason": conflict}
    status = str(subscription.get("status") or "")
    if status in {"canceled", "incomplete_expired"}:
        # Keep the last purchased quantity for history; capacity readers ignore canceled rows.
        if existing and existing.get("stripe_subscription_id") == sub_id:
            with connection() as db:
                db.execute("UPDATE camera_plan_entitlements_v2 SET status='cancelled',stripe_status=?,camera_quantity=0,"
                           "current_period_end=?,cancel_at_period_end=?,updated_at=? WHERE id=?",
                           (status, _period_end(subscription), 1 if subscription.get("cancel_at_period_end") else 0,
                            _now(), existing["id"]))
        return {"status": "cancelled"}
    if status == "incomplete":
        return {"status": "not_granted", "reason": "first payment is incomplete"}
    if not current_item:
        return {"status": "ignored", "reason": "current subscription has no unique verified per-camera price"}
    item, plan = current_item["item"], current_item["plan"]
    try:
        quantity = int(item.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if quantity < MIN_CAMERA_QUANTITY or quantity > MAX_CAMERA_QUANTITY:
        return {"status": "rejected", "reason": "Stripe quantity is outside the supported account range"}
    # Stripe pending_update has not collected the upgrade payment. Keep the prior paid entitlement.
    if subscription.get("pending_update"):
        return {"status": "pending_update", "entitlement_id": existing.get("id") if existing else None}
    if existing and existing.get("stripe_subscription_id") == sub_id and existing.get("status") == "suspended":
        if existing.get("suspended_reason") in {"refunded", "disputed"}:
            try:
                reversal = stripe_state.subscription_payment_reversal(subscription)
            except stripe_state.RetryableStripeEventError:
                raise
            if reversal:
                status = "suspended"
            else:
                status = "active"
        elif existing.get("suspended_reason") == "payment_failed" and status in {"active", "trialing"}:
            # billing_status owns restoration after checking unresolved invoices.
            status = "suspended"
    if status not in {"active", "trialing", "past_due", "unpaid"}:
        return {"status": "ignored", "reason": "subscription status does not grant service"}
    if status in {"past_due", "unpaid"}:
        failed_at = (existing or {}).get("payment_failed_at") or _now()
        entitlement_status, reason = "active", None  # existing 7-day grace keeps service active
    else:
        failed_at = None
        entitlement_status = "active" if status == "active" or status == "trialing" else "suspended"
        reason = None
    # Check the payment covering this period before granting a newly paid entitlement.
    if not existing or existing.get("stripe_subscription_id") != sub_id or existing.get("status") not in {"active", "suspended"}:
        reversal = stripe_state.subscription_payment_reversal(subscription)
        if reversal:
            entitlement_status, reason = "suspended", reversal
    schedule = subscription.get("schedule")
    schedule_id = str((schedule.get("id") if isinstance(schedule, dict) else schedule) or "") or None
    scheduled_key = (existing or {}).get("scheduled_plan_key")
    scheduled_quantity = (existing or {}).get("scheduled_camera_quantity")
    scheduled_at = (existing or {}).get("scheduled_at")
    if scheduled_key == plan["plan_key"] and int(scheduled_quantity or 0) == quantity:
        # Stripe has applied the next phase; remove the obsolete local schedule.
        scheduled_key = scheduled_quantity = scheduled_at = None
    now = _now()
    with connection() as db:
        if existing:
            db.execute("UPDATE camera_plan_entitlements_v2 SET plan_key=?,camera_quantity=?,stripe_customer_id=?,"
                       "stripe_subscription_id=?,stripe_subscription_item_id=?,stripe_price_id=?,status=?,stripe_status=?,"
                       "payment_failed_at=?,suspended_reason=?,current_period_end=?,cancel_at_period_end=?,"
                       "local_retention_days=?,cloud_event_storage=?,cloud_event_retention_days=?,"
                       "scheduled_plan_key=?,scheduled_camera_quantity=?,scheduled_at=?,"
                       "stripe_schedule_id=?,updated_at=? WHERE id=?",
                       (plan["plan_key"], quantity, stripe_customer_id, sub_id, str(item.get("id") or ""), plan["price_id"],
                        entitlement_status, status, failed_at, reason, _period_end(subscription),
                        1 if subscription.get("cancel_at_period_end") else 0, plan["local_retention_days"],
                        1 if plan["cloud_event_storage"] else 0, plan["cloud_event_retention_days"],
                        scheduled_key, scheduled_quantity, scheduled_at, schedule_id, now, existing["id"]))
            entitlement_id = existing["id"]
        else:
            entitlement_id = uuid.uuid4().hex
            db.execute("INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,"
                       "stripe_subscription_id,stripe_subscription_item_id,stripe_price_id,status,stripe_status,payment_failed_at,"
                       "suspended_reason,current_period_end,cancel_at_period_end,local_retention_days,cloud_event_storage,"
                       "cloud_event_retention_days,stripe_schedule_id,created_at,updated_at)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (entitlement_id, customer_id, plan["plan_key"], quantity, stripe_customer_id, sub_id,
                        str(item.get("id") or ""), plan["price_id"], entitlement_status, status, failed_at, reason,
                        _period_end(subscription), 1 if subscription.get("cancel_at_period_end") else 0,
                        plan["local_retention_days"], 1 if plan["cloud_event_storage"] else 0,
                        plan["cloud_event_retention_days"], schedule_id, now, now))
    import checkout_guard
    checkout_guard.bind_stripe_customer(customer_id, stripe_customer_id)
    _complete_matching_attempt(sub_id, plan["plan_key"], quantity)
    return {"status": "entitlement_updated", "entitlement_id": entitlement_id, "customer_id": customer_id,
            "plan_key": plan["plan_key"], "camera_quantity": quantity}


def sync_from_stripe_event(event: dict) -> dict:
    event_type = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    if event_type.startswith("checkout.session."):
        if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            return {"status": "ignored"}
        metadata = obj.get("metadata") or {}
        if metadata.get("anyaicam_billing_version") != "2":
            return {"status": "ignored"}
        if obj.get("mode") != "subscription" or obj.get("payment_status") not in {"paid", "no_payment_required"}:
            return {"status": "not_granted", "reason": "checkout payment is incomplete"}
        import stripe_state
        current = stripe_state.current_subscription(str(obj.get("subscription") or "") or None)
        if current is None:
            return {"status": "ignored", "reason": "checkout has no current subscription"}
        stripe_item = _stripe_item(current)
        try:
            expected_quantity = int(metadata.get("anyaicam_camera_quantity") or 0)
        except (TypeError, ValueError):
            expected_quantity = 0
        if (not stripe_item or stripe_item["plan"]["plan_key"] != str(metadata.get("anyaicam_plan_key") or "")
                or int(stripe_item["item"].get("quantity") or 0) != expected_quantity):
            return {"status": "rejected", "reason": "checkout metadata does not match Stripe's current plan and quantity"}
        return _upsert_current(current, session=obj)
    if not event_type.startswith("customer.subscription."):
        return {"status": "ignored"}
    import stripe_state
    current = stripe_state.current_subscription(str(obj.get("id") or "") or None)
    if current is None:
        return {"status": "ignored", "reason": "subscription id is missing"}
    result = _upsert_current(current)
    return result


def subscription_for_customer(customer_id: str) -> dict | None:
    return entitlement_payload(entitlement_for_customer(customer_id))


def active_quantity(customer_id: str) -> int:
    entitlement = entitlement_for_customer(customer_id)
    return int(entitlement.get("camera_quantity") or 0) if entitlement and entitlement.get("status") == "active" else 0


def product_mode(customer_id: str) -> str:
    entitlement = entitlement_for_customer(customer_id)
    if entitlement and entitlement.get("status") == "active":
        return str(PLANS.get(entitlement.get("plan_key"), {}).get("mode") or "")
    return ""


def vms_license_capacity(customer_id: str) -> int:
    entitlement = entitlement_for_customer(customer_id)
    return int(entitlement.get("camera_quantity") or 0) if entitlement and entitlement.get("status") == "active" else 0


def _set_subscription_payment_failed(subscription_id: str, at=None) -> None:
    with connection() as db:
        db.execute("UPDATE camera_plan_entitlements_v2 SET payment_failed_at=COALESCE(payment_failed_at,?),updated_at=? "
                   "WHERE stripe_subscription_id=? AND status IN ('active','suspended')",
                   (datetime.fromtimestamp(int(at)).isoformat() if isinstance(at, (int, float)) and at else _now(), _now(), subscription_id))


def _suspend(subscription_id: str, reason: str) -> int:
    with connection() as db:
        return db.execute("UPDATE camera_plan_entitlements_v2 SET status='suspended',suspended_reason=?,updated_at=? "
                          "WHERE stripe_subscription_id=? AND status='active'", (reason, _now(), subscription_id)).rowcount


def _restore(subscription_id: str, reasons: tuple[str, ...]) -> int:
    placeholders = ",".join("?" for _ in reasons)
    with connection() as db:
        return db.execute(f"UPDATE camera_plan_entitlements_v2 SET status='active',suspended_reason=NULL,updated_at=? "
                          f"WHERE stripe_subscription_id=? AND status='suspended' AND suspended_reason IN ({placeholders})",
                          (_now(), subscription_id, *reasons)).rowcount


def _resume_paid(subscription_id: str) -> int:
    with connection() as db:
        db.execute("UPDATE camera_plan_entitlements_v2 SET payment_failed_at=NULL,updated_at=? WHERE stripe_subscription_id=?",
                   (_now(), subscription_id))
    return _restore(subscription_id, ("payment_failed",))


def _save_scheduled(customer_id: str, plan_key: str, quantity: int, effective_at: int, schedule_id: str) -> None:
    with connection() as db:
        db.execute("UPDATE camera_plan_entitlements_v2 SET scheduled_plan_key=?,scheduled_camera_quantity=?,scheduled_at=?,"
                   "stripe_schedule_id=?,updated_at=? WHERE customer_id=?",
                   (plan_key, quantity, int(effective_at), schedule_id, _now(), customer_id))


def _change_attempt(customer_id: str, subscription_id: str, plan_key: str, price_id: str, quantity: int, action: str) -> dict:
    """One durable open operation per subscription; retries share its key."""
    with connection() as db:
        existing = db.execute("SELECT * FROM camera_plan_change_attempts_v2 WHERE stripe_subscription_id=? AND open_slot='open'",
                              (subscription_id,)).fetchone()
        if existing:
            if (existing["plan_key"], existing["stripe_price_id"], int(existing["camera_quantity"]), existing["action"]) != (plan_key, price_id, quantity, action):
                raise HTTPException(status_code=409, detail="Another subscription change is still being confirmed.")
            return dict(existing)
        attempt_id = uuid.uuid4().hex
        letters = "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
        idem = f"anyaicam-v2-{action}-{letters}-{attempt_id}"
        db.execute("INSERT INTO camera_plan_change_attempts_v2(id,customer_id,stripe_subscription_id,plan_key,stripe_price_id,"
                   "camera_quantity,action,idempotency_key,open_slot,status,created_at,updated_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?,'open',?,?)",
                   (attempt_id, customer_id, subscription_id, plan_key, price_id, quantity, action, idem, "open", _now(), _now()))
        return dict(db.execute("SELECT * FROM camera_plan_change_attempts_v2 WHERE id=?", (attempt_id,)).fetchone())


def _close_attempt(attempt_id: str, status: str) -> None:
    with connection() as db:
        db.execute("UPDATE camera_plan_change_attempts_v2 SET status=?,open_slot=NULL,updated_at=? WHERE id=?",
                   (status, _now(), attempt_id))


def _complete_matching_attempt(subscription_id: str, plan_key: str, quantity: int) -> None:
    with connection() as db:
        db.execute("UPDATE camera_plan_change_attempts_v2 SET status='completed',open_slot=NULL,updated_at=? "
                   "WHERE stripe_subscription_id=? AND plan_key=? AND camera_quantity=? AND open_slot='open'",
                   (_now(), subscription_id, plan_key, quantity))


def _addon_portal_rows(customer_id: str, plan_key: str, is_owner: bool) -> str:
    import pricing_catalog
    import analytics_entitlements as analytics
    from customer_analytics_panel import ANALYTIC_LABELS
    held = set(analytics.active_addon_keys(customer_id))
    active_features = set(analytics.get_active_analytics_for_customer(customer_id))
    aliases = {
        "ai_essentials": ("Advanced People Counting", "Advanced people-counting rules and reports"),
        "ai_professional": ("Advanced People Counting + PPE", "Advanced people-counting rules and PPE detection"),
        "vehicle_intelligence": ("LPR / Vehicle Intelligence", "License Plate Recognition and vehicle matching"),
        "advanced_analytics": ("Advanced Analytics", "LPR, advanced people counting, and PPE"),
        "talk_down": ("Talk Down / two-way audio", "Supported cameras and sites"),
    }
    site_count = max(1, len(__import__("partner_db").rows("SELECT id FROM sites WHERE customer_id=?", (customer_id,))))
    door_count = analytics.door_count_for_customer(customer_id)
    face_size = analytics.face_access_size_for_customer(customer_id)
    face_held = any(key.startswith("face_access_") or key == "facial_recognition" for key in held) or "facial_recognition" in active_features
    rendered = []
    for key, label, analytic_keys, env_var in analytics.ANALYTICS_CATALOG:
        if key == "facial_recognition" or key.startswith("face_access_"):
            continue
        price_id = os.environ.get(env_var, "").strip()
        is_active = key in held or key in active_features or (key == "advanced_analytics" and {"people_counting", "lpr", "ppe"} <= active_features)
        if key == "talk_down" and plan_key in {"ai_local", "hybrid"} and not is_active:
            continue
        item = analytics.checkout_item(key, customer_id)
        if is_active:
            action = '<span class="pill">Active</span>'
        elif not price_id or not item["sellable"]:
            action = '<span class="pending-badge" aria-disabled="true">Not available yet</span>'
        elif is_owner:
            quantity = site_count if item["unit"] == "per_site" else 1
            action = f'<button class="ghost-button addon-buy-button" data-addon-key="{escape(key, quote=True)}" data-quantity="{quantity}">Add</button>'
        else:
            action = '<span class="health-detail">Not purchased</span>'
        display, description = aliases.get(key, (label, ", ".join(ANALYTIC_LABELS[k][0] for k in analytic_keys if k in ANALYTIC_LABELS)))
        catalog_item = pricing_catalog.find_addon(key)
        price = (f"${catalog_item['monthly_cents'] / 100:.2f}/mo" if catalog_item and catalog_item["monthly_cents"] is not None else "")
        if key == "talk_down":
            description = f"{site_count} sites" if site_count > 1 else description
        details = " · ".join(value for value in (price, description) if value)
        rendered.append(f'<div class="health-row"><span>{escape(display)}<br><span class="health-detail">{escape(details)}</span></span>{action}</div>')
    for tier in pricing_catalog.face_access_tiers():
        key = f"face_access_{tier['size']}"
        is_active = key in held or key in active_features or (key == "face_access_small" and "facial_recognition" in active_features)
        if not is_active and (face_held or key != f"face_access_{face_size}"):
            continue
        item = analytics.checkout_item(key, customer_id)
        price = f"${tier['monthly_cents_per_door'] / 100:.2f}/mo per door"
        detail = f"{door_count} door{'s' if door_count != 1 else ''}" if door_count else "No door cameras set up yet"
        if is_active:
            action = '<span class="pill">Active</span>'
        elif not os.environ.get(tier["price_env_var"], "").strip() or not item["sellable"]:
            action = '<span class="pending-badge" aria-disabled="true">Not available yet</span>'
        elif is_owner:
            action = '<button class="ghost-button addon-buy-button" data-addon-key="' + escape(key, quote=True) + '" data-quantity="1">Add</button>'
        else:
            action = '<span class="health-detail">Not purchased</span>'
        rendered.append(f'<div class="health-row"><span>Face Access / Facial Recognition — {escape(tier["size"].title())}<br><span class="health-detail">{price} · {detail}</span></span>{action}</div>')
    if "enterprise" == face_size and not face_held:
        rendered.append('<div class="health-row"><span>Face Access Enterprise<br><span class="health-detail">Custom pricing</span></span><span class="health-detail">Contact AnyAiCam</span></div>')
    if not rendered:
        rendered.append('<p class="health-detail">No premium analytics add-ons are available for purchase yet.</p>')
    rendered.append('<p class="health-detail">LPR / Vehicle Intelligence, Face Access, advanced people counting, PPE, and other available premium analytics remain separately licensed. Advanced Line Crossing is not sold separately today; existing Smart Motion and line-crossing behavior are unchanged. A separate Advanced Line Crossing entitlement may be offered in the future.</p>')
    return "".join(rendered)


def customer_portal_page(identity: dict, entitlement: dict | None) -> str:
    """The v2 My Subscription panel. Legacy accounts stay on the legacy
    renderer; v2 customers see their exact quantity, plan entitlements and
    separately licensed premium analytics."""
    import main
    import customer_entitlements
    import customer_billing
    import friends_family
    customer_id = identity["customer_id"]
    payload = entitlement_payload(entitlement)
    is_owner = identity.get("role") == "customer_owner"
    is_active = bool(payload and payload.get("status") == "active")
    plan_key = payload.get("plan_key") if payload else ""
    billing_note = ""
    if payload and payload.get("status") == "suspended":
        billing_note = '<p class="health-detail">Your plan is paused because of a billing problem. Update your payment method or contact AnyAiCam support.</p>'
    elif payload and payload.get("cancel_at_period_end"):
        billing_note = '<p class="health-detail">Your plan will stay active until the end of the current paid period and will not renew.</p>'
    elif payload and payload.get("billing_period_end"):
        billing_note = f'<p class="health-detail">Renews at the end of the current billing period ({int(payload["billing_period_end"])}).</p>'
    features = payload["included_features"] if payload else []
    plan_summary = "No per-camera plan selected yet. Choose a plan and licensed camera count to continue."
    current_html = ""
    if payload:
        plan_summary = (f"{escape(payload['display_name'])} · {payload['camera_quantity']} licensed cameras · "
                        f"${payload['monthly_per_camera_amount']:.2f} per camera/month · "
                        f"${payload['monthly_base_total']:.2f} monthly base total")
        retention = f"{payload['local_retention_days']}-day local retention"
        if payload["cloud_event_storage"]:
            retention += f" · {payload['cloud_event_retention_days']}-day cloud EVENT retention"
        current_html = f'<div class="health-row"><span>Licensed cameras</span><span>{payload["camera_quantity"]}</span></div><div class="health-row"><span>Retention</span><span>{retention}</span></div>'
    feature_html = "".join(f'<div class="health-row"><span>{escape(item["label"])}</span><span class="pill">Included</span></div>' for item in features)
    actions = []
    if is_owner and payload and payload.get("status") in {"active", "suspended"}:
        actions.append('<button class="ghost-button" id="v2-cancel-plan">Cancel at period end</button>')
    if is_owner and customer_billing.portal_enabled() and customer_billing.stripe_customer_ids_for_customer(customer_id):
        actions.append('<button class="ghost-button" id="manage-billing-button">Manage billing</button>')
    action_html = f'<div class="plan-actions">{"".join(actions)}</div><p id="v2-message" class="health-detail" role="status"></p>' if actions else '<p id="v2-message" class="health-detail" role="status"></p>'
    picker = ""
    if is_owner and not payload:
        options = "".join(f'<option value="{key}">{escape(plan["display_name"])} · ${plan["monthly_cents_per_camera"] / 100:.2f} per camera/month</option>' for key, plan in PLANS.items())
        picker = ('<section class="panel" id="v2-checkout"><h2>Choose a per-camera plan</h2>'
                  '<label>Plan <select id="v2-plan-key">' + options + '</select></label> '
                  '<label>Cameras <input id="v2-quantity" type="number" min="1" max="64" value="1"></label> '
                  '<p id="v2-total" class="health-detail"></p><button class="action-button" id="v2-checkout-button">Continue to secure checkout</button></section>')
    change = ""
    if is_owner and payload and is_active:
        options = "".join(f'<option value="{key}" {"selected" if key == plan_key else ""}>{escape(plan["display_name"])}</option>' for key, plan in PLANS.items())
        change = ('<section class="panel" style="margin-top:14px"><h2>Change plan or camera count</h2>'
                  f'<label>Plan <select id="v2-change-plan">{options}</select></label> '
                  f'<label>Licensed cameras <input id="v2-change-quantity" type="number" min="1" max="64" value="{payload["camera_quantity"]}"></label> '
                  '<p id="v2-change-total" class="health-detail"></p><button class="action-button" id="v2-change-button">Review change</button>'
                  '<p class="health-detail">Increases and upgrades take effect after payment succeeds. Decreases and downgrades take effect at the next renewal.</p></section>')
    plan_menu = {key: {"cents": plan["monthly_cents_per_camera"], "label": plan["display_name"]} for key, plan in PLANS.items()}
    addons = _addon_portal_rows(customer_id, plan_key, is_owner)
    vms_capacity = customer_entitlements.vms_license_capacity(customer_id)
    license_card = f'<div class="health-row"><span>VMS license</span><span class="pill">Included with the subscription · {payload["camera_quantity"] if payload else vms_capacity} cameras</span></div>' if payload else (
        f'<div class="health-row"><span>Existing stand-alone VMS license</span><span class="pill">{vms_capacity} cameras</span></div>' if vms_capacity else '')
    content = f'''<header class="topbar"><div><p class="eyebrow">Customer self-service</p><h1>My subscription</h1></div></header>
    <section class="panel"><h2>Current plan</h2><p id="v2-plan-summary">{plan_summary}</p>{current_html}{billing_note}{action_html}</section>
    {friends_family.customer_panel_html()}
    <section class="panel" style="margin-top:14px"><h2>Included with {escape(payload['display_name']) if payload else 'your selected plan'}</h2>{feature_html or '<p class="health-detail">Included features will appear here after you select a plan.</p>'}</section>
    <section class="panel" style="margin-top:14px"><h2>Premium Add-ons</h2><p class="health-detail">Advanced analytics are licensed separately from Basic Local, AI Local, and Hybrid.</p>{addons}<p id="subscription-addon-message" class="health-detail"></p></section>
    <section class="panel" style="margin-top:14px"><h2>Software license</h2>{license_card or '<p class="health-detail">The stand-alone software license remains a separate one-time purchase for local ownership.</p>'}</section>
    {picker}{change}'''
    scripts = f'''<script>
    const v2Plans={json.dumps(plan_menu)};
    function v2Total(planEl,quantityEl,targetEl){{if(!planEl||!quantityEl||!targetEl)return;const p=v2Plans[planEl.value],q=Math.max(0,Number(quantityEl.value)||0);targetEl.textContent=p?'Monthly base total: $'+(p.cents*q/100).toFixed(2):'';}}
    const v2Plan=document.getElementById('v2-plan-key'),v2Qty=document.getElementById('v2-quantity');
    const v2Target=document.getElementById('v2-total');if(v2Plan&&v2Qty){{v2Plan.onchange=()=>v2Total(v2Plan,v2Qty,v2Target);v2Qty.oninput=()=>v2Total(v2Plan,v2Qty,v2Target);v2Total(v2Plan,v2Qty,v2Target);}}
    const v2ChangePlan=document.getElementById('v2-change-plan'),v2ChangeQty=document.getElementById('v2-change-quantity'),v2ChangeTarget=document.getElementById('v2-change-total');if(v2ChangePlan&&v2ChangeQty){{v2ChangePlan.onchange=()=>v2Total(v2ChangePlan,v2ChangeQty,v2ChangeTarget);v2ChangeQty.oninput=()=>v2Total(v2ChangePlan,v2ChangeQty,v2ChangeTarget);v2Total(v2ChangePlan,v2ChangeQty,v2ChangeTarget);}}
    const checkoutButton=document.getElementById('v2-checkout-button');if(checkoutButton)checkoutButton.onclick=async()=>{{const message=document.getElementById('v2-message');checkoutButton.disabled=true;message.textContent='';let response,data;try{{response=await fetch('/api/v2/customer/subscription/checkout',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan_key:v2Plan.value,camera_quantity:Number(v2Qty.value)}})}});data=await response.json();}}catch(e){{checkoutButton.disabled=false;message.textContent='Could not reach the server.';return;}}if(!response.ok){{checkoutButton.disabled=false;message.textContent=data.detail||'Checkout could not be started.';return;}}location.href=data.checkout_url;}};
    const changeButton=document.getElementById('v2-change-button');if(changeButton)changeButton.onclick=async()=>{{const message=document.getElementById('v2-message');if(!confirm('Apply this plan or camera-count change?'))return;changeButton.disabled=true;const response=await fetch('/api/v2/customer/subscription/change',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan_key:v2ChangePlan.value,camera_quantity:Number(v2ChangeQty.value)}})}});const data=await response.json();if(!response.ok){{changeButton.disabled=false;message.textContent=data.detail||'The change could not be completed.';return;}}message.textContent=data.message||'The subscription was updated.';setTimeout(()=>location.reload(),1000);}};
    const cancelButton=document.getElementById('v2-cancel-plan');if(cancelButton)cancelButton.onclick=async()=>{{if(!confirm('Cancel at the end of the paid period?'))return;const response=await fetch('/api/v2/customer/subscription/cancel',{{method:'POST'}}),data=await response.json();document.getElementById('v2-message').textContent=data.detail||'Your plan will stay active through the paid period.';if(response.ok)setTimeout(()=>location.reload(),1000);}};
    document.querySelectorAll('.addon-buy-button').forEach(button=>button.onclick=async()=>{{const message=document.getElementById('subscription-addon-message');button.disabled=true;const response=await fetch('/api/customer/analytics/checkout',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{addon_key:button.dataset.addonKey,quantity:Number(button.dataset.quantity||1)}})}}),data=await response.json();if(!response.ok){{button.disabled=false;message.textContent=data.detail||'Add-on checkout could not be started.';return;}}location.href=data.checkout_url;}});
    const manage=document.getElementById('manage-billing-button');if(manage)manage.onclick=async()=>{{const response=await fetch('/api/customer/billing-portal',{{method:'POST'}}),data=await response.json();if(response.ok)location.href=data.url;else document.getElementById('v2-message').textContent=data.detail||'Billing management could not be opened.';}};
    </script>'''
    return main.page_shell("My subscription", "subscription-portal", content, scripts)


def _matches(subscription: dict, plan_key: str, quantity: int) -> bool:
    item = _stripe_item(subscription)
    return bool(item and item["plan"]["plan_key"] == plan_key and int(item["item"].get("quantity") or 0) == quantity
                and not subscription.get("pending_update"))


def _owner_subscription(identity: dict) -> tuple[dict, dict]:
    import stripe_state
    customer_id = str(identity.get("customer_id") or "")
    entitlement = entitlement_for_customer(customer_id)
    if not entitlement or entitlement.get("status") not in {"active", "suspended"}:
        raise HTTPException(status_code=409, detail="There is no current per-camera subscription to change.")
    try:
        current = stripe_state.current_subscription(entitlement.get("stripe_subscription_id"))
    except stripe_state.RetryableStripeEventError as error:
        raise HTTPException(status_code=502, detail="Could not reach Stripe. Please try again shortly.") from error
    if not current or str(current.get("customer") or "") != str(entitlement.get("stripe_customer_id") or ""):
        raise HTTPException(status_code=409, detail="This subscription's billing record needs attention.")
    if _customer_id_for_subscription(current) != customer_id:
        raise HTTPException(status_code=403, detail="This subscription belongs to another account.")
    return entitlement, current


def _change_subscription(identity: dict, request: ChangeRequest) -> dict:
    plan_key = request.plan_key.strip().lower()
    if plan_key not in PLANS:
        raise HTTPException(status_code=400, detail="Unknown plan key.")
    price_id = price_id_for(plan_key)
    if not price_id:
        raise HTTPException(status_code=503, detail=f"No Stripe Price is configured for {plan_key}.")
    import main
    import stripe_state
    entitlement, current = _owner_subscription(identity)
    item = _stripe_item(current)
    if not item:
        raise HTTPException(status_code=409, detail="This subscription's current per-camera Price could not be verified.")
    current_plan = item["plan"]
    current_quantity = int(item["item"].get("quantity") or 0)
    target_quantity = int(request.camera_quantity)
    target = PLANS[plan_key]
    if plan_key == current_plan["plan_key"] and target_quantity == current_quantity:
        open_attempt = row("SELECT id FROM camera_plan_change_attempts_v2 WHERE stripe_subscription_id=? AND open_slot='open'",
                           (str(current["id"]),))
        if open_attempt:
            _close_attempt(open_attempt["id"], "completed")
        _upsert_current(current)
        return {"status": "unchanged", "subscription": entitlement_payload(entitlement)}
    if (entitlement.get("scheduled_plan_key") == plan_key
            and int(entitlement.get("scheduled_camera_quantity") or 0) == target_quantity):
        return {"status": "already_scheduled", "effective_at": entitlement.get("scheduled_at"),
                "message": "That change is already scheduled for the next renewal."}
    immediate = (target["monthly_cents_per_camera"] * target_quantity) > (current_plan["monthly_cents_per_camera"] * current_quantity)
    immediate = immediate or target_quantity > current_quantity
    action = "immediate" if immediate else "period_end"
    main.require_stripe_price_matches_catalog(price_id, target["monthly_cents_per_camera"], "month")
    attempt = _change_attempt(identity["customer_id"], str(current["id"]), plan_key, price_id, target_quantity, action)
    item_id = str(item["item"].get("id") or "")
    if not item_id:
        raise HTTPException(status_code=409, detail="This subscription item could not be verified.")
    if immediate:
        fields = [
            ("items[0][id]", item_id), ("items[0][price]", price_id), ("items[0][quantity]", str(target_quantity)),
            ("proration_behavior", "always_invoice"), ("billing_cycle_anchor", "unchanged"),
            ("payment_behavior", "pending_if_incomplete"), ("metadata[anyaicam_billing_version]", "2"),
            ("metadata[anyaicam_plan_key]", plan_key), ("metadata[anyaicam_customer_id]", identity["customer_id"]),
            ("metadata[anyaicam_stripe_price_id]", price_id),
        ]
        try:
            main.stripe_api_post(f"/v1/subscriptions/{current['id']}", fields, idempotency_key=attempt["idempotency_key"])
        except Exception:
            try:
                current = stripe_state.current_subscription(str(current["id"])) or current
            except stripe_state.RetryableStripeEventError:
                raise HTTPException(status_code=502, detail="The change is still being confirmed. Retry safely in a moment.")
            if not _matches(current, plan_key, target_quantity):
                # Keep the durable attempt open: retry shares its idempotency key.
                raise HTTPException(status_code=502, detail="The change is still being confirmed. Retry safely in a moment.")
        try:
            current = stripe_state.current_subscription(str(current["id"])) or current
        except stripe_state.RetryableStripeEventError as error:
            raise HTTPException(status_code=502, detail="The change is still being confirmed. Retry safely in a moment.") from error
        if current.get("pending_update"):
            # Stripe has an open proration update/invoice. Keep the durable
            # idempotency attempt open so retries cannot create a second update.
            raise HTTPException(status_code=402, detail="The plan change is pending payment. Your current paid entitlement stays in place until Stripe confirms it.")
        if not _matches(current, plan_key, target_quantity):
            _close_attempt(attempt["id"], "declined")
            raise HTTPException(status_code=402, detail="Payment for this change did not complete. Your current plan remains active.")
        result = _upsert_current(current)
        _close_attempt(attempt["id"], "completed")
        return {"status": "updated", "subscription": entitlement_payload(entitlement_for_customer(identity["customer_id"])), "sync": result}
    period_end = _period_end(current)
    if not period_end:
        raise HTTPException(status_code=409, detail="The billing period end could not be confirmed.")
    schedule = current.get("schedule")
    schedule_id = str((schedule.get("id") if isinstance(schedule, dict) else schedule) or "")
    if not schedule_id:
        created = main.stripe_api_post("/v1/subscription_schedules", [("from_subscription", str(current["id"]))],
                                       idempotency_key=f"anyaicam-v2-schedule-{current['id']}-{period_end}")
        schedule_id = str(created.get("id") or "")
    if not schedule_id:
        raise HTTPException(status_code=502, detail="The scheduled change could not be confirmed.")
    phase_start = current.get("current_period_start") or item["item"].get("current_period_start")
    fields = [("end_behavior", "release"), ("proration_behavior", "none")]
    if phase_start:
        fields.append(("phases[0][start_date]", str(phase_start)))
    fields += [
        ("phases[0][items][0][price]", current_plan["price_id"]), ("phases[0][items][0][quantity]", str(current_quantity)),
        ("phases[0][end_date]", str(period_end)), ("phases[1][items][0][price]", price_id),
        ("phases[1][items][0][quantity]", str(target_quantity)),
        ("phases[1][metadata][anyaicam_billing_version]", "2"),
        ("phases[1][metadata][anyaicam_plan_key]", plan_key),
        ("phases[1][metadata][anyaicam_customer_id]", identity["customer_id"]),
        ("phases[1][metadata][anyaicam_stripe_price_id]", price_id),
    ]
    main.stripe_api_post(f"/v1/subscription_schedules/{schedule_id}", fields, idempotency_key=attempt["idempotency_key"])
    _save_scheduled(identity["customer_id"], plan_key, target_quantity, period_end, schedule_id)
    _close_attempt(attempt["id"], "scheduled")
    return {"status": "scheduled", "effective_at": period_end,
            "message": "Your current paid service continues until the scheduled change takes effect."}


def register_routes(app: FastAPI) -> None:
    from partner_portal import partner_identity

    @app.get("/api/v2/pricing/catalog")
    def per_camera_pricing_catalog() -> dict:
        return public_catalog()

    @app.get("/api/v2/customer/subscription")
    def per_camera_customer_subscription(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer sign-in required.")
        return {"subscription": subscription_for_customer(identity["customer_id"])}

    @app.post("/api/v2/customer/subscription/checkout")
    def per_camera_checkout(payload: CheckoutRequest, request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        plan_key = payload.plan_key.strip().lower()
        plan = PLANS.get(plan_key)
        price_id = price_id_for(plan_key)
        if not plan:
            raise HTTPException(status_code=400, detail="Unknown plan key.")
        if not price_id:
            raise HTTPException(status_code=503, detail=f"No Stripe Price is configured for {plan_key}.")
        customer_id = identity["customer_id"]
        if entitlement_for_customer(customer_id) or row(
                "SELECT id FROM customer_entitlements WHERE customer_id=? AND product IN ('camera_slots_local','camera_slots_hybrid') "
                "AND status IN ('active','suspended') LIMIT 1", (customer_id,)):
            raise HTTPException(status_code=409, detail="This account already has a camera plan. Use its plan-change action instead.")
        import main
        if not main.PUBLIC_BASE_URL:
            raise HTTPException(status_code=503, detail="ANYAICAM_PUBLIC_URL is required for Stripe Checkout.")
        main.require_stripe_price_matches_catalog(price_id, plan["monthly_cents_per_camera"], "month")
        import friends_family
        try:
            friends_family.checkout_discount(customer_id, "base")
        except friends_family.CheckoutHeld as held:
            raise HTTPException(status_code=409, detail=str(held))
        except RuntimeError as missing:
            raise HTTPException(status_code=503, detail=str(missing))
        import checkout_guard
        fields = [
            ("mode", "subscription"),
            ("success_url", f"{main.PUBLIC_BASE_URL}/subscription-portal?camera_plan_payment=success&session_id={{CHECKOUT_SESSION_ID}}"),
            ("cancel_url", f"{main.PUBLIC_BASE_URL}/subscription-portal?camera_plan_payment=cancelled"),
            ("client_reference_id", customer_id), ("line_items[0][price]", price_id),
            ("line_items[0][quantity]", str(int(payload.camera_quantity))),
            ("metadata[anyaicam_customer_id]", customer_id), ("metadata[anyaicam_stripe_price_id]", price_id),
            ("metadata[anyaicam_billing_version]", "2"), ("metadata[anyaicam_plan_key]", plan_key),
            ("metadata[anyaicam_camera_quantity]", str(int(payload.camera_quantity))),
            ("subscription_data[metadata][anyaicam_customer_id]", customer_id),
            ("subscription_data[metadata][anyaicam_stripe_price_id]", price_id),
            ("subscription_data[metadata][anyaicam_billing_version]", "2"),
            ("subscription_data[metadata][anyaicam_plan_key]", plan_key),
            ("subscription_data[metadata][anyaicam_camera_quantity]", str(int(payload.camera_quantity))),
        ]
        friends_family.apply_to_checkout_fields(fields, customer_id, "base")
        fields.append(("customer", checkout_guard.canonical_stripe_customer(customer_id, email=identity.get("email"))))
        session = checkout_guard.create_session(customer_id, "base", price_id=price_id,
                                                quantity=int(payload.camera_quantity), fields=fields)
        if not session.get("id") or not session.get("url"):
            raise HTTPException(status_code=502, detail="Stripe did not return a Checkout Session URL.")
        return {"status": "complete", "checkout_url": session["url"], "session_id": session["id"]}

    @app.post("/api/v2/customer/subscription/change")
    def per_camera_change(payload: ChangeRequest, request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        return _change_subscription(identity, payload)

    @app.post("/api/v2/customer/subscription/cancel")
    def per_camera_cancel(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        import main
        entitlement, current = _owner_subscription(identity)
        if current.get("cancel_at_period_end"):
            return {"status": "already_scheduled", "effective_at": _period_end(current)}
        main.stripe_api_post(f"/v1/subscriptions/{current['id']}", [("cancel_at_period_end", "true")],
                             idempotency_key=f"anyaicam-v2-cancel-{current['id']}-{_period_end(current) or ''}")
        current = stripe_state.current_subscription(str(current["id"])) or current
        _upsert_current(current)
        with connection() as db:
            db.execute("UPDATE camera_plan_entitlements_v2 SET cancel_at_period_end=1,updated_at=? WHERE id=?",
                       (_now(), entitlement["id"]))
        return {"status": "cancellation_scheduled", "effective_at": _period_end(current)}
