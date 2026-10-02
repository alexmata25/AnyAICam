"""Checkout guards (Codex audit of billing commit 3f5b9c4, findings 1 and 2).

1. One payable Checkout Session per purchase at a time. Before the first
   webhook arrives nothing in AnyAiCam shows a purchase, so two simultaneous
   requests (two tabs, a double click, Local and Hybrid at once) could each
   start a Stripe subscription. A durable claim per (account, purchase) --
   one row in checkout_pending, taken atomically by its primary key -- lets
   exactly one request create the session; the Stripe call carries an
   Idempotency-Key derived from that claim, so a concurrent request for the
   same thing gets the very same session back from Stripe, never a second.
   Purchases: "base" (Local and Hybrid share it: one base plan per account),
   "addon:<key>" and "vms_license". A claim lasts as long as its session
   (CHECKOUT_SESSION_SECONDS, passed to Stripe as expires_at). The open
   session is reused only for an identical checkout (same fields: plan,
   quantity, discount); anything different -- another plan, a Friends &
   Family decision made since -- first expires the open session in Stripe
   (which Stripe refuses once it has been paid), so only one can ever be
   paid. An expired session frees the claim (webhook step); a session whose
   payment is still pending (bank debit) keeps it. A PAID session holds the
   claim -- with no expiry -- until the webhook's entitlement provisioning
   for it has completed (Codex verification of 9a388c6, finding 1): while
   provisioning waits for a webhook retry, no second checkout can start.
2. One canonical Stripe customer per AnyAiCam account (stripe_customer_bindings,
   both columns unique). Every plan, add-on and VMS-license checkout passes
   `customer=<that id>` -- never only customer_email, which made Stripe
   create a new customer per purchase and left the owner billing portal
   with several. The first checkout creates the customer (idempotently per
   account, so concurrent first purchases share it); an account that already
   bought through exactly one Stripe customer keeps that one. A Stripe
   customer bound to another account is never used (tenant isolation).
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from datetime import datetime

from fastapi import HTTPException

from partner_db import connection, row

# Stripe requires a Checkout Session to live at least 30 minutes.
CHECKOUT_SESSION_SECONDS = 31 * 60
CLOCK_SKEW_SECONDS = 60
MIN_REUSE_SECONDS = 5 * 60  # a session about to expire is replaced, not reused
AWAITING_PAYMENT_SECONDS = 14 * 24 * 3600  # bank debits can take days
FREE_STATES = ("completed", "expired", "abandoned")
# Webhook steps that provision what a Checkout Session bought (main._stripe_webhook_steps).
PROVISIONING_STEPS = ("camera_slot_entitlements", "analytics_entitlements")

IN_PROGRESS = "A checkout for this purchase is already in progress. Finish it in the other window, or try again in a few minutes."
NEEDS_ATTENTION = "This account's billing records need attention. Please contact AnyAiCam support."


def _main():
    return sys.modules["main"]


def _now_iso() -> str:
    return datetime.now().isoformat()


# ------------------------------------------------------------------ 2. canonical Stripe customer

def bound_stripe_customer(customer_id: str) -> str | None:
    found = row("SELECT stripe_customer_id FROM stripe_customer_bindings WHERE customer_id=?", (customer_id,))
    return found["stripe_customer_id"] if found else None


def bind_stripe_customer(customer_id: str, stripe_customer_id: str) -> bool:
    """Bind once; never rebinds an account or a Stripe customer. True if
    this account is (now) bound to this Stripe customer."""
    if not customer_id or not stripe_customer_id:
        return False
    with connection() as db:
        db.execute("INSERT INTO stripe_customer_bindings(customer_id,stripe_customer_id,created_at) VALUES(?,?,?) "
                   "ON CONFLICT DO NOTHING", (customer_id, stripe_customer_id, _now_iso()))
    return bound_stripe_customer(customer_id) == stripe_customer_id


def canonical_stripe_customer(customer_id: str, *, email: str | None) -> str:
    import stripe_state
    from customer_billing import stripe_customer_ids_for_customer
    bound = bound_stripe_customer(customer_id)
    if bound:
        return bound
    known = stripe_customer_ids_for_customer(customer_id)
    if len(known) > 1:
        raise HTTPException(status_code=409, detail=NEEDS_ATTENTION)
    if known:
        candidate = next(iter(known))
        if stripe_state.bound_elsewhere(candidate, customer_id):
            raise HTTPException(status_code=409, detail=NEEDS_ATTENTION)
    else:
        fields = [("metadata[anyaicam_customer_id]", customer_id)]
        if email:
            fields.insert(0, ("email", email))
        created = _main().stripe_api_post("/v1/customers", fields, idempotency_key=f"anyaicam-customer-{customer_id}")
        candidate = str((created or {}).get("id") or "")
        if not candidate.startswith("cus_"):
            raise HTTPException(status_code=502, detail="Stripe did not return a customer. Please try again.")
    if not bind_stripe_customer(customer_id, candidate):
        raise HTTPException(status_code=409, detail=NEEDS_ATTENTION)
    return candidate


# ------------------------------------------------------------------ 1. one payable checkout per purchase

def _fingerprint(fields: list) -> str:
    return hashlib.sha256(json.dumps(sorted((str(k), str(v)) for k, v in fields)).encode()).hexdigest()


def _claim(customer_id: str, purchase: str, price_id: str, quantity: int, fingerprint: str) -> dict | None:
    """The claim row if this request took it, else None."""
    now = int(time.time())
    token = uuid.uuid4().hex
    with connection() as db:
        taken = db.execute(
            "INSERT INTO checkout_pending(customer_id,purchase,token,price_id,quantity,fingerprint,status,expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'creating',?,?,?) "
            "ON CONFLICT(customer_id,purchase) DO UPDATE SET token=excluded.token,price_id=excluded.price_id,"
            "quantity=excluded.quantity,fingerprint=excluded.fingerprint,session_id=NULL,checkout_url=NULL,status='creating',"
            "expires_at=excluded.expires_at,created_at=excluded.created_at,updated_at=excluded.updated_at "
            "WHERE checkout_pending.status IN ('completed','expired','abandoned') "
            "OR (checkout_pending.expires_at<? AND checkout_pending.status<>'paid')",
            (customer_id, purchase, token, price_id, quantity, fingerprint, now + CHECKOUT_SESSION_SECONDS, _now_iso(), _now_iso(),
             now - CLOCK_SKEW_SECONDS)).rowcount
    current = _pending(customer_id, purchase)
    return current if taken and current and current["token"] == token else None


def _pending(customer_id: str, purchase: str) -> dict | None:
    return row("SELECT * FROM checkout_pending WHERE customer_id=? AND purchase=?", (customer_id, purchase))


def _take_over(existing: dict, price_id: str, quantity: int, fingerprint: str) -> dict | None:
    """Replace an open session with a different one: expire it in Stripe
    first (refused once paid), then swap the claim if nobody else did."""
    if existing["status"] != "open" or not existing.get("session_id"):
        return None
    try:
        _main().stripe_api_post(f"/v1/checkout/sessions/{existing['session_id']}/expire", [],
                                idempotency_key=f"anyaicam-checkout-expire-{existing['session_id']}")
    except Exception:
        return None  # already paid (or Stripe unreachable): the open one stands
    now = int(time.time())
    token = uuid.uuid4().hex
    with connection() as db:
        swapped = db.execute(
            "UPDATE checkout_pending SET token=?,price_id=?,quantity=?,fingerprint=?,session_id=NULL,checkout_url=NULL,status='creating',"
            "expires_at=?,created_at=?,updated_at=? WHERE customer_id=? AND purchase=? AND token=?",
            (token, price_id, quantity, fingerprint, now + CHECKOUT_SESSION_SECONDS, _now_iso(), _now_iso(),
             existing["customer_id"], existing["purchase"], existing["token"])).rowcount
    return _pending(existing["customer_id"], existing["purchase"]) if swapped else None


def create_session(customer_id: str, purchase: str, *, price_id: str, quantity: int, fields: list) -> dict:
    """The Checkout Session for this purchase: created once, or the one
    already open for exactly the same purchase. HTTPException 409 while a
    different checkout for the same purchase is in progress."""
    fingerprint = _fingerprint(fields)
    claim = _claim(customer_id, purchase, price_id, quantity, fingerprint)
    if claim is None:
        existing = _pending(customer_id, purchase)
        if existing is None:
            raise HTTPException(status_code=409, detail=IN_PROGRESS)
        same = existing["fingerprint"] == fingerprint
        remaining = int(existing["expires_at"] or 0) - int(time.time())
        if same and existing["status"] == "open" and existing.get("checkout_url") and remaining >= MIN_REUSE_SECONDS:
            return {"id": existing["session_id"], "url": existing["checkout_url"], "reused": True}
        if same and existing["status"] == "creating":
            claim = existing  # a concurrent request: the same Idempotency-Key returns the same session
        else:
            claim = _take_over(existing, price_id, quantity, fingerprint)
            if claim is None:
                raise HTTPException(status_code=409, detail=IN_PROGRESS)
    session_fields = list(fields) + [("expires_at", str(int(claim["expires_at"])))]
    try:
        session = _main().stripe_api_post("/v1/checkout/sessions", session_fields,
                                          idempotency_key=f"anyaicam-checkout-{claim['token']}")
    except Exception:
        with connection() as db:  # nothing was created: free the claim for a retry
            db.execute("UPDATE checkout_pending SET status='abandoned',updated_at=? WHERE customer_id=? AND purchase=? "
                       "AND token=? AND session_id IS NULL", (_now_iso(), customer_id, purchase, claim["token"]))
        raise
    session_id, url = str(session.get("id") or ""), str(session.get("url") or "")
    if session_id and url:
        with connection() as db:
            db.execute("UPDATE checkout_pending SET session_id=?,checkout_url=?,status='open',updated_at=? "
                       "WHERE customer_id=? AND purchase=? AND token=?",
                       (session_id, url, _now_iso(), customer_id, purchase, claim["token"]))
    return session


class ProvisioningPending(Exception):
    """The paid session's entitlement provisioning has not completed yet:
    this step fails, so Stripe redelivers the event, and the claim stays."""


def _provisioned(event_id: str) -> bool:
    with connection() as db:
        done = {r["step"] for r in db.execute(
            "SELECT step FROM stripe_webhook_steps WHERE event_id=? AND status='completed'", (event_id,)).fetchall()}
    return all(step in done for step in PROVISIONING_STEPS)


def sync_from_stripe_event(event: dict) -> dict:
    """Webhook step: a paid session keeps blocking until it is provisioned,
    then frees its claim; one still awaiting a bank payment keeps it while
    the payment can still arrive; an expired one frees it."""
    event_type = str(event.get("type") or "")
    session = (event.get("data") or {}).get("object") or {}
    session_id = str(session.get("id") or "")
    if not session_id or not event_type.startswith("checkout.session."):
        return {"status": "ignored"}
    if event_type == "checkout.session.completed" and session.get("payment_status") not in ("paid", "no_payment_required"):
        state, expires = "awaiting_payment", int(time.time()) + AWAITING_PAYMENT_SECONDS
    elif event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        with connection() as db:  # paid: blocking, whatever happens to provisioning
            db.execute("UPDATE checkout_pending SET status='paid',updated_at=? WHERE session_id=? AND status<>'completed'",
                       (_now_iso(), session_id))
        if not _provisioned(str(event.get("id") or "")):
            raise ProvisioningPending("the purchase is paid but not provisioned yet")
        state, expires = "completed", None
    elif event_type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        state, expires = "expired", None
    else:
        return {"status": "ignored"}
    with connection() as db:
        if expires is None:
            changed = db.execute("UPDATE checkout_pending SET status=?,updated_at=? WHERE session_id=?",
                                 (state, _now_iso(), session_id)).rowcount
        else:
            changed = db.execute("UPDATE checkout_pending SET status=?,expires_at=?,updated_at=? WHERE session_id=? AND status='open'",
                                 (state, expires, _now_iso(), session_id)).rowcount
    return {"status": state if changed else "ignored"}
