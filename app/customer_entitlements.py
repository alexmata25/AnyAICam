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
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Optional

from partner_db import connection, row, rows


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
    return rows("SELECT * FROM customer_entitlements WHERE customer_id=? ORDER BY created_at", (customer_id,))


def total_camera_slots(customer_id: str) -> int:
    """The one function anything (claim/refresh endpoints, the setup
    wizard, a future installer) must call to learn camera-slot capacity.
    Sums every *active* entitlement's camera_slot_quantity -- never a
    hard-coded constant, never a partner-typed `plans.camera_quantity`."""
    return sum(
        (item["camera_slot_quantity"] or 0)
        for item in get_entitlements_for_customer(customer_id)
        if item["status"] == "active"
    )


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

# Server-side, fixed Stripe-Price-ID -> camera-slot-tier mapping. The
# ONLY source of camera-slot quantity as of Phase 3 -- never a browser-
# submitted quantity, never a per-plan constant. Format (via
# ANYAICAM_STRIPE_PRICE_TIER_MAP, a JSON object):
#   {"price_1AbC...": {"product": "camera_slots_1_16", "camera_slot_maximum": 16},
#    "price_1DeF...": {"product": "camera_slots_17_32", "camera_slot_maximum": 32}}
# Ships EMPTY by default -- see module docstring for the Phase 3 audit
# finding that no tier's Price ID is proven anywhere in existing
# configuration. A Price ID not present here is never granted slots.
def _load_price_tier_map() -> dict:
    raw = os.environ.get("ANYAICAM_STRIPE_PRICE_TIER_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


PRICE_ID_CAMERA_SLOT_MAP = _load_price_tier_map()

SUBSCRIPTION_INACTIVE_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


def resolve_tier(price_id: str) -> Optional[dict]:
    """Returns {"product": ..., "camera_slot_maximum": ...} for a
    server-verified Stripe Price ID, or None if this Price ID has no
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
    return {"product": product, "camera_slot_maximum": camera_slot_maximum}


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

    if event_type == "checkout.session.completed":
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
    }


def _sync_checkout_completed(event: dict) -> dict:
    session_obj = (event.get("data") or {}).get("object") or {}
    fields = _extract_checkout_fields(session_obj)

    # Fixed-tier lookup is server-side and Price-ID-keyed only -- never a
    # browser-submitted quantity (see module docstring). A Price ID with
    # no configured tier is never granted any slots, regardless of what
    # any other metadata on this event claims (tamper resistance: a
    # forged/stale anyaicam_camera_slot_quantity value, if one is even
    # present on an old-shaped event, is never read here at all).
    tier = resolve_tier(fields["price_id"])
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
        stripe_checkout_session_id=fields["stripe_checkout_session_id"], stripe_price_id=fields["price_id"],
    )
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
    if not existing and metadata_customer_id:
        # Self-healing path: Stripe does not guarantee webhook delivery
        # order, so a subscription.updated event can in principle arrive
        # before checkout.session.completed created the entitlement row.
        # The subscription's own metadata still carries the authoritative
        # customer_id (see create_stripe_checkout()'s subscription_data
        # metadata), so look the entitlement up that way instead of
        # giving up.
        existing = row("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (metadata_customer_id, tier["product"]))
    if not existing:
        return {"status": "ignored", "reason": "no existing entitlement for this stripe customer/product"}
    new_status = "cancelled" if (cancelled or subscription_obj.get("status") in SUBSCRIPTION_INACTIVE_STATUSES) else "active"
    entitlement = upsert_entitlement(
        customer_id=existing["customer_id"],
        product=tier["product"],
        # An upgrade (a different Price ID's subscription.updated for the
        # SAME product) would arrive with a different tier -- always take
        # the current event's own verified maximum, never carry forward
        # the prior row's value, except on cancellation (0).
        camera_slot_quantity=0 if new_status == "cancelled" else tier["camera_slot_maximum"],
        status=new_status,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=str(subscription_obj.get("id") or "") or None,
        stripe_price_id=price_id,
    )
    return {"status": "entitlement_updated", "entitlement_id": entitlement["id"]}
