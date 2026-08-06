import hashlib
import secrets
from datetime import datetime,timedelta

from partner_db import connection,row

ACCESS_MINUTES=15
REFRESH_DAYS=30


def token_digest(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()


def register_device(user: dict,device_uid: str,device_type='mobile',platform='unknown',app_version='',push_token='',now=None):
    current=now or datetime.now(); existing=row('SELECT id,revoked_at FROM mobile_devices WHERE user_id=? AND device_uid=?',(user['id'],device_uid))
    device_id=existing['id'] if existing else secrets.token_hex(16)
    with connection() as db:
        if existing: db.execute('UPDATE mobile_devices SET device_type=?,platform=?,app_version=?,push_token=?,last_active_at=?,revoked_at=NULL WHERE id=?',(device_type,platform,app_version,push_token or None,current.isoformat(),device_id))
        else: db.execute('INSERT INTO mobile_devices(id,user_id,customer_id,device_uid,device_type,platform,push_token,last_active_at,app_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(device_id,user['id'],user.get('customer_id'),device_uid,device_type,platform,push_token or None,current.isoformat(),app_version,current.isoformat()))
    return device_id


def issue_mobile_tokens(user: dict,device_id: str,family_id=None,now=None):
    current=now or datetime.now(); access=secrets.token_urlsafe(48); refresh=secrets.token_urlsafe(64); session_id=secrets.token_hex(16); refresh_id=secrets.token_hex(16); family=family_id or secrets.token_hex(16)
    with connection() as db:
        db.execute('INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,token_hash,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(session_id,user['id'],user['email'],user['role'],'mobile:'+device_id,'api',token_digest(access),current.isoformat(),current.isoformat(),(current+timedelta(minutes=ACCESS_MINUTES)).isoformat()))
        db.execute('INSERT INTO mobile_refresh_tokens(id,family_id,user_id,device_id,token_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',(refresh_id,family,user['id'],device_id,token_digest(refresh),current.isoformat(),(current+timedelta(days=REFRESH_DAYS)).isoformat()))
        db.execute('UPDATE mobile_devices SET last_active_at=? WHERE id=?',(current.isoformat(),device_id))
    return {'access_token':access,'refresh_token':refresh,'token_type':'bearer','expires_in':ACCESS_MINUTES*60,'refresh_expires_in':REFRESH_DAYS*86400,'session_id':session_id,'refresh_id':refresh_id,'family_id':family,'device_id':device_id}


def rotate_refresh_token(raw: str,now=None):
    current=now or datetime.now(); record=row('''SELECT r.*,u.email,u.role,u.partner_id,u.customer_id,d.revoked_at AS device_revoked
        FROM mobile_refresh_tokens r JOIN partner_users u ON u.id=r.user_id JOIN mobile_devices d ON d.id=r.device_id WHERE r.token_hash=?''',(token_digest(raw),))
    if not record: return None,'invalid'
    if record.get('used_at') or record.get('reuse_detected_at'):
        with connection() as db:
            db.execute('UPDATE mobile_refresh_tokens SET revoked_at=COALESCE(revoked_at,?),reuse_detected_at=? WHERE family_id=?',(current.isoformat(),current.isoformat(),record['family_id']))
            db.execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND device_name=? AND session_type='api' AND revoked_at IS NULL",(current.isoformat(),record['user_id'],'mobile:'+record['device_id']))
            db.execute('UPDATE mobile_devices SET revoked_at=? WHERE id=?',(current.isoformat(),record['device_id']))
        return None,'reuse_detected'
    if record.get('revoked_at') or record.get('device_revoked'): return None,'revoked'
    if record['expires_at']<=current.isoformat(): return None,'expired'
    user={'id':record['user_id'],'email':record['email'],'role':record['role'],'partner_id':record.get('partner_id'),'customer_id':record.get('customer_id')}
    with connection() as db:
        db.execute('UPDATE mobile_refresh_tokens SET used_at=? WHERE id=?',(current.isoformat(),record['id']))
        db.execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND device_name=? AND session_type='api' AND revoked_at IS NULL",(current.isoformat(),record['user_id'],'mobile:'+record['device_id']))
    tokens=issue_mobile_tokens(user,record['device_id'],record['family_id'],current)
    with connection() as db: db.execute('UPDATE mobile_refresh_tokens SET replaced_by=? WHERE id=?',(tokens['refresh_id'],record['id']))
    return tokens,'ok'


def revoke_device(device_id: str,user_id: str,now=None):
    current=(now or datetime.now()).isoformat()
    with connection() as db:
        db.execute('UPDATE mobile_devices SET revoked_at=? WHERE id=? AND user_id=?',(current,device_id,user_id))
        db.execute('UPDATE mobile_refresh_tokens SET revoked_at=? WHERE device_id=? AND user_id=? AND revoked_at IS NULL',(current,device_id,user_id))
        db.execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND device_name=? AND session_type='api' AND revoked_at IS NULL",(current,user_id,'mobile:'+device_id))


def refresh_status(raw: str):
    record=row('SELECT used_at,revoked_at,reuse_detected_at,expires_at FROM mobile_refresh_tokens WHERE token_hash=?',(token_digest(raw),)); return dict(record) if record else None
