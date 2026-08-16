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

from appliance_protocol import ALLOWED_COMMANDS, LIVE_RELAY_SESSION_DURATION_SECONDS, RateLimiter, cloud_settings, health_state, live_relay_s3_prefix, live_relay_session_name, live_relay_session_policy, sanitize_appliance_payload, validate_request_time
from edge.camera_compatibility import NOT_SUPPORTED, PARTIALLY_SUPPORTED, evaluate_scan_results
from live_manifest import LiveManifestStore
from partner_db import audit, connection, password_hash, row, rows, verify_password
from partner_portal import partner_identity, require_partner_access
from notification_engine import fanout_appliance_event
from updates_storage import UPDATES_PRESIGNED_TTL_SECONDS, ManifestNotFound, NotConfigured, UpdatesStorageError, get_latest_manifest, presign_package_url

try:
    import boto3
except ImportError:
    boto3 = None

logger=logging.getLogger('anyaicam.appliance')
request_limiter=RateLimiter(120,60); activation_limiter=RateLimiter(10,300)

# Phase 2 (docs/AI_HANDOFF.md §8): cloud-side control plane for the live-relay
# media path. Defaults to disabled -- with ANYAICAM_LIVE_RELAY_ENABLED unset,
# both new routes below refuse before ever touching boto3/STS. No appliance
# or frontend code calls these endpoints yet (Phases 3/4/6); they exist so
# Phase 2 can be built and tested in isolation, per the approved phase plan.
LIVE_RELAY_ENABLED=os.getenv('ANYAICAM_LIVE_RELAY_ENABLED','false').strip().lower()=='true'
LIVE_UPLOAD_ROLE_ARN=os.getenv('ANYAICAM_LIVE_UPLOAD_ROLE_ARN','').strip()
LIVE_RELAY_S3_BUCKET=os.getenv('ANYAICAM_S3_BUCKET','').strip()
LIVE_RELAY_AWS_REGION=os.getenv('AWS_REGION',os.getenv('AWS_DEFAULT_REGION','')).strip()
live_manifest_store=LiveManifestStore(Path(os.getenv('ANYAICAM_LIVE_MANIFEST_FILE','/app/recordings/live_manifest.json')))

# RDM-2 Group 2D (docs/AI_HANDOFF.md): cloud-side post-restart update-result
# reporting. Defaults to disabled, same fail-closed-by-default convention as
# LIVE_RELAY_ENABLED above -- gates ONLY the new update_result() route below.
# It does NOT gate the already-shipped Group 2C install_update dispatch path
# (queue_command()/appliance-side command handling); narrowing that path was
# explicitly out of scope for this group.
REMOTE_UPDATE_ENABLED=os.getenv('ANYAICAM_REMOTE_UPDATE_ENABLED','false').strip().lower()=='true'

# The full set of UpdateState values UpdateStateMachine.resume_if_pending()
# can actually conclude with (appliance-agent/anyaicam_agent/updater/
# state_machine.py's own docstring) -- broader than just the three terminal
# outcomes, since a rollback triggered mid-resume returns an in-progress
# 'restarting' result awaiting its OWN next restart. 'activation_failed' is
# terminal on the device side (in updater.models.TERMINAL_STATES); 'restarting'
# is not -- this cloud-side distinction is used below to detect a conflicting
# SECOND, different terminal report for the same (appliance_id,update_id).
#
# RDM-2 Group 2E corrective addition: 'restart_failed' -- a rollback
# triggered DURING resume_if_pending() whose OWN restart_signal() call
# then itself raises. The pointer flip to the good version already
# durably succeeded; only the restart signal failed. This is a real,
# reachable resume_if_pending() outcome that Group 2D's original design
# missed. Like 'restarting', it is accepted but NOT terminal here --
# device-side, RESTART_FAILED is deliberately absent from
# updater.models.TERMINAL_STATES (see state_machine.py's own docstring)
# specifically so a LATER real restart can still reconcile the same
# update_id to an actual conclusion (rolled_back/rollback_failed). Adding
# it to _TERMINAL_UPDATE_RESULT_STATES instead would incorrectly reject
# that later, legitimate report as a 409 conflict.
_ACCEPTED_UPDATE_RESULT_STATES={'healthy','rolled_back','rollback_failed','activation_failed','restarting','restart_failed'}
_TERMINAL_UPDATE_RESULT_STATES={'healthy','rolled_back','rollback_failed','activation_failed'}

# RDM-2 Group 2G (docs/AI_HANDOFF.md): cloud-side authenticated manifest
# endpoint -- the real UpdateSourceProvider backend. Same fail-closed-by-
# default convention as LIVE_RELAY_ENABLED/REMOTE_UPDATE_ENABLED above --
# gates ONLY the new updates_latest() route below. S3 is the sole source
# of truth for what is currently published (see updates_storage.py) --
# there is no local database table backing this.
UPDATES_SOURCE_ENABLED=os.getenv('ANYAICAM_UPDATES_SOURCE_ENABLED','false').strip().lower()=='true'


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

    @app.post('/api/appliance/live/{camera_id}/session')
    def live_relay_session(request: Request,camera_id: str) -> dict:
        # docs/AI_HANDOFF.md §8 Phase 2: issues a short-lived, camera-scoped S3 upload
        # credential. FastAPI never receives a media byte through this endpoint -- it
        # only authenticates, authorizes, and brokers an STS credential.
        appliance=authenticate_appliance(request)
        if not LIVE_RELAY_ENABLED: raise HTTPException(status_code=404,detail='Live relay is not enabled.')
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

    @app.post('/api/appliance/live/{camera_id}/segment-available')
    def live_relay_segment_available(request: Request,camera_id: str,payload: dict) -> dict:
        # docs/AI_HANDOFF.md §8 Phase 2: tiny JSON bookkeeping only -- the segment
        # itself was already PutObject'd directly to S3 by the appliance using the
        # credential from live_relay_session() above; this call never carries media.
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
        status=str(payload.get('status','running')); progress=max(0,min(100,int(payload.get('progress',0)))); results=sanitize_appliance_payload(payload.get('results',[]))
        if status not in {'running','complete','error'}: raise HTTPException(status_code=400,detail='Unsupported discovery status.')
        # Camera-compatibility extension (docs/AI_HANDOFF.md): evaluate each
        # discovered device's compatibility only *after* sanitize_appliance_payload()
        # above has already stripped credentials -- the compatibility engine never
        # sees a username/password/rtsp_url, and never does its own network I/O.
        if status=='complete': results=evaluate_scan_results(results)
        with connection() as db:
            db.execute('UPDATE camera_scan_jobs SET status=?,progress=?,results_json=?,message=?,updated_at=? WHERE id=?',(status,progress,json.dumps(results),str(payload.get('message','Discovery update received.'))[:500],datetime.now().isoformat(),job_id))
            if status=='complete':
                for index,item in enumerate(results,1):
                    compatibility_status=item.get('compatibility_status')
                    if compatibility_status==NOT_SUPPORTED: continue  # never onboarded -- see camera_compatibility.py
                    camera_status='needs_review' if compatibility_status==PARTIALLY_SUPPORTED else 'discovered'
                    db.execute('INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(str(item.get('id') or secrets.token_hex(5)),job['customer_id'],appliance['site_id'],appliance['id'],item.get('name') or f'Discovered Camera {index}',item.get('resolution','2mp'),camera_status,datetime.now().isoformat()))
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

    @app.post('/api/appliance/updates/{update_id}/result')
    def update_result(request: Request,update_id: str,payload: dict) -> dict:
        # RDM-2 Group 2D (docs/AI_HANDOFF.md): durable post-restart outcome
        # reporting for UpdateStateMachine.resume_if_pending() conclusions --
        # a DEDICATED, update_id-keyed endpoint, deliberately not a reuse of
        # the generic command_result() channel above (that one is keyed by
        # the CLOUD's own one-shot dispatch id and cannot represent an
        # update_id that legitimately reports more than once across a
        # rollback's own restart cycle). Ownership is structural, not an
        # explicit check: every read/write below is scoped to the
        # AUTHENTICATED appliance['id'], never to any caller-supplied
        # identifier, so an appliance can never affect another appliance's
        # history even if it guesses another appliance's update_id string.
        appliance=authenticate_appliance(request)
        if not REMOTE_UPDATE_ENABLED: raise HTTPException(status_code=404,detail='Remote update reporting is not enabled.')
        if not isinstance(payload,dict): raise HTTPException(status_code=400,detail='Update result payload must be a JSON object.')
        body_update_id=payload.get('update_id')
        if not isinstance(body_update_id,str) or not body_update_id or body_update_id!=update_id:
            raise HTTPException(status_code=400,detail='update_id in the request body must be a non-empty string matching the URL.')
        state=payload.get('state')
        if state not in _ACCEPTED_UPDATE_RESULT_STATES: raise HTTPException(status_code=400,detail='Unsupported update state.')
        from_version=payload.get('from_version'); to_version=payload.get('to_version')
        if not isinstance(from_version,str) or not isinstance(to_version,str):
            raise HTTPException(status_code=400,detail='from_version and to_version are required strings.')
        duration=payload.get('duration_seconds')
        if isinstance(duration,bool) or not isinstance(duration,(int,float)) or duration<0:
            raise HTTPException(status_code=400,detail='duration_seconds must be a non-negative number.')
        rollback_from=payload.get('rollback_from')
        if rollback_from is not None and not isinstance(rollback_from,str):
            raise HTTPException(status_code=400,detail='rollback_from must be a string or null.')
        error=str(payload.get('error','') or '')[:500]; duration=float(duration); now=datetime.now().isoformat()

        existing=rows('SELECT * FROM appliance_update_history WHERE appliance_id=? AND update_id=?',(appliance['id'],update_id))
        same_state=next((item for item in existing if item['state']==state),None)
        if same_state is not None:
            if (same_state['from_version'],same_state['to_version'],same_state['error'],same_state['rollback_from'],float(same_state['duration_seconds']))==(from_version,to_version,error,rollback_from,duration):
                return {'status':'accepted','duplicate':True}
            raise HTTPException(status_code=409,detail='A different result was already recorded for this update_id and state.')
        if any(item['state'] in _TERMINAL_UPDATE_RESULT_STATES for item in existing):
            raise HTTPException(status_code=409,detail='This update_id already has a terminal result on record.')

        with connection() as db:
            db.execute('INSERT INTO appliance_update_history(appliance_id,update_id,from_version,to_version,state,error,rollback_from,duration_seconds,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(appliance['id'],update_id,from_version,to_version,state,error,rollback_from,duration,now))
        audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.update_result_recorded','appliance_update_history',update_id,{'state':state})
        return {'status':'accepted'}

    @app.get('/api/appliance/updates/latest')
    def updates_latest(request: Request,target: str,channel: str) -> dict:
        # RDM-2 Group 2G (docs/AI_HANDOFF.md): the authenticated manifest
        # endpoint backing the real UpdateSourceProvider (appliance-agent's
        # updater/s3_source.py). S3 is the sole source of truth -- this
        # reads manifests/{target}/{channel}/latest.json directly via
        # updates_storage.py, with NO local database involvement at all
        # (no published_updates table exists). The appliance receives
        # only a presigned S3 GET URL for the package here -- never an
        # AWS credential of any kind. target/channel are content
        # selectors, not per-appliance secrets: any authenticated
        # appliance asking for the same target/channel gets the same
        # published content every other one would.
        authenticate_appliance(request)
        if not UPDATES_SOURCE_ENABLED: raise HTTPException(status_code=404,detail='Remote update source is not enabled.')
        try:
            envelope=get_latest_manifest(target,channel)
        except ManifestNotFound:
            return {'status':'no_update_available'}
        except NotConfigured as error:
            raise HTTPException(status_code=503,detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error
        except UpdatesStorageError as error:
            raise HTTPException(status_code=502,detail=f'Could not read the published manifest: {error}') from error
        manifest=envelope.get('manifest'); signature=envelope.get('signature')
        if not isinstance(manifest,dict) or not isinstance(signature,str):
            raise HTTPException(status_code=502,detail='Published manifest envelope is malformed.')
        version=manifest.get('version')
        if not isinstance(version,str) or not version:
            raise HTTPException(status_code=502,detail='Published manifest is missing a version.')
        try:
            package_url=presign_package_url(target,channel,version)
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error
        except UpdatesStorageError as error:
            raise HTTPException(status_code=502,detail=f'Could not presign the package URL: {error}') from error
        expires_at=(datetime.now()+timedelta(seconds=UPDATES_PRESIGNED_TTL_SECONDS)).isoformat()
        return {'manifest':manifest,'signature':signature,'package_url':package_url,'expires_at':expires_at}

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
