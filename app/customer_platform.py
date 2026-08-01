import hashlib
import json
import secrets
from datetime import datetime,timedelta
from html import escape
from pathlib import Path

from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,HTMLResponse

from cloud_config import settings
from cloud_security import clear_login_failures,login_blocked,record_login_failure
from partner_db import audit,authenticate_detailed,connection,password_hash,row,rows
from partner_portal import AUTH_ROLES,PARTNER_ROLES,partner_identity
from customer_policy import CUSTOMER_ROLES,camera_action_allowed,live_session_state
from mobile_security import issue_mobile_tokens,register_device

PAGE=Path(__file__).with_name('customer-login.html')
EVENT_TYPES={'motion','person','vehicle','line_crossing','intrusion','lpr','people_counting','occupancy','health'}


def _bearer_identity(request: Request):
    authorization=request.headers.get('authorization','')
    if not authorization.lower().startswith('bearer '): return None
    digest=hashlib.sha256(authorization.split(' ',1)[1].encode()).hexdigest(); now=datetime.now().isoformat()
    with connection() as db:
        record=db.execute('''SELECT s.id AS session_id,s.email,s.role,u.id AS user_id,u.partner_id,u.customer_id
            FROM user_sessions s LEFT JOIN partner_users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.session_type='api' AND s.revoked_at IS NULL AND s.expires_at>?''',(digest,now)).fetchone()
        if record:
            db.execute('UPDATE user_sessions SET last_seen_at=? WHERE id=?',(now,record['session_id'])); session=db.execute('SELECT device_name FROM user_sessions WHERE id=?',(record['session_id'],)).fetchone()
            if session and str(session['device_name'] or '').startswith('mobile:'): db.execute('UPDATE mobile_devices SET last_active_at=? WHERE id=?',(now,session['device_name'].split(':',1)[1]))
    return dict(record) if record else None


def api_identity(request: Request):
    identity=_bearer_identity(request) or partner_identity(request)
    if not identity: raise HTTPException(status_code=401,detail='Sign in is required.')
    if not identity.get('user_id'):
        user=row('SELECT id,partner_id,customer_id FROM partner_users WHERE lower(email)=?',(identity['email'].lower(),))
        if user: identity={**identity,'user_id':user['id'],'partner_id':user.get('partner_id'),'customer_id':user.get('customer_id')}
    return identity


def customer_scope(request: Request,identity=None):
    identity=identity or api_identity(request); role=identity.get('role')
    if role in CUSTOMER_ROLES:
        customer_id=identity.get('customer_id')
    elif role=='administrator':
        customer_id=request.headers.get('x-customer-id') or request.query_params.get('customer_id')
        if not customer_id:
            first=row('SELECT id FROM customers ORDER BY created_at LIMIT 1'); customer_id=first['id'] if first else None
    else: raise HTTPException(status_code=403,detail='Customer Portal access is not available for this role.')
    if not customer_id or not row('SELECT id FROM customers WHERE id=?',(customer_id,)): raise HTTPException(status_code=404,detail='Customer account not found.')
    return identity,customer_id


def _camera_access(identity,camera_id,action='live'):
    camera=row('SELECT * FROM cameras WHERE id=? AND customer_id=?',(camera_id,identity['customer_id']))
    if not camera: raise HTTPException(status_code=404,detail='Camera not found in this customer account.')
    permission=row('SELECT * FROM customer_camera_permissions WHERE user_id=? AND camera_id=?',(identity.get('user_id'),camera_id))
    permission_count=row('SELECT COUNT(*) AS count FROM customer_camera_permissions WHERE user_id=?',(identity.get('user_id'),))['count']
    if not camera_action_allowed(identity['role'],action,permission,permission_count): raise HTTPException(status_code=403,detail=f'{action.title()} permission is required for this camera.')
    return camera


def _payload(item):
    try: return json.loads(item.get('payload_json') or '{}')
    except json.JSONDecodeError: return {}


def _recording_resource(customer_id,recording_id):
    records=rows('''SELECT e.camera_id,e.payload_json FROM appliance_events e JOIN appliances a ON a.id=e.appliance_id
        WHERE a.customer_id=? AND e.payload_json LIKE ? LIMIT 200''',(customer_id,'%'+str(recording_id)+'%'))
    for item in records:
        payload=_payload(item)
        if str(payload.get('recording_id') or payload.get('linked_recording') or '')==str(recording_id): return {**payload,'camera_id':item.get('camera_id')}
    return None


def register_customer_platform_routes(app: FastAPI):
    @app.get('/customer-login.html')
    def customer_login_page(): return FileResponse(PAGE,media_type='text/html')

    @app.post('/api/v1/auth/token')
    def mobile_token(payload: dict,request: Request):
        email=str(payload.get('email','')).strip().lower()
        if login_blocked(email): audit({'email':email,'role':'anonymous'},'api_login.blocked','user','',{'reason':'rate_limit'}); raise HTTPException(status_code=429,detail='This account is temporarily locked. Try again later or reset the password.')
        user,reason=authenticate_detailed(email,str(payload.get('password','')))
        if not user or user['role'] not in AUTH_ROLES:
            record_login_failure(email); audit({'email':email,'role':'anonymous'},'api_login.failed','user','',{'reason':reason}); raise HTTPException(status_code=403,detail='The email, password, or account status is not valid.')
        clear_login_failures(email)
        device_uid=str(payload.get('device_id') or secrets.token_urlsafe(18))[:200]; device_id=register_device(user,device_uid,str(payload.get('device_type') or 'mobile')[:50],str(payload.get('platform') or 'browser')[:50],str(payload.get('app_version') or '')[:50],str(payload.get('push_token') or '')[:500]); tokens=issue_mobile_tokens(user,device_id)
        audit(user,'api_login.succeeded','user_session',tokens['session_id'],{'device_id':device_id,'platform':payload.get('platform')}); audit(user,'device.registered','mobile_device',device_id,{'device_uid':device_uid}); return {**tokens,'role':user['role']}

    @app.get('/api/v1/me')
    def me(request: Request):
        identity=api_identity(request); return {'email':identity['email'],'role':identity['role'],'customer_id':identity.get('customer_id'),'partner_id':identity.get('partner_id'),'mfa':row('SELECT enabled,method,status FROM mfa_settings WHERE user_id=?',(identity.get('user_id'),)) or {'enabled':0,'status':'not_configured'}}

    @app.post('/api/v1/auth/logout-all')
    def logout_all(request: Request):
        identity=api_identity(request); now=datetime.now().isoformat()
        with connection() as db:
            db.execute('UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(now,identity.get('user_id')))
            db.execute('UPDATE mobile_refresh_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(now,identity.get('user_id')))
        audit(identity,'sessions.revoked','partner_user',identity.get('user_id',''),{'scope':'all_devices'}); return {'message':'All device sessions have been signed out.'}

    @app.get('/api/v1/auth/sessions')
    def active_sessions(request: Request):
        identity=api_identity(request); return rows('SELECT id,device_name,session_type,created_at,last_seen_at,expires_at,ip_address FROM user_sessions WHERE user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY last_seen_at DESC',(identity.get('user_id'),datetime.now().isoformat()))

    @app.get('/api/v1/customer')
    def current_customer(request: Request):
        identity,customer_id=customer_scope(request); customer=row('SELECT id,name,company,email,phone,status,trial_status,billing_status,created_at FROM customers WHERE id=?',(customer_id,)); customer['viewer_role']=identity['role']; return customer

    @app.get('/api/v1/dashboard')
    def customer_dashboard(request: Request):
        identity,customer_id=customer_scope(request); sites=customer_sites(request); cameras=customer_cameras(request); appliances=customer_appliances(request); plan=row('SELECT resolution,recording_mode,retention_days,camera_quantity,retail_monthly,status FROM plans WHERE customer_id=? ORDER BY created_at DESC LIMIT 1',(customer_id,)) or {}; alerts=customer_alerts(request,limit=8); recordings=customer_recordings(request)
        return {'sites':len(sites),'cameras':len(cameras),'appliances':len(appliances),'online_cameras':sum(bool(x['online']) for x in cameras),'recording_cameras':sum(bool(x['recording']) for x in cameras),'recent_alerts':alerts[:8],'recent_recordings':recordings[:8],'plan':plan,'role':identity['role']}

    @app.get('/api/v1/appliances')
    def customer_appliances(request: Request):
        identity,customer_id=customer_scope(request); appliances=rows('''SELECT a.id,a.cloud_id,a.appliance_type,a.software_version,a.last_check_in,a.online_status,a.cpu,a.memory,a.disk,a.camera_capacity,a.state,a.last_error,s.id AS site_id,s.name AS site_name FROM appliances a JOIN sites s ON s.id=a.site_id WHERE a.customer_id=? ORDER BY s.name''',(customer_id,))
        if identity['role']=='customer_viewer':
            permissions=rows('SELECT site_id FROM customer_site_permissions WHERE user_id=?',(identity.get('user_id'),)); allowed={item['site_id'] for item in permissions}
            if permissions: appliances=[item for item in appliances if item['site_id'] in allowed]
        return appliances

    @app.get('/api/v1/customers')
    def admin_customers(request: Request):
        identity=api_identity(request)
        if identity['role']!='administrator': raise HTTPException(status_code=403,detail='Administrator access is required.')
        return rows('SELECT id,name,company,status FROM customers ORDER BY name')

    @app.get('/api/v1/sites')
    def customer_sites(request: Request):
        identity,customer_id=customer_scope(request); sites=rows('SELECT * FROM sites WHERE customer_id=? ORDER BY name',(customer_id,))
        if identity['role']=='customer_viewer':
            permitted={x['site_id'] for x in rows('SELECT site_id FROM customer_site_permissions WHERE user_id=?',(identity.get('user_id'),))}
            if permitted: sites=[site for site in sites if site['id'] in permitted]
        return sites

    @app.get('/api/v1/cameras')
    def customer_cameras(request: Request,site_id: str=''):
        identity,customer_id=customer_scope(request); query='''SELECT c.*,s.name AS site_name,a.cloud_id,a.state AS appliance_state,acs.online,acs.recording,acs.analytics,acs.last_recording_at,acs.last_error
            FROM cameras c JOIN sites s ON s.id=c.site_id LEFT JOIN appliances a ON a.id=c.appliance_id
            LEFT JOIN appliance_camera_status acs ON acs.appliance_id=c.appliance_id AND acs.camera_id=c.id WHERE c.customer_id=?'''; params=[customer_id]
        if site_id: query+=' AND c.site_id=?'; params.append(site_id)
        cameras=rows(query+' ORDER BY s.name,c.name',tuple(params))
        if identity['role']=='customer_viewer':
            permissions=rows('SELECT * FROM customer_camera_permissions WHERE user_id=?',(identity.get('user_id'),)); allowed_ids={p['camera_id'] for p in permissions}
            if permissions: cameras=[camera for camera in cameras if camera['id'] in allowed_ids]
        for camera in cameras:
            camera['online']=bool(camera.get('online')); camera['recording']=bool(camera.get('recording')); camera['analytics']=bool(camera.get('analytics')); camera.pop('cloud_id',None)
        return cameras

    @app.post('/api/v1/cameras/{camera_id}/live-session')
    def live_session(camera_id: str,request: Request):
        identity,customer_id=customer_scope(request); scoped={**identity,'customer_id':customer_id}; camera=_camera_access(scoped,camera_id,'live')
        session_id=secrets.token_hex(16); now=datetime.now(); expires=now+timedelta(minutes=5)
        with connection() as db: db.execute('INSERT INTO live_view_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,transport,requested_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(session_id,customer_id,camera['site_id'],camera_id,identity.get('user_id'),identity['email'],identity['role'],'requested','not_configured',now.isoformat(),expires.isoformat()))
        audit(identity,'live_session.requested','live_view_session',session_id,{'camera_id':camera_id,'site_id':camera['site_id']})
        return {'id':session_id,'camera_id':camera_id,'camera_name':camera.get('name'),'site_id':camera['site_id'],'state':'requested','transport':'not_configured','message':'Secure relay/WebRTC transport is not active yet. The browser will never connect to a private camera IP address.','stream_url':None,'expires_at':expires.isoformat()}

    @app.get('/api/v1/live-sessions/{session_id}')
    def live_session_status(session_id: str,request: Request):
        identity,customer_id=customer_scope(request); session=row('SELECT * FROM live_view_sessions WHERE id=? AND customer_id=?',(session_id,customer_id))
        if not session: raise HTTPException(status_code=404,detail='Live session not found.')
        state=live_session_state(session['state'],session['expires_at'])
        if state=='expired' and session['state']!='expired':
            with connection() as db: db.execute("UPDATE live_view_sessions SET state='expired' WHERE id=?",(session_id,))
        return {'id':session_id,'camera_id':session['camera_id'],'site_id':session['site_id'],'state':state,'transport':session['transport'],'expires_at':session['expires_at'],'stream_url':None,'error':session.get('error')}

    @app.post('/api/v1/cameras/{camera_id}/snapshots')
    def customer_snapshot(camera_id: str,request: Request):
        identity,customer_id=customer_scope(request); scoped={**identity,'customer_id':customer_id}; _camera_access(scoped,camera_id,'live'); audit(identity,'snapshot.requested','camera',camera_id,{'customer_id':customer_id})
        return {'state':'transport_pending','message':'Snapshot authorization is valid, but secure remote frame transport is not active yet.','snapshot_url':None}

    @app.post('/api/v1/cameras/{camera_id}/clips')
    def customer_clip(camera_id: str,request: Request,payload: dict):
        identity,customer_id=customer_scope(request); scoped={**identity,'customer_id':customer_id}; _camera_access(scoped,camera_id,'playback')
        try: start=datetime.fromisoformat(str(payload.get('start_time',''))); end=datetime.fromisoformat(str(payload.get('end_time','')))
        except ValueError as error: raise HTTPException(status_code=400,detail='Valid clip start and end times are required.') from error
        if end<=start or end-start>timedelta(hours=2): raise HTTPException(status_code=400,detail='Clip end must follow start and the range cannot exceed two hours.')
        job_id=secrets.token_hex(16); now=datetime.now()
        with connection() as db: db.execute('INSERT INTO customer_clip_jobs(id,customer_id,camera_id,requested_by,start_time,end_time,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',(job_id,customer_id,camera_id,identity['email'],start.isoformat(),end.isoformat(),'requested',now.isoformat(),(now+timedelta(hours=24)).isoformat()))
        audit(identity,'clip.requested','customer_clip_job',job_id,{'camera_id':camera_id,'start_time':start.isoformat(),'end_time':end.isoformat()}); return {'id':job_id,'state':'requested','message':'Clip authorization is saved. Secure appliance clip transport is not active yet.'}

    @app.get('/api/v1/clips/{job_id}')
    def customer_clip_status(job_id: str,request: Request):
        identity,customer_id=customer_scope(request); job=row('SELECT * FROM customer_clip_jobs WHERE id=? AND customer_id=?',(job_id,customer_id))
        if not job: raise HTTPException(status_code=404,detail='Clip job not found.')
        if job.get('expires_at') and job['expires_at']<=datetime.now().isoformat() and job['state'] not in {'complete','failed'}: job['state']='expired'
        return job

    @app.get('/api/v1/events')
    def customer_events(request: Request,site_id: str='',camera_id: str='',event_type: str='',limit: int=100,access_action: str='alerts'):
        identity,customer_id=customer_scope(request); query='''SELECT e.event_id AS id,e.event_type,e.camera_id,e.event_timestamp,e.received_at,e.payload_json,c.name AS camera_name,c.site_id,s.name AS site_name
            FROM appliance_events e JOIN appliances a ON a.id=e.appliance_id LEFT JOIN cameras c ON c.id=e.camera_id LEFT JOIN sites s ON s.id=c.site_id WHERE a.customer_id=?'''; params=[customer_id]
        if site_id: query+=' AND c.site_id=?'; params.append(site_id)
        if camera_id: query+=' AND e.camera_id=?'; params.append(camera_id)
        if event_type:
            if event_type not in EVENT_TYPES: raise HTTPException(status_code=400,detail='Unsupported event type filter.')
            query+=' AND e.event_type=?'; params.append(event_type)
        records=rows(query+' ORDER BY e.event_timestamp DESC LIMIT ?',tuple(params+[max(1,min(500,limit))]))
        if access_action not in {'alerts','playback'}: raise HTTPException(status_code=400,detail='Unsupported event access mode.')
        if identity['role']=='customer_viewer':
            permissions=rows('SELECT * FROM customer_camera_permissions WHERE user_id=?',(identity.get('user_id'),)); allowed_cameras={item['camera_id'] for item in permissions if camera_action_allowed(identity['role'],access_action,item,len(permissions))}
            site_permissions=rows('SELECT site_id FROM customer_site_permissions WHERE user_id=?',(identity.get('user_id'),)); allowed_sites={item['site_id'] for item in site_permissions}
            if permissions: records=[item for item in records if item.get('camera_id') in allowed_cameras]
            if site_permissions: records=[item for item in records if item.get('site_id') in allowed_sites]
        for item in records:
            payload=_payload(item); item.update({'confidence':payload.get('confidence'),'thumbnail':payload.get('thumbnail'),'recording_id':payload.get('recording_id') or payload.get('linked_recording'),'plate_number':payload.get('plate_number'),'direction':payload.get('direction')}); item.pop('payload_json',None)
        return records

    @app.get('/api/v1/alerts')
    def customer_alerts(request: Request,limit: int=100):
        identity,customer_id=customer_scope(request); events=customer_events(request,limit=limit,access_action='alerts')
        health=rows('''SELECT h.id,a.id AS appliance_id,a.cloud_id,s.id AS site_id,s.name AS site_name,h.status,h.cpu,h.memory,h.disk_capacity,h.disk_used,h.last_error,h.created_at
            FROM appliance_health_history h JOIN appliances a ON a.id=h.appliance_id JOIN sites s ON s.id=a.site_id WHERE a.customer_id=? AND h.status!='online' ORDER BY h.created_at DESC LIMIT ?''',(customer_id,max(1,min(100,limit))))
        if identity['role']=='customer_viewer':
            permissions=rows('SELECT site_id FROM customer_site_permissions WHERE user_id=?',(identity.get('user_id'),)); allowed={item['site_id'] for item in permissions}
            if permissions: health=[item for item in health if item['site_id'] in allowed]
        alerts=[{**event,'alert_type':event['event_type'],'timestamp':event['event_timestamp'],'message':event['event_type'].replace('_',' ').title()} for event in events]
        alerts += [{**item,'alert_type':'health','timestamp':item['created_at'],'message':item.get('last_error') or item['status'].replace('_',' ').title()} for item in health]
        return sorted(alerts,key=lambda item:item.get('timestamp') or '',reverse=True)[:limit]

    @app.get('/api/v1/recordings')
    def customer_recordings(request: Request,camera_id: str='',site_id: str='',date: str=''):
        identity,customer_id=customer_scope(request); events=customer_events(request,site_id=site_id,camera_id=camera_id,limit=300,access_action='playback'); found=[]; seen=set()
        for event in events:
            recording=event.get('recording_id')
            if recording and recording not in seen and (not date or str(event.get('event_timestamp','')).startswith(date)):
                seen.add(recording); found.append({'id':recording,'camera_id':event.get('camera_id'),'camera_name':event.get('camera_name'),'site_id':event.get('site_id'),'started_at':event.get('event_timestamp'),'source':'secure_appliance','playback_status':'transport_pending'})
        return found

    @app.post('/api/v1/recordings/{recording_id}/download')
    def request_download(recording_id: str,request: Request):
        identity,customer_id=customer_scope(request)
        resource=_recording_resource(customer_id,recording_id)
        if not resource: raise HTTPException(status_code=404,detail='Recording not found in this customer account.')
        scoped={**identity,'customer_id':customer_id}; _camera_access(scoped,str(resource.get('camera_id') or ''),'download')
        audit(identity,'recording.download_requested','recording',recording_id,{'customer_id':customer_id})
        return {'status':'transport_pending','message':'Download authorization is recorded. Secure remote clip transport is not implemented yet.','download_url':None}

    @app.post('/api/v1/recordings/{recording_id}/share')
    def share_recording(recording_id: str,request: Request,payload: dict):
        identity,customer_id=customer_scope(request)
        if identity['role']=='customer_viewer': raise HTTPException(status_code=403,detail='Customer owner permission is required to share clips.')
        resource=_recording_resource(customer_id,recording_id)
        if not resource: raise HTTPException(status_code=404,detail='Recording not found in this customer account.')
        scoped={**identity,'customer_id':customer_id}; _camera_access(scoped,str(resource.get('camera_id') or ''),'share')
        share_id=secrets.token_urlsafe(18); now=datetime.now(); hours=max(1,min(168,int(payload.get('expires_hours',24))))
        with connection() as db: db.execute('INSERT INTO customer_clip_shares(id,customer_id,recording_id,created_by,expires_at,created_at) VALUES(?,?,?,?,?,?)',(share_id,customer_id,recording_id,identity['email'],(now+timedelta(hours=hours)).isoformat(),now.isoformat()))
        audit(identity,'recording.shared','recording',recording_id,{'share_id':share_id,'expires_hours':hours}); return {'status':'transport_pending','share_id':share_id,'message':'Share authorization is saved. A public clip URL will become available when secure remote clip transport is enabled.'}

    @app.get('/api/v1/subscription')
    def subscription(request: Request):
        identity,customer_id=customer_scope(request); plan=row('''SELECT id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,annual_total,status,created_at
            FROM plans WHERE customer_id=? ORDER BY created_at DESC LIMIT 1''',(customer_id,)) or {}; analytics=rows('SELECT analytic_key,status,monthly_retail FROM analytics_subscriptions WHERE customer_id=?',(customer_id,)); return {'plan':plan,'analytics':analytics,'pricing_visibility':'customer_retail_only'}

    @app.get('/api/v1/users')
    def customer_users(request: Request):
        identity,customer_id=customer_scope(request); return rows("SELECT id,email,name,role,approved,account_status,created_at FROM partner_users WHERE customer_id=? AND role IN ('customer_owner','customer_viewer') ORDER BY name",(customer_id,))

    @app.post('/api/v1/users')
    def invite_customer_user(request: Request,payload: dict):
        identity,customer_id=customer_scope(request)
        if identity['role'] not in {'customer_owner','administrator'}: raise HTTPException(status_code=403,detail='Customer owner permission is required.')
        role=str(payload.get('role','customer_viewer'))
        if role not in CUSTOMER_ROLES: raise HTTPException(status_code=400,detail='Only customer owner or viewer roles can be invited here.')
        email=str(payload.get('email','')).strip().lower(); temporary=secrets.token_urlsafe(12); user_id=secrets.token_hex(8); now=datetime.now().isoformat()
        try:
            with connection() as db: db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,must_change_password) VALUES(?,?,?,?,?,?,1,?,?,?,1)',(user_id,identity.get('partner_id'),email,str(payload.get('name') or email),role,password_hash(temporary),customer_id,now,'active'))
        except Exception as error: raise HTTPException(status_code=409,detail='A user with this email may already exist.') from error
        audit(identity,'user.invited','partner_user',user_id,{'customer_id':customer_id,'role':role}); return {'message':'Customer invitation preview created.','email':email,'temporary_password':temporary}

    @app.put('/api/v1/users/{user_id}/permissions')
    def customer_user_permissions(user_id: str,request: Request,payload: dict):
        identity,customer_id=customer_scope(request)
        if identity['role'] not in {'customer_owner','administrator'}: raise HTTPException(status_code=403,detail='Customer owner permission is required.')
        target=row("SELECT id FROM partner_users WHERE id=? AND customer_id=? AND role IN ('customer_owner','customer_viewer')",(user_id,customer_id))
        if not target: raise HTTPException(status_code=404,detail='Customer user not found.')
        site_ids={str(value) for value in payload.get('site_ids',[])}; cameras=payload.get('cameras',[])
        valid_sites={item['id'] for item in rows('SELECT id FROM sites WHERE customer_id=?',(customer_id,))}; valid_cameras={item['id'] for item in rows('SELECT id FROM cameras WHERE customer_id=?',(customer_id,))}
        if not site_ids<=valid_sites or any(str(item.get('id')) not in valid_cameras for item in cameras): raise HTTPException(status_code=400,detail='A selected site or camera is outside this customer account.')
        with connection() as db:
            db.execute('DELETE FROM customer_site_permissions WHERE user_id=?',(user_id,)); db.execute('DELETE FROM customer_camera_permissions WHERE user_id=?',(user_id,))
            for site_id in site_ids: db.execute('INSERT INTO customer_site_permissions(user_id,site_id) VALUES(?,?)',(user_id,site_id))
            for item in cameras: db.execute('INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_download,can_share,can_alerts,can_settings) VALUES(?,?,?,?,?,?,?,?)',(user_id,str(item['id']),int(bool(item.get('live',True))),int(bool(item.get('playback',True))),int(bool(item.get('download',False))),int(bool(item.get('share',False))),int(bool(item.get('alerts',True))),int(bool(item.get('settings',False)))))
        audit(identity,'permission.changed','partner_user',user_id,{'customer_id':customer_id,'site_count':len(site_ids),'camera_count':len(cameras)}); return {'message':'Customer user permissions saved.'}

    @app.get('/customer-portal',response_class=HTMLResponse)
    def customer_portal(request: Request):
        identity=partner_identity(request)
        if not identity: return FileResponse(PAGE,media_type='text/html')
        if identity['role'] not in CUSTOMER_ROLES|{'administrator'}: raise HTTPException(status_code=403,detail='Customer Portal access is not available for this role.')
        return HTMLResponse(_customer_portal_html(identity))


def _customer_portal_html(identity):
    role=escape(identity['role']); admin_picker='<select id="admin-customer"></select>' if identity['role']=='administrator' else ''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Customer Portal · AnyAiCam</title><style>{CUSTOMER_CSS}</style></head><body><div class="app"><aside class="side"><div class="logo"><img src="/static/brand-icon.png" alt="AnyAiCam"><b>AnyAiCam</b></div><nav>{_nav()}</nav><button id="logout-all" class="quiet">Log out all devices</button></aside><main><header><div><small>Customer Portal · {role}</small><h1 id="page-title">Dashboard</h1></div><div class="header-actions">{admin_picker}<select id="site-switcher"><option value="">All sites</option></select><span id="account-name"></span></div></header><div id="notice" class="notice">Secure remote video transport is not enabled yet. Camera tiles never connect to private local camera addresses.</div><section id="view" class="view"></section></main><nav class="bottom">{_nav()}</nav></div><dialog id="camera-dialog"><button class="close" onclick="this.closest('dialog').close()">×</button><h2 id="dialog-camera-name">Camera</h2><div class="video-placeholder">Secure live video session placeholder</div><div class="tabs"><button data-camera-tab="live" class="active">Live</button><button data-camera-tab="playback">Playback</button></div><div class="camera-actions"><button>🎙 Microphone</button><button id="camera-mute">🔇 Mute</button><button id="camera-snapshot">📷 Snapshot</button><button onclick="document.querySelector('.video-placeholder').requestFullscreen()">⛶ Full screen</button></div><div id="camera-timeline" class="timeline"></div></dialog><div id="toast"></div><script>{CUSTOMER_JS}</script></body></html>'''


def _nav():
    return ''.join(f'<button data-page="{key}"><span>{icon}</span>{label}</button>' for key,icon,label in [('cameras','▣','Cameras'),('alerts','♢','Alerts'),('dashboard','⌁','Dashboard'),('sites','⌂','Sites'),('account','⚙','Account'),('playback','◴','Playback'),('health','♡','Health'),('users','♙','Users')])


CUSTOMER_CSS='''
:root{--navy:#11162d;--panel:#1d273a;--surface:#edf2f8;--cyan:#5ed2d1;--blue:#505be2;--pink:#c02d92;--good:#4bd58b;--bad:#ff6477}*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--surface);color:#172038}.app{min-height:100vh;display:grid;grid-template-columns:220px 1fr}.side{background:var(--navy);color:#fff;padding:18px 12px;display:flex;flex-direction:column}.logo{display:flex;align-items:center;gap:10px;padding:8px 10px 22px}.logo img{width:44px;height:44px;object-fit:contain}.side nav,.bottom{display:grid;gap:5px}.side nav button,.bottom button{border:0;background:transparent;color:inherit;text-align:left;padding:12px;border-radius:10px;font:inherit;font-weight:700;cursor:pointer}.side nav button.active{background:var(--cyan);color:#10213b}.side nav span,.bottom span{display:inline-block;width:28px}.quiet{margin-top:auto;background:transparent;border:1px solid #5d6880;color:#fff;padding:10px;border-radius:9px}main{min-width:0;padding:20px}header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}h1{margin:.2rem 0}.header-actions{display:flex;align-items:center;gap:10px}.header-actions select{padding:10px;border-radius:9px;border:1px solid #bbc7d7}.notice{background:#fff4d8;border:1px solid #efce74;padding:11px 14px;border-radius:10px;margin-bottom:16px}.toolbar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:14px}.toolbar button,.toolbar select,.toolbar input,.camera-actions button,.tabs button{border:1px solid #b9c4d2;background:#fff;border-radius:9px;padding:9px;cursor:pointer}.toolbar button.active,.tabs button.active{background:var(--blue);color:#fff}.camera-grid{display:grid;grid-template-columns:repeat(var(--columns,2),minmax(0,1fr));gap:10px}.camera{background:#101827;border-radius:13px;overflow:hidden;color:#fff;min-width:0}.camera-screen{aspect-ratio:16/9;display:grid;place-items:center;background:linear-gradient(145deg,#18263a,#0c111c);position:relative;text-align:center;padding:12px}.camera-screen .status{position:absolute;top:9px;right:9px}.camera-meta{padding:10px;display:flex;justify-content:space-between;gap:8px}.status{font-size:.72rem;padding:4px 7px;border-radius:999px;background:#4c5669}.status.online{background:var(--good);color:#082b1a}.status.offline{background:var(--bad)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.card{background:#fff;border:1px solid #d6dfea;border-radius:13px;padding:16px}.event{display:grid;grid-template-columns:110px 1fr auto;gap:12px;align-items:center}.thumb{aspect-ratio:16/9;background:#dce4ee;border-radius:8px;display:grid;place-items:center;overflow:hidden}.thumb img{width:100%;height:100%;object-fit:cover}.bottom{display:none}dialog{width:min(900px,calc(100% - 24px));border:0;border-radius:16px;padding:18px;background:#f4f7fb}.close{float:right;border:0;font-size:1.6rem;background:transparent}.video-placeholder{aspect-ratio:16/9;background:#101827;color:#fff;display:grid;place-items:center;border-radius:12px}.camera-actions,.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.timeline{height:55px;margin-top:12px;border-top:2px solid #8290a3;background:repeating-linear-gradient(90deg,transparent 0 29px,#cbd4df 30px 31px)}#toast{position:fixed;right:18px;bottom:18px;background:#11162d;color:#fff;padding:12px 16px;border-radius:9px;opacity:0;pointer-events:none}#toast.show{opacity:1}@media(max-width:900px){.app{grid-template-columns:84px 1fr}.logo b,.side nav button{font-size:0}.side nav span{font-size:1.2rem}.camera-grid{--columns:2!important}}@media(max-width:640px){.app{display:block;padding-bottom:76px}.side{display:none}main{padding:12px}header{align-items:flex-start}.header-actions{flex-direction:column;align-items:flex-end}.camera-grid{--columns:1!important}.bottom{position:fixed;z-index:5;bottom:0;left:0;right:0;background:var(--navy);color:#fff;display:grid;grid-template-columns:repeat(5,1fr);padding-bottom:env(safe-area-inset-bottom)}.bottom button{font-size:.68rem;text-align:center;padding:9px 2px}.bottom button:nth-child(n+6){display:none}.bottom span{display:block;width:auto;font-size:1.1rem}.event{grid-template-columns:80px 1fr}.event>button{grid-column:2}.notice{font-size:.85rem}}
'''

CUSTOMER_JS=r'''
document.head.insertAdjacentHTML('beforeend','<meta name="theme-color" content="#11162d"><meta name="apple-mobile-web-app-capable" content="yes"><link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/static/brand-icon.png">');if('serviceWorker'in navigator)navigator.serviceWorker.register('/service-worker.js').catch(()=>{});
const state={page:'dashboard',site:'',layout:4,customerId:new URLSearchParams(location.search).get('customer_id')||'',cameraId:'',activeCamera:''};
let installPrompt=null;window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event});async function installPwa(){if(!installPrompt)return toast('Use your browser menu and choose Add to Home Screen.');await installPrompt.prompt();installPrompt=null}
const api=async(path,options={})=>{options.headers={...(options.headers||{}),...(state.customerId?{'X-Customer-ID':state.customerId}:{})};const response=await fetch('/api/v1'+path,options);if(response.status===401){location.href='/customer-login.html';throw Error('Sign in required')}const body=await response.json();if(!response.ok)throw Error(body.detail||'Request failed');return body};
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const toast=text=>{const box=document.getElementById('toast');box.textContent=text;box.classList.add('show');setTimeout(()=>box.classList.remove('show'),2800)};
async function boot(){try{const [me,customer,sites]=await Promise.all([api('/me'),api('/customer'),api('/sites')]);document.getElementById('account-name').textContent=customer.company||customer.name;document.getElementById('site-switcher').innerHTML='<option value="">All sites</option>'+sites.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('');if(me.role==='administrator'){const customers=await api('/customers');document.getElementById('admin-customer').innerHTML=customers.map(c=>`<option value="${c.id}" ${c.id===state.customerId?'selected':''}>${esc(c.name)}</option>`).join('');document.getElementById('admin-customer').onchange=e=>{state.customerId=e.target.value;location.search='customer_id='+encodeURIComponent(state.customerId)}}render()}catch(error){toast(error.message)}}
document.querySelectorAll('[data-page]').forEach(button=>button.onclick=()=>{state.page=button.dataset.page;render()});document.getElementById('site-switcher').onchange=e=>{state.site=e.target.value;render()};document.getElementById('logout-all').onclick=async()=>{await api('/auth/logout-all',{method:'POST'});location.href='/customer-login.html'};
async function render(){document.querySelectorAll('[data-page]').forEach(x=>x.classList.toggle('active',x.dataset.page===state.page));document.getElementById('page-title').textContent=state.page.replace('_',' ').replace(/^./,x=>x.toUpperCase());const view=document.getElementById('view');view.innerHTML='<div class="card">Loading…</div>';try{if(state.page==='dashboard')return dashboard(view);if(state.page==='cameras')return cameras(view);if(state.page==='playback')return playback(view);if(state.page==='alerts')return alerts(view);if(state.page==='sites')return sites(view);if(state.page==='health')return health(view);if(state.page==='users')return users(view);account(view)}catch(error){view.innerHTML=`<div class="card">${esc(error.message)}</div>`}}
async function dashboard(view){const data=await api('/dashboard');view.innerHTML=`<div class="cards"><article class="card"><h2>${data.cameras}</h2><p>Cameras · ${data.online_cameras} online · ${data.recording_cameras} recording</p></article><article class="card"><h2>${data.sites}</h2><p>Sites · ${data.appliances} appliances</p></article><article class="card"><h2>${esc((data.plan.resolution||'—').toUpperCase())}</h2><p>${esc(data.plan.recording_mode||'No plan')} · ${esc(data.plan.retention_days||'—')} days</p></article></div><h2>Recent alerts</h2>${eventCards(data.recent_alerts)}<h2>Recent recordings</h2><div class="cards">${data.recent_recordings.map(r=>`<article class="card"><b>${esc(r.camera_name||'Camera')}</b><p>${esc(r.started_at||'')}</p><button onclick="state.page='playback';render()">Open playback</button></article>`).join('')||'<div class="card">No remote recordings indexed yet.</div>'}</div>`}
async function cameras(view){const items=await api('/cameras'+(state.site?'?site_id='+encodeURIComponent(state.site):''));view.innerHTML=`<div class="toolbar"><strong>Layout</strong>${[1,4,9,16].map(n=>`<button data-layout="${n}" class="${state.layout===n?'active':''}">${n}</button>`).join('')}</div><div class="camera-grid" style="--columns:${Math.sqrt(state.layout)}">${items.slice(0,state.layout).map(c=>`<article class="camera" data-id="${c.id}"><div class="camera-screen"><span class="status ${c.online?'online':'offline'}">${c.online?'Online':'Offline'}</span><div>Secure live video<br><small>${c.recording?'Recording':'Not recording'} · ${c.analytics?'Analytics on':'Analytics off'}</small></div></div><div class="camera-meta"><div><b>${esc(c.name)}</b><small style="display:block">${esc(c.site_name)}</small></div><button class="open-camera">Open</button></div></article>`).join('')||'<div class="card">No cameras are assigned to this site.</div>'}</div>`;view.querySelectorAll('[data-layout]').forEach(b=>b.onclick=()=>{state.layout=Number(b.dataset.layout);cameras(view)});view.querySelectorAll('.open-camera').forEach(b=>b.onclick=()=>openCamera(b.closest('.camera').dataset.id,items))}
async function openCamera(id,items){const camera=items.find(x=>x.id===id),session=await api('/cameras/'+id+'/live-session',{method:'POST'});state.activeCamera=id;document.getElementById('dialog-camera-name').textContent=camera.name+' · '+camera.site_name;document.querySelector('.video-placeholder').textContent=session.message;document.getElementById('camera-dialog').showModal()}document.getElementById('camera-snapshot').onclick=async()=>{if(!state.activeCamera)return;const r=await api('/cameras/'+state.activeCamera+'/snapshots',{method:'POST'});toast(r.message)};document.getElementById('camera-mute').onclick=e=>{e.currentTarget.textContent=e.currentTarget.textContent.includes('Mute')?'🔊 Unmute':'🔇 Mute'};
async function playback(view){const cameras=await api('/cameras'+(state.site?'?site_id='+state.site:''));if(!state.cameraId&&cameras.length)state.cameraId=cameras[0].id;const recordings=await api('/recordings?'+new URLSearchParams({site_id:state.site,camera_id:state.cameraId,date:document.getElementById('playback-date')?.value||''}));view.innerHTML=`<div class="toolbar"><label>Camera <select id="playback-camera">${cameras.map(c=>`<option value="${c.id}" ${c.id===state.cameraId?'selected':''}>${esc(c.name)} · ${esc(c.site_name)}</option>`).join('')}</select></label><label>Date <input id="playback-date" type="date"></label><button id="refresh-playback">Refresh</button></div><div class="timeline"></div><form id="clip-form" class="card"><h3>Create clip</h3><label>Start <input id="clip-start" type="datetime-local" required></label> <label>End <input id="clip-end" type="datetime-local" required></label> <button>Create authorized clip</button></form><div class="cards">${recordings.map(r=>`<article class="card"><h3>${esc(r.camera_name||'Camera')}</h3><p>${esc(r.started_at||'')}</p><span class="status">Secure playback pending</span><p><button onclick="downloadRecording('${r.id}')">Download</button> <button onclick="shareRecording('${r.id}')">Share</button></p></article>`).join('')||'<div class="card">No authorized remote recordings are indexed yet. Local appliance recording continues normally.</div>'}</div>`;document.getElementById('playback-camera').onchange=e=>{state.cameraId=e.target.value;playback(view)};document.getElementById('refresh-playback').onclick=()=>playback(view);document.getElementById('clip-form').onsubmit=async e=>{e.preventDefault();const r=await api('/cameras/'+state.cameraId+'/clips',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_time:document.getElementById('clip-start').value,end_time:document.getElementById('clip-end').value})});toast(r.message)}}
async function downloadRecording(id){const result=await api('/recordings/'+encodeURIComponent(id)+'/download',{method:'POST'});toast(result.message)}async function shareRecording(id){const result=await api('/recordings/'+encodeURIComponent(id)+'/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expires_hours:24})});toast(result.message)}
async function alerts(view){const [items,center,prefs]=await Promise.all([api('/alerts?limit=100'),api('/notifications'),api('/notification-preferences')]);view.innerHTML=`<div class="cards"><article class="card"><h2>Notification center <span class="status">${center.unread} unread</span></h2><div id="notification-list">${center.notifications.map(n=>`<div class="event card"><div class="thumb">${n.thumbnail?`<img src="${esc(n.thumbnail)}">`:'Alert'}</div><div><b>${esc(n.title)}</b><p>${esc(n.message||'')} · ${esc(n.timestamp)}</p></div><div><button onclick="notificationAction('${n.id}','read')">Read</button><button onclick="notificationAction('${n.id}','acknowledge')">Acknowledge</button><button onclick="notificationAction('${n.id}','bookmark')">Bookmark</button><button onclick="notificationAction('${n.id}','dismiss')">Dismiss</button></div></div>`).join('')||'No stored notifications yet.'}</div></article><form id="notification-preference" class="card"><h2>Notification preferences</h2><label>Event <select id="pref-event">${['motion','person','vehicle','line_crossing','intrusion','lpr','people_counting','occupancy','camera_offline','recording_stopped','appliance_offline','low_disk','high_cpu','software_update'].map(x=>`<option value="${x}">${x.replaceAll('_',' ')}</option>`).join('')}</select></label> <label>Severity <select id="pref-severity"><option>all</option><option>info</option><option>warning</option><option>critical</option></select></label><label>From <input id="pref-start" type="time" value="00:00"></label> <label>To <input id="pref-end" type="time" value="23:59"></label><p><label><input id="pref-inapp" type="checkbox" checked> In-app</label> <label><input id="pref-email" type="checkbox"> Email preview</label> <label><input id="pref-push" type="checkbox"> Web push preparation</label> <label><input id="pref-sms" type="checkbox"> SMS disabled</label></p><button>Save preference</button><small>${prefs.length} saved preference(s)</small></form></div><div class="toolbar">${['all','motion','person','vehicle','line_crossing','intrusion','lpr','people_counting','occupancy','health'].map(x=>`<button class="alert-filter" data-type="${x}">${x.replaceAll('_',' ')}</button>`).join('')}</div><div id="alerts-list">${eventCards(items)}</div>`;view.querySelectorAll('.alert-filter').forEach(b=>b.onclick=()=>{document.getElementById('alerts-list').innerHTML=eventCards(b.dataset.type==='all'?items:items.filter(x=>x.alert_type===b.dataset.type))});document.getElementById('notification-preference').onsubmit=async e=>{e.preventDefault();const r=await api('/notification-preferences',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_type:document.getElementById('pref-event').value,severity:document.getElementById('pref-severity').value,schedule_start:document.getElementById('pref-start').value,schedule_end:document.getElementById('pref-end').value,in_app:document.getElementById('pref-inapp').checked,email:document.getElementById('pref-email').checked,web_push:document.getElementById('pref-push').checked,sms:document.getElementById('pref-sms').checked,site_id:state.site||null})});toast(r.message)}}
async function notificationAction(id,action){const r=await api('/notifications/'+id+'/'+action,{method:'POST'});toast(r.message);if(state.page==='alerts')render()}
function eventCards(items){return `<div class="cards">${items.map(e=>`<article class="card event"><div class="thumb">${e.thumbnail?`<img src="${esc(e.thumbnail)}" alt="Event">`:esc(e.alert_type||'Event')}</div><div><b>${esc(e.message)}</b><p>${esc(e.camera_name||e.site_name||'System')} · ${esc(e.timestamp||'')}</p><small>${e.confidence==null?'':Math.round(e.confidence*100)+'% confidence'}</small></div><button onclick="state.page='playback';render()">Review</button></article>`).join('')||'<div class="card">No alerts match this filter.</div>'}</div>`}
async function sites(view){const items=await api('/sites');view.innerHTML=`<div class="cards">${items.map(s=>`<article class="card"><h2>${esc(s.name)}</h2><p>${esc(s.address||'No address entered')}</p><button onclick="state.site='${s.id}';state.page='cameras';render()">View cameras</button></article>`).join('')}</div>`}
async function health(view){const [cameras,alerts]=await Promise.all([api('/cameras'+(state.site?'?site_id='+state.site:'')),api('/alerts?limit=50')]);const healthAlerts=alerts.filter(a=>a.alert_type==='health');view.innerHTML=`<div class="cards"><article class="card"><h2>Camera health</h2><p>${cameras.filter(c=>c.online).length} online · ${cameras.filter(c=>!c.online).length} offline · ${cameras.filter(c=>c.recording).length} recording</p></article><article class="card"><h2>Health alerts</h2><p>${healthAlerts.length} recent issue(s)</p></article></div>${eventCards(healthAlerts)}`}
async function users(view){const items=await api('/users');view.innerHTML=`<div class="cards">${items.map(u=>`<article class="card"><h3>${esc(u.name||u.email)}</h3><p>${esc(u.email)} · ${esc(u.role.replace('_',' '))}</p>${u.role==='customer_viewer'?`<button onclick="managePermissions('${u.id}')">Assign sites, cameras, and permissions</button>`:''}</article>`).join('')}</div><div id="permission-editor"></div><form id="invite" class="card" style="margin-top:14px"><h2>Invite approved user</h2><input id="invite-name" placeholder="Name" required> <input id="invite-email" type="email" placeholder="Email" required> <select id="invite-role"><option value="customer_viewer">Viewer</option><option value="customer_owner">Owner</option></select> <button>Invite</button></form>`;document.getElementById('invite').onsubmit=async e=>{e.preventDefault();const r=await api('/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('invite-name').value,email:document.getElementById('invite-email').value,role:document.getElementById('invite-role').value})});toast(r.message)}}
async function managePermissions(userId){const [sites,cameras]=await Promise.all([api('/sites'),api('/cameras')]),box=document.getElementById('permission-editor');box.innerHTML=`<form id="permissions" class="card"><h2>Site and camera permissions</h2><fieldset><legend>Sites</legend>${sites.map(s=>`<label><input class="permission-site" type="checkbox" value="${s.id}" checked> ${esc(s.name)}</label>`).join('')}</fieldset>${cameras.map(c=>`<fieldset class="permission-camera" data-id="${c.id}"><legend>${esc(c.name)} · ${esc(c.site_name)}</legend>${['live','playback','download','share','alerts','settings'].map(p=>`<label><input data-permission="${p}" type="checkbox" ${['live','playback','alerts'].includes(p)?'checked':''}> ${p}</label>`).join('')}</fieldset>`).join('')}<button>Save permissions</button></form>`;document.getElementById('permissions').onsubmit=async e=>{e.preventDefault();const payload={site_ids:[...document.querySelectorAll('.permission-site:checked')].map(x=>x.value),cameras:[...document.querySelectorAll('.permission-camera')].map(f=>({id:f.dataset.id,...Object.fromEntries([...f.querySelectorAll('[data-permission]')].map(x=>[x.dataset.permission,x.checked]))}))};const r=await api('/users/'+userId+'/permissions',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});toast(r.message)}}
async function account(view){const [customer,subscription,sessions,devices]=await Promise.all([api('/customer'),api('/subscription'),api('/auth/sessions'),api('/devices')]);view.innerHTML=`<div class="cards"><article class="card"><h2>Account</h2><p>${esc(customer.company||customer.name)} · ${esc(customer.status)}</p><p>Trial: ${esc(customer.trial_status||'Not active')}</p><button onclick="installPwa()">Install AnyAiCam app</button></article><article class="card"><h2>Plan</h2><p>${esc((subscription.plan.resolution||'Not configured').toUpperCase())} · ${esc(subscription.plan.camera_quantity||0)} cameras · ${esc(subscription.plan.retention_days||'—')} days</p><p>${subscription.analytics.map(a=>esc(a.analytic_key.replaceAll('_',' '))).join(', ')||'No analytics'}</p></article><article class="card"><h2>Password and sessions</h2><p>${sessions.length} active session(s)</p><a href="/forgot-password">Reset password</a><br><button onclick="document.getElementById('logout-all').click()">Log out all devices</button></article><article class="card"><h2>Registered mobile devices</h2>${devices.map(d=>`<p><b>${esc(d.platform||d.device_type)}</b> · ${esc(d.app_version||'version unknown')}<br><small>Last active ${esc(d.last_active_at||'never')}</small> ${d.revoked_at?'Revoked':`<button onclick="revokeDevice('${d.id}')">Revoke</button>`}</p>`).join('')||'<p>No native-app devices registered.</p>'}</article><article class="card"><h2>Support</h2><a href="mailto:amata@anyaicam.com">Email AnyAiCam support</a><br><a href="tel:+13465544699">(346) 554-4699</a></article></div><div class="notice">Customer retail information only. Partner prices, wholesale costs, commissions, internal audit records, and other customers are excluded.</div>`}async function revokeDevice(id){const r=await api('/devices/'+id,{method:'DELETE'});toast(r.message);account(document.getElementById('view'))}boot();
'''
