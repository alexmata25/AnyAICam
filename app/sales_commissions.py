"""Server-side salesperson commission ledger (2026-09-30).

Driven only by verified Stripe webhook events (the "sales_commissions" step
of POST /api/payments/stripe/webhook) -- never by a browser, a success page
or a visual estimate. Every earned amount is a row in commission_ledger with
its salesperson, customer, subscription/order, tier, the payment that
generated it, status and dates; reversals are recorded on the same rows.

Rules (pricing_catalog):
- No activation commission (owner, 2026-10-04; the earlier $40-$125 was
  removed). Rows already written stay as history.
- Recurring: 20% of subscription revenue actually collected (invoice
  amount paid, excluding tax), for invoices within the customer's first 12
  paid months (counted by paid base-plan invoices). Nothing is paid ahead:
  each month's commission exists only once that month's invoice is paid,
  so a cancellation simply stops it. Failed/unpaid invoices earn nothing.
- Partner agreements (partner_agreements.py, 2026-10-04): on an AnyAiCam-
  billed partner customer's invoice, the partner's upcharge line and
  activation/setup fee line are the partner's money ('partner_upcharge',
  'partner_activation_fee' rows, owed to the partner in full) and earn no
  commission; the 20% applies only to the AnyAiCam plan/add-on lines, at
  the rate recorded on the agreement.
- Hardware: no commission and no residual (owner, 2026-10-04; the earlier
  $50 / $75 / $100 per appliance was removed). Rows already earned stay as
  history, and a refund of such an order still reverses them.
- Stripe processing fees on the partner's own money: the share of the
  charge's Stripe fee attributable to the upcharge and activation fee is a
  negative 'partner_processing_fee' row (settle_processing_fee). Stripe keeps
  its fee when a payment is refunded, so a refund never reverses that row.
- Friends & Family approved: every commission is $0 (the row is still
  written, status not_eligible_friends_family, for attribution/reporting).
  A partner's own upcharge and fee are still owed to the partner.
- Refunds and disputes reverse what that payment generated (proportionally
  for a partial refund) by APPENDING a negative 'reversal' row per entry;
  the original row is never changed in amount, so a closed quarterly payout
  (partner_payouts.py) stays exactly as issued and a reversal after it
  carries forward.

Attribution: sales_attributions (set by an administrator) or, when absent,
the salesperson who created the customer in the Partner Portal
(customers.created_by). No attribution -> no commission rows.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

import pricing_catalog
from partner_db import connection, row, rows

logger = logging.getLogger("anyaicam.sales_commissions")

SALES_ROLES = {"salesperson", "partner_owner"}
EARNED, REVERSED, NOT_ELIGIBLE_FF = "earned", "reversed", "not_eligible_friends_family"
REVERSAL = "reversal"  # status of an appended negative row; kind stays the original's
PARTNER_UPCHARGE, PARTNER_ACTIVATION_FEE = "partner_upcharge", "partner_activation_fee"
PARTNER_PROCESSING_FEE = "partner_processing_fee"
PARTNER_MONEY_KINDS = (PARTNER_UPCHARGE, PARTNER_ACTIVATION_FEE)


def _now() -> str:
    return datetime.now().isoformat()


# ------------------------------------------------------------ attribution

def _sales_user(identifier: str) -> Optional[dict]:
    if not identifier:
        return None
    user = row("SELECT * FROM partner_users WHERE id=? OR lower(email)=lower(?)", (identifier, identifier))
    return user if user and user.get("role") in SALES_ROLES else None


def set_attribution(*, customer_id: str, salesperson: str, source: str, created_by: str, replace: bool = False,
                    actor_role: str = "administrator") -> dict:
    """Assign (or, with replace=True, re-assign) a customer's salesperson.
    Every change is written to the central audit log (2026-10-02, Codex
    finding): who, which customer, previous and new salesperson, and whether
    it replaced an existing attribution -- future commissions follow it."""
    user = _sales_user(salesperson)
    if not user:
        raise ValueError("That user is not a salesperson or partner owner.")
    if not row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        raise LookupError("Customer not found.")
    existing = row("SELECT * FROM sales_attributions WHERE customer_id=?", (customer_id,))
    with connection() as db:
        if existing and not replace:
            return existing  # unchanged: nothing to audit
        if existing:
            db.execute("UPDATE sales_attributions SET salesperson_user_id=?,partner_id=?,source=?,created_at=?,created_by=? WHERE customer_id=?",
                       (user["id"], user.get("partner_id"), source, _now(), created_by, customer_id))
        else:
            db.execute("INSERT INTO sales_attributions(customer_id,salesperson_user_id,partner_id,source,created_at,created_by) VALUES(?,?,?,?,?,?)",
                       (customer_id, user["id"], user.get("partner_id"), source, _now(), created_by))
    from partner_db import audit
    audit({"email": created_by, "role": actor_role},
          "sales_attribution.replaced" if existing else "sales_attribution.assigned", "customer", customer_id,
          {"customer_id": customer_id, "previous_salesperson_user_id": (existing or {}).get("salesperson_user_id"),
           "previous_partner_id": (existing or {}).get("partner_id"), "new_salesperson_user_id": user["id"],
           "new_partner_id": user.get("partner_id"), "replacement": bool(existing), "source": source})
    return row("SELECT * FROM sales_attributions WHERE customer_id=?", (customer_id,))


def attribution_for(customer_id: str) -> Optional[dict]:
    explicit = row("SELECT * FROM sales_attributions WHERE customer_id=?", (customer_id,))
    if explicit:
        return {"salesperson_user_id": explicit["salesperson_user_id"], "partner_id": explicit.get("partner_id"),
                "source": explicit["source"]}
    customer = row("SELECT created_by FROM customers WHERE id=?", (customer_id,))
    user = _sales_user(str((customer or {}).get("created_by") or ""))
    if user:
        return {"salesperson_user_id": user["id"], "partner_id": user.get("partner_id"), "source": "partner_portal_customer"}
    return None


# ------------------------------------------------------------ ledger

def _record(*, kind: str, source_ref: str, customer_id: str, attribution: dict, amount_cents: int,
            basis_cents: int = 0, friends_family: bool, **extra) -> Optional[dict]:
    """Idempotent per (kind, source_ref): a retried webhook never writes twice."""
    if row("SELECT id FROM commission_ledger WHERE kind=? AND source_ref=?", (kind, source_ref)):
        return None
    amount = 0 if friends_family else int(amount_cents)
    record_id = uuid.uuid4().hex
    columns = {
        "id": record_id, "kind": kind, "source_ref": source_ref,
        "salesperson_user_id": attribution["salesperson_user_id"], "partner_id": attribution.get("partner_id"),
        "customer_id": customer_id, "basis_cents": int(basis_cents), "original_amount_cents": amount, "amount_cents": amount,
        "status": NOT_ELIGIBLE_FF if friends_family else EARNED, "friends_family": 1 if friends_family else 0,
        "earned_at": _now(),
    }
    columns.update({k: v for k, v in extra.items() if v is not None})
    names = ",".join(columns)
    with connection() as db:
        db.execute(f"INSERT INTO commission_ledger({names}) VALUES({','.join('?' for _ in columns)})", tuple(columns.values()))
    return row("SELECT * FROM commission_ledger WHERE id=?", (record_id,))


def _ff_approved(customer_id: str) -> bool:
    import friends_family
    return friends_family.is_approved(customer_id)


# ------------------------------------------------------------ Stripe helpers

def _invoice_metadata(invoice: dict) -> dict:
    for candidate in (
        (invoice.get("subscription_details") or {}).get("metadata"),
        ((invoice.get("parent") or {}).get("subscription_details") or {}).get("metadata"),
    ):
        if candidate:
            return candidate
    lines = ((invoice.get("lines") or {}).get("data") or [])
    return (lines[0].get("metadata") or {}) if lines and isinstance(lines[0], dict) else {}


def _invoice_price_id(invoice: dict) -> str:
    lines = ((invoice.get("lines") or {}).get("data") or [])
    if lines and isinstance(lines[0], dict):
        line = lines[0]
        price = line.get("price") or ((line.get("pricing") or {}).get("price_details") or {}).get("price")
        if isinstance(price, dict):
            return str(price.get("id") or "")
        if isinstance(price, str):
            return price
    return str(_invoice_metadata(invoice).get("anyaicam_stripe_price_id") or "")


def _line_price_id(line: dict) -> str:
    price = line.get("price") or ((line.get("pricing") or {}).get("price_details") or {}).get("price")
    if isinstance(price, dict):
        return str(price.get("id") or "")
    return price if isinstance(price, str) else ""


def _classify_invoice(invoice: dict) -> tuple[Optional[str], Optional[dict], str, float]:
    """(product_class, product, price_id, eligible_fraction) for a whole
    invoice, independent of line order (2026-10-02, Codex finding: lines[0]
    alone used to decide, so base+add-on and add-on+base invoices were
    treated differently). The invoice is a base-plan invoice when ANY line
    is a known base plan -- that line's tier drives activation; otherwise an
    add-on invoice when any line is a known add-on. eligible_fraction is the
    share of the invoice's line amounts on known AnyAiCam plan/add-on lines,
    so an unknown price line never earns commission. Percentages, timing and
    add-on rules are unchanged."""
    lines = [line for line in ((invoice.get("lines") or {}).get("data") or []) if isinstance(line, dict)]
    if not lines:
        price_id = _invoice_price_id(invoice)
        product_class, product = _classify(price_id)
        return product_class, product, price_id, 1.0
    classified = []
    for line in lines:
        price_id = _line_price_id(line)
        product_class, product = _classify(price_id)
        classified.append((product_class, product, price_id, max(0, int(line.get("amount") or 0))))
    base = sorted((c for c in classified if c[0] == "base"), key=lambda c: (-int(c[1].get("camera_slot_maximum") or 0), c[2]))
    addon = sorted((c for c in classified if c[0] == "addon"), key=lambda c: c[2])
    chosen = base[0] if base else (addon[0] if addon else None)
    if not chosen:
        return None, None, classified[0][2], 0.0
    total = sum(c[3] for c in classified)
    eligible = sum(c[3] for c in classified if c[0] is not None)
    fraction = (eligible / total) if total > 0 else 1.0
    return chosen[0], chosen[1], chosen[2], fraction


def _invoice_subscription_id(invoice: dict) -> str:
    sub = invoice.get("subscription") or ((invoice.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    return str(sub.get("id") if isinstance(sub, dict) else sub or "")


def _stripe_get(path: str) -> dict:
    """Read-only Stripe call through the app's own helper (auth, errors).
    Imported lazily: main imports this module's webhook step."""
    import main
    return main.stripe_api_get(path)


def _invoice_payment_intent(invoice: dict) -> Optional[str]:
    """The PaymentIntent that paid this invoice. Older Stripe API versions put
    it on the invoice (`payment_intent`); current ones (e.g. 2026-07-29.dahlia,
    this account's version) moved it to the invoice's payments list, which is
    not in the webhook payload, so it is looked up. None if not found."""
    direct = invoice.get("payment_intent")
    if direct:
        return str(direct.get("id") if isinstance(direct, dict) else direct)
    for item in ((invoice.get("payments") or {}).get("data") or []):
        intent = (item.get("payment") or {}).get("payment_intent")
        if intent:
            return str(intent.get("id") if isinstance(intent, dict) else intent)
    try:
        listed = _stripe_get(f"/v1/invoice_payments?invoice={invoice['id']}&limit=5")
    except Exception:
        return None
    for item in listed.get("data") or []:
        intent = (item.get("payment") or {}).get("payment_intent")
        if intent and item.get("status", "paid") == "paid":
            return str(intent.get("id") if isinstance(intent, dict) else intent)
    return None


def _invoice_for_payment_intent(intent: str) -> Optional[str]:
    """Reverse lookup for a refund/dispute on current Stripe API versions,
    whose charges no longer name their invoice."""
    try:
        listed = _stripe_get(f"/v1/invoice_payments?payment[type]=payment_intent&payment[payment_intent]={intent}&limit=1")
    except Exception:
        return None
    data = listed.get("data") or []
    if not data:
        return None
    invoice = data[0].get("invoice")
    return str(invoice.get("id") if isinstance(invoice, dict) else invoice) if invoice else None


def _classify(price_id: str) -> tuple[Optional[str], Optional[dict]]:
    from customer_entitlements import resolve_tier
    tier = resolve_tier(price_id)
    if tier:
        return "base", tier
    from analytics_entitlements import resolve_addon
    addon = resolve_addon(price_id)
    return ("addon", addon) if addon else (None, None)


def _customer_for(metadata: dict, stripe_customer_id: str) -> Optional[str]:
    customer_id = str(metadata.get("anyaicam_customer_id") or "")
    if customer_id and row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        return customer_id
    if stripe_customer_id:
        for table in ("customer_entitlements", "analytics_subscriptions", "hardware_orders"):
            found = row(f"SELECT customer_id FROM {table} WHERE stripe_customer_id=? AND customer_id IS NOT NULL LIMIT 1", (stripe_customer_id,))
            if found:
                return found["customer_id"]
    return None


def _verified_invoice_owner(invoice: dict, subscription_id: str) -> tuple[Optional[str], str]:
    """(customer_id, "") when the invoice's Stripe customer, its subscription
    and its metadata all belong to one AnyAiCam account; (None, why)
    otherwise -- then neither account's payments or commissions change
    (Codex audit of 3f5b9c4, finding 7). Stripe unreadable -> retried."""
    import stripe_state
    stripe_customer_id = str(invoice.get("customer") or "")
    if not stripe_customer_id:
        return None, "invoice has no Stripe customer"
    metadata_customer_id = str(_invoice_metadata(invoice).get("anyaicam_customer_id") or "") or None
    owners = stripe_state.stripe_customer_accounts(stripe_customer_id)
    if metadata_customer_id and owners and owners != {metadata_customer_id}:
        return None, "the invoice names a different account than its Stripe customer"
    if len(owners) > 1:
        return None, "the Stripe customer is linked to more than one account"
    subscription = stripe_state.current_subscription(subscription_id)
    if str(subscription.get("customer") or "") != stripe_customer_id:
        return None, "the invoice and its subscription belong to different Stripe customers"
    subscription_customer_id = str((subscription.get("metadata") or {}).get("anyaicam_customer_id") or "") or None
    candidates = {c for c in (metadata_customer_id, subscription_customer_id, *owners) if c}
    if len(candidates) > 1:
        return None, "the invoice, its subscription and its Stripe customer name different accounts"
    customer_id = next(iter(candidates), None) or _customer_for({}, stripe_customer_id)
    if not customer_id or not row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        return None, "customer not resolved"
    return customer_id, ""


def _base_paid_months(customer_id: str) -> int:
    found = row("SELECT COUNT(*) AS n FROM subscription_payments WHERE customer_id=? AND product_class='base'", (customer_id,))
    return int((found or {}).get("n") or 0)


def _line_kind(line: dict) -> str:
    """'recurring' for a subscription line, 'one_time' for an invoice item."""
    price = line.get("price") if isinstance(line.get("price"), dict) else {}
    parent_type = str((line.get("parent") or {}).get("type") or "")
    if line.get("type") == "invoiceitem" or parent_type == "invoice_item_details" or price.get("type") == "one_time":
        return "one_time"
    return "recurring"


def _split_agreement_invoice(invoice: dict) -> dict:
    """What an AnyAiCam-billed partner customer actually paid, by owner:
    AnyAiCam plan/add-on lines (known catalog prices), the partner's monthly
    upcharge (any other recurring line) and the partner's activation/setup
    fee (any other one-time line). Each line counts net of its discounts,
    and the total is scaled to the amount actually paid excluding tax."""
    lines = [line for line in ((invoice.get("lines") or {}).get("data") or []) if isinstance(line, dict)]
    parts = {"anyaicam": 0, "upcharge": 0, "activation_fee": 0}
    for line in lines:
        net = int(line.get("amount") or 0) - sum(int(d.get("amount") or 0) for d in (line.get("discount_amounts") or []) if isinstance(d, dict))
        if net <= 0:
            continue
        if _classify(_line_price_id(line))[0] is not None:
            parts["anyaicam"] += net
        elif _line_kind(line) == "recurring":
            parts["upcharge"] += net
        else:
            parts["activation_fee"] += net
    tax = int(invoice.get("tax") or 0) + sum(int(t.get("amount") or 0) for t in (invoice.get("total_taxes") or []) if isinstance(t, dict))
    collected = max(0, int(invoice.get("amount_paid") or 0) - tax)
    total = sum(parts.values())
    if total > 0 and collected != total:  # credits, rounding: scale, keeping the cents exact
        scaled = {k: int(v * collected // total) for k, v in parts.items()}
        scaled["anyaicam"] += collected - sum(scaled.values())
        parts = scaled
    parts["collected"] = collected
    return parts


# ------------------------------------------------------------ event handlers

def _invoice_paid(event: dict) -> dict:
    invoice = (event.get("data") or {}).get("object") or {}
    invoice_id = str(invoice.get("id") or "")
    subscription_id = _invoice_subscription_id(invoice)
    amount_paid = int(invoice.get("amount_paid") or 0)
    if not invoice_id or not subscription_id or amount_paid <= 0:
        return {"status": "ignored", "reason": "not a paid subscription invoice"}
    product_class, product, price_id, eligible_fraction = _classify_invoice(invoice)
    if product_class is None:
        return {"status": "ignored", "reason": "not an AnyAiCam plan or add-on price", "price_id": price_id}
    customer_id, why = _verified_invoice_owner(invoice, subscription_id)
    if not customer_id:
        logger.warning("commission.invoice_rejected invoice=%s reason=%s", invoice_id, why)
        return {"status": "rejected" if why != "customer not resolved" else "ignored", "reason": why}
    tax = int(invoice.get("tax") or 0) + sum(int(t.get("amount") or 0) for t in (invoice.get("total_taxes") or []) if isinstance(t, dict))
    basis = int(round(max(0, amount_paid - tax) * eligible_fraction))  # unknown price lines earn nothing
    import partner_agreements
    agreement = partner_agreements.agreement_for_invoice(customer_id, subscription_id, _invoice_metadata(invoice))
    split = _split_agreement_invoice(invoice) if agreement else None
    if split:
        basis = split["anyaicam"]  # the partner's upcharge and fee are never commission basis
    if not row("SELECT id FROM subscription_payments WHERE id=?", (invoice_id,)):
        with connection() as db:
            db.execute(
                "INSERT INTO subscription_payments(id,customer_id,stripe_customer_id,stripe_subscription_id,stripe_price_id,product_class,"
                "amount_paid_cents,currency,period_start,paid_at,stripe_charge_id,stripe_payment_intent_id,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (invoice_id, customer_id, str(invoice.get("customer") or ""), subscription_id, price_id, product_class, basis,
                 str(invoice.get("currency") or "usd"), str(invoice.get("period_start") or ""), _now(),
                 str(invoice.get("charge") or "") or None, _invoice_payment_intent(invoice), "paid"),
            )
    if agreement:
        attribution = {"salesperson_user_id": agreement["salesperson_user_id"], "partner_id": agreement.get("partner_id"),
                       "source": "partner_agreement"}
        rate_bps = int(agreement["commission_rate_bps"])
        max_months = int(agreement["commission_max_paid_months"])
    else:
        attribution = attribution_for(customer_id)
        rate_bps = pricing_catalog.RECURRING_COMMISSION_PERCENT * 100
        max_months = pricing_catalog.RECURRING_COMMISSION_MAX_PAID_MONTHS
    if not attribution:
        return {"status": "payment_recorded", "commission": "no salesperson attribution"}
    ff = _ff_approved(customer_id)
    paid_months = _base_paid_months(customer_id)
    written = []
    plan_type = tier_slots = None
    if product_class == "base":
        plan_type = product["product"].replace("camera_slots_", "")
        tier_slots = int(product["camera_slot_maximum"])
    common = {"stripe_subscription_id": subscription_id, "stripe_invoice_id": invoice_id, "plan_type": plan_type,
              "camera_slot_tier": tier_slots, "paid_month_index": paid_months, "collected_at": _now(),
              "agreement_id": (agreement or {}).get("id"), "billing_party": (agreement or {}).get("billing_party")}
    if 0 < paid_months <= max_months:
        amount = int(round(basis * rate_bps / 10000))
        if _record(kind="recurring", source_ref=f"invoice:{invoice_id}", customer_id=customer_id,
                   attribution=attribution, amount_cents=amount, basis_cents=basis, friends_family=ff,
                   commission_rate_bps=rate_bps, collected_cents=basis, **common):
            written.append("recurring")
    if split:
        # The partner's own money, owed to the partner in full (Friends &
        # Family zeroes AnyAiCam's commission, never the partner's charges).
        for kind, cents in ((PARTNER_UPCHARGE, split["upcharge"]), (PARTNER_ACTIVATION_FEE, split["activation_fee"])):
            if cents > 0 and _record(kind=kind, source_ref=f"invoice:{invoice_id}", customer_id=customer_id,
                                     attribution=attribution, amount_cents=cents, basis_cents=cents, friends_family=False,
                                     collected_cents=cents, **common):
                written.append(kind)
        partner_agreements.note_payment(agreement, paid_month_index=paid_months,
                                        activation_fee_collected=split["activation_fee"] > 0, subscription_id=subscription_id)
        try:
            settle_processing_fee(invoice_id)
        except Exception:  # Stripe unreadable now: settled before the quarter closes (partner_payouts)
            logger.warning("commission.processing_fee_unsettled invoice=%s", invoice_id, exc_info=True)
    return {"status": "commission_recorded", "kinds": written, "paid_month_index": paid_months, "friends_family": ff}


def _hardware_paid(event: dict) -> dict:
    """Hardware earns no commission since 2026-10-04 (owner decision)."""
    return {"status": "ignored", "reason": "hardware earns no commission"}


def _charge_fee(payment: dict) -> Optional[tuple[str, int, int]]:
    """(charge id, charge amount, Stripe fee) for a recorded payment, read
    from the charge's balance transaction; None while Stripe has no fee yet."""
    charge = None
    if payment.get("stripe_charge_id"):
        charge = _stripe_get(f"/v1/charges/{payment['stripe_charge_id']}?expand[]=balance_transaction")
    elif payment.get("stripe_payment_intent_id"):
        intent = _stripe_get(f"/v1/payment_intents/{payment['stripe_payment_intent_id']}?expand[]=latest_charge.balance_transaction")
        charge = intent.get("latest_charge") if isinstance(intent.get("latest_charge"), dict) else None
    balance = (charge or {}).get("balance_transaction")
    if not isinstance(balance, dict) or balance.get("fee") is None or not int(charge.get("amount") or 0):
        return None
    return str(charge.get("id") or ""), int(charge["amount"]), int(balance["fee"])


def settle_processing_fee(invoice_id: str) -> Optional[dict]:
    """Deduct from the partner the Stripe fee attributable to the partner's
    own money on one invoice (owner, 2026-10-04): fee x (partner upcharge +
    activation fee collected) / charge amount, rounded half up to the cent.
    One row per invoice, written once; the note records every input so the
    figure can be recomputed. Returns the row, or None when the invoice has no
    partner money, is already settled, or Stripe has no fee yet."""
    partner = [e for e in rows("SELECT * FROM commission_ledger WHERE stripe_invoice_id=? AND reverses_entry_id IS NULL", (invoice_id,))
               if e["kind"] in PARTNER_MONEY_KINDS]
    if not partner or row("SELECT id FROM commission_ledger WHERE kind=? AND source_ref=?", (PARTNER_PROCESSING_FEE, f"fee:{invoice_id}")):
        return None
    payment = row("SELECT * FROM subscription_payments WHERE id=?", (invoice_id,))
    found = _charge_fee(payment or {})
    if not found:
        return None
    charge_id, charge_amount, fee = found
    partner_cents = sum(int(e["amount_cents"]) for e in partner)
    share = (2 * fee * partner_cents + charge_amount) // (2 * charge_amount)
    first = partner[0]
    return _record(kind=PARTNER_PROCESSING_FEE, source_ref=f"fee:{invoice_id}", customer_id=first["customer_id"],
                   attribution={"salesperson_user_id": first["salesperson_user_id"], "partner_id": first.get("partner_id")},
                   amount_cents=-share, basis_cents=partner_cents, friends_family=False, agreement_id=first.get("agreement_id"),
                   billing_party=first.get("billing_party"), stripe_subscription_id=first.get("stripe_subscription_id"),
                   stripe_invoice_id=invoice_id, collected_cents=charge_amount, collected_at=_now(), created_by="stripe",
                   earned_at=min(e["earned_at"] for e in partner),
                   note=f"Stripe fee {fee} cents on charge {charge_id} of {charge_amount} cents; partner share "
                        f"{partner_cents}/{charge_amount} = {share} cents")


def unsettled_processing_fees(before: str) -> list[str]:
    """Invoices with partner money recorded before `before` and no fee row yet."""
    found = rows("SELECT DISTINCT l.stripe_invoice_id AS invoice_id FROM commission_ledger l WHERE l.kind IN (?,?) "
                 "AND l.reverses_entry_id IS NULL AND l.stripe_invoice_id IS NOT NULL AND l.earned_at < ? AND NOT EXISTS "
                 "(SELECT 1 FROM commission_ledger f WHERE f.kind=? AND f.source_ref='fee:' || l.stripe_invoice_id)",
                 (*PARTNER_MONEY_KINDS, before, PARTNER_PROCESSING_FEE))
    return [r["invoice_id"] for r in found]


def net_amount(entry: dict) -> int:
    """What an original entry is worth now: its amount plus its reversals.
    Rows reversed before 2026-10-04 were zeroed in place and have none."""
    found = row("SELECT COALESCE(SUM(amount_cents),0) AS n FROM commission_ledger WHERE reverses_entry_id=?", (entry["id"],))
    return int(entry["amount_cents"]) + int((found or {}).get("n") or 0)


def reverse_entry(entry: dict, *, remaining_cents: int, reason: str, created_by: str = "stripe") -> Optional[dict]:
    """Bring an earned entry down to remaining_cents by APPENDING a negative
    row (status 'reversal', same kind, reverses_entry_id=the original). The
    original's amount never changes, so a payout that already included it
    stays as issued and the reversal lands in the next one. Idempotent: the
    same target for the same entry writes once."""
    original = int(entry["amount_cents"])
    if entry["status"] not in (EARNED, REVERSED) or original == 0:
        return None
    delta = int(remaining_cents) - net_amount(entry)
    # A reversal moves an entry toward zero: down for money owed to the
    # partner, up for money the partner owes (partner_base_due).
    if delta == 0 or (delta > 0) == (original > 0):
        return None
    reversal = _record(kind=entry["kind"], source_ref=f"reversal:{entry['id']}:{int(remaining_cents)}",
                       customer_id=entry["customer_id"],
                       attribution={"salesperson_user_id": entry["salesperson_user_id"], "partner_id": entry.get("partner_id")},
                       amount_cents=delta, basis_cents=0, friends_family=False, reverses_entry_id=entry["id"],
                       stripe_subscription_id=entry.get("stripe_subscription_id"), stripe_invoice_id=entry.get("stripe_invoice_id"),
                       hardware_order_id=entry.get("hardware_order_id"), agreement_id=entry.get("agreement_id"),
                       billing_party=entry.get("billing_party"), service_month=entry.get("service_month"),
                       created_by=created_by, note=reason, status=REVERSAL)
    if reversal:
        with connection() as db:
            if remaining_cents == 0:  # status/time are annotations; the amount stays as earned
                db.execute("UPDATE commission_ledger SET status=?,reversed_at=?,reversal_reason=? WHERE id=?",
                           (REVERSED, _now(), reason, entry["id"]))
            else:
                db.execute("UPDATE commission_ledger SET reversal_reason=? WHERE id=?", (f"partial {reason}", entry["id"]))
    return reversal


def _reverse(entries: list[dict], *, fraction: float, reason: str) -> list[str]:
    """Reverse (fully or partly) what a payment generated. Stripe reports the
    CUMULATIVE refunded amount, so the target is always computed from the
    original amount."""
    changed = []
    for entry in entries:
        if entry["status"] != EARNED or entry.get("reverses_entry_id") or entry["kind"] == PARTNER_PROCESSING_FEE:
            continue  # Stripe keeps its processing fee on a refund
        remaining = 0 if fraction >= 0.999 else int(round(int(entry["original_amount_cents"]) * (1 - fraction)))
        if reverse_entry(entry, remaining_cents=remaining, reason=reason):
            changed.append(entry["id"])
    return changed


def _charge_reversed(event: dict, *, reason: str) -> dict:
    obj = (event.get("data") or {}).get("object") or {}
    charge = obj if obj.get("object") == "charge" else {"id": obj.get("charge"), "payment_intent": obj.get("payment_intent"),
                                                        "amount": obj.get("amount"), "amount_refunded": obj.get("amount"),
                                                        "invoice": None}
    charge_id = str(charge.get("id") or "")
    intent = str(charge.get("payment_intent") or "")
    invoice_id = str(charge.get("invoice") or "")
    amount = int(charge.get("amount") or 0)
    refunded = int(charge.get("amount_refunded") or 0)
    fraction = 1.0 if reason == "dispute" or not amount else min(1.0, refunded / amount)
    payment = None
    for column, value in (("id", invoice_id), ("stripe_charge_id", charge_id), ("stripe_payment_intent_id", intent)):
        if value:
            payment = row(f"SELECT * FROM subscription_payments WHERE {column}=?", (value,))
            if payment:
                break
    if payment is None and intent:
        # Current Stripe API versions: the charge doesn't name its invoice
        # and the invoice.paid payload didn't carry the PaymentIntent, so ask
        # Stripe which invoice this PaymentIntent paid.
        looked_up = _invoice_for_payment_intent(intent)
        if looked_up:
            payment = row("SELECT * FROM subscription_payments WHERE id=?", (looked_up,))
    reversed_ids = []
    if payment:
        if fraction >= 0.999:
            with connection() as db:
                db.execute("UPDATE subscription_payments SET status=?,refunded_at=? WHERE id=?",
                           ("disputed" if reason == "dispute" else "refunded", _now(), payment["id"]))
        entries = rows("SELECT * FROM commission_ledger WHERE stripe_invoice_id=?", (payment["id"],))
        reversed_ids += _reverse(entries, fraction=fraction, reason=reason)
    if intent:
        order = row("SELECT * FROM hardware_orders WHERE stripe_payment_intent_id=?", (intent,))
        if order:
            entries = rows("SELECT * FROM commission_ledger WHERE hardware_order_id=?", (order["id"],))
            reversed_ids += _reverse(entries, fraction=fraction, reason=reason)
    return {"status": "reversed" if reversed_ids else "ignored", "ledger_ids": reversed_ids}


def sync_commissions_from_stripe_event(event: dict) -> dict:
    event_type = str(event.get("type") or "")
    if event_type in ("invoice.paid", "invoice.payment_succeeded"):
        return _invoice_paid(event)
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return _hardware_paid(event)
    if event_type == "charge.refunded":
        return _charge_reversed(event, reason="refund")
    if event_type == "charge.dispute.created":
        return _charge_reversed(event, reason="dispute")
    if event_type == "customer.subscription.deleted":
        # Nothing already earned changes; the agreement simply ends, so no
        # further invoice (and no further residual) follows.
        import partner_agreements
        subscription = (event.get("data") or {}).get("object") or {}
        ended = partner_agreements.end_for_subscription(str(subscription.get("id") or ""), reason="subscription cancelled")
        return {"status": "agreement_ended" if ended else "ignored"}
    return {"status": "ignored", "reason": f"unhandled event type {event_type!r}"}


# ------------------------------------------------------------ reporting

def ledger_for(*, salesperson_user_id: Optional[str] = None, partner_id: Optional[str] = None) -> list[dict]:
    if salesperson_user_id:
        return rows("SELECT * FROM commission_ledger WHERE salesperson_user_id=? ORDER BY earned_at DESC", (salesperson_user_id,))
    if partner_id:
        return rows("SELECT * FROM commission_ledger WHERE partner_id=? ORDER BY earned_at DESC", (partner_id,))
    return rows("SELECT * FROM commission_ledger ORDER BY earned_at DESC")


SUMMARY_KINDS = ("activation", "hardware", "recurring", PARTNER_UPCHARGE, PARTNER_ACTIVATION_FEE, PARTNER_PROCESSING_FEE,
                 "partner_base_due", "partner_base_remitted")


def summarize(entries: list[dict]) -> dict:
    """Net amounts: earned rows plus their appended reversals (and rows that
    were reversed before reversals were appended, which hold 0). A kind is
    listed when it has any row ('activation' only for history)."""
    counted = [e for e in entries if e["status"] in (EARNED, REVERSED, REVERSAL)]
    present = {e["kind"] for e in entries}
    by_kind = {kind: sum(int(e["amount_cents"]) for e in counted if e["kind"] == kind)
               for kind in SUMMARY_KINDS if kind in present or kind in ("hardware", "recurring")}
    return {"earned_cents": sum(by_kind.values()), "by_kind_cents": by_kind,
            "reversed_count": sum(1 for e in entries if e["status"] == REVERSED),
            "friends_family_count": sum(1 for e in entries if e["status"] == NOT_ELIGIBLE_FF)}
