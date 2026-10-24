import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime,timedelta
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pricing_config import _partner_unit_price, calculate_partner_quote, load_pricing, public_pricing, save_pricing
from partner_db import authenticate_detailed, audit, allowed, connection, create_first_admin, FirstAdminAlreadyExists, password_hash, tenant_owns_partner, verify_password
from cloud_config import settings
from cloud_security import clear_login_failures,login_blocked,record_login_failure
from customer_policy import role_destination
import runtime_paths

SESSION_COOKIE = 'anyaicam_partner_session'
SESSION_SECRETS = ([os.getenv('ANYAICAM_PORTAL_SECRET')] if os.getenv('ANYAICAM_PORTAL_SECRET') else settings.app_secrets) or [secrets.token_hex(32)]
ADMIN_EMAIL = os.getenv('ANYAICAM_ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD = os.getenv('ANYAICAM_ADMIN_PASSWORD', '')
try:
    PORTAL_ACCOUNTS = json.loads(os.getenv('ANYAICAM_PARTNER_ACCOUNTS', '{}'))
except json.JSONDecodeError:
    PORTAL_ACCOUNTS = {}
QUOTES_FILE = runtime_paths.recordings_path('partner_quotes.json')
PARTNER_ROLES = {'administrator', 'partner_owner', 'salesperson', 'technician'}
AUTH_ROLES = PARTNER_ROLES | {'customer_owner', 'customer_viewer'}
CUSTOMER_LOGIN_ROLES = {'customer_owner','customer_viewer','administrator'}


def destination_for_role(role: str) -> str:
    return role_destination(role)


# Sign-in, sign-out and account-recovery routes are never a post-sign-in
# destination (2026-10-05): a crafted ?next= pointing at one bounced a person
# who had just signed in into another authentication flow -- e.g. next=/login,
# the local emergency recovery sign-in. Same list as the sign-in pages' JS.
AUTH_FLOW_PATHS = ('/login', '/logout', '/partner-logout', '/partner-login', '/customer-login.html', '/partner.html', '/change-password', '/customer-signup', '/customer-register', '/forgot-password', '/reset-password', '/customer-forgot-password', '/customer-reset-password', '/accept-invite', '/accept-invitation')


def is_auth_flow_path(path) -> bool:
    from urllib.parse import unquote
    bare = unquote(str(path).split('?', 1)[0].split('#', 1)[0]).rstrip('/') or '/'
    return any(bare == p or bare.startswith(p + '/') for p in AUTH_FLOW_PATHS)


def _token(email: str, role: str, partner_id=None, customer_id=None, session_id=None, ttl_hours: int | None = None, *, ttl_seconds: int | None = None) -> str:
    if ttl_seconds is None:
        if ttl_hours is None:
            from appliance_identity import get_ttl_config
            ttl_hours = get_ttl_config()['session_ttl_hours']
        ttl_seconds = int(ttl_hours) * 3600
    payload = json.dumps({'email': email, 'role': role, 'partner_id': partner_id, 'customer_id': customer_id, 'session_id':session_id, 'expires': int(time.time()) + int(ttl_seconds)}, separators=(',', ':')).encode()
    encoded = urlsafe_b64encode(payload).decode().rstrip('=')
    signature = hmac.new(SESSION_SECRETS[0].encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + '.' + signature


# Linux 1.2.7.1 (D-AUTHZ-1 residual + D-AUTHZ-2b): _identity() is the single
# choke point every Partner Portal identity goes through -- partner_identity(),
# require_partner_access(), cloud_administrator_bridge() / current_user(), the
# Admin Portal, website_partner, aws_cost_dashboard, tenant checks and the
# platform-owner routes all inherit it. A session is honoured only while:
#   - its user_sessions row exists, is not revoked or expired, and names the
#     same email and role as the signed cookie;
#   - the account (partner_users) still exists, is approved and active, still
#     has that role, and its authorization_version equals the one recorded at
#     sign-in (a grant revoke, role change, password or MFA reset ends it);
#   - on the cloud profile, an administrator session carries the server-side
#     MFA marker mfa_state='verified' (set only by MFA verification); one
#     without any marker is revoked and audited on sight.
# An MFA enrollment / re-enrollment session (mfa_state enrollment_only /
# reenroll_required) is never a full session: only partner_enrollment_identity()
# accepts it. Edge only: a session the cloud vouched for (auth_source
# cloud_assertion / local_customer) has no authoritative LOCAL account version
# -- the signed identity manifest governs it (appliance_identity.
# reconcile_sessions_against_manifest, local_customer_access) -- so only a
# local account's approval/status is checked there. Administrator tokens with
# no session row are refused everywhere (phase 1; other roles unchanged).
# env_account: an ANYAICAM_PARTNER_ACCOUNTS sign-in (no database user) -- edge only;
# the cloud refuses it at sign-in and here (L-7).
EDGE_DELEGATED_AUTH_SOURCES = frozenset({'cloud_assertion', 'local_customer', 'env_account'})
ENROLLMENT_MFA_STATES = frozenset({'enrollment_only', 'reenroll_required'})
_INACTIVE_ACCOUNT_STATUSES = frozenset({'suspended', 'revoked', 'pending', 'disabled'})


def _decode(token: str) -> dict | None:
    """Signature, expiry and role of a Partner Portal cookie -- nothing else."""
    try:
        encoded, signature = str(token or '').rsplit('.', 1)
        if not any(hmac.compare_digest(signature,hmac.new(secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()) for secret in SESSION_SECRETS): return None
        payload = json.loads(urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if payload['expires'] < time.time() or payload['role'] not in AUTH_ROLES: return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _raw_identity(request: Request) -> dict | None:
    """The signed cookie's own claims WITHOUT the session/account checks --
    only for signing out (a session that no longer authorizes anything must
    still be revocable by its holder)."""
    return _decode(request.cookies.get(SESSION_COOKIE, ''))


def _account_for_session(db, session: dict):
    if session.get('user_id'):
        account = db.execute('SELECT id,email,role,approved,account_status,authorization_version FROM partner_users WHERE id=?', (session['user_id'],)).fetchone()
        if account is not None: return account
    return db.execute('SELECT id,email,role,approved,account_status,authorization_version FROM partner_users WHERE lower(email)=?',
                      (str(session.get('email') or '').lower(),)).fetchone()


def _account_active(account) -> bool:
    return bool(account['approved']) and str(account['account_status'] or 'active').lower() not in _INACTIVE_ACCOUNT_STATUSES


def _evaluate(payload: dict) -> tuple:
    """(identity, kind) for a signature-valid cookie. kind 'full' = an ordinary
    session, 'enrollment' = an administrator MFA (re-)enrollment session
    (partner_enrollment_identity() only); anything else is a refusal reason."""
    role = payload['role']; session_id = payload.get('session_id')
    if not session_id:
        if role == 'administrator': return None, 'no_session'
        return dict(payload), 'full'
    from admin_auth_policy import is_cloud_profile
    cloud = is_cloud_profile()
    with connection() as db:
        session = db.execute('SELECT * FROM user_sessions WHERE id=?', (session_id,)).fetchone()
        if not session: return None, 'unknown_session'
        session = dict(session)
        if session.get('revoked_at') or str(session.get('expires_at') or '') < datetime.now().isoformat(): return None, 'ended'
        if session.get('role') != role or str(session.get('email') or '').lower() != str(payload.get('email') or '').lower(): return None, 'mismatch'
        account = _account_for_session(db, session)
    auth_source = session.get('auth_source')
    if auth_source in EDGE_DELEGATED_AUTH_SOURCES and not cloud:
        if account is not None and not _account_active(account): return None, 'inactive'
    else:
        if account is None: return None, 'no_account'
        if not _account_active(account): return None, 'inactive'
        if account['role'] != role: return None, 'role_changed'
        version = session.get('authorization_version_at_login')
        if version is None or int(version) != int(account['authorization_version'] or 0): return None, 'authorization_changed'
    mfa_state = session.get('mfa_state')
    kind = 'full'
    if mfa_state in ENROLLMENT_MFA_STATES:
        kind = 'enrollment'
    elif role == 'administrator' and cloud and mfa_state != 'verified':
        # An administrator session issued without the MFA marker (before
        # 1.2.7.1, or by any path other than MFA verification) ends here.
        with connection() as db:
            db.execute('UPDATE user_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (datetime.now().isoformat(), session_id))
        from admin_auth_policy import auth_audit
        auth_audit('session.invalidated', actor_email=payload.get('email'), actor_role=role, reason='admin_without_mfa',
                   entity_id=str(session.get('user_id') or ''))
        return None, 'admin_without_mfa'
    identity = dict(payload)
    identity.update({'user_id': session.get('user_id') or (account['id'] if account is not None else None), 'mfa_state': mfa_state,
                     'mfa_verified_at': session.get('mfa_verified_at'), 'auth_source': auth_source,
                     'authorization_version': session.get('authorization_version_at_login')})
    return identity, kind


def _cached_evaluation(request: Request) -> tuple:
    """Evaluated once per request (memoised on request.state, keyed by the
    cookie) -- the middleware and the route share one decision."""
    token = request.cookies.get(SESSION_COOKIE, '')
    if not token: return None, 'no_cookie'
    try:
        cache = request.scope.setdefault('anyaicam_partner_identity_cache', {})
    except Exception:
        cache = {}
    if token not in cache:
        payload = _decode(token)
        try:
            cache[token] = _evaluate(payload) if payload else (None, 'invalid')
        except Exception:
            cache[token] = (None, 'error')  # fail closed
    identity, kind = cache[token]
    return (dict(identity) if identity else None), kind


def _identity(request: Request) -> dict | None:
    identity, kind = _cached_evaluation(request)
    return identity if kind == 'full' else None


def partner_enrollment_identity(request: Request) -> dict | None:
    """An administrator session that may ONLY enroll (or re-enroll) MFA: the
    restricted session an un-enrolled administrator gets at sign-in on the
    cloud (L-2a) or a break-glass redemption (RC-2). Accepted solely by the MFA
    status/enroll/confirm routes, /admin/mfa-setup and sign-out."""
    identity, kind = _cached_evaluation(request)
    return identity if kind == 'enrollment' and identity and identity.get('role') == 'administrator' else None


def establish_partner_session(destination: str, *, request: Request, email: str, role: str, user: dict | None, authorization_version_at_login: int | None = None,
                              auth_source: str | None = 'password', mfa_state: str | None = None, mfa_method: str | None = None,
                              ttl_minutes: int | None = None) -> RedirectResponse:
    """Writes the user_sessions row and sets the signed partner cookie
    for a freshly-authenticated Partner Portal identity, then redirects
    to destination -- the exact same two steps partner_login_submit()
    (POST /api/partner-login) already performed inline. Factored out so
    a second entry point (main.py's POST /api/portal-login, the blue
    Portal login page's Administrator/Partner/Technician selector) can
    establish an identical session without duplicating this
    security-sensitive cookie-signing logic.

    1.2.7.1: the row always records the account's authorization_version (read
    in the same connection as the INSERT when the caller has none), how the
    sign-in was proven (auth_source: password, mfa, break_glass,
    cloud_assertion, local_customer) and the MFA marker. Only MFA
    verification passes mfa_state='verified'."""
    from appliance_identity import get_ttl_config
    ttl_seconds = int(ttl_minutes) * 60 if ttl_minutes else int(get_ttl_config()['session_ttl_hours']) * 3600
    session_id = secrets.token_hex(16)
    now = datetime.now()
    expiry = now + timedelta(seconds=ttl_seconds)
    user_id = user.get('id') if user else None
    with connection() as db:
        version = authorization_version_at_login
        if version is None and user_id and auth_source not in EDGE_DELEGATED_AUTH_SOURCES:
            current = db.execute('SELECT authorization_version FROM partner_users WHERE id=?', (user_id,)).fetchone()
            version = int(current['authorization_version']) if current else None
        db.execute(
            'INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,ip_address,user_agent,'
            'authorization_version_at_login,auth_source,mfa_state,mfa_verified_at,mfa_method) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                session_id, user_id, email, role, 'Web browser', 'cookie',
                now.isoformat(), now.isoformat(), expiry.isoformat(),
                request.client.host if request.client else None, request.headers.get('user-agent', '')[:500],
                version, auth_source, mfa_state, now.isoformat() if mfa_state == 'verified' else None, mfa_method,
            ),
        )
    response = RedirectResponse(destination, status_code=303)
    # Lax, not Strict: returning from Stripe Checkout (and clicking an email
    # link) is a top-level navigation from another site, which Strict drops,
    # sending the customer to sign-in. Lax still never rides a cross-site
    # POST, and unsafe methods also need the double-submit CSRF token.
    response.set_cookie(
        SESSION_COOKIE,
        _token(email, role, user.get('partner_id') if user else None, user.get('customer_id') if user else None, session_id, ttl_seconds=ttl_seconds),
        httponly=True, samesite='lax', secure=settings.secure_cookies, max_age=ttl_seconds, domain=settings.cookie_domain or None,
    )
    return response


def _safe_next_path(next_path):
    """Same-origin relative path (not //, no backslash, whitespace or control
    characters, not an auth-flow page) or None."""
    if (isinstance(next_path,str) and next_path.startswith('/') and not next_path.startswith('//')
            and '\\' not in next_path and not any(ch.isspace() or ord(ch)<32 for ch in next_path)
            and not is_auth_flow_path(next_path)):
        return next_path
    return None


def _claimed_appliance() -> bool:
    """A claimed edge (persisted activation identity): customers sign in through
    the cloud or their appliance password. An unclaimed edge keeps local sign-in."""
    from appliance_activation import load_persisted_identity
    return load_persisted_identity() is not None


def _edge_customer_login(payload: dict, request: Request, email: str, password: str):
    """Customer sign-in on a claimed appliance (local_customer_access.py):
    cloud-delegated when online, the owner's appliance password when offline.
    Uses the same lockout counters and session as every other sign-in."""
    import local_customer_access
    try:
        result=local_customer_access.sign_in(email,password)
    except local_customer_access.AccessDenied as denied:
        if denied.reason in ('invalid','invalid_offline'):
            record_login_failure(email)
        audit({'email':email,'role':'anonymous'},'login.failed','local_customer','',{'reason':denied.reason})
        raise HTTPException(status_code=denied.status,detail=denied.detail)
    clear_login_failures(email)
    audit({'email':email,'role':result['role']},'login.succeeded','local_customer',result['user']['id'],{'mode':result['mode']})
    destination=_safe_next_path(payload.get('next')) or '/customer-live'  # the cloud setup flow is not on the appliance
    return establish_partner_session(destination,request=request,email=email,role=result['role'],user=result['user'],
                                     authorization_version_at_login=result['authorization_version'],auth_source='local_customer')


def _require(request: Request, roles=PARTNER_ROLES) -> dict:
    identity = _identity(request)
    if not identity or identity['role'] not in roles:
        raise HTTPException(status_code=403, detail='Partner authorization required.')
    # Linux 1.2.7.1 (cloud): an administrator-only route also needs a LIVE
    # global administrator grant -- partner_users.role stays 'administrator'
    # after a grant is revoked, and a partner-scoped administrator carries the
    # same role label (coordinator evidence 2026-10-24, Green Z8d'). Partner-
    # scoped administrators keep the routes open to partner roles.
    if identity['role'] == 'administrator' and set(roles) <= {'administrator'}:
        from admin_auth_policy import auth_audit, is_cloud_profile
        if is_cloud_profile():
            from appliance_identity import has_global_administrator_grant
            with connection() as db:
                live = has_global_administrator_grant(db, email=identity.get('email', ''))
            if not live:
                auth_audit('access.denied', actor_email=identity.get('email'), actor_role='administrator', reason='admin_without_global_grant',
                           entity_id=str(identity.get('user_id') or ''))
                raise HTTPException(status_code=403, detail='Global administrator grant required.')
    return identity


def partner_identity(request: Request) -> dict | None:
    return _identity(request)


def require_partner_access(request: Request, roles=PARTNER_ROLES) -> dict:
    return _require(request, roles)


def _read_all_quotes() -> list:
    try:
        data = json.loads(QUOTES_FILE.read_text(encoding='utf-8')) if QUOTES_FILE.exists() else []
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _read_quotes() -> list:
    # Shared file with main.py's quote builder (records carrying quote_name);
    # only calculator quotes are returned, and _save_quotes() keeps the
    # builder's records (partner portal pass 2026-09-26).
    return [item for item in _read_all_quotes() if 'quote_name' not in item]


def _save_quotes(quotes: list) -> None:
    builder = [item for item in _read_all_quotes() if 'quote_name' in item]
    QUOTES_FILE.parent.mkdir(parents=True, exist_ok=True); temp=QUOTES_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(builder + [item for item in quotes if 'quote_name' not in item], indent=2), encoding='utf-8'); temp.replace(QUOTES_FILE)


def _earned_commissions_panel(identity: dict) -> str:
    """Real, payment-verified commission records (sales_commissions.py) --
    unlike the estimates above, these exist only once Stripe confirmed a
    payment. A salesperson sees their own; a partner owner their company's;
    a global administrator is pointed at /api/sales/commissions."""
    import sales_commissions
    from html import escape as _esc
    role = identity.get('role')
    if role == 'salesperson':
        user = sales_commissions._sales_user(identity.get('email', ''))
        entries = sales_commissions.ledger_for(salesperson_user_id=user['id']) if user else []
    elif role == 'partner_owner' and identity.get('partner_id'):
        entries = sales_commissions.ledger_for(partner_id=identity['partner_id'])
    else:
        return ''
    summary = sales_commissions.summarize(entries)
    from partner_db import rows as _rows
    ids = sorted({e['customer_id'] for e in entries[:100]})
    names = ({c['id']: c['name'] for c in _rows(f"SELECT id,name FROM customers WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))}
             if ids else {})
    kinds = {'activation': 'Activation', 'hardware': 'Hardware', 'recurring': 'Recurring'}
    rows_html = ''.join(
        f"<tr><td>{kinds.get(e['kind'], e['kind'])}</td><td>{_esc(names.get(e['customer_id']) or 'Customer')}</td>"
        f"<td>${int(e['amount_cents']) / 100:,.2f}</td><td>{_esc(e['status'].replace('_', ' '))}</td>"
        f"<td data-local-time=\"{_esc(e['earned_at'], quote=True)}\">{_esc(e['earned_at'][:10])}</td></tr>"
        for e in entries[:100]
    ) or '<tr><td colspan="5">No earned commissions yet.</td></tr>'
    return (f'<section class="panel" style="margin-top:14px"><h2>Earned commissions</h2>'
            f'<p class="health-detail">Recorded when payments are confirmed. Total earned: ${summary["earned_cents"] / 100:,.2f}.</p>'
            f'<table><tr><th>Type</th><th>Customer</th><th>Amount</th><th>Status</th><th>Earned</th></tr>{rows_html}</table>'
            '<script>document.querySelectorAll("[data-local-time]").forEach(el=>{const d=new Date(el.dataset.localTime);if(!isNaN(d))el.textContent=d.toLocaleDateString();});</script></section>')


def register_partner_routes(app: FastAPI, shell: Callable) -> None:
    @app.get('/api/public-pricing')
    def public_prices() -> dict:
        return public_pricing()

    def setup_available():
        if settings.runtime_role not in {'edge', 'combined'}:
            raise HTTPException(status_code=404)
        # L-8 (1.2.7.1): combined is a cloud profile too -- never a web first-admin setup there.
        from admin_auth_policy import is_cloud_profile
        if is_cloud_profile():
            raise HTTPException(status_code=404)
        with connection() as db:
            if db.execute('SELECT 1 FROM partner_users LIMIT 1').fetchone() is not None:
                raise HTTPException(status_code=404)

    @app.get('/first-admin-setup', response_class=HTMLResponse)
    def first_admin_setup(request: Request):
        setup_available()
        content = '<h1>Create the administrator account</h1><form id="first-admin-form"><label>Email<input name="email" type="email" required></label><label>Password<input name="password" type="password" minlength="12" required></label><label>Confirm password<input name="confirm" type="password" minlength="12" required></label><button>Create account</button><p role="status" id="setup-status"></p></form>'
        scripts = """<script>document.getElementById('first-admin-form').addEventListener('submit',async event=>{
          event.preventDefault();const form=event.currentTarget, status=document.getElementById('setup-status');
          if(form.elements.password.value!==form.elements.confirm.value){status.textContent='Passwords do not match.';return;}
          const csrf=document.cookie.split('; ').find(c=>c.startsWith('anyaicam_csrf='));
          try{const response=await fetch('/api/first-admin-setup',{method:'POST',credentials:'same-origin',
            headers:{'Content-Type':'application/json','X-CSRF-Token':csrf?decodeURIComponent(csrf.substring(14)):''},
            body:JSON.stringify({email:form.elements.email.value,password:form.elements.password.value})});
            const payload=await response.json();if(!response.ok){status.textContent=payload.detail||'Setup failed.';return;}
            form.reset();location.href='/partner.html';
          }catch(error){status.textContent='Unable to connect. Try again.';}
        });</script>"""
        return shell('First-time setup','partner-login',content,scripts)

    @app.post('/api/first-admin-setup')
    def first_admin_submit(payload: dict):
        setup_available()
        try:
            user_id=create_first_admin(str(payload.get('email','')),str(payload.get('password','')))
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error
        except FirstAdminAlreadyExists as error:
            raise HTTPException(status_code=409,detail='An administrator account already exists.') from error
        return {'message':'Administrator created. Sign in.', 'destination':'/partner.html'}

    @app.get('/partner-login', response_class=HTMLResponse)
    def partner_login(request: Request) -> str:
        current=_identity(request)
        if current: return RedirectResponse(destination_for_role(current['role']), status_code=303)
        configured = bool(ADMIN_EMAIL and ADMIN_PASSWORD)
        note = 'Partner access is configured.' if configured else 'Set ANYAICAM_ADMIN_EMAIL and ANYAICAM_ADMIN_PASSWORD in the private server environment to activate protected access.'
        content=f'''<header class="topbar"><div><p class="eyebrow">Protected portal</p><h1>Partner sign in</h1></div></header><section class="panel" style="max-width:520px;margin:auto"><p class="health-detail">{note}</p><form class="rule-form" id="partner-login-form"><label>Email<input id="partner-email" type="email" required></label><label>Password<input id="partner-password" type="password" required></label><button class="action-button">Sign in</button><a class="download" href="/forgot-password">Forgot password?</a></form></section>'''
        scripts='''<script>document.getElementById('partner-login-form').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/partner-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('partner-email').value,password:document.getElementById('partner-password').value})});if(response.ok){
    if(response.redirected){
        location.href=response.url;
    }else{
        location.reload();
    }
}else{
    showToast('Invalid or unconfigured partner credentials.');
}})</script>'''
        return shell('Partner login','partner-login',content,scripts)

    @app.post('/api/partner-login')
    def partner_login_submit(payload: dict,request: Request):
        email=str(payload.get('email','')).strip().lower(); password=str(payload.get('password',''))
        if login_blocked(email):
            audit({'email':email,'role':'anonymous'},'login.blocked','partner_user','',{'reason':'rate_limit'})
            raise HTTPException(status_code=429,detail='Account is temporarily locked after repeated sign-in failures. Try again later or reset your password.')
        if settings.runtime_role=='edge' and payload.get('customer_only') and _claimed_appliance():
            return _edge_customer_login(payload,request,email,password)
        user,reason=authenticate_detailed(email,password); role=user['role'] if user else None; env_account = False
        from admin_auth_policy import auth_audit, is_cloud_profile
        cloud=is_cloud_profile()
        account = PORTAL_ACCOUNTS.get(email, {})
        if not role and account.get('approved') is True and account.get('role') in AUTH_ROLES and hmac.compare_digest(password, str(account.get('password',''))):
            if cloud:
                # L-7 (1.2.7.1): an env-defined account has no database user --
                # no MFA, no grant, no revocable version -- so the cloud never
                # signs it in.
                record_login_failure(email)
                auth_audit('login.denied',actor_email=email,actor_role=str(account.get('role') or ''),reason='env_account_on_cloud')
                raise HTTPException(status_code=403,detail='The email or password is incorrect.')
            role = account['role']; env_account = True
        if role=='administrator' and cloud:
            # L-6 (1.2.7.1): administrator access on the cloud starts only at
            # the Admin portal sign-in, which requires MFA (begin_admin_login).
            auth_audit('login.denied',actor_email=email,actor_role='administrator',reason='admin_requires_mfa_sign_in',
                       entity_id=str(user.get('id','') if user else ''))
            raise HTTPException(status_code=403,detail='Administrators sign in through the Admin portal sign-in, with two-factor authentication.')
        if role and payload.get('partner_only') and role not in PARTNER_ROLES:
            audit(user or {'email':email,'role':role},'login.denied','partner_portal','',{'reason':'customer_account'})
            raise HTTPException(status_code=403,detail='Customer accounts cannot enter the Partner Portal. Use the customer sign-in page.')
        if role and payload.get('customer_only') and role not in CUSTOMER_LOGIN_ROLES:
            audit(user or {'email':email,'role':role},'login.denied','customer_portal','',{'reason':'partner_account'})
            raise HTTPException(status_code=403,detail='This account is not authorized for the Customer Portal. Use Partner Login.')
        if not role:
            record_login_failure(email)
            audit({'email':email,'role':'anonymous'},'login.failed','partner_user','',{'reason':reason})
            messages={'pending':'This account is awaiting approval.','suspended':'This account is suspended. Contact an administrator.','revoked':'This account has been revoked. Contact an administrator.','invitation_expired':'This invitation has expired. Ask an administrator for a new invitation.','invalid':'The email or password is incorrect.'}
            raise HTTPException(status_code=403, detail=messages.get(reason,'The email or password is incorrect.'))
        clear_login_failures(email)
        audit(user or {'email':email,'role':role},'login.succeeded','partner_user',user.get('id','') if user else '')
        # next: only ever honored for the plain customer_only login path,
        # and only after the two existing special-case destinations above
        # it (forced password change, admin-viewing-as-customer) still
        # win outright -- neither of those should ever be skipped past
        # just because the customer nav happened to bounce someone here
        # with a next= attached. Validated as a same-origin relative path
        # (starts with "/", not "//") before use, same open-redirect
        # guard as the rest of this app's own next= handling
        # (authentication_middleware's own next_url is never trusted
        # further than this either) -- an unauthenticated visit to a
        # protected customer page (main.py's authentication_middleware,
        # RUNTIME_ROLE=="cloud") is what actually attaches this in
        # practice, so a valid value is always exactly one of the
        # customer nav's own bare paths, never anything external.
        next_path=payload.get('next')
        # A backslash is treated as "/" by browsers ("/\\evil.example" is
        # protocol-relative), and whitespace/control characters have no
        # place in a same-origin path either.
        if not(isinstance(next_path,str) and next_path.startswith('/') and not next_path.startswith('//')
               and '\\' not in next_path and not any(ch.isspace() or ord(ch)<32 for ch in next_path)
               and not is_auth_flow_path(next_path)):
            next_path=None
        if user and user.get('must_change_password'):
            destination='/change-password'
        elif payload.get('customer_only') and role=='administrator':
            destination='/customer-portal'
        elif payload.get('customer_only') and next_path:
            destination=next_path
        else:
            destination=destination_for_role(role)
        response=establish_partner_session(destination, request=request, email=email, role=role, user=user,
                                           auth_source='env_account' if env_account else 'password')
        # Post-RC5 (Green 2026-10-17): after the session fully exists, attach this customer owner's pending guest
        # purchases if their own address was confirmed (signin_purchase_attachment: Red's conditions a-g). Never
        # raises and never touches the response, session, role or destination.
        if role=='customer_owner' and user:
            try:
                from signin_purchase_attachment import attach_after_signin
                attach_after_signin(user)
            except Exception:
                pass
        return response

    @app.post('/partner-logout')
    def partner_logout(request: Request):
        # One sign-out (2026-10-01): inside the full app this is exactly
        # /logout -- both sessions revoked, both cookies cleared, role-based
        # landing (main.perform_logout). Only a bare test app without main
        # keeps the partner-only fallback below.
        import sys
        main_module=sys.modules.get('main')
        if main_module is not None and hasattr(main_module,'perform_logout'):
            return main_module.perform_logout(request)
        identity=_raw_identity(request)
        if identity and identity.get('session_id'):
            with connection() as db: db.execute('UPDATE user_sessions SET revoked_at=? WHERE id=?',(datetime.now().isoformat(),identity['session_id']))
        response=RedirectResponse('/partner.html',status_code=303); response.delete_cookie(SESSION_COOKIE,domain=settings.cookie_domain or None); return response

    @app.get('/change-password',response_class=HTMLResponse)
    def first_password_change(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner.html',status_code=303)
        terms='<label><input id="accept-terms" type="checkbox" required> I accept the current AnyAiCam Partner Terms</label>' if identity['role'] in PARTNER_ROLES else '<input id="accept-terms" type="hidden" value="true">'
        with connection() as db:
            record=db.execute('SELECT must_change_password FROM partner_users WHERE lower(email)=?',(identity['email'].lower(),)).fetchone()
        forced=bool(record and record['must_change_password'])
        current='' if forced else '<label>Current password<input id="current-password" type="password" autocomplete="current-password" required></label>'
        heading='Create your permanent password' if forced else 'Change your password'
        # A customer (or a partner user who is only changing a password) is
        # not "activating a partner account" (2026-10-10).
        is_partner=identity['role'] in PARTNER_ROLES
        page_title=('Activate partner account' if is_partner else 'Activate your account') if forced else 'Change password'
        eyebrow='Account activation' if forced else 'Account security'
        content=f'''<header class="topbar"><div><p class="eyebrow">{eyebrow}</p><h1>{heading}</h1></div></header><section class="panel" style="max-width:560px;margin:auto"><form id="activation-password" class="rule-form">{current}<label>New password<input id="new-password" type="password" minlength="12" autocomplete="new-password" required></label>{terms}<button class="action-button">{'Activate account' if forced else 'Change password'}</button></form></section>'''
        scripts='''<script>document.getElementById('activation-password').addEventListener('submit',async e=>{e.preventDefault();const currentField=document.getElementById('current-password');const response=await fetch('/api/partner/activate-account',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('new-password').value,current_password:currentField?currentField.value:undefined,accept_terms:document.getElementById('accept-terms').checked})}),result=await response.json();if(!response.ok)return showToast(result.detail);showToast(result.message);setTimeout(()=>location.href=result.destination,500)})</script>'''
        return shell(page_title,'partner-login',content,scripts)

    @app.post('/api/partner/activate-account')
    def activate_partner_account(request: Request,payload: dict):
        identity=_identity(request)
        if not identity: raise HTTPException(status_code=401,detail='Sign in is required.')
        password=str(payload.get('password',''))
        if len(password)<12: raise HTTPException(status_code=400,detail='Use at least 12 characters.')
        if identity['role'] in PARTNER_ROLES and not payload.get('accept_terms'): raise HTTPException(status_code=400,detail='Partner Terms must be accepted before activation.')
        now=datetime.now().isoformat(); user_id=''
        with connection() as db:
            user=db.execute('SELECT id,password_hash,must_change_password FROM partner_users WHERE lower(email)=?',(identity['email'].lower(),)).fetchone()
            if not user: raise HTTPException(status_code=404,detail='Partner user was not found.')
            # Only a forced first-password change (a partner-issued temporary
            # password, must_change_password=1) may skip the current
            # password. Otherwise a signed-in session alone -- e.g. a stolen
            # cookie -- could replace the password and take the account over.
            # A user who has lost their password recovers through the reset
            # link (or, for a platform owner, break-glass), not this route.
            if not user['must_change_password'] and not verify_password(str(payload.get('current_password') or ''),user['password_hash'] or ''):
                audit(identity,'password.change_denied','partner_user',user['id'],{'reason':'current_password_mismatch'})
                raise HTTPException(status_code=403,detail='Your current password is incorrect.')
            user_id=user['id']; db.execute('UPDATE partner_users SET password_hash=?,must_change_password=0,terms_accepted_at=? WHERE id=?',(password_hash(password),now,user_id))
            # A changed password also bumps the authorization version, so appliance
            # sessions and offline appliance passwords follow it (local_customer_access.py).
            db.execute('UPDATE partner_users SET authorization_version=authorization_version+1 WHERE id=?',(user_id,))
            # 1.2.7.1: this request's own session follows the new version (same
            # transaction); every other session is revoked just below.
            if identity.get('session_id'):
                db.execute('UPDATE user_sessions SET authorization_version_at_login=(SELECT authorization_version FROM partner_users WHERE id=?) WHERE id=? AND user_id=?',
                           (user_id,identity['session_id'],user_id))
            # A new password signs out every OTHER session of this user, as a
            # reset already does (cloud_security.consume_password_reset):
            # a session stolen with the old password must not outlive it.
            # This request's own session stays signed in (2026-10-10).
            forced_change=bool(user['must_change_password'])
            current_session=str(identity.get('session_id') or '')
            if current_session:
                db.execute('UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND id!=?',(now,user_id,current_session))
            else:
                db.execute('UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(now,user_id))
            db.execute("UPDATE invitations SET status='accepted' WHERE lower(email)=? AND status='pending'",(identity['email'].lower(),))
            if identity['role'] in PARTNER_ROLES: db.execute('INSERT INTO partner_terms_acceptances(id,user_id,terms_version,accepted_at,ip_address) VALUES(?,?,?,?,?)',(secrets.token_hex(8),user_id,'2026-08-01',now,request.client.host if request.client else None))
        if identity['role'] in PARTNER_ROLES: audit(identity,'partner_terms.accepted','partner_user',user_id,{'version':'2026-08-01'})
        audit(identity,'password.changed','partner_user',user_id)
        if not forced_change:
            message='Your password has been changed. Any other signed-in sessions were signed out.'
        else:
            message='Your partner account is active.' if identity['role'] in PARTNER_ROLES else 'Your account is active.'
        return {'message':message,'destination':destination_for_role(identity['role'])}

    @app.get('/api/partner-pricing')
    def partner_prices_api(request: Request) -> dict:
        identity=_require(request)
        if not allowed(identity,'pricing.view'): raise HTTPException(status_code=403,detail='Pricing permission required.')
        return load_pricing()

    @app.post('/api/partner/calculate')
    def partner_calculate(request: Request, payload: dict) -> dict:
        identity=_require(request)
        if not allowed(identity,'quote.create'): raise HTTPException(status_code=403,detail='Quote permission required.')
        try:
            result = calculate_partner_quote(payload)
            if payload.get('save_quote'):
                # 2026-09-23 fix: saved quotes carried no partner_id at all,
                # and partner_revenue() below read every saved quote with no
                # ownership check whatsoever -- any authenticated partner
                # could see every other partner's confidential quotes,
                # commissions, and recurring-revenue totals the moment any
                # quote was ever saved. Stamped with the caller's own
                # partner_id so partner_revenue() can filter correctly.
                quotes = _read_quotes(); quotes.append({'id': secrets.token_hex(5), 'customer': payload.get('customer',''), 'site': payload.get('site',''), 'created_at': int(time.time()), 'partner_id': identity.get('partner_id') or 'anyaicam-primary', **result}); _save_quotes(quotes)
            return result
        except ValueError as error: raise HTTPException(status_code=409, detail=str(error)) from error

    @app.put('/api/admin/partner-pricing')
    def admin_update(request: Request, payload: dict) -> dict:
        identity=_require(request, {'administrator'})
        # HIGH fix (2026-09-14 final tenant-isolation re-audit, Codex):
        # {'administrator'} above is only this codebase's role LABEL --
        # byte-identical whether the grant behind it is scope_type=
        # 'global' or scope_type='partner'. This is a direct global
        # pricing WRITE path (central retail/partner pricing, margins,
        # MAP, commercial settings), so it must require a live,
        # unrevoked global administrator grant before any mutation, not
        # just the role name. Same primitive as appliance_cloud.py's
        # appliance_dashboard() and partner_workspace.py's tenant checks.
        from appliance_identity import has_global_administrator_grant
        with connection() as db:
            if not has_global_administrator_grant(db,email=identity.get('email','')):
                raise HTTPException(status_code=403,detail='Global administrator grant required.')
        config=load_pricing(); partner=config['partner']
        for field in ('pricing_mode','percentage_discount','map_enabled'):
            if field in payload: partner[field]=payload[field]
        if 'trial_days' in payload: config['trial_days']=max(0,min(365,int(payload['trial_days'])))
        if 'annual_discount_percent' in payload: config['annual_discount_percent']=max(0,min(100,float(payload['annual_discount_percent'])))
        for key, values in payload.get('plan_terms',{}).items():
            if key in partner['plan_terms']: partner['plan_terms'][key].update(values)
        for key, values in payload.get('addon_terms',{}).items():
            if key in partner['addon_terms']:
                partner['addon_terms'][key].update(values)
                if values.get('retail_monthly_price') is not None: config['addons'][key]['price']=float(values['retail_monthly_price'])
        for key, value in payload.get('retail_prices',{}).items():
            resolution,recording,retention=key.split('.'); config['plans'][resolution][recording][retention]=float(value); partner['plan_terms'][key]['retail_monthly_price']=float(value)
            if key in config.get('conflicts',{}): config['conflicts'][key].update({'confirmed':True,'confirmed_value':float(value)})
        for index, values in enumerate(payload.get('volume_tiers',[])):
            if index < len(partner['volume_tiers']): partner['volume_tiers'][index]['discount_percent']=max(0,min(100,float(values.get('discount_percent',0))))
        for name in ('hardware','installation'):
            if name in payload: partner[name].update(payload[name])
        if partner.get('pricing_mode') == 'fixed':
            for terms in list(partner['plan_terms'].values()) + list(partner['addon_terms'].values()):
                retail=terms.get('retail_monthly_price'); wholesale=terms.get('partner_monthly_price')
                if retail is not None and wholesale is not None and float(wholesale) > float(retail):
                    raise HTTPException(status_code=400, detail='A fixed partner price cannot exceed its retail price.')
        save_pricing(config); audit(identity,'pricing.changed','pricing','central-config',{'pricing_mode':partner.get('pricing_mode')}); return {'status':'complete','message':'Confidential partner pricing saved.'}

    @app.get('/partner-prices', response_class=HTMLResponse)
    def partner_sheet(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        _require(request)
        if not allowed(identity,'pricing.view'): raise HTTPException(status_code=403,detail='Pricing permission required.')
        config=load_pricing(); rows=[]
        for key,term in config['partner']['plan_terms'].items():
            retail=term.get('retail_monthly_price'); cost=term.get('partner_cost'); suggested=term.get('suggested_retail_price')
            # Partner portal pass (2026-09-26): the sheet only read fixed partner
            # prices, so percentage / volume pricing (which the quote calculator
            # honours via the same _partner_unit_price()) showed "Not configured".
            try: wholesale=_partner_unit_price(float(retail),term,1,config) if retail is not None else None
            except ValueError: wholesale=None
            selling=suggested if suggested is not None else retail
            profit=(selling-wholesale) if selling is not None and wholesale is not None else None; margin=(profit/selling*100) if profit is not None and selling else None
            resolution,recording,retention=(key.split('.')+['','',''])[:3]
            label=f"{config['plans'].get(resolution,{}).get('label',resolution.upper())} · {recording.title()} · {retention} days"
            rows.append(f'<tr><td>{label}</td><td>{"—" if retail is None else f"${retail:.2f}"}</td><td>{"Not configured" if wholesale is None else f"${wholesale:.2f}"}</td><td>{"Not configured" if cost is None else f"${cost:.2f}"}</td><td>{"—" if suggested is None else f"${suggested:.2f}"}</td><td>{"—" if profit is None else f"${profit:.2f}"}</td><td>{"—" if margin is None else f"{margin:.1f}%"}</td></tr>')
        content=f'''<header class="topbar"><div><p class="eyebrow">Confidential · {identity['role'].replace('_',' ').title()}</p><h1>Partner price sheet</h1></div><form method="post" action="/partner-logout"><button class="ghost-button">Sign out</button></form></header><div class="mock-banner">Confidential partner information. Do not share this page with retail customers.</div><section class="panel" style="overflow:auto"><table class="data-table"><thead><tr><th>Plan</th><th>Retail</th><th>Partner price</th><th>Partner cost</th><th>Suggested retail</th><th>Profit/camera</th><th>Margin</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>'''
        return shell('Partner prices','partner-prices',content)

    @app.get('/partner-quotes', response_class=HTMLResponse)
    def partner_quotes(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        _require(request)
        if not allowed(identity,'quote.create'): raise HTTPException(status_code=403,detail='Quote permission required.')
        content='''<header class="topbar"><div><p class="eyebrow">Protected partner tools</p><h1>Partner quote builder</h1></div></header><div class="mock-banner">Retail customers receive only selling prices. Partner costs and profits remain confidential.</div><section class="health-grid"><form class="panel rule-form" id="pq"><label>Customer<input id="pq-customer" required></label><label>Site<input id="pq-site" required></label><label>Resolution<select id="pq-resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label><label>Recording<select id="pq-recording"><option value="motion">Motion</option></select></label><label>Retention<select id="pq-retention"><option>2</option><option>7</option><option>14</option><option>30</option></select></label><label>Cameras<input id="pq-quantity" type="number" min="1" max="128" value="4"></label><label>Customer selling price per camera<input id="pq-sell" type="number" min="0" step="0.01" placeholder="Defaults to retail"></label><label>Installation selling price<input id="pq-install" type="number" min="0" step="0.01" value="0"></label><label>Hardware selling price<input id="pq-hardware" type="number" min="0" step="0.01" value="0"></label><fieldset><legend>Analytics</legend><label><input class="pq-addon" type="checkbox" value="smart_motion"> Smart Motion</label><label><input class="pq-addon" type="checkbox" value="people_counting"> People Counting</label><label><input class="pq-addon" type="checkbox" value="lpr"> LPR</label><label><input class="pq-addon" type="checkbox" value="ppe"> PPE</label></fieldset><button class="action-button">Build confidential quote</button></form><section class="panel" id="pq-summary"><h2>Partner profitability</h2></section></section>'''
        scripts='''<script>document.getElementById('pq').addEventListener('submit',async e=>{e.preventDefault();const payload={save_quote:true,customer:document.getElementById('pq-customer').value,site:document.getElementById('pq-site').value,resolution:document.getElementById('pq-resolution').value,recording:document.getElementById('pq-recording').value,retention:Number(document.getElementById('pq-retention').value),quantity:Number(document.getElementById('pq-quantity').value),addons:[...document.querySelectorAll('.pq-addon:checked')].map(x=>x.value),installation_sell_price:Number(document.getElementById('pq-install').value),hardware_sell_price:Number(document.getElementById('pq-hardware').value)};const sell=document.getElementById('pq-sell').value;if(sell)payload.selling_price_per_camera=Number(sell);const response=await fetch('/api/partner/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),r=await response.json(),box=document.getElementById('pq-summary');if(!response.ok){box.innerHTML=`<div class="mock-banner">${r.detail}</div>`;return}box.innerHTML=`<h2>Confidential profitability</h2><div class="health-row"><span>Customer monthly price</span><strong>$${r.monthly_customer_revenue.toFixed(2)}</strong></div><div class="health-row"><span>Monthly recurring profit</span><strong>$${r.monthly_recurring_profit.toFixed(2)}</strong></div><div class="health-row"><span>Installation profit</span><strong>$${r.installation_profit.toFixed(2)}</strong></div><div class="health-row"><span>Hardware profit</span><strong>$${r.hardware_profit.toFixed(2)}</strong></div><div class="health-row"><span>Total first-year profit</span><strong>$${r.first_year_profit.toFixed(2)}</strong></div><div class="health-row"><span>Partner margin</span><strong>${r.partner_margin_percent.toFixed(1)}%</strong></div><p class="health-detail">Confidential quote saved to partner revenue.</p>`})</script>'''
        return shell('Partner quotes','partner-quotes',content,scripts)

    @app.get('/partner-revenue', response_class=HTMLResponse)
    def partner_revenue(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        _require(request)
        if not allowed(identity,'pricing.view'): raise HTTPException(status_code=403,detail='Pricing permission required.')
        # 2026-09-23 fix: every saved quote was summed here with no
        # ownership check at all -- a real cross-partner leak of
        # confidential recurring-revenue and commission totals. Filtered
        # via tenant_owns_partner(), the same already-audited primitive
        # partner_workspace.py's own 2026-09-14 remediation established
        # elsewhere in this codebase; a global administrator still sees
        # every partner's totals unchanged, and a quote saved before this
        # fix (no partner_id field) is excluded for everyone else --
        # fail closed, never fail open.
        with connection() as db:
            quotes=[q for q in _read_quotes() if tenant_owns_partner(db,identity,q.get('partner_id'))]
        monthly=sum(float(q.get('monthly_recurring_profit',0)) for q in quotes); first_year=sum(float(q.get('first_year_profit',0)) for q in quotes)
        content=f'''<header class="topbar"><div><p class="eyebrow">Protected partner tools</p><h1>Commissions and recurring revenue</h1></div></header><section class="summary"><div class="stat"><span class="stat-label">Active estimates</span><span class="stat-value">{len(quotes)}</span></div><div class="stat"><span class="stat-label">Estimated monthly recurring profit</span><span class="stat-value">${monthly:,.2f}</span></div><div class="stat"><span class="stat-label">Estimated first-year profit</span><span class="stat-value">${first_year:,.2f}</span></div></section><div class="empty">Revenue appears after partner quotes are saved and approved.</div>{_earned_commissions_panel(identity)}'''
        return shell('Partner revenue','partner-revenue',content)

    @app.get('/partner-pricing-admin', response_class=HTMLResponse)
    def partner_admin(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        if identity['role']!='administrator': raise HTTPException(status_code=403,detail='Administrator role required.')
        # HIGH fix (2026-09-14 final tenant-isolation re-audit, Codex):
        # the role check above is only a label -- a partner-scoped
        # administrator carries it too. This page reveals central retail
        # pricing, partner prices/costs, margins, MAP controls, and
        # global commercial settings, so reaching it requires a live,
        # unrevoked global administrator grant, checked before any
        # pricing data is loaded. Same primitive as the PUT handler above.
        from appliance_identity import has_global_administrator_grant
        with connection() as db:
            if not has_global_administrator_grant(db,email=identity.get('email','')):
                raise HTTPException(status_code=403,detail='Global administrator grant required.')
        config=load_pricing(); p=config['partner']; tiers=''.join(f'<label>{t["label"]} discount %<input class="tier" data-index="{i}" type="number" min="0" max="100" step="0.01" value="{t["discount_percent"]}"></label>' for i,t in enumerate(p['volume_tiers']))
        term_inputs=''.join(f'<div class="panel"><strong>{key}</strong><label>Retail monthly price<input class="retail-term" data-key="{key}" type="number" min="0" step="0.01" value="{term.get("retail_monthly_price") if term.get("retail_monthly_price") is not None else ""}" required></label><label>Partner monthly price<input class="partner-term" data-key="{key}" data-field="partner_monthly_price" type="number" min="0" step="0.01" value="{term.get("partner_monthly_price") if term.get("partner_monthly_price") is not None else ""}"></label><label>Partner cost<input class="partner-term" data-key="{key}" data-field="partner_cost" type="number" min="0" step="0.01" value="{term.get("partner_cost") if term.get("partner_cost") is not None else ""}"></label><label>Suggested retail<input class="partner-term" data-key="{key}" data-field="suggested_retail_price" type="number" min="0" step="0.01" value="{term.get("suggested_retail_price") if term.get("suggested_retail_price") is not None else ""}"></label><label>Minimum advertised price<input class="partner-term" data-key="{key}" data-field="minimum_advertised_price" type="number" min="0" step="0.01" value="{term.get("minimum_advertised_price") if term.get("minimum_advertised_price") is not None else ""}"></label><label><input class="partner-check" data-key="{key}" data-field="map_enabled" type="checkbox" {"checked" if term.get("map_enabled") else ""}> Enforce MAP</label></div>' for key,term in p['plan_terms'].items())
        addon_inputs=''.join(f'<div class="panel"><strong>{config["addons"][key]["label"]}</strong><label>Retail price<input class="addon-term" data-key="{key}" data-field="retail_monthly_price" type="number" step="0.01" value="{term["retail_monthly_price"]}"></label><label>Partner price<input class="addon-term" data-key="{key}" data-field="partner_monthly_price" type="number" step="0.01" value="{term.get("partner_monthly_price") if term.get("partner_monthly_price") is not None else ""}"></label><label>Partner cost<input class="addon-term" data-key="{key}" data-field="partner_cost" type="number" step="0.01" value="{term.get("partner_cost") if term.get("partner_cost") is not None else ""}"></label><label>Suggested retail<input class="addon-term" data-key="{key}" data-field="suggested_retail_price" type="number" step="0.01" value="{term.get("suggested_retail_price") if term.get("suggested_retail_price") is not None else ""}"></label><label>MAP<input class="addon-term" data-key="{key}" data-field="minimum_advertised_price" type="number" step="0.01" value="{term.get("minimum_advertised_price") if term.get("minimum_advertised_price") is not None else ""}"></label><label><input class="addon-check" data-key="{key}" data-field="map_enabled" type="checkbox" {"checked" if term.get("map_enabled") else ""}> Enforce MAP</label></div>' for key,term in p['addon_terms'].items())
        content=f'''<header class="topbar"><div><p class="eyebrow">Administrator only</p><h1>Retail and partner pricing controls</h1></div></header><form id="partner-admin" class="rule-form"><section class="panel"><label>Partner pricing method<select id="partner-mode"><option value="fixed">Fixed lower price</option><option value="percentage">Percentage discount from retail</option><option value="volume">Volume-based price</option></select></label><label>Partner discount %<input id="partner-discount" type="number" min="0" max="100" step="0.01" value="{p['percentage_discount']}"></label><label>Trial days<input id="pa-trial" type="number" value="{config['trial_days']}"></label><label>Annual discount %<input id="pa-annual" type="number" value="{config['annual_discount_percent']}"></label><div class="account-grid"><label>Hardware retail<input id="hardware-retail" type="number" step="0.01" value="{p['hardware']['retail_price']}"></label><label>Hardware partner price<input id="hardware-partner" type="number" step="0.01" value="{p['hardware'].get('partner_price') or ''}"></label><label>Hardware cost<input id="hardware-cost" type="number" step="0.01" value="{p['hardware'].get('partner_cost') or ''}"></label><label>Installation retail<input id="installation-retail" type="number" step="0.01" value="{p['installation']['retail_price']}"></label><label>Installation partner price<input id="installation-partner" type="number" step="0.01" value="{p['installation'].get('partner_price') or ''}"></label><label>Installation cost<input id="installation-cost" type="number" step="0.01" value="{p['installation'].get('partner_cost') or ''}"></label></div><h2>Volume tiers</h2>{tiers}</section><h2>Recording plans</h2><div class="account-grid">{term_inputs}</div><h2>Analytics add-ons</h2><div class="account-grid">{addon_inputs}</div><button class="action-button">Save protected pricing</button></form>'''
        scripts='''<script>document.getElementById('partner-mode').value='''+json.dumps(p['pricing_mode'])+''';document.getElementById('partner-admin').addEventListener('submit',async e=>{e.preventDefault();const n=id=>{const v=document.getElementById(id).value;return v===''?null:Number(v)},payload={pricing_mode:document.getElementById('partner-mode').value,percentage_discount:Number(document.getElementById('partner-discount').value),trial_days:Number(document.getElementById('pa-trial').value),annual_discount_percent:Number(document.getElementById('pa-annual').value),plan_terms:{},addon_terms:{},retail_prices:{},volume_tiers:[],hardware:{retail_price:n('hardware-retail'),partner_price:n('hardware-partner'),partner_cost:n('hardware-cost')},installation:{retail_price:n('installation-retail'),partner_price:n('installation-partner'),partner_cost:n('installation-cost')}};document.querySelectorAll('.retail-term').forEach(i=>payload.retail_prices[i.dataset.key]=Number(i.value));document.querySelectorAll('.partner-term').forEach(i=>{payload.plan_terms[i.dataset.key]??={};payload.plan_terms[i.dataset.key][i.dataset.field]=i.value===''?null:Number(i.value)});document.querySelectorAll('.partner-check').forEach(i=>{payload.plan_terms[i.dataset.key]??={};payload.plan_terms[i.dataset.key][i.dataset.field]=i.checked});document.querySelectorAll('.addon-term').forEach(i=>{payload.addon_terms[i.dataset.key]??={};payload.addon_terms[i.dataset.key][i.dataset.field]=i.value===''?null:Number(i.value)});document.querySelectorAll('.addon-check').forEach(i=>{payload.addon_terms[i.dataset.key]??={};payload.addon_terms[i.dataset.key][i.dataset.field]=i.checked});document.querySelectorAll('.tier').forEach(i=>payload.volume_tiers[Number(i.dataset.index)]={discount_percent:Number(i.value)});const response=await fetch('/api/admin/partner-pricing',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message)})</script>'''
        return shell('Partner pricing administration','partner-pricing-admin',content,scripts)
