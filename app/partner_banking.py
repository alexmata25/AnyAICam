"""Payout bank accounts for partners (owner, 2026-10-04).

The payee -- the partner company, or an independent salesperson who belongs
to no partner company -- sets up the bank account for quarterly payouts on
the payout provider's own hosted pages (Stripe Connect Express onboarding).
The bank details are entered on, verified by and kept at the provider.
AnyAiCam stores only:
- the provider's account id;
- verification state (details submitted, payouts enabled, requirements
  outstanding, disabled reason) and a derived status;
- safe display information when the provider reports it: bank name, last
  four digits, currency;
- timestamps; every change is written to audit_logs.
Routing numbers and full account numbers are never requested, stored or
logged here (the provider's response is reduced to the fields above before
anything is saved).

Status: not_started -> onboarding (account created, form not finished) ->
verification_pending (submitted, provider still verifying) -> verified
(payouts enabled); restricted when the provider has disabled the account.
A statement is marked paid only to a verified payee (partner_payouts.py).

Nothing here moves money; payouts are still made and recorded by an
AnyAiCam administrator.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from partner_db import audit, connection, row, rows

PROVIDER = "stripe_connect"
NOT_STARTED, ONBOARDING, PENDING, VERIFIED, RESTRICTED = "not_started", "onboarding", "verification_pending", "verified", "restricted"
STATUS_LABELS = {NOT_STARTED: "Not set up", ONBOARDING: "Setup not finished", PENDING: "Being verified",
                 VERIFIED: "Verified", RESTRICTED: "Action needed"}
SAFE_FIELDS = ("payee_key", "provider", "status", "bank_name", "last4", "currency", "details_submitted", "payouts_enabled",
               "requirements_due", "disabled_reason", "last_synced_at", "created_at", "updated_at")


class BankingError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now().isoformat()


def payee_of(identity: dict) -> dict:
    """The payee this signed-in user manages banking for."""
    from partner_agreements import AgreementForbidden
    me = row("SELECT id,partner_id,role FROM partner_users WHERE lower(email)=lower(?)", (identity.get("email", ""),))
    role = identity.get("role")
    if role in ("partner_owner", "administrator") and identity.get("partner_id"):
        return {"payee_key": identity["partner_id"], "partner_id": identity["partner_id"], "salesperson_user_id": None}
    if role == "salesperson" and me and not me.get("partner_id"):
        return {"payee_key": f"user:{me['id']}", "partner_id": None, "salesperson_user_id": me["id"]}
    if role == "salesperson":
        raise AgreementForbidden("Payouts go to your partner company; its owner sets up the bank account.")
    raise AgreementForbidden("Only the partner owner or an independent salesperson sets up a payout bank account.")


def account(payee_key: str) -> Optional[dict]:
    return row("SELECT * FROM partner_payout_accounts WHERE payee_key=?", (payee_key,))


def public_view(record: Optional[dict], payee_key: str = "") -> dict:
    if not record:
        return {"payee_key": payee_key, "provider": PROVIDER, "status": NOT_STARTED, "status_label": STATUS_LABELS[NOT_STARTED]}
    view = {k: record.get(k) for k in SAFE_FIELDS}
    view["status_label"] = STATUS_LABELS.get(record["status"], record["status"])
    return view


def is_verified(payee_key: str) -> bool:
    record = account(payee_key)
    return bool(record) and record["status"] == VERIFIED


def _stripe_post(path: str, fields: list, idempotency_key: Optional[str] = None) -> dict:
    import main
    try:
        return main.stripe_api_post(path, fields, idempotency_key=idempotency_key)
    except Exception as error:
        raise BankingError("Bank account setup is not available right now. Try again later or contact AnyAiCam.", status=503) from error


def _stripe_get(path: str) -> dict:
    import main
    try:
        return main.stripe_api_get(path)
    except Exception as error:
        raise BankingError("The payout provider could not be reached. Try again later.", status=503) from error


def _derive(provider_account: dict) -> dict:
    """Reduce the provider's account object to the safe fields we keep."""
    requirements = provider_account.get("requirements") or {}
    due = len(requirements.get("currently_due") or []) + len(requirements.get("past_due") or [])
    banks = [b for b in ((provider_account.get("external_accounts") or {}).get("data") or [])
             if isinstance(b, dict) and b.get("object", "bank_account") == "bank_account"]
    bank = next((b for b in banks if b.get("default_for_currency")), banks[0] if banks else {})
    submitted, enabled = bool(provider_account.get("details_submitted")), bool(provider_account.get("payouts_enabled"))
    disabled = requirements.get("disabled_reason") or None
    if enabled and bank:
        status = VERIFIED
    elif disabled and submitted:
        status = RESTRICTED
    elif submitted:
        status = PENDING
    else:
        status = ONBOARDING
    last4 = str(bank.get("last4") or "")[-4:] or None
    return {"status": status, "bank_name": (str(bank.get("bank_name") or "")[:80] or None), "last4": last4,
            "currency": (str(bank.get("currency") or "")[:3].lower() or None), "details_submitted": int(submitted),
            "payouts_enabled": int(enabled), "requirements_due": due, "disabled_reason": (str(disabled)[:80] if disabled else None)}


def start_onboarding(identity: dict, *, return_url: str, refresh_url: str) -> str:
    """Create (once) the payee's provider account and return a one-time link
    to the provider's hosted onboarding, where the bank details are entered."""
    payee = payee_of(identity)
    record = account(payee["payee_key"])
    now, actor = _now(), identity.get("email", "")
    if not record or not record.get("provider_account_id"):
        created = _stripe_post("/v1/accounts", [
            ("type", "express"), ("country", "US"), ("capabilities[transfers][requested]", "true"),
            ("email", identity.get("email", "")), ("metadata[anyaicam_payee_key]", payee["payee_key"]),
        ], idempotency_key=f"anyaicam-payout-account-{payee['payee_key']}")
        provider_account_id = str(created.get("id") or "")
        if not provider_account_id.startswith("acct_"):
            raise BankingError("The payout provider did not create an account. Try again later.", status=502)
        with connection() as db:
            db.execute("INSERT INTO partner_payout_accounts(payee_key,partner_id,salesperson_user_id,provider,provider_account_id,status,"
                       "created_at,created_by,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(payee_key) DO UPDATE SET provider_account_id=excluded.provider_account_id,status=excluded.status,"
                       "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                       (payee["payee_key"], payee["partner_id"], payee["salesperson_user_id"], PROVIDER, provider_account_id,
                        ONBOARDING, now, actor, now, actor))
        audit(identity, "payout_account.created", "partner_payout_account", payee["payee_key"],
              {"provider": PROVIDER, "provider_account_id": provider_account_id})
        record = account(payee["payee_key"])
    link = _stripe_post("/v1/account_links", [
        ("account", record["provider_account_id"]), ("type", "account_onboarding"),
        ("return_url", return_url), ("refresh_url", refresh_url),
    ])
    url = str(link.get("url") or "")
    if not url.startswith("https://"):
        raise BankingError("The payout provider did not return a setup link. Try again later.", status=502)
    audit(identity, "payout_account.onboarding_started", "partner_payout_account", payee["payee_key"], {"provider": PROVIDER})
    return url


def refresh(identity: dict, payee_key: Optional[str] = None) -> dict:
    """Re-read the payee's account from the provider and keep only the safe
    fields. The payee refreshes its own; a global administrator any."""
    from partner_agreements import is_global_admin
    if payee_key and is_global_admin(identity):
        key = payee_key
    else:
        key = payee_of(identity)["payee_key"]
    record = account(key)
    if not record or not record.get("provider_account_id"):
        return public_view(None, key)
    provider_account = _stripe_get(f"/v1/accounts/{record['provider_account_id']}")
    if not ((provider_account.get("external_accounts") or {}).get("data")):
        try:  # not every account read includes the bank: list it (still reduced to safe fields)
            provider_account["external_accounts"] = _stripe_get(
                f"/v1/accounts/{record['provider_account_id']}/external_accounts?object=bank_account&limit=3")
        except BankingError:
            pass
    derived = _derive(provider_account)
    now = _now()
    with connection() as db:
        db.execute("UPDATE partner_payout_accounts SET status=?,bank_name=?,last4=?,currency=?,details_submitted=?,payouts_enabled=?,"
                   "requirements_due=?,disabled_reason=?,last_synced_at=?,updated_at=?,updated_by=? WHERE payee_key=?",
                   (derived["status"], derived["bank_name"], derived["last4"], derived["currency"], derived["details_submitted"],
                    derived["payouts_enabled"], derived["requirements_due"], derived["disabled_reason"], now, now,
                    identity.get("email", ""), key))
    if derived["status"] != record["status"] or derived["last4"] != record.get("last4"):
        audit(identity, "payout_account.status_changed", "partner_payout_account", key,
              {"from": record["status"], "to": derived["status"], "bank_name": derived["bank_name"], "last4": derived["last4"]})
    return public_view(account(key))


def all_accounts() -> list[dict]:
    return [public_view(r) for r in rows("SELECT * FROM partner_payout_accounts ORDER BY payee_key")]
