import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from appliance_protocol import ALLOWED_COMMANDS, RateLimiter, cloud_settings, health_state, sanitize_appliance_payload, validate_request_time
from partner_db import audit, connection, password_hash, row, rows, verify_password
from partner_portal import partner_identity, require_partner_access
from notification_engine import fanout_appliance_event

logger=logging.getLogger('anyaicam.appliance')
request_limiter=RateLimiter(120,60); activation_limiter=RateLimiter(10,300)


def _bearer(request: Request) -> str:
    return request.headers.get('authorization','').removeprefix('Bearer ').strip()


def authenticate_appliance(request: Request) -> dict:
    appliance_id=request.headers.get('x-appliance-id','').strip(); timestamp=request.headers.get('x-request-timestamp',''); nonce=request.headers.get('x-request-nonce','').strip(); credential=_bearer(request)
    if not appliance_id or not timestamp or len(nonce)<16 or not credential: raise HTTPException(status_code=401,detail='Appliance authentication headers are required.')
    try: request_timestamp=int(timestamp)
    except ValueError as error: raise HTTPException(status_code=401,detail='Invalid request timestamp.') from error
    if not validate_request_time(request_timestamp): raise HTTPException(status_code=401,detail='Request timestamp is outside the allowed window.')
    if not request_limiter.allow(appliance_id): raise HTTPException(status_code=429,detail='Appliance request rate exceeded.')
    appliance=row('SELECT * FROM appliances WHERE id=?',(appliance_id,))
    if not appliance or appliance.get('state')=='revoked': raise HTTPException(status_code=403,detail='Appliance is revoked or unknown.')
    credentials=rows('SELECT * FROM appliance_credentials WHERE appliance_id=? AND revoked_at IS NULL',(appliance_id,))
    matched=next((item for item in credentials if verify_password(credential,item['credential_hash'])),None)
    if not matched: raise HTTPException(status_code=403,detail='Invalid appliance credential.')
    try:
        with connection() as db:
            db.execute('DELETE FROM appliance_request_nonces WHERE request_timestamp<?',(int(time.time())-600,)); db.execute('INSERT INTO appliance_request_nonces(appliance_id,nonce,request_timestamp,created_at) VALUES(?,?,?,?)',(appliance_id,nonce,request_timestamp,datetime.now().isoformat())); db.execute('UPDATE appliance_credentials SET last_used_at=? WHERE id=?',(datetime.now().isoformat(),matched['id']))
    except Exception as error:
        raise HTTPException(status_code=409,detail='Duplicate or replayed appliance request.') from error
    return appliance


def register_appliance_cloud_routes(app: FastAPI,shell: Callable) -> None:
    @app.get('/api/appliance/config')
    def appliance_config() -> dict:
        settings=cloud_settings(); return {'mode':settings['mode'],'base_url':settings['base_url'],'mock_cloud':settings['mock'],'timestamp_window_seconds':300,'camera_credentials_allowed':False}

    @app.post('/api/appliance/activate')
    def activate_appliance(request: Request,payload: dict) -> dict:
        client=request.client.host if request.client else 'unknown'
        if not activation_limiter.allow(client): raise HTTPException(status_code=429,detail='Activation attempt rate exceeded.')
        cloud_id=str(payload.get('cloud_id','')).strip().upper(); token=str(payload.get('activation_token','')).strip(); appliance=row('SELECT * FROM appliances WHERE cloud_id=?',(cloud_id,))
        if not appliance or not token: raise HTTPException(status_code=403,detail='Invalid activation request.')
        token_rows=rows('SELECT * FROM appliance_activation_tokens WHERE appliance_id=? AND used_at IS NULL AND revoked_at IS NULL ORDER BY created_at DESC',(appliance['id'],)); now=datetime.now(); match=None
        for candidate in token_rows:
            try: valid_time=datetime.fromisoformat(candidate['expires_at'])>now
            except ValueError: valid_time=False
            if valid_time and verify_password(token,candidate['token_hash']): match=candidate; break
        if not match: raise HTTPException(status_code=403,detail='Activation token is invalid, expired, used, or revoked.')
        credential=secrets.token_urlsafe(48); credential_id=secrets.token_hex(8); now_text=now.isoformat()
        with connection() as db:
            changed=db.execute('UPDATE appliance_activation_tokens SET used_at=? WHERE id=? AND used_at IS NULL',(now_text,match['id'])).rowcount
            if changed!=1: raise HTTPException(status_code=409,detail='Activation token was already used.')
            db.execute('INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,created_by) VALUES(?,?,?,?,?)',(credential_id,appliance['id'],password_hash(credential),now_text,'activation'))
            db.execute("UPDATE appliances SET activation_status='activated',state='offline',partner_id=COALESCE(partner_id,(SELECT partner_id FROM customers WHERE id=appliances.customer_id)) WHERE id=?",(appliance['id'],))
        appliance=row('SELECT * FROM appliances WHERE id=?',(appliance['id'],))
        audit({'email':cloud_id,'role':'appliance'},'appliance.activated','appliance',appliance['id']); logger.info('Appliance activated cloud_id=%s',cloud_id)
        return {'appliance_id':appliance['id'],'cloud_id':cloud_id,'credential':credential,'credential_id':credential_id,'partner_id':appliance.get('partner_id'),'customer_id':appliance['customer_id'],'site_id':appliance['site_id'],'message':'Store this permanent credential securely; it will not be shown again.'}

    @app.post('/api/appliance/heartbeat')
    def heartbeat(request: Request,payload: dict) -> dict:
        appliance=authenticate_appliance(request); safe=sanitize_appliance_payload(payload); state,warnings=health_state(safe); now=datetime.now().isoformat()
        with connection() as db:
            db.execute('UPDATE appliances SET state=?,online_status=?,last_check_in=?,software_version=?,uptime_seconds=?,cpu=?,memory=?,disk_capacity=?,disk=?,recording_used=?,last_error=?,camera_capacity=? WHERE id=?',(state,state,now,safe.get('software_version','Unknown'),int(safe.get('uptime_seconds',0)),float(safe.get('cpu',0)),float(safe.get('memory',0)),float(safe.get('disk_capacity',0)),float(safe.get('disk_used',0)),float(safe.get('recording_used',0)),safe.get('last_error'),int(safe.get('camera_count',0)),appliance['id']))
            db.execute('INSERT INTO appliance_health_history(appliance_id,status,cpu,memory,disk_capacity,disk_used,recording_used,uptime_seconds,camera_count,last_error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(appliance['id'],state,safe.get('cpu',0),safe.get('memory',0),safe.get('disk_capacity',0),safe.get('disk_used',0),safe.get('recording_used',0),safe.get('uptime_seconds',0),safe.get('camera_count',0),safe.get('last_error'),now))
        return {'status':'accepted','state':state,'warnings':warnings,'server_time':int(time.time())}

    @app.post('/api/appliance/health')
    def health(request: Request,payload: dict) -> dict:
        return heartbeat(request,payload)

    @app.post('/api/appliance/version')
    def version(request: Request,payload: dict) -> dict:
        appliance=authenticate_appliance(request); version_value=str(payload.get('software_version','Unknown'))[:80]
        with connection() as db: db.execute('UPDATE appliances SET software_version=?,last_check_in=? WHERE id=?',(version_value,datetime.now().isoformat(),appliance['id']))
        return {'status':'accepted'}

    @app.post('/api/appliance/cameras')
    def cameras(request: Request,payload: dict) -> dict:
        appliance=authenticate_appliance(request); safe=sanitize_appliance_payload(payload); items=safe.get('cameras',[]); now=datetime.now().isoformat()
        with connection() as db:
            for item in items:
                camera_id=str(item.get('id',''))[:100]
                if not camera_id: continue
                db.execute('INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,last_recording_at,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(appliance_id,camera_id) DO UPDATE SET name=excluded.name,online=excluded.online,recording=excluded.recording,analytics=excluded.analytics,last_recording_at=excluded.last_recording_at,last_error=excluded.last_error,updated_at=excluded.updated_at',(appliance['id'],camera_id,item.get('name','Camera'),int(bool(item.get('online'))),int(bool(item.get('recording'))),int(bool(item.get('analytics'))),item.get('last_recording_at'),item.get('last_error'),now))
        return {'status':'accepted','camera_count':len(items),'credentials_received':False}

    @app.get('/api/appliance/configuration')
    def appliance_configuration(request: Request) -> dict:
        appliance=authenticate_appliance(request); camera_items=rows('SELECT id,name,site_id,resolution,status FROM cameras WHERE appliance_id=? ORDER BY name',(appliance['id'],)); return {'configuration_version':max([item.get('status','') for item in camera_items],default='empty'),'cameras':camera_items,'camera_credentials_included':False}

    @app.post('/api/appliance/events')
    def events(request: Request,payload: dict) -> dict:
        appliance=authenticate_appliance(request); safe=sanitize_appliance_payload(payload); inserted=duplicates=0; accepted=[]; now=datetime.now().isoformat()
        with connection() as db:
            for item in safe.get('events',[]):
                event_id=str(item.get('id',''))[:120]
                if not event_id: continue
                cursor=db.execute('INSERT OR IGNORE INTO appliance_events(appliance_id,event_id,event_type,camera_id,event_timestamp,payload_json,received_at) VALUES(?,?,?,?,?,?,?)',(appliance['id'],event_id,item.get('event_type'),item.get('camera_id'),item.get('timestamp'),json.dumps(item),now)); inserted+=cursor.rowcount; duplicates+=1-cursor.rowcount
                if cursor.rowcount: accepted.append(item)
        notifications=sum(fanout_appliance_event(appliance,item) for item in accepted)
        return {'status':'accepted','inserted':inserted,'duplicates':duplicates,'notifications_created':notifications}

    @app.get('/api/appliance/{cloud_id}/scan-jobs')
    def secure_scan_jobs(request: Request,cloud_id: str) -> dict:
        appliance=authenticate_appliance(request)
        if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403,detail='Cloud ID does not match authenticated appliance.')
        jobs=rows("SELECT id,status,created_at FROM camera_scan_jobs WHERE appliance_id=? AND status IN ('queued','waiting_for_appliance') ORDER BY created_at",(appliance['id'],))
        with connection() as db:
            for job in jobs: db.execute("UPDATE camera_scan_jobs SET status='running',progress=5,message='Appliance accepted discovery job.',updated_at=? WHERE id=?",(datetime.now().isoformat(),job['id']))
        return {'jobs':jobs}

    @app.post('/api/appliance/{cloud_id}/scan-jobs/{job_id}')
    def secure_scan_results(request: Request,cloud_id: str,job_id: str,payload: dict) -> dict:
        appliance=authenticate_appliance(request)
        if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403,detail='Cloud ID does not match authenticated appliance.')
        job=row('SELECT * FROM camera_scan_jobs WHERE id=? AND appliance_id=?',(job_id,appliance['id']))
        if not job: raise HTTPException(status_code=404,detail='Discovery job not found.')
        status=str(payload.get('status','running')); progress=max(0,min(100,int(payload.get('progress',0)))); results=sanitize_appliance_payload(payload.get('results',[]))
        if status not in {'running','complete','error'}: raise HTTPException(status_code=400,detail='Unsupported discovery status.')
        with connection() as db:
            db.execute('UPDATE camera_scan_jobs SET status=?,progress=?,results_json=?,message=?,updated_at=? WHERE id=?',(status,progress,json.dumps(results),str(payload.get('message','Discovery update received.'))[:500],datetime.now().isoformat(),job_id))
            if status=='complete':
                for index,item in enumerate(results,1): db.execute('INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(str(item.get('id') or secrets.token_hex(5)),job['customer_id'],appliance['site_id'],appliance['id'],item.get('name') or f'Discovered Camera {index}',item.get('resolution','2mp'),'discovered',datetime.now().isoformat()))
        audit({'email':appliance['cloud_id'],'role':'appliance'},'camera.discovery_result','camera_scan_job',job_id,{'status':status,'count':len(results)}); return {'message':'Discovery result accepted.'}

    @app.get('/api/appliance/commands')
    def appliance_commands(request: Request) -> dict:
        appliance=authenticate_appliance(request); now=datetime.now().isoformat()
        with connection() as db:
            db.execute("UPDATE appliance_commands SET status='expired' WHERE appliance_id=? AND status IN ('pending','delivered') AND expires_at<?",(appliance['id'],now)); commands=[dict(item) for item in db.execute("SELECT * FROM appliance_commands WHERE appliance_id=? AND status='pending' ORDER BY created_at",(appliance['id'],)).fetchall()]
            for item in commands: db.execute("UPDATE appliance_commands SET status='delivered',delivered_at=? WHERE id=?",(now,item['id']))
        for item in commands: audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.command_delivered','appliance_command',item['id'])
        return {'commands':[{'id':item['id'],'command':item['command'],'payload':json.loads(item['payload_json']),'expires_at':item['expires_at']} for item in commands]}

    @app.post('/api/appliance/commands/{command_id}')
    def command_result(request: Request,command_id: str,payload: dict) -> dict:
        appliance=authenticate_appliance(request); status=str(payload.get('status',''))
        if status not in {'completed','failed'}: raise HTTPException(status_code=400,detail='Command result must be completed or failed.')
        with connection() as db:
            changed=db.execute('UPDATE appliance_commands SET status=?,completed_at=?,error=? WHERE id=? AND appliance_id=? AND status IN (\'delivered\',\'pending\')',(status,datetime.now().isoformat(),str(payload.get('error',''))[:500],command_id,appliance['id'])).rowcount
        if not changed: raise HTTPException(status_code=409,detail='Command is unknown or already finalized.')
        audit({'email':appliance['cloud_id'],'role':'appliance'},f'appliance.command_{status}','appliance_command',command_id); return {'status':'accepted'}

    @app.post('/api/admin/appliances/{appliance_id}/activation-token')
    def admin_activation_token(request: Request,appliance_id: str,payload: dict) -> dict:
        identity=require_partner_access(request,{'administrator'}); hours=max(1,min(168,int(payload.get('expires_hours',24)))); token=secrets.token_urlsafe(24); now=datetime.now(); token_id=secrets.token_hex(7)
        with connection() as db:
            db.execute('UPDATE appliance_activation_tokens SET revoked_at=? WHERE appliance_id=? AND used_at IS NULL AND revoked_at IS NULL',(now.isoformat(),appliance_id)); db.execute('INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?)',(token_id,appliance_id,password_hash(token),(now+timedelta(hours=hours)).isoformat(),now.isoformat(),identity['email']))
        audit(identity,'appliance.activation_token_regenerated','appliance',appliance_id,{'expires_hours':hours}); return {'activation_token':token,'expires_at':(now+timedelta(hours=hours)).isoformat(),'message':'Single-use activation token generated.'}

    @app.post('/api/admin/appliances/{appliance_id}/revoke')
    def revoke_appliance(request: Request,appliance_id: str) -> dict:
        identity=require_partner_access(request,{'administrator'}); now=datetime.now().isoformat()
        with connection() as db: db.execute('UPDATE appliance_credentials SET revoked_at=? WHERE appliance_id=? AND revoked_at IS NULL',(now,appliance_id)); db.execute("UPDATE appliances SET state='revoked',online_status='revoked',credential_revoked_at=? WHERE id=?",(now,appliance_id))
        audit(identity,'appliance.credentials_revoked','appliance',appliance_id); return {'message':'Appliance credentials revoked.'}

    @app.post('/api/partner/appliances/{appliance_id}/commands')
    def queue_command(request: Request,appliance_id: str,payload: dict) -> dict:
        identity=require_partner_access(request); command=str(payload.get('command',''))
        if not payload.get('confirmed'): raise HTTPException(status_code=400,detail='Explicit command confirmation is required.')
        if command not in ALLOWED_COMMANDS: raise HTTPException(status_code=400,detail='Only approved appliance commands are allowed. Remote shell is not supported.')
        from partner_db import require_permission
        try: require_permission(identity,'appliance.action')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        command_id=secrets.token_hex(7); now=datetime.now(); expires=now+timedelta(minutes=max(5,min(1440,int(payload.get('expires_minutes',60)))))
        with connection() as db: db.execute('INSERT INTO appliance_commands(id,appliance_id,command,payload_json,status,created_at,expires_at,created_by) VALUES(?,?,?,?,?,?,?,?)',(command_id,appliance_id,command,json.dumps(sanitize_appliance_payload(payload.get('payload',{}))),'pending',now.isoformat(),expires.isoformat(),identity['email']))
        if command=='install_update':
            with connection() as db: db.execute("UPDATE appliances SET state='updating' WHERE id=?",(appliance_id,))
        audit(identity,'appliance.command_queued','appliance_command',command_id,{'command':command,'appliance_id':appliance_id}); return {'id':command_id,'status':'pending','message':'Authorized appliance command queued.'}

    @app.get('/partner/appliance-dashboard',response_class=HTMLResponse)
    def appliance_dashboard(request: Request,partner: str='',customer: str='',site: str='',status: str='',version: str=''):
        identity=partner_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        require_partner_access(request)
        stale_before=(datetime.now()-timedelta(minutes=3)).isoformat()
        with connection() as db: db.execute("UPDATE appliances SET state='offline',online_status='offline' WHERE state IN ('online','degraded') AND (last_check_in IS NULL OR last_check_in<?)",(stale_before,))
        clauses=['1=1']; params=[]
        if identity['role']!='administrator': clauses.append('a.partner_id=?'); params.append(identity.get('partner_id') or 'anyaicam-primary')
        for value,column in [(partner,'a.partner_id'),(customer,'a.customer_id'),(site,'a.site_id'),(status,'a.state'),(version,'a.software_version')]:
            if value: clauses.append(column+'=?'); params.append(value)
        appliances=rows('SELECT a.*,c.name customer_name,s.name site_name FROM appliances a LEFT JOIN customers c ON c.id=a.customer_id LEFT JOIN sites s ON s.id=a.site_id WHERE '+' AND '.join(clauses)+' ORDER BY a.last_check_in DESC',params)
        cards=[]
        for item in appliances:
            history=rows('SELECT * FROM appliance_health_history WHERE appliance_id=? ORDER BY created_at DESC LIMIT 5',(item['id'],)); camera_status=rows('SELECT * FROM appliance_camera_status WHERE appliance_id=?',(item['id'],)); warnings=[]
            if item.get('disk_capacity') and float(item.get('disk') or 0)/float(item['disk_capacity'])>=.9: warnings.append('Low disk')
            if float(item.get('cpu') or 0)>=90: warnings.append('High CPU')
            if any(not c['online'] for c in camera_status): warnings.append('Camera offline')
            if any(c['online'] and not c['recording'] for c in camera_status): warnings.append('Recording stopped')
            cards.append(f'''<article class="panel"><div class="panel-head"><div><h2>{escape(item['cloud_id'])}</h2><div class="health-detail">{escape(item.get('customer_name') or 'Unassigned')} · {escape(item.get('site_name') or 'No site')} · {escape(item.get('software_version') or 'Unknown')}</div></div><span class="pill">{escape(item.get('state') or 'offline')}</span></div><div class="health-row"><span>Last check-in</span><strong>{escape(item.get('last_check_in') or 'Never')}</strong></div><div class="health-row"><span>CPU / Memory / Disk</span><strong>{item.get('cpu',0)}% / {item.get('memory',0)}% / {item.get('disk',0)} GB</strong></div><div class="health-row"><span>Cameras</span><strong>{len(camera_status)}</strong></div><div class="mock-banner" {'' if warnings else 'hidden'}>{', '.join(warnings)}</div><div class="library-toolbar">{''.join(f'<button class="filter queue-command" data-appliance="{item["id"]}" data-command="{command}">{label}</button>' for command,label in [('restart_service','Restart service'),('refresh_cameras','Refresh cameras'),('run_diagnostics','Diagnostics'),('install_update','Install update')])}</div><details><summary>Recent health history ({len(history)})</summary>{''.join(f'<p>{escape(h["created_at"])} · {escape(h["status"])} · CPU {h["cpu"]}%</p>' for h in history)}</details></article>''')
        command_rows=rows('SELECT c.*,a.cloud_id FROM appliance_commands c JOIN appliances a ON a.id=c.appliance_id ORDER BY c.created_at DESC LIMIT 50')
        command_table=''.join(f'<tr><td>{escape(item["cloud_id"])}</td><td>{escape(item["command"].replace("_"," "))}</td><td><span class="pill">{escape(item["status"])}</span></td><td>{escape(item["created_at"])}</td><td>{escape(item.get("error") or "")}</td></tr>' for item in command_rows) or '<tr><td colspan="5">No remote actions have been queued.</td></tr>'
        content=f'''<header class="topbar"><div><p class="eyebrow">Secure appliance fleet</p><h1>Appliance dashboard</h1></div></header><form class="panel clip-form" method="get"><label>Partner<input name="partner" value="{escape(partner,quote=True)}"></label><label>Customer<input name="customer" value="{escape(customer,quote=True)}"></label><label>Site<input name="site" value="{escape(site,quote=True)}"></label><label>Status<select name="status"><option value="">All</option>{''.join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ['online','degraded','offline','updating','revoked'])}</select></label><label>Version<input name="version" value="{escape(version,quote=True)}"></label><button class="action-button">Filter</button></form><div class="account-grid" style="margin-top:18px">{''.join(cards) or '<div class="empty">No appliances match these filters.</div>'}</div><section class="panel" style="margin-top:18px;overflow:auto"><h2>Remote action history</h2><table class="data-table"><thead><tr><th>Appliance</th><th>Command</th><th>Status</th><th>Created</th><th>Error</th></tr></thead><tbody>{command_table}</tbody></table></section>'''
        scripts='''<script>document.querySelectorAll('.queue-command').forEach(button=>button.onclick=async()=>{if(!confirm(`Queue ${button.textContent} for this appliance?`))return;const response=await fetch(`/api/partner/appliances/${button.dataset.appliance}/commands`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:button.dataset.command,confirmed:true})}),r=await response.json();showToast(r.message||r.detail)})</script>'''
        return shell('Appliance dashboard','appliances',content,scripts)
