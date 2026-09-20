from cloud_config import settings as cloud_settings
import base64
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
from object_storage import get_storage
from partner_db import audit, authorize_appliance_tenant, connection, password_hash, row, rows, verify_password
from partner_portal import partner_identity, require_partner_access
from notification_engine import fanout_appliance_event
from recording_credentials import RECORDING_SESSION_DURATION_SECONDS, event_media_session_policy, recording_s3_prefix, recording_session_name, recording_session_policy
from event_media_policy import allows_event_media

try:
    import boto3
except ImportError:
    boto3 = None

logger=logging.getLogger('anyaicam.appliance')
request_limiter=RateLimiter(120,60); activation_limiter=RateLimiter(10,300)
# Live Relay per-camera rate limit (2026-09-13): confirmed live under real
# 5-camera concurrent load that /live/{camera_id}/segment-available's own
# legitimate traffic (~30 requests/minute per actively-relayed camera --
# one per 2-second HLS segment) already exceeds the shared request_limiter's
# 120/60 budget on its own math at just 5 cameras, and would be roughly 2x
# over budget at the current 8-camera Starter entitlement -- worse at any
# larger future tier. Deliberately NOT a second appliance-wide ceiling sized
# for one particular tier (that just relocates the same "wrong number for
# some fleet size" problem) -- keyed by camera_id instead of appliance_id,
# so the effective allowed aggregate for an appliance scales automatically
# with however many cameras it actually has actively relaying (N cameras x
# 60/min), always ~2x the ~30/min legitimate cadence at any fleet size,
# while still bounding a single malfunctioning camera's own runaway traffic
# independently of every other camera and of the appliance's own unrelated
# control-plane traffic (heartbeat/commands/configuration/etc., which stay
# on the original, completely unchanged request_limiter).
live_relay_camera_limiter=RateLimiter(60,60)
LIVE_RELAY_ENABLED=os.getenv('ANYAICAM_LIVE_RELAY_ENABLED','false').strip().lower()=='true'
LIVE_UPLOAD_ROLE_ARN=os.getenv('ANYAICAM_LIVE_UPLOAD_ROLE_ARN','').strip()
LIVE_RELAY_S3_BUCKET=os.getenv('ANYAICAM_S3_BUCKET','').strip()
LIVE_RELAY_AWS_REGION=os.getenv('AWS_REGION',os.getenv('AWS_DEFAULT_REGION','')).strip()
live_manifest_store=LiveManifestStore(Path(os.getenv('ANYAICAM_LIVE_MANIFEST_FILE','/app/recordings/live_manifest.json')))
# R1 (recording-pipeline roadmap): independent flag/role/bucket from live
# relay, deliberately not defaulted to the live bucket/role -- see
# docs/r1-recording-iam.md (its "designed, not yet applied" framing is
# stale -- the role/bucket are real and already applied; see
# recording_credentials.py's own module docstring). If none of these
# three are configured, recording_upload_credentials() below still
# fails closed with 503 regardless of which flag authorized the request.
RECORDING_UPLOAD_ENABLED=os.getenv('ANYAICAM_RECORDING_UPLOAD_ENABLED','false').strip().lower()=='true'
RECORDING_UPLOAD_ROLE_ARN=os.getenv('ANYAICAM_RECORDING_UPLOAD_ROLE_ARN','').strip()
RECORDING_S3_BUCKET=os.getenv('ANYAICAM_RECORDING_S3_BUCKET','').strip()
RECORDING_AWS_REGION=os.getenv('AWS_REGION',os.getenv('AWS_DEFAULT_REGION','')).strip()
# Analytics-event sync (separate milestone, separate flag from recording
# upload -- deliberately independently toggleable). No AWS/STS involved at
# all; this only ever writes to the detection_events SQL table.
ANALYTICS_SYNC_ENABLED=os.getenv('ANYAICAM_ANALYTICS_SYNC_ENABLED','false').strip().lower()=='true'
# 2026-09-13: event-media (motion-event thumbnail/clip) upload needs the
# same short-lived S3 credentials recording_upload_credentials() below
# already issues, but must never require the much bigger bulk/continuous
# recording-upload feature (RECORDING_UPLOAD_ENABLED) to be turned on --
# that flag also starts a real, continuous, unrelated appliance-side
# upload worker for every completed recording on every camera. This is
# its own independent read of the same edge-side env var name
# (ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED already governs
# event_media_uploader.py's own edge-side gate) so the credentials route
# can be authorized by EITHER feature without coupling them.
EVENT_MEDIA_UPLOAD_ENABLED=os.getenv('ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED','false').strip().lower()=='true'
# 2026-09-13 (Phase 2, Historical Playback validation): a controlled,
# explicit, camera_id-keyed allowlist -- unset/empty (the default) means
# no pilot cameras and zero behavior change for every existing caller,
# same convention as analytics_sync.SYNC_CAMERA_SCOPE and
# recording_uploader.RECORDING_UPLOAD_CAMERA_SCOPE. Lets exactly one
# already-approved camera exercise the real bulk-recording credential
# scope and catalog route (recording_upload_credentials()/
# recording_available() below) without turning on RECORDING_UPLOAD_
# ENABLED globally, which would also activate those same two routes for
# every other appliance/customer in the system. Deliberately does NOT
# touch cameras.cloud_recording_mode -- that column is a single value
# per camera already meaning 'motion' for this same camera's proven
# event-media entitlement; reusing it for this would silently break
# that pipeline, which is exactly why this is a separate mechanism.
RECORDING_UPLOAD_PILOT_CAMERAS: frozenset[str] = frozenset(
    item.strip() for item in os.getenv('ANYAICAM_RECORDING_UPLOAD_PILOT_CAMERAS','').split(',') if item.strip()
)
# AAC (facial recognition), Phase 2: gates GET /api/appliance/facial-directory
# below -- the cloud side of facial_embedding_sync.py's edge-pull worker.
# Independently toggleable from ANALYTICS_SYNC_ENABLED (that flag is for the
# edge->cloud event direction; this one is cloud->edge enrollment data).
FACIAL_EMBEDDING_SYNC_ENABLED=os.getenv('ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED','false').strip().lower()=='true'


def _bearer(request: Request) -> str:
    return request.headers.get('authorization','').removeprefix('Bearer ').strip()


def authenticate_appliance(request: Request, *, limiter: "RateLimiter | None" = request_limiter) -> dict:
    # `limiter` defaults to the module-level request_limiter -- every
    # existing call site (all ~21 of them) calls authenticate_appliance(
    # request) with no argument and is completely unaffected by this
    # parameter's existence. Only live_relay_session()/
    # live_relay_segment_available() pass limiter=None, to skip this
    # appliance-wide check entirely for those two routes -- their own
    # traffic is rate-limited per-camera instead (live_relay_camera_limiter,
    # checked after camera-ownership authorization, inside each of those
    # two routes) precisely so high-volume, legitimate live-relay traffic
    # can never crowd out or be crowded out by the same appliance's
    # heartbeat/commands/configuration budget. limiter=None never skips
    # identity, credential, timestamp, or replay-nonce verification below --
    # only this one rate-limit check.
    appliance_id=request.headers.get('x-appliance-id','').strip(); timestamp=request.headers.get('x-request-timestamp',''); nonce=request.headers.get('x-request-nonce','').strip(); credential=_bearer(request)
    if not appliance_id or not timestamp or len(nonce)<16 or not credential: raise HTTPException(status_code=401,detail='Appliance authentication headers are required.')
    try: request_timestamp=int(timestamp)
    except ValueError as error: raise HTTPException(status_code=401,detail='Invalid request timestamp.') from error
    if not validate_request_time(request_timestamp): raise HTTPException(status_code=401,detail='Request timestamp is outside the allowed window.')
    if limiter is not None and not limiter.allow(appliance_id): raise HTTPException(status_code=429,detail='Appliance request rate exceeded.')
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


def _resolve_parent_motion_event(db,camera_id: str,appliance_id: str,parent_local_event_id: str) -> str | None:
    """Resolves a submitted LOCAL parent id (an appliance's own
    local_event_id, never a cloud id) to this exact appliance's own
    cloud detection_events.id, scoped to the SAME camera and the SAME
    authenticated appliance, and requiring the parent's own stored
    event_type to be 'motion' -- never another smart_motion event, and
    never anything on a different camera/appliance/customer/site (the
    camera_id+appliance_id pair already IS that ownership boundary,
    since a camera's own row can only ever belong to one appliance at a
    time). Returns None (never raises) when the parent hasn't synced
    yet, belongs to a different camera or appliance, or isn't a real
    Motion event -- ingestion of the child event itself must never fail
    just because its parent isn't resolvable yet; the child simply
    stays unresolved (and therefore permanently ineligible for the
    shared-media route) until a later resync succeeds."""
    parent=db.execute(
        'SELECT id FROM detection_events WHERE camera_id=? AND appliance_id=? AND local_event_id=? AND event_type=?',
        (camera_id,appliance_id,parent_local_event_id,'motion'),
    ).fetchone()
    return parent['id'] if parent else None


def register_appliance_cloud_routes(app: FastAPI,shell: Callable,current_user: Callable[[Request],dict] | None=None) -> None:
    @app.get('/api/appliance/config')
    def appliance_config() -> dict:
        settings=cloud_settings(); return {'mode':settings['mode'],'base_url':settings['base_url'],'mock_cloud':settings['mock'],'timestamp_window_seconds':300,'camera_credentials_allowed':False}

    @app.post('/api/appliance/activate')
    def activate_appliance(request: Request,payload: dict) -> dict:
        client=request.client.host if request.client else 'unknown'
        if not activation_limiter.allow(client): raise HTTPException(status_code=429,detail='Activation attempt rate exceeded.')
        cloud_id=str(payload.get('cloud_id','')).strip().upper(); token=str(payload.get('activation_token','')).strip(); appliance=row('SELECT * FROM appliances WHERE cloud_id=?',(cloud_id,))
        if not appliance or not token: raise HTTPException(status_code=403,detail='Invalid activation request.')
        # Conflict check before the token is touched or a credential is minted -- see appliance_activation.py.
        # Only meaningful for a genuinely single-tenant process (RUNTIME_
        # ROLE edge/combined) -- see local_activation_tracking_applies()'s
        # own docstring for why a cloud deployment (many independent
        # appliances, one shared process) must skip this entirely rather
        # than let one appliance's local identity block every other
        # device's activation.
        from appliance_activation import ActivationConflict, load_persisted_identity, local_activation_tracking_applies
        if local_activation_tracking_applies():
            existing_identity=load_persisted_identity()
            if existing_identity and existing_identity['cloud_id']!=cloud_id:
                raise HTTPException(status_code=409,detail=f"This appliance is already activated as {existing_identity['cloud_id']!r}. Reset the local activation identity before activating as a different appliance.")
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
        # Durable persistence (gap #1) -- see appliance_activation.py. Can still raise on a genuine race (two activations at once). Skipped entirely for a cloud deployment -- see the conflict-check comment above.
        if local_activation_tracking_applies():
            try:
                from appliance_activation import persist_activation
                persist_activation(appliance_id=appliance['id'],cloud_id=cloud_id,credential=credential,customer_id=appliance['customer_id'],site_id=appliance['site_id'],partner_id=appliance.get('partner_id'))
            except ActivationConflict as error:
                raise HTTPException(status_code=409,detail=str(error)) from error
        audit({'email':cloud_id,'role':'appliance'},'appliance.activated','appliance',appliance['id']); logger.info('Appliance activated cloud_id=%s',cloud_id)
        return {'appliance_id':appliance['id'],'cloud_id':cloud_id,'credential':credential,'credential_id':credential_id,'partner_id':appliance.get('partner_id'),'customer_id':appliance['customer_id'],'site_id':appliance['site_id'],'message':'Store this permanent credential securely; it will not be shown again.'}

    @app.post('/api/appliance/heartbeat')
    def heartbeat(request: Request,payload: dict) -> dict:
        appliance=authenticate_appliance(request); safe=sanitize_appliance_payload(payload); state,warnings=health_state(safe); now=datetime.now().isoformat()
        # Identity contract gap #2: the heartbeat ACK carries the
        # current manifest_version (appliance_identity.
        # current_manifest_version()) so an appliance can detect
        # staleness with one integer comparison, no full manifest fetch
        # needed on every cycle. Only on an actual mismatch -- the
        # appliance reports what it's cached via cached_manifest_version,
        # deliberately optional so a heartbeat payload that omits it
        # (an appliance that's never cached anything, or an older
        # payload shape) always triggers a refresh rather than silently
        # skipping one -- do we do the heavier work of reconciling this
        # appliance's own local sessions against a freshly rebuilt
        # manifest. No separate polling loop: this reuses heartbeat's
        # own existing, already-regular cadence instead of adding one.
        #
        # 2026-09-04, Option B: reconcile_sessions_against_manifest() is
        # called again here -- Option A (shipped first, same day, as the
        # immediate mitigation) had disabled this call entirely because
        # its underlying query had no scoping at all and was force-
        # revoking essentially every unrelated customer's own cloud
        # session platform-wide on every heartbeat (see that function's
        # own docstring, and appliance_identity.
        # _candidate_session_user_ids_for_appliance(), for the fix).
        # Re-enabling it here is now safe.
        from appliance_identity import build_manifest, current_manifest_version, reconcile_sessions_against_manifest, should_refresh_manifest
        cached_manifest_version=payload.get('cached_manifest_version')
        manifest_refreshed=False; revoked_session_count=0
        with connection() as db:
            live_version=current_manifest_version(db,cloud_id=appliance['cloud_id'])
            if should_refresh_manifest(cached_manifest_version,live_version):
                manifest=build_manifest(db,cloud_id=appliance['cloud_id'])
                revoked=reconcile_sessions_against_manifest(db,manifest=manifest)
                manifest_refreshed=True; revoked_session_count=len(revoked)
        if revoked_session_count: audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.sessions_revoked_on_manifest_refresh','appliance',appliance['id'],{'session_count':revoked_session_count,'trigger':'heartbeat'})
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
            # storage_state/storage_free_percent/storage_last_cleanup_at
            # (2026-09-17, local recording storage management): reported
            # only when the appliance's own local_storage_manager.py
            # worker is actually enabled and has written its cross-
            # process state file (see that module's docstring) --
            # metrics.py includes these keys only when present, so an
            # appliance that hasn't enabled this feature simply never
            # sends them and these columns stay NULL, distinct from a
            # real 'healthy' report.
            storage_state=safe.get('storage_state')
            storage_state=str(storage_state)[:20] if storage_state in ('healthy','warning','cleanup_active','critical') else None
            storage_free_percent=safe.get('storage_free_percent')
            storage_free_percent=float(storage_free_percent) if isinstance(storage_free_percent,(int,float)) else None
            storage_last_cleanup_at=safe.get('storage_last_cleanup_at')
            storage_last_cleanup_at=str(storage_last_cleanup_at)[:40] if isinstance(storage_last_cleanup_at,str) else None
            # wireguard_status/wireguard_last_handshake_at (docs/wireguard-
            # remote-connectivity-plan.md Sec 14): same optional-field,
            # COALESCE-on-omission shape as storage_state directly above --
            # an appliance that has never enrolled a WireGuard identity
            # (the overwhelming majority today, since Phase B/C/D have not
            # shipped) simply never sends these keys, and this UPDATE
            # leaves the columns exactly as they were (NULL, initially).
            wireguard_status=safe.get('wireguard_status')
            wireguard_status=str(wireguard_status)[:20] if wireguard_status in ('disabled','enrolling','enrolled','active','degraded','failed') else None
            wireguard_last_handshake_at=safe.get('wireguard_last_handshake_at')
            wireguard_last_handshake_at=str(wireguard_last_handshake_at)[:40] if isinstance(wireguard_last_handshake_at,str) else None
            db.execute('UPDATE appliances SET state=?,online_status=?,last_check_in=?,software_version=?,uptime_seconds=?,cpu=?,memory=?,disk_capacity=?,disk=?,recording_used=?,last_error=?,camera_capacity=?,storage_state=COALESCE(?,storage_state),storage_free_percent=COALESCE(?,storage_free_percent),storage_last_cleanup_at=COALESCE(?,storage_last_cleanup_at),wireguard_status=COALESCE(?,wireguard_status),wireguard_last_handshake_at=COALESCE(?,wireguard_last_handshake_at) WHERE id=?',(state,state,now,safe.get('software_version','Unknown'),new_uptime,float(safe.get('cpu',0)),float(safe.get('memory',0)),float(safe.get('disk_capacity',0)),float(safe.get('disk_used',0)),float(safe.get('recording_used',0)),safe.get('last_error'),int(safe.get('camera_count',0)),storage_state,storage_free_percent,storage_last_cleanup_at,wireguard_status,wireguard_last_handshake_at,appliance['id']))
            db.execute('INSERT INTO appliance_health_history(appliance_id,status,cpu,memory,disk_capacity,disk_used,recording_used,uptime_seconds,camera_count,last_error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(appliance['id'],state,safe.get('cpu',0),safe.get('memory',0),safe.get('disk_capacity',0),safe.get('disk_used',0),safe.get('recording_used',0),safe.get('uptime_seconds',0),safe.get('camera_count',0),safe.get('last_error'),now))
        return {'status':'accepted','state':state,'warnings':warnings,'restarted':restarted,'server_time':int(time.time()),'current_manifest_version':live_version,'manifest_refreshed':manifest_refreshed}

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
        # smart_motion_enabled/lpr_enabled/ppe_enabled (2026-09-16) join
        # people_counting_enabled here for the same reason: this is the
        # one route recording_uploader._refresh_camera_map() polls to
        # build the in-memory map every real per-camera entitlement read
        # in the appliance (lpr.is_camera_enabled(), ppe.is_camera_enabled(),
        # smart_motion's own caller in main.py, people_counting_worker())
        # ultimately consults -- omitting a column here is exactly the gap
        # that left people_counting_enabled unreachable in practice.
        # local_recording_mode (and its 4 configurable fields) joins the
        # same exposure list for the same reason -- this is the one route
        # the appliance's own edge_camera_sync.py polls to bring cloud-set
        # per-camera config down into the LOCAL cameras table
        # process_supervisor() actually reads before deciding which
        # recording strategy to run for a camera. Omitting it here would
        # be the exact same unreachable-in-practice gap this comment
        # already documents for people_counting_enabled.
        appliance=authenticate_appliance(request); camera_items=rows('SELECT id,name,site_id,resolution,status,camera_number,device_key,onvif_endpoint,cloud_recording_mode AS recording_mode,local_recording_mode,local_recording_pre_roll_seconds,local_recording_post_roll_seconds,local_recording_merge_gap_seconds,local_recording_max_event_seconds,people_counting_enabled,smart_motion_enabled,lpr_enabled,ppe_enabled,talk_down_supported,talk_down_metadata FROM cameras WHERE appliance_id=? ORDER BY camera_number,name',(appliance['id'],))
        for item in camera_items:
            raw_metadata=item.pop('talk_down_metadata',None)
            supported=item.pop('talk_down_supported',None)
            if supported is None:
                item['talk_down']=None
            else:
                metadata=None
                if raw_metadata:
                    try:
                        metadata=json.loads(raw_metadata)
                    except (TypeError, ValueError):
                        metadata=None
                item['talk_down']={'supported':bool(supported),'metadata':metadata}
        # cloud_policy (2026-09-16): the real, effective Hybrid cloud-
        # cost policy for THIS appliance's own customer -- an RDM-set
        # override (customer_cloud_policy, POST /api/admin/customers/
        # {id}/cloud-policy above) or the system default, resolved
        # exactly once per poll via event_media_policy.cloud_policy_for_
        # customer(), the same function the cloud-side event-media gate
        # itself uses, so both halves always agree. Top-level, not
        # per-camera: the 6-hour allowance and retention window are
        # both customer-wide entitlements, even though the allowance is
        # enforced per camera on the appliance (each camera gets its
        # own independent 6 hours, not a shared pool). This is the
        # single sync channel recording_uploader.py's own periodic
        # config refresh reads on the edge -- no second, parallel
        # config-delivery mechanism.
        from event_media_policy import cloud_policy_for_customer
        # storage_policy (2026-09-17): same sync channel and the same
        # "RDM override, else system default, resolved once per poll"
        # shape as cloud_policy immediately above -- local_storage_
        # manager.py's own periodic config refresh reads this exact
        # field, mirroring recording_uploader.py's established pattern
        # for cloud_policy.
        from local_storage_policy import local_storage_policy_for_customer
        with connection() as db:
            cloud_policy=cloud_policy_for_customer(db,appliance['customer_id'])
            storage_policy=local_storage_policy_for_customer(db,appliance['customer_id'])
            # identity (2026-09-20): the appliance's own customer/site/
            # partner rows, real records from THIS cloud database -- not
            # fabricated -- so edge_camera_sync.py can materialize valid
            # local parent rows for the same foreign keys it already
            # writes into every camera row (identity['customer_id']/
            # ['site_id']/['appliance_id'], sourced from this exact
            # appliance's own load_persisted_identity()). Confirmed live
            # on Ryzen: without a local customers/sites/appliances row,
            # any INSERT into the local recordings table (cataloging a
            # newly-discovered recording file for cloud upload) 500s with
            # sqlite3.IntegrityError -- affecting every camera on the
            # appliance, Event-mode and Continuous-mode alike, not
            # anything specific to this feature. Selected columns are
            # exactly what the local schema's NOT NULL constraints
            # require (see partner_db.py's customers/sites/appliances/
            # partners table definitions) -- nothing more.
            customer_row=row('SELECT id,partner_id,name,company,email,phone,status,trial_status,billing_status FROM customers WHERE id=?',(appliance['customer_id'],))
            site_row=row('SELECT id,customer_id,name,address,site_type FROM sites WHERE id=?',(appliance['site_id'],))
            partner_row=row('SELECT id,name,approval_status FROM partners WHERE id=?',(appliance['partner_id'],)) if appliance.get('partner_id') else None
            identity={
                'customer':dict(customer_row) if customer_row else None,
                'site':dict(site_row) if site_row else None,
                'partner':dict(partner_row) if partner_row else None,
                'appliance':{'id':appliance['id'],'customer_id':appliance['customer_id'],'site_id':appliance['site_id'],'cloud_id':appliance['cloud_id'],'partner_id':appliance.get('partner_id')},
            }
        return {'configuration_version':max([item.get('status','') for item in camera_items],default='empty'),'cameras':camera_items,'camera_credentials_included':False,'cloud_policy':cloud_policy,'storage_policy':storage_policy,'identity':identity}

    def _sanitize_rtsp_uri(value: str) -> str | None:
        # Second, independent layer of defense against a credential-
        # bearing URI reaching storage -- the appliance-side ONVIF
        # resolver (anyaicam_agent/onvif_media.py) already strips
        # embedded userinfo before submitting here, but this endpoint
        # never trusts that alone, matching this codebase's existing
        # defense-in-depth precedent (PortalClient.request() stripping
        # credential-shaped keys as a second layer -- see provisioning.py).
        if not isinstance(value,str) or not value.strip(): return None
        from urllib.parse import urlsplit, urlunsplit
        try: parsed=urlsplit(value.strip())
        except ValueError: return None
        if parsed.scheme.lower()!='rtsp' or not parsed.hostname: return None
        netloc=parsed.hostname+(f':{parsed.port}' if parsed.port else '')
        return urlunsplit((parsed.scheme,netloc,parsed.path,parsed.query,parsed.fragment))

    @app.post('/api/appliance/{cloud_id}/cameras/{camera_id}/media-uri')
    def appliance_submit_media_uri(request: Request,cloud_id: str,camera_id: str,payload: dict) -> dict:
        # Persists the read-only ONVIF GetProfiles/GetStreamUri result
        # (anyaicam_agent/onvif_media.py -- appliance-side only, this
        # agent has direct LAN reachability to the camera) into
        # cameras.onvif_endpoint, the exact field app/main.py's
        # _provisioned_camera_stream() already reads for camera_url().
        # Idempotent by construction: an already-populated onvif_endpoint
        # is never overwritten here -- reprovisioning (which clears the
        # camera row / device_key association) is the only path that
        # can make this field eligible for a fresh resolution again.
        appliance=authenticate_appliance(request)
        if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403,detail='Cloud ID does not match authenticated appliance.')
        camera=_authorized_camera(appliance,camera_id)
        device_key=str(payload.get('device_key') or '').strip()
        if not device_key or device_key!=(camera.get('device_key') or ''):
            raise HTTPException(status_code=409,detail='device_key does not match this camera; refusing to associate the result.')
        rtsp_uri=_sanitize_rtsp_uri(str(payload.get('rtsp_uri') or ''))
        if not rtsp_uri:
            raise HTTPException(status_code=400,detail='A valid rtsp:// media URI is required.')
        updated=False
        if not camera.get('onvif_endpoint'):
            with connection() as db:
                changed=db.execute('UPDATE cameras SET onvif_endpoint=? WHERE id=? AND appliance_id=? AND (onvif_endpoint IS NULL OR onvif_endpoint=?)',(rtsp_uri,camera_id,appliance['id'],'')).rowcount
                updated=bool(changed)
            if updated:
                audit({'email':appliance['cloud_id'],'role':'appliance'},'camera.media_uri_resolved','camera',camera_id,{'device_key':device_key})
        return {'message':'Media URI recorded.' if updated else 'Camera already has a media URI; not replaced.','updated':updated}

    @app.get('/api/appliance/signing-keys')
    def identity_signing_keys() -> dict:
        # Public keys only -- deliberately unauthenticated, matching how
        # any public-key distribution endpoint works. An appliance calls
        # this once after activation and again whenever it sees an
        # unrecognized key_id, so key rotation never requires
        # re-activation. See appliance_identity.py's module docstring.
        from appliance_identity import get_cloud_identity_backend
        return {'keys': get_cloud_identity_backend().public_keys()}

    @app.get('/api/appliance/{cloud_id}/identity-manifest')
    def identity_manifest(request: Request,cloud_id: str) -> dict:
        appliance=authenticate_appliance(request)
        if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403,detail='Cloud ID does not match authenticated appliance.')
        from appliance_identity import get_cloud_identity_backend, reconcile_sessions_against_manifest
        manifest=get_cloud_identity_backend().fetch_manifest(cloud_id=appliance['cloud_id'])
        # Reconciliation runs on every fetch, not on a separate schedule --
        # in this mock/dev setup the 'cloud' database the manifest was
        # just built from and the local session table live in the same
        # place, so this is the one real, already-wired trigger point.
        # See appliance_identity.reconcile_sessions_against_manifest()'s
        # own docstring for what still needs a genuine periodic caller
        # once the appliance and cloud are actually separate processes.
        #
        # 2026-09-04, Option B: re-enabled -- see the matching comment in
        # heartbeat() above. Properly scoped now via appliance_identity.
        # _candidate_session_user_ids_for_appliance().
        with connection() as db: revoked=reconcile_sessions_against_manifest(db,manifest=manifest)
        if revoked: audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.sessions_revoked_on_manifest_refresh','appliance',appliance['id'],{'session_count':len(revoked)})
        audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.identity_manifest_fetched','appliance',appliance['id'],{'identity_count':len(manifest['identities']),'manifest_version':manifest['manifest_version']})
        return manifest

    @app.post('/api/appliance/{cloud_id}/authenticate-operator')
    def authenticate_operator_route(request: Request,cloud_id: str,payload: dict) -> dict:
        # Cloud-delegated login (design doc §3): the appliance forwards a
        # browser's email/password/portal here using its OWN credential --
        # the browser's password is never persisted locally, only carried
        # through in this one request. A correct password alone is never
        # sufficient; authenticate_operator() also requires a grant that
        # resolves to this specific appliance (§2) -- same-partner_id is
        # never enough on its own.
        appliance=authenticate_appliance(request)
        if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403,detail='Cloud ID does not match authenticated appliance.')
        email=str(payload.get('email','')); password=str(payload.get('password','')); portal=payload.get('portal')
        from appliance_identity import get_cloud_identity_backend
        result=get_cloud_identity_backend().authenticate_operator(email=email,password=password,portal=portal,cloud_id=appliance['cloud_id'])
        outcome='login.delegated_succeeded' if result.get('status')=='ok' else 'login.delegated_denied'
        audit({'email':email,'role':'appliance'},outcome,'appliance',appliance['id'],{'portal':portal,'reason':result.get('reason')})
        return result

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
        appliance=authenticate_appliance(request,limiter=None)
        if not LIVE_RELAY_ENABLED or not appliance.get('live_relay_pilot'):
            raise HTTPException(status_code=404,detail='Live relay is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        if not live_relay_camera_limiter.allow(camera_id):
            raise HTTPException(status_code=429,detail='Live relay request rate exceeded.')
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
        # separate flag/role -- see recording_credentials.py. If
        # RECORDING_UPLOAD_ROLE_ARN/RECORDING_S3_BUCKET are ever unset,
        # this still returns 503 regardless of which flag authorized the
        # request -- fails closed the same way Phase 2 did for live
        # relay before Phase 1's IAM was applied.
        #
        # 2026-09-13: authorized by EITHER RECORDING_UPLOAD_ENABLED
        # (bulk/continuous recording upload) OR EVENT_MEDIA_UPLOAD_ENABLED
        # (motion-event thumbnail/clip upload) -- deliberately an OR, not
        # a shared single flag, so either feature can be enabled/disabled
        # completely independently of the other. Which flag actually
        # authorized the request also picks the *scope* of the issued
        # credential: RECORDING_UPLOAD_ENABLED gets the existing,
        # unchanged, whole-camera-prefix policy (bulk upload needs to
        # write anywhere under a camera's recording prefix); the
        # event-media-only path gets the narrower events-only policy
        # instead (see event_media_session_policy()'s own docstring). A
        # request authorized by both simply prefers the broader,
        # already-established bulk policy -- there is nothing for the
        # narrower one to additionally restrict in that case, and this
        # keeps existing bulk-recording behavior completely unchanged
        # when that flag is the one in use.
        appliance=authenticate_appliance(request)
        # 2026-09-15: added `or camera_id in RECORDING_UPLOAD_PILOT_CAMERAS`
        # -- confirmed live as a real gap between this route and
        # recording_available() below: that route already lets a
        # pilot-listed camera_id through even while both flags stay
        # false (see its own 2026-09-13 comment), but this one -- the
        # credential-issuance route a pilot camera must reach FIRST,
        # before it can ever call recording_available() -- did not,
        # making the /available pilot support unreachable in practice.
        # Checked against the raw path param, same as recording_
        # available()'s own pilot check, before _authorized_camera()
        # runs below -- a caller not actually authorized for this
        # camera is still rejected there exactly as before.
        if not (RECORDING_UPLOAD_ENABLED or EVENT_MEDIA_UPLOAD_ENABLED or camera_id in RECORDING_UPLOAD_PILOT_CAMERAS):
            raise HTTPException(status_code=404,detail='Recording upload is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        if boto3 is None or not RECORDING_UPLOAD_ROLE_ARN or not RECORDING_S3_BUCKET or not RECORDING_AWS_REGION:
            raise HTTPException(status_code=503,detail='Recording upload is not configured.')
        if RECORDING_UPLOAD_ENABLED or camera['id'] in RECORDING_UPLOAD_PILOT_CAMERAS:
            policy=recording_session_policy(RECORDING_S3_BUCKET,camera['customer_id'],camera['site_id'],appliance['id'],camera_id)
        else:
            policy=event_media_session_policy(RECORDING_S3_BUCKET,camera['customer_id'],camera['site_id'],appliance['id'],camera_id)
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
        #
        # 2026-09-13: also accepts a pilot-listed camera_id (see
        # RECORDING_UPLOAD_PILOT_CAMERAS's own comment) even while
        # RECORDING_UPLOAD_ENABLED stays globally false -- checked
        # against the raw path param, before _authorized_camera() runs
        # below; a caller not actually authorized for this camera is
        # still rejected there exactly as before, unaffected by this.
        appliance=authenticate_appliance(request)
        if not (RECORDING_UPLOAD_ENABLED or camera_id in RECORDING_UPLOAD_PILOT_CAMERAS):
            raise HTTPException(status_code=404,detail='Recording upload is not enabled.')
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
        # Smart Motion shared-media correlation (2026-09-14 Phase A): a
        # LOCAL id (never a cloud id -- see _resolve_parent_motion_event()'s
        # own docstring), meaningful only for a real smart_motion event.
        # Resolved and frozen here, at ingestion time, never trusted again
        # from the later .../media/shared request itself.
        parent_local_event_id=str(safe.get('parent_local_event_id') or '').strip() or None
        event_id=secrets.token_hex(12); now=datetime.now().isoformat()
        with connection() as db:
            parent_detection_event_id=(
                _resolve_parent_motion_event(db,camera_id,appliance['id'],parent_local_event_id)
                if event_type=='smart_motion' and parent_local_event_id else None
            )
            try:
                db.execute(
                    'INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,object_count,detections_json,event_timestamp,parent_detection_event_id,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (event_id,camera['customer_id'],camera['site_id'],appliance['id'],camera_id,local_event_id,event_type,
                     float(confidence) if confidence is not None else None,
                     int(object_count) if object_count is not None else 1,
                     detections_json,event_timestamp,parent_detection_event_id,now),
                )
            except Exception:
                existing=db.execute(
                    'SELECT id,event_type,event_timestamp,parent_detection_event_id FROM detection_events WHERE camera_id=? AND local_event_id=?',
                    (camera_id,local_event_id),
                ).fetchone()
                if not existing:
                    raise HTTPException(status_code=500,detail='Could not record this event.')
                # Event identity (type, timestamp) is immutable once
                # recorded -- a replay asserting a different one is a
                # conflict, never a silent overwrite (closes the same
                # class of "duplicate mutates" gap already confirmed on
                # the media-registration route, here for event identity
                # itself).
                if existing['event_type']!=event_type or existing['event_timestamp']!=event_timestamp:
                    raise HTTPException(status_code=409,detail='Event identity does not match the previously recorded event.')
                if parent_detection_event_id is not None:
                    if existing['parent_detection_event_id'] is None:
                        # First-time completion of a previously-unresolved
                        # relationship (e.g. the child synced before its
                        # parent) -- allowed exactly once, never again.
                        db.execute('UPDATE detection_events SET parent_detection_event_id=? WHERE id=?',(parent_detection_event_id,existing['id']))
                    elif existing['parent_detection_event_id']!=parent_detection_event_id:
                        raise HTTPException(status_code=409,detail='This event is already correlated with a different Motion event.')
                return {'status':'duplicate','event_id':existing['id']}
            # AAC (facial recognition), Phase 2: closes the second of the
            # three split-topology gaps from the Phase 1 Codex review --
            # this generic route already stored the detection_events row
            # above for ANY event_type (including facial_recognition,
            # forwarded here via analytics_sync.py's own facial_recognition
            # special case in _build_payload()); only the AAC-specific
            # facial_events detail row (matched person/watchlist,
            # confidence, engine) had nowhere to land on the cloud side.
            # Only ever reached on the fresh-insert path above (the
            # duplicate-replay branch returns early), so a retried
            # analytics-event POST can never create a second facial_events
            # row for the same detection_events id -- matching
            # facial_events.facial_events.detection_event_id's own UNIQUE
            # constraint, which would otherwise reject a second insert
            # anyway. No thumbnail is stored here -- see the Phase 2
            # report's own note on face-crop thumbnail cloud sync being a
            # separate, not-yet-implemented piece.
            facial_notify_message=None
            if event_type=='facial_recognition' and isinstance(detections,list) and detections and isinstance(detections[0],dict):
                facial_fields=detections[0]
                db.execute(
                    'INSERT INTO facial_events(id,detection_event_id,customer_id,site_id,camera_id,match_state,matched_person_id,matched_person_name,matched_watchlist_id,matched_watchlist_name,confidence,engine,engine_version,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        secrets.token_hex(12),event_id,camera['customer_id'],camera['site_id'],camera_id,
                        facial_fields.get('match_state') or 'unknown',
                        facial_fields.get('matched_person_id'),facial_fields.get('matched_person_name'),
                        facial_fields.get('matched_watchlist_id'),facial_fields.get('matched_watchlist_name'),
                        float(confidence) if confidence is not None else 0.0,
                        facial_fields.get('engine') or 'unknown',facial_fields.get('engine_version'),now,
                    ),
                )
                # Face Access (2026-09-17): door_notify_message is set by
                # the edge (facial_events.record_facial_events()) only
                # for modes 2/3 (recognized-not-authorized / unknown) --
                # never for mode 1 (an authorized automatic unlock) and
                # never for a facial-recognition camera that isn't a
                # configured door. The cloud never re-derives that
                # authorization decision itself; it only relays the
                # message the edge already decided on, exactly like the
                # PPE hard_hat_present/safety_vest_present fields above.
                facial_notify_message=str(facial_fields.get('door_notify_message') or '').strip() or None
        # 2026-09-04, Smart Alerts fix: this is the currently-active
        # event-ingestion path (the older POST /api/appliance/events ->
        # appliance_events route also calls fanout_appliance_event(),
        # but stopped being called by any real appliance in late August
        # -- see the session notes for the full root-cause trace) --
        # notifications had gone to zero because nothing in this route
        # ever told the existing, unmodified notification engine a new
        # event had arrived. Reuses that same fanout_appliance_event()
        # exactly as the old route already did -- no second notification
        # system, no change to detection_events/Events/Playback/
        # analytics-sync/event-media behavior above. Only ever reached
        # on a genuine fresh insert (the duplicate-replay branch above
        # returns early), so a replayed/retried event can never fan out
        # twice. fanout_appliance_event() itself already no-ops for any
        # event_type outside its own SUPPORTED set -- not re-checked
        # here, to avoid a second, potentially-drifting copy of that
        # list. customer_id/site_id come from the camera's own
        # authoritative row (camera['customer_id']/camera['site_id']),
        # the same source detection_events itself was just stored
        # under above, not the raw appliance object -- matching this
        # function's own established "camera's own authoritative site,
        # never anything else" convention. Never allowed to fail the
        # actual, already-successful ingestion response below: a
        # notification-side error is logged and swallowed, not raised.
        # Face Access (2026-09-17): a facial_recognition event fans out
        # ONLY when the edge attached a door_notify_message (modes 2/3)
        # -- a mode-1 authorized automatic unlock, or a facial-
        # recognition camera that isn't a configured door at all, never
        # reaches fanout_appliance_event() here, exactly matching the
        # requirements doc's own framing ("do NOT auto-unlock... send
        # the customer a notification" is specific to modes 2/3; mode 1
        # has nothing for a customer to act on). Every other event_type
        # is completely unaffected -- this skip is scoped to event_
        # type=='facial_recognition' alone.
        if event_type!='facial_recognition' or facial_notify_message:
            try:
                fanout_appliance_event(
                    {'customer_id': camera['customer_id'], 'site_id': camera['site_id']},
                    {'id': event_id, 'camera_id': camera_id, 'event_type': event_type, 'timestamp': event_timestamp, 'message': facial_notify_message},
                )
            except Exception:
                logger.exception('analytics_event.fanout_failed event_id=%s camera_id=%s', event_id, camera_id)
        return {'status':'accepted','event_id':event_id}

    @app.get('/api/appliance/facial-directory')
    def facial_directory(request: Request) -> dict:
        # AAC (facial recognition), Phase 2: the cloud side of
        # facial_embedding_sync.py's edge-pull worker -- closes the
        # largest of the three split-topology gaps from the Phase 1
        # Codex review (enrolled embeddings only existed in whichever
        # single database matching ran against). Scoped to the
        # authenticated appliance's OWN customer_id only, the same
        # tenant-isolation guarantee every other appliance-scoped route
        # here already has -- an appliance can never request or receive
        # another customer's enrollment data, regardless of what it
        # asks for (this route takes no customer_id parameter at all).
        appliance=authenticate_appliance(request)
        if not FACIAL_EMBEDDING_SYNC_ENABLED: raise HTTPException(status_code=404,detail='Facial embedding sync is not enabled.')
        customer_id=appliance['customer_id']
        with connection() as db:
            people=[dict(item) for item in db.execute(
                'SELECT id,site_id,external_reference,display_name,status,notes,created_at,updated_at,created_by FROM facial_people WHERE customer_id=? AND status=?',
                (customer_id,'active'),
            ).fetchall()]
            embeddings=[dict(item) for item in db.execute(
                'SELECT fe.id,fe.person_id,fe.engine,fe.engine_version,fe.embedding_json,fe.quality,fe.created_at '
                'FROM facial_embeddings fe JOIN facial_people fp ON fp.id=fe.person_id '
                'WHERE fe.customer_id=? AND fp.status=?',
                (customer_id,'active'),
            ).fetchall()]
            watchlists=[dict(item) for item in db.execute(
                'SELECT id,site_id,name,classification,description,created_at,updated_at,created_by FROM facial_watchlists WHERE customer_id=?',
                (customer_id,),
            ).fetchall()]
            watchlist_members=[dict(item) for item in db.execute(
                'SELECT fwm.watchlist_id,fwm.person_id,fwm.added_at,fwm.added_by FROM facial_watchlist_members fwm '
                'JOIN facial_watchlists fw ON fw.id=fwm.watchlist_id WHERE fw.customer_id=?',
                (customer_id,),
            ).fetchall()]
        return {
            'customer_id': customer_id,
            'people': people,
            'embeddings': embeddings,
            'watchlists': watchlists,
            'watchlist_members': watchlist_members,
        }

    @app.post('/api/appliance/facial-events/{detection_event_id}/thumbnail')
    def facial_event_thumbnail_available(request: Request,detection_event_id: str,payload: dict) -> dict:
        # AAC (facial recognition), Phase 2: the third and last of the
        # split-topology gaps from the Phase 1 Codex review -- face-crop
        # thumbnails were local-appliance-disk-only, so a separate cloud
        # customer-portal process could never display one. Reuses the
        # existing object_storage.py abstraction (the SAME 'thumbnails'
        # category/backend other event media already goes through, S3
        # or local per settings.storage_backend) rather than inventing a
        # second storage path.
        #
        # The facial_events row (looked up by detection_event_id, which
        # is globally unique -- facial_events.detection_event_id has its
        # own UNIQUE constraint) must already exist AND belong to a
        # camera assigned to THIS authenticated appliance -- both
        # checked below -- before any bytes are accepted or stored,
        # closing the same tenant-isolation gap every other appliance-
        # scoped route here already closes.
        #
        # Edge-side automatic upload (reading the local face-crop file
        # save_yolo_events()'s AAC hook already writes and POSTing it
        # here through analytics_sync.py) is not yet wired -- this route
        # is the tested, ready cloud half; see the Phase 2 report's own
        # note on this being the one still-manual step.
        appliance=authenticate_appliance(request)
        if not FACIAL_EMBEDDING_SYNC_ENABLED: raise HTTPException(status_code=404,detail='Facial embedding sync is not enabled.')
        image_base64=str(payload.get('image_base64') or '').strip()
        if not image_base64: raise HTTPException(status_code=400,detail='image_base64 is required.')
        try:
            import base64
            image_bytes=base64.b64decode(image_base64,validate=True)
        except Exception as error:
            raise HTTPException(status_code=400,detail='image_base64 is not valid base64.') from error
        with connection() as db:
            event=db.execute(
                'SELECT fe.id,fe.customer_id,fe.camera_id FROM facial_events fe WHERE fe.detection_event_id=?',
                (detection_event_id,),
            ).fetchone()
            if not event: raise HTTPException(status_code=404,detail='Facial event not found.')
            camera=db.execute('SELECT id FROM cameras WHERE id=? AND appliance_id=?',(event['camera_id'],appliance['id'])).fetchone()
            if not camera: raise HTTPException(status_code=403,detail='Camera is not assigned to this appliance.')
            from object_storage import get_storage
            storage_key=f"facial/{event['customer_id']}/{detection_event_id}.jpg"
            stored=get_storage().put('thumbnails',storage_key,image_bytes,content_type='image/jpeg')
            db.execute('UPDATE facial_events SET face_thumbnail_path=? WHERE detection_event_id=?',(stored['key'],detection_event_id))
        return {'status':'accepted','thumbnail_key':stored['key']}

    @app.post('/api/appliance/analytics/{camera_id}/events/{local_event_id}/media')
    def analytics_event_media_available(request: Request,camera_id: str,local_event_id: str,payload: dict) -> dict:
        # Associates one short event clip with an already-synced analytics
        # event. Media bytes are uploaded directly appliance -> S3 using the
        # existing camera-scoped recording upload credential; this route only
        # catalogs the object after upload succeeds.
        appliance=authenticate_appliance(request)
        if not ANALYTICS_SYNC_ENABLED:
            raise HTTPException(status_code=404,detail='Analytics sync is not enabled.')

        camera=_authorized_camera(appliance,camera_id)
        if camera.get('cloud_recording_mode') != 'motion':
            raise HTTPException(status_code=403,detail='Camera is not entitled to motion event recording.')
        safe=sanitize_appliance_payload(payload)

        local_event_id=str(local_event_id or '').strip()
        s3_key=str(safe.get('s3_key','')).strip()
        thumbnail_s3_key=str(safe.get('thumbnail_s3_key','')).strip() or None
        started_at=str(safe.get('started_at','')).strip()
        ended_at=str(safe.get('ended_at','')).strip()
        duration_seconds=safe.get('duration_seconds')
        size_bytes=safe.get('size_bytes')
        # New, optional (2026-09-18): re-validated here, never trusted
        # from the request alone -- must look like the same
        # /recordings/... shape event_media_uploader.py's own
        # _local_path_from_recording_url() already enforces on the
        # appliance side. An absent or malformed value is silently
        # dropped (stored as NULL), never a 400 -- this field is purely
        # additive and must never be able to fail an otherwise-valid
        # upload registration.
        local_relative_path=str(safe.get('local_relative_path') or '').strip() or None
        if local_relative_path and (not local_relative_path.startswith('/recordings/') or '..' in local_relative_path):
            local_relative_path=None

        if not local_event_id:
            raise HTTPException(status_code=400,detail='local_event_id is required.')
        if not s3_key:
            raise HTTPException(status_code=400,detail='s3_key is required.')
        if not started_at or not ended_at:
            raise HTTPException(status_code=400,detail='started_at and ended_at are required.')

        expected_prefix=recording_s3_prefix(
            camera['customer_id'],
            camera['site_id'],
            appliance['id'],
            camera_id,
        )

        now=datetime.now().isoformat()

        with connection() as db:
            event=db.execute(
                'SELECT id FROM detection_events '
                'WHERE camera_id=? AND local_event_id=?',
                (camera_id,local_event_id),
            ).fetchone()

            if not event:
                raise HTTPException(
                    status_code=404,
                    detail='Detection event has not reached the cloud yet.',
                )

            # A camera-scoped upload credential can write beneath its
            # recording prefix.  This catalog endpoint is intentionally
            # narrower: it only accepts the deterministic event
            # clip/thumbnail pair for the event being registered, so an
            # appliance cannot label an unrelated recording object as this
            # event's media.  Use the already-stored event timestamp, not
            # client timing fields: a pre-roll window can cross midnight.
            event_timestamp=db.execute(
                'SELECT event_timestamp FROM detection_events WHERE id=?',
                (event['id'],),
            ).fetchone()['event_timestamp']
            try:
                event_date=datetime.fromisoformat(str(event_timestamp).replace('Z','+00:00')).strftime('%Y/%m/%d')
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,detail='Detection event timestamp is invalid.')
            event_prefix=f"{expected_prefix}{event_date}/events/motion_{local_event_id}"
            expected_clip_key=event_prefix + '.mp4'
            expected_thumbnail_key=event_prefix + '.jpg'
            if s3_key != expected_clip_key:
                raise HTTPException(status_code=403,detail="s3_key is not this event's authorized clip key.")
            if thumbnail_s3_key and thumbnail_s3_key != expected_thumbnail_key:
                raise HTTPException(status_code=403,detail="thumbnail_s3_key is not this event's authorized thumbnail key.")
            try:
                event_at=datetime.fromisoformat(str(event_timestamp).replace('Z','+00:00')).replace(tzinfo=None)
                duration=float(duration_seconds)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,detail='duration_seconds is required.')
            allowed, reason=allows_event_media(db,camera['customer_id'],camera_id,event_at,duration)
            if not allowed: raise HTTPException(status_code=403,detail=reason)

            existing=db.execute(
                'SELECT id FROM detection_event_media WHERE detection_event_id=?',
                (event['id'],),
            ).fetchone()

            if existing:
                db.execute(
                    'UPDATE detection_event_media SET '
                    's3_key=?,thumbnail_s3_key=?,started_at=?,ended_at=?,'
                    'duration_seconds=?,size_bytes=?,local_relative_path=? '
                    'WHERE detection_event_id=?',
                    (
                        s3_key,
                        thumbnail_s3_key,
                        started_at,
                        ended_at,
                        float(duration_seconds) if duration_seconds is not None else None,
                        int(size_bytes) if size_bytes is not None else None,
                        local_relative_path,
                        event['id'],
                    ),
                )
                return {'status':'duplicate','media_id':existing['id']}

            media_id=secrets.token_hex(12)

            db.execute(
                'INSERT INTO detection_event_media('
                'id,detection_event_id,customer_id,camera_id,s3_key,'
                'thumbnail_s3_key,started_at,ended_at,duration_seconds,'
                'size_bytes,local_relative_path,created_at'
                ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    media_id,
                    event['id'],
                    camera['customer_id'],
                    camera_id,
                    s3_key,
                    thumbnail_s3_key,
                    started_at,
                    ended_at,
                    float(duration_seconds) if duration_seconds is not None else None,
                    int(size_bytes) if size_bytes is not None else None,
                    local_relative_path,
                    now,
                ),
            )

        audit(
            {'email':appliance['cloud_id'],'role':'appliance'},
            'appliance.analytics_event_media_available',
            'detection_event_media',
            media_id,
            {'camera_id':camera_id,'local_event_id':local_event_id,'s3_key':s3_key},
        )

        return {'status':'accepted','media_id':media_id}

    @app.post('/api/appliance/analytics/{camera_id}/events/{local_event_id}/media/shared')
    def analytics_event_media_shared(request: Request,camera_id: str,local_event_id: str,payload: dict) -> dict:
        # Registers a SECOND, independently-owned detection_event_media
        # row for a correlated Smart Motion event, referencing its
        # already-verified base Motion event's own already-registered
        # media. Deliberately strict: this request's own schema has no
        # s3_key/thumbnail_s3_key/timing/duration/size field at all, so
        # an appliance cannot supply an arbitrary storage reference here
        # even by mistake -- the only thing accepted is the parent's own
        # LOCAL event id, independently re-resolved and cross-checked
        # against this child's own already-frozen parent_detection_
        # event_id (set once, at ingestion time, by analytics_event_
        # available() above -- never by this route). Every approved
        # clip/thumbnail/timing/duration/size value is copied verbatim
        # from the verified parent's OWN detection_event_media row --
        # never from this request. No ffmpeg encode, no S3 PutObject, no
        # S3 credential of any kind is used or needed here.
        appliance=authenticate_appliance(request)
        if not ANALYTICS_SYNC_ENABLED:
            raise HTTPException(status_code=404,detail='Analytics sync is not enabled.')

        camera=_authorized_camera(appliance,camera_id)
        if camera.get('cloud_recording_mode') != 'motion':
            raise HTTPException(status_code=403,detail='Camera is not entitled to motion event recording.')
        safe=sanitize_appliance_payload(payload)

        local_event_id=str(local_event_id or '').strip()
        parent_local_event_id=str(safe.get('parent_local_event_id') or '').strip()
        if not local_event_id:
            raise HTTPException(status_code=400,detail='local_event_id is required.')
        if not parent_local_event_id:
            raise HTTPException(status_code=400,detail='parent_local_event_id is required.')

        now=datetime.now().isoformat()

        with connection() as db:
            child=db.execute(
                'SELECT id,event_type,appliance_id,camera_id,customer_id,site_id,parent_detection_event_id '
                'FROM detection_events WHERE camera_id=? AND local_event_id=?',
                (camera_id,local_event_id),
            ).fetchone()
            if not child:
                raise HTTPException(status_code=404,detail='Detection event has not reached the cloud yet.')
            if child['event_type']!='smart_motion':
                raise HTTPException(status_code=403,detail='Only a smart_motion event may use shared media registration.')
            if child['appliance_id']!=appliance['id']:
                raise HTTPException(status_code=403,detail='Event does not belong to this appliance.')
            if child['customer_id']!=camera['customer_id'] or child['site_id']!=camera['site_id']:
                raise HTTPException(status_code=403,detail="Event does not belong to this camera's current customer/site.")
            if child['parent_detection_event_id'] is None:
                raise HTTPException(status_code=409,detail='parent_media_pending: correlation not yet established.')

            # Re-resolve the SUBMITTED parent id independently -- it must
            # equal the child's own already-frozen, previously-verified
            # correlation. A request can never silently reassign a
            # different parent after the fact.
            resolved_parent_id=_resolve_parent_motion_event(db,camera_id,appliance['id'],parent_local_event_id)
            if resolved_parent_id is None or resolved_parent_id!=child['parent_detection_event_id']:
                raise HTTPException(status_code=403,detail="parent_local_event_id does not match this event's established correlation.")

            parent=db.execute(
                'SELECT id,event_type,appliance_id,camera_id,customer_id,site_id FROM detection_events WHERE id=?',
                (resolved_parent_id,),
            ).fetchone()
            if not parent:
                raise HTTPException(status_code=404,detail='Claimed parent Motion event no longer exists.')
            if parent['event_type']!='motion':
                raise HTTPException(status_code=403,detail='parent_local_event_id does not identify a base Motion event.')
            if parent['appliance_id']!=appliance['id']:
                raise HTTPException(status_code=403,detail='Claimed parent Motion event does not belong to this appliance.')
            if parent['camera_id']!=camera_id:
                raise HTTPException(status_code=403,detail='Claimed parent Motion event is on a different camera.')
            if parent['customer_id']!=child['customer_id'] or parent['site_id']!=child['site_id']:
                raise HTTPException(status_code=403,detail='Claimed parent Motion event belongs to a different customer or site.')

            parent_media=db.execute(
                'SELECT id,s3_key,thumbnail_s3_key,started_at,ended_at,duration_seconds,size_bytes,local_relative_path '
                'FROM detection_event_media WHERE detection_event_id=?',
                (parent['id'],),
            ).fetchone()
            if not parent_media:
                raise HTTPException(status_code=409,detail='parent_media_pending: base Motion event has no registered media yet.')

            existing=db.execute('SELECT id,source_media_id FROM detection_event_media WHERE detection_event_id=?',(child['id'],)).fetchone()
            if existing:
                if existing['source_media_id']==parent_media['id']:
                    return {'status':'duplicate','media_id':existing['id']}
                raise HTTPException(status_code=409,detail='This event already has different registered media.')

            media_id=secrets.token_hex(12)
            try:
                db.execute(
                    'INSERT INTO detection_event_media('
                    'id,detection_event_id,customer_id,camera_id,s3_key,'
                    'thumbnail_s3_key,started_at,ended_at,duration_seconds,'
                    'size_bytes,local_relative_path,source_media_id,created_at'
                    ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        media_id,
                        child['id'],
                        camera['customer_id'],
                        camera_id,
                        parent_media['s3_key'],
                        parent_media['thumbnail_s3_key'],
                        parent_media['started_at'],
                        parent_media['ended_at'],
                        parent_media['duration_seconds'],
                        parent_media['size_bytes'],
                        parent_media['local_relative_path'],
                        parent_media['id'],
                        now,
                    ),
                )
            except Exception:
                existing=db.execute('SELECT id,source_media_id FROM detection_event_media WHERE detection_event_id=?',(child['id'],)).fetchone()
                if existing and existing['source_media_id']==parent_media['id']:
                    return {'status':'duplicate','media_id':existing['id']}
                raise HTTPException(status_code=500,detail='Could not record shared media.')

        audit(
            {'email':appliance['cloud_id'],'role':'appliance'},
            'appliance.analytics_event_media_shared',
            'detection_event_media',
            media_id,
            {'camera_id':camera_id,'local_event_id':local_event_id,'source_media_id':parent_media['id']},
        )

        return {'status':'accepted','media_id':media_id}

    @app.post('/api/appliance/live/{camera_id}/segment-available')
    def live_relay_segment_available(request: Request,camera_id: str,payload: dict) -> dict:
        appliance=authenticate_appliance(request,limiter=None)
        if not LIVE_RELAY_ENABLED: raise HTTPException(status_code=404,detail='Live relay is not enabled.')
        camera=_authorized_camera(appliance,camera_id)
        if not live_relay_camera_limiter.allow(camera_id):
            raise HTTPException(status_code=429,detail='Live relay request rate exceeded.')
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
                # Single-delivery is enforced by the queued->verifying
                # transition alone (the WHERE status='queued' filter
                # above already makes a second GET call return nothing
                # for this job, regardless of this column) -- the
                # at-rest encrypted blob itself is intentionally NOT
                # cleared here anymore. It stays in place only long
                # enough for appliance_submit_provisioning() (this same
                # job's own conclusion, typically seconds away) to copy
                # it into camera_credentials -- the table
                # _provisioned_camera_stream() (app/main.py) actually
                # reads from -- and clear it there. Before this,
                # nothing ever performed that copy, so a camera
                # provisioned WITH credentials still had none in
                # camera_credentials and camera_url() could never
                # resolve it. Still cleared well within the existing
                # CAMERA_PROVISIONING_TIMEOUT_SECONDS window either way
                # (see partner_workspace.py), never retained
                # indefinitely.
                db.execute("UPDATE camera_provisioning_requests SET status='verifying',updated_at=? WHERE id=?",(now,job['id']))
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
            # Provisioning Phase 7: second, appliance-authenticated
            # enforcement of the purchased camera-slot maximum -- the
            # first gate is request_camera_provisioning()'s own check
            # (partner_workspace.py) at request time, but that check
            # can't see camera counts on OTHER outstanding jobs decided
            # between request and this confirmation, so the limit is
            # re-checked here, right before the row that actually
            # consumes a slot would be created. Only applies to a
            # genuinely NEW device_key -- rediscovering/reconnecting an
            # existing camera below never consumes a new slot and is
            # never blocked by this check.
            if success:
                already_known=db.execute('SELECT id FROM cameras WHERE customer_id=? AND device_key=?',(job['customer_id'],job['device_key'])).fetchone()
                if not already_known:
                    from customer_entitlements import total_camera_slots
                    slot_limit=total_camera_slots(job['customer_id'])
                    configured=db.execute('SELECT COUNT(*) AS n FROM cameras WHERE customer_id=? AND device_key IS NOT NULL',(job['customer_id'],)).fetchone()['n']
                    if configured>=slot_limit:
                        success=False
                        message=f'Camera limit reached: this account is licensed for {slot_limit} camera(s). Upgrade the plan or remove a camera before adding another.'
            if success:
                # device_key identifies the physical camera itself (its
                # ONVIF endpoint reference UUID), not an appliance-camera
                # relationship -- scoped to customer_id, never looked up
                # outside this one customer, so an appliance-scoped
                # duplicate lookup here could never violate
                # idx_cameras_customer_device_key's UNIQUE constraint. A
                # second, differently-scoped implementation of this same
                # dedup rule (partner_workspace.py's provision_customer_
                # camera()) used to exist for a synchronous customer-portal
                # path, but that route was permanently unreachable (a
                # duplicate-path bug) and has been deleted -- this is now
                # the only camera-creation implementation, so there is
                # nothing left to stay consistent with.
                existing=db.execute('SELECT id FROM cameras WHERE customer_id=? AND device_key=?',(job['customer_id'],job['device_key'])).fetchone()
                if existing:
                    # The same physical camera, rediscovered -- possibly
                    # via a different appliance than whichever one last
                    # provisioned it (moved to a new site, or simply
                    # re-scanned from a second appliance that can also
                    # see it). appliance_id and site_id are reassigned to
                    # this job's own values so the camera row always
                    # reflects where it was MOST RECENTLY actually seen,
                    # never left pointing at a stale appliance/site after
                    # a successful reprovision.
                    camera_id=existing['id']
                    db.execute(
                        "UPDATE cameras SET name=?,appliance_id=?,site_id=?,status='configured' WHERE id=?",
                        (job['camera_name'],appliance['id'],job['site_id'],camera_id),
                    )
                else:
                    # Placeholder-consumption fix (confirmed live,
                    # 2026-09-13): a customer's purchased camera-slot
                    # entitlement is represented up front as N placeholder
                    # `cameras` rows (device_key IS NULL, status=
                    # 'pending_installation'; see partner_workspace.py's
                    # provision_customer_appliance()/onboard_customer(),
                    # one INSERT per licensed slot) -- created precisely so
                    # Step 5/6 and /api/customer/cameras have something to
                    # count against before any real device is discovered.
                    # Before this fix, a genuinely new device_key always
                    # inserted a brand-new row here regardless, leaving
                    # every placeholder as a permanent, never-consumed dead
                    # row: an 8-slot customer who provisions 1 real camera
                    # ended up with 9 total rows (8 placeholders + 1 real),
                    # not 8. The entitlement cap above still correctly
                    # bounded how many rows could ever get a real
                    # device_key -- this only fixes which row that
                    # device_key lands on. Scoped to this exact customer/
                    # site/appliance -- a placeholder belonging to any
                    # other tenant, site, or appliance is never touched or
                    # selected. Oldest-first (created_at) purely for
                    # determinism; every placeholder is otherwise identical.
                    placeholder=db.execute(
                        "SELECT id FROM cameras WHERE customer_id=? AND site_id=? AND appliance_id=? AND device_key IS NULL AND status='pending_installation' ORDER BY created_at LIMIT 1",
                        (job['customer_id'],job['site_id'],appliance['id']),
                    ).fetchone()
                    if placeholder:
                        camera_id=placeholder['id']
                        db.execute(
                            "UPDATE cameras SET device_key=?,name=?,status='configured' WHERE id=?",
                            (job['device_key'],job['camera_name'],camera_id),
                        )
                    else:
                        camera_id=secrets.token_hex(5)
                        db.execute(
                            'INSERT INTO cameras(id,customer_id,site_id,appliance_id,device_key,name,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                            (camera_id,job['customer_id'],job['site_id'],appliance['id'],job['device_key'],job['camera_name'],'configured',now),
                        )
                # A camera provisioned through this appliance-driven async
                # path previously never received a camera_number at all --
                # confirmed live on Samsung: live-view start requests 409'd
                # forever because resolve_camera_number() (live_view_
                # sessions.py) had nothing to return. Assigns the lowest
                # free slot only when camera_number is still NULL. An
                # existing valid camera_number (e.g. on a reprovision of
                # an already-bound camera) is never touched here.
                camera_row=db.execute('SELECT camera_number FROM cameras WHERE id=?',(camera_id,)).fetchone()
                if camera_row and camera_row['camera_number'] is None:
                    used=set(int(item['camera_number']) for item in db.execute('SELECT camera_number FROM cameras WHERE appliance_id=? AND camera_number IS NOT NULL',(appliance['id'],)).fetchall())
                    candidate=1
                    while candidate in used: candidate+=1
                    try:
                        from camera_mapping import assign_camera_number
                        assign_camera_number(db,camera_id,candidate,appliance_id=appliance['id'],customer_id=job['customer_id'])
                    except (LookupError, ValueError):
                        pass
                if job['encrypted_credentials']:
                    # Persists the customer-supplied credentials from
                    # THIS job into camera_credentials -- the table
                    # _provisioned_camera_stream() (app/main.py) and
                    # this route's own POST .../media-uri handler
                    # actually read from. request_camera_provisioning()
                    # (partner_workspace.py) already encrypts and stores
                    # them on camera_provisioning_requests at request
                    # time; nothing before this fix ever copied them
                    # across to where the running pipeline looks for
                    # them. Upsert, not insert-only, so a reprovision
                    # with corrected credentials updates the existing
                    # row in place -- same never-delete/recreate,
                    # never-a-duplicate discipline as the camera row
                    # reprovision above. A reprovision submitted WITHOUT
                    # credentials (encrypted_credentials NULL/empty on
                    # this job) never reaches this branch at all, so it
                    # can never blank out a previously-good credential.
                    db.execute(
                        'INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?) '
                        'ON CONFLICT(camera_id) DO UPDATE SET encrypted_blob=excluded.encrypted_blob,updated_at=excluded.updated_at',
                        (camera_id,job['encrypted_credentials'],now,now),
                    )
                # Cleared here, at this job's own conclusion, now that
                # it has done everything it will ever do with it --
                # copied into camera_credentials above if present. See
                # appliance_provisioning_jobs()'s own comment for why
                # this, not the earlier GET/delivery step, is where
                # retention actually ends.
                db.execute("UPDATE camera_provisioning_requests SET status='provisioned',camera_id=?,message=?,updated_at=?,encrypted_credentials=NULL WHERE id=?",(camera_id,message or 'Camera provisioned.',now,job_id))
            else:
                db.execute("UPDATE camera_provisioning_requests SET status='failed',message=?,updated_at=?,encrypted_credentials=NULL WHERE id=?",(message or 'Provisioning failed.',now,job_id))
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

    @app.get('/api/appliance/updates/latest')
    def appliance_update_latest(request: Request,target: str='',channel: str='') -> dict:
        # RDM-2 Group 2D: the server half of an already-built, already-
        # tested device client (updater/s3_source.py's ManifestSource) --
        # see updates_storage.py's own module docstring for how this gap
        # was found (the endpoint was never registered, so every call
        # 404'd and the device raised SourceUnavailable). target/channel
        # are re-validated independently of whatever the client already
        # checked -- there is no shared import path between this package
        # and the agent's, so nothing from the request is trusted as-is.
        authenticate_appliance(request)
        from updates_storage import get_latest_release,load_signing_key,sign_manifest,validate_path_segment
        try:
            target=validate_path_segment(target,'target'); channel=validate_path_segment(channel,'channel')
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error
        release=get_latest_release(target,channel)
        if not release:
            # The safe-by-default, common case: no release has been
            # published for this target/channel. Never a 404 -- s3_source.
            # py's ManifestSource treats exactly this shape as "no update
            # right now," not an error.
            return {'status':'no_update_available'}
        signing_key=load_signing_key()
        if signing_key is None:
            logger.error('appliance_updates.signing_key_unavailable target=%s channel=%s',target,channel)
            raise HTTPException(status_code=503,detail='Update signing is not configured.')
        signature=sign_manifest(release['manifest'],signing_key)
        package_url=get_storage().url('updates',release['package_key'],expires_seconds=300)
        return {'manifest':release['manifest'],'signature':base64.b64encode(signature).decode('ascii'),'package_url':package_url}

    @app.post('/api/appliance/updates/{update_id}/result')
    def appliance_update_result(request: Request,update_id: str,payload: dict) -> dict:
        # RDM-2 Group 2E's reporting counterpart -- service.py's
        # report_update_result() posts here. A second report for the
        # same (update_id, appliance_id) is a 409, matching that
        # method's own documented expectation (never retried) and the
        # same "never-retryable conflict" shape command_result() above
        # already uses for the generic command channel.
        appliance=authenticate_appliance(request)
        state=str(payload.get('state','')).strip()
        if not state: raise HTTPException(status_code=400,detail='state is required.')
        now=datetime.now().isoformat()
        with connection() as db:
            try:
                db.execute('INSERT INTO appliance_update_results(update_id,appliance_id,from_version,to_version,state,error,rollback_from,duration_seconds,reported_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (update_id,appliance['id'],payload.get('from_version'),payload.get('to_version'),state,str(payload.get('error',''))[:500],payload.get('rollback_from'),payload.get('duration_seconds'),now))
            except Exception as error:
                raise HTTPException(status_code=409,detail='An update result was already reported for this update_id.') from error
        audit({'email':appliance['cloud_id'],'role':'appliance'},'appliance.update_result_reported','appliance_update',update_id,{'state':state})
        return {'status':'accepted'}

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

    @app.post('/api/admin/cameras/{camera_id}/local-recording-mode')
    def set_local_recording_mode(request: Request,camera_id: str,payload: dict) -> dict:
        # Same no-hidden-default convention as cloud_recording_mode above,
        # and deliberately a completely separate column/decision:
        # cloud_recording_mode gates whether an already-recorded LOCAL
        # file gets uploaded to cloud; this gates whether that file gets
        # written to local disk at all. NULL/'continuous' (every camera
        # today) is read everywhere exactly like the current unconditional
        # 5-minute segmenter -- only an explicit 'event' value switches a
        # camera to motion/activity-triggered recording (see
        # local_recording_policy.py and main.py's process_supervisor).
        identity=require_partner_access(request,{'administrator'})
        mode=payload.get('local_recording_mode')
        if mode is not None and mode not in ('continuous','event'):
            raise HTTPException(status_code=400,detail="local_recording_mode must be 'continuous', 'event', or null.")
        # Configurable pre-roll/post-roll/merge-gap/max-event-length --
        # all optional; a value left out of the payload is left
        # untouched in the database (omission means "don't change it",
        # not "clear it to null"). Validated as plain positive integers
        # within a sane bound -- these are seconds, not milliseconds,
        # and a customer-facing typo (e.g. 36000 meant as minutes)
        # should fail loudly here rather than silently record ten hours
        # of "pre-roll".
        FIELD_COLUMNS = (
            ('pre_roll_seconds', 'local_recording_pre_roll_seconds', 300),
            ('post_roll_seconds', 'local_recording_post_roll_seconds', 300),
            ('merge_gap_seconds', 'local_recording_merge_gap_seconds', 300),
            ('max_event_seconds', 'local_recording_max_event_seconds', 3600),
        )
        updates: dict[str, int] = {}
        for field, column, max_seconds in FIELD_COLUMNS:
            if field not in payload or payload[field] is None:
                continue
            value = payload[field]
            if not isinstance(value, int) or isinstance(value, bool) or not (0 < value <= max_seconds):
                raise HTTPException(status_code=400, detail=f"{field} must be a positive integer, at most {max_seconds} seconds.")
            updates[column] = value
        set_clauses = ['local_recording_mode=?'] + [f'{column}=?' for column in updates]
        values = [mode] + list(updates.values()) + [camera_id]
        with connection() as db:
            cursor=db.execute(f'UPDATE cameras SET {", ".join(set_clauses)} WHERE id=?', values)
            if cursor.rowcount!=1: raise HTTPException(status_code=404,detail='Camera not found.')
        response = {'camera_id': camera_id, 'local_recording_mode': mode}
        for field, column, _ in FIELD_COLUMNS:
            if column in updates:
                response[field] = updates[column]
        audit(identity,'camera.local_recording_mode_changed','camera',camera_id,response)
        return response

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

    @app.post('/api/admin/customers/{customer_id}/camera-quota')
    def set_camera_quota(request: Request,customer_id: str,payload: dict) -> dict:
        # RDM's direct control over a customer's licensed camera count --
        # the missing link the customer_entitlements.py module docstring
        # itself already calls out: this table is the one function
        # (total_camera_slots()) both provisioning-enforcement gates
        # (partner_workspace.request_camera_provisioning(), appliance_
        # cloud.appliance_submit_provisioning() -- Phase 7) actually call,
        # but until this route, the ONLY way to write to it was a real
        # Stripe checkout/subscription event. The pre-existing partner-
        # facing "Update camera entitlement" form (PUT /api/partner/
        # customers/{id}/plan) writes a DIFFERENT column (plans.camera_
        # quantity) that total_camera_slots() has never read -- confirmed
        # by that module's own docstring ("does not replace or migrate
        # #1-#3 [including plans.camera_quantity]... a product decision
        # for a later phase"). This route is that later phase, scoped
        # exactly to what RDM needs: set the real, enforced number
        # directly, administrator-only, with an audit trail -- not a
        # second cosmetic-only entitlement concept.
        #
        # Reuses upsert_entitlement() unchanged (idempotent per (customer_
        # id, product), "updates in place rather than accumulating rows"
        # per its own docstring) under a dedicated product key so an RDM-
        # granted quota is never silently double-counted alongside a real
        # future Stripe-driven camera_slots_local/camera_slots_hybrid
        # entitlement for the same customer -- total_camera_slots() sums
        # every active product, so each product key must represent
        # exactly one real grant.
        #
        # No hidden default, no negative/zero-is-implicitly-unlimited
        # coercion: 0 is a valid, explicit "no cameras licensed right
        # now" (e.g. a suspended account), and must never be confused
        # with "not set" (no entitlement row at all, which total_camera_
        # slots() already treats as 0 via its own sum-of-nothing default).
        identity=require_partner_access(request,{'administrator'})
        quota=payload.get('camera_quota')
        if not isinstance(quota,int) or isinstance(quota,bool) or quota<0:
            raise HTTPException(status_code=400,detail='camera_quota must be a non-negative integer.')
        with connection() as db:
            customer=db.execute('SELECT id FROM customers WHERE id=?',(customer_id,)).fetchone()
        if not customer:
            raise HTTPException(status_code=404,detail='Customer not found.')
        from customer_entitlements import upsert_entitlement, total_camera_slots
        upsert_entitlement(customer_id=customer_id,product='camera_slots_rdm',camera_slot_quantity=quota,status='active')
        audit(identity,'customer.camera_quota_changed','customer',customer_id,{'camera_quota':quota})
        return {'customer_id':customer_id,'camera_quota':quota,'total_camera_slots':total_camera_slots(customer_id)}

    @app.post('/api/admin/customers/{customer_id}/cloud-policy')
    def set_cloud_policy(request: Request,customer_id: str,payload: dict) -> dict:
        # RDM's direct control over a customer's cloud-cost policy -- the
        # same "RDM entitlement, cloud DB authoritative, generic per
        # customer_id" shape as cloud_recording_mode and the camera-quota
        # route above, applied here to the two real cost-control levers:
        # daily_cloud_seconds (an OPT-IN-ONLY ceiling on the Continuous/
        # Cloud tier's own continuous-segment upload volume -- Hybrid no
        # longer uploads continuous segments at all, so this has no
        # effect there; unset means no cap, matching that tier's own
        # "unlimited, customer pays for what they use" product purpose)
        # and retention_days (7/14/30, the real S3 lifecycle-expiration
        # window, applies to every customer's cloud media -- for Hybrid
        # that means event clips/thumbnails, event_media_uploader.py's
        # own pipeline). Either field is optional and independently settable;
        # omitting one leaves it unchanged (a partial update, not a
        # destructive full overwrite) -- an administrator raising only
        # retention_days must never accidentally reset the allowance
        # back to NULL/default in the same call.
        #
        # Product-wide by construction: nothing here reads or special-
        # cases any specific customer_id, appliance_id, or camera_id --
        # the exact same route and logic apply to every real customer
        # this or any future appliance is activated under. Reuses the
        # existing GET /api/appliance/configuration sync channel (see
        # appliance_configuration() below) to reach the edge appliance,
        # exactly like cloud_recording_mode/camera entitlements already
        # do -- no second, parallel config-delivery mechanism.
        identity=require_partner_access(request,{'administrator'})
        with connection() as db:
            customer=db.execute('SELECT id FROM customers WHERE id=?',(customer_id,)).fetchone()
            if not customer:
                raise HTTPException(status_code=404,detail='Customer not found.')
            existing=db.execute('SELECT daily_cloud_seconds,retention_days FROM customer_cloud_policy WHERE customer_id=?',(customer_id,)).fetchone()
            daily_cloud_seconds=existing['daily_cloud_seconds'] if existing else None
            retention_days=existing['retention_days'] if existing else None
            if 'daily_cloud_seconds' in payload:
                value=payload.get('daily_cloud_seconds')
                if value is not None and (not isinstance(value,int) or isinstance(value,bool) or value<=0):
                    raise HTTPException(status_code=400,detail='daily_cloud_seconds must be a positive integer, or null to clear back to the system default.')
                daily_cloud_seconds=value
            if 'retention_days' in payload:
                value=payload.get('retention_days')
                if value is not None and value not in (7,14,30):
                    raise HTTPException(status_code=400,detail='retention_days must be 7, 14, or 30 (or null to clear back to the legacy plan default).')
                retention_days=value
            now=datetime.now().isoformat()
            db.execute(
                'INSERT INTO customer_cloud_policy(customer_id,daily_cloud_seconds,retention_days,updated_at,updated_by) VALUES(?,?,?,?,?) '
                'ON CONFLICT(customer_id) DO UPDATE SET daily_cloud_seconds=excluded.daily_cloud_seconds,retention_days=excluded.retention_days,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                (customer_id,daily_cloud_seconds,retention_days,now,identity['email']),
            )
        audit(identity,'customer.cloud_policy_changed','customer',customer_id,{'daily_cloud_seconds':daily_cloud_seconds,'retention_days':retention_days})
        return {'customer_id':customer_id,'daily_cloud_seconds':daily_cloud_seconds,'retention_days':retention_days}

    @app.post('/api/admin/customers/{customer_id}/storage-policy')
    def set_storage_policy(request: Request,customer_id: str,payload: dict) -> dict:
        # RDM's direct control over a customer's local recording storage
        # thresholds -- same "RDM override, cloud DB authoritative,
        # generic per customer_id, partial update never a destructive
        # full overwrite" shape as set_cloud_policy() immediately above.
        # reserved_free_percent is the automatic-cleanup floor (default
        # local_storage_policy.DEFAULT_RESERVED_FREE_PERCENT, 10);
        # warning_free_percent is the earlier, non-destructive line
        # (default DEFAULT_WARNING_FREE_PERCENT, 20). local_retention_days
        # is the independent age-based local trigger (null = no age
        # limit, the default for most customers) -- deliberately separate
        # from the existing Hybrid AWS/S3 cloud retention entitlement
        # (set_cloud_policy's own retention_days immediately above), which
        # governs the cloud copy, not this appliance's local disk. Reuses
        # the same GET /api/appliance/configuration sync channel
        # (storage_policy field, appliance_configuration() above)
        # local_storage_manager.py already polls -- no second, parallel
        # config-delivery mechanism.
        identity=require_partner_access(request,{'administrator'})
        with connection() as db:
            customer=db.execute('SELECT id FROM customers WHERE id=?',(customer_id,)).fetchone()
            if not customer:
                raise HTTPException(status_code=404,detail='Customer not found.')
            existing=db.execute('SELECT reserved_free_percent,warning_free_percent,local_retention_days FROM local_storage_policy WHERE customer_id=?',(customer_id,)).fetchone()
            reserved_free_percent=existing['reserved_free_percent'] if existing else None
            warning_free_percent=existing['warning_free_percent'] if existing else None
            local_retention_days=existing['local_retention_days'] if existing else None
            if 'reserved_free_percent' in payload:
                value=payload.get('reserved_free_percent')
                if value is not None and (not isinstance(value,int) or isinstance(value,bool) or value<1 or value>90):
                    raise HTTPException(status_code=400,detail='reserved_free_percent must be an integer from 1 to 90, or null to clear back to the system default.')
                reserved_free_percent=value
            if 'warning_free_percent' in payload:
                value=payload.get('warning_free_percent')
                if value is not None and (not isinstance(value,int) or isinstance(value,bool) or value<1 or value>95):
                    raise HTTPException(status_code=400,detail='warning_free_percent must be an integer from 1 to 95, or null to clear back to the system default.')
                warning_free_percent=value
            if 'local_retention_days' in payload:
                value=payload.get('local_retention_days')
                if value is not None and (not isinstance(value,int) or isinstance(value,bool) or value<1 or value>3650):
                    raise HTTPException(status_code=400,detail='local_retention_days must be a positive integer (days), or null to clear back to no age limit.')
                local_retention_days=value
            if reserved_free_percent is not None and warning_free_percent is not None and reserved_free_percent>=warning_free_percent:
                raise HTTPException(status_code=400,detail='reserved_free_percent must be lower than warning_free_percent (cleanup only ever triggers below the warning line).')
            now=datetime.now().isoformat()
            db.execute(
                'INSERT INTO local_storage_policy(customer_id,reserved_free_percent,warning_free_percent,local_retention_days,updated_at,updated_by) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(customer_id) DO UPDATE SET reserved_free_percent=excluded.reserved_free_percent,warning_free_percent=excluded.warning_free_percent,local_retention_days=excluded.local_retention_days,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                (customer_id,reserved_free_percent,warning_free_percent,local_retention_days,now,identity['email']),
            )
        audit(identity,'customer.storage_policy_changed','customer',customer_id,{'reserved_free_percent':reserved_free_percent,'warning_free_percent':warning_free_percent,'local_retention_days':local_retention_days})
        return {'customer_id':customer_id,'reserved_free_percent':reserved_free_percent,'warning_free_percent':warning_free_percent,'local_retention_days':local_retention_days}

    @app.post('/api/partner/appliances/{appliance_id}/commands')
    def queue_command(request: Request,appliance_id: str,payload: dict) -> dict:
        # A direct Partner Portal session is tried first and is
        # completely unmodified from before -- a partner-only account's
        # own behavior here never changes. Only when that fails do we
        # attempt the admin_partner_bridge (see its own module docstring):
        # an Admin Portal session with a live, explicit link to an
        # eligible partner_users row. Any bridge failure (no session, no
        # link, revoked, role no longer eligible) re-raises the exact
        # original 403 -- an unauthorized caller, admin or otherwise,
        # sees precisely the same denial as before this bridge existed.
        try:
            identity=require_partner_access(request)
        except HTTPException:
            identity=None
            if current_user is not None:
                from admin_partner_bridge import bridge_partner_identity
                with connection() as db:
                    identity=bridge_partner_identity(db,admin_user=current_user(request))
            if not identity:
                raise
        command=str(payload.get('command',''))
        if not payload.get('confirmed'): raise HTTPException(status_code=400,detail='Explicit command confirmation is required.')
        if command not in ALLOWED_COMMANDS: raise HTTPException(status_code=400,detail='Only approved appliance commands are allowed. Remote shell is not supported.')
        from partner_db import require_permission
        try: require_permission(identity,'appliance.action')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        command_id=secrets.token_hex(7); now=datetime.now(); expires=now+timedelta(minutes=max(5,min(1440,int(payload.get('expires_minutes',60)))))
        with connection() as db:
            # HIGH fix (2026-09-14 multi-tenant security remediation,
            # Codex tenant-isolation audit): this route previously
            # required only the appliance.action permission itself,
            # never that appliance_id belonged to the caller's own
            # tenant -- any partner_owner/technician could queue a real
            # remote command for another tenant's appliance just by
            # naming its id. Resolved and tenant-verified BEFORE the
            # de-dup lookup and any insert, in this same connection, so
            # a denied cross-tenant request neither reveals whether a
            # matching command is already queued for the foreign
            # appliance nor queues one. Same 404 whether the id doesn't
            # exist at all or simply isn't this caller's tenant -- see
            # authorize_appliance_tenant()'s own docstring for why
            # that's deliberate (no cross-tenant existence oracle).
            if not authorize_appliance_tenant(db,identity,appliance_id):
                raise HTTPException(status_code=404,detail='Appliance not found.')
            # De-dup guard: an identical command already pending or
            # delivered (not yet completed/failed/expired) for this
            # appliance is returned as-is instead of queuing a second
            # copy -- a partner double-clicking "Restart service", or a
            # dashboard auto-refresh replaying the same request, must
            # never queue N redundant restarts/reboots for one appliance
            # to work through.
            existing=db.execute("SELECT id FROM appliance_commands WHERE appliance_id=? AND command=? AND status IN ('pending','delivered') ORDER BY created_at LIMIT 1",(appliance_id,command)).fetchone()
            if existing: return {'id':existing['id'],'status':'pending','message':'An identical command is already queued for this appliance; not queuing a duplicate.'}
            db.execute('INSERT INTO appliance_commands(id,appliance_id,command,payload_json,status,created_at,expires_at,created_by) VALUES(?,?,?,?,?,?,?,?)',(command_id,appliance_id,command,json.dumps(sanitize_appliance_payload(payload.get('payload',{}))),'pending',now.isoformat(),expires.isoformat(),identity['email']))
            if command=='install_update':
                db.execute("UPDATE appliances SET state='updating' WHERE id=?",(appliance_id,))
        audit(identity,'appliance.command_queued','appliance_command',command_id,{'command':command,'appliance_id':appliance_id}); return {'id':command_id,'status':'pending','message':'Authorized appliance command queued.'}

    @app.get('/partner/appliance-dashboard',response_class=HTMLResponse)
    def appliance_dashboard(request: Request,partner: str='',customer: str='',site: str='',status: str='',version: str=''):
        identity=partner_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        require_partner_access(request)
        # HIGH fix (2026-09-14 partner-scoped-administrator follow-up,
        # Codex tenant-isolation re-audit): sibling-audit finding, same
        # pattern and same fix as partner_workspace.py's render_partner_
        # workspace() customer listing -- identity['role']!='administrator'
        # was a bare role-name shortcut that let a partner-scoped
        # administrator drop the partner_id filter entirely and enumerate
        # every other partner's real appliances here. Only a live-verified
        # GLOBAL administrator grant may see appliances across every
        # partner. Resolved once, up front, so the stale-state housekeeping
        # mutation and the command-history query below can reuse the exact
        # same tenant boundary as the appliance-card query.
        from appliance_identity import has_global_administrator_grant
        with connection() as db:
            is_global=has_global_administrator_grant(db,email=identity.get('email',''))
        owned_partner_id=identity.get('partner_id') or 'anyaicam-primary'
        # HIGH fix (2026-09-14 final tenant-isolation re-audit, Codex):
        # this housekeeping UPDATE previously ran unconditionally for
        # every appliance on every partner's page load -- any
        # authenticated partner could trigger a cross-tenant state
        # mutation on appliances they don't own just by loading this
        # dashboard. It's now confined to the same tenant boundary as
        # the rest of this page; only a genuine global administrator's
        # visit sweeps every partner's stale appliances.
        stale_before=(datetime.now()-timedelta(minutes=3)).isoformat()
        with connection() as db:
            if is_global:
                db.execute("UPDATE appliances SET state='offline',online_status='offline' WHERE state IN ('online','degraded') AND (last_check_in IS NULL OR last_check_in<?)",(stale_before,))
            else:
                db.execute("UPDATE appliances SET state='offline',online_status='offline' WHERE partner_id=? AND state IN ('online','degraded') AND (last_check_in IS NULL OR last_check_in<?)",(owned_partner_id,stale_before))
        clauses=['1=1']; params=[]
        if not is_global: clauses.append('a.partner_id=?'); params.append(owned_partner_id)
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
            # storage_state (2026-09-17, local recording storage management):
            # 'healthy'/None (never reported, e.g. feature not enabled on
            # this appliance) adds no warning -- 'warning'/'cleanup_active'/
            # 'critical' surface exactly the label the requirement asks for
            # ("Healthy / Warning / Cleanup Active / Critical"), sourced
            # from the appliance's own real-time report, not re-derived
            # from disk_capacity/disk_used here (see heartbeat()'s own
            # comment on why 'cleanup_active' can't be inferred cloud-side).
            storage_state=item.get('storage_state')
            storage_labels={'warning':'Storage warning','cleanup_active':'Storage cleanup active','critical':'Storage critical'}
            if storage_state in storage_labels: warnings.append(storage_labels[storage_state])
            pending_count=item.get('upload_pending_count'); quarantined_count=item.get('upload_quarantined_count')
            backlog_text=f'{pending_count} pending · {quarantined_count} quarantined' if pending_count is not None else 'Not yet reported'
            storage_free_percent=item.get('storage_free_percent')
            storage_text=f'{(storage_state or "healthy").replace("_"," ").title()} · {storage_free_percent:.1f}% free' if storage_state is not None and storage_free_percent is not None else 'Not yet reported'
            cards.append(f'''<article class="panel"><div class="panel-head"><div><h2>{escape(item['cloud_id'])}</h2><div class="health-detail">{escape(item.get('customer_name') or 'Unassigned')} · {escape(item.get('site_name') or 'No site')} · {escape(item.get('software_version') or 'Unknown')}</div></div><span class="pill">{escape(item.get('state') or 'offline')}</span></div><div class="health-row"><span>Last check-in</span><strong>{escape(item.get('last_check_in') or 'Never')}</strong></div><div class="health-row"><span>CPU / Memory / Disk</span><strong>{item.get('cpu',0)}% / {item.get('memory',0)}% / {item.get('disk',0)} GB</strong></div><div class="health-row"><span>Local storage</span><strong>{escape(storage_text)}</strong></div><div class="health-row"><span>Cameras</span><strong>{len(camera_status)}</strong></div><div class="health-row"><span>Restarts</span><strong>{item.get('restart_count',0)}</strong></div><div class="health-row"><span>Upload backlog</span><strong>{escape(backlog_text)}</strong></div><div class="mock-banner" {'' if warnings else 'hidden'}>{', '.join(warnings)}</div><div class="library-toolbar">{''.join(f'<button class="filter queue-command" data-appliance="{item["id"]}" data-command="{command}">{label}</button>' for command,label in [('restart_service','Restart service'),('refresh_cameras','Refresh cameras'),('run_diagnostics','Diagnostics'),('install_update','Install update'),('reboot_appliance','Reboot appliance'),('restart_vms','Restart VMS')])}</div><details><summary>Recent health history ({len(history)})</summary>{''.join(f'<p>{escape(h["created_at"])} · {escape(h["status"])} · CPU {h["cpu"]}%</p>' for h in history)}</details></article>''')
        # HIGH fix (2026-09-14 final tenant-isolation re-audit, Codex):
        # this query previously had no tenant predicate at all, so any
        # partner-scoped administrator saw every other partner's queued
        # commands -- foreign cloud IDs, commands, statuses, timestamps,
        # and errors. Same tenant boundary as the appliance-card query
        # above (is_global / owned_partner_id).
        command_clauses=['1=1']; command_params=[]
        if not is_global: command_clauses.append('a.partner_id=?'); command_params.append(owned_partner_id)
        command_rows=rows('SELECT c.*,a.cloud_id FROM appliance_commands c JOIN appliances a ON a.id=c.appliance_id WHERE '+' AND '.join(command_clauses)+' ORDER BY c.created_at DESC LIMIT 50',command_params)
        command_table=''.join(f'<tr><td>{escape(item["cloud_id"])}</td><td>{escape(item["command"].replace("_"," "))}</td><td><span class="pill">{escape(item["status"])}</span></td><td>{escape(item["created_at"])}</td><td>{escape(item.get("error") or "")}</td></tr>' for item in command_rows) or '<tr><td colspan="5">No remote actions have been queued.</td></tr>'
        content=f'''<header class="topbar"><div><p class="eyebrow">Secure appliance fleet</p><h1>Appliance dashboard</h1></div></header><form class="panel clip-form" method="get"><label>Partner<input name="partner" value="{escape(partner,quote=True)}"></label><label>Customer<input name="customer" value="{escape(customer,quote=True)}"></label><label>Site<input name="site" value="{escape(site,quote=True)}"></label><label>Status<select name="status"><option value="">All</option>{''.join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ['online','degraded','offline','updating','revoked'])}</select></label><label>Version<input name="version" value="{escape(version,quote=True)}"></label><button class="action-button">Filter</button></form><div class="account-grid" style="margin-top:18px">{''.join(cards) or '<div class="empty">No appliances match these filters.</div>'}</div><section class="panel" style="margin-top:18px;overflow:auto"><h2>Remote action history</h2><table class="data-table"><thead><tr><th>Appliance</th><th>Command</th><th>Status</th><th>Created</th><th>Error</th></tr></thead><tbody>{command_table}</tbody></table></section>'''
        scripts='''<script>const DISRUPTIVE_COMMAND_WARNINGS={reboot_appliance:'This reboots the physical appliance. All cameras and recording will be briefly interrupted.',restart_vms:'This restarts the AnyAiCam VMS service on this appliance. Live view and recording will be briefly interrupted.'};document.querySelectorAll('.queue-command').forEach(button=>button.onclick=async()=>{const warning=DISRUPTIVE_COMMAND_WARNINGS[button.dataset.command];if(!confirm(warning?`${warning} Continue?`:`Queue ${button.textContent} for this appliance?`))return;const response=await fetch(`/api/partner/appliances/${button.dataset.appliance}/commands`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:button.dataset.command,confirmed:true})}),r=await response.json();showToast(r.message||r.detail)})</script>'''
        return shell('Appliance dashboard','appliances',content,scripts)
