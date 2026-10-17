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
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from partner_db import connection, row, rows
from stripe_checkout_payment import CHECKOUT_GRANT_EVENT_TYPES, awaiting_payment, awaiting_payment_result

logger = logging.getLogger("anyaicam.analytics_entitlements")

# addon_key (the customer-billed SKU), display label, the tuple of
# internal feature keys this SKU grants when purchased, Stripe Price ID
# env var. 2026-09-30: built from pricing_catalog (the one authoritative
# catalog) instead of being maintained here:
# - the four analytics packages grant real per-camera analytics (owner-
#   approved mapping): AI Essentials -> people_counting; AI Professional
#   -> people_counting + ppe; Vehicle Intelligence -> lpr; Advanced
#   Analytics -> people_counting + lpr + ppe. Smart Motion is no longer
#   sold: it is included with every paid Local/Hybrid plan
#   (pricing_catalog.INCLUDED_FEATURES, see included_feature_active()).
#   Existing smart_motion rows from earlier Advanced purchases are left
#   untouched -- nothing a customer already has is removed.
# - Talk Down grants talk_down AND voice_call (Voice Call is included).
# - Face Access is sold per door in three sizes; the original single
#   facial_recognition SKU stays mapped so existing purchases still
#   resolve on renewal/cancellation.
# Packages can overlap (people_counting is in three), so which packages a
# customer holds is tracked per addon_key in addon_subscriptions, and a
# feature is only switched off when no remaining active package grants it.
import pricing_catalog as _catalog

ANALYTICS_CATALOG = (
    [(a["addon_key"], a["label"], tuple(a["grants"]), a["price_env_var"]) for a in _catalog.addons()]
    + [(f"face_access_{t['size']}", t["label"], tuple(t["grants"]), t["price_env_var"]) for t in _catalog.face_access_tiers()]
    + [("facial_recognition", "Face Access", tuple(_catalog.FACE_ACCESS_GRANTS), "ANYAICAM_STRIPE_PRICE_ANALYTICS_FACIAL_RECOGNITION")]
)
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
        # 2026-09-30: AACO is included with every paid Local/Hybrid plan
        # (pricing_catalog.INCLUDED_FEATURES) -- never sold on its own.
        "included_with_plans": True,
        "reason": "AACO is included with every paid Local and Hybrid plan; it is not sold separately.",
    }


def _load_price_map() -> dict:
    mapping = {}
    for addon_key, label, analytic_keys, env_var in ANALYTICS_CATALOG:
        price_id = os.environ.get(env_var, "").strip()
        if price_id:
            mapping[price_id] = {"addon_key": addon_key, "label": label, "analytic_keys": analytic_keys}
    return mapping


ANALYTICS_PRICE_MAP = _load_price_map()

# Ended in Stripe; past_due/unpaid get the 7-day grace (billing_status.py).
SUBSCRIPTION_INACTIVE_STATUSES = {"canceled", "incomplete_expired"}


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
    # Same rule as the camera-plan webhook (shared via the neutral customer_lookup module, keeping this module
    # separate from customer_entitlements): a suspended or closed account never receives a purchase by email.
    import customer_lookup
    return customer_lookup.find_usable_customer_by_email(email)


def upsert_analytics_subscription(
    *,
    customer_id: str,
    analytic_key: str,
    status: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    stripe_price_id: Optional[str] = None,
    licensed_quantity: Optional[int] = None,
) -> dict:
    """Idempotent per (customer_id, analytic_key) at site_id=NULL --
    updates the SAME licensing row in place on a repeat purchase/upgrade/
    cancel cycle rather than accumulating duplicate rows, mirroring
    customer_entitlements.upsert_entitlement()'s per-(customer_id,product)
    design. A field left as None on an update never blanks out a
    previously-recorded Stripe reference (COALESCE), since not every event
    carries every field (a subscription-cancelled event has no new price
    id, for instance). Stripe quantities are copied into each per-analytic
    license count. Missing quantities preserve existing values and default
    new legacy/manual rows to one."""
    if licensed_quantity is not None and (isinstance(licensed_quantity, bool) or int(licensed_quantity) < 1 or int(licensed_quantity) > 256):
        raise ValueError("licensed_quantity must be between 1 and 256")
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
                "licensed_quantity=COALESCE(?,licensed_quantity),"
                "updated_at=? WHERE id=?",
                (status, stripe_customer_id, stripe_subscription_id, stripe_price_id, licensed_quantity, now, existing["id"]),
            )
            subscription_id = existing["id"]
        else:
            subscription_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,"
                "monthly_retail,monthly_partner,licensed_quantity,stripe_customer_id,stripe_subscription_id,"
                "stripe_price_id,created_at,updated_at) VALUES(?,?,NULL,?,?,NULL,NULL,?,?,?,?,?,?)",
                (subscription_id, customer_id, analytic_key, status,
                 licensed_quantity or 1, stripe_customer_id, stripe_subscription_id, stripe_price_id, now, now),
            )
    if analytic_key == "facial_recognition" and status != "cancelled":
        promote_requested_doors(customer_id)
    return row("SELECT * FROM analytics_subscriptions WHERE id=?", (subscription_id,))


NO_DOORS_REASON = ("Face Access is billed per door. Turn on \"Enable Face Access for this camera\" in "
                   "Camera Settings for at least one door camera, then add it here.")


def checkout_item(addon_key: str, customer_id: Optional[str] = None) -> dict:
    """Whether this SKU can be bought right now, how it is counted and which
    Friends & Family discount class it belongs to -- all from
    pricing_catalog. The original single-price Face Access SKU is kept for
    existing subscriptions but is no longer sold; Face Access is sold per
    door in the size that matches the customer's enrolled people."""
    addon = _catalog.find_addon(addon_key)
    if addon:
        return {"sellable": addon["sellable"], "unavailable_reason": addon["unavailable_reason"],
                "unit": addon["unit"], "discount_class": addon["discount_class"]}
    tier = next((t for t in _catalog.face_access_tiers() if f"face_access_{t['size']}" == addon_key), None)
    if tier:
        result = {"sellable": tier["sellable"], "unavailable_reason": tier["unavailable_reason"],
                  "unit": "per_door", "discount_class": "face_access"}
        if tier["sellable"] and customer_id:
            size = face_access_size_for_customer(customer_id)
            if size == _catalog.FACE_ACCESS_ENTERPRISE:
                result.update(sellable=False, unavailable_reason="More than 500 enrolled people needs Face Access Enterprise pricing. Contact AnyAiCam.")
            elif size != tier["size"]:
                result.update(sellable=False, unavailable_reason=f"Your account needs Face Access {size.title()} for the people enrolled.")
            elif door_count_for_customer(customer_id) < 1:
                # Billed per door: with no door set up there is nothing to bill.
                result.update(sellable=False, unavailable_reason=NO_DOORS_REASON)
        return result
    return {"sellable": False, "unavailable_reason": "Face Access is now sold per door by size.",
            "unit": "per_account", "discount_class": "face_access"}


def face_access_size_for_customer(customer_id: str) -> str:
    enrolled = row("SELECT COUNT(*) AS n FROM facial_people WHERE customer_id=? AND status='active'", (customer_id,))
    return _catalog.face_access_size_for(int((enrolled or {}).get("n") or 0))


def door_count_for_customer(customer_id: str) -> int:
    """Doors for per-door Face Access billing: cameras set up with a door,
    including a door set up before the add-on was bought
    (door_access_requested -- see door_access.update_door_config())."""
    doors = row("SELECT COUNT(*) AS n FROM cameras WHERE customer_id=? AND "
                "(door_access_enabled=1 OR COALESCE(door_access_requested,0)=1)", (customer_id,))
    return int((doors or {}).get("n") or 0)


def promote_requested_doors(customer_id: str) -> int:
    """Face Access is now held: the doors the owner set up while waiting for
    it (door_access_requested=1, relay channel chosen) become real doors.
    Called from upsert_analytics_subscription(); never raises."""
    try:
        with connection() as db:
            return db.execute(
                "UPDATE cameras SET door_access_enabled=1,door_access_requested=0 WHERE customer_id=? "
                "AND COALESCE(door_access_requested,0)=1 AND door_relay_channel IS NOT NULL",
                (customer_id,),
            ).rowcount
    except Exception:
        logger.exception("face_access.promote_requested_doors_failed customer_id=%s", customer_id)
        return 0


def _grants_for(addon_key: str) -> tuple:
    return next((item[2] for item in ANALYTICS_CATALOG if item[0] == addon_key), ())


def upsert_addon_subscription(
    *, customer_id: str, addon_key: str, status: str, quantity: Optional[int] = None,
    stripe_customer_id: Optional[str] = None, stripe_subscription_id: Optional[str] = None,
    stripe_price_id: Optional[str] = None,
) -> dict:
    """Package-level state, one row per (customer_id, addon_key). This is
    what makes overlapping packages safe: a feature row is only cancelled
    when no remaining active package grants it (see _cancel_features())."""
    existing = row("SELECT * FROM addon_subscriptions WHERE customer_id=? AND addon_key=?", (customer_id, addon_key))
    now = datetime.now().isoformat()
    with connection() as db:
        if existing:
            db.execute(
                "UPDATE addon_subscriptions SET status=?,quantity=COALESCE(?,quantity),"
                "stripe_customer_id=COALESCE(?,stripe_customer_id),stripe_subscription_id=COALESCE(?,stripe_subscription_id),"
                "stripe_price_id=COALESCE(?,stripe_price_id),updated_at=? WHERE id=?",
                (status, quantity, stripe_customer_id, stripe_subscription_id, stripe_price_id, now, existing["id"]),
            )
            record_id = existing["id"]
        else:
            record_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO addon_subscriptions(id,customer_id,addon_key,status,quantity,stripe_customer_id,"
                "stripe_subscription_id,stripe_price_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record_id, customer_id, addon_key, status, quantity or 1, stripe_customer_id,
                 stripe_subscription_id, stripe_price_id, now, now),
            )
    return row("SELECT * FROM addon_subscriptions WHERE id=?", (record_id,))


def active_addon_keys(customer_id: str) -> list[str]:
    return [item["addon_key"] for item in rows(
        "SELECT addon_key FROM addon_subscriptions WHERE customer_id=? AND status='active' ORDER BY addon_key", (customer_id,))]


def feature_granted_by_other_addon(customer_id: str, feature_key: str, *, excluding: str) -> bool:
    return any(feature_key in _grants_for(key) for key in active_addon_keys(customer_id) if key != excluding)


def licensed_quantity_for_feature(customer_id: str, feature_key: str) -> int:
    """Sum active purchased package quantities that grant one feature."""
    total = 0
    for item in rows("SELECT addon_key,quantity FROM addon_subscriptions WHERE customer_id=? AND status='active'",
                     (customer_id,)):
        if feature_key in _grants_for(item["addon_key"]):
            total += _positive_int(item.get("quantity")) or 1
    return min(256, total)


def account_wide_feature_active(db, customer_id: str, feature_key: str) -> bool:
    """True when this feature covers every camera on the account: an
    INCLUDED feature (Smart Motion, ...) while the customer holds an
    active paid Local/Hybrid plan, or a feature granted by an active flat
    package (packages are never per camera). Takes the caller's open
    connection so it can run inside customer_analytics_panel's own
    transaction."""
    try:
        import per_camera_billing
        v2 = per_camera_billing.entitlement_for_customer(customer_id)
    except Exception:
        v2 = None
    if v2 and v2.get("status") == "active":
        plan = per_camera_billing.PLANS.get(v2.get("plan_key"), {})
        included = set(plan.get("features") or [])
        # AAC Voice Call is NOT part of ordinary Talk Down (owner decision
        # 2026-10-05): no plan grants it; it comes only from a package that
        # grants voice_call itself (the add-on loop below), so an account that
        # already holds one keeps it.
        v2_feature = {"smart_motion": "smart_motion", "talk_down": "supported_talk_down",
                      "aaco": "core_aaco_retrieval"}.get(feature_key)
        if v2_feature and v2_feature in included:
            return True
        # Not in the plan: Talk Down bought as an add-on (Basic Local) is
        # honoured by the add-on loop below; Smart Motion and AACO stay
        # plan-only exactly as before (next check).
        # Do not fall through to the legacy Local/Hybrid global feature set:
        # Basic Local must not inherit Smart Motion or AACO from that model.
        if feature_key in _catalog.INCLUDED_FEATURE_KEYS:
            return False
    elif feature_key in _catalog.INCLUDED_FEATURE_KEYS:
        plan = db.execute(
            "SELECT 1 FROM customer_entitlements WHERE customer_id=? AND status='active' AND camera_slot_quantity>0 "
            "AND product IN ('camera_slots_local','camera_slots_hybrid') LIMIT 1",
            (customer_id,),
        ).fetchone()
        if plan:
            return True
    for item in db.execute(
        "SELECT addon_key FROM addon_subscriptions WHERE customer_id=? AND status='active'", (customer_id,)
    ).fetchall():
        if feature_key in _grants_for(item["addon_key"]):
            return True
    return False


def plan_included_feature_active(db, customer_id: str, feature_key: str) -> bool:
    """Return only base-plan inclusion, excluding paid analytics add-ons.

    Paid analytics subscriptions carry a finite ``licensed_quantity`` and
    are assigned per camera. ``account_wide_feature_active`` intentionally
    also answers whether a flat add-on grants a feature for legacy callers;
    the Analytics Control Center must use this narrower predicate so an
    add-on purchase cannot silently authorize every camera.
    """
    try:
        import per_camera_billing
        entitlement = per_camera_billing.entitlement_for_customer(customer_id)
    except Exception:
        entitlement = None
    if entitlement and entitlement.get("status") == "active":
        plan = per_camera_billing.PLANS.get(entitlement.get("plan_key"), {})
        feature = {"smart_motion": "smart_motion", "talk_down": "supported_talk_down",
                   "aaco": "core_aaco_retrieval"}.get(feature_key)
        return bool(feature and feature in set(plan.get("features") or []))
    if feature_key in _catalog.INCLUDED_FEATURE_KEYS:
        return bool(db.execute(
            "SELECT 1 FROM customer_entitlements WHERE customer_id=? AND status='active' "
            "AND camera_slot_quantity>0 AND product IN ('camera_slots_local','camera_slots_hybrid') LIMIT 1",
            (customer_id,),
        ).fetchone())
    return False


def commercial_camera_analytic_authorized(db, *, customer_id: str, site_id: str | None,
                                          camera_id: str, analytic_key: str) -> bool:
    """Fail-closed per-camera authorization against the purchased seat count.

    A subscription alone never activates every camera. The camera must have
    an active assignment row, and the customer's active assignment count
    must fit the applicable site- or account-scoped license quantity.
    Base-plan Smart Motion is unlimited only for cameras explicitly assigned
    in ``camera_analytics_entitlements``.
    """
    assigned = db.execute(
        "SELECT 1 FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=? AND status='active'",
        (camera_id, analytic_key),
    ).fetchone()
    if not assigned:
        return False
    if plan_included_feature_active(db, customer_id, analytic_key):
        return True
    subscription = db.execute(
        "SELECT site_id,licensed_quantity FROM analytics_subscriptions "
        "WHERE customer_id=? AND (site_id=? OR site_id IS NULL) AND analytic_key=? AND status='active' "
        "ORDER BY site_id IS NULL LIMIT 1",
        (customer_id, site_id, analytic_key),
    ).fetchone()
    if not subscription:
        return False
    quantity = int(subscription["licensed_quantity"] or 0)
    if quantity < 1:
        return False
    if subscription["site_id"] is not None:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM camera_analytics_entitlements e JOIN cameras c ON c.id=e.camera_id "
            "WHERE c.customer_id=? AND c.site_id=? AND e.analytic_key=? AND e.status='active'",
            (customer_id, site_id, analytic_key),
        ).fetchone()["n"]
    else:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM camera_analytics_entitlements e JOIN cameras c ON c.id=e.camera_id "
            "WHERE c.customer_id=? AND e.analytic_key=? AND e.status='active'",
            (customer_id, analytic_key),
        ).fetchone()["n"]
    # No arbitrary camera ordering when licenses are reduced. Fail closed for
    # the affected analytic until the owner restores seats or reassigns the
    # excess seats; this cannot silently choose a different customer camera.
    return int(count) <= quantity


def effective_camera_analytic(db, customer_id: str, site_id, analytic_key: str, stored, cache: dict | None = None):
    """A camera's stored per-camera analytic flag (cameras.<key>_enabled) as
    the appliance must act on it (2026-10-10).

    The stored value is the customer's choice and is never rewritten here, so
    re-subscribing restores it. For a billing-v2 (per-camera plan) customer a
    PLAN-INCLUDED analytic (pricing_catalog.INCLUDED_FEATURE_KEYS: Smart
    Motion) is reported enabled only while something still grants it -- the
    same gate customer_analytics_panel.assign_entitlement() applies when it is
    switched on: an active plan that includes it (account_wide_feature_active)
    or a licence row in analytics_subscriptions that is not cancelled. A
    cancelled Hybrid plan, or a downgrade to Basic Local, therefore turns it
    off on the edge at the next configuration sync.

    Unchanged (the stored value): People Counting, LPR and PPE (add-ons or an
    administrator's explicit choice, never part of a plan) and every legacy /
    partner-managed customer -- mirroring effective_cloud_recording_mode()."""
    if not stored or analytic_key not in _catalog.INCLUDED_FEATURE_KEYS or not customer_id:
        return stored
    try:
        import per_camera_billing
        v2 = per_camera_billing.entitlement_for_customer(customer_id)
    except Exception:
        v2 = None
    if not v2:
        return stored
    key = (str(customer_id), str(site_id or ""), analytic_key)
    if cache is not None and key in cache:
        allowed = cache[key]
    else:
        allowed = plan_included_feature_active(db, customer_id, analytic_key) or db.execute(
            "SELECT 1 FROM analytics_subscriptions WHERE customer_id=? AND (site_id=? OR site_id IS NULL) "
            "AND analytic_key=? AND status!='cancelled' LIMIT 1", (customer_id, site_id, analytic_key)).fetchone() is not None
        if cache is not None:
            cache[key] = allowed
    return stored if allowed else 0


def get_analytics_subscriptions_for_customer(customer_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM analytics_subscriptions WHERE customer_id=? AND site_id IS NULL ORDER BY created_at",
        (customer_id,),
    )


def create_pending_link(
    *, email: str, stripe_price_id: str, analytic_key: str,
    stripe_customer_id: Optional[str] = None,
    stripe_checkout_session_id: Optional[str] = None, raw_event: dict,
    licensed_quantity: int = 1,
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
            "stripe_checkout_session_id,stripe_price_id,analytic_key,licensed_quantity,raw_event_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (link_id, (email or "").strip().lower(), stripe_customer_id, stripe_checkout_session_id,
             stripe_price_id, analytic_key, licensed_quantity, json.dumps(raw_event), "pending", datetime.now().isoformat()),
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
            licensed_quantity=int(link.get("licensed_quantity") or 1),
        )
        with connection() as db:
            db.execute(
                "UPDATE pending_analytics_links SET status='resolved',resolved_at=?,resolved_customer_id=? WHERE id=?",
                (datetime.now().isoformat(), customer_id, link["id"]),
            )
        resolved_ids.append(link["id"])
        addon = resolve_addon(link["stripe_price_id"])
        if addon:
            upsert_addon_subscription(
                customer_id=customer_id, addon_key=addon["addon_key"], status="active",
                quantity=int(link.get("licensed_quantity") or 1),
                stripe_customer_id=link["stripe_customer_id"], stripe_price_id=link["stripe_price_id"],
            )
    return resolved_ids


def apply_addon_state(addon: dict, *, status: str, suspended_reason: str | None) -> None:
    """billing_status: suspend or restore one add-on and its features
    (features another active add-on grants stay on)."""
    resolved = resolve_addon(str(addon.get("stripe_price_id") or ""))
    keys = list(resolved["analytic_keys"]) if resolved else []
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute("UPDATE addon_subscriptions SET status=?,suspended_reason=?,updated_at=? WHERE id=?",
                   (status, suspended_reason, now, addon["id"]))
    for analytic_key in keys:
        feature_status = "active" if status == "active" else "cancelled"
        if feature_status == "cancelled" and feature_granted_by_other_addon(addon["customer_id"], analytic_key, excluding=addon["addon_key"]):
            feature_status = "active"
        aggregate_quantity = licensed_quantity_for_feature(addon["customer_id"], analytic_key)
        upsert_analytics_subscription(customer_id=addon["customer_id"], analytic_key=analytic_key, status=feature_status,
                                      stripe_customer_id=addon.get("stripe_customer_id"),
                                      stripe_subscription_id=addon.get("stripe_subscription_id"),
                                      stripe_price_id=addon.get("stripe_price_id"),
                                      licensed_quantity=aggregate_quantity or None)


def get_active_analytics_for_customer(customer_id: str) -> list[str]:
    try:
        import billing_status
        billing_status.sweep_grace(customer_id=customer_id)
    except Exception:
        pass
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


def _positive_int(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _stripe_subscription_quantity(subscription_obj: dict, price_id: str, fallback=None) -> int:
    """Read the purchased item quantity from Stripe's current subscription.

    Checkout metadata is only a compatibility fallback for old test and
    legacy webhook fixtures. When Stripe includes a matching line item, its
    quantity is authoritative and malformed/zero values fail closed.
    """
    items = ((subscription_obj.get("items") or {}).get("data") or [])
    matching = [item for item in items if isinstance(item, dict)
                and str((item.get("price") or {}).get("id") or "") == str(price_id)]
    if matching:
        raw = matching[0].get("quantity")
        quantity = _positive_int(raw)
        return quantity or 0
    # If Stripe supplied line items but none maps to this entitlement's
    # configured price, metadata must not stand in for a purchased quantity.
    # This can occur during a price change or with a malformed/mixed
    # subscription object; fail closed rather than granting a seat.
    if items:
        return 0
    return _positive_int(fallback) or 1


def _sync_checkout_completed(event: dict) -> dict:
    session_obj = (event.get("data") or {}).get("object") or {}
    if awaiting_payment(session_obj):
        return awaiting_payment_result(session_obj)  # granted on async_payment_succeeded
    fields = _extract_checkout_fields(session_obj)

    addon = resolve_addon(fields["price_id"])
    if not addon:
        return {"status": "ignored", "reason": "no verified analytics mapping for this stripe price id", "price_id": fields["price_id"]}
    if checkout_item(addon["addon_key"])["unit"] == "per_door" and (
            not _positive_int((session_obj.get("metadata") or {}).get("anyaicam_quantity"))
            or ("amount_total" in session_obj and int(session_obj.get("amount_total") or 0) <= 0)):
        # Billed per door: no paid door, no Face Access.
        return {"status": "ignored", "reason": "per-door add-on without a paid door quantity", "addon_key": addon["addon_key"]}

    customer = None
    if fields["authoritative_customer_id"]:
        customer = row("SELECT * FROM customers WHERE id=?", (fields["authoritative_customer_id"],))
    if not customer and fields["email"]:
        customer = _find_customer_by_email(fields["email"])
    # Same ownership and current-state rules as camera plans (2026-10-02,
    # customer_entitlements): a Stripe customer paying for another account
    # never provisions this one, and a subscription that has already ended in
    # Stripe is never granted by a late checkout event.
    from stripe_state import (RetryableStripeEventError, bound_elsewhere as _bound_elsewhere, conflicting_subscription,
                              current_subscription, subscription_payment_reversal)
    if customer and _bound_elsewhere(fields["stripe_customer_id"], customer["id"]):
        return {"status": "rejected", "reason": "stripe customer belongs to another account", "customer_id": customer["id"]}
    subscription_id = str(session_obj.get("subscription") or "") or None
    current = current_subscription(subscription_id) if session_obj.get("mode") == "subscription" else None
    if current is None:
        # Every add-on is a subscription; without Stripe's current state of
        # it nothing is granted (Codex audit of 3f5b9c4, finding 9).
        return {"status": "ignored", "reason": "add-on checkout without a subscription"}
    current_meta = str((current.get("metadata") or {}).get("anyaicam_customer_id") or "") or None
    if (fields["stripe_customer_id"] and str(current.get("customer") or "") != fields["stripe_customer_id"]) or \
            (customer and current_meta and current_meta != customer["id"]):
        return {"status": "rejected", "reason": "checkout and its subscription disagree about the account"}
    if current.get("status") in SUBSCRIPTION_INACTIVE_STATUSES:
        return {"status": "not_granted", "reason": "subscription is no longer active in Stripe"}
    if current.get("status") == "incomplete":  # first payment not completed: no service (finding 5)
        return {"status": "not_granted", "reason": "the first payment has not completed"}
    quantity = _stripe_subscription_quantity(
        current, fields["price_id"], (session_obj.get("metadata") or {}).get("anyaicam_quantity"))
    if not quantity:
        return {"status": "not_granted", "reason": "the Stripe subscription has an invalid analytics quantity"}
    held_addon = row("SELECT * FROM addon_subscriptions WHERE customer_id=? AND addon_key=?",
                     (customer["id"], addon["addon_key"])) if customer else None
    if held_addon:
        conflict = conflicting_subscription(held_addon.get("stripe_subscription_id"), current)  # finding 3
        if conflict:
            return {"status": "ignored", "reason": conflict}
    reversal = subscription_payment_reversal(current)  # finding 4

    if not customer:
        if not fields["email"]:
            return {"status": "ignored", "reason": "no authoritative customer id and no email to reconcile against"}
        if reversal:
            return {"status": "not_granted", "reason": f"the payment was {reversal}"}
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
                licensed_quantity=quantity,
            )
            link_ids.append(link["id"])
        return {"status": "pending_link_created", "pending_link_ids": link_ids, "addon_key": addon["addon_key"], "analytic_keys": list(addon["analytic_keys"])}

    keep_reason = (held_addon or {}).get("suspended_reason") if (
        held_addon and held_addon.get("status") == "suspended" and held_addon.get("stripe_subscription_id") == subscription_id
        and held_addon.get("suspended_reason") != "payment_incomplete") else None
    package = upsert_addon_subscription(
        customer_id=customer["id"], addon_key=addon["addon_key"], status="active",
        quantity=quantity,
        stripe_customer_id=fields["stripe_customer_id"], stripe_price_id=fields["price_id"],
        stripe_subscription_id=subscription_id,
    )
    subscription_ids = []
    for analytic_key in addon["analytic_keys"]:
        subscription = upsert_analytics_subscription(
            customer_id=customer["id"], analytic_key=analytic_key, status="active",
            stripe_customer_id=fields["stripe_customer_id"], stripe_price_id=fields["price_id"],
            licensed_quantity=quantity,
        )
        subscription_ids.append(subscription["id"])
    if reversal or keep_reason:
        # A refunded/disputed payment, or a suspension only billing_status
        # lifts: the package and its features stay off.
        apply_addon_state(package, status="suspended", suspended_reason=reversal or keep_reason)
    else:
        apply_addon_state(package, status="active", suspended_reason=None)
    if fields["stripe_customer_id"]:
        import checkout_guard
        checkout_guard.bind_stripe_customer(customer["id"], fields["stripe_customer_id"])
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
    from stripe_state import (conflicting_subscription, current_subscription, stripe_customer_accounts,
                              subscription_payment_reversal, unresolved)
    current = current_subscription(str(subscription_obj.get("id") or "") or None)
    if current is None:
        return {"status": "ignored", "reason": "subscription event without a subscription id"}
    # Stripe's current state wins over this event's snapshot, and nothing is
    # applied without it (2026-10-02; Codex finding 9).
    subscription_obj = current
    cancelled = current.get("status") in SUBSCRIPTION_INACTIVE_STATUSES
    stripe_customer_id = str(subscription_obj.get("customer") or "")
    metadata = subscription_obj.get("metadata") or {}
    price_id = _current_subscription_price_id(subscription_obj)
    metadata_customer_id = str(metadata.get("anyaicam_customer_id") or "") or None
    if not stripe_customer_id or not price_id:
        return {"status": "ignored", "reason": "missing stripe customer id or anyaicam_stripe_price_id metadata"}

    addon = resolve_addon(price_id)
    if not addon:
        return {"status": "ignored", "reason": "no verified analytics mapping for this stripe price id", "price_id": price_id}

    owners = stripe_customer_accounts(stripe_customer_id)
    if metadata_customer_id and owners and owners != {metadata_customer_id}:
        return {"status": "rejected", "reason": "stripe customer and subscription metadata name different accounts"}
    if not owners and (not metadata_customer_id or not row("SELECT id FROM customers WHERE id=?", (metadata_customer_id,))):
        return unresolved("add_on", str(subscription_obj.get("id") or "") or None, stripe_customer_id)
    owner = metadata_customer_id or (next(iter(owners)) if len(owners) == 1 else None)
    package_row = row("SELECT * FROM addon_subscriptions WHERE customer_id=? AND addon_key=?", (owner, addon["addon_key"])) if owner else None
    if package_row:
        # A delayed event for an older subscription never alters the
        # account's newer one (finding 3).
        conflict = conflicting_subscription(package_row.get("stripe_subscription_id"), subscription_obj)
        if conflict:
            return {"status": "ignored", "reason": conflict}
    new_status = "cancelled" if (cancelled or subscription_obj.get("status") in SUBSCRIPTION_INACTIVE_STATUSES) else "active"
    new_reason = None
    held = row("SELECT status,suspended_reason FROM addon_subscriptions WHERE stripe_subscription_id=? AND addon_key=?",
               (str(subscription_obj.get("id") or ""), addon["addon_key"]))
    if new_status == "active" and subscription_obj.get("status") == "incomplete":
        # First payment not completed: no service (finding 5).
        if not held or held["status"] != "active":
            return {"status": "not_granted", "reason": "the first payment has not completed"}
        new_status, new_reason = "suspended", "payment_incomplete"
    if held and held["status"] == "suspended" and new_status == "active" and held.get("suspended_reason") != "payment_incomplete":
        new_status = "suspended"  # only billing_status lifts a suspension
    if new_status == "active" and (not held or held["status"] != "active"):
        reversal = subscription_payment_reversal(subscription_obj)  # finding 4
        if reversal:
            new_status, new_reason = "suspended", reversal
    quantity = _stripe_subscription_quantity(subscription_obj, price_id)
    if not quantity:
        return {"status": "rejected", "reason": "Stripe subscription has an invalid analytics quantity"}
    subscription_ids = []
    granted_keys = []
    package_customer_id = None
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
        if package_customer_id is None:
            package_customer_id = customer_id
            upsert_addon_subscription(
                customer_id=customer_id, addon_key=addon["addon_key"], status=new_status, quantity=quantity,
                stripe_customer_id=stripe_customer_id, stripe_subscription_id=str(subscription_obj.get("id") or "") or None,
                stripe_price_id=price_id,
            )
        # Overlapping packages: a feature another active package still
        # grants stays active when this package is cancelled.
        # A suspended add-on's features are off (the add-on row keeps
        # 'suspended' and why); another active add-on can still grant them.
        feature_status = "active" if new_status == "active" else "cancelled"
        if feature_status == "cancelled" and feature_granted_by_other_addon(customer_id, analytic_key, excluding=addon["addon_key"]):
            feature_status = "active"
        subscription = upsert_analytics_subscription(
            customer_id=customer_id, analytic_key=analytic_key, status=feature_status,
            stripe_customer_id=stripe_customer_id, stripe_subscription_id=str(subscription_obj.get("id") or "") or None,
            stripe_price_id=price_id, licensed_quantity=quantity,
        )
        subscription_ids.append(subscription["id"])
        granted_keys.append(analytic_key)

    if not subscription_ids:
        return {"status": "ignored", "reason": "no existing analytics subscription for this stripe customer/addon"}
    if package_customer_id is not None:
        package = row("SELECT * FROM addon_subscriptions WHERE customer_id=? AND addon_key=?", (package_customer_id, addon["addon_key"]))
        if package and (new_reason or (new_status == "active" and package.get("suspended_reason"))):
            apply_addon_state(package, status=new_status, suspended_reason=new_reason)
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
