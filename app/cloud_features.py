import csv
import io
import json
from datetime import datetime
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
    def password_reset_request(payload: dict):
        email=str(payload.get('email','')).strip().lower(); user=row('SELECT id,email FROM partner_users WHERE email=?',(email,))
        if user:
            raw=create_password_reset(user['id'],email); link=settings.password_reset_url+'?token='+raw
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
        content=f'''<header class="topbar"><div><p class="eyebrow">Account security</p><h1>Reset password</h1></div></header><section class="panel" style="max-width:520px;margin:auto"><form id="reset-form" class="rule-form"><input id="reset-token" type="hidden" value="{token}"><label>New password<input id="reset-password" type="password" minlength="12" required></label><button class="action-button">Update password</button></form></section>'''; scripts='''<script>document.getElementById('reset-form').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/password-reset/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.getElementById('reset-token').value,password:document.getElementById('reset-password').value})}),r=await response.json();showToast(r.message||r.detail);if(response.ok)setTimeout(()=>location.href='/partner-login',800)})</script>'''; return shell('Reset password','users',content,scripts)

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
