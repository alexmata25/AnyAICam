"""Durable, customer-scoped mobile push for the shared notification engine.

Enrolling a device opts that device in. Event/camera/quiet-hours choices come
from existing customer notification settings, independently of email/SMS toggles.
"""
import asyncio
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from partner_db import connection
import mobile_push_provider as provider

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def eligible(db, notification, *, now):
    from camera_access import authorized_camera_ids
    from notification_preferences import get_preferences, resolve_effective_camera_ids
    from notification_engine import EMERGENCY_EVENT_TYPES, _quiet_hours_clock, _within_quiet_hours
    user = db.execute("SELECT * FROM partner_users WHERE id=? AND customer_id=? AND approved=1 AND account_status='active' AND role IN ('customer_owner','customer_viewer')",
                      (notification['user_id'], notification['customer_id'])).fetchone()
    if not user:
        return False
    prefs = get_preferences(db, user_id=user['id'])
    camera = notification['camera_id']
    if camera:
        cameras = authorized_camera_ids(db, user_id=user['id'], customer_id=notification['customer_id'], role=user['role'], access_mode=user['camera_access_mode'] or 'selected')
        if camera not in resolve_effective_camera_ids(camera_scope=prefs['camera_scope'], camera_ids=prefs['camera_ids'], authorized_camera_ids=cameras):
            return False
        if user['role'] == 'customer_viewer':
            permissions = db.execute('SELECT camera_id,can_alerts FROM customer_camera_permissions WHERE user_id=?', (user['id'],)).fetchall()
            if permissions and camera not in {p['camera_id'] for p in permissions if p['can_alerts']}:
                return False
    if notification['event_type'] in EMERGENCY_EVENT_TYPES:
        return True
    if notification['event_type'] not in prefs['event_types']:
        return False
    return not (prefs['quiet_hours_enabled'] and _within_quiet_hours(
        _quiet_hours_clock(now.replace(tzinfo=timezone.utc)), prefs['quiet_start'], prefs['quiet_end']))


# One push per visit (2026-09-30, owner-approved after real staging tests). One person
# walking past a camera produced person + motion + smart_motion + PPE pushes (4-5 buzzes),
# and per-camera grouping still gave 4-5 because two driveway cameras saw the same walk
# and a fixed window restarted mid-walk. Now:
# - A visit is ordinary activity from any camera at the same site (the camera itself
#   when it has no site), lasting while activity continues with gaps under
#   VISIT_GAP_SECONDS, capped at VISIT_MAX_SECONDS so continuous activity still
#   reminds periodically.
# - Ordinary activity is held ACTIVITY_HOLD_SECONDS so the most important type of the
#   visit can arrive first; at most VISIT_MAX_PUSHES per visit (the first push plus one
#   upgrade to a clearly more important type).
# - Visitor Calls and intrusion alarms are never held or grouped, and they absorb the
#   visit's ordinary pushes. System alerts are not grouped. In-app, email and SMS are
#   unaffected. A grouped event keeps a 'skipped'/'grouped_into_visit' outbox row, so
#   the delivery history explains it and the sliding window can see the activity.
ACTIVITY_RANK = {
    'motion': 1, 'ppe': 1, 'people_counting': 1, 'occupancy': 1,
    'smart_motion': 2,
    'person': 3, 'vehicle': 3, 'lpr': 3, 'line_crossing': 3, 'intrusion': 3, 'loitering': 3, 'facial_recognition': 3,
}
VISIT_RANK = {**ACTIVITY_RANK, 'aac_voice_call': 4, 'intrusion_alarm': 5}
ACTIVITY_HOLD_SECONDS = 5
VISIT_GAP_SECONDS = 60
VISIT_MAX_SECONDS = 600
VISIT_MAX_PUSHES = 2
_ACTIVE = ('pending', 'retry', 'sending', 'sent', 'unavailable')


def _visit_scope(n):
    if n['site_id']:
        return 'site_id', n['site_id']
    if n['camera_id']:
        return 'camera_id', n['camera_id']
    return None


def _visit_rows(db, n, *, since, until):
    """This user's visit-relevant outbox rows in the same scope (any device)."""
    column, value = _visit_scope(n)
    types = tuple(VISIT_RANK)
    marks = ','.join('?' * len(types))
    return db.execute(
        f"SELECT DISTINCT n.id AS nid, n.event_type, p.status, p.created_at FROM mobile_push_outbox p "
        f"JOIN notifications n ON n.id=p.notification_id WHERE n.user_id=? AND n.customer_id=? AND n.{column}=? "
        f"AND n.event_type IN ({marks}) AND p.created_at>=? AND p.created_at<=? AND n.id!=?",
        (n['user_id'], n['customer_id'], value, *types, since, until, n['id'])).fetchall()


def _visit_state(db, n, now):
    """(highest rank pushed or queued, pushes) for the visit that is still going at `now`."""
    rows = _visit_rows(db, n, since=(now-timedelta(seconds=VISIT_MAX_SECONDS)).isoformat(), until=now.isoformat())
    start = now
    for created in sorted({datetime.fromisoformat(r['created_at']) for r in rows}, reverse=True):
        if (start - created).total_seconds() > VISIT_GAP_SECONDS:
            break
        start = created
    active = [r for r in rows if r['status'] in _ACTIVE and datetime.fromisoformat(r['created_at']) >= start]
    return max((VISIT_RANK[r['event_type']] for r in active), default=0), len({r['nid'] for r in active})


def enqueue(db, notification_id, *, now=None):
    """Called in the notification INSERT transaction; network-free and atomic."""
    from notification_engine import EMERGENCY_EVENT_TYPES, NOTIFICATION_CHANNEL_COOLDOWN_SECONDS, VOICE_CALL_CHANNEL_COOLDOWN_SECONDS
    now = now or utcnow()
    n = db.execute('SELECT * FROM notifications WHERE id=?', (notification_id,)).fetchone()
    if not n or not eligible(db, n, now=now):
        return 0
    rank = ACTIVITY_RANK.get(n['event_type'])
    grouped_visit = bool(rank and _visit_scope(n))
    grouped = False
    if grouped_visit:
        top, pushes = _visit_state(db, n, now)
        grouped = top >= rank or pushes >= VISIT_MAX_PUSHES
    urgent = n['event_type'] in EMERGENCY_EVENT_TYPES
    cooldown = VOICE_CALL_CHANNEL_COOLDOWN_SECONDS if n['event_type'] == 'aac_voice_call' else NOTIFICATION_CHANNEL_COOLDOWN_SECONDS
    if not grouped and not urgent and cooldown > 0:
        recent = db.execute("SELECT 1 FROM mobile_push_outbox p JOIN notifications n ON n.id=p.notification_id WHERE n.user_id=? AND n.customer_id=? AND COALESCE(n.site_id,'')=? AND COALESCE(n.camera_id,'')=? AND n.event_type=? AND p.created_at>=? AND n.id!=? AND p.status NOT IN ('skipped','expired') LIMIT 1",
            (n['user_id'], n['customer_id'], n['site_id'] or '', n['camera_id'] or '', n['event_type'], (now-timedelta(seconds=cooldown)).isoformat(), n['id'])).fetchone()
        if recent:
            return 0
    # Incoming event IDs deduplicate across repeat fanouts, including emergency alarms.
    identity = [n['customer_id'], n['site_id'], n['camera_id'], n['event_type'], n['event_id'] or n['id']]
    import json
    event_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    ttl = 120 if n['event_type'] == 'aac_voice_call' else (300 if urgent else 3600)
    devices = db.execute('SELECT id FROM mobile_push_devices WHERE user_id=? AND customer_id=? AND enabled=1', (n['user_id'], n['customer_id'])).fetchall()
    # Ordinary activity waits briefly so a more important type from the same visit can supersede it.
    send_at = now + timedelta(seconds=ACTIVITY_HOLD_SECONDS) if grouped_visit else now
    status, error = ('skipped', 'grouped_into_visit') if grouped else ('pending', None)
    count = 0
    for device in devices:
        result = db.execute("INSERT INTO mobile_push_outbox(id,notification_id,device_id,event_key,status,next_at,expires_at,created_at,error) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,event_key) DO NOTHING",
            (secrets.token_hex(16), n['id'], device['id'], event_key, status, send_at.isoformat(), (now+timedelta(seconds=ttl)).isoformat(), now.isoformat(), error))
        count += 0 if grouped else result.rowcount
    return count


def drain(*, now=None, limit=100, urgent_only=None):
    """Bounded scan; atomic leases prevent concurrent workers sending one row.

    Success means provider acceptance, not handset delivery. Crash-after-send can
    repeat a notification: client must deduplicate its stable notification_id.
    """
    fixed_now = now
    now = now or utcnow()
    urgency = '' if urgent_only is None else (" AND n.event_type IN ('intrusion_alarm','aac_voice_call')" if urgent_only else " AND n.event_type NOT IN ('intrusion_alarm','aac_voice_call')")
    with connection() as db:
        jobs = db.execute("SELECT p.id FROM mobile_push_outbox p JOIN notifications n ON n.id=p.notification_id WHERE ((p.status IN ('pending','retry','unavailable') AND p.next_at<=?) OR (p.status='sending' AND p.lease_until<=?))" + urgency + " ORDER BY CASE WHEN n.event_type='intrusion_alarm' THEN 0 WHEN n.event_type='aac_voice_call' THEN 1 ELSE 2 END,p.created_at LIMIT ?", (now.isoformat(), now.isoformat(), limit)).fetchall()
    stats = {'attempted': 0, 'sent': 0}
    for item in jobs:
        now = fixed_now or utcnow()
        claim = secrets.token_hex(16)
        with connection() as db:
            claimed = db.execute("UPDATE mobile_push_outbox SET status='sending',claim_id=?,lease_until=? WHERE id=? AND ((status IN ('pending','retry','unavailable') AND next_at<=?) OR (status='sending' AND lease_until<=?))",
                (claim, (now+timedelta(minutes=5)).isoformat(), item['id'], now.isoformat(), now.isoformat()))
            if claimed.rowcount != 1:
                continue
            job = dict(db.execute('SELECT * FROM mobile_push_outbox WHERE id=?', (item['id'],)).fetchone())
            n = db.execute('SELECT * FROM notifications WHERE id=?', (job['notification_id'],)).fetchone()
            d = db.execute('SELECT * FROM mobile_push_devices WHERE id=?', (job['device_id'],)).fetchone()
            allowed = bool(n and d and d['enabled'] and d['user_id']==n['user_id'] and d['customer_id']==n['customer_id'] and eligible(db, n, now=now))
            grouped = False
            if allowed and job['attempt'] == 0 and ACTIVITY_RANK.get(n['event_type']) and _visit_scope(n):
                # Superseded while held: a more important push from the same visit
                # (queued up to the window after this one, or sent shortly before it).
                created = datetime.fromisoformat(job['created_at'])
                nearby = _visit_rows(db, n, since=(created-timedelta(seconds=VISIT_GAP_SECONDS)).isoformat(),
                    until=(created+timedelta(seconds=VISIT_GAP_SECONDS)).isoformat())
                grouped = max((VISIT_RANK[r['event_type']] for r in nearby if r['status'] in _ACTIVE), default=0) > ACTIVITY_RANK[n['event_type']]
        attempt = job['attempt']
        if now.isoformat() >= job['expires_at']:
            result = {'status': 'expired'}
        elif not allowed:
            result = {'status': 'skipped'}
        elif grouped:
            result = {'status': 'skipped', 'error': 'grouped_into_visit'}
        else:
            stats['attempted'] += 1
            attempt += 1
            try:
                result = provider.send(dict(d), dict(n), ttl_seconds=int((datetime.fromisoformat(job['expires_at'])-now).total_seconds()))
            except Exception:
                result = {'status': 'retry', 'error': 'provider_exception'}
        status = result['status']
        if status == 'retry' and attempt >= 5:
            status = 'failed'
        delay = min(900, 60 * 2 ** min(attempt, 4))
        if status == 'retry' and n and n['event_type'] in {'intrusion_alarm', 'aac_voice_call'}:
            delay = min(60, 5 * 2 ** max(0, attempt - 1))
            if result.get('error') == 'provider_throttled':
                delay = max(60, delay)  # FCM quota backoff must be at least one minute.
        with connection() as db:
            db.execute('UPDATE mobile_push_outbox SET status=?,attempt=?,next_at=?,lease_until=NULL,claim_id=NULL,error=?,provider_id=? WHERE id=? AND claim_id=?',
                (status, attempt, (now+timedelta(seconds=delay)).isoformat(), result.get('error'), result.get('provider_id'), job['id'], claim))
            if status == 'invalid_token':
                # A token refreshed while the send was in flight must survive.
                db.execute('UPDATE mobile_push_devices SET enabled=0 WHERE id=? AND token=?', (d['id'], d['token']))
        stats['sent'] += status == 'sent'
    return stats


async def _delivery_loop(urgent_only):
    while True:
        try:
            await asyncio.to_thread(drain, urgent_only=urgent_only)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('push scan failed: %s', type(exc).__name__)
        await asyncio.sleep(1)


async def worker():
    if os.getenv('ANYAICAM_RUNTIME_ROLE', 'edge').lower() not in {'cloud', 'combined'}:
        return
    # A slow provider request for routine activity cannot occupy the alarm lane.
    await asyncio.gather(_delivery_loop(True), _delivery_loop(False))
