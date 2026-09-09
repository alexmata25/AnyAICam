"""Provisioning Phase 5: one-time HARDWARE purchase records (Ryzen
appliances, the Numato relay module) -- deliberately separate from
customer_entitlements.py's recurring Local/Hybrid camera-slot
subscriptions.

Why this is its own module, not an extension of customer_entitlements.py
-----------------------------------------------------------------------
customer_entitlements.py answers "how many camera slots does this
customer currently have a right to use" -- a single, current-state number
per (customer_id, product) that a later purchase/cancellation updates in
place (see its own module docstring). A hardware purchase is a
fundamentally different shape of fact: a one-time payment for a specific
physical item, at a specific price, at a specific moment -- there is no
"current maximum" to converge on, and a customer who buys three Ryzen
Enterprise appliances over a year should end up with three order rows,
never one row whose quantity silently overwrites the last. Reusing
customer_entitlements' upsert-in-place model for hardware would either
lose that purchase history or require bolting an order concept onto a
table designed to not have one. HARDWARE_CATALOG below is intentionally
the same *shape* of fail-closed, server-side, Price-ID-keyed mapping as
customer_entitlements.PLAN_TIERS/PRICE_ID_CAMERA_SLOT_MAP -- same
discipline, separate table, separate map, separate resolver.

The fail-closed separation contract
------------------------------------
resolve_hardware_sku() only ever recognizes a Price ID present in
HARDWARE_PRICE_MAP (built from HARDWARE_CATALOG below); a camera-slot
Price ID is never in that map, so it always resolves to None here.
Symmetrically, customer_entitlements.resolve_tier() only recognizes a
Price ID present in PRICE_ID_CAMERA_SLOT_MAP; a hardware Price ID is
never in that map, so it always resolves to None there. Both modules are
wired into the same POST /api/payments/stripe/webhook route in main.py,
each in its own try/except (matching the Phase 2 pattern that already
protects customer_entitlements' hook from the legacy handler and vice
versa) -- a hardware Price ID can therefore never grant a camera slot,
and a camera-slot Price ID can never create a hardware order, by
construction: each side's own map is the only place a grant can come
from, and the two maps are disjoint by design. test_hardware_orders.py
proves this both ways.

The AAC-vs-Enterprise naming guard
------------------------------------
HARDWARE_CATALOG's product keys are 'ryzen_starter', 'ryzen_enterprise',
'ryzen_aac_facial_recognition', and 'numato_3_channel_relay' -- note
'ryzen_enterprise' is spelled distinctly from this same codebase's
pre-existing legacy 'enterprise' LICENSE_PLAN_FEATURES/STRIPE_PRICE_
ENTERPRISE software-plan key (main.py). They are unrelated products (a
physical appliance vs. a software feature tier) that happen to share the
word "enterprise" -- see test_hardware_orders.py's collision guard test,
which asserts HARDWARE_CATALOG never uses the bare string 'enterprise' as
a product key.

Verified prices (staging source of truth as of this pass)
------------------------------------------------------------
Read directly from website-pricing-review/build-your-system.html (the
approved staging Build Your System page) for the three Ryzen tiers, and
from this task's explicit instruction for the relay module:
    ryzen_starter                  $1,249.99 (124999 cents)
    ryzen_enterprise                $1,749.99 (174999 cents)
    ryzen_aac_facial_recognition   $2,249.99 (224999 cents)
    numato_3_channel_relay           $149.99 ( 14999 cents)

Real Stripe TEST/SANDBOX Price IDs for all four SKUs were confirmed by
the site owner and are documented in deploy/.env.staging.example under
ANYAICAM_STRIPE_PRICE_RYZEN_STARTER/RYZEN_ENTERPRISE/RYZEN_AAC_FACIAL/
RELAY_NUMATO_3CH -- this module still resolves them purely from the
environment (never hardcoded here), so a staging deployment must set
those four env vars (and ANYAICAM_STRIPE_SECRET_KEY to a sk_test_...
key) for HARDWARE_PRICE_MAP to actually populate. Until then, or in any
environment without them set, every *_PRICE_ID resolves to None and
HARDWARE_PRICE_MAP is empty -- fail closed, not a guess.

Product-label correction: the AAC Facial Recognition appliance's correct
model name is "MINISFORUM AI X1 Pro-470" -- never "AI X1-255" (that name
belongs to the Ryzen Starter unit, a different physical appliance).
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


# ------------------------------------------------------------- catalog

# sku, product (stable key -- never reused across different physical
# items), customer-facing name, amount_cents, price_id_env_var.
HARDWARE_CATALOG = [
    ("AIC-APPLIANCE-RYZEN-STARTER", "ryzen_starter", "AnyAiCam Ryzen Starter Appliance", 124999, "ANYAICAM_STRIPE_PRICE_RYZEN_STARTER"),
    ("AIC-APPLIANCE-RYZEN-ENTERPRISE", "ryzen_enterprise", "AnyAiCam Ryzen Enterprise Appliance", 174999, "ANYAICAM_STRIPE_PRICE_RYZEN_ENTERPRISE"),
    ("AIC-APPLIANCE-RYZEN-AAC-FACIAL", "ryzen_aac_facial_recognition", "AnyAiCam Ryzen AAC Facial Recognition Appliance", 224999, "ANYAICAM_STRIPE_PRICE_RYZEN_AAC_FACIAL"),
    ("AIC-RELAY-NUMATO-3CH", "numato_3_channel_relay", "Numato 3-Channel Relay Module", 14999, "ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH"),
]


def _catalog_rows() -> list[dict]:
    out = []
    for sku, product, name, amount_cents, env_var in HARDWARE_CATALOG:
        out.append({
            "sku": sku,
            "product": product,
            "name": name,
            "amount_cents": amount_cents,
            "stripe_price_id": os.environ.get(env_var, "").strip() or None,
        })
    return out


def hardware_catalog_for_website() -> list[dict]:
    """Ready-to-render catalog data for a customer-facing hardware
    picker -- sku, product key, name, and verified one-time price. Omits
    stripe_price_id (irrelevant to a pricing page, and unset for all four
    items today besides), matching customer_entitlements.pricing_table_
    for_website()'s same omission for the same reason."""
    return [
        {"sku": r["sku"], "product": r["product"], "name": r["name"], "amount_cents": r["amount_cents"]}
        for r in _catalog_rows()
    ]


def _load_hardware_price_map() -> dict:
    mapping = {}
    for r in _catalog_rows():
        if r["stripe_price_id"]:
            mapping[r["stripe_price_id"]] = {
                "sku": r["sku"], "product": r["product"], "name": r["name"], "amount_cents": r["amount_cents"],
            }
    return mapping


HARDWARE_PRICE_MAP = _load_hardware_price_map()


def resolve_hardware_sku(price_id: str) -> Optional[dict]:
    """Returns {"sku","product","name","amount_cents"} for a server-
    verified hardware Price ID, or None if unrecognized -- callers must
    treat None as "not a hardware purchase" (e.g. let customer_
    entitlements.resolve_tier() have a turn), never assume/guess."""
    if not price_id:
        return None
    entry = HARDWARE_PRICE_MAP.get(price_id)
    if not isinstance(entry, dict):
        return None
    return dict(entry)


# --------------------------------------------------------------- orders


def upsert_order(
    *,
    order_id: Optional[str] = None,
    customer_id: Optional[str],
    sku: str,
    product_name: str,
    stripe_price_id: str,
    quantity: int = 1,
    amount_cents: int,
    stripe_checkout_session_id: Optional[str] = None,
    stripe_payment_intent_id: Optional[str] = None,
    stripe_customer_id: Optional[str] = None,
    status: str = "pending",
    fulfillment_status: str = "unfulfilled",
) -> dict:
    """Idempotent per (stripe_checkout_session_id, sku) when a session id
    is present -- a replayed webhook delivery for the same session/SKU
    updates the SAME row (e.g. pending -> paid) rather than creating a
    duplicate order. Without a session id (a rare/manual path) this
    always inserts a new row, since there is nothing to key a replay
    check against."""
    existing = None
    if stripe_checkout_session_id:
        existing = row(
            "SELECT * FROM hardware_orders WHERE stripe_checkout_session_id=? AND sku=?",
            (stripe_checkout_session_id, sku),
        )
    now = _now()
    with connection() as db:
        if existing:
            db.execute(
                "UPDATE hardware_orders SET customer_id=COALESCE(?,customer_id),status=?,"
                "fulfillment_status=?,stripe_payment_intent_id=COALESCE(?,stripe_payment_intent_id),"
                "stripe_customer_id=COALESCE(?,stripe_customer_id),updated_at=? WHERE id=?",
                (customer_id, status, fulfillment_status, stripe_payment_intent_id,
                 stripe_customer_id, now, existing["id"]),
            )
            order_row_id = existing["id"]
        else:
            order_row_id = order_id or uuid.uuid4().hex
            db.execute(
                "INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,"
                "stripe_checkout_session_id,stripe_payment_intent_id,stripe_customer_id,quantity,"
                "amount_cents,currency,status,fulfillment_status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_row_id, customer_id, sku, product_name, stripe_price_id,
                 stripe_checkout_session_id, stripe_payment_intent_id, stripe_customer_id, quantity,
                 amount_cents, "usd", status, fulfillment_status, now, now),
            )
    return row("SELECT * FROM hardware_orders WHERE id=?", (order_row_id,))


def get_orders_for_customer(customer_id: str) -> list:
    return rows("SELECT * FROM hardware_orders WHERE customer_id=? ORDER BY created_at DESC", (customer_id,))


# ----------------------------------------------------------- pending links


def create_pending_link(
    *, email: str, stripe_price_id: str, sku: str, product_name: str, amount_cents: int,
    quantity: int = 1, stripe_customer_id: Optional[str] = None,
    stripe_checkout_session_id: Optional[str] = None, raw_event: dict,
) -> dict:
    """Same checkout-first safety net as customer_entitlements.create_
    pending_link(), for a hardware purchase made under an email with no
    matching authoritative customer yet."""
    link_id = uuid.uuid4().hex
    with connection() as db:
        db.execute(
            "INSERT INTO pending_hardware_order_links(id,normalized_email,stripe_customer_id,"
            "stripe_checkout_session_id,stripe_price_id,sku,product_name,quantity,amount_cents,"
            "raw_event_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (link_id, normalize_email(email), stripe_customer_id, stripe_checkout_session_id,
             stripe_price_id, sku, product_name, quantity, amount_cents, json.dumps(raw_event),
             "pending", _now()),
        )
    return row("SELECT * FROM pending_hardware_order_links WHERE id=?", (link_id,))


def resolve_pending_links_for_customer(customer_id: str, email: str) -> list:
    """Mirrors customer_entitlements.resolve_pending_links_for_customer()
    -- call only against an email the caller has already verified belongs
    to this customer (e.g. right after registration approval)."""
    normalized = normalize_email(email)
    pending = rows("SELECT * FROM pending_hardware_order_links WHERE normalized_email=? AND status='pending'", (normalized,))
    resolved_ids = []
    for link in pending:
        upsert_order(
            customer_id=customer_id, sku=link["sku"], product_name=link["product_name"],
            stripe_price_id=link["stripe_price_id"], quantity=link["quantity"], amount_cents=link["amount_cents"],
            stripe_checkout_session_id=link["stripe_checkout_session_id"],
            stripe_customer_id=link["stripe_customer_id"], status="paid",
        )
        with connection() as db:
            db.execute(
                "UPDATE pending_hardware_order_links SET status='resolved',resolved_at=?,resolved_customer_id=? WHERE id=?",
                (_now(), customer_id, link["id"]),
            )
        resolved_ids.append(link["id"])
    return resolved_ids


# --------------------------------------------------------- Stripe webhook bridge
#
# Wired into the live POST /api/payments/stripe/webhook route in main.py
# alongside (never replacing) customer_entitlements.sync_entitlement_
# from_stripe_event() -- see this module's docstring for why the two stay
# independent. Idempotency here is table-level (upsert_order's session+sku
# key), not a separate processed-events table, since a hardware order has
# no "current state to converge on" the way an entitlement does -- a
# replayed checkout.session.completed for the same session+sku is simply
# the same UPDATE running twice, which is safe by construction.


def sync_hardware_order_from_stripe_event(event: dict) -> dict:
    """Never raises for a normal 'nothing to do here' event. Only
    checkout.session.completed sessions in mode=payment are considered --
    a hardware purchase is one-time, never a subscription event."""
    event_type = str(event.get("type") or "")
    if event_type != "checkout.session.completed":
        return {"status": "ignored", "reason": f"unhandled event type {event_type!r}"}

    session_obj = (event.get("data") or {}).get("object") or {}
    if str(session_obj.get("mode") or "") not in ("payment", ""):
        # An explicit non-"payment" mode (e.g. "subscription") is never a
        # hardware purchase -- ignore rather than guess. Missing/empty
        # mode (older test payloads) is tolerated so existing hardware-
        # order tests don't need to thread a mode field they don't care
        # about; metadata price-id resolution below is what actually
        # gates whether anything is created either way.
        return {"status": "ignored", "reason": "not a one-time payment session"}

    metadata = session_obj.get("metadata") or {}
    price_id = str(metadata.get("anyaicam_stripe_price_id") or "").strip()
    hardware = resolve_hardware_sku(price_id)
    if not hardware:
        return {"status": "ignored", "reason": "no verified hardware mapping for this stripe price id", "price_id": price_id}

    quantity = max(1, int(metadata.get("anyaicam_hardware_quantity") or 1))
    email = (session_obj.get("customer_details") or {}).get("email") or session_obj.get("customer_email") or ""
    authoritative_customer_id = str(metadata.get("anyaicam_customer_id") or "") or None
    stripe_customer_id = str(session_obj.get("customer") or "") or None
    stripe_checkout_session_id = str(session_obj.get("id") or "") or None
    payment_status = str(session_obj.get("payment_status") or "")
    status = "paid" if payment_status == "paid" else "pending"

    customer = None
    if authoritative_customer_id:
        customer = row("SELECT * FROM customers WHERE id=?", (authoritative_customer_id,))
    if not customer and email:
        customer = find_customer_by_email(email)

    if not customer:
        if not email:
            return {"status": "ignored", "reason": "no authoritative customer id and no email to reconcile against"}
        link = create_pending_link(
            email=email, stripe_price_id=price_id, sku=hardware["sku"], product_name=hardware["name"],
            amount_cents=hardware["amount_cents"] * quantity, quantity=quantity,
            stripe_customer_id=stripe_customer_id, stripe_checkout_session_id=stripe_checkout_session_id,
            raw_event=event,
        )
        return {"status": "pending_link_created", "pending_link_id": link["id"]}

    order = upsert_order(
        customer_id=customer["id"], sku=hardware["sku"], product_name=hardware["name"],
        stripe_price_id=price_id, quantity=quantity, amount_cents=hardware["amount_cents"] * quantity,
        stripe_checkout_session_id=stripe_checkout_session_id, stripe_customer_id=stripe_customer_id,
        status=status,
    )
    return {"status": "order_recorded", "order_id": order["id"], "customer_id": customer["id"], "sku": hardware["sku"]}
