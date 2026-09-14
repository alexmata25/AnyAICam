import secrets
from datetime import datetime

from notification_service import CHANNELS
from partner_db import connection,row,rows

SUPPORTED={'motion','smart_motion','person','vehicle','line_crossing','intrusion','lpr','people_counting','occupancy','camera_offline','recording_stopped','appliance_offline','low_disk','high_cpu','software_update'}


def fanout_appliance_event(appliance: dict,event: dict):
    customer_id=appliance.get('customer_id'); site_id=appliance.get('site_id'); camera_id=str(event.get('camera_id') or '') or None; event_type=str(event.get('event_type') or '')
    if not customer_id or event_type not in SUPPORTED: return 0
    now=datetime.now(); current_time=now.strftime('%H:%M'); users=rows("SELECT id,email,role,camera_access_mode FROM partner_users WHERE customer_id=? AND approved=1 AND account_status='active' AND role IN ('customer_owner','customer_viewer')",(customer_id,)); created=0
    for user in users:
        if user['role']=='customer_viewer' and camera_id:
            # Notifications Reliability Phase (2026-09-14): a customer_viewer
            # with NO customer_camera_permissions rows at all -- the real
            # state of every brand-new viewer, before a customer_owner has
            # granted any camera access -- previously fell through this
            # check entirely (`if [] and ...` is always False) and got
            # notified about EVERY camera on the account, the opposite of
            # camera_access.py's own established fail-closed default
            # (DEFAULT_ACCESS_MODE='selected': "authorized only if camera_id
            # is explicitly in permitted_camera_ids... never True by
            # default"). partner_users.camera_access_mode (db_migrations.py,
            # NOT NULL DEFAULT 'selected') is that same policy's own stored
            # value, so it -- not silence -- is what decides the empty-rows
            # case: 'all' still notifies about every camera (no explicit
            # rows required, matching camera_access.is_camera_authorized()'s
            # access_mode='all' branch); anything else (the real default)
            # notifies about none until the owner grants specific access.
            # A viewer who DOES have rows keeps the exact prior behavior --
            # can_alerts=1 on that specific camera, unchanged.
            permissions=rows('SELECT camera_id,can_alerts FROM customer_camera_permissions WHERE user_id=?',(user['id'],))
            if permissions:
                if camera_id not in {item['camera_id'] for item in permissions if item['can_alerts']}: continue
            elif (user['camera_access_mode'] or 'selected')!='all':
                continue
        preference=row('''SELECT * FROM notification_preferences WHERE user_id=? AND customer_id=? AND event_type=? AND enabled=1
            AND (site_id IS NULL OR site_id=?) AND (camera_id IS NULL OR camera_id=?) ORDER BY camera_id DESC,site_id DESC LIMIT 1''',(user['id'],customer_id,event_type,site_id,camera_id))
        if preference and not (preference['schedule_start']<=current_time<=preference['schedule_end']): continue
        channels=preference or {'in_app':1,'email':0,'web_push':0,'sms':0}; notification_id=secrets.token_hex(16); timestamp=str(event.get('timestamp') or now.isoformat()); title=event_type.replace('_',' ').title(); message=str(event.get('message') or f'{title} detected')[:1000]
        notification={'id':notification_id,'title':title,'message':message}
        with connection() as db: db.execute('INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,event_type,severity,title,message,timestamp,thumbnail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(notification_id,user['id'],customer_id,site_id,camera_id,event.get('id'),event.get('recording_id') or event.get('linked_recording'),event_type,event.get('severity','info'),title,message,timestamp,event.get('thumbnail'),now.isoformat()))
        for channel in ('in_app','email','web_push','sms'):
            if not channels.get(channel): continue
            try: result=CHANNELS[channel].send(notification,user['email'])
            except Exception as error: result={'channel':channel,'status':'error','provider':'configured','error':str(error)}
            with connection() as db: db.execute('INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,created_at) VALUES(?,?,?,?,?,?,?)',(secrets.token_hex(12),notification_id,channel,result['status'],result['provider'],result.get('error'),now.isoformat()))
        created+=1
    return created
