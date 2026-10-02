"""Authoritative entitlement model for AnyAiCam customer provisioning
(Phase 1: website -> Stripe -> AWS -> customer -> camera slots ->
installation activation).

Audit finding this module exists to fix
----------------------------------------
Before this module, the codebase had THREE separate, disconnected
"entitlement-shaped" concepts, none of which is both (a) driven by a
real, verified Stripe payment and (b) tied to the authoritative
`customers` table (partner_db.py) that partner_workspace.py,
provisioning_service.py, and every appliance/camera row already use:

1. `billing_accounts.json` + the live POST /api/payments/stripe/webhook
   handler in main.py (`process_stripe_webhook_event()`) + `current_user()`
   + `LICENSE_PLAN_FEATURES`. This IS driven by real Stripe events, but
   it is keyed to the legacy VMS user system (users.json/current_user()),
   not to `customers.id` -- exactly the "legacy identity system" the
   provisioning spec says not to build on. `create_stripe_checkout()`
   calls `current_user(request)`, not `partner_identity(request)`.

2. `plans` table (partner_db.py, customer_id-scoped). Has a
   `camera_quantity` column and IS read by /customer/setup's Step 6
   review -- but it is only ever populated by a partner typing numbers
   into the "Add New Customer" onboarding wizard. No Stripe event has
   ever written to this table.

3. `analytics_subscriptions` table (partner_db.py, customer_id-scoped).
   Tracks per-add-on (`analytic_key`) billing status with a
   `licensed_quantity` column -- also partner-entered pricing, also
   never touched by Stripe.

None of the three can answer "did this customer actually pay for N
camera slots, verified by Stripe, right now" for the authoritative
customer identity. This module is that answer. It is deliberately
additive: it does not replace or migrate #1-#3 (that is a product
decision for a later phase, once a partner-facing reconciliation between
partner-quoted `plans`/`analytics_subscriptions` and Stripe-verified
`customer_entitlements` is designed).

Camera-slot capacity must never be hard-coded into the installer: any
future claim/refresh code must call `total_camera_slots()`, never read a
constant.

Phase 2 update: wired into production + authoritative-identity-first
------------------------------------------------------------------
`sync_entitlement_from_stripe_event()` is now called from the live POST
/api/payments/stripe/webhook route in main.py (additively -- after the
existing legacy `process_stripe_webhook_event()` call, in its own
try/except so a failure here can never break the legacy 200 response
Stripe needs to stop retrying). `create_stripe_checkout()` carries the
authoritative `customers.id` through Stripe metadata
(`anyaicam_customer_id`, set only for a real signed-in customer_owner
session) so identity resolution never depends on customer-supplied
email when a session already exists.

Phase 3 update: FIXED camera-slot tiers, server-side only
-----------------------------------------------------------
Approved product decision: AnyAiCam uses fixed camera-slot tiers (e.g.
"1-16 cameras"), never an arbitrary customer-chosen quantity. Phase 2's
`anyaicam_camera_slot_quantity` metadata (the browser-chosen Stripe
line-item quantity) is REMOVED as an entitlement-quantity source -- it
was exactly the "trust a browser-submitted number" gap Phase 3's
security review explicitly closes (see PRICE_ID_CAMERA_SLOT_MAP below).
`create_stripe_checkout()` also now hard-codes the Stripe line-item
quantity to 1 rather than a browser-submitted number, so even Stripe's
own `quantity` field can never be used as a slot-count vector if a
future maintainer starts reading it.

The new, sole source of camera-slot quantity is PRICE_ID_CAMERA_SLOT_MAP:
a server-side, env-configured mapping from the exact Stripe Price ID
purchased (carried through as `anyaicam_stripe_price_id` metadata --
selected server-side in create_stripe_checkout() via stripe_price_map(),
never client-supplied) to {"product": ..., "camera_slot_maximum": ...}.
A checkout or subscription event referencing a Price ID that is not in
this map is NEVER granted any camera slots -- fail closed, not a guess.

Phase 3 audit finding: no fixed camera-slot tier (including the
confirmed "1-16 cameras = $14.99" tier) has a proven Stripe Price ID
anywhere in existing configuration. Checked: (a) this app's own
STRIPE_PRICE_STARTER/PROFESSIONAL/ENTERPRISE env vars -- unset on the
one production-style instance checked; (b) the entire anyaicam.com PHP
website codebase's Stripe integration (stripe-config.php, checkout.php,
stripe-webhook.php) -- which is scoped EXCLUSIVELY to one-time Videoloft
adapter/camera/PoE hardware purchases; checkout.php's own comment reads
"Only adapter payments are accepted through Stripe. Cloud billing is
handled separately by Videoloft." No camera-slot subscription Price ID
or product exists in either place. PRICE_ID_CAMERA_SLOT_MAP therefore
ships EMPTY by default (`{}`) -- see the Phase 3 report for what
production configuration is still required before this can grant real
entitlements, and why none was invented here.

Phase 4 update: verified Local/Hybrid pricing from the AWS cost model
----------------------------------------------------------------------
PLAN_TIERS replaces the placeholder 1-16/17-32 example from Phase 3 with
the real 8-tier structure verified directly from AnyAiCam_AWS_Cost_
Model_2026-09.xlsx's "Local" and "Hybrid" sheets (read from the workbook
itself, not retyped from a description): four camera-count bands (1-8,
9-16, 17-32, 33-64) under each of two separate service lines, Local and
Hybrid, each with its own monthly retail price and its own `product`
key (`camera_slots_local`/`camera_slots_hybrid`) so the two service
lines can never collide even where their camera_slot_maximum coincides.
PRICE_ID_CAMERA_SLOT_MAP is now built from PLAN_TIERS instead of a raw
JSON env var. Re-audited against this same set of sources (this app's
env vars, pricing_config.py, and the entire anyaicam.com PHP codebase)
for all 8 exact price points -- still none proven anywhere; every tier's
Price ID env var ships unset.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Optional

from partner_db import connection, row, rows
from stripe_checkout_payment import CHECKOUT_GRANT_EVENT_TYPES, awaiting_payment, awaiting_payment_result


# Stripe's current state and Stripe-customer ownership (2026-10-02): see
# stripe_state.py, shared with add-ons.
from stripe_state import RetryableStripeEventError, bound_elsewhere as _bound_elsewhere, current_subscription, unresolved


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _now() -> str:
    return datetime.now().isoformat()


def find_customer_by_email(email: str) -> Optional[dict]:
    normalized = normalize_email(email)
    if not normalized:
        return None
    return row("SELECT * FROM customers WHERE lower(email)=?", (normalized,))


# --------------------------------------------------------------- entitlements


def upsert_entitlement(
    *,
    customer_id: str,
    product: str,
    camera_slot_quantity: int,
    status: str = "active",
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    stripe_checkout_session_id: Optional[str] = None,
    stripe_price_id: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> dict:
    """Idempotent per (customer_id, product) -- see module docstring for
    why this updates in place rather than accumulating rows. `product` is
    a STABLE category string (e.g. always "camera_slots"), not a
    per-tier name -- an upgrade/downgrade between fixed tiers must update
    this SAME row, never fragment into one row per tier; stripe_price_id
    records which specific tier is currently active. A field left as
    None on an update never blanks out a previously-recorded Stripe
    reference (COALESCE), since not every event carries every field
    (e.g. a subscription-cancelled event has no checkout_session_id)."""
    existing = row("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (customer_id, product))
    now = _now()
    with connection() as db:
        if existing:
            db.execute(
                "UPDATE customer_entitlements SET camera_slot_quantity=?,status=?,"
                "stripe_customer_id=COALESCE(?,stripe_customer_id),"
                "stripe_subscription_id=COALESCE(?,stripe_subscription_id),"
                "stripe_checkout_session_id=COALESCE(?,stripe_checkout_session_id),"
                "stripe_price_id=COALESCE(?,stripe_price_id),"
                "expires_at=?,updated_at=? WHERE id=?",
                (camera_slot_quantity, status, stripe_customer_id, stripe_subscription_id,
                 stripe_checkout_session_id, stripe_price_id, expires_at, now, existing["id"]),
            )
            entitlement_id = existing["id"]
        else:
            entitlement_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,"
                "stripe_customer_id,stripe_subscription_id,stripe_checkout_session_id,stripe_price_id,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (entitlement_id, customer_id, product, camera_slot_quantity, status,
                 stripe_customer_id, stripe_subscription_id, stripe_checkout_session_id, stripe_price_id, now, now, expires_at),
            )
    return row("SELECT * FROM customer_entitlements WHERE id=?", (entitlement_id,))


def get_entitlements_for_customer(customer_id: str) -> list:
    # Access never outlives the failed-payment grace period just because no
    # event arrived (billing_status.sweep_grace; also run periodically).
    try:
        import billing_status
        billing_status.sweep_grace(customer_id=customer_id)
    except Exception:
        pass
    return _entitlement_rows(customer_id)


def _entitlement_rows(customer_id: str) -> list:
    return rows("SELECT * FROM customer_entitlements WHERE customer_id=? ORDER BY created_at", (customer_id,))


def product_mode_for_customer(customer_id: str) -> str:
    """The authoritative Local-vs-Hybrid product mode for this customer,
    derived from their real camera-slot entitlement -- never a separate
    admin-set flag, so an "Upgrade Local -> Hybrid" action is genuinely
    entitlement/billing-driven: the moment a Hybrid checkout completes
    (create_camera_slot_checkout() in main.py -> the existing Stripe
    webhook -> upsert_entitlement(product="camera_slots_hybrid")), this
    function starts returning "hybrid" for that customer with no
    separate code path to keep in sync.

    "hybrid" wins if both an active camera_slots_local AND an active
    camera_slots_hybrid entitlement exist (upsert_entitlement() is
    idempotent per (customer_id, product), so a customer's original
    one-time Local purchase and a later Hybrid subscription are two
    independent rows that can coexist) -- this is exactly the moment
    right after an upgrade purchase, before any decision is made about
    the now-redundant one-time Local entitlement, and "the customer
    just paid for Hybrid" should take effect immediately regardless.

    Returns "" (never a guessed default) when neither entitlement is
    active -- a customer who cancelled Hybrid with no prior Local
    entitlement on file has no product mode this function can honestly
    report; see docs/product-mode-local-hybrid-2026-09-21.md for why
    this is a flagged business decision (auto-grant an equivalent Local
    entitlement on downgrade, or require a fresh purchase) rather than
    something silently assumed here. Callers (appliance_cloud.
    appliance_configuration()) must treat "" as "no mode to report",
    the same fail-closed discipline resolve_tier()/resolve_addon() use
    for an unconfigured Price ID."""
    active_products = {
        item["product"] for item in get_entitlements_for_customer(customer_id)
        if item["status"] == "active"
    }
    if "camera_slots_hybrid" in active_products:
        return "hybrid"
    if "camera_slots_local" in active_products:
        return "local"
    return ""


# The two base plans (2026-10-02, owner: Local -> Hybrid is an upgrade, one
# base plan at a time). They are alternatives, never additive: if both are
# ever active at once (an older separate-subscription upgrade), camera
# capacity is the larger of the two, not their sum.
BASE_PLAN_PRODUCTS = ("camera_slots_local", "camera_slots_hybrid")


def total_camera_slots(customer_id: str) -> int:
    """The one function anything (claim/refresh endpoints, the setup
    wizard, a future installer) must call to learn camera-slot capacity.
    Sums every *active* entitlement's camera_slot_quantity -- never a
    hard-coded constant, never a partner-typed `plans.camera_quantity`."""
    # The one-time VMS software license (product "vms_license", 2026-09-30)
    # is a licence of the software, not additional camera slots -- counting
    # it here would double a customer's capacity.
    active = [item for item in get_entitlements_for_customer(customer_id)
              if item["status"] == "active" and item["product"] != VMS_LICENSE_PRODUCT]
    base = max((int(item["camera_slot_quantity"] or 0) for item in active if item["product"] in BASE_PLAN_PRODUCTS), default=0)
    return base + sum(int(item["camera_slot_quantity"] or 0) for item in active if item["product"] not in BASE_PLAN_PRODUCTS)


def appliance_includes_vms_license(customer_id: str) -> bool:
    """An AnyAiCam appliance purchase includes the VMS software license, so
    such a customer is never charged for it separately."""
    return row(
        "SELECT id FROM hardware_orders WHERE customer_id=? AND sku LIKE 'AIC-APPLIANCE-%' "
        "AND status='paid' LIMIT 1",
        (customer_id,),
    ) is not None


def vms_license_capacity(customer_id: str) -> int:
    """Camera capacity of this customer's VMS software license: a purchased
    (DIY) license, or -- for an appliance customer -- the capacity of their
    current Local/Hybrid plan, which the appliance's included license
    follows. 0 means no license on record."""
    entitlements = [e for e in get_entitlements_for_customer(customer_id) if e["status"] == "active"]
    purchased = max((int(e["camera_slot_quantity"] or 0) for e in entitlements if e["product"] == VMS_LICENSE_PRODUCT), default=0)
    included = 0
    if appliance_includes_vms_license(customer_id):
        included = max((int(e["camera_slot_quantity"] or 0) for e in entitlements
                        if e["product"] in ("camera_slots_local", "camera_slots_hybrid")), default=0)
    return max(purchased, included)



def usable_camera_capacity(customer_id: str) -> int:
    """How many cameras this customer's VMS may actually provision and run
    (2026-10-01): the camera plan (subscription slots) AND the VMS software
    license must both cover a camera, so capacity is the smaller of the two.
    An appliance purchase includes a license matching the plan, so for an
    appliance customer this equals the plan; a customer with a copied
    installer and no license gets 0. Claiming an appliance is deliberately
    NOT gated by this -- only provisioning and operating cameras are."""
    return max(0, min(total_camera_slots(customer_id), vms_license_capacity(customer_id)))


def capacity_breakdown(customer_id: str) -> dict:
    plan = total_camera_slots(customer_id)
    license_capacity = vms_license_capacity(customer_id)
    return {"camera_slot_quantity": max(0, min(plan, license_capacity)), "plan_camera_slots": plan,
            "vms_license_capacity": license_capacity, "vms_license_required": license_capacity <= 0}


def capacity_refusal(customer_id: str) -> str:
    """The customer-facing reason a new camera cannot be added."""
    breakdown = capacity_breakdown(customer_id)
    if breakdown["vms_license_required"]:
        return ("A VMS software license is required to add cameras. It is included with every AnyAiCam appliance, "
                "or can be purchased for your own PC from My subscription.")
    usable = breakdown["camera_slot_quantity"]
    return (f"Camera limit reached: this account is licensed for {usable} camera(s). "
            "Upgrade your plan or license, or remove a camera before adding another.")

# ----------------------------------------------------------- pending links


def create_pending_link(
    *,
    email: str,
    product: str,
    camera_slot_quantity: int,
    stripe_customer_id: Optional[str] = None,
    stripe_checkout_session_id: Optional[str] = None,
    stripe_price_id: Optional[str] = None,
    raw_event: dict,
) -> dict:
    """Checkout-first safety net: a Stripe purchase completed under an
    email with no matching authoritative customer yet (new customer who
    paid before finishing registration). Recorded here instead of either
    (a) silently creating a duplicate/guessed customer record, or (b)
    dropping the purchase -- see resolve_pending_links_for_customer()."""
    link_id = uuid.uuid4().hex
    with connection() as db:
        db.execute(
            "INSERT INTO pending_customer_links(id,normalized_email,stripe_customer_id,stripe_checkout_session_id,"
            "stripe_price_id,product,camera_slot_quantity,raw_event_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (link_id, normalize_email(email), stripe_customer_id, stripe_checkout_session_id, stripe_price_id,
             product, camera_slot_quantity, json.dumps(raw_event), "pending", _now()),
        )
    return row("SELECT * FROM pending_customer_links WHERE id=?", (link_id,))


def resolve_pending_links_for_customer(customer_id: str, email: str) -> list:
    """Call this once a customer account exists with a VERIFIED email --
    e.g. immediately after customer_registration.py confirms/approves the
    account, or on first authenticated login. Attaches every still-
    pending purchase made under that email before the account existed.

    Callers must never invoke this on unverified say-so (a bare
    query-string email, an unauthenticated form field): resolving a
    pending link is what turns a Stripe purchase into real camera-slot
    entitlement, so it must only ever run against an email the caller
    has already proven the customer owns (matching this module's
    'server-side verification of entitlements' requirement)."""
    normalized = normalize_email(email)
    pending = rows("SELECT * FROM pending_customer_links WHERE normalized_email=? AND status='pending'", (normalized,))
    resolved_ids = []
    for link in pending:
        upsert_entitlement(
            customer_id=customer_id,
            product=link["product"],
            camera_slot_quantity=link["camera_slot_quantity"],
            status="active",
            stripe_customer_id=link["stripe_customer_id"],
            stripe_checkout_session_id=link["stripe_checkout_session_id"],
            stripe_price_id=link.get("stripe_price_id"),
        )
        with connection() as db:
            db.execute(
                "UPDATE pending_customer_links SET status='resolved',resolved_at=?,resolved_customer_id=? WHERE id=?",
                (_now(), customer_id, link["id"]),
            )
        resolved_ids.append(link["id"])
    return resolved_ids


# --------------------------------------------------------- Stripe webhook bridge
#
# Wired into the live POST /api/payments/stripe/webhook route in main.py
# as of Phase 2 -- see module docstring. Each function here remains
# independently callable/testable against a synthetic Stripe event dict.

# Local and Hybrid are deliberately separate service lines even where
# their camera_slot_maximum coincides (both top out at 64) -- `product`
# encodes which one, so a customer's Local and Hybrid entitlements (if
# they somehow held both) never collide or overwrite each other, and an
# upgrade within one line (e.g. Local 1-8 -> Local 9-16) never touches
# the other. Each tier's Stripe Price ID goes in its own env var; unset
# means that tier is not purchasable/grantable yet, never a guess.
# create_camera_slot_checkout() in main.py branches Stripe Checkout
# `mode` off billing_type alone, never off a plan_type string.
#
# 2026-09-30: prices and billing now come from pricing_catalog.BASE_PLANS,
# the one authoritative catalog (approved working pricing). Local and
# Hybrid are both MONTHLY subscriptions again (Local $14.99/$24.99/$39.99/
# $69.99, Hybrid $24.99/$39.99/$69.99/$99.99), so every tier is
# billing_type "recurring" -> Stripe mode="subscription". The tuple shape
# below is unchanged for existing callers. The earlier live-mode Price IDs
# were created at the previous amounts, so new Stripe Price objects at
# these amounts must be configured in ANYAICAM_STRIPE_PRICE_LOCAL_*/
# HYBRID_* -- see PROJECT_CHECKPOINT.md.
from pricing_catalog import BASE_PLANS as _CATALOG_BASE_PLANS
from pricing_catalog import VMS_LICENSE_PRODUCT, VMS_LICENSES as _CATALOG_VMS_LICENSES

PLAN_TIERS = [
    # plan_type, tier_label, min_cameras, max_cameras, camera_slot_maximum, monthly_retail_usd, price_id_env_var, billing_type
    (plan_type, tier_label, min_cameras, max_cameras, max_cameras, round(cents / 100, 2), env_var, "recurring")
    for plan_type, tier_label, min_cameras, max_cameras, cents, env_var in _CATALOG_BASE_PLANS
]


def _plan_tier_rows() -> list[dict]:
    rows = []
    for plan_type, tier_label, min_cameras, max_cameras, camera_slot_maximum, monthly_retail_usd, env_var, billing_type in PLAN_TIERS:
        rows.append({
            "plan_type": plan_type,
            "tier_label": tier_label,
            "min_cameras": min_cameras,
            "max_cameras": max_cameras,
            "camera_slot_maximum": camera_slot_maximum,
            "monthly_retail_usd": monthly_retail_usd,
            "billing_type": billing_type,
            "product": f"camera_slots_{plan_type}",
            "stripe_price_id": os.environ.get(env_var, "").strip() or None,
        })
    return rows


def pricing_table_for_website() -> list[dict]:
    """Prepared, ready-to-render pricing data for the customer-facing VMS
    website pricing section -- plan type, camera range, billing_type
    (every tier is "recurring" -- monthly -- as of 2026-09-30; the website
    still renders off this field, never infers it from plan_type), and the
    catalog's monthly retail price for all 8 tiers. Does NOT include
    stripe_price_id (irrelevant to a pricing page)."""
    return [
        {"plan_type": r["plan_type"], "tier_label": r["tier_label"], "min_cameras": r["min_cameras"],
         "max_cameras": r["max_cameras"], "monthly_retail_usd": r["monthly_retail_usd"], "billing_type": r["billing_type"]}
        for r in _plan_tier_rows()
    ]


def _load_price_tier_map() -> dict:
    """Server-side, fixed Stripe-Price-ID -> camera-slot-tier mapping.
    The ONLY source of camera-slot quantity as of Phase 3 -- never a
    browser-submitted quantity, never a per-plan constant. Built from
    PLAN_TIERS above; a tier whose env var is unset contributes no entry
    at all, so resolve_tier() correctly treats its Price ID (there isn't
    one yet) as unverified -- fail closed, not a guess."""
    mapping = {}
    for tier in _plan_tier_rows():
        if tier["stripe_price_id"]:
            mapping[tier["stripe_price_id"]] = {
                "product": tier["product"],
                "camera_slot_maximum": tier["camera_slot_maximum"],
                "billing_type": tier["billing_type"],
            }
    return mapping


PRICE_ID_CAMERA_SLOT_MAP = _load_price_tier_map()


def _load_vms_license_map() -> dict:
    """Stripe Price ID -> one-time VMS license capacity; unset env vars
    contribute nothing (fail closed)."""
    mapping = {}
    for capacity, _cents, env_var in _CATALOG_VMS_LICENSES:
        price_id = os.environ.get(env_var, "").strip()
        if price_id:
            mapping[price_id] = {"product": VMS_LICENSE_PRODUCT, "camera_slot_maximum": capacity, "billing_type": "one_time"}
    return mapping


PRICE_ID_VMS_LICENSE_MAP = _load_vms_license_map()


def resolve_vms_license(price_id: str) -> Optional[dict]:
    entry = PRICE_ID_VMS_LICENSE_MAP.get(price_id or "")
    return dict(entry) if isinstance(entry, dict) else None

# A subscription Stripe has ENDED. 'past_due' and 'unpaid' are not here: a
# failed payment gets the 7-day grace period (owner policy 2026-10-02,
# billing_status.py), after which the plan is suspended, not cancelled.
SUBSCRIPTION_INACTIVE_STATUSES = {"canceled", "incomplete_expired"}


def resolve_tier(price_id: str) -> Optional[dict]:
    """Returns {"product": ..., "camera_slot_maximum": ..., "billing_type": ...}
    for a server-verified Stripe Price ID, or None if this Price ID has no
    configured tier -- callers must treat None as "grant nothing",
    never fall back to a guessed quantity."""
    if not price_id:
        return None
    tier = PRICE_ID_CAMERA_SLOT_MAP.get(price_id)
    if not isinstance(tier, dict):
        return None
    product = str(tier.get("product") or "").strip()
    try:
        camera_slot_maximum = int(tier.get("camera_slot_maximum"))
    except (TypeError, ValueError):
        return None
    if not product:
        return None
    return {
        "product": product,
        "camera_slot_maximum": camera_slot_maximum,
        "billing_type": str(tier.get("billing_type") or "recurring"),
    }


def is_event_processed(event_id: str) -> bool:
    return row("SELECT id FROM provisioning_webhook_events WHERE id=?", (event_id,)) is not None


def _record_processed(event_id: str, event_type: str, result: dict) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO provisioning_webhook_events(id,event_type,processed_at,result) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (event_id, event_type, _now(), json.dumps(result)),
        )


def sync_entitlement_from_stripe_event(event: dict) -> dict:
    """Idempotent per Stripe event id -- a retried webhook delivery
    (Stripe retries on anything but a 2xx response) must never
    double-grant camera slots. Returns a small status dict for logging/
    testing; never raises for a normal "nothing to do here" event, only
    for a caller error (see the two ValueErrors below)."""
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        raise ValueError("event is missing an id; refusing to process (would break idempotency).")
    if is_event_processed(event_id):
        return {"status": "already_processed", "event_id": event_id}

    if event_type in CHECKOUT_GRANT_EVENT_TYPES:
        result = _sync_checkout_completed(event)
    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        result = _sync_subscription_change(event, cancelled=event_type.endswith("deleted"))
    else:
        result = {"status": "ignored", "reason": f"unhandled event type {event_type!r}"}

    _record_processed(event_id, event_type, result)
    return result


def _extract_checkout_fields(session_obj: dict) -> dict:
    email = (session_obj.get("customer_details") or {}).get("email") or session_obj.get("customer_email") or ""
    metadata = session_obj.get("metadata") or {}
    price_id = str(metadata.get("anyaicam_stripe_price_id") or "").strip()
    return {
        "email": email,
        "price_id": price_id,
        "authoritative_customer_id": str(metadata.get("anyaicam_customer_id") or "") or None,
        "stripe_customer_id": str(session_obj.get("customer") or "") or None,
        "stripe_checkout_session_id": str(session_obj.get("id") or "") or None,
        # One-time purchases (VMS license): the payment a refund or dispute
        # names (billing_status.py).
        "stripe_payment_intent_id": str(session_obj.get("payment_intent") or "") or None,
    }


def _sync_checkout_completed(event: dict) -> dict:
    session_obj = (event.get("data") or {}).get("object") or {}
    if awaiting_payment(session_obj):
        return awaiting_payment_result(session_obj)  # granted on async_payment_succeeded
    fields = _extract_checkout_fields(session_obj)

    # Fixed-tier lookup is server-side and Price-ID-keyed only -- never a
    # browser-submitted quantity (see module docstring). A Price ID with
    # no configured tier is never granted any slots, regardless of what
    # any other metadata on this event claims (tamper resistance: a
    # forged/stale anyaicam_camera_slot_quantity value, if one is even
    # present on an old-shaped event, is never read here at all).
    tier = resolve_tier(fields["price_id"]) or resolve_vms_license(fields["price_id"])
    if not tier:
        return {"status": "ignored", "reason": "no verified tier mapping for this stripe price id", "price_id": fields["price_id"]}

    # Authoritative-identity-first: a checkout created from a real,
    # signed-in customer_owner session carries its own customers.id in
    # metadata (see create_stripe_checkout() in main.py) -- trust that
    # directly, never re-derive identity from the customer-supplied
    # email when it's available. Email is only the fallback/reconciliation
    # path for a checkout-before-registration purchase.
    customer = None
    if fields["authoritative_customer_id"]:
        customer = row("SELECT * FROM customers WHERE id=?", (fields["authoritative_customer_id"],))
    if not customer and fields["email"]:
        customer = find_customer_by_email(fields["email"])

    if customer and _bound_elsewhere(fields["stripe_customer_id"], customer["id"]):
        # The Stripe customer already pays for a DIFFERENT AnyAiCam account:
        # never provision this one from it (2026-10-02, cross-tenant).
        return {"status": "rejected", "reason": "stripe customer belongs to another account", "customer_id": customer["id"]}
    subscription_id = str(session_obj.get("subscription") or "") or None
    current = current_subscription(subscription_id) if session_obj.get("mode") == "subscription" else None
    if current is not None:
        current_customer = str(current.get("customer") or "")
        current_meta = str((current.get("metadata") or {}).get("anyaicam_customer_id") or "") or None
        if (fields["stripe_customer_id"] and current_customer != fields["stripe_customer_id"]) or \
                (customer and current_meta and current_meta != customer["id"]):
            return {"status": "rejected", "reason": "checkout and its subscription disagree about the account"}
        current_price = _current_subscription_price_id(current)
        tier = resolve_tier(current_price) or tier
        if current_price:
            fields["price_id"] = current_price
        if current.get("status") in SUBSCRIPTION_INACTIVE_STATUSES:
            # Already ended in Stripe (this checkout event arrived late):
            # grant nothing; an existing row reflects the ended state.
            if customer:
                existing = row("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (customer["id"], tier["product"]))
                if existing:
                    upsert_entitlement(customer_id=customer["id"], product=tier["product"], camera_slot_quantity=0, status="cancelled",
                                       stripe_customer_id=fields["stripe_customer_id"], stripe_subscription_id=subscription_id,
                                       stripe_price_id=fields["price_id"])
            return {"status": "not_granted", "reason": "subscription is no longer active in Stripe"}
    if not customer:
        if not fields["email"]:
            return {"status": "ignored", "reason": "no authoritative customer id and no email to reconcile against"}
        link = create_pending_link(
            email=fields["email"], product=tier["product"], camera_slot_quantity=tier["camera_slot_maximum"],
            stripe_customer_id=fields["stripe_customer_id"], stripe_checkout_session_id=fields["stripe_checkout_session_id"],
            stripe_price_id=fields["price_id"], raw_event=event,
        )
        return {"status": "pending_link_created", "pending_link_id": link["id"]}
    entitlement = upsert_entitlement(
        customer_id=customer["id"], product=tier["product"], camera_slot_quantity=tier["camera_slot_maximum"],
        status="active", stripe_customer_id=fields["stripe_customer_id"],
        stripe_subscription_id=subscription_id if current is not None else None,
        stripe_checkout_session_id=fields["stripe_checkout_session_id"], stripe_price_id=fields["price_id"],
    )
    if current is not None:
        record_billing_state(entitlement["id"], current)
        supersede_other_base_plans(entitlement, current)
    if fields.get("stripe_payment_intent_id") and session_obj.get("mode") == "payment":
        with connection() as db:
            db.execute("UPDATE customer_entitlements SET stripe_payment_intent_id=? WHERE id=?",
                       (fields["stripe_payment_intent_id"], entitlement["id"]))
    return {"status": "entitlement_updated", "entitlement_id": entitlement["id"], "customer_id": customer["id"]}


def _current_subscription_price_id(subscription_obj: dict) -> str:
    """Stripe's subscription object always carries its own current
    `items.data[].price.id` -- the true, authoritative price a customer
    is on right now, including after a self-service upgrade/downgrade
    through the Stripe Customer Portal (which does NOT automatically
    update arbitrary metadata fields set at creation time, so trusting
    only our own anyaicam_stripe_price_id metadata here would silently
    miss a portal-driven tier change). Falls back to that metadata only
    for a stripped-down payload with no items data (e.g. a minimal test
    event)."""
    items = ((subscription_obj.get("items") or {}).get("data") or [])
    if items and isinstance(items[0], dict):
        price_id = str((items[0].get("price") or {}).get("id") or "").strip()
        if price_id:
            return price_id
    return str((subscription_obj.get("metadata") or {}).get("anyaicam_stripe_price_id") or "").strip()


def _sync_subscription_change(event: dict, *, cancelled: bool) -> dict:
    subscription_obj = (event.get("data") or {}).get("object") or {}
    current = current_subscription(str(subscription_obj.get("id") or "") or None)
    if current is not None:
        # What Stripe says now wins over this event's snapshot; a deletion
        # is only "cancelled" if the subscription really has ended.
        subscription_obj = current
        cancelled = current.get("status") in SUBSCRIPTION_INACTIVE_STATUSES
    stripe_customer_id = str(subscription_obj.get("customer") or "")
    metadata = subscription_obj.get("metadata") or {}
    price_id = _current_subscription_price_id(subscription_obj)
    metadata_customer_id = str(metadata.get("anyaicam_customer_id") or "") or None
    if not stripe_customer_id or not price_id:
        return {"status": "ignored", "reason": "missing stripe customer id or anyaicam_stripe_price_id metadata"}
    tier = resolve_tier(price_id)
    if not tier:
        return {"status": "ignored", "reason": "no verified tier mapping for this stripe price id", "price_id": price_id}
    existing = row(
        "SELECT * FROM customer_entitlements WHERE stripe_customer_id=? AND product=?",
        (stripe_customer_id, tier["product"]),
    )
    if existing and metadata_customer_id and existing["customer_id"] != metadata_customer_id:
        # The Stripe customer maps to one account and the subscription
        # names another: apply it to neither (2026-10-02).
        return {"status": "rejected", "reason": "stripe customer and subscription metadata name different accounts"}
    if not existing and metadata_customer_id and _bound_elsewhere(stripe_customer_id, metadata_customer_id):
        return {"status": "rejected", "reason": "stripe customer belongs to another account"}
    if not existing and metadata_customer_id:
        # Self-healing path: Stripe does not guarantee webhook delivery
        # order, so a subscription.updated event can in principle arrive
        # before checkout.session.completed created the entitlement row.
        # The subscription's own metadata still carries the authoritative
        # customer_id (see create_stripe_checkout()'s subscription_data
        # metadata), so look the entitlement up that way instead of
        # giving up.
        existing = row("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (metadata_customer_id, tier["product"]))
    new_status = "cancelled" if (cancelled or subscription_obj.get("status") in SUBSCRIPTION_INACTIVE_STATUSES) else "active"
    if existing and existing.get("status") == "suspended" and new_status == "active":
        # Suspended for an unpaid period, a refund or a dispute: only
        # billing_status lifts that (a payment, a won dispute), never a
        # subscription update that merely says the subscription exists.
        new_status = "suspended"
    if not existing:
        known = metadata_customer_id and row("SELECT id FROM customers WHERE id=?", (metadata_customer_id,))
        if not known:
            return unresolved("camera_plan", str(subscription_obj.get("id") or "") or None, stripe_customer_id)
        if current is None and new_status == "active":
            # Without Stripe's current state, only a checkout grants.
            return {"status": "ignored", "reason": "no existing entitlement for this stripe customer/product"}
    entitlement = upsert_entitlement(
        customer_id=(existing or {}).get("customer_id") or metadata_customer_id,
        product=tier["product"],
        # An upgrade (a different Price ID's subscription.updated for the
        # SAME product) would arrive with a different tier -- always take
        # the current event's own verified maximum, never carry forward
        # the prior row's value, except on cancellation (0).
        camera_slot_quantity=0 if new_status == "cancelled" else tier["camera_slot_maximum"],  # suspended keeps its size
        status=new_status,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=str(subscription_obj.get("id") or "") or None,
        stripe_price_id=price_id,
    )
    if current is not None:
        record_billing_state(entitlement["id"], current)
        supersede_other_base_plans(entitlement, current)
        import plan_changes
        plan_changes.sync_scheduled_change(entitlement, current)
        plan_changes.sync_addons_with_base(entitlement, current)
    return {"status": "entitlement_updated", "entitlement_id": entitlement["id"]}


def _period_end(subscription: dict) -> int | None:
    """current_period_end lives on the subscription in older Stripe API
    versions and on each item in newer ones."""
    value = subscription.get("current_period_end")
    if not value:
        items = ((subscription.get("items") or {}).get("data") or [])
        value = items[0].get("current_period_end") if items and isinstance(items[0], dict) else None
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def record_billing_state(entitlement_id: str, subscription: dict) -> None:
    """Facts from Stripe's current subscription, shown on My subscription:
    its status, when the period ends, and whether it is set to end then.
    Display only -- which statuses keep access is unchanged."""
    with connection() as db:
        db.execute("UPDATE customer_entitlements SET stripe_status=?,current_period_end=?,cancel_at_period_end=? WHERE id=?",
                   (str(subscription.get("status") or "") or None, _period_end(subscription),
                    1 if subscription.get("cancel_at_period_end") else 0, entitlement_id))


def supersede_other_base_plans(entitlement: dict, subscription: dict) -> list[str]:
    """Local -> Hybrid is one subscription changing its price (owner,
    2026-10-02): once Stripe says a subscription is on one base plan, the
    other base plan held through that SAME subscription is no longer active
    -- one base plan, never both, never double capacity. A separate
    subscription is never touched here (that would hide double billing)."""
    if entitlement.get("product") not in BASE_PLAN_PRODUCTS or entitlement.get("status") != "active":
        return []
    subscription_id = str(subscription.get("id") or "")
    if not subscription_id:
        return []
    superseded = []
    for other in rows("SELECT * FROM customer_entitlements WHERE customer_id=? AND product<>? AND status='active' "
                      "AND stripe_subscription_id=?", (entitlement["customer_id"], entitlement["product"], subscription_id)):
        if other["product"] not in BASE_PLAN_PRODUCTS:
            continue
        with connection() as db:
            db.execute("UPDATE customer_entitlements SET status='superseded',camera_slot_quantity=0,scheduled_change=NULL,"
                       "scheduled_change_at=NULL,stripe_schedule_id=NULL,updated_at=? WHERE id=? AND status IN ('active','suspended')",
                       (_now(), other["id"]))
        superseded.append(other["id"])
    return superseded
