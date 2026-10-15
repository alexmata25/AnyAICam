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
from stripe_state import conflicting_subscription, payment_reversal, subscription_payment_reversal

# Stripe's status for a subscription whose FIRST payment has not completed:
# never active service (Codex audit of 3f5b9c4, finding 5). The 7-day grace
# is for a renewal of service already paid for, not an unpaid purchase.
INITIAL_PAYMENT_INCOMPLETE = "incomplete"
PAYMENT_INCOMPLETE_REASON = "payment_incomplete"


def _set_suspended_reason(entitlement_id: str, reason) -> None:
    with connection() as db:
        db.execute("UPDATE customer_entitlements SET suspended_reason=? WHERE id=?", (reason, entitlement_id))


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
    if not existing:
        # Two simultaneous requests (e.g. duplicate upgrade clicks, or one
        # with its webhook) may both find no row: one inserts, the other
        # updates that row instead of failing on UNIQUE(customer_id, product).
        entitlement_id = uuid.uuid4().hex
        with connection() as db:
            inserted = db.execute(
                "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,"
                "stripe_customer_id,stripe_subscription_id,stripe_checkout_session_id,stripe_price_id,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(customer_id,product) DO NOTHING",
                (entitlement_id, customer_id, product, camera_slot_quantity, status,
                 stripe_customer_id, stripe_subscription_id, stripe_checkout_session_id, stripe_price_id, now, now, expires_at),
            ).rowcount
        if inserted:
            return row("SELECT * FROM customer_entitlements WHERE id=?", (entitlement_id,))
        existing = row("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (customer_id, product))
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
    # Billing v2 is authoritative when a v2 subscription exists; legacy
    # camera_slots_local/hybrid rows remain untouched for grandfathered
    # accounts and are consulted only when no active v2 plan is present.
    try:
        import per_camera_billing
        v2_mode = per_camera_billing.product_mode(customer_id)
        if v2_mode:
            return v2_mode
    except Exception:
        pass  # databases before the additive v2 migration retain legacy behavior
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
MAX_CAMERA_SLOTS = 10_000  # shared with the Linux accepted-capacity verifier


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
    try:
        import per_camera_billing
        v2 = per_camera_billing.entitlement_for_customer(customer_id)
    except Exception:
        v2 = None
    if v2 and v2.get("status") == "active":
        base = int(v2.get("camera_quantity") or 0)
    else:
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
    try:
        import per_camera_billing
        v2_capacity = per_camera_billing.vms_license_capacity(customer_id)
        if v2_capacity:
            return v2_capacity
    except Exception:
        pass  # pre-v2 database: preserve the fixed-capacity path
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
    return max(0, min(MAX_CAMERA_SLOTS, total_camera_slots(customer_id), vms_license_capacity(customer_id)))


def capacity_breakdown(customer_id: str) -> dict:
    plan = min(MAX_CAMERA_SLOTS, max(0, total_camera_slots(customer_id)))
    license_capacity = min(MAX_CAMERA_SLOTS, max(0, vms_license_capacity(customer_id)))
    return {"camera_slot_quantity": max(0, min(plan, license_capacity)), "plan_camera_slots": plan,
            "vms_license_capacity": license_capacity, "vms_license_required": license_capacity <= 0}


def _count(quantity: int) -> str:
    return f"{quantity} camera" + ("" if quantity == 1 else "s")


def vms_license_summary(customer_id: str) -> dict:
    """The account's VMS software license for My subscription (2026-10-15).

    cameras is capacity_breakdown()["vms_license_capacity"] -- the very value
    feature_entitlements.appliance_snapshot() signs for the account's
    appliances -- so the page and the appliance cannot disagree. kind names
    the source vms_license_capacity() took it from:

    * per_camera_plan      -- an active billing-v2 per-camera plan;
    * one_time_vms_license -- a purchased (DIY) one-time license row;
    * appliance_included   -- a paid AnyAiCam appliance order: the license
                              follows the Local/Hybrid plan quantity;
    * legacy_slots         -- an earlier Local/Hybrid plan with no license
                              on record (cameras 0);
    * none                 -- nothing.

    The plan view (mode separation, per_camera_billing.existing_plan /
    has_camera_plan, cached Stripe modes only -- never a Stripe call) decides
    suspended states and the legacy plan name. "stand-alone" appears in a
    label only when a one-time vms_license row exists. Read-only."""
    breakdown = capacity_breakdown(customer_id)
    cameras = int(breakdown["vms_license_capacity"])
    try:
        import per_camera_billing
        v2_capacity = min(MAX_CAMERA_SLOTS, max(0, int(per_camera_billing.vms_license_capacity(customer_id) or 0)))
        plan = per_camera_billing.existing_plan(customer_id, lookup=False)
        has_plan = per_camera_billing.has_camera_plan(customer_id, lookup=False)
    except Exception:  # pre-v2 database
        v2_capacity, plan, has_plan = 0, None, False
    entitlements = _entitlement_rows(customer_id)
    licenses = [e for e in entitlements if e["product"] == VMS_LICENSE_PRODUCT]
    purchased = max((int(e["camera_slot_quantity"] or 0) for e in licenses if e["status"] == "active"), default=0)
    purchased = min(MAX_CAMERA_SLOTS, purchased)
    appliance_order = appliance_includes_vms_license(customer_id)
    base_rows = [e for e in entitlements if e["product"] in BASE_PLAN_PRODUCTS]
    kind, status = "none", "none"
    if cameras > 0:
        status = "active"
        if v2_capacity and v2_capacity == cameras:
            kind = "per_camera_plan"
        elif purchased and purchased == cameras:
            kind = "one_time_vms_license"
        else:
            kind = "appliance_included"
    elif plan and plan["kind"] == "per_camera" and plan["entitlement"].get("status") == "suspended":
        kind, status = "per_camera_plan", "suspended"
    elif any(e["status"] == "suspended" for e in licenses):
        kind, status = "one_time_vms_license", "suspended"
    elif appliance_order and any(e["status"] == "suspended" for e in base_rows):
        kind, status = "appliance_included", "suspended"
    elif plan and plan["kind"] == "legacy":
        kind = "legacy_slots"
    # The licensed camera count is returned separately ("cameras") and shown on its
    # own row by the Software License panel, so active labels don't repeat it.
    labels = {
        ("per_camera_plan", "active"): "VMS software license included with your per-camera plan",
        ("one_time_vms_license", "active"): "One-time stand-alone VMS software license (yours to keep)",
        ("appliance_included", "active"): "VMS software license included with your AnyAiCam appliance",
        ("per_camera_plan", "suspended"): "VMS software license for your per-camera plan · suspended until billing is resolved",
        ("one_time_vms_license", "suspended"): "One-time stand-alone VMS software license · suspended (payment refunded or disputed)",
        ("appliance_included", "suspended"): "VMS software license included with your AnyAiCam appliance · suspended until billing is resolved",
    }
    label = labels.get((kind, status))
    if kind == "legacy_slots":
        label = f"Earlier {plan.get('name') or 'camera'} plan ({_count(int(plan.get('quantity') or 0))}) · no VMS software license on record"
    elif label is None:
        label = "No VMS software license on record. It is included with every AnyAiCam appliance, or can be purchased for your own PC."
    if kind == "per_camera_plan" and status == "active" and purchased:
        label += f" · one-time stand-alone license for up to {_count(purchased)} also on record"
    return {
        "kind": kind, "cameras": cameras, "status": status, "label": label,
        "plan_cameras": int(breakdown["plan_camera_slots"]), "usable_cameras": int(breakdown["camera_slot_quantity"]),
        "has_camera_plan": bool(has_plan), "appliance_order": bool(appliance_order),
        "sources": [{"product": e["product"], "qty": int(e["camera_slot_quantity"] or 0), "status": e["status"], "row_id": e["id"]}
                    for e in entitlements if e["product"] in BASE_PLAN_PRODUCTS + (VMS_LICENSE_PRODUCT,)],
    }


def capacity_refusal(customer_id: str) -> str:
    """The customer-facing reason a new camera cannot be added.

    Per-camera plans (2026-10-08): with no camera plan the customer is told
    to choose one; when the per-camera plan's quantity is what is full, to
    increase that quantity (or remove a camera). Never a price -- the plan
    page shows those."""
    breakdown = capacity_breakdown(customer_id)
    if breakdown["plan_camera_slots"] <= 0:
        return ("A camera plan is required to add cameras. Choose Basic Local, AI Local or Hybrid and how many "
                "cameras on My subscription.")
    if breakdown["vms_license_required"]:
        return ("A VMS software license is required to add cameras. It is included with every AnyAiCam appliance, "
                "or can be purchased for your own PC from My subscription.")
    usable = breakdown["camera_slot_quantity"]
    if usable >= breakdown["plan_camera_slots"] and _per_camera_plan_active(customer_id):
        return (f"Camera limit reached: this account is licensed for {usable} camera(s). To add another camera, "
                "increase the number of cameras on your plan in My subscription, or remove a camera first.")
    return (f"Camera limit reached: this account is licensed for {usable} camera(s). "
            "Upgrade your plan or license, or remove a camera before adding another.")


def _per_camera_plan_active(customer_id: str) -> bool:
    try:
        import per_camera_billing
        plan = per_camera_billing.entitlement_for_customer(customer_id)
    except Exception:
        return False
    return bool(plan and plan.get("status") == "active")


def capacity_refusal_code(customer_id: str) -> str:
    """Machine-readable twin of capacity_refusal() for the Cameras tab."""
    breakdown = capacity_breakdown(customer_id)
    if breakdown["plan_camera_slots"] <= 0:
        return "camera_plan_required"
    if breakdown["vms_license_required"]:
        return "vms_license_required"
    return "camera_limit_reached"


# An add request holds its place while it is queued or being verified by the
# appliance, for as long as it can still complete: partner_workspace times a
# request out after this many seconds without the appliance confirming it.
CAMERA_ADD_PENDING_SECONDS = 300
CAMERA_ADD_PENDING_STATES = ("queued", "verifying")
SUBSCRIPTION_PAGE_URL = "/subscription-portal"


def pending_camera_additions(customer_id: str) -> int:
    from datetime import timedelta
    from partner_db import row as _row
    cutoff = (datetime.now() - timedelta(seconds=CAMERA_ADD_PENDING_SECONDS)).isoformat()
    return int(_row(
        "SELECT COUNT(*) AS n FROM camera_provisioning_requests WHERE customer_id=? AND status IN (?,?) AND updated_at>=?",
        (customer_id, *CAMERA_ADD_PENDING_STATES, cutoff))["n"])


def cameras_in_service(customer_id: str) -> int:
    from partner_db import row as _row
    return int(_row("SELECT COUNT(*) AS n FROM cameras WHERE customer_id=? AND device_key IS NOT NULL", (customer_id,))["n"])


def camera_capacity_status(customer_id: str, *, can_manage: bool = False) -> dict:
    """Account-wide camera capacity for the Cameras tab (2026-10-08): how many
    cameras the account may run, how many are in service or being added (the
    same count both provisioning gates enforce), whether this caller may add
    or remove cameras, and, when full, why (text, code and where to go).
    Removing a camera frees its place; it never changes billing."""
    usable = usable_camera_capacity(customer_id)
    in_use = cameras_in_service(customer_id)
    pending = pending_camera_additions(customer_id)
    can_add = in_use + pending < usable
    return {"usable": usable, "in_use": in_use, "pending": pending, "available": max(0, usable - in_use - pending),
            "can_add_camera": can_add, "can_manage": bool(can_manage),
            "message": None if can_add else capacity_refusal(customer_id),
            "code": None if can_add else capacity_refusal_code(customer_id),
            "action_url": SUBSCRIPTION_PAGE_URL}

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

# Fixed camera-slot subscription tiers (Local/Hybrid 1-8, 9-16, 17-32, 33-64)
# were removed on 2026-10-08 (owner decision): camera plans are per camera
# (per_camera_billing, billing v2) and no customer ever paid for a fixed tier.
# No Stripe Price ID maps to a fixed tier any more, so resolve_tier() grants
# nothing for one (fail closed). The camera_slots_* entitlement products and
# the webhook handling below stay: camera_slots_rdm (operator-set quota) and
# total_camera_slots() still use them.
from pricing_catalog import VMS_LICENSE_PRODUCT, VMS_LICENSES as _CATALOG_VMS_LICENSES

PRICE_ID_CAMERA_SLOT_MAP: dict = {}


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
    if session_obj.get("mode") == "subscription" and current is None:
        return {"status": "ignored", "reason": "subscription checkout without a subscription"}
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
                # Only a row bound to THIS subscription reflects its end: a
                # newer subscription's row is never touched (finding 3).
                if existing and str(existing.get("stripe_subscription_id") or subscription_id) == subscription_id:
                    upsert_entitlement(customer_id=customer["id"], product=tier["product"], camera_slot_quantity=0, status="cancelled",
                                       stripe_customer_id=fields["stripe_customer_id"], stripe_subscription_id=subscription_id,
                                       stripe_price_id=fields["price_id"])
            return {"status": "not_granted", "reason": "subscription is no longer active in Stripe"}
        if current.get("status") == INITIAL_PAYMENT_INCOMPLETE:
            return {"status": "not_granted", "reason": "the first payment has not completed"}
    existing = None
    if customer:
        existing = row("SELECT * FROM customer_entitlements WHERE customer_id=? AND product=?", (customer["id"], tier["product"]))
        if existing and current is not None:
            conflict = conflicting_subscription(existing.get("stripe_subscription_id"), current)
            if conflict:
                return {"status": "ignored", "reason": conflict}
    # Never grant service for a payment already fully refunded or disputed,
    # whichever event arrived first (finding 4).
    if current is not None:
        reversal = subscription_payment_reversal(current)
    else:
        if not fields["stripe_payment_intent_id"] and int(session_obj.get("amount_total") or 0) > 0:
            raise RetryableStripeEventError("the checkout's payment could not be identified")
        reversal = payment_reversal(payment_intent=fields["stripe_payment_intent_id"])
    if not customer:
        if not fields["email"]:
            return {"status": "ignored", "reason": "no authoritative customer id and no email to reconcile against"}
        if reversal:
            return {"status": "not_granted", "reason": f"the payment was {reversal}"}
        link = create_pending_link(
            email=fields["email"], product=tier["product"], camera_slot_quantity=tier["camera_slot_maximum"],
            stripe_customer_id=fields["stripe_customer_id"], stripe_checkout_session_id=fields["stripe_checkout_session_id"],
            stripe_price_id=fields["price_id"], raw_event=event,
        )
        return {"status": "pending_link_created", "pending_link_id": link["id"]}
    # Only billing_status lifts a suspension of this same subscription (a
    # payment, a won dispute) -- never a late or repeated checkout event.
    keep_suspended = bool(existing and existing.get("status") == "suspended" and subscription_id
                          and existing.get("stripe_subscription_id") == subscription_id
                          and existing.get("suspended_reason") != PAYMENT_INCOMPLETE_REASON)
    status = "suspended" if (reversal or keep_suspended) else "active"
    entitlement = upsert_entitlement(
        customer_id=customer["id"], product=tier["product"], camera_slot_quantity=tier["camera_slot_maximum"],
        status=status, stripe_customer_id=fields["stripe_customer_id"],
        stripe_subscription_id=subscription_id if current is not None else None,
        stripe_checkout_session_id=fields["stripe_checkout_session_id"], stripe_price_id=fields["price_id"],
    )
    if reversal:
        _set_suspended_reason(entitlement["id"], reversal)
    elif status == "active":
        _set_suspended_reason(entitlement["id"], None)
    if current is not None:
        record_billing_state(entitlement["id"], current)
        supersede_other_base_plans(entitlement, current)
    if fields.get("stripe_payment_intent_id") and session_obj.get("mode") == "payment":
        with connection() as db:
            db.execute("UPDATE customer_entitlements SET stripe_payment_intent_id=? WHERE id=?",
                       (fields["stripe_payment_intent_id"], entitlement["id"]))
    if fields["stripe_customer_id"]:
        import checkout_guard
        checkout_guard.bind_stripe_customer(customer["id"], fields["stripe_customer_id"])
    return {"status": "entitlement_updated", "entitlement_id": entitlement["id"], "customer_id": customer["id"],
            **({"suspended_reason": reversal} if reversal else {})}


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
    if current is None:
        return {"status": "ignored", "reason": "subscription event without a subscription id"}
    # What Stripe says now wins over this event's snapshot (never applied
    # without it, finding 9); a deletion is only "cancelled" if the
    # subscription really has ended.
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
    if existing:
        # A delayed event for an older subscription never alters the
        # account's newer one (finding 3).
        conflict = conflicting_subscription(existing.get("stripe_subscription_id"), subscription_obj)
        if conflict:
            return {"status": "ignored", "reason": conflict}
    new_status = "cancelled" if (cancelled or subscription_obj.get("status") in SUBSCRIPTION_INACTIVE_STATUSES) else "active"
    new_reason = None
    if new_status == "active" and subscription_obj.get("status") == INITIAL_PAYMENT_INCOMPLETE:
        # First payment not completed: no service (finding 5).
        if not existing or existing.get("status") != "active":
            return {"status": "not_granted", "reason": "the first payment has not completed"}
        new_status, new_reason = "suspended", PAYMENT_INCOMPLETE_REASON
    if existing and existing.get("status") == "suspended" and new_status == "active" \
            and existing.get("suspended_reason") != PAYMENT_INCOMPLETE_REASON:
        # Suspended for an unpaid period, a refund or a dispute: only
        # billing_status lifts that (a payment, a won dispute), never a
        # subscription update that merely says the subscription exists.
        new_status = "suspended"
    if new_status == "active" and (not existing or existing.get("status") != "active"):
        # Becoming active here (first grant, or after the first payment
        # completed): never for a refunded/disputed payment (finding 4).
        reversal = subscription_payment_reversal(subscription_obj)
        if reversal:
            new_status, new_reason = "suspended", reversal
    if not existing:
        known = metadata_customer_id and row("SELECT id FROM customers WHERE id=?", (metadata_customer_id,))
        if not known:
            return unresolved("camera_plan", str(subscription_obj.get("id") or "") or None, stripe_customer_id)
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
    if new_reason:
        _set_suspended_reason(entitlement["id"], new_reason)
    elif new_status == "active":
        _set_suspended_reason(entitlement["id"], None)
    record_billing_state(entitlement["id"], current)
    supersede_other_base_plans(entitlement, current)
    import plan_changes
    plan_changes.sync_scheduled_change(entitlement, current)
    plan_changes.sync_addons_with_base(entitlement, current)
    # Stripe shows the price an upgrade asked for: that attempt is done,
    # however its own answer was lost (webhook recovery).
    plan_changes.reconcile_upgrade_attempts(current)
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
