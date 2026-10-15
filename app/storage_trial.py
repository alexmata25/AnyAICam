"""30-day free cloud-storage trial (owner request, 2026-10-15).

A customer evaluation feature, not a discount system:

* Scope: the cloud-storage plan only -- a per-camera plan whose PLANS entry
  has cloud_event_storage (Hybrid). Hardware, appliances, relays, the VMS
  software license, analytics packages, Talk Down and every other product
  keep their normal prices; none of their checkouts read this module.
* Granted only by a platform administrator (website_partner.
  _require_global_admin), with a reason. One trial per customer: a second
  one needs an explicit administrator re-grant approval with its own reason.
  Every grant, re-grant, cancellation and paid authorization is audited
  (audit_logs).
* The trial is 30 days from the moment the administrator activates it
  (starts_at .. expires_at). Expiry is derived from expires_at when read --
  no cron is needed -- and Stripe ends the subscription itself.
* Mechanism: Stripe's native subscription trial, never a 100% coupon. A
  storage subscription created while the trial is active carries
  trial_end = expires_at (so the free period can never exceed the granted
  30 days) and trial_settings[end_behavior][missing_payment_method]=cancel.
  Checkout uses payment_method_collection=if_required, so no card is asked
  for during the trial.
* Customer protection: nobody is charged when the trial ends unless they
  explicitly chose paid storage. Every trial subscription is set to
  cancel_at_period_end=true (directly when the server creates it; by the
  verified webhook right after a Checkout creates it), so it ends at the
  trial end even if a card happens to be on file. Only the customer
  owner's explicit "Continue cloud storage after my trial" action
  (authorize_paid) -- or an explicit un-cancel by a person in Stripe --
  lets it renew as a paid plan.
* Friends & Family stays separate and unchanged: the trial comes first, and
  an approved F&F coupon on the same subscription applies to the paid plan
  after conversion. The F&F coupon policy (50% / 25%) is never altered.
* Not offered on the mixed own-PC checkout (plan + one-time VMS license in
  one Checkout Session): whether Stripe charges a one-time line during a
  trial was not verified against live Stripe, and the license must never
  become free. Those customers pay the normal price at checkout.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from partner_db import audit, connection, row, rows

logger = logging.getLogger("anyaicam.storage_trial")

TRIAL_DAYS = 30
ACTIVE, EXPIRED, CANCELLED, CONVERTED = "active", "expired", "cancelled", "converted"
# Stripe Checkout requires subscription_data[trial_end] at least 48 hours
# ahead; the server-created (appliance) subscription only needs it ahead.
CHECKOUT_MIN_REMAINING = timedelta(hours=49)
SUBSCRIPTION_MIN_REMAINING = timedelta(hours=1)
METADATA_KEY = "anyaicam_storage_trial"
_GUARD_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded",
                 "customer.subscription.created", "customer.subscription.updated"}
_SYSTEM = {"email": "stripe-webhook", "role": "system"}
MAX_TEXT = 500


class TrialError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


# ------------------------------------------------------------------ time

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ records

def effective_status(trial: dict | None) -> str | None:
    """The stored status, except that an active trial past expires_at is
    expired -- whether or not anything has written that down yet."""
    if not trial:
        return None
    if trial["status"] == ACTIVE:
        expires = _parse(trial.get("expires_at"))
        if not expires or _now() >= expires:
            return EXPIRED
    return trial["status"]


def _with_effective(trial: dict | None) -> dict | None:
    if trial:
        trial = dict(trial, effective_status=effective_status(trial))
    return trial


def trials_for(customer_id: str) -> list[dict]:
    return [_with_effective(t) for t in rows(
        "SELECT * FROM storage_trials WHERE customer_id=? ORDER BY granted_at DESC, id", (customer_id,))]


def latest(customer_id: str) -> dict | None:
    found = trials_for(customer_id)
    return found[0] if found else None


def active_trial(customer_id: str) -> dict | None:
    trial = latest(customer_id)
    return trial if trial and trial["effective_status"] == ACTIVE else None


def by_subscription(subscription_id: str) -> dict | None:
    if not subscription_id:
        return None
    return _with_effective(row("SELECT * FROM storage_trials WHERE stripe_subscription_id=? "
                               "ORDER BY granted_at DESC LIMIT 1", (subscription_id,)))


def _expire_lapsed(db, customer_id: str) -> None:
    now = _iso(_now())
    db.execute("UPDATE storage_trials SET status=?,ended_at=COALESCE(ended_at,expires_at),updated_at=? "
               "WHERE customer_id=? AND status=? AND expires_at<=?", (EXPIRED, now, customer_id, ACTIVE, now))


def is_storage_plan(plan_key: str) -> bool:
    import per_camera_billing as billing
    return bool(billing.PLANS.get(plan_key, {}).get("cloud_event_storage"))


# ------------------------------------------------------------------ administrator actions

def _text(value, label: str, *, required: bool) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise TrialError(400, f"{label} is required.")
    if len(text) > MAX_TEXT:
        raise TrialError(400, f"{label} must be at most {MAX_TEXT} characters.")
    return text


def grant(customer_id: str, *, admin: dict, reason: str, allow_repeat: bool = False, repeat_reason: str = "") -> dict:
    """Activate the 30-day trial for an eligible customer (administrator)."""
    import per_camera_billing as billing
    customer_id = str(customer_id or "").strip()
    reason = _text(reason, "A reason", required=True)
    if not customer_id or not row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        raise TrialError(404, "Customer not found.")
    if billing.has_camera_plan(customer_id):
        raise TrialError(409, "This customer already has a camera plan. The free storage trial is for customers evaluating "
                              "cloud storage before buying.")
    previous = latest(customer_id)
    if previous and previous["effective_status"] == ACTIVE:
        raise TrialError(409, "This customer already has an active free storage trial.")
    if previous and not allow_repeat:
        raise TrialError(409, "This customer already had a free storage trial. A second trial needs an explicit "
                              "administrator re-grant approval (allow_repeat) with its own reason.")
    repeat_reason = _text(repeat_reason, "The re-grant approval reason", required=bool(previous)) if previous else ""
    now = _now()
    trial = {"id": uuid.uuid4().hex, "customer_id": customer_id, "status": ACTIVE, "starts_at": _iso(now),
             "expires_at": _iso(now + timedelta(days=TRIAL_DAYS)), "granted_by": admin.get("email") or "",
             "granted_at": _iso(now), "reason": reason, "repeat_of": previous["id"] if previous else None,
             "repeat_approved_by": (admin.get("email") or "") if previous else None,
             "repeat_reason": repeat_reason or None, "updated_at": _iso(now)}
    try:
        with connection() as db:
            _expire_lapsed(db, customer_id)
            db.execute(f"INSERT INTO storage_trials({','.join(trial)}) VALUES({','.join('?' * len(trial))})",
                       tuple(trial.values()))
    except Exception as error:  # the one-active-trial unique index: a concurrent grant won
        if "unique" in str(error).lower():
            raise TrialError(409, "This customer already has an active free storage trial.")
        raise
    audit(admin, "storage_trial.regrant" if previous else "storage_trial.grant", "customer", customer_id,
          {"trial_id": trial["id"], "starts_at": trial["starts_at"], "expires_at": trial["expires_at"], "reason": reason,
           "repeat_of": trial["repeat_of"], "repeat_reason": trial["repeat_reason"]})
    return _with_effective(trial)


def cancel(trial_id: str, *, admin: dict, reason: str) -> dict:
    """Revoke a trial (administrator). Its free storage ends now: a trial
    subscription still in its trial is cancelled on Stripe immediately, which
    never charges the customer."""
    reason = _text(reason, "A reason", required=True)
    trial = _with_effective(row("SELECT * FROM storage_trials WHERE id=?", (trial_id,)))
    if not trial:
        raise TrialError(404, "Trial not found.")
    if trial["status"] in (CANCELLED, CONVERTED):
        raise TrialError(409, f"This trial is already {trial['status']}.")
    subscription_status = None
    if trial.get("stripe_subscription_id"):
        import main
        import stripe_state
        current = stripe_state.current_subscription(trial["stripe_subscription_id"])
        subscription_status = str(current.get("status") or "")
        if subscription_status == "trialing":
            main.stripe_api_delete(f"/v1/subscriptions/{trial['stripe_subscription_id']}")
        elif subscription_status not in stripe_state.ENDED_STATUSES:
            raise TrialError(409, "This trial's subscription is already a paid plan; manage it as a normal subscription.")
    now = _iso(_now())
    with connection() as db:
        db.execute("UPDATE storage_trials SET status=?,cancelled_by=?,cancel_reason=?,ended_at=COALESCE(ended_at,?),updated_at=? "
                   "WHERE id=?", (CANCELLED, admin.get("email") or "", reason, now, now, trial_id))
    audit(admin, "storage_trial.cancel", "customer", trial["customer_id"],
          {"trial_id": trial_id, "reason": reason, "previous_status": trial["effective_status"],
           "stripe_subscription_id": trial.get("stripe_subscription_id"), "subscription_status": subscription_status})
    return _with_effective(row("SELECT * FROM storage_trials WHERE id=?", (trial_id,)))


# ------------------------------------------------------------------ Stripe request fields

def _usable(customer_id: str, plan_key: str, minimum: timedelta) -> dict | None:
    if not is_storage_plan(plan_key):
        return None
    trial = active_trial(customer_id)
    if not trial or trial.get("stripe_subscription_id"):
        return None  # one trial subscription per trial
    if _parse(trial["expires_at"]) - _now() < minimum:
        return None  # too little of the window left for Stripe to accept a trial
    return trial


def _trial_end(trial: dict) -> str:
    return str(int(_parse(trial["expires_at"]).timestamp()))


def subscription_trial_fields(customer_id: str, *, checkout: bool, plan_key: str) -> list[tuple[str, str]]:
    """THE hook every storage-subscription start calls (plan Checkout,
    Build Your System plan order, appliance activation, and any later
    storage start such as a hardware-first per-camera Checkout).

    Returns the Stripe fields to append to the request, or [] when nothing
    applies -- no active administrator-granted trial, a plan without cloud
    storage, a trial already used by a subscription, or too little of the
    window left for Stripe to accept it. [] means the request is exactly as
    it would be without this feature.

    checkout=True (POST /v1/checkout/sessions, mode=subscription, storage
    plan line only -- never a session with one-time hardware/license lines):
      subscription_data[trial_end]=<expires_at epoch>,
      subscription_data[trial_settings][end_behavior][missing_payment_method]=cancel,
      payment_method_collection=if_required,
      metadata[anyaicam_storage_trial] and subscription_data[metadata][anyaicam_storage_trial]=<trial id>.
      The verified webhook (storage_trials step) then sets the new trial
      subscription to cancel_at_period_end=true.
    checkout=False (POST /v1/subscriptions, server-created):
      trial_end, trial_settings[end_behavior][missing_payment_method]=cancel,
      cancel_at_period_end=true, metadata[anyaicam_storage_trial]=<trial id>.
      The caller must not attach default_payment_method when this is non-empty,
      and should call link_subscription() with the created subscription id
      (the webhook links it too).
    """
    trial = _usable(customer_id, plan_key, CHECKOUT_MIN_REMAINING if checkout else SUBSCRIPTION_MIN_REMAINING)
    if not trial:
        return []
    if checkout:
        return [("subscription_data[trial_end]", _trial_end(trial)),
                ("subscription_data[trial_settings][end_behavior][missing_payment_method]", "cancel"),
                ("payment_method_collection", "if_required"),
                (f"metadata[{METADATA_KEY}]", trial["id"]),
                (f"subscription_data[metadata][{METADATA_KEY}]", trial["id"])]
    return [("trial_end", _trial_end(trial)),
            ("trial_settings[end_behavior][missing_payment_method]", "cancel"),
            ("cancel_at_period_end", "true"),
            (f"metadata[{METADATA_KEY}]", trial["id"])]


def link_subscription(trial_id: str, customer_id: str, subscription_id: str) -> None:
    if not trial_id or not subscription_id:
        return
    with connection() as db:
        db.execute("UPDATE storage_trials SET stripe_subscription_id=?,updated_at=? "
                   "WHERE id=? AND customer_id=? AND stripe_subscription_id IS NULL",
                   (subscription_id, _iso(_now()), trial_id, customer_id))


# ------------------------------------------------------------------ explicit paid authorization

def authorize_paid(identity: dict) -> dict:
    """The customer owner explicitly chooses paid cloud storage after the
    trial: the trial subscription stops being set to end at the trial end
    and renews on the customer's own payment method."""
    import main
    import per_camera_billing as billing
    import stripe_state
    customer_id = identity["customer_id"]
    entitlement = billing.entitlement_for_customer(customer_id)
    trial = by_subscription(str((entitlement or {}).get("stripe_subscription_id") or ""))
    if not trial or trial["customer_id"] != customer_id or trial["effective_status"] != ACTIVE:
        raise TrialError(409, "There is no free storage trial in progress on this account.")
    if trial.get("paid_authorized_at"):
        return {"status": "already_authorized", "expires_at": trial["expires_at"]}
    current = stripe_state.current_subscription(trial["stripe_subscription_id"])
    if str(current.get("status") or "") != "trialing":
        raise TrialError(409, "This subscription is no longer in its free trial.")
    method = _id(current.get("default_payment_method"))
    fields = [("cancel_at_period_end", "false")]
    if not method:
        stripe_customer = _id(current.get("customer"))
        found = main.stripe_api_get(f"/v1/customers/{stripe_customer}") if stripe_customer else {}
        method = _id((found.get("invoice_settings") or {}).get("default_payment_method"))
        if not method:
            deferred = billing.deferred_storage_plan(customer_id)
            method = (deferred or {}).get("stripe_payment_method_id")
        if not method:
            raise TrialError(409, "Add a payment method in Manage billing first, then choose to continue cloud storage "
                                  "after your trial.")
        fields.append(("default_payment_method", method))
    main.stripe_api_post(f"/v1/subscriptions/{trial['stripe_subscription_id']}", fields,
                         idempotency_key=f"anyaicam-storage-trial-authorize-{trial['stripe_subscription_id']}")
    now = _iso(_now())
    with connection() as db:
        db.execute("UPDATE storage_trials SET paid_authorized_at=?,paid_authorized_by=?,updated_at=? WHERE id=?",
                   (now, identity.get("email") or "", now, trial["id"]))
    audit(identity, "storage_trial.paid_authorized", "customer", customer_id,
          {"trial_id": trial["id"], "stripe_subscription_id": trial["stripe_subscription_id"], "starts_paid_at": trial["expires_at"]})
    try:
        billing._upsert_current(stripe_state.current_subscription(trial["stripe_subscription_id"]))
    except Exception:
        logger.info("storage_trial.entitlement_waits_for_webhook customer_id=%s", customer_id)
    return {"status": "authorized", "expires_at": trial["expires_at"]}


def _id(value) -> str:
    return str((value.get("id") if isinstance(value, dict) else value) or "")


# ------------------------------------------------------------------ verified webhook step

def _subscription_id(event_type: str, obj: dict) -> str:
    if event_type.startswith("customer.subscription."):
        return _id(obj.get("id"))
    if event_type.startswith("checkout.session."):
        return _id(obj.get("subscription"))
    if event_type.startswith("invoice."):
        details = obj.get("subscription_details") or ((obj.get("parent") or {}).get("subscription_details")) or {}
        return _id(obj.get("subscription") or details.get("subscription"))
    return ""


def _invoice_paid(subscription: dict) -> bool:
    import stripe_state
    invoice = subscription.get("latest_invoice")
    if not isinstance(invoice, dict):
        invoice_id = _id(invoice)
        if not invoice_id:
            return False
        invoice = stripe_state.stripe_get(f"/v1/invoices/{invoice_id}", "the subscription's invoice")
    return str(invoice.get("status") or "") == "paid"


def sync_from_stripe_event(event: dict) -> dict:
    """Keeps trial records in step with Stripe and enforces the no-charge
    guard. Acts only on trial subscriptions; everything else is ignored
    without a Stripe call. Errors propagate so Stripe retries the event."""
    import main
    import stripe_state
    event_type = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    subscription_id = _subscription_id(event_type, obj)
    if not subscription_id:
        return {"status": "ignored"}
    trial_id = str((obj.get("metadata") or {}).get(METADATA_KEY) or "") if not event_type.startswith("invoice.") else ""
    if not trial_id and not by_subscription(subscription_id):
        return {"status": "ignored"}
    current = stripe_state.current_subscription(subscription_id)
    metadata = current.get("metadata") or {}
    trial_id = str(metadata.get(METADATA_KEY) or trial_id)
    trial = _with_effective(row("SELECT * FROM storage_trials WHERE id=?", (trial_id,))) if trial_id else by_subscription(subscription_id)
    if not trial or str(metadata.get("anyaicam_customer_id") or trial["customer_id"]) != trial["customer_id"]:
        return {"status": "ignored", "reason": "not a trial subscription of this account"}
    link_subscription(trial["id"], trial["customer_id"], subscription_id)
    trial = _with_effective(row("SELECT * FROM storage_trials WHERE id=?", (trial["id"],)))
    status = str(current.get("status") or "")
    bound = trial.get("stripe_subscription_id") == subscription_id
    if status == "trialing":
        if trial["status"] == CANCELLED:
            main.stripe_api_delete(f"/v1/subscriptions/{subscription_id}")
            return {"status": "cancelled_on_stripe", "reason": "the trial was revoked by an administrator"}
        authorized = bound and trial.get("paid_authorized_at")
        if event_type in _GUARD_EVENTS and not authorized and not current.get("cancel_at_period_end"):
            if event_type == "customer.subscription.updated" and (
                    ((event.get("data") or {}).get("previous_attributes") or {}).get("cancel_at_period_end") is True):
                # A person un-cancelled it in Stripe (customer portal or
                # dashboard): an explicit choice of paid storage.
                now = _iso(_now())
                with connection() as db:
                    db.execute("UPDATE storage_trials SET paid_authorized_at=COALESCE(paid_authorized_at,?),"
                               "paid_authorized_by=COALESCE(paid_authorized_by,'stripe'),updated_at=? WHERE id=?",
                               (now, now, trial["id"]))
                audit(_SYSTEM, "storage_trial.paid_authorized", "customer", trial["customer_id"],
                      {"trial_id": trial["id"], "stripe_subscription_id": subscription_id, "via": "stripe"})
                return {"status": "paid_authorized_in_stripe"}
            main.stripe_api_post(f"/v1/subscriptions/{subscription_id}", [("cancel_at_period_end", "true")],
                                 idempotency_key=f"anyaicam-storage-trial-guard-{subscription_id}")
            return {"status": "guarded", "subscription_id": subscription_id}
        return {"status": "trialing"}
    if not bound:
        return {"status": "ignored", "reason": "another subscription holds this trial"}
    now = _iso(_now())
    if status == "active" and trial["status"] in (ACTIVE, EXPIRED) and _invoice_paid(current):
        with connection() as db:
            db.execute("UPDATE storage_trials SET status=?,converted_at=?,ended_at=COALESCE(ended_at,?),updated_at=? WHERE id=?",
                       (CONVERTED, now, trial["expires_at"], now, trial["id"]))
        audit(_SYSTEM, "storage_trial.converted", "customer", trial["customer_id"],
              {"trial_id": trial["id"], "stripe_subscription_id": subscription_id})
        return {"status": CONVERTED}
    if status in stripe_state.ENDED_STATUSES and trial["status"] == ACTIVE:
        ended = current.get("ended_at") or current.get("canceled_at")
        expires = _parse(trial["expires_at"])
        at_trial_end = not ended or datetime.fromtimestamp(int(ended), tz=timezone.utc) >= expires - timedelta(minutes=5)
        outcome = EXPIRED if at_trial_end else CANCELLED
        with connection() as db:
            db.execute("UPDATE storage_trials SET status=?,ended_at=?,cancelled_by=CASE WHEN ?='cancelled' THEN 'customer' "
                       "ELSE cancelled_by END,updated_at=? WHERE id=? AND status=?",
                       (outcome, now, outcome, now, trial["id"], ACTIVE))
        audit(_SYSTEM, f"storage_trial.{outcome}", "customer", trial["customer_id"],
              {"trial_id": trial["id"], "stripe_subscription_id": subscription_id})
        return {"status": outcome}
    return {"status": "unchanged"}


# ------------------------------------------------------------------ customer view

def customer_view(customer_id: str) -> dict | None:
    trial = latest(customer_id)
    if not trial:
        return None
    return {"status": trial["effective_status"], "starts_at": trial["starts_at"], "expires_at": trial["expires_at"],
            "applies_to": "Cloud storage plan (Hybrid) only", "subscription_started": bool(trial.get("stripe_subscription_id")),
            "paid_after_trial": bool(trial.get("paid_authorized_at"))}


def customer_note_html(customer_id: str, is_owner: bool) -> str:
    try:
        trial = active_trial(customer_id)
    except Exception:
        return ""  # a database before the storage_trials migration
    if not trial:
        return ""
    until = escape(_parse(trial["expires_at"]).strftime("%b %d, %Y"))
    note = (f'<p class="health-detail" id="storage-trial-note"><strong>Free cloud storage trial until '
            f'<time datetime="{escape(trial["expires_at"], quote=True)}" data-local-date>{until}</time>.</strong> '
            'Hardware, software licenses and add-ons are charged at their normal prices. ')
    if trial.get("paid_authorized_at"):
        return note + 'You chose to continue cloud storage as a paid plan after the trial.</p>'
    if not trial.get("stripe_subscription_id"):
        return note + 'It applies when you start the Hybrid cloud storage plan before it ends.</p>'
    note += 'Your storage plan ends with the trial and you will not be charged unless you choose to continue.</p>'
    if is_owner:
        note += ('<button class="ghost-button" id="storage-trial-continue" type="button">Continue cloud storage after my trial</button>'
                 '<script>(()=>{const b=document.getElementById("storage-trial-continue");if(!b)return;b.onclick=async()=>{'
                 'if(!confirm("After your free trial ends, your cloud storage plan will renew at its normal price on your '
                 'payment method. Continue?"))return;b.disabled=true;const r=await fetch("/api/v2/customer/storage-trial/continue-paid",'
                 '{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({authorize_paid_storage:true})});'
                 'const d=await r.json().catch(()=>({}));const m=document.getElementById("v2-message");if(m)m.textContent=r.ok?'
                 '"Your cloud storage will continue as a paid plan after the trial.":(d.detail||"That did not work.");'
                 'if(r.ok)setTimeout(()=>location.reload(),1200);else b.disabled=false;};})();</script>')
    return note


# ------------------------------------------------------------------ routes

def _admin_page(items: list[dict]) -> str:
    body = "".join(
        f'<tr><td>{escape(t.get("customer_name") or t["customer_id"])}<br><small>{escape(t["customer_id"])}</small></td>'
        f'<td>{escape(t["effective_status"])}</td><td>{escape(t["starts_at"][:10])}</td><td>{escape(t["expires_at"][:10])}</td>'
        f'<td>{escape(t["granted_by"])}</td><td>{escape(t["reason"])}{"<br><small>Re-grant: " + escape(t.get("repeat_reason") or "") + "</small>" if t.get("repeat_of") else ""}</td>'
        f'<td>{escape(t.get("stripe_subscription_id") or "")}</td>'
        f'<td>{"" if t["status"] in (CANCELLED, CONVERTED) else "<button class=ghost-button data-cancel=" + escape(t["id"], quote=True) + ">Cancel</button>"}</td></tr>'
        for t in items) or '<tr><td colspan="8">No storage trials yet.</td></tr>'
    return f'''<header class="topbar"><div><p class="eyebrow">Administrator</p><h1>Free storage trials</h1></div></header>
<section class="panel"><h2>Activate a 30-day free storage trial</h2>
<p class="health-detail">Cloud storage plan only (Hybrid). Hardware, licenses and add-ons stay at normal prices. The customer is never
charged after the trial unless they choose to continue. One trial per customer; a second needs a re-grant approval reason.</p>
<label>Customer ID <input id="st-customer" maxlength="80"></label> <label>Reason <input id="st-reason" maxlength="500"></label>
<label><input type="checkbox" id="st-repeat"> Approve a repeat trial</label> <label>Re-grant reason <input id="st-repeat-reason" maxlength="500"></label>
<button class="action-button" id="st-grant" type="button">Activate trial</button><p class="health-detail" id="st-result" role="status"></p></section>
<section class="panel" style="margin-top:14px"><table><tr><th>Customer</th><th>Status</th><th>Starts</th><th>Expires</th><th>Granted by</th>
<th>Reason</th><th>Subscription</th><th></th></tr>{body}</table></section>
<script>
const out=document.getElementById('st-result');
document.getElementById('st-grant').onclick=async()=>{{const r=await fetch('/api/admin/storage-trials',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{customer_id:document.getElementById('st-customer').value,reason:document.getElementById('st-reason').value,allow_repeat:document.getElementById('st-repeat').checked,repeat_reason:document.getElementById('st-repeat-reason').value}})}});const d=await r.json().catch(()=>({{}}));out.textContent=r.ok?'Trial active until '+d.expires_at:(d.detail||'Could not activate the trial.');if(r.ok)setTimeout(()=>location.reload(),900);}};
document.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{{const reason=prompt('Reason for cancelling this trial?');if(!reason)return;const r=await fetch('/api/admin/storage-trials/'+b.dataset.cancel+'/cancel',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{reason}})}});const d=await r.json().catch(()=>({{}}));out.textContent=r.ok?'Trial cancelled.':(d.detail||'Could not cancel the trial.');if(r.ok)setTimeout(()=>location.reload(),900);}});
</script>'''


def register_routes(app, shell) -> None:
    from partner_portal import partner_identity

    def administrator(request: Request) -> dict:
        from website_partner import _require_global_admin
        return _require_global_admin(request)

    async def _payload(request: Request) -> dict:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _raise(error: TrialError):
        raise HTTPException(status_code=error.status_code, detail=error.detail)

    @app.get("/admin/storage-trials", response_class=HTMLResponse)
    def storage_trials_admin_page(request: Request):
        administrator(request)
        items = [_with_effective(t) for t in rows(
            "SELECT t.*, c.name AS customer_name FROM storage_trials t LEFT JOIN customers c ON c.id=t.customer_id "
            "ORDER BY t.granted_at DESC LIMIT 200")]
        return shell("Free storage trials", "admin-portal", _admin_page(items))

    @app.get("/api/admin/storage-trials")
    def storage_trials_admin_list(request: Request, customer_id: str = "") -> dict:
        administrator(request)
        if customer_id:
            return {"trials": trials_for(customer_id)}
        return {"trials": [_with_effective(t) for t in rows("SELECT * FROM storage_trials ORDER BY granted_at DESC LIMIT 200")]}

    @app.post("/api/admin/storage-trials")
    async def storage_trials_admin_grant(request: Request) -> dict:
        identity = administrator(request)
        payload = await _payload(request)
        try:
            return grant(str(payload.get("customer_id") or ""), admin=identity, reason=payload.get("reason"),
                         allow_repeat=payload.get("allow_repeat") is True, repeat_reason=payload.get("repeat_reason") or "")
        except TrialError as error:
            _raise(error)

    @app.post("/api/admin/storage-trials/{trial_id}/cancel")
    async def storage_trials_admin_cancel(trial_id: str, request: Request) -> dict:
        identity = administrator(request)
        payload = await _payload(request)
        try:
            return cancel(trial_id, admin=identity, reason=payload.get("reason"))
        except TrialError as error:
            _raise(error)

    @app.get("/api/v2/customer/storage-trial")
    def customer_storage_trial(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer sign-in required.")
        return {"storage_trial": customer_view(identity["customer_id"])}

    @app.post("/api/v2/customer/storage-trial/continue-paid")
    async def customer_storage_trial_continue_paid(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        if (await _payload(request)).get("authorize_paid_storage") is not True:
            raise HTTPException(status_code=400, detail="Confirm that cloud storage should continue as a paid plan after the trial.")
        try:
            return authorize_paid(identity)
        except TrialError as error:
            _raise(error)
