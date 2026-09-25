"""Stripe TEST analytics-add-on entitlement bridge.

Does NOT invent a second analytics/licensing architecture. The manual
analytics entitlement mechanism already used by master-admin
(customer_platform.py's ANALYTICS_CATALOG) and by the customer-facing
per-camera analytics panel (customer_analytics_panel.py's ANALYTIC_LABELS)
is the pre-existing `analytics_subscriptions` table (partner_db.py,
customer_id/site_id-scoped, keyed by `analytic_key`) -- see
customer_entitlements.py's module docstring, audit finding #3: this table
"tracks per-add-on billing status... also never touched by Stripe." This
module is what makes it Stripe-driven, additively, exactly the way
customer_entitlements.py made camera-slot entitlements Stripe-driven
without replacing the pre-existing `plans`/`customer_entitlements` split.

Site-level licensing vs. per-camera assignment
------------------------------------------------
A Stripe event carries a customer, never a specific camera, so this module
only ever writes the SITE/CUSTOMER-level licensing row (site_id=NULL,
meaning "licensed for this customer at any site" -- the exact semantics
customer_analytics_panel.assign_entitlement()'s own
`(site_id=? OR site_id IS NULL)` query already expects). Assigning a
licensed analytic to one specific camera remains the customer's own manual
action via the pre-existing POST /api/customer/cameras/{camera_id}/
analytics/{analytic_key} route (live_view_page.py), gated by
`licensed_quantity` exactly as it already is today. Nothing here writes to
`camera_analytics_entitlements`.

Billing SKU vs. internal feature-key naming
--------------------------------------------
Correction, 2026-09-21 (superseding this module's original one-SKU-per-
analytic-key design from the same day): Advanced Analytics is ONE
customer-billed, recurring product -- a single Stripe Price -- that
unlocks smart_motion, people_counting, lpr, and ppe together. It is NOT
four separate customer purchases. Face Access (facial_recognition)
remains its own separate recurring add-on, unaffected by this
correction. AACO stays unsellable (see aaco_product_status() below).

This is why ANALYTICS_CATALOG's rows are keyed by a purchasable
`addon_key` (a billing-level SKU: "advanced_analytics", "facial_
recognition", ...), each carrying a tuple of the one or more internal
`analytic_key` feature flags it grants -- NOT keyed by `analytic_key`
directly the way the pre-correction version of this module was. The
internal analytic_keys themselves (smart_motion / people_counting / lpr
/ ppe / facial_recognition / ...) are UNCHANGED: they still match
customer_analytics_panel.ANALYTIC_LABELS exactly, still gate per-camera
assignment through assign_entitlement() exactly as before, and still
each get their own independent analytics_subscriptions row (site_id=
NULL) -- purchasing the "advanced_analytics" SKU simply grants all four
of those rows from one Stripe event instead of requiring four separate
purchases. (Note: customer_platform.py's separate admin-facing
ANALYTICS_CATALOG spells the PPE one "ppe_detection" instead of "ppe" --
a pre-existing inconsistency between two already-existing catalogs, not
something this module's scope changes.) talk_down / ai_essentials /
ai_professional / vehicle_intelligence / cloud_overflow remain single-
analytic_key SKUs (their own addon_key equals their own analytic_key),
unaffected by this correction.

Fail-closed, same as PRICE_ID_CAMERA_SLOT_MAP/HARDWARE_PRICE_MAP
-------------------------------------------------------------------
ANALYTICS_PRICE_MAP is built once from env vars at import time. A Stripe
Price ID with no configured mapping resolves to None and is granted
nothing -- never a guess. Every analytics env var is independent of every
camera-slot (ANYAICAM_STRIPE_PRICE_LOCAL_*/HYBRID_*) and hardware
(ANYAICAM_STRIPE_PRICE_RYZEN_*/RELAY_*) env var, so an analytics purchase
can never grant camera slots or create a hardware order, and vice versa --
this module never imports customer_entitlements or hardware_orders, and
never writes to customer_entitlements/hardware_orders tables.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Optional

from partner_db import connection, row, rows
from stripe_checkout_payment import CHECKOUT_GRANT_EVENT_TYPES, awaiting_payment, awaiting_payment_result

# addon_key (the customer-billed SKU), display label, the tuple of
# internal analytic_key feature flags this SKU grants when purchased
# (see customer_analytics_panel.ANALYTIC_LABELS for what each analytic_
# key actually gates per-camera -- unchanged by this catalog), Stripe
# TEST Price ID env var. "advanced_analytics" is the one row with more
# than one analytic_key -- see the module docstring's 2026-09-21
# correction. Every other row's addon_key equals its sole analytic_key,
# same as before the correction.
ANALYTICS_CATALOG = [
    ("advanced_analytics", "Advanced Analytics", ("smart_motion", "people_counting", "lpr", "ppe"), "ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS"),
    ("facial_recognition", "Face Access", ("facial_recognition",), "ANYAICAM_STRIPE_PRICE_ANALYTICS_FACIAL_RECOGNITION"),
    ("talk_down", "Talk Down", ("talk_down",), "ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN"),
    ("ai_essentials", "AnyAiCam AI Essentials", ("ai_essentials",), "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS"),
    ("ai_professional", "AnyAiCam AI Professional", ("ai_professional",), "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_PROFESSIONAL"),
    ("vehicle_intelligence", "Vehicle Intelligence", ("vehicle_intelligence",), "ANYAICAM_STRIPE_PRICE_ANALYTICS_VEHICLE_INTELLIGENCE"),
    ("cloud_overflow", "Cloud Overflow", ("cloud_overflow",), "ANYAICAM_STRIPE_PRICE_ANALYTICS_CLOUD_OVERFLOW"),
]

ADDON_KEYS = tuple(item[0] for item in ANALYTICS_CATALOG)

# Every internal analytic_key feature flag granted by ANY catalog entry,
# flattened -- used by tests and callers that need "every analytic_key
# this module can ever write to analytics_subscriptions", as opposed to
# ADDON_KEYS ("every purchasable billing SKU").
ANALYTIC_KEYS = tuple(sorted({key for item in ANALYTICS_CATALOG for key in item[2]}))


def aaco_product_status() -> dict:
    """AACO ("future premium add-on" per the 2026-09-21 restructure
    instruction) deliberately has NO ANALYTICS_CATALOG entry, NO price
    env var, and NO checkout/webhook wiring -- inventing a placeholder
    Price ID or analytic_key for a product whose scope, dependency on
    Face Access, and price are all still undecided would be exactly the
    kind of guess this codebase's other resolvers (resolve_tier(),
    resolve_addon(), hardware_orders' resolvers) are built to refuse.
    This function exists only so a future website/pricing page has one
    place to ask "can a customer buy this yet" and get an honest answer
    instead of the page author having to know this history."""
    return {
        "key": "aaco",
        "label": "AACO",
        "sellable": False,
        "reason": "Pricing, scope, and dependency on Face Access are not finalized -- business decision required before any catalog entry, Price ID, or checkout path is built.",
    }


def _load_price_map() -> dict:
    mapping = {}
    for addon_key, label, analytic_keys, env_var in ANALYTICS_CATALOG:
        price_id = os.environ.get(env_var, "").strip()
        if price_id:
            mapping[price_id] = {"addon_key": addon_key, "label": label, "analytic_keys": analytic_keys}
    return mapping


ANALYTICS_PRICE_MAP = _load_price_map()

SUBSCRIPTION_INACTIVE_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


def resolve_addon(price_id: str) -> Optional[dict]:
    """Returns {"addon_key": ..., "label": ..., "analytic_keys": (...)}
    for a server-verified Stripe Price ID, or None if this Price ID has
    no configured addon mapping -- callers must treat None as "grant
    nothing", never fall back to a guessed key. `analytic_keys` is a
    tuple of one (every SKU except "advanced_analytics") or more
    (exactly "advanced_analytics", per the 2026-09-21 correction)
    internal feature flags this one purchase grants together."""
    if not price_id:
        return None
    entry = ANALYTICS_PRICE_MAP.get(price_id)
    if not isinstance(entry, dict):
        return None
    addon_key = str(entry.get("addon_key") or "").strip()
    analytic_keys = tuple(entry.get("analytic_keys") or ())
    if not addon_key or not analytic_keys:
        return None
    return {"addon_key": addon_key, "label": entry.get("label"), "analytic_keys": analytic_keys}


def _find_customer_by_email(email: str) -> Optional[dict]:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    return row("SELECT * FROM customers WHERE lower(email)=?", (normalized,))


def upsert_analytics_subscription(
    *,
    customer_id: str,
    analytic_key: str,
    status: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    stripe_price_id: Optional[str] = None,
) -> dict:
    """Idempotent per (customer_id, analytic_key) at site_id=NULL --
    updates the SAME licensing row in place on a repeat purchase/upgrade/
    cancel cycle rather than accumulating duplicate rows, mirroring
    customer_entitlements.upsert_entitlement()'s per-(customer_id,product)
    design. A field left as None on an update never blanks out a
    previously-recorded Stripe reference (COALESCE), since not every event
    carries every field (a subscription-cancelled event has no new price
    id, for instance)."""
    existing = row(
        "SELECT * FROM analytics_subscriptions WHERE customer_id=? AND analytic_key=? AND site_id IS NULL",
        (customer_id, analytic_key),
    )
    now = datetime.now().isoformat()
    with connection() as db:
        if existing:
            db.execute(
                "UPDATE analytics_subscriptions SET status=?,"
                "stripe_customer_id=COALESCE(?,stripe_customer_id),"
                "stripe_subscription_id=COALESCE(?,stripe_subscription_id),"
                "stripe_price_id=COALESCE(?,stripe_price_id),"
                "updated_at=? WHERE id=?",
                (status, stripe_customer_id, stripe_subscription_id, stripe_price_id, now, existing["id"]),
            )
            subscription_id = existing["id"]
        else:
            subscription_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,"
                "monthly_retail,monthly_partner,licensed_quantity,stripe_customer_id,stripe_subscription_id,"
                "stripe_price_id,created_at,updated_at) VALUES(?,?,NULL,?,?,NULL,NULL,1,?,?,?,?,?)",
                (subscription_id, customer_id, analytic_key, status,
                 stripe_customer_id, stripe_subscription_id, stripe_price_id, now, now),
            )
    return row("SELECT * FROM analytics_subscriptions WHERE id=?", (subscription_id,))


def get_analytics_subscriptions_for_customer(customer_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM analytics_subscriptions WHERE customer_id=? AND site_id IS NULL ORDER BY created_at",
        (customer_id,),
    )


def create_pending_link(
    *, email: str, stripe_price_id: str, analytic_key: str,
    stripe_customer_id: Optional[str] = None,
    stripe_checkout_session_id: Optional[str] = None, raw_event: dict,
) -> dict:
    """Same checkout-before-registration safety net as customer_
    entitlements.create_pending_link() and hardware_orders.create_pending_
    link() -- a website purchase made under an email with no matching
    customer yet (the normal case for a marketing-site visitor who has
    never signed in to the VMS). Without this, an analytics purchase
    under those conditions would previously just return "ignored" and be
    dropped, unlike camera-slot and hardware purchases which already had
    this net."""
    link_id = uuid.uuid4().hex
    with connection() as db:
        db.execute(
            "INSERT INTO pending_analytics_links(id,normalized_email,stripe_customer_id,"
            "stripe_checkout_session_id,stripe_price_id,analytic_key,raw_event_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (link_id, (email or "").strip().lower(), stripe_customer_id, stripe_checkout_session_id,
             stripe_price_id, analytic_key, json.dumps(raw_event), "pending", datetime.now().isoformat()),
        )
    return row("SELECT * FROM pending_analytics_links WHERE id=?", (link_id,))


def resolve_pending_links_for_customer(customer_id: str, email: str) -> list[str]:
    """Mirrors customer_entitlements.resolve_pending_links_for_customer()
    and hardware_orders.resolve_pending_links_for_customer() exactly --
    call only against an email the caller has already verified belongs
    to this customer (e.g. right after registration approval)."""
    normalized = (email or "").strip().lower()
    pending = rows("SELECT * FROM pending_analytics_links WHERE normalized_email=? AND status='pending'", (normalized,))
    resolved_ids = []
    for link in pending:
        upsert_analytics_subscription(
            customer_id=customer_id, analytic_key=link["analytic_key"], status="active",
            stripe_customer_id=link["stripe_customer_id"], stripe_price_id=link["stripe_price_id"],
        )
        with connection() as db:
            db.execute(
                "UPDATE pending_analytics_links SET status='resolved',resolved_at=?,resolved_customer_id=? WHERE id=?",
                (datetime.now().isoformat(), customer_id, link["id"]),
            )
        resolved_ids.append(link["id"])
    return resolved_ids


def get_active_analytics_for_customer(customer_id: str) -> list[str]:
    """Every analytic_key currently licensed for this customer. 'active'
    here means status != 'cancelled', the exact convention
    customer_analytics_panel.py and customer_platform.py already use
    elsewhere to mean 'currently enabled' -- never invented fresh here."""
    return sorted(
        item["analytic_key"]
        for item in get_analytics_subscriptions_for_customer(customer_id)
        if item["analytic_key"] and item["status"] != "cancelled"
    )


def _extract_checkout_fields(session_obj: dict) -> dict:
    email = (session_obj.get("customer_details") or {}).get("email") or session_obj.get("customer_email") or ""
    metadata = session_obj.get("metadata") or {}
    price_id = str(metadata.get("anyaicam_stripe_price_id") or "").strip()
    return {
        "email": email,
        "price_id": price_id,
        "authoritative_customer_id": str(metadata.get("anyaicam_customer_id") or "") or None,
        "stripe_customer_id": str(session_obj.get("customer") or "") or None,
    }


def _sync_checkout_completed(event: dict) -> dict:
    session_obj = (event.get("data") or {}).get("object") or {}
    if awaiting_payment(session_obj):
        return awaiting_payment_result(session_obj)  # granted on async_payment_succeeded
    fields = _extract_checkout_fields(session_obj)

    addon = resolve_addon(fields["price_id"])
    if not addon:
        return {"status": "ignored", "reason": "no verified analytics mapping for this stripe price id", "price_id": fields["price_id"]}

    customer = None
    if fields["authoritative_customer_id"]:
        customer = row("SELECT * FROM customers WHERE id=?", (fields["authoritative_customer_id"],))
    if not customer and fields["email"]:
        customer = _find_customer_by_email(fields["email"])

    if not customer:
        if not fields["email"]:
            return {"status": "ignored", "reason": "no authoritative customer id and no email to reconcile against"}
        # One purchase can grant more than one analytic_key ("advanced_
        # analytics" grants four) -- pending_analytics_links is keyed
        # one row per analytic_key, so a multi-key addon creates one
        # pending row per key. resolve_pending_links_for_customer()
        # already resolves every pending row for an email in one pass,
        # so this fans back out into all N subscriptions correctly once
        # the customer registers, with no change needed there.
        link_ids = []
        for analytic_key in addon["analytic_keys"]:
            link = create_pending_link(
                email=fields["email"], stripe_price_id=fields["price_id"], analytic_key=analytic_key,
                stripe_customer_id=fields["stripe_customer_id"], raw_event=event,
            )
            link_ids.append(link["id"])
        return {"status": "pending_link_created", "pending_link_ids": link_ids, "addon_key": addon["addon_key"], "analytic_keys": list(addon["analytic_keys"])}

    subscription_ids = []
    for analytic_key in addon["analytic_keys"]:
        subscription = upsert_analytics_subscription(
            customer_id=customer["id"], analytic_key=analytic_key, status="active",
            stripe_customer_id=fields["stripe_customer_id"], stripe_price_id=fields["price_id"],
        )
        subscription_ids.append(subscription["id"])
    return {
        "status": "analytics_subscription_updated",
        "subscription_ids": subscription_ids,
        "addon_key": addon["addon_key"],
        "analytic_keys": list(addon["analytic_keys"]),
    }


def _current_subscription_price_id(subscription_obj: dict) -> str:
    items = ((subscription_obj.get("items") or {}).get("data") or [])
    if items and isinstance(items[0], dict):
        price_id = str((items[0].get("price") or {}).get("id") or "").strip()
        if price_id:
            return price_id
    return str((subscription_obj.get("metadata") or {}).get("anyaicam_stripe_price_id") or "").strip()


def _sync_subscription_change(event: dict, *, cancelled: bool) -> dict:
    subscription_obj = (event.get("data") or {}).get("object") or {}
    stripe_customer_id = str(subscription_obj.get("customer") or "")
    metadata = subscription_obj.get("metadata") or {}
    price_id = _current_subscription_price_id(subscription_obj)
    metadata_customer_id = str(metadata.get("anyaicam_customer_id") or "") or None
    if not stripe_customer_id or not price_id:
        return {"status": "ignored", "reason": "missing stripe customer id or anyaicam_stripe_price_id metadata"}

    addon = resolve_addon(price_id)
    if not addon:
        return {"status": "ignored", "reason": "no verified analytics mapping for this stripe price id", "price_id": price_id}

    new_status = "cancelled" if (cancelled or subscription_obj.get("status") in SUBSCRIPTION_INACTIVE_STATUSES) else "active"
    subscription_ids = []
    granted_keys = []
    # One subscription can cover several analytic_keys ("advanced_
    # analytics" covers four) -- update/cancel every one of them
    # together from this one event, each still its own independent row,
    # so a customer's four Advanced Analytics feature flags never drift
    # out of sync with each other or with their single underlying
    # subscription.
    for analytic_key in addon["analytic_keys"]:
        existing = row(
            "SELECT * FROM analytics_subscriptions WHERE stripe_customer_id=? AND analytic_key=? AND site_id IS NULL",
            (stripe_customer_id, analytic_key),
        )
        customer_id = existing["customer_id"] if existing else None
        if not customer_id and metadata_customer_id:
            # Self-healing path, same reasoning as customer_entitlements.
            # _sync_subscription_change(): Stripe delivery order isn't
            # guaranteed, so subscription.updated/deleted can in principle
            # arrive before checkout.session.completed created this row.
            customer_id = metadata_customer_id
        if not customer_id:
            continue
        subscription = upsert_analytics_subscription(
            customer_id=customer_id, analytic_key=analytic_key, status=new_status,
            stripe_customer_id=stripe_customer_id, stripe_subscription_id=str(subscription_obj.get("id") or "") or None,
            stripe_price_id=price_id,
        )
        subscription_ids.append(subscription["id"])
        granted_keys.append(analytic_key)

    if not subscription_ids:
        return {"status": "ignored", "reason": "no existing analytics subscription for this stripe customer/addon"}
    return {
        "status": "analytics_subscription_updated",
        "subscription_ids": subscription_ids,
        "addon_key": addon["addon_key"],
        "analytic_keys": granted_keys,
    }


def sync_analytics_from_stripe_event(event: dict) -> dict:
    """Same three event types customer_entitlements.py's camera-slot sync
    handles, for the same reason: checkout.session.completed is the
    initial grant (it alone carries the metadata needed for first-time
    identity resolution); customer.subscription.created is deliberately
    NOT handled here either, matching that same precedent -- the checkout
    event already grants on first purchase, so acting on the companion
    subscription.created event would be redundant, not additive."""
    event_type = str(event.get("type") or "")
    if event_type in CHECKOUT_GRANT_EVENT_TYPES:
        return _sync_checkout_completed(event)
    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        return _sync_subscription_change(event, cancelled=event_type.endswith("deleted"))
    return {"status": "ignored", "reason": f"unhandled event type {event_type!r}"}
