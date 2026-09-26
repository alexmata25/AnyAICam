import csv
import io
import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable

from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,HTMLResponse,StreamingResponse

from cloud_config import settings
from cloud_security import consume_password_reset,create_password_reset
from appliance_protocol import RateLimiter
from email_service import get_email_service
from object_storage import LocalStorage,get_storage,safe_key
from partner_db import audit,authorize_customer_tenant,connection,require_permission,row,rows,tenant_owns_partner
from partner_portal import partner_identity,require_partner_access


# Anonymous recovery requests deliberately receive the same generic response
# whether or not the account exists.  These limits bound email abuse while
# leaving the lockout policy itself unchanged.
_password_reset_email_limiter = RateLimiter(limit=3, window_seconds=900)
_password_reset_ip_limiter = RateLimiter(limit=30, window_seconds=900)
# Completing a reset is unauthenticated and costs a PBKDF2 verification;
# bounded per client IP (2026-09-24). Generous enough for a real user
# retrying a mistyped password several times.
_password_reset_complete_ip_limiter = RateLimiter(limit=20, window_seconds=900)

CUSTOMER_RESET_ROLES = ('customer_owner', 'customer_viewer')


def customer_reset_url() -> str:
    """The customer-branded reset page on the configured public portal.
    ANYAICAM_PASSWORD_RESET_URL points at the partner/admin reset page
    (/reset-password, rendered in the Admin/Partner portal chrome); a
    customer must stay in the customer experience, so their links use
    /customer-reset-password on the same origin (2026-09-24 -- previously
    only the edge_production path did this)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(settings.password_reset_url)
    path = parts.path
    if path.endswith('/reset-password'):
        path = path[: -len('/reset-password')] + '/customer-reset-password'
    else:
        path = '/customer-reset-password'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def deployment_status():
    checks={'configuration':'ok','database':'pending','storage':'pending','email':'pending','public_urls':'pending'}
    try: settings.validate()
    except Exception as error: checks['configuration']=str(error)
    try:
        with connection() as db: db.execute('SELECT 1').fetchone()
        checks['database']='ok'
    except Exception as error: checks['database']=f'error: {type(error).__name__}'
    try:
        storage=get_storage(); checks['storage']='ok: '+type(storage).__name__
    except Exception as error: checks['storage']=f'error: {type(error).__name__}'
    checks['email']='ok: '+settings.email_backend
    urls=[settings.public_website_url,settings.portal_url,settings.api_base_url,settings.partner_login_url,settings.customer_login_url]
    checks['public_urls']='ok' if all(value.startswith(('http://','https://')) for value in urls) else 'error'
    return {'status':'ready' if all(str(value).startswith('ok') for value in checks.values()) else 'degraded','environment':settings.environment,'checks':checks}


def register_cloud_feature_routes(app: FastAPI,shell: Callable):
    @app.get('/health/live')
    def liveness(): return {'status':'alive'}

    @app.get('/health/ready')
    def readiness():
        status=deployment_status()
        if status['checks']['configuration']!='ok' or status['checks']['database']!='ok': raise HTTPException(status_code=503,detail=status)
        return status

    @app.get('/api/admin/deployment-verification')
    def verify_deployment(request: Request):
        require_partner_access(request,{'administrator'}); return deployment_status()

    @app.get('/api/runtime-config')
    def runtime_config():
        return {'environment':settings.environment,'database_backend':settings.database_backend,'storage_backend':settings.storage_backend,'email_backend':settings.email_backend,'public_website_url':settings.public_website_url,'portal_url':settings.portal_url,'api_base_url':settings.api_base_url,'partner_login_url':settings.partner_login_url,'customer_login_url':settings.customer_login_url,'https_only':settings.https_only,'csrf_enabled':settings.csrf_enabled}

    @app.get('/storage/{category}/{object_key:path}')
    def local_storage_file(category: str,object_key: str):
        if settings.storage_backend!='local': raise HTTPException(status_code=404,detail='Local storage backend is disabled.')
        path=LocalStorage().path(category,object_key)
        if not path.exists(): raise HTTPException(status_code=404,detail='Stored object not found.')
        return FileResponse(path)

    @app.post('/api/password-reset/request')
    def password_reset_request(payload: dict,request: Request):
        email=str(payload.get('email','')).strip().lower(); client_ip=(request.client.host if request.client else 'unknown')
        allowed=_password_reset_email_limiter.allow(email) and _password_reset_ip_limiter.allow(client_ip)
        user=row('SELECT id,email,role FROM partner_users WHERE email=?',(email,)) if allowed else None
        if user:
            raw=create_password_reset(user['id'],email)
            # Confirmed live on Samsung: settings.password_reset_url defaults
            # to http://localhost:8000/reset-password, an install-time-fixed
            # value -- for a cloud deployment with one real public domain
            # that's exactly right and stays exactly as-is below. An edge
            # appliance has no such fixed address (LAN IP, Tailscale IP,
            # mDNS name -- whatever DHCP/Tailscale happens to assign), so a
            # link built from that fixed default pointed at "localhost" no
            # matter which real address the requester's own browser was
            # actually using, forcing error-prone manual URL editing (swap
            # in the real host, keep the ~43-character token intact by
            # hand) before every single reset -- the repeated "token
            # invalid/expired" reports tonight were exactly this, with a
            # token that was independently confirmed valid, unused, and
            # unexpired in the database every time. For edge_production,
            # the link is instead built from this exact request's own Host
            # header -- already trusted (TrustedHostMiddleware accepts any
            # host for edge_production, see cloud_config.py's effective_
            # trusted_hosts), and guaranteed to be the address that will
            # actually work for whoever just submitted this request.
            if settings.edge_production:
                scheme=request.headers.get('x-forwarded-proto',request.url.scheme)
                host=request.headers.get('host') or request.url.netloc
                reset_path='/customer-reset-password' if user['role'] in ('customer_owner','customer_viewer') else '/reset-password'
                link=f'{scheme}://{host}{reset_path}?token={raw}'
            else:
                base=customer_reset_url() if user['role'] in CUSTOMER_RESET_ROLES else settings.password_reset_url
                link=base+'?token='+raw
            message=get_email_service().send('password_reset',email,'Reset your AnyAiCam password',f'Use this one-hour password reset link:\n{link}',metadata={'expires_minutes':60})
            with connection() as db: db.execute('INSERT INTO email_messages(id,message_type,recipient,status,provider,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)',(message.get('id',datetime.now().strftime('%Y%m%d%H%M%S%f')),'password_reset',email,message['status'],settings.email_backend,json.dumps({'expires_minutes':60}),datetime.now().isoformat()))
            audit({'email':email,'role':'account'},'password_reset.requested','partner_user',user['id'],{'provider':settings.email_backend})
        return {'message':'If the account exists, a password-reset message has been prepared.'}

    @app.post('/api/partner/customers/{customer_id}/accounts/{user_id}/unlock')
    def unlock_customer_account(request: Request,customer_id: str,user_id: str):
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.edit')
        except PermissionError as error: raise HTTPException(status_code=403,detail='Customer account management permission is required.') from error
        with connection() as db:
            customer=authorize_customer_tenant(db,identity,customer_id)
            if not customer:
                raise HTTPException(status_code=404,detail='Customer not found.')
            user=db.execute("SELECT id,email FROM partner_users WHERE id=? AND customer_id=? AND role IN ('customer_owner','customer_viewer')",(user_id,customer_id)).fetchone()
            if not user:
                raise HTTPException(status_code=404,detail='Customer account not found.')
            deleted=db.execute('DELETE FROM account_lockouts WHERE email=?',(user['email'].lower(),)).rowcount
        audit(identity,'customer_account.unlocked','partner_user',user_id,{'customer_id':customer_id,'lockout_cleared':bool(deleted)})
        return {'message':'Customer account lockout cleared. The password and customer access were not changed.','lockout_cleared':bool(deleted)}

    @app.post('/api/partner/customers/{customer_id}/accounts/{user_id}/reset-password')
    def initiate_customer_password_reset(request: Request,customer_id: str,user_id: str):
        # Admin-authorized password reset/initiation (2026-09-17): the
        # audit for this milestone confirmed unlock_customer_account()
        # immediately above is the ONLY existing admin-facing customer-
        # account control -- there was no way for a partner/admin to
        # start a password reset on a customer's behalf (e.g. a customer
        # who's locked out of the email address on file, or who calls
        # support asking for a reset). Same tenant-scoped-permission
        # shape as that route, and reuses create_password_reset()
        # unchanged -- this produces the exact same kind of single-use,
        # one-hour, hashed-at-rest token the self-service /forgot-
        # password flow already does; nothing about the token's own
        # security properties is special-cased for this admin-initiated
        # path.
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.edit')
        except PermissionError as error: raise HTTPException(status_code=403,detail='Customer account management permission is required.') from error
        with connection() as db:
            customer=authorize_customer_tenant(db,identity,customer_id)
            if not customer:
                raise HTTPException(status_code=404,detail='Customer not found.')
            user=db.execute("SELECT id,email FROM partner_users WHERE id=? AND customer_id=? AND role IN ('customer_owner','customer_viewer')",(user_id,customer_id)).fetchone()
            if not user:
                raise HTTPException(status_code=404,detail='Customer account not found.')
        raw=create_password_reset(user['id'],user['email'])
        # Same edge_production vs fixed-URL link-building rule as the
        # self-service request route above -- an admin-initiated reset on
        # an edge appliance must land the customer back on THAT
        # appliance's own real address, never a fixed "localhost" default.
        if settings.edge_production:
            scheme=request.headers.get('x-forwarded-proto',request.url.scheme)
            host=request.headers.get('host') or request.url.netloc
            link=f'{scheme}://{host}/customer-reset-password?token={raw}'
        else:
            # Always a customer account here (see the role filter above).
            link=customer_reset_url()+'?token='+raw
        message=get_email_service().send('password_reset',user['email'],'Reset your AnyAiCam password',f'An administrator started a password reset for your account. Use this one-hour link to set a new password:\n{link}',metadata={'expires_minutes':60,'initiated_by':'admin'})
        with connection() as db: db.execute('INSERT INTO email_messages(id,message_type,recipient,status,provider,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)',(message.get('id',datetime.now().strftime('%Y%m%d%H%M%S%f')),'password_reset',user['email'],message['status'],settings.email_backend,json.dumps({'expires_minutes':60,'initiated_by':'admin'}),datetime.now().isoformat()))
        audit(identity,'customer_account.password_reset_initiated','partner_user',user_id,{'customer_id':customer_id,'provider':settings.email_backend})
        return {'message':'Password-reset message sent to the account on file. The customer\'s current password remains unchanged until they complete the reset.'}

    @app.post('/api/partner/customers/{customer_id}/accounts/{user_id}/change-email')
    def change_customer_account_email(request: Request,customer_id: str,user_id: str,payload: dict):
        # Admin-initiated email change (2026-09-17): the Password Recovery +
        # Account Controls audit confirmed unlock and admin-initiated
        # password reset (both immediately above) were the only two
        # existing admin-facing customer-account controls -- there was no
        # way for a partner/admin to correct a customer's login email on
        # their behalf (e.g. a typo at signup, or a customer who lost
        # access to their old address and can't complete a self-service
        # flow that depends on it). Same tenant-scoped-permission shape as
        # its two siblings. Deliberately an immediate change, not a
        # verify-the-new-address-first flow: this account already required
        # a human support interaction to reach an administrator in the
        # first place (unlike self-service signup, which does verify),
        # matching the same immediacy the sibling unlock/reset-password
        # actions already have.
        from notification_preferences import is_valid_email
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.edit')
        except PermissionError as error: raise HTTPException(status_code=403,detail='Customer account management permission is required.') from error
        new_email=str(payload.get('new_email','')).strip().lower()
        if not is_valid_email(new_email):
            raise HTTPException(status_code=400,detail='A valid new email address is required.')
        with connection() as db:
            customer=authorize_customer_tenant(db,identity,customer_id)
            if not customer:
                raise HTTPException(status_code=404,detail='Customer not found.')
            user=db.execute("SELECT id,email FROM partner_users WHERE id=? AND customer_id=? AND role IN ('customer_owner','customer_viewer')",(user_id,customer_id)).fetchone()
            if not user:
                raise HTTPException(status_code=404,detail='Customer account not found.')
            old_email=user['email']
            if new_email==old_email.lower():
                raise HTTPException(status_code=400,detail='The new email must be different from the current one.')
            # Login is looked up by email (partner_authenticate_detailed()
            # and friends) -- a duplicate would make one of the two
            # accounts unreachable rather than raising a clean error at
            # sign-in time, so this must be checked and rejected here,
            # not discovered later as a support ticket.
            if db.execute('SELECT 1 FROM partner_users WHERE email=?',(new_email,)).fetchone():
                raise HTTPException(status_code=409,detail='That email address is already in use by another account.')
            db.execute('UPDATE partner_users SET email=? WHERE id=?',(new_email,user['id']))
        # Security notice to the OLD address, not the new one -- the
        # standard "your account email was changed" pattern: if this
        # change was not actually requested by the account owner, the
        # notice needs to reach the address an attacker (or a support
        # mistake) just moved away from, not the one they moved to.
        message=get_email_service().send('account_email_changed',old_email,'Your AnyAiCam account email was changed',f'An administrator changed the email address on your AnyAiCam account from {old_email} to {new_email}. If you did not request this, contact support immediately.',metadata={'old_email':old_email,'new_email':new_email,'initiated_by':'admin'})
        with connection() as db: db.execute('INSERT INTO email_messages(id,message_type,recipient,status,provider,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)',(message.get('id',datetime.now().strftime('%Y%m%d%H%M%S%f')),'account_email_changed',old_email,message['status'],settings.email_backend,json.dumps({'old_email':old_email,'new_email':new_email,'initiated_by':'admin'}),datetime.now().isoformat()))
        audit(identity,'customer_account.email_changed','partner_user',user_id,{'customer_id':customer_id,'old_email':old_email,'new_email':new_email})
        return {'message':'Email address updated. The customer should sign in with the new email address from now on.','old_email':old_email,'new_email':new_email}

    @app.get('/api/partner/customer-accounts')
    def customer_accounts(request: Request):
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.edit')
        except PermissionError as error: raise HTTPException(status_code=403,detail='Customer account management permission is required.') from error
        with connection() as db:
            customers=[dict(item) for item in db.execute('SELECT id,name,email,partner_id FROM customers ORDER BY name').fetchall()]
            permitted=[item for item in customers if tenant_owns_partner(db,identity,item['partner_id'])]
            accounts=[]
            for customer in permitted:
                for user in db.execute("SELECT id,email,name,role FROM partner_users WHERE customer_id=? AND role IN ('customer_owner','customer_viewer') ORDER BY email",(customer['id'],)).fetchall():
                    account=dict(user); account['customer_id']=customer['id']; account['customer_name']=customer['name']; account['locked']=bool(db.execute('SELECT 1 FROM account_lockouts WHERE email=? AND locked_until>?',(account['email'].lower(),datetime.now().isoformat())).fetchone()); accounts.append(account)
        return {'accounts':accounts}

    @app.get('/partner/customer-accounts',response_class=HTMLResponse)
    def customer_accounts_page(request: Request):
        require_partner_access(request)
        return HTMLResponse('''<!doctype html><html><head><meta charset="utf-8"><title>Customer accounts | AnyAiCam</title></head><body><main><h1>Customer account recovery</h1><p>Unlocking clears only temporary failed-login state. It never changes a password, plan, camera, site, appliance, subscription, or permission. Sending a password reset emails the account's own address on file a one-hour reset link -- it never changes the password itself until the customer completes that link. Changing the email address updates the account's login email immediately and emails a security notice to the OLD address -- it never changes the password, plan, camera, site, appliance, subscription, or permission either.</p><div id="accounts">Loading authorized customer accounts…</div><p id="result" role="status"></p></main><script>async function load(){const r=await fetch('/api/partner/customer-accounts'),b=await r.json(),box=document.getElementById('accounts');if(!r.ok){box.textContent=b.detail||'Unable to load customer accounts.';return}box.replaceChildren(...b.accounts.map(a=>{const d=document.createElement('div'),unlockButton=document.createElement('button'),resetButton=document.createElement('button'),emailButton=document.createElement('button');d.textContent=`${a.customer_name} — ${a.email} (${a.locked?'locked':'not locked'}) `;unlockButton.textContent='Unlock account';unlockButton.onclick=async()=>{if(!confirm(`Clear temporary lockout for ${a.email}? Password and customer access will not change.`))return;const x=await fetch(`/api/partner/customers/${encodeURIComponent(a.customer_id)}/accounts/${encodeURIComponent(a.id)}/unlock`,{method:'POST',headers:{'X-CSRF-Token':document.cookie.match(/(?:^|; )anyaicam_csrf=([^;]*)/)?.[1]||''}}),y=await x.json();document.getElementById('result').textContent=y.message||y.detail||'Request failed.';if(x.ok)load()};resetButton.textContent='Send password reset';resetButton.onclick=async()=>{if(!confirm(`Send a password-reset link to ${a.email}? Their current password stays unchanged until they use it.`))return;const x=await fetch(`/api/partner/customers/${encodeURIComponent(a.customer_id)}/accounts/${encodeURIComponent(a.id)}/reset-password`,{method:'POST',headers:{'X-CSRF-Token':document.cookie.match(/(?:^|; )anyaicam_csrf=([^;]*)/)?.[1]||''}}),y=await x.json();document.getElementById('result').textContent=y.message||y.detail||'Request failed.'};emailButton.textContent='Change email';emailButton.onclick=async()=>{const newEmail=prompt(`New email address for ${a.email}:`);if(!newEmail)return;if(!confirm(`Change this account's login email from ${a.email} to ${newEmail}? A security notice will be sent to the OLD address.`))return;const x=await fetch(`/api/partner/customers/${encodeURIComponent(a.customer_id)}/accounts/${encodeURIComponent(a.id)}/change-email`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.cookie.match(/(?:^|; )anyaicam_csrf=([^;]*)/)?.[1]||''},body:JSON.stringify({new_email:newEmail})}),y=await x.json();document.getElementById('result').textContent=y.message||y.detail||'Request failed.';if(x.ok)load()};d.append(unlockButton,resetButton,emailButton);return d}))}load()</script></body></html>''')

    @app.get('/forgot-password',response_class=HTMLResponse)
    def forgot_password_page():
        content='''<header class="topbar"><div><p class="eyebrow">Account security</p><h1>Forgot password</h1></div></header><section class="panel" style="max-width:520px;margin:auto"><form id="forgot-form" class="rule-form"><label>Account email<input id="forgot-email" type="email" required></label><button class="action-button">Prepare reset message</button></form><p class="health-detail">Local development writes the reset message to the email-preview folder. Production uses the configured email provider.</p></section>'''; scripts='''<script>const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='"'&&v[v.length-1]==='"'?v.slice(1,-1):v};document.getElementById('forgot-form').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/password-reset/request',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({email:document.getElementById('forgot-email').value})}),r=await response.json().catch(()=>({}));showToast(response.ok?(r.message||'If the account exists, a password-reset message has been prepared.'):(r.detail||'Request failed. Please try again.'))})</script>'''; return shell('Forgot password','users',content,scripts)

    @app.post('/api/password-reset/complete')
    def password_reset_complete(payload: dict,request: Request):
        client_ip=(request.client.host if request.client else 'unknown')
        if not _password_reset_complete_ip_limiter.allow(client_ip):
            raise HTTPException(status_code=429,detail='Too many password reset attempts. Please wait a few minutes and try again.')
        password=str(payload.get('password',''))
        if len(password)<12: raise HTTPException(status_code=400,detail='Password must contain at least 12 characters.')
        role=consume_password_reset(str(payload.get('token','')),password)
        if not role:
            audit({'email':'unknown','role':'anonymous'},'password_reset.failed','partner_user','',{'reason':'invalid_or_expired'})
            raise HTTPException(status_code=400,detail='Reset token is invalid, expired, or already used.')
        audit({'email':'password-reset','role':'account'},'password_reset.completed','partner_user','')
        # Role-aware post-reset destination -- a customer_owner/customer_
        # viewer account must land on the customer sign-in page, never the
        # partner one (that page's own login form has no "customer" portal
        # option at all, and its own text says customer accounts cannot
        # use it). Every other role goes to /partner.html, the confirmed-
        # public, actively-used partner/admin/technician sign-in page --
        # not /partner-login, a second, older login page under this same
        # app that (independent of this fix) isn't reachable without an
        # existing session, so redirecting there would reproduce the same
        # "bounced to /login" failure this endpoint is fixing.
        destination='/customer-login.html' if role in ('customer_owner','customer_viewer') else '/partner.html'
        return {'message':'Password updated. You can now sign in.','destination':destination}

    @app.get('/reset-password',response_class=HTMLResponse)
    def password_reset_page(token: str=''):
        # escape(...,quote=True): the customer-facing twin of this page
        # (customer_reset_password_page below) already does this; this one
        # didn't, reflecting the raw ?token= query value straight into an
        # HTML attribute -- a real, independent reflected-XSS gap (a
        # crafted /reset-password?token=x"onmouseover="..." link could
        # execute script in an admin's browser) found while tracing
        # tonight's reset-link failures. secrets.token_urlsafe()'s own
        # alphabet never contains a quote character, so this was never the
        # cause of those failures (the real cause was the link's host,
        # fixed separately above) -- but it's a genuine bug on its own.
        safe_token=escape(token,quote=True)
        # Show/Hide toggle: same markup, classes, and toggle behavior as
        # the only existing instance of this pattern in the app
        # (partner.html's own sign-in password field) -- shell()-wrapped
        # pages like this one don't already carry .password-wrap/.show-
        # password CSS, so it's inlined here rather than duplicated into
        # the shared site-wide stylesheet for one field. Purely a display
        # toggle: it only flips the input's type attribute between
        # "password" and "text" client-side -- the value, the submit
        # handler, and what's sent to /api/password-reset/complete are
        # completely unchanged, so nothing about what's transmitted or
        # logged is affected. Defaults masked (type="password") on load.
        # This page has one password field today (no separate confirm
        # field to mirror it onto).
        content=f'''<style>.password-wrap{{position:relative}}.password-wrap input{{padding-right:5rem}}.show-password{{position:absolute;right:.4rem;top:.4rem;border:0;background:#edf1fa;border-radius:8px;padding:.48rem;cursor:pointer}}</style><header class="topbar"><div><p class="eyebrow">Account security</p><h1>Reset password</h1></div></header><section class="panel" style="max-width:520px;margin:auto"><form id="reset-form" class="rule-form"><input id="reset-token" type="hidden" value="{safe_token}"><label>New password<div class="password-wrap"><input id="reset-password" type="password" minlength="12" autocomplete="new-password" required><button id="show-password" class="show-password" type="button">Show</button></div></label><button class="action-button">Update password</button></form></section>'''; scripts='''<script>const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='"'&&v[v.length-1]==='"'?v.slice(1,-1):v};document.getElementById('show-password').onclick=()=>{const input=document.getElementById('reset-password'),button=document.getElementById('show-password');input.type=input.type==='password'?'text':'password';button.textContent=input.type==='password'?'Show':'Hide'};document.getElementById('reset-form').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/password-reset/complete',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({token:document.getElementById('reset-token').value,password:document.getElementById('reset-password').value})}),r=await response.json().catch(()=>({}));showToast(r.message||r.detail||'Password reset failed. Please try again.');if(response.ok)setTimeout(()=>location.href=r.destination||'/partner.html',800)})</script>'''; return shell('Reset password','users',content,scripts)

    # /forgot-password and /reset-password above render inside shell() --
    # the same dark Admin/Partner Portal chrome every /partner.html-side
    # page uses. Reused for the customer flow, that chrome (and, worse,
    # /reset-password's own hardcoded post-reset redirect to
    # /partner-login) is exactly the "redirects to or reuses the
    # Partner/Admin portal login" bug: a customer clicking "Forgot
    # password?" from customer-login.html must never leave the customer
    # experience or land back on the blue portal login. These two routes
    # are a separate, minimal, customer-branded pair (styled to match
    # customer-login.html's own card, not shell()) that call the exact
    # same, already role-agnostic /api/password-reset/request and
    # /api/password-reset/complete endpoints above -- no new backend
    # behavior, only a customer-facing frontend for it that stays
    # customer-branded and redirects back to /customer-login.html.
    _CUSTOMER_AUTH_STYLE = ':root{--navy:#10162d;--blue:#5360df;--pink:#bd2b90}*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,Arial;background:#eef3f9;color:#17233e}.head{display:flex;align-items:center;padding:14px clamp(16px,5vw,64px);background:#fff}.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--navy);font-weight:900}.brand img{width:50px}.auth-wrap{min-height:calc(100vh - 130px);display:grid;place-items:center;padding:32px 16px}.card{width:100%;max-width:420px;background:#fff;color:#17233e;padding:28px;border-radius:20px;box-shadow:0 20px 60px #05091a25}.card form{display:grid;gap:14px;margin-top:6px}.card label{display:grid;gap:5px;font-weight:700}.card input{padding:12px;border:1px solid #aab7ca;border-radius:9px;font:inherit}.submit{border:0;border-radius:999px;padding:12px;background:linear-gradient(135deg,var(--pink),var(--blue));color:#fff;font-weight:900;cursor:pointer}.message{display:none;padding:10px;background:#ffe8ec;color:#8b1730;border-radius:8px}.back-link{display:block;margin-top:16px;text-align:center;font-weight:700;color:var(--blue);text-decoration:none}'

    @app.get('/customer-forgot-password',response_class=HTMLResponse)
    def customer_forgot_password_page():
        return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Forgot password | ANY AI CAM</title><style>{_CUSTOMER_AUTH_STYLE}</style></head><body>
<header class="head"><a class="brand" href="/customer-login.html"><img src="/static/brand-icon.png" alt="AnyAiCam">ANY AI CAM</a></header>
<main class="auth-wrap"><section class="card"><h2>Forgot your password?</h2><p>Enter the email on your customer account and we'll prepare a reset link.</p><form id="forgot-form"><label>Email<input id="forgot-email" type="email" autocomplete="username" required></label><div id="message" class="message"></div><button class="submit">Send reset link</button></form><a class="back-link" href="/customer-login.html">Back to customer sign in</a></section></main>
<script>const csrf=()=>{{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='"'&&v[v.length-1]==='"'?v.slice(1,-1):v}};document.getElementById('forgot-form').addEventListener('submit',async e=>{{e.preventDefault();const response=await fetch('/api/password-reset/request',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf()}},body:JSON.stringify({{email:document.getElementById('forgot-email').value}})}}),r=await response.json().catch(()=>({{}})),box=document.getElementById('message');box.textContent=response.ok?(r.message||'If the account exists, a reset message has been prepared.'):(r.detail||'Request failed. Please try again.');box.style.display='block'}});</script>
</body></html>''')

    @app.get('/customer-reset-password',response_class=HTMLResponse)
    def customer_reset_password_page(token: str=''):
        safe_token=escape(token,quote=True)
        return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reset password | ANY AI CAM</title><style>{_CUSTOMER_AUTH_STYLE}</style></head><body>
<header class="head"><a class="brand" href="/customer-login.html"><img src="/static/brand-icon.png" alt="AnyAiCam">ANY AI CAM</a></header>
<main class="auth-wrap"><section class="card"><h2>Reset your password</h2><form id="reset-form"><input id="reset-token" type="hidden" value="{safe_token}"><label>New password<input id="reset-password" type="password" minlength="12" autocomplete="new-password" required></label><div id="message" class="message"></div><button class="submit">Update password</button></form><a class="back-link" href="/customer-login.html">Back to customer sign in</a></section></main>
<script>const csrf=()=>{{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='"'&&v[v.length-1]==='"'?v.slice(1,-1):v}};document.getElementById('reset-form').addEventListener('submit',async e=>{{e.preventDefault();const response=await fetch('/api/password-reset/complete',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf()}},body:JSON.stringify({{token:document.getElementById('reset-token').value,password:document.getElementById('reset-password').value}})}}),r=await response.json().catch(()=>({{}})),box=document.getElementById('message');box.textContent=r.message||r.detail||'Password reset failed. Please try again.';box.style.display='block';if(response.ok)setTimeout(()=>location.href='/customer-login.html',900)}});</script>
</body></html>''')

    @app.get('/api/admin/audit-export')
    def export_audit(request: Request,format: str='csv'):
        identity=require_partner_access(request,{'administrator'}); records=rows('SELECT * FROM audit_logs ORDER BY created_at DESC')
        audit(identity,'audit.exported','audit','all',{'format':format,'records':len(records)})
        if format=='json': return StreamingResponse(iter([json.dumps(records,indent=2)]),media_type='application/json',headers={'Content-Disposition':'attachment; filename=anyaicam-audit.json'})
        output=io.StringIO(); fields=['id','actor_email','actor_role','action','entity_type','entity_id','details_json','created_at']; writer=csv.DictWriter(output,fieldnames=fields); writer.writeheader(); writer.writerows([{key:item.get(key) for key in fields} for item in records]); return StreamingResponse(iter([output.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=anyaicam-audit.csv'})

    @app.get('/api/admin/data-retention')
    def data_retention(request: Request):
        require_partner_access(request,{'administrator'}); configured={item['category']:item['retention_days'] for item in rows('SELECT * FROM data_retention_policies')}; return {'media_days':configured.get('media',settings.media_retention_days),'audit_days':configured.get('audit',settings.audit_retention_days),'configured':configured}

    @app.put('/api/admin/data-retention')
    def update_data_retention(request: Request,payload: dict):
        identity=require_partner_access(request,{'administrator'}); now=datetime.now().isoformat()
        with connection() as db:
            for category,value in payload.items():
                if category not in {'media','audit','snapshots','thumbnails','clips','documents','partner-materials'}: continue
                days=max(1,min(3650,int(value))); db.execute('INSERT INTO data_retention_policies(category,retention_days,updated_at,updated_by) VALUES(?,?,?,?) ON CONFLICT(category) DO UPDATE SET retention_days=excluded.retention_days,updated_at=excluded.updated_at,updated_by=excluded.updated_by',(category,days,now,identity['email']))
        audit(identity,'retention.changed','data_retention','policies',payload); return {'message':'Data-retention configuration saved.'}

    @app.post('/api/partner/quotes/{quote_id}/deliver')
    def deliver_quote(request: Request,quote_id: str,payload: dict):
        identity=require_partner_access(request)
        # Directly-equivalent finding, same 2026-09-14 remediation pass:
        # this route had no tenant-ownership check of its own -- any
        # authenticated partner_owner/salesperson/technician could probe
        # a foreign quote_id (200 vs 404 confirmed existence) and trigger
        # a delivery email referencing it to an arbitrary attacker-chosen
        # recipient address. quotes.partner_id is always populated at
        # creation time (unlike appliances.partner_id), so this reuses
        # the same core tenant_owns_partner() primitive directly rather
        # than needing a join. Same 404 for "doesn't exist" and "not
        # yours" -- no cross-tenant existence oracle.
        with connection() as db:
            quote=db.execute('SELECT * FROM quotes WHERE id=?',(quote_id,)).fetchone()
            if not quote or not tenant_owns_partner(db,identity,quote['partner_id']):
                raise HTTPException(status_code=404,detail='Quote not found.')
        recipient=str(payload.get('email','')).strip(); message=get_email_service().send('quote_delivery',recipient,'Your AnyAiCam quote','Your AnyAiCam quote is ready for review.',metadata={'quote_id':quote_id}); audit(identity,'quote.delivered','quote',quote_id,{'recipient':recipient,'provider':settings.email_backend}); return {'message':'Quote delivery processed.','status':message['status']}
