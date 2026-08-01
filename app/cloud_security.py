import hmac
import secrets
import time
from datetime import datetime,timedelta

from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse,RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cloud_config import settings
from partner_db import connection,password_hash,row,verify_password
from token_security import sign,unsign
from redirect_security import safe_redirect


class ProductionSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self,request: Request,call_next):
        origin=request.headers.get('origin','').rstrip('/')
        if origin and origin not in settings.allowed_origins:
            return JSONResponse({'detail':'Origin is not allowed.'},status_code=403)
        if request.method=='OPTIONS':
            response=JSONResponse({},status_code=204)
        else:
            response=None
        if settings.production and settings.https_only and request.headers.get('x-forwarded-proto',request.url.scheme)!='https':
            destination=settings.portal_url.rstrip('/')+request.url.path
            if request.url.query: destination+='?'+request.url.query
            return RedirectResponse(destination,status_code=308)
        bearer=request.headers.get('authorization','').lower().startswith('bearer ')
        if settings.csrf_enabled and not bearer and request.method in {'POST','PUT','PATCH','DELETE'} and not request.url.path.startswith('/api/appliance/') and request.url.path!='/partner-logout':
            cookie=request.cookies.get('anyaicam_csrf'); header=request.headers.get('x-csrf-token')
            if not cookie or not header or not hmac.compare_digest(cookie,header) or unsign(cookie)!='csrf': return JSONResponse({'detail':'CSRF validation failed.'},status_code=403)
        if response is None: response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'; response.headers['X-Frame-Options']='DENY'; response.headers['Referrer-Policy']='same-origin'; response.headers['Permissions-Policy']='camera=(self), microphone=(self)'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' "+' '.join(settings.allowed_origins)+"; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if 'server' in response.headers: del response.headers['server']
        if origin:
            response.headers['Access-Control-Allow-Origin']=origin; response.headers['Access-Control-Allow-Credentials']='true'; response.headers['Vary']='Origin'; response.headers['Access-Control-Allow-Headers']='Content-Type, X-CSRF-Token, Authorization, X-Customer-ID'; response.headers['Access-Control-Allow-Methods']='GET, POST, PUT, PATCH, DELETE, OPTIONS'
        if settings.csrf_enabled and not request.cookies.get('anyaicam_csrf'): response.set_cookie('anyaicam_csrf',sign('csrf',28800),secure=settings.secure_cookies,httponly=False,samesite='strict',max_age=28800,domain=settings.cookie_domain or None)
        if settings.production: response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
        if settings.staging: response.headers['X-AnyAiCam-Environment']='staging'
        return response


def login_blocked(email: str):
    record=row('SELECT * FROM account_lockouts WHERE email=?',(email.lower(),)); return bool(record and record.get('locked_until') and datetime.fromisoformat(record['locked_until'])>datetime.now())


def record_login_failure(email: str):
    now=datetime.now(); record=row('SELECT * FROM account_lockouts WHERE email=?',(email.lower(),)); attempts=(record['attempts'] if record else 0)+1; locked=(now+timedelta(minutes=settings.login_lockout_minutes)).isoformat() if attempts>=settings.login_attempt_limit else None
    with connection() as db: db.execute('INSERT INTO account_lockouts(email,attempts,locked_until,last_attempt_at) VALUES(?,?,?,?) ON CONFLICT(email) DO UPDATE SET attempts=excluded.attempts,locked_until=excluded.locked_until,last_attempt_at=excluded.last_attempt_at',(email.lower(),attempts,locked,now.isoformat()))


def clear_login_failures(email: str):
    with connection() as db: db.execute('DELETE FROM account_lockouts WHERE email=?',(email.lower(),))


def create_password_reset(user_id: str,email: str):
    raw=secrets.token_urlsafe(32); token_hash=password_hash(raw); now=datetime.now()
    with connection() as db: db.execute('INSERT INTO password_reset_tokens(id,user_id,email,token_hash,expires_at,used_at,created_at) VALUES(?,?,?,?,?,?,?)',(secrets.token_hex(8),user_id,email,token_hash,(now+timedelta(hours=1)).isoformat(),None,now.isoformat()))
    return raw


def consume_password_reset(raw: str,new_password: str):
    records=[]
    with connection() as db: records=[dict(item) for item in db.execute('SELECT * FROM password_reset_tokens WHERE used_at IS NULL AND expires_at>?',(datetime.now().isoformat(),)).fetchall()]
    match=next((item for item in records if verify_password(raw,item['token_hash'])),None)
    if not match: return False
    with connection() as db: db.execute('UPDATE partner_users SET password_hash=? WHERE id=?',(password_hash(new_password),match['user_id'])); db.execute('UPDATE password_reset_tokens SET used_at=? WHERE id=?',(datetime.now().isoformat(),match['id']))
    return True
