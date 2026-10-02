"""Direct self-service customer onboarding (2026-10-02 launch requirement).

Website -> create account -> verify email -> choose Local/Hybrid -> Stripe
checkout -> entitlement -> My subscription -> licensed installer -> claim.

This coexists with partner-assisted onboarding, which is unchanged: a
/customer-register request still waits for administrator approval and a
partner assignment (customer_registration.py), and partners still onboard
their own customers (partner_workspace.py).

A direct customer:
- needs no administrator approval and no partner assignment to buy;
- proves they own the email first: the account does not exist until the
  emailed single-use link (SHA-256 stored, 24-hour expiry) is followed;
- is recorded explicitly as a direct/house customer: AnyAiCam's own house
  partner (HOUSE_PARTNER_ID) plus customers.onboarding_channel='direct' --
  never silently placed under a reseller -- and with no salesperson
  attribution (created_by is not a sales user, so sales_commissions finds
  none) unless a legitimate referral is recorded later;
- gets exactly one identity: an email already used by any account, customer
  or pending request is refused, and a verification link is consumed once.

Purchase, entitlement, licensing and installer download are the existing
flows (camera-slot checkout + Stripe webhook, customer_entitlements,
customer_downloads) -- no second checkout. Friends & Family rules apply
unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta
from html import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from appliance_protocol import RateLimiter
from partner_db import audit, connection, password_hash

logger = logging.getLogger("anyaicam.direct_onboarding")
HOUSE_PARTNER_ID = "anyaicam-primary"
DIRECT_CHANNEL = "direct"
VERIFY_TTL_HOURS = 24
MIN_PASSWORD_LENGTH = 12
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_signup_limiter = RateLimiter(limit=10, window_seconds=900)
_verify_limiter = RateLimiter(limit=30, window_seconds=900)
GENERIC_SENT = "Check your email for a link to confirm your address and finish creating your account."


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _email_in_use(db, email: str) -> bool:
    return bool(
        db.execute("SELECT 1 FROM partner_users WHERE lower(email)=?", (email,)).fetchone()
        or db.execute("SELECT 1 FROM customers WHERE lower(email)=?", (email,)).fetchone()
        or db.execute("SELECT 1 FROM customer_registration_requests WHERE lower(email)=? AND status IN ('pending','approved')", (email,)).fetchone()
    )


def start_signup(db, *, name: str, email: str, password: str, now: datetime) -> str | None:
    """Records a pending, unverified signup and returns the raw link token,
    or None when the email is already in use (the caller answers the same
    way either way, so this cannot be used to probe for accounts)."""
    email = email.strip().lower()
    if not name.strip() or len(name) > 120:
        raise ValueError("Enter your name.")
    if not _EMAIL.match(email) or len(email) > 254:
        raise ValueError("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters.")
    if _email_in_use(db, email):
        return None
    raw = secrets.token_urlsafe(32)
    expires = (now + timedelta(hours=VERIFY_TTL_HOURS)).isoformat()
    # One live pending signup per email: a repeated signup replaces the link.
    db.execute("DELETE FROM direct_signups WHERE email=? AND verified_at IS NULL", (email,))
    db.execute("INSERT INTO direct_signups(id,email,name,password_hash,token_hash,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
               (secrets.token_hex(16), email, name.strip(), password_hash(password), _hash(raw), expires, now.isoformat()))
    return raw


def verify_signup(db, *, raw: str, now: datetime) -> dict:
    """Consumes the link and creates the direct customer, its house-partner
    placement, the owner account and its grant -- one transaction."""
    refused = "This link is invalid, expired, or already used."
    if not raw or len(raw) > 200:
        raise ValueError(refused)
    pending = db.execute("SELECT * FROM direct_signups WHERE token_hash=? AND verified_at IS NULL AND expires_at>=?",
                         (_hash(raw), now.isoformat())).fetchone()
    if not pending:
        raise ValueError(refused)
    if db.execute("UPDATE direct_signups SET verified_at=? WHERE id=? AND verified_at IS NULL", (now.isoformat(), pending["id"])).rowcount != 1:
        raise ValueError(refused)
    email = pending["email"]
    if _email_in_use(db, email):  # someone else got there first (e.g. a partner onboarded them)
        raise ValueError("That email is already associated with an account. Sign in instead.")
    customer_id, user_id, site_id = secrets.token_hex(16), secrets.token_hex(16), secrets.token_hex(8)
    stamp = now.isoformat()
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",
               (HOUSE_PARTNER_ID, "AnyAiCam", "approved", "real", stamp))
    db.execute("INSERT INTO customers(id,partner_id,name,company,email,status,source,created_at,created_by,onboarding_channel) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (customer_id, HOUSE_PARTNER_ID, pending["name"], pending["name"], email, "active", "real", stamp, "direct-signup", DIRECT_CHANNEL))
    db.execute("INSERT INTO sites(id,customer_id,name,site_type,created_at) VALUES(?,?,?,?,?)",
               (site_id, customer_id, "Home", "Customer site", stamp))
    db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
               "VALUES(?,?,?,?,'customer_owner',?,1,?,?,'active','all')",
               (user_id, HOUSE_PARTNER_ID, email, pending["name"], pending["password_hash"], customer_id, stamp))
    from appliance_identity import create_grant
    create_grant(db, user_id=user_id, role="customer_owner", scope_type="customer", scope_id=customer_id,
                 granted_by="direct-signup", now=stamp)
    db.execute("UPDATE direct_signups SET customer_id=?,user_id=? WHERE id=?", (customer_id, user_id, pending["id"]))
    return {"customer_id": customer_id, "user_id": user_id, "email": email}


def _base(request: Request) -> str:
    import os
    base = os.environ.get("ANYAICAM_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return base
    try:
        return f"{request.url.scheme}://{request.url.netloc}"
    except AttributeError:
        return ""


def register_direct_onboarding_routes(app: FastAPI) -> None:
    @app.post("/api/customer/direct-signup")
    def direct_signup(request: Request, payload: dict) -> dict:
        client_ip = request.client.host if request.client else "unknown"
        if not _signup_limiter.allow(client_ip):
            raise HTTPException(status_code=429, detail="Too many attempts. Please wait a few minutes and try again.")
        password = str(payload.get("password", ""))
        if password != str(payload.get("confirm_password", password)):
            raise HTTPException(status_code=400, detail="The two passwords don't match.")
        try:
            with connection() as db:
                raw = start_signup(db, name=str(payload.get("name", "")), email=str(payload.get("email", "")),
                                   password=password, now=datetime.now())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        email = str(payload.get("email", "")).strip().lower()
        from email_service import get_email_service
        if raw:
            link = f"{_base(request)}/customer/verify-email?token={raw}"
            subject = "Confirm your AnyAiCam account"
            text = (f"Confirm your email address to finish creating your AnyAiCam account:\n{link}\n\n"
                    f"This link works once and expires in {VERIFY_TTL_HOURS} hours. If you didn't ask for this, ignore this email.")
        else:
            subject = "Your AnyAiCam account"
            text = ("Someone tried to create an AnyAiCam account with this email address, which already has one. "
                    f"Sign in or reset your password instead: {_base(request)}/customer-login.html\n\n"
                    "If this wasn't you, no action is needed.")
        try:
            result = get_email_service().send("email_verification", email, subject, text)
        except Exception as error:  # mail server down: say so plainly; a retry replaces the pending link
            logger.warning("direct_signup.email_failed error=%s", type(error).__name__)
            result = {"status": "failed"}
        if isinstance(result, dict) and result.get("status") in ("failed", "error"):
            raise HTTPException(status_code=503, detail="We couldn't send the confirmation email just now. Please try again in a few minutes.")
        if raw:
            audit({"email": email, "role": "anonymous"}, "direct_signup.started", "direct_signup", "")
        return {"message": GENERIC_SENT}

    @app.get("/customer/verify-email", response_class=HTMLResponse)
    def verify_email(request: Request, token: str = ""):
        client_ip = request.client.host if request.client else "unknown"
        if not _verify_limiter.allow(client_ip):
            return HTMLResponse(_page("Please wait", "Too many attempts. Please wait a few minutes and try again."), status_code=429)
        try:
            with connection() as db:
                created = verify_signup(db, raw=token, now=datetime.now())
        except ValueError as error:
            return HTMLResponse(_page("This link can't be used", str(error)), status_code=400,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        audit({"email": created["email"], "role": "customer_owner"}, "direct_signup.verified", "customer", created["customer_id"],
              {"channel": DIRECT_CHANNEL})
        return HTMLResponse(_page("Your account is ready", "Sign in, then choose Local or Hybrid on My subscription.",
                                  link=("/customer-login.html", "Sign in")),
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/customer-signup", response_class=HTMLResponse)
    def signup_page(request: Request):
        form = ('<h2>Create your AnyAiCam account</h2><p>Buy directly from AnyAiCam. We\'ll email you a link to confirm your address.</p>'
                '<form id="signup-form"><label>Name<input id="s-name" autocomplete="name" required></label>'
                '<label>Email<input id="s-email" type="email" autocomplete="email" required></label>'
                f'<label>Password<input id="s-password" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>'
                f'<label>Confirm password<input id="s-confirm" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>'
                f'<p style="margin:0;color:#4b5873;font-size:14px">At least {MIN_PASSWORD_LENGTH} characters.</p>'
                '<div id="message" class="message" role="status"></div><button class="submit">Create account</button>'
                '<p style="margin:0;color:#4b5873;font-size:13px">By creating an account you agree to the AnyAiCam '
                '<a href="https://anyaicam.com/terms.html" target="_blank" rel="noopener">Terms of Service</a> and '
                '<a href="https://anyaicam.com/privacy-policy.html" target="_blank" rel="noopener">Privacy Policy</a>.</p></form>'
                '<p style="font-size:14px">Already have an account? <a href="/customer-login.html">Sign in</a></p>'
                '<p style="font-size:14px">Working with an installer? <a href="/customer-register">Request an account through your installer</a>.</p>')
        script = ("const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';"
                  "let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='\"'&&v[v.length-1]==='\"'?v.slice(1,-1):v};"
                  "document.getElementById('signup-form').addEventListener('submit',async e=>{e.preventDefault();const msg=document.getElementById('message'),btn=e.target.querySelector('button');btn.disabled=true;"
                  "const r=await fetch('/api/customer/direct-signup',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({"
                  "name:document.getElementById('s-name').value,email:document.getElementById('s-email').value,password:document.getElementById('s-password').value,"
                  "confirm_password:document.getElementById('s-confirm').value})}),b=await r.json().catch(()=>({}));msg.style.display='block';"
                  "msg.textContent=b.message||b.detail||'Something went wrong. Please try again.';"
                  "if(r.ok){msg.style.background='#e7f6ec';msg.style.color='#14532d';e.target.querySelectorAll('input').forEach(i=>i.disabled=true)}else btn.disabled=false});")
        return HTMLResponse(_page(None, None, body=form, script=script))


def _page(title: str | None, text: str | None, *, link=None, body: str | None = None, script: str = "") -> str:
    try:
        from cloud_features import CUSTOMER_AUTH_STYLE as style
    except Exception:
        style = ""
    if body is None:
        body = f"<h2>{escape(title or '')}</h2><p>{escape(text or '')}</p>"
        if link:
            body += f'<a class="submit" href="{escape(link[0], quote=True)}">{escape(link[1])}</a>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><title>Create your account | ANY AI CAM</title>'
            f'<style>{style}</style></head><body>'
            '<header class="head"><a class="brand" href="/customer-login.html"><img src="/static/brand-icon.png" alt="AnyAiCam">ANY AI CAM</a></header>'
            f'<main class="auth-wrap"><section class="card">{body}</section></main><script>{script}</script></body></html>')
