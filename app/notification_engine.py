import os
import secrets
from datetime import datetime

from notification_service import CHANNELS
from partner_db import connection,row,rows

# 'ppe' added (2026-09-16): confirmed a real, previously-silent gap --
# analytics_sync.py's _notification_event_type() already passes 'ppe'
# through unchanged (it isn't a vehicle/people_counting/plate sub-type
# needing translation), but this set never included it, so every real
# PPE violation was silently dropped by the very first line of
# fanout_appliance_event() below -- zero notifications of ANY kind
# (not even in-app), even though PPE is a fully working, already-
# verified analytic and customer_notification_channels' own EVENT_TYPES
# (notification_preferences.py) already lets a customer select "PPE
# violation" as something they want to hear about.
# 'storage_problem' added (2026-09-17): the same real, previously-silent
# gap 'ppe' fixed above -- notification_preferences.py's own EVENT_TYPES
# (what a customer actually sees and can toggle on the Notifications
# settings page) has always called this event 'storage_problem', not
# 'low_disk'. Without it here, a customer who explicitly opted in to
# "Recording/storage problem" alerts could never actually receive one --
# _external_channels()'s own `event_type not in prefs['event_types']`
# check would silently fail to match, the exact same class of dead
# wiring 'ppe' was missing for. 'low_disk' is left in place alongside it
# (not removed) since existing internal/admin-panel code already
# references that label independently of customer notification
# preferences.
SUPPORTED={'motion','smart_motion','person','vehicle','line_crossing','intrusion','lpr','people_counting','occupancy','camera_offline','recording_stopped','appliance_offline','low_disk','storage_problem','high_cpu','software_update','ppe'}

# Per-(user, camera, event_type) minimum spacing between EXTERNAL
# (email/sms) delivery attempts -- "Prevent duplicate/spam notifications
# from repeated camera events." In-app notifications (the Smart Alerts
# list) are deliberately NOT throttled by this: they're the existing,
# already-correct one-row-per-real-event behavior this phase must
# preserve unchanged. This only suppresses the external channels, which
# is where repeated real (non-duplicate, e.g. a person lingering in
# frame re-triggering Smart Motion every few seconds) events actually
# become spam for a customer's inbox/phone.
NOTIFICATION_CHANNEL_COOLDOWN_SECONDS=max(0,int(os.environ.get("ANYAICAM_NOTIFICATION_CHANNEL_COOLDOWN_SECONDS","300")))


def _within_quiet_hours(current_time: str, quiet_start: str, quiet_end: str) -> bool:
    """HH:MM string comparison, wrap-aware: quiet_start > quiet_end means
    the window crosses midnight (e.g. 22:00-07:00), matching notification_
    preferences.py's own quiet_start/quiet_end contract exactly (no
    timezone conversion here -- both sides are already this appliance's
    own local HH:MM, the same convention notification_preferences.py's
    validation and the old schedule_start/schedule_end check it replaces
    both already used)."""
    if quiet_start <= quiet_end:
        return quiet_start <= current_time <= quiet_end
    return current_time >= quiet_start or current_time <= quiet_end


def _external_channel_recently_notified(db,*,user_id: str,camera_id: str | None,event_type: str,now: datetime,exclude_notification_id: str) -> bool:
    """True if this exact (user, camera, event_type) combination already
    had a DIFFERENT notification created within the cooldown window --
    external channels are suppressed for this new one (the in-app row
    is still created either way, see fanout_appliance_event() below).
    exclude_notification_id is required, not optional: fanout_appliance_
    event() inserts the new notification row BEFORE calling this check
    (so its own id is already in the table by the time this query runs)
    -- omitting the exclusion would make every event's own cooldown
    check match itself, permanently suppressing every first-ever
    notification's external delivery. camera_id IS NULL-safe: appliance-
    level event types (camera_id None) are grouped together by
    event_type alone, same as everything else."""
    if NOTIFICATION_CHANNEL_COOLDOWN_SECONDS<=0:
        return False
    cutoff=(now.timestamp()-NOTIFICATION_CHANNEL_COOLDOWN_SECONDS)
    cutoff_iso=datetime.fromtimestamp(cutoff).isoformat()
    camera_clause="camera_id IS NULL" if camera_id is None else "camera_id=?"
    params=[user_id,event_type,cutoff_iso,exclude_notification_id]
    if camera_id is not None:
        params.insert(1,camera_id)
    query=f"SELECT 1 FROM notifications WHERE user_id=? AND {camera_clause} AND event_type=? AND created_at>=? AND id!=? LIMIT 1"
    return db.execute(query,tuple(params)).fetchone() is not None


def _external_channels(db,*,user,customer_id: str,camera_id: str | None,event_type: str,current_time: str,now: datetime,notification_id: str) -> dict:
    """Whether email/sms should actually fire for this one event, for
    this one recipient, and the real contact address/number to send to
    -- the real link this phase closes: customer_notification_channels
    (notification_preferences.py, what the customer's own Notifications
    settings page actually writes) was never consulted here before;
    fanout_appliance_event() read a different, similarly-named table
    (notification_preferences) that has no writers anywhere in this
    codebase, so external delivery was always silently off regardless
    of what a customer configured. The recipient is deliberately the
    address/number the customer saved FOR notifications (prefs['email_
    address']/['phone_number']), never partner_users.email (their
    portal login address) -- a customer may reasonably want alerts sent
    somewhere other than their login inbox.

    Fail-closed throughout, matching this codebase's established
    convention: no saved row, no selected event_types, an unauthorized/
    out-of-scope camera, quiet hours, or the cooldown window all result
    in disabled -- never a guessed default of enabled. Delivery is
    deliberately NOT gated on email_verified_at/phone_verified_at:
    verification here only ever means "a Test message was previously
    delivered successfully" (notification_settings_page.py's test-
    email/test-sms routes), not a code-confirmed address -- requiring
    it first would silently withhold every real alert from a customer
    who enabled notifications but never separately clicked Test,
    stricter than anything asked for."""
    from notification_preferences import get_preferences,resolve_effective_camera_ids
    prefs=get_preferences(db,user_id=user['id'])
    disabled={'email':False,'sms':False,'email_address':'','phone_number':''}
    if not prefs['email_enabled'] and not prefs['sms_enabled']:
        return disabled
    if event_type not in prefs['event_types']:
        return disabled
    if camera_id is not None:
        from camera_access import authorized_camera_ids
        authorized=authorized_camera_ids(db,user_id=user['id'],customer_id=customer_id,role=user['role'],access_mode=user['camera_access_mode'] or 'selected')
        effective=set(resolve_effective_camera_ids(camera_scope=prefs['camera_scope'],camera_ids=prefs['camera_ids'],authorized_camera_ids=authorized))
        if camera_id not in effective:
            return disabled
    if prefs['quiet_hours_enabled'] and _within_quiet_hours(current_time,prefs['quiet_start'],prefs['quiet_end']):
        return disabled
    if _external_channel_recently_notified(db,user_id=user['id'],camera_id=camera_id,event_type=event_type,now=now,exclude_notification_id=notification_id):
        return disabled
    return {
        'email':bool(prefs['email_enabled'] and prefs['email_address']),
        'sms':bool(prefs['sms_enabled'] and prefs['phone_number']),
        'email_address':prefs['email_address'],
        'phone_number':prefs['phone_number'],
    }


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
        notification_id=secrets.token_hex(16); timestamp=str(event.get('timestamp') or now.isoformat()); title=event_type.replace('_',' ').title(); message=str(event.get('message') or f'{title} detected')[:1000]
        notification={'id':notification_id,'title':title,'message':message}
        with connection() as db:
            db.execute('INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,event_type,severity,title,message,timestamp,thumbnail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(notification_id,user['id'],customer_id,site_id,camera_id,event.get('id'),event.get('recording_id') or event.get('linked_recording'),event_type,event.get('severity','info'),title,message,timestamp,event.get('thumbnail'),now.isoformat()))
            external=_external_channels(db,user=user,customer_id=customer_id,camera_id=camera_id,event_type=event_type,current_time=current_time,now=now,notification_id=notification_id)
        recipients={'in_app':'local','email':external['email_address'],'sms':external['phone_number']}
        channels={'in_app':True,'email':external['email'],'sms':external['sms']}
        for channel in ('in_app','email','sms'):
            if not channels.get(channel): continue
            try: result=CHANNELS[channel].send(notification,recipients[channel])
            except Exception as error: result={'channel':channel,'status':'error','provider':'configured','error':str(error)}
            with connection() as db: db.execute('INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,recipient,attempt,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(secrets.token_hex(12),notification_id,channel,result['status'],result.get('provider'),result.get('error'),recipients[channel],1,now.isoformat()))
        created+=1
    return created
