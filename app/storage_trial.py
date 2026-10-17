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
* Countdown (owner requirement change, 2026-10-15): the 30 days must not run
  while hardware is still on its way. An administrator grant only reserves
  the trial (status "granted": no starts_at, no expires_at). The 30 days
  start when the cloud-storage subscription itself is created:
    - subscription_trial_fields() (the one hook every storage start calls)
      computes trial_end = now + 30 days, sends it to Stripe and keeps it on
      the record as the provisional expires_at; the record stays "granted"
      (an abandoned Checkout costs the customer nothing);
    - the trial becomes "active" when the trial subscription exists: at once
      for a server-created subscription (link_subscription(), called by
      build_orders), and on the verified customer.subscription.created /
      checkout.session.completed webhook for Checkout -- starts_at and
      expires_at are then Stripe's own trial_start / trial_end.
  A repeated Checkout request within OFFER_REUSE re-sends the same
  trial_end, so checkout_guard can reuse the open session.
  Statuses: granted -> active -> expired | cancelled | converted;
  granted -> cancelled (administrator, no Stripe call) | lapsed (unused for
  GRANT_LAPSE_DAYS after the grant; derived when read, like expiry).
  Stripe's 48-hour minimum for a Checkout trial_end no longer matters: a
  storage start always gets a fresh 30 days.
  Rows written before this change (status active, no subscription yet) are
  moved back to "granted" by migration 20261015_storage_trials_pending_start
  (the feature was never deployed).
* Expiry is derived from expires_at when read -- no cron is needed -- and
  Stripe ends the subscription itself.
* Mechanism: Stripe's native subscription trial, never a 100% coupon. The
  storage subscription carries trial_end = start + 30 days and
  trial_settings[end_behavior][missing_payment_method]=cancel. Checkout uses
  payment_method_collection=if_required, so no card is asked for during the
  trial.
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
GRANT_LAPSE_DAYS = 180  # an unused grant lapses; a re-grant needs the repeat approval
GRANTED, ACTIVE, EXPIRED, CANCELLED, CONVERTED, LAPSED = "granted", "active", "expired", "cancelled", "converted", "lapsed"
OPEN = (GRANTED, ACTIVE)
FINAL = (CANCELLED, CONVERTED, LAPSED)
# A repeated storage-start request this soon after the last one re-sends the
# same trial_end (identical Checkout fields: checkout_guard reuses the open
# session instead of refusing a second one). Longer than a Checkout Session
# lives (checkout_guard.CHECKOUT_SESSION_SECONDS, 31 minutes).
OFFER_REUSE = timedelta(hours=1)
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

def _epoch_iso(epoch) -> str:
    return _iso(datetime.fromtimestamp(int(epoch), tz=timezone.utc))


def lapses_at(trial: dict) -> datetime | None:
    granted = _parse(trial.get("granted_at"))
    return granted + timedelta(days=GRANT_LAPSE_DAYS) if granted else None


def effective_status(trial: dict | None) -> str | None:
    """The stored status, except that an active trial past expires_at is
    expired and a grant unused for GRANT_LAPSE_DAYS is lapsed -- whether or
    not anything has written that down yet."""
    if not trial:
        return None
    if trial["status"] == ACTIVE:
        expires = _parse(trial.get("expires_at"))
        if not expires or _now() >= expires:
            return EXPIRED
    if trial["status"] == GRANTED:
        lapse = lapses_at(trial)
        if not lapse or _now() >= lapse:
            return LAPSED
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
    """A trial whose 30 days are running."""
    trial = latest(customer_id)
    return trial if trial and trial["effective_status"] == ACTIVE else None


def open_trial(customer_id: str) -> dict | None:
    """A trial granted and not yet started, or running."""
    trial = latest(customer_id)
    return trial if trial and trial["effective_status"] in OPEN else None


def by_subscription(subscription_id: str) -> dict | None:
    if not subscription_id:
        return None
    return _with_effective(row("SELECT * FROM storage_trials WHERE stripe_subscription_id=? "
                               "ORDER BY granted_at DESC LIMIT 1", (subscription_id,)))


def _expire_lapsed(db, customer_id: str) -> None:
    now = _now()
    db.execute("UPDATE storage_trials SET status=?,ended_at=COALESCE(ended_at,expires_at),updated_at=? "
               "WHERE customer_id=? AND status=? AND expires_at<>'' AND expires_at<=?",
               (EXPIRED, _iso(now), customer_id, ACTIVE, _iso(now)))
    db.execute("UPDATE storage_trials SET status=?,ended_at=COALESCE(ended_at,?),updated_at=? "
               "WHERE customer_id=? AND status=? AND granted_at<=?",
               (LAPSED, _iso(now), _iso(now), customer_id, GRANTED, _iso(now - timedelta(days=GRANT_LAPSE_DAYS))))


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
    """Grant the 30-day trial to an eligible customer (administrator). It is
    reserved, not started: the 30 days begin when the customer's cloud-storage
    subscription is created (subscription_trial_fields / link_subscription),
    so time spent waiting for hardware delivery never uses it up."""
    import per_camera_billing as billing
    customer_id = str(customer_id or "").strip()
    reason = _text(reason, "A reason", required=True)
    if not customer_id or not row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        raise TrialError(404, "Customer not found.")
    if billing.has_camera_plan(customer_id):
        raise TrialError(409, "This customer already has a camera plan. The free storage trial is for customers evaluating "
                              "cloud storage before buying.")
    previous = latest(customer_id)
    if previous and previous["effective_status"] in OPEN:
        raise TrialError(409, f"This customer already has a free storage trial ({previous['effective_status']}).")
    if previous and not allow_repeat:
        raise TrialError(409, "This customer already had a free storage trial. A second trial needs an explicit "
                              "administrator re-grant approval (allow_repeat) with its own reason.")
    repeat_reason = _text(repeat_reason, "The re-grant approval reason", required=bool(previous)) if previous else ""
    now = _now()
    # starts_at / expires_at stay empty until the storage subscription starts
    # the countdown (the columns are NOT NULL in the 20261015 schema).
    trial = {"id": uuid.uuid4().hex, "customer_id": customer_id, "status": GRANTED, "starts_at": "",
             "expires_at": "", "granted_by": admin.get("email") or "",
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
            raise TrialError(409, "This customer already has a free storage trial.")
        raise
    audit(admin, "storage_trial.regrant" if previous else "storage_trial.grant", "customer", customer_id,
          {"trial_id": trial["id"], "status": GRANTED, "trial_days": TRIAL_DAYS,
           "countdown": "starts when the cloud storage subscription is created",
           "lapses_at": _iso(lapses_at(trial)), "reason": reason,
           "repeat_of": trial["repeat_of"], "repeat_reason": trial["repeat_reason"]})
    return _with_effective(trial)


def cancel(trial_id: str, *, admin: dict, reason: str) -> dict:
    """Revoke a trial (administrator). A granted trial that has not started
    has no subscription, so this is a record change only (a Checkout still
    open with it is caught by the webhook: its subscription is cancelled at
    once). A running trial's free storage ends now: a subscription still in
    its trial is cancelled on Stripe immediately, which never charges the
    customer."""
    reason = _text(reason, "A reason", required=True)
    trial = _with_effective(row("SELECT * FROM storage_trials WHERE id=?", (trial_id,)))
    if not trial:
        raise TrialError(404, "Trial not found.")
    if trial["status"] in FINAL:
        raise TrialError(409, f"This trial is already {trial['status']}.")
    subscription_status = None
    if trial["status"] != GRANTED and trial.get("stripe_subscription_id"):
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

def _usable(customer_id: str, plan_key: str) -> dict | None:
    if not is_storage_plan(plan_key):
        return None
    trial = latest(customer_id)
    if not trial or trial["effective_status"] != GRANTED or trial.get("stripe_subscription_id"):
        return None  # started, ended, lapsed, cancelled -- or never granted
    return trial


def offer_available(customer_id: str, plan_key: str) -> bool:
    """Display only: a storage-plan Checkout for this account would carry the
    30-day trial (subscription_trial_fields would return fields). Starts nothing."""
    try:
        return bool(customer_id) and _usable(customer_id, plan_key) is not None
    except Exception:  # a database before the storage_trials migration
        return False


def _offer_trial_end(trial: dict) -> int:
    """trial_end (epoch seconds) for a storage start now: now + 30 days,
    kept on the record as the provisional expires_at. A repeat within
    OFFER_REUSE re-sends the same value."""
    now = _now()
    provisional = _parse(trial.get("expires_at"))
    if provisional and now <= provisional - timedelta(days=TRIAL_DAYS) + OFFER_REUSE:
        return int(provisional.timestamp())
    trial_end = int((now + timedelta(days=TRIAL_DAYS)).timestamp())
    with connection() as db:
        db.execute("UPDATE storage_trials SET expires_at=?,updated_at=? WHERE id=? AND status=?",
                   (_epoch_iso(trial_end), _iso(now), trial["id"], GRANTED))
    logger.info("storage_trial.start_offered trial_id=%s customer_id=%s trial_end=%s",
                trial["id"], trial["customer_id"], trial_end)
    return trial_end


def subscription_trial_fields(customer_id: str, *, checkout: bool, plan_key: str) -> list[tuple[str, str]]:
    """THE hook every storage-subscription start calls (plan Checkout,
    Build Your System plan order, appliance activation, and any later
    storage start such as a hardware-first per-camera Checkout).

    Returns the Stripe fields to append to the request, or [] when nothing
    applies -- no granted, not-yet-started administrator trial, a plan
    without cloud storage, or a trial already started (or ended) by a
    subscription. [] means the request is exactly as it would be without
    this feature.

    This is where the 30-day countdown is set: trial_end = now + 30 days
    (the provisional expires_at; the trial turns "active" once the
    subscription exists -- link_subscription / the verified webhook).

    checkout=True (POST /v1/checkout/sessions, mode=subscription, storage
    plan line only -- never a session with one-time hardware/license lines):
      subscription_data[trial_end]=<now + 30 days, epoch>,
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
    trial = _usable(customer_id, plan_key)
    if not trial:
        return []
    trial_end = str(_offer_trial_end(trial))
    if checkout:
        return [("subscription_data[trial_end]", trial_end),
                ("subscription_data[trial_settings][end_behavior][missing_payment_method]", "cancel"),
                ("payment_method_collection", "if_required"),
                (f"metadata[{METADATA_KEY}]", trial["id"]),
                (f"subscription_data[metadata][{METADATA_KEY}]", trial["id"])]
    return [("trial_end", trial_end),
            ("trial_settings[end_behavior][missing_payment_method]", "cancel"),
            ("cancel_at_period_end", "true"),
            (f"metadata[{METADATA_KEY}]", trial["id"])]


def link_subscription(trial_id: str, customer_id: str, subscription_id: str, *,
                      trial_start=None, trial_end=None, source: str = "server") -> None:
    """Binds the trial subscription to its trial and starts the 30-day
    countdown of a granted trial: starts_at = Stripe's trial_start (or now),
    expires_at = Stripe's trial_end (or the provisional value this module
    sent). Called for a server-created subscription (build_orders) and by
    the verified webhook for a trialing trial subscription."""
    if not trial_id or not subscription_id:
        return
    now = _now()
    with connection() as db:
        db.execute("UPDATE storage_trials SET stripe_subscription_id=?,updated_at=? "
                   "WHERE id=? AND customer_id=? AND stripe_subscription_id IS NULL",
                   (subscription_id, _iso(now), trial_id, customer_id))
    trial = row("SELECT * FROM storage_trials WHERE id=? AND customer_id=?", (trial_id, customer_id))
    if not trial or trial["status"] != GRANTED or trial.get("stripe_subscription_id") != subscription_id:
        return
    starts = _epoch_iso(trial_start) if trial_start else _iso(now)
    expires = (_epoch_iso(trial_end) if trial_end else trial.get("expires_at")) or _iso(now + timedelta(days=TRIAL_DAYS))
    with connection() as db:
        started = db.execute("UPDATE storage_trials SET status=?,starts_at=?,expires_at=?,updated_at=? WHERE id=? AND status=?",
                             (ACTIVE, starts, expires, _iso(now), trial_id, GRANTED)).rowcount
    if started:
        audit(_SYSTEM if source == "webhook" else {"email": "storage-start", "role": "system"}, "storage_trial.start",
              "customer", customer_id, {"trial_id": trial_id, "stripe_subscription_id": subscription_id,
                                        "starts_at": starts, "expires_at": expires, "source": source})


# ------------------------------------------------------------------ explicit paid authorization

def withdraw_paid_authorization(subscription_id: str, *, by: dict) -> bool:
    """The trial subscription is set to end again (the owner pressed Cancel, or cancelled in Stripe) after the
    owner had chosen paid storage: that choice no longer holds, so the notes and a later "Continue" follow
    Stripe again (Green 2026-10-16). True when a recorded authorization was withdrawn."""
    trial = by_subscription(str(subscription_id or ""))
    if not trial or not trial.get("paid_authorized_at") or trial["effective_status"] != ACTIVE:
        return False
    now = _iso(_now())
    with connection() as db:
        withdrawn = db.execute("UPDATE storage_trials SET paid_authorized_at=NULL,paid_authorized_by=NULL,updated_at=? "
                               "WHERE id=? AND paid_authorized_at IS NOT NULL", (now, trial["id"])).rowcount
    if withdrawn:
        audit(by, "storage_trial.paid_authorization_withdrawn", "customer", trial["customer_id"],
              {"trial_id": trial["id"], "stripe_subscription_id": subscription_id})
    return bool(withdrawn)


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
    # The owner's choice is recorded BEFORE Stripe is asked (Green review of 90f061c): Stripe's own
    # customer.subscription.updated (cancel_at_period_end true -> false) can reach the webhook before this call
    # returns, and the guard must then see an authorized trial -- otherwise it would set the subscription to end again
    # and silently undo the customer's explicit choice. If the Stripe call fails, the record is withdrawn again (only
    # the value written here, never a later one).
    now = _iso(_now())
    with connection() as db:
        claimed = db.execute("UPDATE storage_trials SET paid_authorized_at=?,paid_authorized_by=?,updated_at=? "
                             "WHERE id=? AND paid_authorized_at IS NULL",
                             (now, identity.get("email") or "", now, trial["id"])).rowcount
    if not claimed:
        return {"status": "already_authorized", "expires_at": trial["expires_at"]}
    # One key per attempt: after a cancel withdrew an earlier choice, a second "Continue" must reach Stripe again
    # (a per-subscription key would replay the first answer and change nothing).
    try:
        main.stripe_api_post(f"/v1/subscriptions/{trial['stripe_subscription_id']}", fields,
                             idempotency_key=f"anyaicam-storage-trial-authorize-{trial['stripe_subscription_id']}-{uuid.uuid4().hex}")
    except Exception as error:
        # Codex 2026-10-17: a failed call is not proof the change failed -- Stripe may have applied it and only the
        # response was lost (timeout). Ask Stripe what is true before touching the customer's recorded choice.
        applied = _renewal_applied(trial["stripe_subscription_id"])
        if applied is True:
            logger.warning("storage_trial.authorize_reconciled trial_id=%s error_type=%s: Stripe applied the change",
                           trial["id"], type(error).__name__)
            audit(identity, "storage_trial.paid_authorization_reconciled", "customer", customer_id,
                  {"trial_id": trial["id"], "stripe_subscription_id": trial["stripe_subscription_id"],
                   "error_type": type(error).__name__})
        elif applied is False:
            with connection() as db:
                db.execute("UPDATE storage_trials SET paid_authorized_at=NULL,paid_authorized_by=NULL,updated_at=? "
                           "WHERE id=? AND paid_authorized_at=?", (_iso(_now()), trial["id"], now))
            raise
        else:
            # Stripe can't be read either. The choice stays recorded: if Stripe did not apply it, the next trialing
            # event (authorized + still ending) withdraws it (withdraw_paid_authorization); if it did, the delayed
            # webhook finds it authorized and is never guarded back. No charge either way without Stripe agreeing.
            logger.warning("storage_trial.authorize_unconfirmed trial_id=%s error_type=%s",
                           trial["id"], type(error).__name__)
            raise TrialError(503, "We couldn't confirm your choice with our payment provider yet. It will be "
                                  "confirmed automatically; check My subscription again in a few minutes.") from error
    audit(identity, "storage_trial.paid_authorized", "customer", customer_id,
          {"trial_id": trial["id"], "stripe_subscription_id": trial["stripe_subscription_id"], "starts_paid_at": trial["expires_at"]})
    try:
        billing._upsert_current(stripe_state.current_subscription(trial["stripe_subscription_id"]))
    except Exception:
        logger.info("storage_trial.entitlement_waits_for_webhook customer_id=%s", customer_id)
    return {"status": "authorized", "expires_at": trial["expires_at"]}


def _renewal_applied(subscription_id: str):
    """After a failed "Continue" call: True if Stripe shows the subscription renewing (cancel_at_period_end false),
    False if it still ends at the trial end, None if Stripe can't be read."""
    import stripe_state
    try:
        current = stripe_state.current_subscription(subscription_id) or {}
    except Exception:
        return None
    if str(current.get("status") or "") in stripe_state.ENDED_STATUSES:
        return False
    return not bool(current.get("cancel_at_period_end"))


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


CONSENT_EXCEPTION_ACTION = "storage_trial.charged_without_authorization"
BILLING_ALERT_EMAIL_ENV = "ANYAICAM_BILLING_ALERT_EMAIL"
CONSENT_EXCEPTION_ACTION_REQUIRED = ("Review the charge in Stripe and refund it or confirm it with the customer. "
                                     "Nothing was cancelled, refunded or changed automatically.")


def _flag_unauthorized_charge(trial: dict, subscription_id: str, current: dict) -> None:
    """Billing Consent Exception (Codex / owner 2026-10-17): Stripe billed a trial subscription nobody authorized.
    Never recorded as a conversion. Recorded once per trial and invoice (Stripe's repeated events add nothing), shown
    on /admin/storage-trials, and emailed to the billing administrator. Never touches Stripe."""
    invoice = current.get("latest_invoice")
    invoice_id = _id(invoice)
    logger.error("storage_trial.billing_consent_exception trial_id=%s customer_id=%s subscription_id=%s invoice_id=%s",
                 trial["id"], trial["customer_id"], subscription_id, invoice_id)
    seen = rows("SELECT id FROM audit_logs WHERE action=? AND entity_id=? AND details_json LIKE ? AND details_json LIKE ?",
                (CONSENT_EXCEPTION_ACTION, trial["customer_id"], f'%"trial_id": "{trial["id"]}"%',
                 f'%"invoice_id": "{invoice_id}"%'))
    if seen:
        return
    details = {"trial_id": trial["id"], "stripe_subscription_id": subscription_id, "invoice_id": invoice_id,
               "stripe_customer_id": _id(current.get("customer")), "subscription_status": str(current.get("status") or ""),
               "trial_expires_at": trial.get("expires_at"), "paid_authorized_at": trial.get("paid_authorized_at"),
               "amount_paid": invoice.get("amount_paid") if isinstance(invoice, dict) else None,
               "currency": invoice.get("currency") if isinstance(invoice, dict) else None,
               "action_required": CONSENT_EXCEPTION_ACTION_REQUIRED}
    audit(_SYSTEM, CONSENT_EXCEPTION_ACTION, "customer", trial["customer_id"], details)
    try:
        _notify_consent_exception(trial, details)
    except Exception:  # the alert never breaks webhook processing; the admin page still shows it
        logger.exception("storage_trial.billing_consent_exception_alert_failed trial_id=%s", trial["id"])


def _notify_consent_exception(trial: dict, details: dict) -> dict:
    import os
    from purchase_notifications import SUPPORT_EMAIL, _portal_url, _send_once
    recipient = os.environ.get(BILLING_ALERT_EMAIL_ENV, "").strip() or SUPPORT_EMAIL
    link = _portal_url("/admin/storage-trials")
    lines = [f"{key}: {value}" for key, value in details.items() if key != "action_required" and value not in (None, "")]
    subject = f"Billing Consent Exception: customer {trial['customer_id']}"
    text = ("Stripe charged a free-storage-trial subscription the customer never authorized to continue as paid.\n\n"
            + "\n".join(lines) + f"\n\nAction required: {CONSENT_EXCEPTION_ACTION_REQUIRED}\n"
            f"Review (sign-in required): {link}")
    html = ("<p>Stripe charged a free-storage-trial subscription the customer never authorized to continue as paid.</p><ul>"
            + "".join(f"<li>{escape(line)}</li>" for line in lines)
            + f"</ul><p><strong>Action required:</strong> {escape(CONSENT_EXCEPTION_ACTION_REQUIRED)}</p>"
            f'<p><a href="{escape(link, quote=True)}">Review on Free storage trials</a> (sign-in required)</p>')
    return _send_once(event_id=f"billing-consent-exception:{trial['id']}:{details['invoice_id']}",
                      notification_type="billing_consent_exception", customer_id=trial["customer_id"],
                      recipient_email=recipient, subject=subject, text=text, html=html,
                      metadata={"trial_id": trial["id"], "invoice_id": details["invoice_id"]})


def stop_unauthorized_renewal(trial_id: str, *, admin: dict, reason: str) -> dict:
    """An administrator, after reviewing a Billing Consent Exception, stops FUTURE renewals of that subscription:
    cancel_at_period_end=true, so the period already paid for runs out and nothing more is billed. It never refunds
    and never cancels immediately (a refund is a separate, deliberate decision). Refused unless the recorded state
    still holds: an exception exists for this trial, the customer has not since authorized paid storage, and Stripe
    shows this account's subscription still renewing."""
    import main
    import stripe_state
    reason = _text(reason, "reason", required=True)
    trial = _with_effective(row("SELECT * FROM storage_trials WHERE id=?", (str(trial_id or ""),)))
    if not trial:
        raise TrialError(404, "Storage trial not found.")
    if not rows("SELECT id FROM audit_logs WHERE action=? AND entity_id=? AND details_json LIKE ?",
                (CONSENT_EXCEPTION_ACTION, trial["customer_id"], f'%"trial_id": "{trial["id"]}"%')):
        raise TrialError(409, "No Billing Consent Exception is recorded for this trial.")
    if trial.get("paid_authorized_at"):
        raise TrialError(409, "The customer has since authorized paid storage; there is nothing to stop.")
    subscription_id = str(trial.get("stripe_subscription_id") or "")
    if not subscription_id:
        raise TrialError(409, "This trial has no Stripe subscription.")
    current = stripe_state.current_subscription(subscription_id)
    owner = str((current.get("metadata") or {}).get("anyaicam_customer_id") or trial["customer_id"])
    if owner != trial["customer_id"]:
        raise TrialError(409, "The Stripe subscription does not belong to this trial's account.")
    status = str(current.get("status") or "")
    if status in stripe_state.ENDED_STATUSES:
        raise TrialError(409, f"The subscription has already ended ({status}).")
    if status == "trialing":
        # Still in its free trial: that is the customer's "Continue as paid" decision window, never an exception.
        raise TrialError(409, "The subscription is still in its free trial; the customer's choice decides it.")
    if current.get("cancel_at_period_end"):
        return {"status": "already_stopped", "current_period_end": current.get("current_period_end")}
    main.stripe_api_post(f"/v1/subscriptions/{subscription_id}", [("cancel_at_period_end", "true")],
                         idempotency_key=f"anyaicam-consent-exception-stop-{subscription_id}-{uuid.uuid4().hex}")
    # Codex 2026-10-17 (admin/customer race): if the customer's explicit choice was recorded while this stop was in
    # flight, the customer's consent wins -- put renewal back so Stripe and our record agree.
    if (row("SELECT paid_authorized_at FROM storage_trials WHERE id=?", (trial["id"],)) or {}).get("paid_authorized_at"):
        main.stripe_api_post(f"/v1/subscriptions/{subscription_id}", [("cancel_at_period_end", "false")],
                             idempotency_key=f"anyaicam-consent-exception-restore-{subscription_id}-{uuid.uuid4().hex}")
        audit(admin, "storage_trial.consent_exception_stop_superseded", "customer", trial["customer_id"],
              {"trial_id": trial["id"], "stripe_subscription_id": subscription_id, "reason": reason})
        return {"status": "superseded_by_customer_consent"}
    audit(admin, "storage_trial.consent_exception_renewal_stopped", "customer", trial["customer_id"],
          {"trial_id": trial["id"], "stripe_subscription_id": subscription_id, "reason": reason,
           "subscription_status": status, "current_period_end": current.get("current_period_end"),
           "refund": "not issued (a separate decision)"})
    return {"status": "renewal_stopped", "current_period_end": current.get("current_period_end")}


def consent_exceptions(limit: int = 200) -> list[dict]:
    """Every Billing Consent Exception, newest first, with its audit details."""
    import json
    found = []
    for item in rows("SELECT entity_id, created_at, details_json FROM audit_logs WHERE action=? "
                     "ORDER BY created_at DESC, id DESC LIMIT ?", (CONSENT_EXCEPTION_ACTION, limit)):
        try:
            details = json.loads(item["details_json"] or "{}")
        except ValueError:
            details = {}
        found.append({"customer_id": item["entity_id"], "flagged_at": item["created_at"], **details})
    stopped = {}
    for item in rows("SELECT details_json, created_at FROM audit_logs WHERE action='storage_trial.consent_exception_renewal_stopped'"):
        try:
            stopped.setdefault(json.loads(item["details_json"] or "{}").get("trial_id"), item["created_at"])
        except ValueError:
            continue
    for entry in found:
        entry["renewal_stopped_at"] = stopped.get(entry.get("trial_id"))
    return found


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
    status = str(current.get("status") or "")
    if status == "trialing":
        if trial["status"] in (CANCELLED, LAPSED):
            # Revoked (or lapsed and re-granted) while a Checkout was open:
            # the free subscription ends now, which never charges anyone.
            main.stripe_api_delete(f"/v1/subscriptions/{subscription_id}")
            return {"status": "cancelled_on_stripe", "reason": f"the trial is {trial['status']}"}
        # The trial subscription exists: the 30-day countdown starts here
        # (a granted trial), at Stripe's own trial_start / trial_end.
        link_subscription(trial["id"], trial["customer_id"], subscription_id, trial_start=current.get("trial_start"),
                          trial_end=current.get("trial_end"), source="webhook")
        trial = _with_effective(row("SELECT * FROM storage_trials WHERE id=?", (trial["id"],)))
        bound = trial.get("stripe_subscription_id") == subscription_id
        if bound and trial["status"] == ACTIVE and current.get("trial_end") and _epoch_iso(current["trial_end"]) != trial["expires_at"]:
            # Stripe is the record of when the free period ends (e.g. the
            # trial was ended early in Stripe by a person).
            with connection() as db:
                db.execute("UPDATE storage_trials SET expires_at=?,updated_at=? WHERE id=?",
                           (_epoch_iso(current["trial_end"]), _iso(_now()), trial["id"]))
        authorized = bound and trial.get("paid_authorized_at")
        if authorized and current.get("cancel_at_period_end"):
            # Set to end again after the owner chose paid storage (cancelled in the portal or in Stripe).
            withdraw_paid_authorization(subscription_id, by=_SYSTEM)
            return {"status": "paid_authorization_withdrawn"}
        if event_type in _GUARD_EVENTS and not authorized and not current.get("cancel_at_period_end"):
            # Codex 2026-10-17: an un-cancel in Stripe (portal or dashboard) is NOT verifiable customer consent --
            # a subscription.updated event does not say who did it. Only the owner's in-app "Continue as paid"
            # (authorize_paid: signed-in customer owner, recorded identity) authorizes paid storage. Anything
            # else that leaves the trial subscription renewing is guarded back to ending at the trial end.
            # The key is per event: a per-subscription key made Stripe replay the FIRST guard's answer for 24 h,
            # so a second guard (e.g. after an un-cancel) changed nothing.
            main.stripe_api_post(f"/v1/subscriptions/{subscription_id}", [("cancel_at_period_end", "true")],
                                 idempotency_key=f"anyaicam-storage-trial-guard-{subscription_id}-{event.get('id') or uuid.uuid4().hex}")
            return {"status": "guarded", "subscription_id": subscription_id}
        return {"status": "trialing"}
    if trial.get("stripe_subscription_id") != subscription_id:
        # Includes a granted trial: only a trialing subscription starts it.
        return {"status": "ignored", "reason": "another subscription holds this trial"}
    now = _iso(_now())
    if status == "active" and trial["status"] in (ACTIVE, EXPIRED) and _invoice_paid(current):
        # Paid storage after the trial needs the owner's verifiable choice (authorize_paid). An active, paid
        # subscription is not that choice: the guard may have failed or been undone, or a person changed it in
        # Stripe. That is a Billing Consent Exception, never a conversion.
        if not trial.get("paid_authorized_at"):
            _flag_unauthorized_charge(trial, subscription_id, current)
            return {"status": "charged_without_authorization"}
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
    started = trial["status"] not in (GRANTED, LAPSED) and bool(trial.get("starts_at"))
    return {"status": trial["effective_status"], "trial_days": TRIAL_DAYS, "countdown_started": started,
            "starts_at": trial["starts_at"] if started else None, "expires_at": trial["expires_at"] if started else None,
            "unused_grant_lapses_at": _iso(lapses_at(trial)) if trial["effective_status"] == GRANTED else None,
            "applies_to": "Cloud storage plan (Hybrid) only", "subscription_started": bool(trial.get("stripe_subscription_id")),
            "paid_after_trial": bool(trial.get("paid_authorized_at"))}


def customer_note_html(customer_id: str, is_owner: bool) -> str:
    try:
        trial = open_trial(customer_id)
    except Exception:
        return ""  # a database before the storage_trials migration
    if not trial:
        return ""
    if trial["effective_status"] == GRANTED:
        # An account already paying for a camera plan cannot start the reserved trial (plan changes never start one),
        # so it is not promised a trial it will never get (Green 2026-10-16).
        import per_camera_billing
        if per_camera_billing.has_camera_plan(customer_id):
            return ""
        return ('<p class="health-detail" id="storage-trial-note"><strong>A 30-day free cloud storage trial is reserved for '
                'your account.</strong> The 30 days start only when you start the Hybrid cloud storage plan (for example after '
                'your equipment is delivered), not before. Hardware, software licenses and add-ons are charged at their normal '
                'prices. You will not be charged for storage after the trial unless you choose to continue.</p>')
    until = escape(_parse(trial["expires_at"]).strftime("%b %d, %Y"))
    note = (f'<p class="health-detail" id="storage-trial-note"><strong>Free cloud storage trial until '
            f'<time datetime="{escape(trial["expires_at"], quote=True)}" data-local-date>{until}</time>.</strong> '
            'Hardware, software licenses and add-ons are charged at their normal prices. ')
    if trial.get("paid_authorized_at"):
        return note + 'You chose to continue cloud storage as a paid plan after the trial.</p>'
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

def _consent_exceptions_panel(exceptions: list[dict]) -> str:
    if not exceptions:
        return ""
    body = "".join(
        f'<tr><td>{escape(str(e.get("flagged_at") or "")[:19])}</td><td>{escape(str(e.get("customer_id") or ""))}</td>'
        f'<td>{escape(str(e.get("stripe_subscription_id") or ""))}<br><small>{escape(str(e.get("stripe_customer_id") or ""))}</small></td>'
        f'<td>{escape(str(e.get("invoice_id") or ""))}<br><small>{escape(str(e.get("amount_paid") if e.get("amount_paid") is not None else ""))} '
        f'{escape(str(e.get("currency") or ""))}</small></td><td>{escape(str(e.get("trial_id") or ""))}</td>'
        + (f'<td>Future renewals stopped {escape(str(e["renewal_stopped_at"])[:19])}</td></tr>' if e.get("renewal_stopped_at") else
           '<td><button class="ghost-button" data-stop-renewal="' + escape(str(e.get("trial_id") or ""), quote=True)
           + '">Stop future renewals</button></td></tr>')
        for e in exceptions)
    return f'''<section class="panel" id="billing-consent-exceptions" role="alert" style="border:2px solid #b42318;margin-bottom:14px">
<h2>Billing Consent Exceptions ({len(exceptions)})</h2>
<p class="health-detail">Stripe charged a free-storage-trial subscription without the customer's authorization to continue as paid.
{escape(CONSENT_EXCEPTION_ACTION_REQUIRED)}</p>
<p class="health-detail">"Stop future renewals" checks the current Stripe state, then sets the subscription to end at the end of
the period already paid. It does not refund and does not cancel immediately.</p>
<table><tr><th>Flagged</th><th>Customer</th><th>Subscription / Stripe customer</th><th>Invoice / amount</th><th>Trial</th><th>Review</th></tr>{body}</table></section>'''


def _admin_page(items: list[dict], exceptions: list[dict] | None = None) -> str:
    body = "".join(
        f'<tr><td>{escape(t.get("customer_name") or t["customer_id"])}<br><small>{escape(t["customer_id"])}</small></td>'
        f'<td>{escape(t["effective_status"])}</td>'
        f'<td>{escape((t["starts_at"] or "")[:10] if t["status"] not in (GRANTED, LAPSED) else "When storage starts")}</td>'
        f'<td>{escape((t["expires_at"] or "")[:10] if t["status"] not in (GRANTED, LAPSED) else "Start +" + str(TRIAL_DAYS) + " days")}</td>'
        f'<td>{escape(t["granted_by"])}</td><td>{escape(t["reason"])}{"<br><small>Re-grant: " + escape(t.get("repeat_reason") or "") + "</small>" if t.get("repeat_of") else ""}</td>'
        f'<td>{escape(t.get("stripe_subscription_id") or "")}</td>'
        f'<td>{"" if t["status"] in (CANCELLED, CONVERTED) else "<button class=ghost-button data-cancel=" + escape(t["id"], quote=True) + ">Cancel</button>"}</td></tr>'
        for t in items) or '<tr><td colspan="8">No storage trials yet.</td></tr>'
    return f'''<header class="topbar"><div><p class="eyebrow">Administrator</p><h1>Free storage trials</h1></div></header>
{_consent_exceptions_panel(exceptions or [])}<section class="panel"><h2>Grant a 30-day free storage trial</h2>
<p class="health-detail">Cloud storage plan only (Hybrid). Hardware, licenses and add-ons stay at normal prices. The customer is never
charged after the trial unless they choose to continue. One trial per customer; a second needs a re-grant approval reason.
The 30 days start when the customer starts the cloud storage plan (never while hardware is awaiting delivery); a grant unused
for {GRANT_LAPSE_DAYS} days lapses.</p>
<label>Customer ID <input id="st-customer" maxlength="80"></label> <label>Reason <input id="st-reason" maxlength="500"></label>
<label><input type="checkbox" id="st-repeat"> Approve a repeat trial</label> <label>Re-grant reason <input id="st-repeat-reason" maxlength="500"></label>
<button class="action-button" id="st-grant" type="button">Grant trial</button><p class="health-detail" id="st-result" role="status"></p></section>
<section class="panel" style="margin-top:14px"><table><tr><th>Customer</th><th>Status</th><th>Starts</th><th>Expires</th><th>Granted by</th>
<th>Reason</th><th>Subscription</th><th></th></tr>{body}</table></section>
<script>
const out=document.getElementById('st-result');
document.getElementById('st-grant').onclick=async()=>{{const r=await fetch('/api/admin/storage-trials',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{customer_id:document.getElementById('st-customer').value,reason:document.getElementById('st-reason').value,allow_repeat:document.getElementById('st-repeat').checked,repeat_reason:document.getElementById('st-repeat-reason').value}})}});const d=await r.json().catch(()=>({{}}));out.textContent=r.ok?'Trial granted. The 30 days start when the customer starts cloud storage.':(d.detail||'Could not grant the trial.');if(r.ok)setTimeout(()=>location.reload(),900);}};
document.querySelectorAll('[data-stop-renewal]').forEach(b=>b.onclick=async()=>{{const reason=prompt('Reason for stopping future renewals (no refund is issued)?');if(!reason)return;const r=await fetch('/api/admin/storage-trials/consent-exceptions/'+b.dataset.stopRenewal+'/stop-renewal',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{reason}})}});const d=await r.json().catch(()=>({{}}));out.textContent=r.ok?'Future renewals stopped. No refund was issued.':(d.detail||'Could not stop renewals.');if(r.ok)setTimeout(()=>location.reload(),900);}});
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
        return shell("Free storage trials", "admin-portal", _admin_page(items, consent_exceptions()))

    @app.get("/api/admin/storage-trials")
    def storage_trials_admin_list(request: Request, customer_id: str = "") -> dict:
        administrator(request)
        if customer_id:
            return {"trials": trials_for(customer_id)}
        return {"trials": [_with_effective(t) for t in rows("SELECT * FROM storage_trials ORDER BY granted_at DESC LIMIT 200")]}

    @app.get("/api/admin/storage-trials/consent-exceptions")
    def storage_trials_consent_exceptions(request: Request) -> dict:
        administrator(request)
        return {"exceptions": consent_exceptions()}

    @app.post("/api/admin/storage-trials/consent-exceptions/{trial_id}/stop-renewal")
    async def storage_trials_consent_exception_stop(trial_id: str, request: Request) -> dict:
        admin = administrator(request)
        payload = await _payload(request)
        try:
            return stop_unauthorized_renewal(trial_id, admin=admin, reason=str(payload.get("reason") or ""))
        except TrialError as error:
            _raise(error)

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
