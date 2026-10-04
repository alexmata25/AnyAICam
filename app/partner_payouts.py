"""Quarterly partner payout statements (owner rules, 2026-10-04).

Amounts accrue in commission_ledger as money is collected (sales_commissions.py,
partner_agreements.py). After a calendar quarter ends (Jan-Mar, Apr-Jun,
Jul-Sep, Oct-Dec), a global administrator closes it: for every payee, each
ledger entry recorded before the quarter's end and not yet on a statement is
attached to that quarter's statement (partner_payout_items, one statement per
entry, ever). The statement is:

    opening balance (a negative balance carried from the previous statement)
  + the entries attached now (positive amounts owed to the partner, negative
    reversals and amounts the partner owes AnyAiCam)
  = period total; payable = max(0, total); carried forward = min(0, total).

Partner money collected by AnyAiCam carries its share of the Stripe
processing fee (sales_commissions.settle_processing_fee). Before a quarter is
closed every such fee is settled from Stripe; an invoice whose fee Stripe
does not report yet is held, with all its entries, for the next statement,
so a partner is never paid gross of a fee that is later deducted.

A statement is marked paid only to a payee whose payout bank account is
verified with the payout provider (partner_banking.py).

A refund or chargeback after a statement was issued is a new negative entry,
so it lands on the next statement and the issued one never changes. The
payee is the partner company (partner_id), or the salesperson when they
belong to no partner. Every payment-level row behind a statement stays in
the ledger, so each payout is auditable.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date, datetime
from typing import Optional

from partner_db import audit, connection, row, rows

ISSUED, NOTHING_PAYABLE, PAID = "issued", "nothing_payable", "paid"
_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")


class PayoutError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def quarter_of(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def bounds(quarter: str) -> tuple[date, date]:
    """(first day, first day of the next quarter)."""
    match = _QUARTER.match(str(quarter or ""))
    if not match:
        raise PayoutError("Choose a quarter as YYYY-Qn.")
    year, q = int(match.group(1)), int(match.group(2))
    start = date(year, 3 * (q - 1) + 1, 1)
    end = date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
    return start, end


def payee_key(partner_id: Optional[str], salesperson_user_id: str) -> str:
    return partner_id or f"user:{salesperson_user_id}"


def _payee_expr() -> str:
    return "COALESCE(l.partner_id, 'user:' || l.salesperson_user_id)"


def _last(payee: str) -> Optional[dict]:
    return row("SELECT * FROM partner_payouts WHERE payee_key=? ORDER BY period_end DESC LIMIT 1", (payee,))


def close_quarter(identity: dict, quarter: str, *, today: Optional[date] = None) -> list[dict]:
    """Issue every payee's statement for a quarter that has ended.
    Idempotent: a payee already issued for the quarter is skipped."""
    from partner_agreements import AgreementForbidden, is_global_admin
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    start, end = bounds(quarter)
    if (today or date.today()) < end:
        raise PayoutError(f"{quarter} has not ended yet.", status=409)
    cutoff = end.isoformat()
    import sales_commissions
    for invoice_id in sales_commissions.unsettled_processing_fees(cutoff):
        try:
            sales_commissions.settle_processing_fee(invoice_id)
        except Exception:
            pass  # held below and reported
    held_invoices = set(sales_commissions.unsettled_processing_fees(cutoff))
    pending = rows(f"SELECT l.*, {_payee_expr()} AS payee FROM commission_ledger l "
                   "LEFT JOIN partner_payout_items i ON i.ledger_entry_id=l.id WHERE i.ledger_entry_id IS NULL AND l.earned_at < ?",
                   (cutoff,))
    by_payee: dict[str, list[dict]] = {}
    for entry in pending:
        if entry.get("stripe_invoice_id") in held_invoices:
            continue  # its processing fee is not known yet: next statement
        by_payee.setdefault(entry["payee"], []).append(entry)
    now, actor, issued = datetime.now().isoformat(), identity.get("email", ""), []
    for payee, entries in sorted(by_payee.items()):
        if row("SELECT id FROM partner_payouts WHERE payee_key=? AND quarter=?", (payee, quarter)):
            continue
        previous = _last(payee)
        if previous and previous["period_end"] > start.isoformat():
            raise PayoutError(f"A later quarter ({previous['quarter']}) is already closed for {payee}.", status=409)
        opening = int((previous or {}).get("carried_forward_cents") or 0)
        counted = [e for e in entries if e["status"] != "not_eligible_friends_family"]
        period_total = sum(int(e["amount_cents"]) for e in counted)
        balance = opening + period_total
        payout_id = uuid.uuid4().hex
        first = entries[0]
        with connection() as db:
            db.execute("INSERT INTO partner_payouts(id,payee_key,partner_id,salesperson_user_id,quarter,period_start,period_end,"
                       "opening_balance_cents,period_total_cents,payable_cents,carried_forward_cents,status,issued_at,issued_by) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (payout_id, payee, first.get("partner_id"), None if first.get("partner_id") else first["salesperson_user_id"],
                        quarter, start.isoformat(), end.isoformat(), opening, period_total, max(0, balance), min(0, balance),
                        ISSUED if balance > 0 else NOTHING_PAYABLE, now, actor))
            for entry in entries:
                db.execute("INSERT INTO partner_payout_items(ledger_entry_id,payout_id,amount_cents) VALUES(?,?,?)",
                           (entry["id"], payout_id, int(entry["amount_cents"])))
        statement = row("SELECT * FROM partner_payouts WHERE id=?", (payout_id,))
        audit(identity, "partner_payout.issued", "partner_payout", payout_id,
              {"payee": payee, "quarter": quarter, "opening_balance_cents": opening, "period_total_cents": period_total,
               "payable_cents": statement["payable_cents"], "carried_forward_cents": statement["carried_forward_cents"],
               "entries": len(entries)})
        issued.append(statement)
    if held_invoices:
        audit(identity, "partner_payout.held_unsettled_fees", "partner_payout", quarter, {"invoices": sorted(held_invoices)})
    return issued


def mark_paid(identity: dict, payout_id: str, *, reference: str) -> dict:
    from partner_agreements import AgreementForbidden, is_global_admin
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    reference = str(reference or "").strip()
    if not reference or len(reference) > 120:
        raise PayoutError("Enter the payment reference.")
    payout = row("SELECT * FROM partner_payouts WHERE id=?", (str(payout_id or ""),))
    if not payout:
        raise LookupError("Payout not found.")
    if payout["status"] != ISSUED:
        raise PayoutError("Only an issued statement with an amount payable can be marked paid.", status=409)
    import partner_banking
    if not partner_banking.is_verified(payout["payee_key"]):
        raise PayoutError("This payee has no verified payout bank account yet.", status=409)
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute("UPDATE partner_payouts SET status=?,paid_at=?,paid_by=?,payment_reference=? WHERE id=?",
                   (PAID, now, identity.get("email", ""), reference, payout["id"]))
    audit(identity, "partner_payout.paid", "partner_payout", payout["id"],
          {"payee": payout["payee_key"], "quarter": payout["quarter"], "payable_cents": payout["payable_cents"], "reference": reference})
    return row("SELECT * FROM partner_payouts WHERE id=?", (payout["id"],))


def _scope(identity: dict) -> tuple[str, tuple]:
    """SQL filter on partner_payouts for what this identity may see."""
    from partner_agreements import is_global_admin
    if is_global_admin(identity):
        return "1=1", ()
    if identity.get("role") in ("partner_owner", "administrator") and identity.get("partner_id"):
        return "payee_key=?", (identity["partner_id"],)
    if identity.get("role") == "salesperson":
        me = row("SELECT id,partner_id FROM partner_users WHERE lower(email)=lower(?)", (identity.get("email", ""),))
        if me and not me.get("partner_id"):
            return "payee_key=?", (f"user:{me['id']}",)
    return "1=0", ()


def statements(identity: dict) -> list[dict]:
    where, params = _scope(identity)
    return rows(f"SELECT * FROM partner_payouts WHERE {where} ORDER BY period_end DESC, payee_key", params)


def statement(identity: dict, payout_id: str) -> dict:
    where, params = _scope(identity)
    payout = row(f"SELECT * FROM partner_payouts WHERE id=? AND {where}", (str(payout_id or ""), *params))
    if not payout:
        raise LookupError("Payout not found.")
    payout["entries"] = rows("SELECT l.* FROM partner_payout_items i JOIN commission_ledger l ON l.id=i.ledger_entry_id "
                             "WHERE i.payout_id=? ORDER BY l.earned_at", (payout["id"],))
    return payout


def next_payout_for(partner_id: Optional[str], salesperson_user_id: str, *, today: Optional[date] = None) -> dict:
    """The statement the payee's unpaid entries will go on next."""
    payee = payee_key(partner_id, salesperson_user_id)
    current = quarter_of(today or date.today())
    _, end = bounds(current)
    accrued = row("SELECT COALESCE(SUM(l.amount_cents),0) AS n FROM commission_ledger l "
                  "LEFT JOIN partner_payout_items i ON i.ledger_entry_id=l.id "
                  f"WHERE i.ledger_entry_id IS NULL AND {_payee_expr()}=? AND l.status<>'not_eligible_friends_family'", (payee,))
    last = _last(payee)
    return {"quarter": current, "closes_after": end.isoformat(),
            "carried_forward_cents": int((last or {}).get("carried_forward_cents") or 0),
            "accrued_cents": int((accrued or {}).get("n") or 0),
            "last_statement": {k: last[k] for k in ("quarter", "status", "payable_cents")} if last else None}


KIND_LABELS = {"recurring": "20% residual", "partner_upcharge": "Partner monthly upcharge",
               "partner_activation_fee": "Partner activation / setup fee", "partner_processing_fee": "Stripe processing fee on partner money",
               "partner_base_due": "AnyAiCam base owed by partner", "partner_base_remitted": "Base paid to AnyAiCam",
               "hardware": "Hardware commission (discontinued)", "activation": "Activation commission (discontinued)"}


def dashboard(partner_id: Optional[str], salesperson_user_id: Optional[str] = None, *, today: Optional[date] = None) -> dict:
    """What the payee has accrued and not yet received, split the way a
    partner reads it: 20% residual, partner markup (upcharge and activation
    fee net of their processing fees), adjustments (refunds, chargebacks and
    partner-billed base owed/paid), the balance carried from the last
    statement, the current quarter and the next payout."""
    payee = payee_key(partner_id, salesperson_user_id or "")
    entries = rows(f"SELECT l.* FROM commission_ledger l LEFT JOIN partner_payout_items i ON i.ledger_entry_id=l.id "
                   f"WHERE i.ledger_entry_id IS NULL AND {_payee_expr()}=? AND l.status<>'not_eligible_friends_family'", (payee,))
    residual = sum(int(e["amount_cents"]) for e in entries if e["kind"] == "recurring" and not e.get("reverses_entry_id"))
    markup = sum(int(e["amount_cents"]) for e in entries if not e.get("reverses_entry_id")
                 and e["kind"] in ("partner_upcharge", "partner_activation_fee", "partner_processing_fee"))
    other = sum(int(e["amount_cents"]) for e in entries if not e.get("reverses_entry_id")
                and e["kind"] not in ("recurring", "partner_upcharge", "partner_activation_fee", "partner_processing_fee"))
    adjustments = sum(int(e["amount_cents"]) for e in entries if e.get("reverses_entry_id")) + other
    last = _last(payee)
    carried = int((last or {}).get("carried_forward_cents") or 0)
    current = quarter_of(today or date.today())
    _, end = bounds(current)
    accrued = residual + markup + adjustments
    return {"payee_key": payee, "quarter": current, "statement_after": end.isoformat(), "residual_cents": residual,
            "markup_cents": markup, "adjustments_cents": adjustments, "accrued_cents": accrued, "carried_forward_cents": carried,
            "estimated_next_payout_cents": max(0, accrued + carried),
            "history": rows("SELECT * FROM partner_payouts WHERE payee_key=? ORDER BY period_end DESC", (payee,))}


def _text(value: str) -> str:
    """A spreadsheet must never run a customer name or note as a formula."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def statement_csv(identity: dict, payout_id: str) -> tuple[str, str]:
    """(file name, CSV text) of one statement with every payment-level line."""
    s = statement(identity, payout_id)
    names = {c["id"]: c["name"] for c in rows("SELECT id,name FROM customers")} if s["entries"] else {}
    out = io.StringIO()
    writer = csv.writer(out)
    money = lambda cents: f"{int(cents) / 100:.2f}"
    for label, value in (("Quarter", s["quarter"]), ("Payee", s["payee_key"]), ("Period", f"{s['period_start']} to {s['period_end']}"),
                         ("Opening balance", money(s["opening_balance_cents"])), ("This quarter", money(s["period_total_cents"])),
                         ("Payable", money(s["payable_cents"])), ("Carried forward", money(s["carried_forward_cents"])),
                         ("Status", s["status"]), ("Paid", s.get("paid_at") or ""), ("Payment reference", s.get("payment_reference") or "")):
        writer.writerow([label, value])
    writer.writerow([])
    writer.writerow(["Recorded", "Customer", "Type", "Reversal of", "Invoice", "Service month", "Collected", "Amount", "Note"])
    for e in s["entries"]:
        writer.writerow([e["earned_at"], _text(names.get(e["customer_id"], e["customer_id"])), KIND_LABELS.get(e["kind"], e["kind"]),
                         e.get("reverses_entry_id") or "", e.get("stripe_invoice_id") or "", e.get("service_month") or "",
                         money(e["collected_cents"]) if e.get("collected_cents") is not None else "", money(e["amount_cents"]),
                         _text(e.get("note") or "")])
    return f"anyaicam-payout-{s['quarter']}-{s['payee_key'].replace(':', '-')}.csv", out.getvalue()
