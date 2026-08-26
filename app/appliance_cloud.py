from cloud_config import settings as cloud_settings
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from appliance_protocol import ALLOWED_COMMANDS, LIVE_RELAY_SESSION_DURATION_SECONDS, RateLimiter, cloud_settings, decrypt_camera_credentials, health_state, live_relay_s3_prefix, live_relay_session_name, live_relay_session_policy, sanitize_appliance_payload, sanitize_discovery_results, validate_request_time
from live_manifest import LiveManifestStore
from partner_db import audit, connection, password_hash, row, rows, verify_password
from partner_portal import partner_identity, require_partner_access
from notification_engine import fanout_appliance_event
from recording_credentials import RECORDING_SESSION_DURATION_SECONDS, recording_s3_prefix, recording_session_name, recording_session_policy

try:
    import boto3
except ImportError:
    boto3 = None

logger=logging.getLogger('anyaicam.appliance')
request_limiter=RateLimiter(120,60); activation_limiter=RateLimiter(10,300)
LIVE_RELAY_ENABLED=os.getenv('ANYAICAM_LIVE_RELAY_ENABLED','false').strip().lower()=='true'
LIVE_UPLOAD_ROLE_ARN=os.getenv('ANYAICAM_LIVE_UPLOAD_ROLE_ARN','').strip()
LIVE_RELAY_S3_BUCKET=os.getenv('ANYAICAM_S3_BUCKET','').strip()
LIVE_RELAY_AWS_REGION=os.getenv('AWS_REGION',os.getenv('AWS_DEFAULT_REGION','')).strip()
live_manifest_store=LiveManifestStore(Path(os.getenv('ANYAICAM_LIVE_MANIFEST_FILE','/app/recordings/live_manifest.json')))
# R1 (recording-pipeline roadmap): independent flag/role/bucket from live
# relay, deliberately not defaulted to the live bucket/role -- see
# docs/r1-recording-iam.md. All three are unset until that IAM design is
# actually applied, so recording_upload_credentials() below fails closed
# with 503 even if ANYAICAM_RECORDING_UPLOAD_ENABLED were ever set true
# ahead of that.
RECORDING_UPLOAD_ENABLED=os.getenv('ANYAICAM_RECORDING_UPLOAD_ENABLED','false').strip().lower()=='true'
RECORDING_UPLOAD_ROLE_ARN=os.getenv('ANYAICAM_RECORDING_UPLOAD_ROLE_ARN','').strip()
RECORDING_S3_BUCKET=os.getenv('ANYAICAM_RECORDING_S3_BUCKET','').strip()
RECORDING_AWS_REGION=os.getenv('AWS_REGION',os.getenv('AWS_DEFAULT_REGION','')).strip()
# Analytics-event sync (separate milestone, separate flag from recording
# upload -- deliberately independently toggleable). No AWS/STS involved at
# all; this only ever writes to the detection_events SQL table.
ANALYTICS_SYNC_ENABLED=os.getenv('ANYAICAM_ANALYTICS_SYNC_ENABLED','false').strip().lower()=='true'


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


def _authorized_camera(appliance: dict,camera_id: str) -> dict:
    camera=row('SELECT * FROM cameras WHERE id=? AND appliance_id=?',(camera_id,appliance['id']))
    if not camera: raise HTTPException(status_code=403,detail='Camera is not assigned to this appliance.')
    return camera


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
        new_uptime=int(safe.get('uptime_seconds',0))
        # A restart is inferred, never self-reported: uptime_seconds resetting
        # to a value meaningfully lower than what this same appliance last
        # reported is the one signal a heartbeat payload can't omit or get
        # wrong, since it comes straight from /proc/uptime every cycle
        # regardless of agent version. A 30s tolerance absorbs ordinary
        # measurement jitter between consecutive heartbeats without ever
        # miscounting a real restart as jitter -- a genuine restart drops
        # uptime by minutes at least.
        previous_uptime=int(appliance.get('uptime_seconds') or 0); restarted=new_uptime<previous_uptime-30
        with connection() as db:
            if restarted: db.execute('UPDATE appliances SET restart_count=COALESCE(restart_count,0)+1 WHERE id=?',(appliance['id'],))
            db.execute('UPDATE appliances SET state=?,online_status=?,last_check_in=?,software_version=?,uptime_seconds=?,cpu=?,memory=?,disk_capacity=?,disk=?,recording_used=?,last_error=?,camera_capacity=? WHERE id=?',(state,state,now,safe.get('software_version','Unknown'),new_uptime,float(safe.get('cpu',0)),float(safe.get('memory',0)),float(safe.get('disk_capacity',0)),float(safe.get('disk_used',0)),float(safe.get('recording_used',0)),safe.get('last_error'),int(safe.get('camera_count',0)),appliance['id']))
            db.execute('INSERT INTO appliance_health_history(appliance_id,status,cpu,memory,disk_capacity,disk_used,recording_used,uptime_seconds,camera_count,last_error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(appliance['id'],state,safe.get('cpu',0),safe.get('memory',0),safe.get('disk_capacity',0),safe.get('disk_used',0),safe.get('recording_used',0),safe.get('uptime_seconds',0),safe.get('camera_count',0),safe.get('last_error'),now))
        return {'status':'accepted','state':state,'warnings':warnings,'restarted':restarted,'server_time':int(time.time())}

    @app.post('/api/appliance/recordings/backlog')
    def recordings_backlog(request: Request,payload: dict) -> dict:
        """Fleet-visible summary of the edge-side recording_uploader.py
        backlog -- see upload_pending_count's own column comment in
        db_migrations.py. Reported by the appliance itself once per
        upload-worker scan cycle; deliberately separate from heartbeat()
        (a different, existing worker with its own independent cadence and
        failure domain -- this must keep working even if the heartbeat
        agent process is unhealthy, and vice versa)."""
        appliance=authenticate_appliance(request); safe=sanitize_appliance_payload(payload); now=datetime.now().isoformat()
        pending=max(0,int(safe.get('pending_count',0))); quarantined=max(0,int(safe.get('quarantined_count',0)))
        with connection() as db: db.execute('UPDATE appliances SET upload_pending_count=?,upload_quarantined_count=?,upload_backlog_reported_at=? WHERE id=?',(pending,quarantined,now,appliance['id']))
        return {'status':'accepted'}

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
        # Talk-down capability foundation: an item may optionally include a
        # "talk_down" object -- {"supported": bool, "metadata": {...}} --
        # reported by the appliance's own ONVIF discovery/rescan (not yet
        # implemented on the edge side; this route is ready to receive it
        # the moment it is). Absent/malformed talk_down leaves the
        # camera's existing talk_down_supported value untouched -- an
        # appliance running older code that never sends this key must
        # never be read as "confirmed unsupported", only as "not yet
        # reported this cycle". camera_id is always scoped to this
        # authenticated appliance's own rows -- never trusted blindly
        # from the payload beyond that ownership check.
        appliance=authenticate_appliance(request); safe=sanitize_appliance_payload(payload); items=safe.get('cameras',[]); now=datetime.now().isoformat()
        with connection() as db:
            for item in items:
                camera_id=str(item.get('id',''))[:100]
                if not camera_id: continue
                db.execute('INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,analytics,last_recording_at,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(appliance_id,camera_id) DO UPDATE SET name=excluded.name,online=excluded.online,recording=excluded.recording,analytics=excluded.analytics,last_recording_at=excluded.last_recording_at,last_error=excluded.last_error,updated_at=excluded.updated_at',(appliance['id'],camera_id,item.get('name','Camera'),int(bool(item.get('online'))),int(bool(item.get('recording'))),int(bool(item.get('analytics'))),item.get('last_recording_at'),item.get('last_error'),now))
                talk_down=item.get('talk_down')
                if isinstance(talk_down,dict) and 'supported' in talk_down:
                    supported=1 if talk_down.get('supported') else 0
                    metadata=talk_down.get('metadata')
                    metadata_json=json.dumps(metadata) if isinstance(metadata,dict) else None
                    db.execute('UPDATE cameras SET talk_down_supported=?,talk_down_metadata=?,talk_down_verified_at=? WHERE id=? AND appliance_id=?',(supported,metadata_json,now,camera_id,appliance['id']))
        return {'status':'accepted','camera_count':len(items),'credentials_received':False}

    @app.get('/api/appliance/configuration')
    def appliance_configuration(request: Request) -> dict:
        appliance=authenticate_appliance(request); camera_items=rows('SELECT id,name,site_id,resolution,status,camera_number,cloud_recording_mode AS recording_mode,people_counting_enabled FROM cameras WHERE appliance_id=? ORDER BY camera_number,name',(appliance['id'],)); return {'configuration_version':max([item.get('status','') for item in camera_items],default='empty'),'cameras':camera_items,'camera_credentials_included':False}

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

    @app.post('/api/appliance/live/{camera_id}/session')
    def live_relay_session(request: Request,camera_id: str) -> dict:
        appliance=authenticate_appliance(request)
        if not LIVE_RELAY_ENABLED or not appliance.get('live_relay_pilot'):
            raise HTTPException(status_code=404,detail='Live relay is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        if boto3 is None or not LIVE_UPLOAD_ROLE_ARN or not LIVE_RELAY_S3_BUCKET or not LIVE_RELAY_AWS_REGION:
            raise HTTPException(status_code=503,detail='Live relay is not configured.')
        policy=live_relay_session_policy(LIVE_RELAY_S3_BUCKET,camera['customer_id'],camera['site_id'],appliance['id'],camera_id)
        session_name=live_relay_session_name(appliance['id'],camera_id)
        try:
            sts=boto3.client('sts',region_name=LIVE_RELAY_AWS_REGION)
            assumed=sts.assume_role(RoleArn=LIVE_UPLOAD_ROLE_ARN,RoleSessionName=session_name,Policy=json.dumps(policy),DurationSeconds=LIVE_RELAY_SESSION_DURATION_SECONDS)
        except Exception as error:
            logger.exception('live_relay.assume_role_failed appliance_id=%s camera_id=%s',appliance['id'],camera_id)
            raise HTTPException(status_code=502,detail='Could not obtain a live-upload credential.') from error
        issued=assumed['Credentials']
        audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.live_relay_session_issued','camera',camera_id,{'session_name':session_name})
        return {
            'status':'accepted',
            'bucket':LIVE_RELAY_S3_BUCKET,
            'key_prefix':live_relay_s3_prefix(camera['customer_id'],camera['site_id'],appliance['id'],camera_id),
            'credentials':{
                'access_key_id':issued['AccessKeyId'],
                'secret_access_key':issued['SecretAccessKey'],
                'session_token':issued['SessionToken'],
                'expiration':issued['Expiration'].isoformat(),
            },
        }

    @app.get('/api/appliance/recordings/status')
    def recording_upload_status(request: Request) -> dict:
        # Deliberately the cheapest possible check -- a single env-var read,
        # no S3/STS call, no per-camera authorization -- so the appliance can
        # call this every scan tick (not just when a session is expiring)
        # without adding any real load. Exists so an EC2-side disable takes
        # effect on the appliance within one scan interval instead of only
        # once an already-cached, still-valid STS session (up to 900s) runs
        # out -- see recording_uploader.py's _upload_currently_authorized()
        # and _ensure_session()'s revalidation.
        authenticate_appliance(request)
        return {'enabled':RECORDING_UPLOAD_ENABLED}

    @app.post('/api/appliance/recordings/{camera_id}/credentials')
    def recording_upload_credentials(request: Request,camera_id: str) -> dict:
        # R1 (recording-pipeline roadmap): mirrors live_relay_session()
        # above exactly, for a separate `recordings/` prefix and a
        # separate flag/role -- see recording_credentials.py and
        # docs/r1-recording-iam.md. RECORDING_UPLOAD_ROLE_ARN/
        # RECORDING_S3_BUCKET are unset until the IAM design in that doc
        # is actually applied, so this returns 503 even if the flag
        # alone were ever set true ahead of that -- fails closed the
        # same way Phase 2 did for live relay before Phase 1's IAM was
        # applied. Nothing calls this route yet (that's R3); nothing
        # reads what it would let an appliance write (that's R2/R4).
        appliance=authenticate_appliance(request)
        if not RECORDING_UPLOAD_ENABLED:
            raise HTTPException(status_code=404,detail='Recording upload is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        if boto3 is None or not RECORDING_UPLOAD_ROLE_ARN or not RECORDING_S3_BUCKET or not RECORDING_AWS_REGION:
            raise HTTPException(status_code=503,detail='Recording upload is not configured.')
        policy=recording_session_policy(RECORDING_S3_BUCKET,camera['customer_id'],camera['site_id'],appliance['id'],camera_id)
        session_name=recording_session_name(appliance['id'],camera_id)
        try:
            sts=boto3.client('sts',region_name=RECORDING_AWS_REGION)
            assumed=sts.assume_role(RoleArn=RECORDING_UPLOAD_ROLE_ARN,RoleSessionName=session_name,Policy=json.dumps(policy),DurationSeconds=RECORDING_SESSION_DURATION_SECONDS)
        except Exception as error:
            logger.exception('recording_upload.assume_role_failed appliance_id=%s camera_id=%s',appliance['id'],camera_id)
            raise HTTPException(status_code=502,detail='Could not obtain a recording-upload credential.') from error
        issued=assumed['Credentials']
        audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.recording_upload_credentials_issued','camera',camera_id,{'session_name':session_name})
        return {
            'status':'accepted',
            'bucket':RECORDING_S3_BUCKET,
            'key_prefix':recording_s3_prefix(camera['customer_id'],camera['site_id'],appliance['id'],camera_id),
            'credentials':{
                'access_key_id':issued['AccessKeyId'],
                'secret_access_key':issued['SecretAccessKey'],
                'session_token':issued['SessionToken'],
                'expiration':issued['Expiration'].isoformat(),
            },
        }

    @app.post('/api/appliance/recordings/{camera_id}/available')
    def recording_available(request: Request,camera_id: str,payload: dict) -> dict:
        # R2 (recording-pipeline roadmap): catalogs one completed
        # recording object, mirroring live_relay_segment_available()
        # below almost exactly -- same auth/flag/prefix-validation
        # shape, writing to the durable `recordings` table (R1's own
        # migration) instead of the ephemeral live manifest. Nothing
        # calls this route yet (that's R3's appliance-side uploader);
        # nothing reads what it catalogs (that's R4).
        appliance=authenticate_appliance(request)
        if not RECORDING_UPLOAD_ENABLED: raise HTTPException(status_code=404,detail='Recording upload is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        safe=sanitize_appliance_payload(payload)
        s3_key=str(safe.get('s3_key','')).strip()
        if not s3_key: raise HTTPException(status_code=400,detail='s3_key is required.')
        expected_prefix=recording_s3_prefix(camera['customer_id'],camera['site_id'],appliance['id'],camera_id)
        if not s3_key.startswith(expected_prefix):
            raise HTTPException(status_code=403,detail="s3_key is outside this camera's authorized prefix.")
        started_at=str(safe.get('started_at','')).strip(); ended_at=str(safe.get('ended_at','')).strip()
        if not started_at or not ended_at: raise HTTPException(status_code=400,detail='started_at and ended_at are required.')
        duration_seconds=safe.get('duration_seconds'); size_bytes=safe.get('size_bytes')
        recording_id=secrets.token_hex(12); now=datetime.now().isoformat()
        with connection() as db:
            try:
                db.execute(
                    'INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,duration_seconds,size_bytes,status,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (recording_id,camera['customer_id'],camera['site_id'],appliance['id'],camera_id,s3_key,started_at,ended_at,
                     int(duration_seconds) if duration_seconds is not None else None,
                     int(size_bytes) if size_bytes is not None else None,
                     'available',now),
                )
            except Exception:
                # Idempotent replay: a byte-identical (camera_id, s3_key)
                # resubmission is a 200 no-op, not an error -- the same
                # duplicate-replay-is-a-200 pattern already used for
                # appliance_commands/update-result reporting elsewhere
                # in this codebase.
                existing=db.execute('SELECT id FROM recordings WHERE camera_id=? AND s3_key=?',(camera_id,s3_key)).fetchone()
                if existing: return {'status':'duplicate','recording_id':existing['id']}
                raise HTTPException(status_code=500,detail='Could not record this upload.')
        audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.recording_available','recording',recording_id,{'camera_id':camera_id,'s3_key':s3_key})
        return {'status':'accepted','recording_id':recording_id}

    @app.post('/api/appliance/analytics/{camera_id}/events')
    def analytics_event_available(request: Request,camera_id: str,payload: dict) -> dict:
        # Analytics-event sync milestone: catalogs one local YOLO/motion
        # detection event into the durable, tenant-scoped detection_events
        # table. Mirrors recording_available() above almost exactly --
        # same auth/flag shape, same idempotent-replay-is-a-200 pattern.
        # customer_id/site_id/appliance_id/camera_id are ALL resolved
        # server-side from the authenticated appliance + the
        # authorized-camera lookup below -- camera['site_id'] (the
        # camera's own authoritative site) is what's stored, never
        # anything from the payload. Only a fixed allowlist of fields is
        # ever read from the payload; local-only fields like the
        # appliance's thumbnail file path or linked_recording are never
        # looked at, so they can never reach this table by construction.
        appliance=authenticate_appliance(request)
        if not ANALYTICS_SYNC_ENABLED: raise HTTPException(status_code=404,detail='Analytics sync is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        safe=sanitize_appliance_payload(payload)
        local_event_id=str(safe.get('local_event_id','')).strip()
        event_type=str(safe.get('event_type','')).strip()
        event_timestamp=str(safe.get('event_timestamp','')).strip()
        if not local_event_id or not event_type or not event_timestamp:
            raise HTTPException(status_code=400,detail='local_event_id, event_type, and event_timestamp are required.')
        confidence=safe.get('confidence')
        object_count=safe.get('object_count')
        detections=safe.get('detections')
        detections_json=json.dumps(detections) if isinstance(detections,list) else None
        event_id=secrets.token_hex(12); now=datetime.now().isoformat()
        with connection() as db:
            try:
                db.execute(
                    'INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,object_count,detections_json,event_timestamp,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (event_id,camera['customer_id'],camera['site_id'],appliance['id'],camera_id,local_event_id,event_type,
                     float(confidence) if confidence is not None else None,
                     int(object_count) if object_count is not None else 1,
                     detections_json,event_timestamp,now),
                )
            except Exception:
                existing=db.execute('SELECT id FROM detection_events WHERE camera_id=? AND local_event_id=?',(camera_id,local_event_id)).fetchone()
                if existing: return {'status':'duplicate','event_id':existing['id']}
                raise HTTPException(status_code=500,detail='Could not record this event.')
        return {'status':'accepted','event_id':event_id}

    @app.post('/api/appliance/live/{camera_id}/segment-available')
    def live_relay_segment_available(request: Request,camera_id: str,payload: dict) -> dict:
        appliance=authenticate_appliance(request)
        if not LIVE_RELAY_ENABLED: raise HTTPException(status_code=404,detail='Live relay is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        safe=sanitize_appliance_payload(payload); segment_key=str(safe.get('segment_key','')).strip()
        if not segment_key: raise HTTPException(status_code=400,detail='segment_key is required.')
        expected_prefix=live_relay_s3_prefix(camera['customer_id'],camera['site_id'],appliance['id'],camera_id)
        if not segment_key.startswith(expected_prefix):
            raise HTTPException(status_code=403,detail="segment_key is outside this camera's authorized prefix.")
        sequence=safe.get('sequence')
        entry=live_manifest_store.record_segment(camera_id,segment_key,int(sequence) if sequence is not None else None)
        return {'status':'accepted','segment_count':len(entry['segments'])}

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
        status=str(payload.get('status','running')); progress=max(0,min(100,int(payload.get('progress',0)))); results=sanitize_discovery_results(payload.get('results',[]))
        if status not in {'running','complete','error'}: raise HTTPException(status_code=400,detail='Unsupported discovery status.')
        with connection() as db:
            db.execute('UPDATE camera_scan_jobs SET status=?,progress=?,results_json=?,message=?,updated_at=? WHERE id=?',(status,progress,json.dumps(results),str(payload.get('message','Discovery update received.'))[:500],datetime.now().isoformat(),job_id))
        audit({'email':appliance['cloud_id'],'role':'appliance'},'camera.discovery_result','camera_scan_job',job_id,{'status':status,'count':len(results)}); return {'message':'Discovery result accepted; no camera records created until explicit binding.'}

    # ---- Camera provisioning (appliance side). Re-homed here from the
    # weaker "-legacy" appliance_agent() bearer-vs-activation-token check
    # onto authenticate_appliance() -- the same bearer+nonce+timestamp+
    # replay-table mechanism every other appliance route on this file
    # uses -- plus an explicit tenancy check on the job row itself, the
    # same defense-in-depth shape as _authorized_camera(). Credentials
    # are still encrypted at rest by the customer-facing route in
    # partner_workspace.py and decrypted here only once, at the instant
    # of delivery to the one already-authenticated appliance they were
    # queued for -- see appliance_protocol.encrypt/decrypt_camera_credentials.
    @app.get('/api/appliance/{cloud_id}/provisioning-jobs')
    def appliance_provisioning_jobs(request: Request,cloud_id: str) -> dict:
        appliance=authenticate_appliance(request)
        if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403,detail='Cloud ID does not match authenticated appliance.')
        jobs=rows("SELECT id,customer_id,site_id,device_key,camera_name,recording_mode,analytics_json,encrypted_credentials FROM camera_provisioning_requests WHERE appliance_id=? AND status='queued' ORDER BY created_at",(appliance['id'],))
        delivered=[]; now=datetime.now().isoformat()
        with connection() as db:
            for job in jobs:
                # Tenancy defense-in-depth: the job's own customer_id/
                # site_id must match the authenticated appliance's --
                # on top of the appliance_id filter above, mirroring
                # _authorized_camera()'s pattern. A job that somehow
                # carries a mismatched tenant is skipped, not delivered.
                if job['customer_id']!=appliance['customer_id'] or job['site_id']!=appliance['site_id']: continue
                credentials=decrypt_camera_credentials(job.pop('encrypted_credentials'))
                delivered.append({'id':job['id'],'device_key':job['device_key'],'camera_name':job['camera_name'],'recording_mode':job['recording_mode'],'analytics':json.loads(job['analytics_json']),'credentials':credentials})
                # Single-delivery: cleared from storage the instant it is
                # handed to this specific, already-authenticated
                # appliance -- never retained at rest any longer than
                # the queue wait itself required.
                db.execute("UPDATE camera_provisioning_requests SET status='verifying',encrypted_credentials=NULL,updated_at=? WHERE id=?",(now,job['id']))
        audit({'email':appliance['cloud_id'],'role':'appliance'},'camera.provisioning_jobs_delivered','appliance',appliance['id'],{'count':len(delivered)})
        return {'jobs':delivered}

    @app.post('/api/appliance/{cloud_id}/provisioning-jobs/{job_id}')
    def appliance_submit_provisioning(request: Request,cloud_id: str,job_id: str,payload: dict) -> dict:
        appliance=authenticate_appliance(request)
        if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403,detail='Cloud ID does not match authenticated appliance.')
        job=row('SELECT * FROM camera_provisioning_requests WHERE id=? AND appliance_id=?',(job_id,appliance['id']))
        if not job: raise HTTPException(status_code=404,detail='Provisioning job not found.')
        if job['customer_id']!=appliance['customer_id'] or job['site_id']!=appliance['site_id']:
            raise HTTPException(status_code=403,detail='Provisioning job does not belong to this appliance.')
        success=bool(payload.get('success')); message=str(payload.get('message',''))[:500]
        now=datetime.now().isoformat(); camera_id=None
        with connection() as db:
            if success:
                # device_key is scoped to this one appliance, matching the
                # tenancy every other camera-facing route already enforces
                # -- a discovered device from one customer's appliance can
                # never collide with or attach to another tenant's camera.
                existing=db.execute('SELECT id FROM cameras WHERE appliance_id=? AND device_key=?',(appliance['id'],job['device_key'])).fetchone()
                if existing:
                    camera_id=existing['id']
                    db.execute("UPDATE cameras SET name=?,status='configured' WHERE id=?",(job['camera_name'],camera_id))
                else:
                    camera_id=secrets.token_hex(5)
                    db.execute(
                        'INSERT INTO cameras(id,customer_id,site_id,appliance_id,device_key,name,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                        (camera_id,job['customer_id'],job['site_id'],appliance['id'],job['device_key'],job['camera_name'],'configured',now),
                    )
                db.execute("UPDATE camera_provisioning_requests SET status='provisioned',camera_id=?,message=?,updated_at=? WHERE id=?",(camera_id,message or 'Camera provisioned.',now,job_id))
            else:
                db.execute("UPDATE camera_provisioning_requests SET status='failed',message=?,updated_at=? WHERE id=?",(message or 'Provisioning failed.',now,job_id))
        audit({'email':appliance['cloud_id'],'role':'appliance'},'camera.provisioning_result','camera_provisioning_request',job_id,{'success':success,'camera_id':camera_id})
        return {'message':'Provisioning result saved.'}

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

    @app.post('/api/admin/appliances/{appliance_id}/live-relay-pilot')
    def set_live_relay_pilot(request: Request,appliance_id: str,payload: dict) -> dict:
        identity=require_partner_access(request,{'administrator'})
        enabled=1 if payload.get('enabled') else 0
        with connection() as db:
            cursor=db.execute('UPDATE appliances SET live_relay_pilot=? WHERE id=?',(enabled,appliance_id))
            if cursor.rowcount!=1: raise HTTPException(status_code=404,detail='Appliance not found.')
        audit(identity,'appliance.live_relay_pilot_changed','appliance',appliance_id,{'enabled':bool(enabled)})
        return {'appliance_id':appliance_id,'live_relay_pilot':bool(enabled)}

    @app.post('/api/admin/cameras/{camera_id}/cloud-recording-mode')
    def set_cloud_recording_mode(request: Request,camera_id: str,payload: dict) -> dict:
        # No hidden default, by design (see db_migrations.py's own comment
        # on this column): only these three explicit values are ever
        # accepted, or null to clear back to "not set" (== continuous
        # behavior everywhere it's read). Anything else is a 400, never
        # silently coerced to one side or the other.
        #
        # 'disabled' (added for the per-camera cloud-recording-upload gate):
        # the master ANYAICAM_RECORDING_UPLOAD_ENABLED flag in
        # recording_uploader.py is only ever the appliance-wide permission
        # switch -- this is the per-camera authorization it was always
        # meant to be paired with, reusing this same existing column/route
        # rather than adding a second control surface. Unlike 'motion'
        # (upload only motion-overlapping segments) and 'continuous'/null
        # (upload everything), 'disabled' means this camera's recordings
        # are never uploaded or cataloged at all -- local recording and
        # retention are completely unaffected either way, exactly like the
        # other two values.
        identity=require_partner_access(request,{'administrator'})
        mode=payload.get('cloud_recording_mode')
        if mode is not None and mode not in ('motion','continuous','disabled'):
            raise HTTPException(status_code=400,detail="cloud_recording_mode must be 'motion', 'continuous', 'disabled', or null.")
        with connection() as db:
            cursor=db.execute('UPDATE cameras SET cloud_recording_mode=? WHERE id=?',(mode,camera_id))
            if cursor.rowcount!=1: raise HTTPException(status_code=404,detail='Camera not found.')
        audit(identity,'camera.cloud_recording_mode_changed','camera',camera_id,{'cloud_recording_mode':mode})
        return {'camera_id':camera_id,'cloud_recording_mode':mode}

    @app.post('/api/admin/cameras/{camera_id}/people-counting')
    def set_people_counting_enabled(request: Request,camera_id: str,payload: dict) -> dict:
        # Same no-hidden-default convention as cloud_recording_mode above
        # (see db_migrations.py's own comment on this column): only an
        # explicit true/false is ever accepted -- never coerced from a
        # billing/add-on selection automatically, since that automatic
        # link is exactly the analytics-entitlement disconnect this
        # column exists to start correcting. Enabling this here is a
        # necessary but not sufficient condition for the appliance to
        # actually run People Counting on this camera -- the appliance
        # also needs a configured counting-line rule for the same camera
        # (see people_counting.py, edge lineage) and its own
        # PEOPLE_COUNTING_ENABLED master flag turned on.
        identity=require_partner_access(request,{'administrator'})
        enabled=1 if payload.get('people_counting_enabled') else 0
        with connection() as db:
            cursor=db.execute('UPDATE cameras SET people_counting_enabled=? WHERE id=?',(enabled,camera_id))
            if cursor.rowcount!=1: raise HTTPException(status_code=404,detail='Camera not found.')
        audit(identity,'camera.people_counting_enabled_changed','camera',camera_id,{'people_counting_enabled':bool(enabled)})
        return {'camera_id':camera_id,'people_counting_enabled':bool(enabled)}

    @app.post('/api/partner/appliances/{appliance_id}/commands')
    def queue_command(request: Request,appliance_id: str,payload: dict) -> dict:
        identity=require_partner_access(request); command=str(payload.get('command',''))
        if not payload.get('confirmed'): raise HTTPException(status_code=400,detail='Explicit command confirmation is required.')
        if command not in ALLOWED_COMMANDS: raise HTTPException(status_code=400,detail='Only approved appliance commands are allowed. Remote shell is not supported.')
        from partner_db import require_permission
        try: require_permission(identity,'appliance.action')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        # De-dup guard: an identical command already pending or delivered
        # (not yet completed/failed/expired) for this appliance is returned
        # as-is instead of queuing a second copy -- a partner double-
        # clicking "Restart service", or a dashboard auto-refresh replaying
        # the same request, must never queue N redundant restarts/reboots
        # for one appliance to work through.
        existing=row("SELECT id FROM appliance_commands WHERE appliance_id=? AND command=? AND status IN ('pending','delivered') ORDER BY created_at LIMIT 1",(appliance_id,command))
        if existing: return {'id':existing['id'],'status':'pending','message':'An identical command is already queued for this appliance; not queuing a duplicate.'}
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
            pending_count=item.get('upload_pending_count'); quarantined_count=item.get('upload_quarantined_count')
            backlog_text=f'{pending_count} pending · {quarantined_count} quarantined' if pending_count is not None else 'Not yet reported'
            cards.append(f'''<article class="panel"><div class="panel-head"><div><h2>{escape(item['cloud_id'])}</h2><div class="health-detail">{escape(item.get('customer_name') or 'Unassigned')} · {escape(item.get('site_name') or 'No site')} · {escape(item.get('software_version') or 'Unknown')}</div></div><span class="pill">{escape(item.get('state') or 'offline')}</span></div><div class="health-row"><span>Last check-in</span><strong>{escape(item.get('last_check_in') or 'Never')}</strong></div><div class="health-row"><span>CPU / Memory / Disk</span><strong>{item.get('cpu',0)}% / {item.get('memory',0)}% / {item.get('disk',0)} GB</strong></div><div class="health-row"><span>Cameras</span><strong>{len(camera_status)}</strong></div><div class="health-row"><span>Restarts</span><strong>{item.get('restart_count',0)}</strong></div><div class="health-row"><span>Upload backlog</span><strong>{escape(backlog_text)}</strong></div><div class="mock-banner" {'' if warnings else 'hidden'}>{', '.join(warnings)}</div><div class="library-toolbar">{''.join(f'<button class="filter queue-command" data-appliance="{item["id"]}" data-command="{command}">{label}</button>' for command,label in [('restart_service','Restart service'),('refresh_cameras','Refresh cameras'),('run_diagnostics','Diagnostics'),('install_update','Install update'),('reboot_appliance','Reboot appliance'),('restart_vms','Restart VMS')])}</div><details><summary>Recent health history ({len(history)})</summary>{''.join(f'<p>{escape(h["created_at"])} · {escape(h["status"])} · CPU {h["cpu"]}%</p>' for h in history)}</details></article>''')
        command_rows=rows('SELECT c.*,a.cloud_id FROM appliance_commands c JOIN appliances a ON a.id=c.appliance_id ORDER BY c.created_at DESC LIMIT 50')
        command_table=''.join(f'<tr><td>{escape(item["cloud_id"])}</td><td>{escape(item["command"].replace("_"," "))}</td><td><span class="pill">{escape(item["status"])}</span></td><td>{escape(item["created_at"])}</td><td>{escape(item.get("error") or "")}</td></tr>' for item in command_rows) or '<tr><td colspan="5">No remote actions have been queued.</td></tr>'
        content=f'''<header class="topbar"><div><p class="eyebrow">Secure appliance fleet</p><h1>Appliance dashboard</h1></div></header><form class="panel clip-form" method="get"><label>Partner<input name="partner" value="{escape(partner,quote=True)}"></label><label>Customer<input name="customer" value="{escape(customer,quote=True)}"></label><label>Site<input name="site" value="{escape(site,quote=True)}"></label><label>Status<select name="status"><option value="">All</option>{''.join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ['online','degraded','offline','updating','revoked'])}</select></label><label>Version<input name="version" value="{escape(version,quote=True)}"></label><button class="action-button">Filter</button></form><div class="account-grid" style="margin-top:18px">{''.join(cards) or '<div class="empty">No appliances match these filters.</div>'}</div><section class="panel" style="margin-top:18px;overflow:auto"><h2>Remote action history</h2><table class="data-table"><thead><tr><th>Appliance</th><th>Command</th><th>Status</th><th>Created</th><th>Error</th></tr></thead><tbody>{command_table}</tbody></table></section>'''
        scripts='''<script>const DISRUPTIVE_COMMAND_WARNINGS={reboot_appliance:'This reboots the physical appliance. All cameras and recording will be briefly interrupted.',restart_vms:'This restarts the AnyAiCam VMS service on this appliance. Live view and recording will be briefly interrupted.'};document.querySelectorAll('.queue-command').forEach(button=>button.onclick=async()=>{const warning=DISRUPTIVE_COMMAND_WARNINGS[button.dataset.command];if(!confirm(warning?`${warning} Continue?`:`Queue ${button.textContent} for this appliance?`))return;const response=await fetch(`/api/partner/appliances/${button.dataset.appliance}/commands`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:button.dataset.command,confirmed:true})}),r=await response.json();showToast(r.message||r.detail)})</script>'''
        return shell('Appliance dashboard','appliances',content,scripts)
