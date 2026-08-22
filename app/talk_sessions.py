"""Talk-down capability foundation: customer-facing push-to-talk session
start/stop routes. Deliberately stops at the transport boundary -- see
the module-level note on _queue_relay_command below (there isn't one).
No audio byte is ever sent anywhere by this module. Its entire job is
to prove and enforce the one thing that must be correct before any
real transport work begins: a customer can only start a talk session
for a camera they are authorized to view/control AND that camera has
been independently confirmed (via server-persisted capability data,
never a client-supplied claim) to support talk-down at all.

Modeled directly on live_view_sessions.py's start/stop pattern
(authorization, lazy expiry-sweep, idempotent stop) -- deliberately
duplicated rather than shared, matching that module's own documented
scope decision. The one structural difference: live_view_sessions.py
queues an appliance_commands row to actually start a relay; this
module queues nothing, because there is no relay to start yet. When
real RTSP/ISAPI transport is implemented in a later milestone, that is
the one piece this module is missing -- everything else (auth, tenant
isolation, capability gating, session lifecycle) is already here.

customer_talk_sessions is a new table (see db_migrations.py's
20260821_talk_down_sessions), independent of live_view_sessions --
a talk session and a live-view session are different lifecycles (a
customer can watch without talking, and -- once transport exists --
briefly talk while a separate live-view session is also open).
"""

import secrets
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request

from partner_db import audit, connection
from partner_portal import partner_identity

# A generous ceiling for a single press-and-hold, not the expected
# duration -- the frontend's own release/cancel/pointerleave stop call
# ends a session in practice within a few seconds. This is the backstop
# for a browser crash/lost connection that never sends the stop call.
TALK_SESSION_DURATION_SECONDS = 60


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
        raise HTTPException(status_code=403, detail='Customer account required.')
    return identity


def _authorized_talk_camera(db, camera_id: str, identity: dict) -> dict:
    """Camera lookup + can_talk permission check + talk-down capability
    check, scoped to the authenticated customer. Mirrors
    live_view_sessions.py's _authorized_camera() exactly for the
    ownership/permission half (customer_owner has implicit full-fleet
    access, customer_viewer needs an explicit can_talk=1 grant --
    deliberately a SEPARATE permission from can_live, since talk-down
    produces real-world audible sound at the customer's premises and is
    a materially more consequential capability than viewing), then adds
    the capability gate live_view_sessions.py has no equivalent of:
    talk_down_supported must be exactly 1 (confirmed supported) -- NULL
    (never verified) and 0 (confirmed unsupported) are both rejected,
    with different error messages so the frontend can show the right
    one, but neither is ever treated as "go ahead"."""
    camera = db.execute(
        'SELECT * FROM cameras WHERE id=? AND customer_id=?',
        (camera_id, identity['customer_id']),
    ).fetchone()
    if not camera:
        raise HTTPException(status_code=404, detail='Camera not found.')

    user = db.execute(
        'SELECT id FROM partner_users WHERE email=?',
        (identity['email'],),
    ).fetchone()
    if not user:
        raise HTTPException(status_code=403, detail='Customer owner permission required.')

    if identity.get('role') != 'customer_owner':
        permission = db.execute(
            'SELECT can_talk FROM customer_camera_permissions WHERE user_id=? AND camera_id=?',
            (user['id'], camera_id),
        ).fetchone()
        if not permission or not permission['can_talk']:
            raise HTTPException(status_code=403, detail='Not authorized to talk to this camera.')

    supported = camera['talk_down_supported']
    if supported != 1:
        detail = 'Talk-down capability not verified for this camera.' if supported is None else 'Talk-down is not supported by this camera.'
        raise HTTPException(status_code=409, detail=detail)

    return {**dict(camera), 'user_id': user['id']}


def _sweep_expired_sessions(db, now: datetime) -> None:
    """Lazy expiry, identical pattern to live_view_sessions.py's own
    sweep: scoped to state='requested' only, so an already-'stopped'
    row (a customer-initiated, terminal outcome) is never overwritten."""
    db.execute(
        "UPDATE customer_talk_sessions SET state='expired' WHERE state='requested' AND expires_at<?",
        (now.isoformat(),),
    )


def register_talk_session_routes(app: FastAPI) -> None:
    @app.post('/api/customer/cameras/{camera_id}/talk/start')
    def start_talk_session(request: Request, camera_id: str) -> dict:
        identity = _customer_identity(request)
        now = datetime.now()
        with connection() as db:
            _sweep_expired_sessions(db, now)
            camera = _authorized_talk_camera(db, camera_id, identity)

            session_id = secrets.token_hex(12)
            expires = now + timedelta(seconds=TALK_SESSION_DURATION_SECONDS)
            db.execute(
                'INSERT INTO customer_talk_sessions(id,customer_id,site_id,camera_id,user_id,requested_by,role,state,'
                'requested_at,ended_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (
                    session_id, identity['customer_id'], camera['site_id'], camera_id, camera['user_id'],
                    identity['email'], identity['role'], 'requested', now.isoformat(), None, expires.isoformat(),
                ),
            )

        audit(identity, 'customer.talk_session_started', 'customer_talk_session', session_id, {'camera_id': camera_id})
        return {'session_id': session_id, 'status': 'requested', 'expires_at': expires.isoformat()}

    @app.post('/api/customer/talk/sessions/{session_id}/stop')
    def stop_talk_session(request: Request, session_id: str) -> dict:
        identity = _customer_identity(request)
        now = datetime.now()
        with connection() as db:
            _sweep_expired_sessions(db, now)

            session = db.execute(
                'SELECT * FROM customer_talk_sessions WHERE id=? AND customer_id=?',
                (session_id, identity['customer_id']),
            ).fetchone()
            if not session:
                raise HTTPException(status_code=404, detail='Talk session not found.')

            if session['state'] != 'requested':
                # Idempotent no-op for an already-terminal session, matching
                # live_view_sessions.py's own duplicate-stop-is-a-200 pattern.
                return {'session_id': session_id, 'status': session['state']}

            db.execute(
                "UPDATE customer_talk_sessions SET state='stopped',ended_at=? WHERE id=?",
                (now.isoformat(), session_id),
            )

        audit(identity, 'customer.talk_session_stopped', 'customer_talk_session', session_id, {'camera_id': session['camera_id']})
        return {'session_id': session_id, 'status': 'stopped'}
