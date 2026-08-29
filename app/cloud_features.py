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
from email_service import get_email_service
from object_storage import LocalStorage,get_storage,safe_key
from partner_db import audit,connection,row,rows
from partner_portal import partner_identity,require_partner_access


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
        email=str(payload.get('email','')).strip().lower(); user=row('SELECT id,email FROM partner_users WHERE email=?',(email,))
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
                link=f'{scheme}://{host}/reset-password?token={raw}'
            else:
                link=settings.password_reset_url+'?token='+raw
            message=get_email_service().send('password_reset',email,'Reset your AnyAiCam password',f'Use this one-hour password reset link:\n{link}',metadata={'expires_minutes':60})
            with connection() as db: db.execute('INSERT INTO email_messages(id,message_type,recipient,status,provider,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)',(message.get('id',datetime.now().strftime('%Y%m%d%H%M%S%f')),'password_reset',email,message['status'],settings.email_backend,json.dumps({'expires_minutes':60}),datetime.now().isoformat()))
            audit({'email':email,'role':'account'},'password_reset.requested','partner_user',user['id'],{'provider':settings.email_backend})
        return {'message':'If the account exists, a password-reset message has been prepared.'}

    @app.get('/forgot-password',response_class=HTMLResponse)
    def forgot_password_page():
        content='''<header class="topbar"><div><p class="eyebrow">Account security</p><h1>Forgot password</h1></div></header><section class="panel" style="max-width:520px;margin:auto"><form id="forgot-form" class="rule-form"><label>Account email<input id="forgot-email" type="email" required></label><button class="action-button">Prepare reset message</button></form><p class="health-detail">Local development writes the reset message to the email-preview folder. Production uses the configured email provider.</p></section>'''; scripts='''<script>document.getElementById('forgot-form').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/password-reset/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('forgot-email').value})}),r=await response.json();showToast(r.message)})</script>'''; return shell('Forgot password','users',content,scripts)

    @app.post('/api/password-reset/complete')
    def password_reset_complete(payload: dict):
        password=str(payload.get('password',''))
        if len(password)<12: raise HTTPException(status_code=400,detail='Password must contain at least 12 characters.')
        if not consume_password_reset(str(payload.get('token','')),password):
            audit({'email':'unknown','role':'anonymous'},'password_reset.failed','partner_user','',{'reason':'invalid_or_expired'})
            raise HTTPException(status_code=400,detail='Reset token is invalid, expired, or already used.')
        audit({'email':'password-reset','role':'account'},'password_reset.completed','partner_user','')
        return {'message':'Password updated. You can now sign in.'}

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
        content=f'''<header class="topbar"><div><p class="eyebrow">Account security</p><h1>Reset password</h1></div></header><section class="panel" style="max-width:520px;margin:auto"><form id="reset-form" class="rule-form"><input id="reset-token" type="hidden" value="{safe_token}"><label>New password<input id="reset-password" type="password" minlength="12" required></label><button class="action-button">Update password</button></form></section>'''; scripts='''<script>document.getElementById('reset-form').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/password-reset/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.getElementById('reset-token').value,password:document.getElementById('reset-password').value})}),r=await response.json();showToast(r.message||r.detail);if(response.ok)setTimeout(()=>location.href='/partner-login',800)})</script>'''; return shell('Reset password','users',content,scripts)

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
<script>document.getElementById('forgot-form').addEventListener('submit',async e=>{{e.preventDefault();const response=await fetch('/api/password-reset/request',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:document.getElementById('forgot-email').value}})}}),r=await response.json(),box=document.getElementById('message');box.textContent=r.message||'If the account exists, a reset message has been prepared.';box.style.display='block'}});</script>
</body></html>''')

    @app.get('/customer-reset-password',response_class=HTMLResponse)
    def customer_reset_password_page(token: str=''):
        safe_token=escape(token,quote=True)
        return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reset password | ANY AI CAM</title><style>{_CUSTOMER_AUTH_STYLE}</style></head><body>
<header class="head"><a class="brand" href="/customer-login.html"><img src="/static/brand-icon.png" alt="AnyAiCam">ANY AI CAM</a></header>
<main class="auth-wrap"><section class="card"><h2>Reset your password</h2><form id="reset-form"><input id="reset-token" type="hidden" value="{safe_token}"><label>New password<input id="reset-password" type="password" minlength="12" autocomplete="new-password" required></label><div id="message" class="message"></div><button class="submit">Update password</button></form><a class="back-link" href="/customer-login.html">Back to customer sign in</a></section></main>
<script>document.getElementById('reset-form').addEventListener('submit',async e=>{{e.preventDefault();const response=await fetch('/api/password-reset/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:document.getElementById('reset-token').value,password:document.getElementById('reset-password').value}})}}),r=await response.json(),box=document.getElementById('message');box.textContent=r.message||r.detail;box.style.display='block';if(response.ok)setTimeout(()=>location.href='/customer-login.html',900)}});</script>
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
        identity=require_partner_access(request); quote=row('SELECT * FROM quotes WHERE id=?',(quote_id,))
        if not quote: raise HTTPException(status_code=404,detail='Quote not found.')
        recipient=str(payload.get('email','')).strip(); message=get_email_service().send('quote_delivery',recipient,'Your AnyAiCam quote','Your AnyAiCam quote is ready for review.',metadata={'quote_id':quote_id}); audit(identity,'quote.delivered','quote',quote_id,{'recipient':recipient,'provider':settings.email_backend}); return {'message':'Quote delivery processed.','status':message['status']}
