import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from appliance_identity import create_grant
from database_backend import backend
from partner_db import connection, password_hash
from partner_portal import partner_identity


def _now() -> str:
    return datetime.now().isoformat()


# Email ownership (owner decision 2026-10-17): approving a request attaches any purchase made earlier under
# that email (resolve_pending_links_for_customer), so the requester must first prove they own the address.
VERIFY_TTL_HOURS = 48
VERIFY_RESEND_SECONDS = 120  # reviewer resend throttle
VERIFY_REISSUE_HOLD_SECONDS = 3600  # a public re-submission keeps a link this young valid instead of replacing it
MIN_PASSWORD = 10  # the /customer-register form's rule
VERIFIED_MESSAGE = ("Thank you. Your email address is confirmed and your password is set. An administrator will "
                    "review your AnyAiCam account request; you'll be able to sign in once it is approved.")
VERIFY_INVALID_MESSAGE = ("This confirmation link is invalid or has expired. Submit the registration form again "
                          "to receive a new link.")


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_registration_verification(email: str, *, now: datetime | None = None, force: bool = False) -> str | None:
    """A new single-use link token for the PENDING, UNVERIFIED request of this email, or None (no such request,
    already verified, or -- unless forced by a reviewer's resend -- a link was issued less than
    VERIFY_REISSUE_HOLD_SECONDS ago: anyone can re-submit the public form, and that must not keep invalidating the
    owner's link). The previous link stops working."""
    normalized = (email or "").strip().lower()
    now = now or datetime.now()
    with connection() as db:
        item = db.execute(
            "SELECT id,email_verified_at,verification_sent_at,verification_token_hash FROM customer_registration_requests "
            "WHERE email=? AND status='pending'", (normalized,)).fetchone()
        if not item or item["email_verified_at"]:
            return None
        if not force and item["verification_sent_at"]:
            try:
                if (now - datetime.fromisoformat(item["verification_sent_at"])).total_seconds() < VERIFY_REISSUE_HOLD_SECONDS:
                    return None
            except ValueError:
                pass
        raw = secrets.token_urlsafe(32)
        # Codex 2026-10-17 (concurrent resends): compare-and-set on the link this call read. Of two simultaneous
        # issues only the first replaces it; the other gets None and sends nothing, so no email ever carries a
        # token that a concurrent issue has already made invalid.
        replaced = db.execute(
            "UPDATE customer_registration_requests SET verification_token_hash=?,verification_expires_at=?,"
            "verification_sent_at=? WHERE id=? AND status='pending' AND email_verified_at IS NULL "
            "AND COALESCE(verification_token_hash,'')=?",
            (_token_hash(raw), (now + timedelta(hours=VERIFY_TTL_HOURS)).isoformat(), now.isoformat(), item["id"],
             item["verification_token_hash"] or "")).rowcount
    return raw if replaced == 1 else None


def _link_base() -> str:
    import os
    return os.environ.get("ANYAICAM_PUBLIC_URL", "").strip().rstrip("/")


def send_registration_verification(email: str, base_url: str | None = None, *, force: bool = False) -> bool:
    """Issues a link (issue_registration_verification) and emails it to that same address. True when the email
    service accepted it. The email says nothing about purchases or other accounts. The link base is the
    configured ANYAICAM_PUBLIC_URL, never the request's Host (Green review F3); without it nothing is sent."""
    base_url = base_url or _link_base()
    if not base_url:
        logging.getLogger("anyaicam.customer_registration").warning(
            "registration_verification.not_sent reason=ANYAICAM_PUBLIC_URL_missing")
        return False
    normalized = email.strip().lower()
    with connection() as db:  # the link in force now: put back if the new one never reaches the inbox
        previous = db.execute("SELECT verification_token_hash,verification_expires_at,verification_sent_at "
                              "FROM customer_registration_requests WHERE email=? AND status='pending'", (normalized,)).fetchone()
    previous = dict(previous) if previous else None
    raw = issue_registration_verification(email, force=force)
    if not raw:
        return False
    link = f"{base_url.rstrip('/')}/customer-register/verify-email?token={raw}"
    subject = "Confirm your email for your AnyAiCam account request"
    text = ("Confirm this email address and choose your password to complete your AnyAiCam account request:\n"
            f"{link}\n\nThis link works once and expires in {VERIFY_TTL_HOURS} hours. "
            "Your request is reviewed only after the address is confirmed. If you didn't ask for this, ignore this email.")
    try:
        from email_service import get_email_service
        result = get_email_service().send("email_verification", normalized, subject, text)
        sent = not (isinstance(result, dict) and result.get("status") in ("failed", "error"))
    except Exception as error:
        logging.getLogger("anyaicam.customer_registration").warning(
            "registration_verification.email_failed error_type=%s", type(error).__name__)
        sent = False
    if not sent and previous is not None:
        # Green review of 80a961a: a failed send must not leave the owner with a dead earlier link AND the 1 h
        # re-submission hold. The earlier link (or no link, so a re-submission issues one) comes back -- only if
        # nothing replaced the link issued here in the meantime.
        with connection() as db:
            db.execute("UPDATE customer_registration_requests SET verification_token_hash=?,verification_expires_at=?,"
                       "verification_sent_at=? WHERE email=? AND verification_token_hash=? AND email_verified_at IS NULL",
                       (previous["verification_token_hash"], previous["verification_expires_at"],
                        previous["verification_sent_at"], normalized, _token_hash(raw)))
    return sent


def verify_registration_email(raw: str, password: str, display_name: str, *, now: datetime | None = None) -> bool:
    """Marks the request this link belongs to as verified FOR ITS OWN EMAIL and sets its password to the one chosen
    by whoever holds the link (Green review F1: the first submitter's password must never survive -- the password
    typed on /customer-register by someone who does not own the inbox is discarded here). Single use; expired,
    unknown, used or non-pending links and a too-short password all return False with no detail.
    The name is set here too (owner follow-up 2026-10-17): approval creates the account with the name the inbox
    owner gave, never one an unverified applicant typed. Exactly one of two simultaneous submissions of a link
    succeeds (the UPDATE re-checks the token; only rowcount 1 is a success)."""
    name = (display_name or "").strip()
    if not raw or len(raw) > 200 or len(password or "") < MIN_PASSWORD or not 2 <= len(name) <= 120:
        return False
    now = now or datetime.now()
    with connection() as db:
        item = db.execute(
            "SELECT id,email,verification_expires_at FROM customer_registration_requests "
            "WHERE verification_token_hash=? AND status='pending' AND email_verified_at IS NULL",
            (_token_hash(raw),)).fetchone()
        if not item or not item["verification_expires_at"] or item["verification_expires_at"] < now.isoformat():
            return False
        updated = db.execute(
            "UPDATE customer_registration_requests SET email_verified_at=?,email_verified_for=?,password_hash=?,"
            "display_name=?,verification_token_hash=NULL,verification_expires_at=NULL "
            "WHERE id=? AND verification_token_hash=? AND status='pending' AND email_verified_at IS NULL",
            (now.isoformat(), item["email"], password_hash(password), name, item["id"], _token_hash(raw))).rowcount
        if updated != 1:
            return False  # another submission of this link (or a reissue) won the race
        db.execute(
            "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (item["email"], "anonymous", "customer_registration.email_verified", "customer_registration", item["id"],
             '{"password_set": true}', now.isoformat()))
    return True


def _verify_form(token: str, error: str = "") -> str:
    from html import escape
    return ((f'<p role="alert">{escape(error)}</p>' if error else "")
            + '<p>Enter your name and choose the password for your AnyAiCam account to confirm this email address.</p>'
            '<form method="post" action="/customer-register/verify-email" id="verify-form">'
            f'<input type="hidden" name="token" value="{escape(token or "", quote=True)}">'
            '<input type="hidden" name="csrf_token" value="">'
            '<label>Full name<br><input name="display_name" minlength="2" maxlength="120" autocomplete="name" required></label><br>'
            '<label>Password<br><input name="password" type="password" minlength="10" autocomplete="new-password" required></label><br>'
            '<label>Confirm password<br><input name="confirm_password" type="password" minlength="10" autocomplete="new-password" required></label><br>'
            '<button type="submit">Confirm email</button></form>'
            "<script>document.getElementById('verify-form').addEventListener('submit',function(){"
            "var m=document.cookie.match(/(?:^|; )anyaicam_csrf=([^;]*)/);if(m)this.csrf_token.value=decodeURIComponent(m[1]);});</script>")


def _verify_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    response = HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>AnyAiCam</title>'
        '<style>body{font-family:system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem;color:#0b1530}'
        'input{width:100%;padding:.5rem;margin:.25rem 0 .75rem;box-sizing:border-box}</style>'
        f'</head><body><h1>{title}</h1>{body}</body></html>', status_code=status_code)
    # Green review F5: the token is in the URL.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def create_pending_registration(display_name: str, email: str, password: str) -> tuple[str, bool]:
    """Create an unassigned, inactive request in the authoritative database."""
    normalized = email.strip().lower()
    with connection() as db:
        existing = db.execute(
            "SELECT id,status,customer_id,user_id,decided_at,decided_by,rejection_reason FROM customer_registration_requests WHERE email=?",
            (normalized,),
        ).fetchone()
        if existing and existing["status"] == "rejected" and not existing["customer_id"] and not existing["user_id"]:
            # Codex review 2026-10-16: a rejected request used to block that email forever. A rejection that never
            # created an account is reopened as a new pending request (same row: email is UNIQUE); the earlier
            # decision stays in the audit log. A rejection of an approved account (customer/user rows exist,
            # suspended/revoked) is not reopened here: those rows still hold the email (see below).
            import json as _json
            if db.execute("SELECT 1 FROM partner_users WHERE lower(email)=?", (normalized,)).fetchone() or                     db.execute("SELECT 1 FROM customers WHERE lower(email)=?", (normalized,)).fetchone():
                raise HTTPException(status_code=409, detail="That email belongs to an existing account. Please contact AnyAiCam support.")
            now = _now()
            db.execute(
                "UPDATE customer_registration_requests SET display_name=?,password_hash=?,status='pending',partner_id=NULL,"
                "requested_at=?,decided_at=NULL,decided_by=NULL,rejection_reason=NULL,email_verified_at=NULL,"
                "email_verified_for=NULL,verification_token_hash=NULL,verification_expires_at=NULL,verification_sent_at=NULL "
                "WHERE id=? AND status='rejected'",
                (display_name.strip(), password_hash(password), now, existing["id"]),
            )
            db.execute(
                "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (normalized, "anonymous", "customer_registration.reopened", "customer_registration", existing["id"],
                 _json.dumps({"previous_status": "rejected", "previous_decided_at": existing["decided_at"],
                              "previous_decided_by": existing["decided_by"],
                              "previous_reason": (existing["rejection_reason"] or "")[:200]}), now),
            )
            return "pending", True
        if existing and existing["status"] == "rejected":
            raise HTTPException(status_code=409, detail="That email belongs to an existing account. Please contact AnyAiCam support.")
        if existing:
            return existing["status"], False
        if db.execute("SELECT 1 FROM partner_users WHERE lower(email)=?", (normalized,)).fetchone():
            raise HTTPException(status_code=409, detail="That email is already registered. Use the sign-in page.")
        if db.execute("SELECT 1 FROM customers WHERE lower(email)=?", (normalized,)).fetchone():
            raise HTTPException(status_code=409, detail="That email is already registered. Use the sign-in page.")
        request_id = secrets.token_hex(16)
        try:
            db.execute(
                "INSERT INTO customer_registration_requests(id,display_name,email,password_hash,status,requested_at) VALUES(?,?,?,?,?,?)",
                (request_id, display_name.strip(), normalized, password_hash(password), "pending", _now()),
            )
            db.execute(
                "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (normalized, "anonymous", "customer_registration.requested", "customer_registration", request_id, "{}", _now()),
            )
        except Exception as error:
            if "unique" in str(error).lower() or "duplicate" in str(error).lower():
                raise HTTPException(status_code=409, detail="That email already has an account request.") from error
            raise
    return "pending", True


def _approval_identity(request: Request, current_user, is_master_admin) -> dict:
    identity = partner_identity(request)
    if identity:
        if identity.get("role") == "administrator":
            with connection() as db:
                global_grant = db.execute(
                    "SELECT 1 FROM identity_grants g JOIN partner_users u ON u.id=g.user_id "
                    "WHERE lower(u.email)=? AND g.role='administrator' AND g.scope_type='global' AND g.revoked_at IS NULL",
                    (str(identity.get("email") or "").lower(),),
                ).fetchone()
            if global_grant:
                return {"master": True, "email": identity["email"], "partner_id": identity.get("partner_id")}
        if identity.get("role") in {"partner_owner", "administrator"} and identity.get("partner_id"):
            return {"master": False, "email": identity["email"], "partner_id": identity["partner_id"]}
        raise HTTPException(status_code=403, detail="Customer registration approval permission required.")
    legacy = current_user(request)
    if is_master_admin(legacy):
        return {"master": True, "email": legacy.get("email", "admin@local"), "partner_id": None}
    raise HTTPException(status_code=403, detail="Customer registration approval permission required.")


def _request_for_actor(db, request_id: str, actor: dict):
    item = db.execute("SELECT * FROM customer_registration_requests WHERE id=?", (request_id,)).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Customer registration request not found.")
    item = dict(item)
    if not actor["master"] and item.get("partner_id") != actor["partner_id"]:
        raise HTTPException(status_code=403, detail="This registration belongs to another tenant or is unassigned.")
    return item


def approve_registration(request_id: str, partner_id: str | None, actor: dict) -> dict:
    with connection() as db:
        if backend() == "sqlite":
            db.execute("BEGIN IMMEDIATE")
        else:
            db.execute("SELECT id FROM customer_registration_requests WHERE id=? FOR UPDATE", (request_id,))
        item = _request_for_actor(db, request_id, actor)
        if item["status"] == "approved":
            return {"status": "complete", "message": "Account was already approved.", "customer_id": item["customer_id"], "user_id": item["user_id"]}
        if item["status"] == "rejected":
            raise HTTPException(status_code=409, detail="A rejected registration cannot be approved.")
        # Owner decision 2026-10-17: never approve (and so never attach pending purchases to) an address nobody
        # has proven they own. The proof must be for THIS request's email.
        if (not item.get("email_verified_at")
                or (item.get("email_verified_for") or "").lower() != (item.get("email") or "").lower()):
            raise HTTPException(status_code=409, detail=(
                "This request's email address has not been confirmed yet. Ask the requester to use the "
                "confirmation link we emailed, or resend it from this page."))
        selected_partner = (partner_id or item.get("partner_id") or "").strip()
        if not selected_partner:
            raise HTTPException(status_code=422, detail="Select an existing partner before approval.")
        if not actor["master"] and selected_partner != actor["partner_id"]:
            raise HTTPException(status_code=403, detail="Cross-tenant approval is not allowed.")
        partner = db.execute("SELECT id FROM partners WHERE id=? AND approval_status='approved'", (selected_partner,)).fetchone()
        if not partner:
            raise HTTPException(status_code=422, detail="Select an approved partner.")
        if db.execute("SELECT 1 FROM partner_users WHERE lower(email)=?", (item["email"],)).fetchone() or db.execute("SELECT 1 FROM customers WHERE lower(email)=?", (item["email"],)).fetchone():
            raise HTTPException(status_code=409, detail="That email is already associated with an account.")
        now = _now(); customer_id = secrets.token_hex(16); user_id = secrets.token_hex(16)
        db.execute(
            "INSERT INTO customers(id,partner_id,name,company,email,status,source,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (customer_id, selected_partner, item["display_name"], item["display_name"], item["email"], "active", "real", now, actor["email"]),
        )
        db.execute(
            "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, selected_partner, item["email"], item["display_name"], "customer_owner", item["password_hash"], 1, customer_id, now, "active", "all"),
        )
        if not db.execute("SELECT 1 FROM identity_grants WHERE user_id=? AND role='customer_owner' AND scope_type='customer' AND scope_id=? AND revoked_at IS NULL", (user_id, customer_id)).fetchone():
            create_grant(db, user_id=user_id, role="customer_owner", scope_type="customer", scope_id=customer_id, granted_by=actor["email"], now=now)
        db.execute(
            "UPDATE customer_registration_requests SET status='approved',partner_id=?,customer_id=?,user_id=?,decided_at=?,decided_by=?,rejection_reason=NULL WHERE id=?",
            (selected_partner, customer_id, user_id, now, actor["email"], request_id),
        )
        db.execute(
            "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (actor["email"], "administrator" if actor["master"] else "partner_owner", "customer_registration.approved", "customer_registration", request_id, '{}', now),
        )
    # Provisioning Phase 1: this is the exact moment a brand-new customer's
    # authoritative identity (customers.id) first exists with a verified
    # email -- the safe point to attach any Stripe purchase that completed
    # under this email before the account existed (see customer_
    # entitlements.create_pending_link()/resolve_pending_links_for_
    # customer()'s own docstring for why this must only ever run against
    # an email the approval flow has already verified, never an
    # unauthenticated caller-supplied one). A brand-new approval can never
    # have a pending link to resolve except in this exact race, so this is
    # a no-op the overwhelming majority of the time; kept best-effort so a
    # customer's account approval is never blocked by it.
    try:
        from customer_entitlements import resolve_pending_links_for_customer
        resolve_pending_links_for_customer(customer_id, item["email"])
    except Exception:
        logging.getLogger("anyaicam.customer_registration").exception(
            "Failed to resolve pending entitlement links for newly approved customer %s", customer_id
        )
    # Same best-effort, checkout-before-registration reconciliation as
    # above, for one-time HARDWARE purchases (see hardware_orders.py) --
    # a separate call because it's a separate table/pending-link kind,
    # never conflated with camera-slot entitlements.
    try:
        from hardware_orders import resolve_pending_links_for_customer as resolve_pending_hardware_links
        resolve_pending_hardware_links(customer_id, item["email"])
    except Exception:
        logging.getLogger("anyaicam.customer_registration").exception(
            "Failed to resolve pending hardware order links for newly approved customer %s", customer_id
        )
    # Same best-effort, checkout-before-registration reconciliation as
    # above, for ANALYTICS add-on purchases (see analytics_entitlements.py) --
    # its own separate table/pending-link kind, never conflated with
    # camera-slot entitlements or hardware orders.
    try:
        from analytics_entitlements import resolve_pending_links_for_customer as resolve_pending_analytics_links
        resolve_pending_analytics_links(customer_id, item["email"])
    except Exception:
        logging.getLogger("anyaicam.customer_registration").exception(
            "Failed to resolve pending analytics links for newly approved customer %s", customer_id
        )
    # Provisioning Phase 6: now that any pending purchase (camera-slot
    # entitlement and/or hardware order) has been attached to this real
    # account, send the final customer-facing "ready"/order-confirmation
    # email(s) -- see purchase_notifications.py's module docstring for
    # why this uses its own idempotency key here rather than a Stripe
    # event id. Best-effort: a notification failure must never fail
    # account approval, which has already fully succeeded by this point.
    try:
        from purchase_notifications import notify_registration_resolved
        notify_registration_resolved(customer_id)
    except Exception:
        logging.getLogger("anyaicam.customer_registration").exception(
            "Failed to send post-registration purchase notifications for customer %s", customer_id
        )
    return {"status": "complete", "message": "Customer account approved.", "customer_id": customer_id, "user_id": user_id}


def assign_registration(request_id: str, partner_id: str, actor: dict) -> dict:
    if not actor["master"]:
        raise HTTPException(status_code=403, detail="Only a master administrator can assign an unassigned registration.")
    selected_partner = partner_id.strip()
    with connection() as db:
        item = _request_for_actor(db, request_id, actor)
        if item["status"] != "pending":
            raise HTTPException(status_code=409, detail="Only pending registrations can be assigned.")
        if not db.execute("SELECT 1 FROM partners WHERE id=? AND approval_status='approved'", (selected_partner,)).fetchone():
            raise HTTPException(status_code=422, detail="Select an approved partner.")
        now = _now()
        db.execute("UPDATE customer_registration_requests SET partner_id=? WHERE id=?", (selected_partner, request_id))
        db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)", (actor["email"], "administrator", "customer_registration.assigned", "customer_registration", request_id, "{}", now))
    return {"status": "complete", "message": "Registration assigned to partner.", "partner_id": selected_partner}


def reject_registration(request_id: str, reason: str, actor: dict) -> dict:
    with connection() as db:
        item = _request_for_actor(db, request_id, actor)
        now = _now()
        if item.get("user_id"):
            db.execute("UPDATE partner_users SET approved=0,account_status='revoked',authorization_version=authorization_version+1 WHERE id=?", (item["user_id"],))
            db.execute("UPDATE identity_grants SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, item["user_id"]))
        if item.get("customer_id"):
            db.execute("UPDATE customers SET status='suspended' WHERE id=?", (item["customer_id"],))
        db.execute("UPDATE customer_registration_requests SET status='rejected',decided_at=?,decided_by=?,rejection_reason=? WHERE id=?", (now, actor["email"], reason.strip()[:1000], request_id))
        db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)", (actor["email"], "administrator" if actor["master"] else "partner_owner", "customer_registration.rejected", "customer_registration", request_id, '{}', now))
    return {"status": "complete", "message": "Customer registration rejected."}


def register_customer_registration_routes(app, page_shell, current_user, is_master_admin) -> None:
    def actor(request):
        return _approval_identity(request, current_user, is_master_admin)

    @app.get("/api/customer-registration-requests")
    def list_requests(request: Request):
        who = actor(request)
        with connection() as db:
            if who["master"]:
                items = db.execute("SELECT * FROM customer_registration_requests ORDER BY requested_at DESC").fetchall()
            else:
                items = db.execute("SELECT * FROM customer_registration_requests WHERE partner_id=? ORDER BY requested_at DESC", (who["partner_id"],)).fetchall()
        hidden = {"password_hash", "verification_token_hash"}
        return {"requests": [{k: v for k, v in dict(row).items() if k not in hidden} for row in items]}

    @app.post("/api/customer-registration-requests/{request_id}/approve")
    def approve(request_id: str, request: Request, payload: dict):
        return approve_registration(request_id, str(payload.get("partner_id") or ""), actor(request))

    @app.post("/api/customer-registration-requests/{request_id}/assign")
    def assign(request_id: str, request: Request, payload: dict):
        return assign_registration(request_id, str(payload.get("partner_id") or ""), actor(request))

    @app.post("/api/customer-registration-requests/{request_id}/resend-verification")
    def resend_verification(request_id: str, request: Request):
        who = actor(request)
        with connection() as db:
            item = _request_for_actor(db, request_id, who)
        if item["status"] != "pending":
            raise HTTPException(status_code=409, detail="Only pending registrations can be confirmed.")
        if item.get("email_verified_at"):
            return {"status": "complete", "message": "This email address is already confirmed."}
        sent_at = item.get("verification_sent_at")
        try:
            recent = bool(sent_at) and (datetime.now() - datetime.fromisoformat(sent_at)).total_seconds() < VERIFY_RESEND_SECONDS
        except ValueError:
            recent = False
        if recent:  # Green review F4: no unthrottled mail to an address, even from a reviewer
            raise HTTPException(status_code=429, detail="A confirmation link was sent recently. Try again in a few minutes.")
        if not send_registration_verification(item["email"], force=True):
            raise HTTPException(status_code=503, detail="The confirmation email could not be sent. Please try again.")
        with connection() as db:
            db.execute(
                "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (who["email"], "administrator" if who["master"] else "partner_owner", "customer_registration.verification_resent",
                 "customer_registration", request_id, "{}", _now()))
        return {"status": "complete", "message": "A new confirmation link was emailed to the requester."}

    @app.get("/customer-register/verify-email", response_class=HTMLResponse)
    def verify_registration_email_form(token: str = ""):
        # Green review F2: a GET never changes anything -- mail scanners prefetch links. It only shows the form;
        # it does not say whether the token is valid.
        return _verify_page("Confirm your email", _verify_form(token))

    @app.post("/customer-register/verify-email", response_class=HTMLResponse)
    def verify_registration_email_submit(token: str = Form(""), password: str = Form(""), confirm_password: str = Form(""),
                                         display_name: str = Form("")):
        if not 2 <= len(display_name.strip()) <= 120:
            return _verify_page("Confirm your email", _verify_form(token, "Enter your full name."), status_code=422)
        if len(password) < MIN_PASSWORD or password != confirm_password:
            error = ("Password must contain at least 10 characters." if len(password) < MIN_PASSWORD
                     else "The two passwords do not match.")
            return _verify_page("Confirm your email", _verify_form(token, error), status_code=422)
        if verify_registration_email(token, password, display_name):
            return _verify_page("Email confirmed", f"<p>{VERIFIED_MESSAGE}</p>"
                                '<p><a href="/customer-login.html">Go to sign in</a></p>')
        return _verify_page("Link not valid", f"<p>{VERIFY_INVALID_MESSAGE}</p>", status_code=400)

    @app.post("/api/customer-registration-requests/{request_id}/reject")
    def reject(request_id: str, request: Request, payload: dict):
        return reject_registration(request_id, str(payload.get("reason") or ""), actor(request))

    @app.get("/customer-registration-requests", response_class=HTMLResponse)
    def requests_page(request: Request):
        actor(request)
        content = '''<header class="topbar"><div><p class="eyebrow">Customer access</p><h1>Registration requests</h1></div></header><section class="panel"><p class="health-detail">Unassigned public requests require a master administrator to select an existing partner. Partner administrators see only requests assigned to their tenant.</p><div id="registration-requests">Loading…</div></section>'''
        scripts = '''<script>const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));async function loadRegistrations(){const r=await fetch('/api/customer-registration-requests'),p=await r.json(),box=document.getElementById('registration-requests');box.innerHTML=(p.requests||[]).map(x=>`<article class="feature-card"><strong>${esc(x.display_name)}</strong> <small>${x.email_verified_at?'(name set by the confirmed email owner)':'(name NOT verified -- typed by the applicant)'}</small><p>${esc(x.email)} · ${esc(x.status)} · ${x.email_verified_at?'email confirmed':'email NOT confirmed'}</p><label>Existing partner ID<input id="partner-${x.id}" value="${esc(x.partner_id)}"></label><button onclick="decide('${x.id}','assign')">Assign</button> <button onclick="decide('${x.id}','approve')">Approve</button> <button onclick="decide('${x.id}','resend-verification')">Resend confirmation</button> <button onclick="decide('${x.id}','reject')">Reject</button></article>`).join('')||'<div class="empty">No registration requests.</div>'}async function decide(id,action){const partner=document.getElementById('partner-'+id)?.value||'',reason=action==='reject'?prompt('Rejection reason')||'Rejected':'';const r=await fetch(`/api/customer-registration-requests/${id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({partner_id:partner,reason})}),p=await r.json();showToast(p.message||p.detail);if(r.ok)loadRegistrations()}loadRegistrations();</script>'''
        return page_shell("Customer registrations", "business-users", content, scripts=scripts)
