import secrets
from datetime import datetime

from fastapi import FastAPI,HTTPException,Request

from customer_platform import api_identity,customer_scope
from customer_policy import notification_scope_allowed
from mobile_security import register_device,revoke_device,rotate_refresh_token
from notification_service import CHANNELS
from partner_db import audit,connection,row,rows

EVENT_TYPES={'motion','person','vehicle','line_crossing','intrusion','lpr','people_counting','occupancy','camera_offline','recording_stopped','appliance_offline','low_disk','high_cpu','software_update'}
SEVERITIES={'all','info','warning','critical'}
ACTIONS={'read':'read_at','acknowledge':'acknowledged_at','dismiss':'dismissed_at','bookmark':'bookmarked_at'}


def _target_user(identity,customer_id,payload):
    requested=str(payload.get('user_id') or identity.get('user_id'))
    if requested!=identity.get('user_id') and identity['role'] not in {'customer_owner','administrator'}: raise HTTPException(status_code=403,detail='You cannot change another user’s notification preferences.')
    user=row('SELECT id,email,customer_id,role FROM partner_users WHERE id=? AND customer_id=?',(requested,customer_id))
    if not user: raise HTTPException(status_code=404,detail='Notification user not found in this customer account.')
    return user


def _validate_scope(customer_id,site_id,camera_id):
    if site_id and not row('SELECT id FROM sites WHERE id=? AND customer_id=?',(site_id,customer_id)): raise HTTPException(status_code=400,detail='Selected site is outside this customer account.')
    if camera_id:
        camera=row('SELECT id,site_id FROM cameras WHERE id=? AND customer_id=?',(camera_id,customer_id))
        if not camera or (site_id and camera['site_id']!=site_id): raise HTTPException(status_code=400,detail='Selected camera is outside the selected customer or site.')


def _validate_user_access(user,site_id,camera_id):
    if user.get('role')!='customer_viewer': return
    site_permissions=rows('SELECT site_id FROM customer_site_permissions WHERE user_id=?',(user['id'],)); camera_permissions=rows('SELECT camera_id,can_alerts FROM customer_camera_permissions WHERE user_id=?',(user['id'],))
    allowed_sites={item['site_id'] for item in site_permissions}; allowed_cameras={item['camera_id'] for item in camera_permissions if item['can_alerts']}
    if not notification_scope_allowed(user['customer_id'],user['customer_id'],site_id,allowed_sites,camera_id,allowed_cameras): raise HTTPException(status_code=403,detail='This user is not assigned notification access for the selected site or camera.')


def register_mobile_notification_routes(app: FastAPI):
    @app.post('/api/v1/auth/refresh')
    def refresh_mobile_token(payload: dict):
        raw=str(payload.get('refresh_token','')); tokens,status=rotate_refresh_token(raw)
        if status=='reuse_detected':
            audit({'email':'mobile-session','role':'unknown'},'refresh_token.reuse_detected','mobile_refresh_token',''); raise HTTPException(status_code=401,detail='Refresh-token reuse was detected. This device and token family were revoked.')
        if not tokens: raise HTTPException(status_code=401,detail=f'Refresh token is {status}. Sign in again.')
        record=row('SELECT u.email,u.role,u.id FROM mobile_refresh_tokens r JOIN partner_users u ON u.id=r.user_id WHERE r.id=?',(tokens['refresh_id'],)); audit(record,'refresh_token.rotated','mobile_refresh_token',tokens['refresh_id']); return tokens

    @app.post('/api/v1/auth/logout')
    def logout_device_session(request: Request,payload: dict):
        identity=api_identity(request); now=datetime.now().isoformat()
        with connection() as db:
            if identity.get('session_id'): db.execute('UPDATE user_sessions SET revoked_at=? WHERE id=? AND user_id=?',(now,identity['session_id'],identity.get('user_id')))
            if payload.get('refresh_token'):
                digest=__import__('hashlib').sha256(str(payload['refresh_token']).encode()).hexdigest(); db.execute('UPDATE mobile_refresh_tokens SET revoked_at=? WHERE token_hash=? AND user_id=?',(now,digest,identity.get('user_id')))
        audit(identity,'session.revoked','user_session',identity.get('session_id',''),{'scope':'individual'}); return {'message':'This device session has been signed out.'}

    @app.get('/api/v1/devices')
    def devices(request: Request):
        identity=api_identity(request); return rows('SELECT id,device_uid,device_type,platform,last_active_at,app_version,revoked_at,created_at FROM mobile_devices WHERE user_id=? ORDER BY last_active_at DESC',(identity.get('user_id'),))

    @app.post('/api/v1/devices')
    def device_registration(request: Request,payload: dict):
        identity=api_identity(request); user=row('SELECT * FROM partner_users WHERE id=?',(identity.get('user_id'),)); uid=str(payload.get('device_id','')).strip()
        if not uid: raise HTTPException(status_code=400,detail='Device ID is required.')
        device_id=register_device(user,uid,str(payload.get('device_type') or 'mobile')[:50],str(payload.get('platform') or 'unknown')[:50],str(payload.get('app_version') or '')[:50],str(payload.get('push_token') or '')[:500]); audit(identity,'device.registered','mobile_device',device_id,{'platform':payload.get('platform')}); return {'id':device_id,'message':'Device registration saved. Push delivery remains inactive until a provider is configured.'}

    @app.delete('/api/v1/devices/{device_id}')
    def revoke_mobile_device(device_id: str,request: Request):
        identity=api_identity(request)
        if not row('SELECT id FROM mobile_devices WHERE id=? AND user_id=?',(device_id,identity.get('user_id'))): raise HTTPException(status_code=404,detail='Device not found.')
        revoke_device(device_id,identity['user_id']); audit(identity,'device.revoked','mobile_device',device_id); return {'message':'Device and its mobile sessions were revoked.'}

    @app.get('/api/v1/notification-preferences')
    def notification_preferences(request: Request,user_id: str=''):
        identity,customer_id=customer_scope(request); target=user_id or identity.get('user_id')
        if target!=identity.get('user_id') and identity['role'] not in {'customer_owner','administrator'}: raise HTTPException(status_code=403,detail='Notification preference access denied.')
        if not row('SELECT id FROM partner_users WHERE id=? AND customer_id=?',(target,customer_id)): raise HTTPException(status_code=404,detail='User not found in this customer account.')
        return rows('SELECT * FROM notification_preferences WHERE user_id=? AND customer_id=? ORDER BY event_type,site_id,camera_id',(target,customer_id))

    @app.put('/api/v1/notification-preferences')
    def save_notification_preference(request: Request,payload: dict):
        identity,customer_id=customer_scope(request); user=_target_user(identity,customer_id,payload); event_type=str(payload.get('event_type',''))
        if event_type not in EVENT_TYPES: raise HTTPException(status_code=400,detail='Unsupported notification event type.')
        severity=str(payload.get('severity','all'))
        if severity not in SEVERITIES: raise HTTPException(status_code=400,detail='Unsupported notification severity.')
        site_id=str(payload.get('site_id') or '') or None; camera_id=str(payload.get('camera_id') or '') or None; _validate_scope(customer_id,site_id,camera_id); _validate_user_access(user,site_id,camera_id); now=datetime.now().isoformat()
        existing=row('SELECT id FROM notification_preferences WHERE user_id=? AND customer_id=? AND COALESCE(site_id,\'\')=? AND COALESCE(camera_id,\'\')=? AND event_type=?',(user['id'],customer_id,site_id or '',camera_id or '',event_type)); values=(severity,str(payload.get('schedule_start','00:00')),str(payload.get('schedule_end','23:59')),int(bool(payload.get('in_app',True))),int(bool(payload.get('email',False))),int(bool(payload.get('web_push',False))),int(bool(payload.get('sms',False))),int(bool(payload.get('enabled',True))),now)
        with connection() as db:
            if existing: db.execute('UPDATE notification_preferences SET severity=?,schedule_start=?,schedule_end=?,in_app=?,email=?,web_push=?,sms=?,enabled=?,updated_at=? WHERE id=?',values+(existing['id'],)); preference_id=existing['id']
            else: preference_id=secrets.token_hex(16); db.execute('INSERT INTO notification_preferences(id,user_id,customer_id,site_id,camera_id,event_type,severity,schedule_start,schedule_end,in_app,email,web_push,sms,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(preference_id,user['id'],customer_id,site_id,camera_id,event_type)+values+(now,))
        audit(identity,'notification_preference.changed','notification_preference',preference_id,{'user_id':user['id'],'event_type':event_type,'site_id':site_id,'camera_id':camera_id}); return {'id':preference_id,'message':'Notification preference saved.'}

    @app.get('/api/v1/notifications')
    def notification_history(request: Request,status: str='all',limit: int=100):
        identity,customer_id=customer_scope(request); query='SELECT * FROM notifications WHERE user_id=? AND customer_id=? AND dismissed_at IS NULL'; params=[identity.get('user_id'),customer_id]
        if status=='unread': query+=' AND read_at IS NULL'
        elif status=='read': query+=' AND read_at IS NOT NULL'
        elif status!='all': raise HTTPException(status_code=400,detail='Notification status must be all, read, or unread.')
        items=rows(query+' ORDER BY timestamp DESC LIMIT ?',tuple(params+[max(1,min(500,limit))])); unread=row('SELECT COUNT(*) AS count FROM notifications WHERE user_id=? AND customer_id=? AND read_at IS NULL AND dismissed_at IS NULL',(identity.get('user_id'),customer_id))['count']; return {'unread':unread,'notifications':items}

    @app.post('/api/v1/notifications/{notification_id}/{action}')
    def notification_action(notification_id: str,action: str,request: Request):
        identity,customer_id=customer_scope(request)
        if action not in ACTIONS: raise HTTPException(status_code=400,detail='Unsupported notification action.')
        notification=row('SELECT id FROM notifications WHERE id=? AND user_id=? AND customer_id=?',(notification_id,identity.get('user_id'),customer_id))
        if not notification: raise HTTPException(status_code=404,detail='Notification not found.')
        with connection() as db: db.execute(f'UPDATE notifications SET {ACTIONS[action]}=? WHERE id=?',(datetime.now().isoformat(),notification_id))
        audit(identity,'notification.'+action,'notification',notification_id); return {'message':action.title()+' saved.'}

    @app.post('/api/v1/admin/notifications')
    def create_notification(request: Request,payload: dict):
        identity=api_identity(request)
        if identity['role']!='administrator': raise HTTPException(status_code=403,detail='Administrator permission required.')
        customer_id=str(payload.get('customer_id','')); user=_target_user(identity,customer_id,payload); site_id=str(payload.get('site_id') or '') or None; camera_id=str(payload.get('camera_id') or '') or None; _validate_scope(customer_id,site_id,camera_id); _validate_user_access(user,site_id,camera_id); event_type=str(payload.get('event_type',''))
        if event_type not in EVENT_TYPES: raise HTTPException(status_code=400,detail='Unsupported notification event type.')
        notification_id=secrets.token_hex(16); now=datetime.now().isoformat(); notification={'id':notification_id,'title':str(payload.get('title') or event_type.replace('_',' ').title())[:200],'message':str(payload.get('message') or '')[:1000]}
        with connection() as db: db.execute('INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,event_type,severity,title,message,timestamp,thumbnail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(notification_id,user['id'],customer_id,site_id,camera_id,payload.get('event_id'),payload.get('recording_id'),event_type,payload.get('severity','info'),notification['title'],notification['message'],payload.get('timestamp') or now,payload.get('thumbnail'),now))
        preference=row('SELECT * FROM notification_preferences WHERE user_id=? AND customer_id=? AND event_type=? AND enabled=1 ORDER BY camera_id DESC,site_id DESC LIMIT 1',(user['id'],customer_id,event_type)) or {'in_app':1,'email':0,'web_push':0,'sms':0}; deliveries=[]
        for channel in ('in_app','email','web_push','sms'):
            if not preference.get(channel): continue
            result=CHANNELS[channel].send(notification,user['email']); deliveries.append(result)
            with connection() as db: db.execute('INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,created_at) VALUES(?,?,?,?,?,?,?)',(secrets.token_hex(12),notification_id,channel,result['status'],result['provider'],result.get('error'),now))
        audit(identity,'notification.created','notification',notification_id,{'user_id':user['id'],'channels':[item['channel'] for item in deliveries]}); return {'id':notification_id,'deliveries':deliveries,'message':'Notification stored and configured preview channels processed.'}
