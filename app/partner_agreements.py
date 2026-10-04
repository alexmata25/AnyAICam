"""Partner agreements: what a partner-sold customer pays, and who bills it
(owner rules, 2026-10-04).

An agreement belongs to one customer and records, as agreed at the time:
- the AnyAiCam plan and camera tier, and its authoritative base price
  (pricing_catalog -- never typed in, never overwritten by a selling price);
- the partner's approved monthly upcharge and the resulting customer price
  (base + upcharge, stored separately);
- the partner's activation/setup fee (the partner's money);
- the billing party: ANYAICAM_BILLED (AnyAiCam charges the customer the
  final price through Stripe and owes the partner its upcharge and fee) or
  PARTNER_BILLED (the partner charges the customer and owes AnyAiCam the
  base price);
- the residual terms: 20% for the customer's first 12 paid months.

A change is a NEW version (previous_agreement_id), never an edit, so every
payment stays tied to the terms that applied when it was collected. A
partner's proposal waits for a global administrator's approval (the
"approved upcharge"); an administrator's own agreement is approved as made.
Every proposal, approval, rejection, cancellation and collection is written
to audit_logs with the money terms before and after.

The money itself is in commission_ledger (sales_commissions.py); quarterly
statements are in partner_payouts.py.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Optional

import pricing_catalog
from partner_db import allowed, audit, connection, row, rows, tenant_owns_partner

PENDING, APPROVED, ACTIVE = "pending_approval", "approved", "active"
SUPERSEDED, REJECTED, CANCELLED, ENDED = "superseded", "rejected", "cancelled", "ended"
IN_FORCE = (APPROVED, ACTIVE)
EDIT_ROLES = {"administrator", "partner_owner", "salesperson"}
SALES_ROLES = {"salesperson", "partner_owner"}
BASE_DUE, BASE_REMITTED = "partner_base_due", "partner_base_remitted"
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MONEY_FIELDS = ("plan_type", "tier_label", "camera_tier", "base_price_cents", "upcharge_cents", "customer_price_cents",
                "activation_fee_cents", "billing_party", "commission_rate_bps", "commission_max_paid_months")


class AgreementError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AgreementForbidden(PermissionError):
    pass


def _now() -> str:
    return datetime.now().isoformat()


def _user(identifier: str) -> Optional[dict]:
    return row("SELECT * FROM partner_users WHERE id=? OR lower(email)=lower(?)", (identifier, identifier)) if identifier else None


def is_global_admin(identity: dict) -> bool:
    if identity.get("role") != "administrator":
        return False
    from appliance_identity import has_global_administrator_grant
    with connection() as db:
        return bool(has_global_administrator_grant(db, email=identity.get("email", "")))


def _cents(value, label: str) -> int:
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit())):
        raise AgreementError(f"Enter the {label} in cents, as a whole number of 0 or more.")
    cents = int(value)
    if cents < 0:
        raise AgreementError(f"The {label} cannot be negative.")
    return cents


def terms(plan_type: str, tier_label: str, upcharge_cents, activation_fee_cents) -> dict:
    """The agreement's money terms, priced only from the catalog."""
    plan = pricing_catalog.find_base_plan(str(plan_type or "").strip().lower(), str(tier_label or "").strip())
    if not plan:
        raise AgreementError("Choose an AnyAiCam plan and camera tier.")
    upcharge = _cents(upcharge_cents, "monthly upcharge")
    base = int(plan["monthly_cents"])
    return {"plan_type": plan["plan_type"], "tier_label": plan["tier_label"], "plan_label": plan["label"],
            "camera_tier": int(plan["camera_slot_maximum"]), "base_price_cents": base, "upcharge_cents": upcharge,
            "customer_price_cents": base + upcharge, "activation_fee_cents": _cents(activation_fee_cents, "activation fee"),
            "commission_rate_bps": pricing_catalog.RECURRING_COMMISSION_PERCENT * 100,
            "commission_max_paid_months": pricing_catalog.RECURRING_COMMISSION_MAX_PAID_MONTHS}


def in_force(customer_id: str) -> Optional[dict]:
    return row(f"SELECT * FROM partner_agreements WHERE customer_id=? AND status IN ({','.join('?' for _ in IN_FORCE)}) "
               "ORDER BY version DESC LIMIT 1", (customer_id, *IN_FORCE))


def pending(customer_id: str) -> Optional[dict]:
    return row("SELECT * FROM partner_agreements WHERE customer_id=? AND status=?", (customer_id, PENDING))


def partner_priced(agreement: Optional[dict]) -> bool:
    """Carries partner pricing: an upcharge, an activation fee, or partner
    billing. Never combined with Friends & Family (owner, 2026-10-04)."""
    return bool(agreement) and (int(agreement["upcharge_cents"]) > 0 or int(agreement["activation_fee_cents"]) > 0
                                or agreement["billing_party"] == pricing_catalog.BILLING_PARTNER)


def blocks_friends_family(customer_id: str) -> bool:
    return any(partner_priced(a) for a in (in_force(customer_id), pending(customer_id)))


def _money_view(agreement: Optional[dict]) -> Optional[dict]:
    return {k: agreement.get(k) for k in ("id", "version", "status", *MONEY_FIELDS)} if agreement else None


# ------------------------------------------------------------ authorization

def _require_editor(identity: dict) -> None:
    if identity.get("role") not in EDIT_ROLES or not allowed(identity, "customer.edit"):
        raise AgreementForbidden("Partner permission required.")


def _independent(identity: dict) -> Optional[dict]:
    """The signed-in salesperson when they belong to no partner company."""
    if identity.get("role") != "salesperson":
        return None
    me = _user(identity.get("email", ""))
    return me if me and not me.get("partner_id") else None


def _customer(identity: dict, customer_id: str) -> dict:
    customer = row("SELECT id,partner_id,name FROM customers WHERE id=?", (str(customer_id or ""),))
    if not customer:
        raise LookupError("Customer not found.")
    with connection() as db:
        owned = tenant_owns_partner(db, identity, customer.get("partner_id"))
    if not owned:
        indie = _independent(identity)
        from sales_commissions import attribution_for
        attribution = attribution_for(customer["id"]) if indie else None
        if not (attribution and attribution["salesperson_user_id"] == indie["id"]):
            raise AgreementForbidden("This customer belongs to another partner.")
    return customer


def _can_see(identity: dict, agreement: dict) -> bool:
    if is_global_admin(identity):
        return True
    indie = _independent(identity)
    if indie:
        return indie["id"] == agreement["salesperson_user_id"]
    with connection() as db:
        if not tenant_owns_partner(db, identity, agreement.get("partner_id")):
            return False
    if identity.get("role") == "salesperson":
        me = _user(identity.get("email", ""))
        return bool(me) and me["id"] == agreement["salesperson_user_id"]
    return identity.get("role") in EDIT_ROLES


def get(identity: dict, agreement_id: str) -> dict:
    agreement = row("SELECT * FROM partner_agreements WHERE id=?", (str(agreement_id or ""),))
    if not agreement:
        raise LookupError("Agreement not found.")
    if not _can_see(identity, agreement):
        raise AgreementForbidden("This agreement belongs to another partner.")
    return agreement


def visible(identity: dict) -> list[dict]:
    if is_global_admin(identity):
        return rows("SELECT * FROM partner_agreements ORDER BY created_at DESC")
    if identity.get("role") == "salesperson":
        me = _user(identity.get("email", ""))
        return rows("SELECT * FROM partner_agreements WHERE salesperson_user_id=? ORDER BY created_at DESC", (me["id"],)) if me else []
    if identity.get("role") in EDIT_ROLES and identity.get("partner_id"):
        return rows("SELECT * FROM partner_agreements WHERE partner_id=? ORDER BY created_at DESC", (identity["partner_id"],))
    return []


def _salesperson_for(identity: dict, customer: dict, salesperson: Optional[str]) -> dict:
    if identity.get("role") in SALES_ROLES:
        user = _user(identity.get("email", ""))
    else:
        user = _user(str(salesperson or "").strip())
        if not user:
            from sales_commissions import attribution_for
            attribution = attribution_for(customer["id"])
            user = _user(attribution["salesperson_user_id"]) if attribution else None
    if not user or user.get("role") not in SALES_ROLES:
        raise AgreementError("Choose the salesperson or partner owner who earns the residual.")
    from direct_onboarding import HOUSE_PARTNER_ID
    if user.get("partner_id") != customer.get("partner_id") and not (not user.get("partner_id")
                                                                      and customer.get("partner_id") in (None, HOUSE_PARTNER_ID)):
        raise AgreementError("The salesperson must belong to this customer's partner.")
    return user


def _check_change_allowed(current: Optional[dict], new: dict) -> None:
    """A running AnyAiCam-billed Stripe subscription charges what was agreed;
    changing its plan, upcharge or billing party would need a Stripe
    subscription change, which this release does not make. Refused rather
    than letting the records and the charge disagree."""
    if not current or current["status"] != ACTIVE or current["billing_party"] != pricing_catalog.BILLING_ANYAICAM \
            or not current.get("stripe_subscription_id"):
        return
    if any(current[k] != new[k] for k in ("plan_type", "tier_label", "base_price_cents", "upcharge_cents", "billing_party")):
        raise AgreementError("This customer's AnyAiCam subscription is already running at the agreed price. Changing the plan, "
                             "upcharge or billing party of a running subscription is not available yet.", status=409)


def _supersede_and_inherit(db, current: Optional[dict], new_id: str, actor: str, now: str) -> Optional[str]:
    """End the agreement in force and carry its running relationship (Stripe
    subscription, collected fee, residual dates) to the new version."""
    if not current:
        return None
    db.execute("UPDATE partner_agreements SET status=?,ended_at=?,end_reason=?,updated_at=?,updated_by=? WHERE id=?",
               (SUPERSEDED, now, f"replaced by {new_id}", now, actor, current["id"]))
    db.execute("UPDATE partner_agreements SET stripe_subscription_id=?,activation_fee_collected_at=?,commission_start_at=?,"
               "commission_end_at=? WHERE id=?",
               (current.get("stripe_subscription_id"), current.get("activation_fee_collected_at"),
                current.get("commission_start_at"), current.get("commission_end_at"), new_id))
    return ACTIVE if current["status"] == ACTIVE else APPROVED


# ------------------------------------------------------------ lifecycle

def propose(identity: dict, *, customer_id: str, plan_type: str, tier_label: str, upcharge_cents, activation_fee_cents,
            billing_party: str, salesperson: Optional[str] = None) -> dict:
    """Create (or change) a customer's agreement. A partner's proposal waits
    for approval; a global administrator's takes effect at once."""
    _require_editor(identity)
    customer = _customer(identity, customer_id)
    if billing_party not in pricing_catalog.BILLING_PARTIES:
        raise AgreementError("Choose who bills the customer: AnyAiCam or the partner.")
    new = terms(plan_type, tier_label, upcharge_cents, activation_fee_cents)
    new["billing_party"] = billing_party
    seller = _salesperson_for(identity, customer, salesperson)
    import friends_family
    if partner_priced(new) and friends_family.is_approved(customer["id"]):
        raise AgreementError("This customer has a Friends & Family discount, which cannot be combined with partner pricing "
                             "(an upcharge, activation fee or partner billing).", status=409)
    current = in_force(customer["id"])
    _check_change_allowed(current, new)
    admin = is_global_admin(identity)
    now, actor, agreement_id = _now(), identity.get("email", ""), uuid.uuid4().hex
    version = int((row("SELECT MAX(version) AS v FROM partner_agreements WHERE customer_id=?", (customer["id"],)) or {}).get("v") or 0) + 1
    with connection() as db:
        db.execute("UPDATE partner_agreements SET status=?,ended_at=?,end_reason=?,updated_at=?,updated_by=? WHERE customer_id=? AND status=?",
                   (SUPERSEDED, now, f"replaced by {agreement_id}", now, actor, customer["id"], PENDING))
        db.execute(
            "INSERT INTO partner_agreements(id,customer_id,partner_id,salesperson_user_id,version,previous_agreement_id,plan_type,tier_label,"
            "camera_tier,base_price_cents,upcharge_cents,customer_price_cents,activation_fee_cents,billing_party,commission_rate_bps,"
            "commission_max_paid_months,status,approved_at,approved_by,created_at,created_by,updated_at,updated_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (agreement_id, customer["id"], seller.get("partner_id"), seller["id"], version, (current or {}).get("id"),
             new["plan_type"], new["tier_label"], new["camera_tier"], new["base_price_cents"], new["upcharge_cents"],
             new["customer_price_cents"], new["activation_fee_cents"], billing_party, new["commission_rate_bps"],
             new["commission_max_paid_months"], PENDING, None, None, now, actor, now, actor))
        if admin:
            status = _supersede_and_inherit(db, current, agreement_id, actor, now) or APPROVED
            db.execute("UPDATE partner_agreements SET status=?,approved_at=?,approved_by=? WHERE id=?", (status, now, actor, agreement_id))
    created = row("SELECT * FROM partner_agreements WHERE id=?", (agreement_id,))
    audit(identity, "partner_agreement.approved" if admin else "partner_agreement.proposed", "partner_agreement", agreement_id,
          {"customer_id": customer["id"], "before": _money_view(current), "after": _money_view(created)})
    return created


def approve(identity: dict, agreement_id: str) -> dict:
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    agreement = get(identity, agreement_id)
    if agreement["status"] != PENDING:
        raise AgreementError("Only an agreement waiting for approval can be approved.", status=409)
    current = in_force(agreement["customer_id"])
    _check_change_allowed(current, agreement)
    now, actor = _now(), identity.get("email", "")
    with connection() as db:
        status = _supersede_and_inherit(db, current, agreement["id"], actor, now) or APPROVED
        db.execute("UPDATE partner_agreements SET status=?,approved_at=?,approved_by=?,updated_at=?,updated_by=? WHERE id=?",
                   (status, now, actor, now, actor, agreement["id"]))
    approved = row("SELECT * FROM partner_agreements WHERE id=?", (agreement["id"],))
    audit(identity, "partner_agreement.approved", "partner_agreement", agreement["id"],
          {"customer_id": agreement["customer_id"], "before": _money_view(current), "after": _money_view(approved)})
    return approved


def reject(identity: dict, agreement_id: str, reason: str) -> dict:
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    agreement = get(identity, agreement_id)
    if agreement["status"] != PENDING:
        raise AgreementError("Only an agreement waiting for approval can be rejected.", status=409)
    now = _now()
    with connection() as db:
        db.execute("UPDATE partner_agreements SET status=?,ended_at=?,end_reason=?,updated_at=?,updated_by=? WHERE id=?",
                   (REJECTED, now, str(reason or "")[:300], now, identity.get("email", ""), agreement["id"]))
    audit(identity, "partner_agreement.rejected", "partner_agreement", agreement["id"],
          {"customer_id": agreement["customer_id"], "reason": str(reason or "")[:300], "terms": _money_view(agreement)})
    return row("SELECT * FROM partner_agreements WHERE id=?", (agreement["id"],))


def cancel(identity: dict, agreement_id: str, reason: str) -> dict:
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    agreement = get(identity, agreement_id)
    if agreement["status"] not in (PENDING, *IN_FORCE):
        raise AgreementError("This agreement has already ended.", status=409)
    if agreement["status"] == ACTIVE and agreement["billing_party"] == pricing_catalog.BILLING_ANYAICAM and agreement.get("stripe_subscription_id"):
        raise AgreementError("This customer's AnyAiCam subscription is running. Cancel the subscription; the agreement ends with it.", status=409)
    now = _now()
    with connection() as db:
        db.execute("UPDATE partner_agreements SET status=?,ended_at=?,end_reason=?,updated_at=?,updated_by=? WHERE id=?",
                   (CANCELLED, now, str(reason or "")[:300], now, identity.get("email", ""), agreement["id"]))
    audit(identity, "partner_agreement.cancelled", "partner_agreement", agreement["id"],
          {"customer_id": agreement["customer_id"], "reason": str(reason or "")[:300], "terms": _money_view(agreement)})
    return row("SELECT * FROM partner_agreements WHERE id=?", (agreement["id"],))


# ------------------------------------------------------------ AnyAiCam billing (Stripe)

def checkout_lines(customer_id: str, plan_type: str, tier_label: str, *, first_index: int = 1) -> tuple[Optional[dict], list]:
    """Extra Checkout fields for a customer with an agreement in force, all
    from the stored agreement (never from the browser). The plan line itself
    stays the catalog Price that create_camera_slot_checkout() verifies
    against the catalog; the partner's upcharge (monthly) and activation fee
    (first invoice only) are inline price_data lines, so no Stripe Price
    object is created per partner markup. (None, []) without an agreement."""
    agreement = in_force(customer_id)
    if not agreement:
        return None, []
    if agreement["billing_party"] == pricing_catalog.BILLING_PARTNER:
        raise AgreementError("Your AnyAiCam service is billed by your partner. Contact them about your plan.", status=409)
    plan = pricing_catalog.find_base_plan(plan_type, tier_label)
    if (plan_type, tier_label) != (agreement["plan_type"], agreement["tier_label"]):
        agreed = pricing_catalog.find_base_plan(agreement["plan_type"], agreement["tier_label"])
        raise AgreementError(f"Your agreement with your partner is for the {agreed['label'] if agreed else 'agreed'} plan. "
                             "Choose that plan, or ask your partner to change the agreement.", status=409)
    if not plan or int(plan["monthly_cents"]) != int(agreement["base_price_cents"]):
        raise AgreementError("The AnyAiCam price for this plan changed after your agreement was made. "
                             "Your partner needs to renew the agreement before checkout.", status=409)
    partner = row("SELECT name FROM partners WHERE id=?", (agreement.get("partner_id") or "",))
    seller = (partner or {}).get("name") or "Partner"
    fields: list[tuple[str, str]] = []
    index = first_index
    lines = []
    if int(agreement["upcharge_cents"]) > 0:
        lines.append(("partner_upcharge", f"{seller} service (monthly)", int(agreement["upcharge_cents"]), "month"))
    if int(agreement["activation_fee_cents"]) > 0 and not agreement.get("activation_fee_collected_at"):
        lines.append(("partner_activation_fee", f"{seller} activation / setup fee", int(agreement["activation_fee_cents"]), None))
    for line_kind, name, cents, interval in lines:
        prefix = f"line_items[{index}]"
        fields += [(f"{prefix}[price_data][currency]", "usd"), (f"{prefix}[price_data][unit_amount]", str(cents)),
                   (f"{prefix}[price_data][product_data][name]", name),
                   (f"{prefix}[price_data][product_data][metadata][anyaicam_line]", line_kind),
                   (f"{prefix}[price_data][product_data][metadata][anyaicam_agreement_id]", agreement["id"]),
                   (f"{prefix}[quantity]", "1")]
        if interval:
            fields.append((f"{prefix}[price_data][recurring][interval]", interval))
        index += 1
    fields += [("metadata[anyaicam_agreement_id]", agreement["id"]),
               ("subscription_data[metadata][anyaicam_agreement_id]", agreement["id"])]
    return agreement, fields


def agreement_for_invoice(customer_id: str, subscription_id: str, metadata: dict) -> Optional[dict]:
    """The AnyAiCam-billed agreement a verified invoice belongs to: the one
    its subscription carries, or (after a change of version) the version now
    in force for the same customer and subscription."""
    agreement = None
    agreement_id = str((metadata or {}).get("anyaicam_agreement_id") or "")
    if agreement_id:
        agreement = row("SELECT * FROM partner_agreements WHERE id=?", (agreement_id,))
    if subscription_id and (not agreement or agreement["status"] not in IN_FORCE):
        agreement = row("SELECT * FROM partner_agreements WHERE stripe_subscription_id=? AND customer_id=? "
                        "ORDER BY version DESC LIMIT 1", (subscription_id, customer_id)) or agreement
    if agreement and agreement["status"] == SUPERSEDED:
        agreement = in_force(agreement["customer_id"]) or agreement
    if not agreement or agreement["customer_id"] != customer_id or agreement["billing_party"] != pricing_catalog.BILLING_ANYAICAM:
        return None
    return agreement


def note_payment(agreement: dict, *, paid_month_index: int, activation_fee_collected: bool, subscription_id: str) -> None:
    now = _now()
    with connection() as db:
        db.execute("UPDATE partner_agreements SET stripe_subscription_id=COALESCE(stripe_subscription_id,?),"
                   "status=CASE WHEN status=? THEN ? ELSE status END,updated_at=? WHERE id=?",
                   (subscription_id or None, APPROVED, ACTIVE, now, agreement["id"]))
        _note_residual_dates(db, agreement, paid_month_index, now)
        if activation_fee_collected:
            db.execute("UPDATE partner_agreements SET activation_fee_collected_at=COALESCE(activation_fee_collected_at,?) WHERE id=?",
                       (now, agreement["id"]))


def _note_residual_dates(db, agreement: dict, paid_month_index: int, now: str) -> None:
    if paid_month_index >= 1:
        db.execute("UPDATE partner_agreements SET commission_start_at=COALESCE(commission_start_at,?) WHERE id=?", (now, agreement["id"]))
    if paid_month_index >= int(agreement["commission_max_paid_months"]):
        db.execute("UPDATE partner_agreements SET commission_end_at=COALESCE(commission_end_at,?) WHERE id=?", (now, agreement["id"]))


def end_for_subscription(subscription_id: str, *, reason: str) -> bool:
    if not subscription_id:
        return False
    ended = rows(f"SELECT * FROM partner_agreements WHERE stripe_subscription_id=? AND status IN ({','.join('?' for _ in IN_FORCE)})",
                 (subscription_id, *IN_FORCE))
    now = _now()
    with connection() as db:
        for agreement in ended:
            db.execute("UPDATE partner_agreements SET status=?,ended_at=?,end_reason=?,updated_at=?,updated_by=? WHERE id=?",
                       (ENDED, now, reason, now, "stripe", agreement["id"]))
    for agreement in ended:
        audit({"email": "stripe", "role": "system"}, "partner_agreement.ended", "partner_agreement", agreement["id"],
              {"customer_id": agreement["customer_id"], "reason": reason, "stripe_subscription_id": subscription_id})
    return bool(ended)


# ------------------------------------------------------------ partner billing

def _month(value: str) -> str:
    month = str(value or "").strip()
    if not _MONTH.match(month):
        raise AgreementError("Enter the service month as YYYY-MM.")
    if month > datetime.now().strftime("%Y-%m"):
        raise AgreementError("A service month cannot be in the future.")
    return month


def _partner_billed(agreement: dict) -> None:
    if agreement["billing_party"] != pricing_catalog.BILLING_PARTNER:
        raise AgreementError("This customer is billed by AnyAiCam; payments are recorded from Stripe.", status=409)


def _attribution(agreement: dict) -> dict:
    return {"salesperson_user_id": agreement["salesperson_user_id"], "partner_id": agreement.get("partner_id")}


def record_collection(identity: dict, agreement_id: str, *, service_month: str) -> dict:
    """The partner billed its customer for a service month: the partner now
    owes AnyAiCam that month's agreed base price (a negative ledger row)."""
    _require_editor(identity)
    agreement = get(identity, agreement_id)
    _partner_billed(agreement)
    if agreement["status"] not in IN_FORCE:
        raise AgreementError("This agreement is not in force.", status=409)
    month = _month(service_month)
    from sales_commissions import _record
    entry = _record(kind=BASE_DUE, source_ref=f"due:{agreement['customer_id']}:{month}", customer_id=agreement["customer_id"],
                    attribution=_attribution(agreement), amount_cents=-int(agreement["base_price_cents"]),
                    basis_cents=int(agreement["base_price_cents"]), friends_family=False, agreement_id=agreement["id"],
                    billing_party=agreement["billing_party"], service_month=month, collected_cents=int(agreement["customer_price_cents"]),
                    collected_at=_now(), created_by=identity.get("email", ""), plan_type=agreement["plan_type"],
                    camera_slot_tier=int(agreement["camera_tier"]))
    if not entry:
        raise AgreementError(f"{month} is already recorded for this customer.", status=409)
    with connection() as db:
        db.execute("UPDATE partner_agreements SET status=?,updated_at=? WHERE id=? AND status=?", (ACTIVE, _now(), agreement["id"], APPROVED))
    audit(identity, "partner_agreement.collection_recorded", "partner_agreement", agreement["id"],
          {"customer_id": agreement["customer_id"], "service_month": month, "base_due_cents": int(agreement["base_price_cents"]),
           "customer_price_cents": int(agreement["customer_price_cents"])})
    return entry


def _net(entry: dict) -> int:
    from sales_commissions import net_amount
    return net_amount(entry)


def record_remittance(identity: dict, agreement_id: str, *, service_month: str, amount_cents, reference: str) -> dict:
    """AnyAiCam received base-price money from the partner for a service
    month. Only a global administrator records it (it is AnyAiCam's bank
    account). It clears that much of the month's amount owed, and the
    partner earns the 20% residual on it while within the 12 paid months."""
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    agreement = get(identity, agreement_id)
    _partner_billed(agreement)
    month = _month(service_month)
    reference = str(reference or "").strip()
    if not reference or len(reference) > 120:
        raise AgreementError("Enter the payment reference (for example the bank transfer number).")
    amount = _cents(amount_cents, "amount received")
    source_ref = f"remit:{agreement['customer_id']}:{reference}"
    if row("SELECT id FROM commission_ledger WHERE kind=? AND source_ref=?", (BASE_REMITTED, source_ref)):
        raise AgreementError("That payment reference is already recorded.", status=409)
    due = row("SELECT * FROM commission_ledger WHERE kind=? AND source_ref=?", (BASE_DUE, f"due:{agreement['customer_id']}:{month}"))
    if not due:
        raise AgreementError(f"Record the partner's collection for {month} first.", status=409)
    remitted = sum(_net(e) for e in rows("SELECT * FROM commission_ledger WHERE kind=? AND customer_id=? AND service_month=? "
                                         "AND reverses_entry_id IS NULL", (BASE_REMITTED, agreement["customer_id"], month)))
    outstanding = -_net(due) - remitted
    if amount <= 0 or amount > outstanding:
        raise AgreementError(f"The amount must be between $0.01 and the ${outstanding / 100:,.2f} still owed for {month}.")
    from sales_commissions import _ff_approved, _record
    common ={"agreement_id": agreement["id"], "billing_party": agreement["billing_party"], "service_month": month,
              "collected_cents": amount, "collected_at": _now(), "created_by": identity.get("email", ""),
              "plan_type": agreement["plan_type"], "camera_slot_tier": int(agreement["camera_tier"]), "note": reference}
    remittance = _record(kind=BASE_REMITTED, source_ref=source_ref, customer_id=agreement["customer_id"],
                         attribution=_attribution(agreement), amount_cents=amount, basis_cents=amount, friends_family=False, **common)
    if not remittance:
        raise AgreementError("That payment reference is already recorded.", status=409)
    # Paid months = service months with money actually kept (a bounced,
    # reversed remittance does not count), in service-month order.
    kept = rows("SELECT * FROM commission_ledger WHERE kind=? AND customer_id=? AND reverses_entry_id IS NULL",
                (BASE_REMITTED, agreement["customer_id"]))
    months = sorted({e["service_month"] for e in kept if _net(e) > 0})
    paid_index = months.index(month) + 1
    commission = None
    if paid_index <= int(agreement["commission_max_paid_months"]):
        rate = int(agreement["commission_rate_bps"])
        commission = _record(kind="recurring", source_ref=source_ref, customer_id=agreement["customer_id"],
                             attribution=_attribution(agreement), amount_cents=int(round(amount * rate / 10000)), basis_cents=amount,
                             friends_family=_ff_approved(agreement["customer_id"]), commission_rate_bps=rate,
                             paid_month_index=paid_index, **common)
    with connection() as db:
        _note_residual_dates(db, agreement, paid_index, _now())
    audit(identity, "partner_agreement.remittance_recorded", "partner_agreement", agreement["id"],
          {"customer_id": agreement["customer_id"], "service_month": month, "amount_cents": amount, "reference": reference,
           "residual_cents": (commission or {}).get("amount_cents"), "paid_month_index": paid_index})
    return {"remittance": remittance, "commission": commission, "paid_month_index": paid_index}


def reverse_partner_entry(identity: dict, entry_id: str, *, reason: str) -> list[dict]:
    """Undo a partner-billed record by appending reversals: a collection the
    partner refunded to its customer, or a remittance that bounced or was
    charged back (which also reverses the residual it earned)."""
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    reason = str(reason or "").strip()[:300]
    if not reason:
        raise AgreementError("Enter the reason.")
    entry = row("SELECT * FROM commission_ledger WHERE id=?", (str(entry_id or ""),))
    if not entry or entry["kind"] not in (BASE_DUE, BASE_REMITTED) or entry.get("reverses_entry_id"):
        raise LookupError("Partner-billed record not found.")
    from sales_commissions import reverse_entry
    targets = [entry] + (rows("SELECT * FROM commission_ledger WHERE kind='recurring' AND source_ref=? AND reverses_entry_id IS NULL",
                              (entry["source_ref"],)) if entry["kind"] == BASE_REMITTED else [])
    reversals = [r for r in (reverse_entry(t, remaining_cents=0, reason=reason, created_by=identity.get("email", "")) for t in targets) if r]
    if not reversals:
        raise AgreementError("This record is already reversed.", status=409)
    audit(identity, "partner_agreement.entry_reversed", "commission_ledger", entry["id"],
          {"customer_id": entry["customer_id"], "kind": entry["kind"], "reason": reason, "reversal_ids": [r["id"] for r in reversals]})
    return reversals


# ------------------------------------------------------------ partner-billed service

def _confirmed_remittances(customer_id: str) -> int:
    kept = rows("SELECT * FROM commission_ledger WHERE kind=? AND customer_id=? AND reverses_entry_id IS NULL",
                (BASE_REMITTED, customer_id))
    return sum(1 for e in kept if _net(e) > 0)


def activate_service(identity: dict, agreement_id: str) -> dict:
    """A partner-billed customer's camera capacity is granted only by a
    global administrator, and only after AnyAiCam has recorded receiving the
    partner's base payment -- never because the partner reports collecting
    from its customer (owner, 2026-10-04). The entitlement carries no Stripe
    reference, so Stripe synchronisation never touches it."""
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    agreement = get(identity, agreement_id)
    _partner_billed(agreement)
    if agreement["status"] not in IN_FORCE:
        raise AgreementError("This agreement is not in force.", status=409)
    if not _confirmed_remittances(agreement["customer_id"]):
        raise AgreementError("Record the base payment received from the partner before activating service.", status=409)
    from customer_entitlements import BASE_PLAN_PRODUCTS, get_entitlements_for_customer, upsert_entitlement
    product = f"camera_slots_{agreement['plan_type']}"
    for held in get_entitlements_for_customer(agreement["customer_id"]):
        if held["product"] in BASE_PLAN_PRODUCTS and held["status"] in ("active", "suspended") and held.get("stripe_subscription_id"):
            raise AgreementError("This customer already has an AnyAiCam-billed plan in Stripe. End it before partner billing.", status=409)
    upsert_entitlement(customer_id=agreement["customer_id"], product=product, camera_slot_quantity=int(agreement["camera_tier"]),
                       status="active")
    now = _now()
    with connection() as db:
        db.execute("UPDATE partner_agreements SET status=?,service_activated_at=?,service_activated_by=?,service_suspended_at=NULL,"
                   "service_suspended_by=NULL,updated_at=?,updated_by=? WHERE id=?",
                   (ACTIVE, now, identity.get("email", ""), now, identity.get("email", ""), agreement["id"]))
    audit(identity, "partner_agreement.service_activated", "partner_agreement", agreement["id"],
          {"customer_id": agreement["customer_id"], "product": product, "camera_slots": int(agreement["camera_tier"]),
           "confirmed_remittances": _confirmed_remittances(agreement["customer_id"])})
    return row("SELECT * FROM partner_agreements WHERE id=?", (agreement["id"],))


def suspend_service(identity: dict, agreement_id: str, reason: str) -> dict:
    """Suspend a partner-billed customer's service (for example the partner
    stopped paying). Global administrator only; recorded and reversible by
    activating again."""
    if not is_global_admin(identity):
        raise AgreementForbidden("Global administrator grant required.")
    agreement = get(identity, agreement_id)
    _partner_billed(agreement)
    reason = str(reason or "").strip()[:300]
    if not reason:
        raise AgreementError("Enter the reason.")
    if not agreement.get("service_activated_at") or agreement.get("service_suspended_at"):
        raise AgreementError("Service is not active for this customer.", status=409)
    from customer_entitlements import upsert_entitlement
    product = f"camera_slots_{agreement['plan_type']}"
    upsert_entitlement(customer_id=agreement["customer_id"], product=product, camera_slot_quantity=int(agreement["camera_tier"]),
                       status="suspended")
    now = _now()
    with connection() as db:
        db.execute("UPDATE partner_agreements SET service_suspended_at=?,service_suspended_by=?,updated_at=?,updated_by=? WHERE id=?",
                   (now, identity.get("email", ""), now, identity.get("email", ""), agreement["id"]))
    audit(identity, "partner_agreement.service_suspended", "partner_agreement", agreement["id"],
          {"customer_id": agreement["customer_id"], "product": product, "reason": reason})
    return row("SELECT * FROM partner_agreements WHERE id=?", (agreement["id"],))


# ------------------------------------------------------------ reporting

def financials(agreement: dict) -> dict:
    """Everything recorded for this customer's agreements (all versions)."""
    import partner_payouts
    entries = rows("SELECT l.*, i.payout_id FROM commission_ledger l LEFT JOIN partner_payout_items i ON i.ledger_entry_id=l.id "
                   "WHERE l.customer_id=? AND l.agreement_id IS NOT NULL ORDER BY l.earned_at", (agreement["customer_id"],))
    counted = [e for e in entries if e["status"] != "not_eligible_friends_family"]
    by_kind: dict[str, int] = {}
    for entry in counted:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + int(entry["amount_cents"])
    residual_months = len({e["paid_month_index"] for e in entries if e["kind"] == "recurring" and not e.get("reverses_entry_id")
                           and e.get("paid_month_index")})
    return {"entries": entries, "by_kind_cents": by_kind, "net_cents": sum(by_kind.values()),
            "unpaid_cents": sum(int(e["amount_cents"]) for e in counted if not e.get("payout_id")),
            "residual_paid_months": residual_months,
            "residual_months_remaining": max(0, int(agreement["commission_max_paid_months"]) - residual_months),
            "next_payout": partner_payouts.next_payout_for(agreement.get("partner_id"), agreement["salesperson_user_id"])}
