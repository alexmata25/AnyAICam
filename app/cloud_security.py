import hmac
import secrets
import time
import os
from datetime import datetime,timedelta

from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse,RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cloud_config import settings
from partner_db import connection,password_hash,row,verify_password
from token_security import sign,unsign
from redirect_security import safe_redirect

def _media_src_csp() -> str:
    """The <video> element that plays a customer's recordings loads
    directly from a real presigned S3 URL (see _presigned_recording_url()
    in main.py) -- media-src must allow that exact bucket's origin or
    every browser blocks the load as a CSP violation before the video
    element ever gets a chance to fetch anything. This was the real,
    silent root cause behind a customer-reported Playback regression:
    every server-side and curl/node-based test of the presigned URL
    succeeded (CSP is a browser-only enforcement, invisible to any
    non-browser HTTP client), while every real browser -- hard refresh,
    incognito, made no difference -- silently refused to load it,
    leaving the player permanently at 0:00 with no console-visible
    network failure. Falls back to media-src 'self' blob: only (today's
    prior behavior) if the recording bucket is not configured -- never
    widened beyond exactly this one bucket."""
    bucket = os.environ.get("ANYAICAM_RECORDING_S3_BUCKET", "").strip()
    if not bucket:
        return "media-src 'self' blob:"
    return f"media-src 'self' blob: https://{bucket}.s3.amazonaws.com"


def _img_src_csp() -> str:
    """Allow customer recording/event thumbnails from the same exact
    recordings bucket used by Playback media, while keeping the image
    policy otherwise restricted to same-origin/data/blob sources."""
    bucket = os.environ.get("ANYAICAM_RECORDING_S3_BUCKET", "").strip()
    if not bucket:
        return "img-src 'self' data: blob:"
    return f"img-src 'self' data: blob: https://{bucket}.s3.amazonaws.com"


def _connect_src_csp() -> str:
    """Allow browser/service-worker fetches to the exact recordings
    bucket used by presigned Playback media and thumbnails."""
    sources = [
        "'self'",
        *settings.allowed_origins,
        "https://d31cxfv0l904ar.cloudfront.net",
    ]
    bucket = os.environ.get("ANYAICAM_RECORDING_S3_BUCKET", "").strip()
    if bucket:
        sources.append(f"https://{bucket}.s3.amazonaws.com")
    return "connect-src " + " ".join(sources)


def _frame_ancestors_csp(request: Request) -> str:
    """'none' (never framable, by anyone, anywhere) for every page
    except the customer single-camera Live view -- confirmed live
    (2026-09-23, found via independent review of AAC Voice Call Phase
    1): that page's own AAC Voice Call call screen (main.py's
    aac_voice_call.py, GET /aac/voice-call/{event_id}) embeds it via
    <iframe> as its camera video, exactly matching the product spec's
    own "reuse the existing Live camera... instead of creating a
    completely separate streaming system" instruction -- but the
    blanket frame-ancestors 'none'/X-Frame-Options: DENY below applied
    unconditionally to every route including this one, so the browser
    silently refused to render the iframe at all ("refused to
    connect"), even though the embedding page is this SAME application,
    already carrying the same authenticated session. 'self' here means
    same-origin framing only -- an attacker's site still cannot frame
    this page from anywhere else; only this application can frame its
    own already-authenticated page, and only for this one route."""
    path = request.url.path
    if path.startswith("/customer/cameras/") and path.endswith("/live"):
        return "self"
    return "none"


_MAX_CSRF_FORM_BODY_BYTES = 65_536  # generous for a login/registration form; not a general upload limit

# Provisioning Phase 4: Stripe's real webhook POST carries a
# Stripe-Signature header, never our anyaicam_csrf cookie/token pair --
# Stripe cannot present a token it was never issued. Confirmed live on
# app.anyaicam.com: every POST to this route, including one with a
# stripe-signature header, was rejected 403 "CSRF validation failed"
# before ever reaching that route's own cryptographic signature check
# (verify_stripe_webhook_signature() in main.py), which means no real
# Stripe event could ever have been processed. Exempted by EXACT path
# only -- not a prefix -- so this never widens to /api/payments/* or
# /api/* (e.g. POST /api/payments/checkout, a browser-originated
# request that legitimately carries the CSRF cookie/token, keeps
# requiring it exactly as before). The route itself remains fully
# protected: it still requires and verifies Stripe-Signature against
# the configured webhook signing secret before doing anything else --
# this exemption removes only the CSRF check, never authentication.
#
# POST /api/provisioning/refresh (provisioning_api.py) is appliance-
# authenticated exactly like every /api/appliance/* route (signed
# X-Appliance-Id/X-Request-Timestamp/X-Request-Nonce/Bearer credential,
# already CSRF-exempt via this dispatch method's own `not bearer` check
# above whenever a real appliance sends its Bearer credential) but lives
# under a different path prefix, so the '/api/appliance/' prefix
# exemption below never covered it. Added here, by exact path, for the
# same reason as the webhook route above: an appliance request with no
# credentials at all (e.g. a misconfigured/compromised device, or this
# module's own test coverage of that case) must still fail with
# authenticate_appliance()'s own 401, not a misleading "CSRF validation
# failed" that has nothing to do with why the request was rejected.
CSRF_EXEMPT_EXACT_PATHS = {'/api/payments/stripe/webhook', '/api/provisioning/refresh'}


class ProductionSecurityMiddleware(BaseHTTPMiddleware):
    @staticmethod
    def _unquote_double_submit_value(value: str) -> str:
        # Mirrors Starlette's own Cookie-header unquoting (http.cookies
        # unquotes on read). A double-submit token read client-side from
        # document.cookie can still carry a literal wrapping quote pair --
        # e.g. any cookie a pre-fix server issued with base64 '=' padding,
        # already sitting in someone's browser -- while request.cookies.get()
        # never does. Stripping one matching pair here makes the two sides
        # comparable again without touching the HMAC/signature check itself.
        if len(value)>=2 and value[0]=='"'==value[-1]: return value[1:-1]
        return value

    @staticmethod
    async def _read_body_capped(request: Request, limit: int):
        # Mirrors Request.body()'s own caching -- sets request._body, which
        # BaseHTTPMiddleware's _CachedRequest replays to the downstream app --
        # but reads via .stream() and stops as soon as more than `limit`
        # bytes have arrived, instead of buffering an unbounded or chunked
        # body in full first. Returns the cached bytes, or None if the body
        # exceeded the cap (nothing is cached in that case).
        if hasattr(request,'_body'):
            body=request._body
            return body if len(body)<=limit else None
        chunks=[]; total=0
        try:
            async for chunk in request.stream():
                total+=len(chunk)
                if total>limit: return None
                chunks.append(chunk)
        except Exception: return None
        body=b''.join(chunks)
        request._body=body
        return body

    async def dispatch(self,request: Request,call_next):
        origin=request.headers.get('origin','').rstrip('/')
        method=request.method.upper()
        unsafe_method=method in {'POST','PUT','PATCH','DELETE'}
        preflight=method=='OPTIONS'

        # Allow ordinary browser page navigation. Enforce the origin allowlist
        # only for CORS preflight and requests that can change server state.
        # effective_allowed_origins (cloud_config.py) is settings.allowed_
        # origins itself for every profile except edge_production with an
        # untouched default, where it's ["*"] -- an edge appliance has no
        # fixed address to enumerate in advance, unlike cloud's single
        # fixed public domain, so "*" here means "any origin accepted",
        # never a literal Origin header value a browser could send.
        allowed_origins=settings.effective_allowed_origins
        if origin and (unsafe_method or preflight) and '*' not in allowed_origins and origin not in allowed_origins:
            return JSONResponse({'detail':'Origin is not allowed.'},status_code=403)

        if preflight:
            response=JSONResponse({},status_code=204)
        else:
            response=None
        if settings.production and settings.https_only and request.headers.get('x-forwarded-proto',request.url.scheme)!='https':
            destination=settings.portal_url.rstrip('/')+request.url.path
            if request.url.query: destination+='?'+request.url.query
            return RedirectResponse(destination,status_code=308)
        bearer=request.headers.get('authorization','').lower().startswith('bearer ')
        if settings.csrf_enabled and not bearer and request.method in {'POST','PUT','PATCH','DELETE'} and not request.url.path.startswith('/api/appliance/') and request.url.path!='/partner-logout' and request.url.path not in CSRF_EXEMPT_EXACT_PATHS:
            cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
            # No cookie means the final check below can never pass regardless of
            # what the body contains -- fail now instead of buffering a body an
            # anonymous, cookie-less caller controls the size of.
            if not cookie: return JSONResponse({'detail':'CSRF validation failed.'},status_code=403)
            if not token and request.headers.get('content-type','').split(';')[0].strip().lower()=='application/x-www-form-urlencoded':
                # Plain HTML form posts (e.g. /login) can't set a custom header, so accept the
                # same double-submit token from a hidden form field as a fallback.
                # A declared Content-Length that's negative, unparsable, or over the
                # cap can be rejected without reading anything. An absent
                # Content-Length (e.g. chunked transfer-encoding) is not itself
                # disqualifying -- _read_body_capped below bounds that case instead.
                content_length=request.headers.get('content-length')
                try:
                    parsed_length=int(content_length) if content_length is not None else None
                    declared_oversized=parsed_length is not None and (parsed_length<0 or parsed_length>_MAX_CSRF_FORM_BODY_BYTES)
                except ValueError: declared_oversized=True
                if declared_oversized: return JSONResponse({'detail':'CSRF validation failed.'},status_code=403)
                # Read the body ourselves, capped at _MAX_CSRF_FORM_BODY_BYTES, and
                # cache it the same way Request.body() does so BaseHTTPMiddleware
                # replays it downstream (otherwise routes with their own Form(...)
                # params, e.g. /login, /customer-register, /register, would receive
                # an empty body). This also bounds chunked/no-Content-Length bodies:
                # reading stops as soon as the cap is exceeded rather than buffering
                # an unbounded stream first.
                if await self._read_body_capped(request,_MAX_CSRF_FORM_BODY_BYTES) is None:
                    return JSONResponse({'detail':'CSRF validation failed.'},status_code=403)
                try: form_token=(await request.form()).get('csrf_token')
                except Exception: form_token=None
                if isinstance(form_token,str): token=form_token
            if isinstance(token,str): token=self._unquote_double_submit_value(token)
            if not cookie or not token or not hmac.compare_digest(cookie,token) or unsign(cookie)!='csrf': return JSONResponse({'detail':'CSRF validation failed.'},status_code=403)
        if response is None: response=await call_next(request)
        frame_ancestors=_frame_ancestors_csp(request)
        response.headers['X-Content-Type-Options']='nosniff'; response.headers['X-Frame-Options']='DENY' if frame_ancestors=='none' else 'SAMEORIGIN'; response.headers['Referrer-Policy']='same-origin'; response.headers['Permissions-Policy']='camera=(self), microphone=(self)'
        response.headers['Content-Security-Policy']="default-src 'self'; "+_img_src_csp()+"; "+_media_src_csp()+"; "+_connect_src_csp()+"; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; worker-src 'self' blob:; frame-ancestors '"+frame_ancestors+"'; base-uri 'self'; form-action 'self'"
        if 'server' in response.headers: del response.headers['server']
        if origin:
            response.headers['Access-Control-Allow-Origin']=origin; response.headers['Access-Control-Allow-Credentials']='true'; response.headers['Vary']='Origin'; response.headers['Access-Control-Allow-Headers']='Content-Type, X-CSRF-Token, Authorization, X-Customer-ID'; response.headers['Access-Control-Allow-Methods']='GET, POST, PUT, PATCH, DELETE, OPTIONS'
        if settings.csrf_enabled and not request.cookies.get('anyaicam_csrf'): response.set_cookie('anyaicam_csrf',sign('csrf',28800),secure=settings.secure_cookies,httponly=False,samesite='strict',max_age=28800,domain=settings.cookie_domain or None)
        # Confirmed live on Samsung: this used to fire for any production
        # profile, so a plain-HTTP edge appliance (no TLS listener at
        # all) was telling browsers "only ever connect to this host over
        # HTTPS" -- a promise the appliance can't keep, and one that
        # locks a customer's browser out of the LAN/Tailscale address
        # they actually use. Cloud/combined production is unaffected and
        # still gets this unconditionally, same as before.
        if settings.production and not settings.edge_production: response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
        if settings.staging: response.headers['X-AnyAiCam-Environment']='staging'
        return response


def login_blocked(email: str):
    record=row('SELECT * FROM account_lockouts WHERE email=?',(email.lower(),))
    if not record or not record.get('locked_until'):
        return False
    return datetime.fromisoformat(record['locked_until']) > datetime.now()


def record_login_failure(email: str):
    now=datetime.now(); record=row('SELECT * FROM account_lockouts WHERE email=?',(email.lower(),)); attempts=0
    if record:
        # A lockout is a bounded security window, not a permanent strike
        # counter.  Once its timer has elapsed, the next bad password starts
        # a fresh window instead of immediately re-locking the customer.
        last_attempt=record.get('last_attempt_at')
        locked_until=record.get('locked_until')
        expired_lock=bool(locked_until and datetime.fromisoformat(locked_until)<=now)
        stale_attempt=bool(last_attempt and datetime.fromisoformat(last_attempt)<=now-timedelta(minutes=settings.login_lockout_minutes))
        if not expired_lock and not stale_attempt:
            attempts=record['attempts']
    attempts+=1; locked=(now+timedelta(minutes=settings.login_lockout_minutes)).isoformat() if attempts>=settings.login_attempt_limit else None
    with connection() as db: db.execute('INSERT INTO account_lockouts(email,attempts,locked_until,last_attempt_at) VALUES(?,?,?,?) ON CONFLICT(email) DO UPDATE SET attempts=excluded.attempts,locked_until=excluded.locked_until,last_attempt_at=excluded.last_attempt_at',(email.lower(),attempts,locked,now.isoformat()))


def clear_login_failures(email: str):
    with connection() as db: db.execute('DELETE FROM account_lockouts WHERE email=?',(email.lower(),))


def create_password_reset(user_id: str,email: str):
    raw=secrets.token_urlsafe(32); token_hash=password_hash(raw); now=datetime.now()
    with connection() as db: db.execute('INSERT INTO password_reset_tokens(id,user_id,email,token_hash,expires_at,used_at,created_at) VALUES(?,?,?,?,?,?,?)',(secrets.token_hex(8),user_id,email,token_hash,(now+timedelta(hours=1)).isoformat(),None,now.isoformat()))
    return raw


def consume_password_reset(raw: str,new_password: str):
    """Returns the account's role on success (a truthy string -- every
    partner_users role is a non-empty value), or None if the token was
    invalid/expired/already used. Callers that only care about success/
    failure can keep using this exactly like the old bool return; the
    role is additionally needed to route a customer_owner/customer_viewer
    account back to the customer sign-in page instead of the partner one
    (see password_reset_complete() in cloud_features.py) -- confirmed
    live on staging: the reset page previously always redirected to
    /partner-login regardless of role, which (a) is the wrong page for a
    customer account and (b) wasn't even reachable pre-login itself (see
    PUBLIC_PATH_PREFIXES's own history), together producing the exact
    "redirected to the emergency recovery page, then invalid email or
    password" report this fixes.
    also clears must_change_password: the user just set a real password
    of their own choosing through this exact flow, so forcing them
    through ANOTHER "create your permanent password" step immediately
    after logging in would be a confusing loop, not a security
    improvement -- must_change_password exists for a partner-issued
    temporary password the recipient has never chosen themselves, which
    this is no longer true of the moment this function runs."""
    records=[]
    with connection() as db: records=[dict(item) for item in db.execute('SELECT * FROM password_reset_tokens WHERE used_at IS NULL AND expires_at>?',(datetime.now().isoformat(),)).fetchall()]
    match=next((item for item in records if verify_password(raw,item['token_hash'])),None)
    if not match: return None
    with connection() as db:
        now=datetime.now().isoformat()
        # Claim the token atomically.  A concurrent reset cannot reuse a
        # token after this update, and all other outstanding reset links for
        # the account are invalidated as soon as the password changes.
        claimed=db.execute('UPDATE password_reset_tokens SET used_at=? WHERE id=? AND used_at IS NULL',(now,match['id']))
        if not claimed.rowcount:
            return None
        db.execute('UPDATE partner_users SET password_hash=?,must_change_password=0 WHERE id=?',(password_hash(new_password),match['user_id']))
        db.execute('UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL',(now,match['user_id']))
        db.execute('DELETE FROM account_lockouts WHERE email=?',(match['email'].lower(),))
        db.execute('UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(now,match['user_id']))
        role_row=db.execute('SELECT role FROM partner_users WHERE id=?',(match['user_id'],)).fetchone()
    return role_row['role'] if role_row else None
