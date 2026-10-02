"""Single-use, expiring invitation links for accounts created by someone
else (2026-10-02, Codex finding): partner-assisted customer onboarding,
partner/customer user invitations and approved partner applications.

Those flows created the account with a temporary password, stored it inside
invitations.email_preview and returned it from the API even after the email
was delivered; some invitations never expired. Now, mirroring household
invitations (household_users.py):

- the account is created with an unusable random password -- nobody,
  including the inviter, ever knows a password for it;
- the invitation stores only the SHA-256 of a random link token
  (token_hash), expires after INVITE_TTL_DAYS, and names the account it
  opens (invitee_user_id);
- the raw link goes into the email only; the API returns it once, for a
  manual handoff, only when the email was not delivered;
- the link is single use: accepting it sets the invitee's own password and
  consumes it in one transaction; a used, expired or unknown link is
  refused, indistinguishably.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from html import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from appliance_protocol import RateLimiter
from partner_db import audit, connection, password_hash

INVITE_TTL_DAYS = 7
MIN_PASSWORD_LENGTH = 12
ACCEPTABLE_STATUSES = ("pending", "sent", "preview", "queued", "failed", "error")
CUSTOMER_ROLES = ("customer_owner", "customer_viewer")
_accept_limiter = RateLimiter(limit=20, window_seconds=900)


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def unusable_password_hash() -> str:
    """A real hash of a random secret nobody keeps: the account cannot be
    signed into until its invitation is accepted."""
    return password_hash(secrets.token_urlsafe(32))


def issue(db, *, invitation_id: str, email: str, role: str, customer_id: str | None, user_id: str,
          created_by: str, now: datetime, preview: str) -> tuple[str, str]:
    """Insert the invitation; returns (raw_token, expires_at). `preview` must
    not contain the link or any secret."""
    raw = secrets.token_urlsafe(32)
    expires_at = (now + timedelta(days=INVITE_TTL_DAYS)).isoformat()
    db.execute("INSERT INTO invitations(id,email,role,customer_id,status,email_preview,expires_at,created_at,created_by,token_hash,"
               "invitee_user_id,last_sent_at,send_count) VALUES(?,?,?,?,'pending',?,?,?,?,?,?,?,1)",
               (invitation_id, email, role, customer_id, preview, expires_at, now.isoformat(), created_by, token_hash(raw),
                user_id, now.isoformat()))
    return raw, expires_at


def link(base: str, raw: str) -> str:
    return f"{(base or '').rstrip('/')}/accept-invitation?token={raw}"


def public_base(request: Request | None = None) -> str:
    import os
    base = os.environ.get("ANYAICAM_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return base
    try:
        return f"{request.url.scheme}://{request.url.netloc}" if request is not None else ""
    except AttributeError:
        return ""


def _live(db, raw: str, now: datetime) -> dict | None:
    if not raw or len(raw) > 200:
        return None
    row = db.execute(
        "SELECT * FROM invitations WHERE token_hash=? AND invitee_user_id IS NOT NULL AND accepted_at IS NULL "
        f"AND status IN ({','.join('?' for _ in ACCEPTABLE_STATUSES)}) AND expires_at>=?",
        (token_hash(raw), *ACCEPTABLE_STATUSES, now.isoformat()),
    ).fetchone()
    return dict(row) if row else None


def accept(db, *, raw: str, password: str, now: datetime) -> dict:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters.")
    invitation = _live(db, raw, now)
    refused = "This invitation link is invalid, expired, or already used."
    if not invitation:
        raise ValueError(refused)
    consumed = db.execute("UPDATE invitations SET status='accepted',accepted_at=?,accepted_user_id=invitee_user_id "
                          "WHERE id=? AND accepted_at IS NULL AND token_hash=?",
                          (now.isoformat(), invitation["id"], token_hash(raw))).rowcount
    if consumed != 1:
        raise ValueError(refused)
    updated = db.execute("UPDATE partner_users SET password_hash=?,must_change_password=0,approved=1,"
                         "authorization_version=COALESCE(authorization_version,1)+1 WHERE id=? AND lower(email)=lower(?)",
                         (password_hash(password), invitation["invitee_user_id"], invitation["email"])).rowcount
    if updated != 1:
        raise ValueError(refused)
    return invitation


def register_account_invitation_routes(app: FastAPI) -> None:
    @app.post("/api/invitations/accept")
    def accept_route(request: Request, payload: dict) -> dict:
        client_ip = request.client.host if request.client else "unknown"
        if not _accept_limiter.allow(client_ip):
            raise HTTPException(status_code=429, detail="Too many attempts. Please wait a few minutes and try again.")
        password = str(payload.get("password", ""))
        if password != str(payload.get("confirm_password", password)):
            raise HTTPException(status_code=400, detail="The two passwords don't match.")
        try:
            with connection() as db:
                invitation = accept(db, raw=str(payload.get("token", "")), password=password, now=datetime.now())
        except ValueError as error:
            audit({"email": "unknown", "role": "anonymous"}, "invitation.accept_failed", "invitation", "", {"reason": str(error)[:80]})
            raise HTTPException(status_code=400, detail=str(error)) from error
        audit({"email": invitation["email"], "role": invitation["role"]}, "invitation.accepted", "invitation", invitation["id"])
        destination = "/customer-login.html" if invitation["role"] in CUSTOMER_ROLES else "/partner-login"
        return {"message": "Your password is set. Sign in with your email and new password.", "destination": destination}

    @app.get("/accept-invitation", response_class=HTMLResponse)
    def accept_page(request: Request, token: str = ""):
        with connection() as db:
            invitation = _live(db, token, datetime.now())
        return HTMLResponse(_page(invitation, token), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _page(invitation: dict | None, token: str) -> str:
    if invitation:
        body = ('<h2>Set your AnyAiCam password</h2>'
                f'<p>Create a password for <strong>{escape(invitation["email"])}</strong>.</p>'
                f'<form id="accept-form"><input id="accept-token" type="hidden" value="{escape(token, quote=True)}">'
                f'<label>New password<input id="accept-password" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>'
                f'<label>Confirm password<input id="accept-confirm" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>'
                f'<p style="margin:0;color:#4b5873;font-size:14px">At least {MIN_PASSWORD_LENGTH} characters.</p>'
                '<div id="message" class="message" role="status"></div><button class="submit">Set password</button></form>')
    else:
        body = ("<h2>This invitation can't be used</h2><p>The link is invalid, has expired, or was already used. "
                "Ask the person who invited you to send a new invitation.</p>")
    script = ("const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';"
              "let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='\"'&&v[v.length-1]==='\"'?v.slice(1,-1):v};"
              "const form=document.getElementById('accept-form');if(form)form.addEventListener('submit',async e=>{e.preventDefault();"
              "const msg=document.getElementById('message'),btn=form.querySelector('button');btn.disabled=true;"
              "const r=await fetch('/api/invitations/accept',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},"
              "body:JSON.stringify({token:document.getElementById('accept-token').value,password:document.getElementById('accept-password').value,"
              "confirm_password:document.getElementById('accept-confirm').value})}),b=await r.json().catch(()=>({}));"
              "msg.style.display='block';msg.textContent=b.message||b.detail||'Something went wrong. Please try again.';"
              "if(r.ok){form.querySelectorAll('input').forEach(i=>i.disabled=true);setTimeout(()=>location.href=b.destination||'/customer-login.html',1500)}else btn.disabled=false});")
    try:
        from cloud_features import CUSTOMER_AUTH_STYLE as style
    except Exception:
        style = ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><title>Set your password | ANY AI CAM</title>'
            f'<style>{style}</style></head><body>'
            '<header class="head"><a class="brand" href="/customer-login.html"><img src="/static/brand-icon.png" alt="AnyAiCam">ANY AI CAM</a></header>'
            f'<main class="auth-wrap"><section class="card">{body}</section></main><script>{script}</script></body></html>')
