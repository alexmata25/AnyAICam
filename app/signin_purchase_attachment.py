"""Attach an existing customer's guest purchases after sign-in (Green 2026-10-17, post-RC5; owner-approved for development
on claude/green-127-post-rc5 under Red's seven conditions; rewritten after Codex's security review -- three high
blockers -- and owner acceptance of that review. NOT for RC5 without the owner's explicit approval).

The ONLY entry point that attaches purchases by email is attach_after_signin(), called by partner_login_submit right
after a fully successful customer sign-in. Email confirmation (direct_onboarding) and registration approval
(customer_registration) no longer attach anything (Codex blocker 1); the old resolve_pending_links_for_customer()
helpers now delegate here, so every remaining caller goes through the same fenced protocol.

Conditions (Red, 2026-10-17):
  (a) after a fully successful sign-in only (password checked; account active and approved; no forced password change;
      customer accounts have no second factor today -- if one is introduced, the call must move after it), and only
      for role customer_owner;
  (b) only when THIS account's own address was confirmed (verified self-service signup for this customer and address,
      or an approved /customer-register request with email_verified_at / email_verified_for = this address), and the
      customers row carries the same address -- never a partner-created or otherwise unconfirmed account;
  (c) only rows still pending/unattached whose purchase email equals that address (trim + lowercase); nothing already
      attached is ever moved;
  (e) an audit record per attachment, written in the SAME transaction as the purchase transition and the grant (Codex
      blocker 3), and one "added to your account" email, delivered separately (retryable, deduplicated);
  (f) never touches the session, cookie, role or destination; never raises into the sign-in;
  (g) never called from password reset.

Fenced protocol per purchase row (Codex blocker 2; condition d):
  1. CLAIM (own transaction): one conditional UPDATE sets status='attaching', a fresh random claim_token and
     claimed_at -- only from 'pending', or from an 'attaching' claim whose lease (CLAIM_LEASE_SECONDS) has expired
     (takeover). Exactly one competing worker can win; the others change no row and do nothing.
  2. FINALIZE (one transaction): first a conditional UPDATE to 'resolved' that requires the SAME claim_token (the
     fence: a worker whose claim was taken over changes no row and grants nothing); then the grant (existing idempotent
     upserts, joined into this transaction); then a unique purchase_attachments row; then the audit record. Any failure
     rolls the whole step back -- the row returns to 'attaching' under the same token and is retried after the lease.
  No claim is ever deleted, so one worker can never remove another's claim.
  3. EMAIL (after commit): notify_purchase_attached, keyed by the attachment id; purchase_attachments.email_sent_at
     records delivery; retry_purchase_attached_emails() (billing worker) re-sends what is still unsent.
"""
from __future__ import annotations

import logging
import json
import secrets
from datetime import datetime, timedelta

from partner_db import audit, backend, connection, transaction

logger = logging.getLogger("anyaicam.signin_purchase_attachment")
CLAIM_LEASE_SECONDS = 120

# (kind, table, label)
PENDING_TABLES = (("plan", "pending_customer_links", "camera plan"),
                  ("hardware", "pending_hardware_order_links", "hardware order"),
                  ("analytics", "pending_analytics_links", "analytics add-on"))
GUEST_CHECKOUTS = "hardware_checkouts"


class FenceLost(Exception):
    """The claim was taken over (lease expired) or the row changed: nothing may be granted."""


def _normalize(email) -> str:
    return (email or "").strip().lower()


def _now() -> datetime:
    return datetime.now()


def _stripe_refs(link: dict) -> dict[str, str]:
    """Recover durable Stripe identifiers from the verified checkout event."""
    try:
        event = json.loads(str(link.get("raw_event_json") or "{}"))
    except (TypeError, ValueError):
        event = {}
    obj = ((event.get("data") or {}).get("object") or {}) if isinstance(event, dict) else {}
    return {
        "payment_intent": str(obj.get("payment_intent") or ""),
        "charge": str(obj.get("charge") or ""),
        "invoice": str(obj.get("invoice") or ""),
        "subscription": str(obj.get("subscription") or ""),
    }


def _recorded_reversal(db, kind: str, table: str, link: dict) -> str | None:
    """Return a durable reversal for a pending purchase, without making a network call."""
    refs = _stripe_refs(link)
    if table == GUEST_CHECKOUTS:
        order = db.execute(
            "SELECT stripe_payment_intent_id,status FROM hardware_orders WHERE stripe_checkout_session_id=? LIMIT 1",
            (link.get("stripe_checkout_session_id"),),
        ).fetchone()
        if order:
            if str(order["status"] or "") in ("refunded", "disputed"):
                return str(order["status"])
            refs["payment_intent"] = refs["payment_intent"] or str(order["stripe_payment_intent_id"] or "")
    if not any(refs.values()):
        return None
    import billing_status
    return billing_status.reversal_reason(refs["charge"], refs["payment_intent"], refs["invoice"])


# ------------------------------------------------------------------ eligibility (a), (b)

def _eligible(user: dict | None) -> tuple[bool, str]:
    if not user:
        return False, "no account"
    if user.get("role") != "customer_owner":
        return False, "not a customer owner"
    if not user.get("customer_id"):
        return False, "no customer"
    if user.get("must_change_password"):
        return False, "password change pending"
    if str(user.get("account_status") or "active") != "active" or not user.get("approved", 1):
        return False, "account not active"
    return True, ""


def address_confirmed_at(db, *, customer_id: str, email: str) -> str | None:
    """When THIS account's own address was confirmed (ISO time), or None if it never was."""
    found = db.execute("SELECT verified_at FROM direct_signups WHERE customer_id=? AND lower(email)=? "
                       "AND verified_at IS NOT NULL ORDER BY verified_at LIMIT 1", (customer_id, email)).fetchone()
    if found:
        return str(found["verified_at"])
    try:
        found = db.execute(
            "SELECT email_verified_at FROM customer_registration_requests WHERE customer_id=? AND status='approved' "
            "AND email_verified_at IS NOT NULL AND lower(email)=? AND lower(email_verified_for)=? "
            "ORDER BY email_verified_at LIMIT 1", (customer_id, email, email)).fetchone()
    except Exception:
        return None
    return str(found["email_verified_at"]) if found else None


def account_address_confirmed(db, *, customer_id: str, email: str) -> bool:
    return address_confirmed_at(db, customer_id=customer_id, email=email) is not None


# ------------------------------------------------------------------ grants (joined into the finalize transaction)

def _grant(kind: str, link: dict, customer_id: str) -> None:
    refs = _stripe_refs(link)
    if kind == "plan":
        import customer_entitlements as ce
        ce.upsert_entitlement(customer_id=customer_id, product=link["product"],
                              camera_slot_quantity=link["camera_slot_quantity"], status="active",
                              stripe_customer_id=link["stripe_customer_id"],
                              stripe_subscription_id=refs["subscription"] or None,
                              stripe_checkout_session_id=link["stripe_checkout_session_id"],
                              stripe_price_id=link.get("stripe_price_id"))
        if refs["payment_intent"]:
            with connection() as db:
                db.execute("UPDATE customer_entitlements SET stripe_payment_intent_id=? WHERE customer_id=? AND product=?",
                           (refs["payment_intent"], customer_id, link["product"]))
    elif kind == "hardware":
        import hardware_orders as ho
        ho.upsert_order(customer_id=customer_id, sku=link["sku"], product_name=link["product_name"],
                        stripe_price_id=link["stripe_price_id"], quantity=link["quantity"],
                        amount_cents=link["amount_cents"], stripe_checkout_session_id=link["stripe_checkout_session_id"],
                        stripe_payment_intent_id=refs["payment_intent"] or None,
                        stripe_customer_id=link["stripe_customer_id"], status="paid")
    elif kind == "analytics":
        import analytics_entitlements as ae
        ae.upsert_analytics_subscription(customer_id=customer_id, analytic_key=link["analytic_key"], status="active",
                                         stripe_customer_id=link["stripe_customer_id"],
                                         stripe_subscription_id=refs["subscription"] or None,
                                         stripe_price_id=link["stripe_price_id"],
                                         licensed_quantity=int(link.get("licensed_quantity") or 1))
        addon = ae.resolve_addon(link["stripe_price_id"])
        if addon:
            ae.upsert_addon_subscription(customer_id=customer_id, addon_key=addon["addon_key"], status="active",
                                         quantity=int(link.get("licensed_quantity") or 1),
                                         stripe_customer_id=link["stripe_customer_id"],
                                         stripe_subscription_id=refs["subscription"] or None,
                                         stripe_price_id=link["stripe_price_id"])
    elif kind == "guest_hardware":
        with connection() as db:
            db.execute("UPDATE hardware_orders SET customer_id=?,updated_at=? WHERE stripe_checkout_session_id=? "
                       "AND customer_id IS NULL", (customer_id, _now().isoformat(), link["stripe_checkout_session_id"]))
    else:
        raise ValueError(kind)


# ------------------------------------------------------------------ the fenced protocol

def claim(table: str, row_id: str, email: str, *, now: datetime | None = None) -> str | None:
    """Step 1. Returns the claim token, or None when another worker holds a live claim / the row is not attachable."""
    now = now or _now()
    token = secrets.token_hex(16)
    stale = (now - timedelta(seconds=CLAIM_LEASE_SECONDS)).isoformat()
    with transaction() as db:
        if table == GUEST_CHECKOUTS:
            moved = db.execute(
                "UPDATE hardware_checkouts SET claim_token=?,claimed_at=? WHERE id=? AND lower(email)=? AND status='paid' "
                "AND customer_id IS NULL AND (claim_token IS NULL OR claimed_at<?)",
                (token, now.isoformat(), row_id, email, stale)).rowcount
        else:
            moved = db.execute(
                f"UPDATE {table} SET status='attaching',claim_token=?,claimed_at=? WHERE id=? AND normalized_email=? "
                "AND (status='pending' OR (status='attaching' AND claimed_at<?))",
                (token, now.isoformat(), row_id, email, stale)).rowcount
    return token if moved == 1 else None


def finalize(kind: str, table: str, row_id: str, token: str, *, customer_id: str, email: str, label: str) -> str | None:
    """Step 2. Transition (fenced by token) + grant + attachment record + audit, all or nothing. Returns the
    attachment id; raises FenceLost (nothing granted) when the claim is no longer ours. A payment already marked
    refunded/disputed is durably rejected before any ownership or entitlement change."""
    attachment_id = secrets.token_hex(16)
    now = _now().isoformat()
    with transaction() as db:
        if table == GUEST_CHECKOUTS:
            link_row = db.execute("SELECT * FROM hardware_checkouts WHERE id=? AND claim_token=? AND customer_id IS NULL "
                                  "AND status='paid' AND lower(email)=?", (row_id, token, email)).fetchone()
            if not link_row:
                raise FenceLost(f"{table}:{row_id}")
            link = dict(link_row)
            if _recorded_reversal(db, kind, table, link):
                return None
            moved = db.execute(
                "UPDATE hardware_checkouts SET customer_id=?,linked_at=?,updated_at=?,claim_token=NULL WHERE id=? "
                "AND claim_token=? AND customer_id IS NULL AND status='paid' AND lower(email)=?",
                (customer_id, now, now, row_id, token, email)).rowcount
        else:
            select = f"SELECT * FROM {table} WHERE id=? AND status='attaching' AND claim_token=? AND normalized_email=?"
            if backend() == "postgresql":
                select += " FOR UPDATE"
            link_row = db.execute(select, (row_id, token, email)).fetchone()
            if not link_row:
                raise FenceLost(f"{table}:{row_id}")
            link = dict(link_row)
            reason = _recorded_reversal(db, kind, table, link)
            if reason:
                db.execute(f"UPDATE {table} SET status='reversed',claim_token=NULL,claimed_at=NULL WHERE id=? "
                           "AND status='attaching' AND claim_token=? AND normalized_email=?",
                           (row_id, token, email))
                return None
            moved = db.execute(
                f"UPDATE {table} SET status='resolved',resolved_at=?,resolved_customer_id=? WHERE id=? "
                "AND status='attaching' AND claim_token=? AND normalized_email=?",
                (now, customer_id, row_id, token, email)).rowcount
        if moved != 1:
            raise FenceLost(f"{table}:{row_id}")
        _grant(kind, link, customer_id)
        db.execute("INSERT INTO purchase_attachments(id,source_table,source_id,customer_id,normalized_email,kind,"
                   "attached_at) VALUES(?,?,?,?,?,?,?)", (attachment_id, table, row_id, customer_id, email, label, now))
        audit({"email": email, "role": "customer_owner"}, "signin.purchase_attached", "customer", customer_id,
              {"kind": label, "source_table": table, "source_id": row_id, "attachment_id": attachment_id})
    return attachment_id


def _candidates(email: str, *, confirmed_at: str | None = None, only_session_id: str | None = None) -> list[tuple[str, str, str, str]]:
    found = []
    with connection() as db:
        for kind, table, label in PENDING_TABLES:
            for r in db.execute(f"SELECT id FROM {table} WHERE normalized_email=? AND status IN ('pending','attaching')",
                                (email,)).fetchall():
                found.append((kind, table, r["id"], label))
        # A guest hardware-first checkout (RC5 rule, hardware_checkout.py): never linked to an account by email alone.
        # It attaches by email only when the owner confirmed this address AFTER the checkout was paid (the buyer then
        # created the account with the order email); otherwise only from the order's own confirmation page, which
        # proves possession of the checkout session (only_session_id).
        for r in db.execute("SELECT id,stripe_checkout_session_id,COALESCE(paid_at,created_at) AS paid FROM hardware_checkouts "
                            "WHERE lower(email)=? AND status='paid' AND customer_id IS NULL", (email,)).fetchall():
            by_session = only_session_id is not None and r["stripe_checkout_session_id"] == only_session_id
            by_order_then_confirm = confirmed_at is not None and str(r["paid"] or "") <= confirmed_at
            if by_session or by_order_then_confirm:
                found.append(("guest_hardware", GUEST_CHECKOUTS, r["id"], "hardware order"))
    return found


def attach_for_customer(customer_id: str, email: str, *, only_session_id: str | None = None,
                        kinds: tuple[str, ...] | None = None, confirmed_at: str | None = None) -> list[dict]:
    """Runs the protocol for every attachable purchase of this address. The caller has checked (a) and (b)."""
    email = _normalize(email)
    if only_session_id is not None:
        # The hardware order-confirmation page: only that guest checkout.
        kinds = ("guest_hardware",)
    attached = []
    for kind, table, row_id, label in _candidates(email, confirmed_at=confirmed_at, only_session_id=only_session_id):
        if kinds is not None and kind not in kinds:
            continue
        token = claim(table, row_id, email)
        if not token:
            continue
        try:
            attachment_id = finalize(kind, table, row_id, token, customer_id=customer_id, email=email, label=label)
        except FenceLost:
            continue
        except Exception:
            logger.exception("signin_attachment.finalize_failed table=%s id=%s", table, row_id)
            continue  # rolled back: still 'attaching' under our token; retried after the lease
        if not attachment_id:
            continue
        attached.append({"kind": label, "attachment_id": attachment_id})
        _deliver_email(attachment_id)
    return attached


def attach_after_signin(user: dict | None, *, only_session_id: str | None = None) -> dict:
    """Never raises."""
    try:
        ok, reason = _eligible(user)
        if not ok:
            return {"status": "skipped", "reason": reason}
        customer_id, email = str(user["customer_id"]), _normalize(user.get("email"))
        with connection() as db:
            confirmed_at = address_confirmed_at(db, customer_id=customer_id, email=email)
            if confirmed_at is None:
                return {"status": "skipped", "reason": "the account's address was never confirmed"}
            customer = db.execute("SELECT email FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer or _normalize(customer["email"]) != email:
            return {"status": "skipped", "reason": "sign-in address does not match the customer record"}
        attached = attach_for_customer(customer_id, email, only_session_id=only_session_id, confirmed_at=confirmed_at)
        return {"status": "attached" if attached else "nothing to attach", "attached": attached}
    except Exception as exc:
        logger.exception("signin_attachment.failed")
        return {"status": "error", "reason": type(exc).__name__}


# ------------------------------------------------------------------ email (separate, retryable, deduplicated)

def _deliver_email(attachment_id: str) -> bool:
    try:
        with connection() as db:
            item = db.execute("SELECT * FROM purchase_attachments WHERE id=?", (attachment_id,)).fetchone()
        if not item or item["email_sent_at"]:
            return bool(item)
        from purchase_notifications import notify_purchase_attached
        result = notify_purchase_attached(item["customer_id"], item["kind"], attachment_id)
        if result.get("status") == "sent" or (result.get("status") == "skipped" and result.get("reason") == "already sent"):
            with connection() as db:
                db.execute("UPDATE purchase_attachments SET email_sent_at=? WHERE id=? AND email_sent_at IS NULL",
                           (_now().isoformat(), attachment_id))
            return True
    except Exception:
        logger.exception("signin_attachment.email_failed id=%s", attachment_id)
    return False


def retry_purchase_attached_emails(limit: int = 50) -> int:
    """Billing-worker pass: attachments whose email has not been delivered yet."""
    with connection() as db:
        pending = [r["id"] for r in db.execute(
            "SELECT id FROM purchase_attachments WHERE email_sent_at IS NULL ORDER BY attached_at LIMIT ?", (limit,)).fetchall()]
    return sum(1 for attachment_id in pending if _deliver_email(attachment_id))
