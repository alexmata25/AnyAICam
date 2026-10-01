"""Face Access -- Door/Relay Control (2026-09-17).

Manual "Unlock Door" button handling and the shared door-access audit
trail, per the user's own explicit product specification
(docs/aac-face-access-door-control-requirements.md). Builds on the
Phase 1 AAC foundation (relay_control.py's RelayProvider/RelayRequest/
RelayResult, facial_events.py's automatic-unlock evaluation) rather
than a second, parallel relay-control mechanism -- this module owns
only what Phase 1 didn't: the manual button's own authorization/
execution path, and the one audit table (door_access_events) that
covers BOTH the manual and automatic paths uniformly.

Fail-closed throughout, matching every other permission/entitlement
check in this codebase: a camera that isn't a configured door, a user
without the can_unlock grant, a door with no relay channel assigned,
or any unexpected error during the relay call all result in a denied/
failed outcome -- never a guessed "go ahead". Authorization is
re-checked here, at the moment the command is executed, never trusted
from a cached session claim or from anything a notification/link
carried.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request

import relay_control
from partner_db import audit, connection
from partner_portal import partner_identity


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
        raise HTTPException(status_code=403, detail='Customer account required.')
    return identity


def record_door_access_event(
    db,
    *,
    customer_id: str,
    camera_id: str,
    door_name: str,
    relay_channel: int | None,
    trigger_type: str,
    authorization_result: str,
    relay_result: str,
    success: bool,
    actor_user_id: str | None = None,
    actor_email: str | None = None,
    matched_person_id: str | None = None,
    matched_person_name: str | None = None,
    facial_event_id: str | None = None,
    aac_voice_call_event_id: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> str:
    """The one place a door-access attempt -- manual button press,
    automatic facial-recognition trigger, AACO voice command, or an
    owner-approved AAC Voice Call unlock -- becomes a durable audit
    row. Always writes, even (especially) for a denied/failed attempt:
    the security requirement is "every attempt is auditable", not
    "every successful attempt".

    aac_voice_call_event_id (2026-09-23): the specific visitor/call
    event a trigger_type='aac_voice_call' attempt was approved from --
    None for every other trigger_type, matching facial_event_id's own
    "only set for the one trigger_type it applies to" convention just
    above it.

    now accepts a real datetime OR a plain ISO string -- matching
    facial_events.create_match_event()'s own established `now.isoformat()
    if hasattr(now, "isoformat") else str(now)` convention, since
    facial_events.py's own test suite (and any future caller) may pass
    either."""
    now = now or datetime.now()
    now_text = now.isoformat() if hasattr(now, "isoformat") else str(now)
    event_id = uuid.uuid4().hex[:12]
    db.execute(
        'INSERT INTO door_access_events(id,customer_id,camera_id,door_name,relay_channel,trigger_type,'
        'actor_user_id,actor_email,matched_person_id,matched_person_name,facial_event_id,'
        'aac_voice_call_event_id,authorization_result,relay_result,success,error,created_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            event_id, customer_id, camera_id, door_name, relay_channel, trigger_type,
            actor_user_id, actor_email, matched_person_id, matched_person_name, facial_event_id,
            aac_voice_call_event_id, authorization_result, relay_result, 1 if success else 0, error, now_text,
        ),
    )
    return event_id


def trigger_door(camera: dict, *, reason: str, actor: str, trigger_type: str, pulse_ms: int | None = None,
                 dry_run: bool = False, facial_event_id: str | None = None,
                 person_id: str | None = None) -> relay_control.RelayResult:
    """The one physical dispatch for every door consumer (the Unlock
    button, AACO, AAC Voice Call, facial access rules), 2026-09-25.

    A camera with a door configured in the access-control service
    (access_control.py: a Z-Wave lock, a relay strike/maglock, or the
    simulator) goes through that service -- health, fail-secure checks,
    one command at a time, timed relock, hardware command log. Any other
    door-enabled camera keeps exactly its previous path: a RelayRequest to
    relay_control.get_provider(). Either way the caller gets a RelayResult
    and keeps writing its own door_access_events audit row, as before.

    A dry-run request (facial rules default to dry_run) never reaches
    hardware on either path."""
    import access_control
    service = access_control.get_service()
    door = service.door_for_camera(camera["id"]) if service is not None else None
    if door is None:
        request_obj = relay_control.RelayRequest(
            channel=camera["door_relay_channel"],
            pulse_ms=pulse_ms or camera.get("door_relay_pulse_ms") or relay_control.DEFAULT_PULSE_MS,
            reason=reason, dry_run=dry_run, requested_by=actor,
        )
        return relay_control.get_provider().trigger(request_obj)
    channel = camera.get("door_relay_channel") or 0
    if dry_run:
        return relay_control.RelayResult(channel=channel, activated=False, dry_run=True)
    outcome = service.unlock(door.id, duration_seconds=(pulse_ms / 1000.0) if pulse_ms else None, reason=reason,
                             actor=actor, trigger=trigger_type, person_id=person_id, facial_event_id=facial_event_id)
    if outcome.ok:
        return relay_control.RelayResult(channel=channel, activated=True, dry_run=False)
    # "duplicate" keeps the existing "just unlocked, wait a moment" message.
    suppressed = "cooldown" if outcome.result == "duplicate" else outcome.result
    return relay_control.RelayResult(channel=channel, activated=False, dry_run=outcome.result == "dry_run",
                                     suppressed_reason=suppressed)


class CameraDoorProvider(relay_control.RelayProvider):
    """What facial_events.evaluate_access_rules() is given for a door
    camera: the same RelayProvider interface, routed per camera through
    trigger_door() (automatic trigger: the service additionally refuses
    an unknown/jammed lock state or an offline controller)."""

    def __init__(self, camera: dict, base: relay_control.RelayProvider, *, person_id: str | None = None):
        self.camera = camera
        self.base = base
        self.person_id = person_id

    def capability(self) -> dict:
        return self.base.capability()

    def trigger(self, request: relay_control.RelayRequest) -> relay_control.RelayResult:
        import access_control
        service = access_control.get_service()
        if service is None or service.door_for_camera(self.camera["id"]) is None:
            return self.base.trigger(request)
        reason = request.reason or ""
        facial_event_id = reason.split(":", 1)[1] if reason.startswith("facial_event:") else None
        return trigger_door(self.camera, reason=reason, actor="facial_recognition", trigger_type="automatic",
                            pulse_ms=request.pulse_ms, dry_run=request.dry_run, facial_event_id=facial_event_id,
                            person_id=self.person_id)


def door_camera(db, *, customer_id: str, camera_id: str) -> dict | None:
    """The one door-configured-camera lookup every consumer (the manual
    unlock route below, the live-tile visibility check, Camera Settings)
    shares -- tenant-scoped, and None for anything not door_access_
    enabled=1, so "not a door" and "not this customer's camera" are
    indistinguishable to a caller, matching this codebase's established
    no-oracle convention."""
    row = db.execute(
        'SELECT id,name,door_access_enabled,door_relay_channel,door_relay_pulse_ms FROM cameras '
        'WHERE id=? AND customer_id=?',
        (camera_id, customer_id),
    ).fetchone()
    if not row or not row['door_access_enabled']:
        return None
    return dict(row)


def customer_door_cameras(db, customer_id: str) -> list[dict]:
    """Every door_access_enabled=1 camera for this tenant, in the same
    row shape door_camera() returns for one -- the shared lookup AACO's
    unlock_door command (app/main.py's _ClassicAacoBoundary) uses to
    resolve a display-name/camera-number door token against only this
    customer's real doors, and to detect two doors sharing the same
    display name (an ambiguous match) before ever calling
    _authorized_door_camera() below."""
    rows = db.execute(
        'SELECT id,name,camera_number,door_access_enabled,door_relay_channel,door_relay_pulse_ms FROM cameras '
        'WHERE customer_id=? AND door_access_enabled=1',
        (customer_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _authorized_door_camera(db, camera_id: str, identity: dict) -> dict:
    """Mirrors talk_sessions.py's _authorized_talk_camera() exactly for
    the ownership/permission half (customer_owner has implicit full-
    fleet access; customer_viewer needs an explicit can_unlock=1 grant
    -- a separate permission from can_talk/can_alerts, since unlocking
    a real door is a materially more consequential action than viewing
    or talking), then adds the door-configuration gate: a camera that
    isn't door_access_enabled, or has no relay channel assigned, is
    rejected with a distinct reason either way -- never treated as
    "go ahead"."""
    camera = db.execute(
        'SELECT * FROM cameras WHERE id=? AND customer_id=?',
        (camera_id, identity['customer_id']),
    ).fetchone()
    if not camera:
        raise HTTPException(status_code=404, detail='Camera not found.')
    if not camera['door_access_enabled']:
        raise HTTPException(status_code=404, detail='This camera is not configured for door access.')

    user = db.execute('SELECT id FROM partner_users WHERE email=?', (identity['email'],)).fetchone()
    if not user:
        raise HTTPException(status_code=403, detail='Customer owner permission required.')

    if identity.get('role') != 'customer_owner':
        permission = db.execute(
            'SELECT can_unlock FROM customer_camera_permissions WHERE user_id=? AND camera_id=?',
            (user['id'], camera_id),
        ).fetchone()
        if not permission or not permission['can_unlock']:
            raise HTTPException(status_code=403, detail='Not authorized to unlock this door.')

    if camera['door_relay_channel'] is None:
        raise HTTPException(status_code=409, detail='No relay is configured for this door.')

    return dict(camera), user['id']


def register_door_access_routes(app: FastAPI) -> None:
    @app.post('/api/customer/cameras/{camera_id}/door/unlock')
    def unlock_door(request: Request, camera_id: str) -> dict:
        identity = _customer_identity(request)
        now = datetime.now()
        # Authorization is read in its own, short transaction -- kept
        # separate from the audit writes below on purpose. connect()'s
        # own commit/rollback wrapper (database_backend.py) rolls back
        # EVERYTHING in a `with connection()` block the moment an
        # exception propagates out of it, which would silently erase a
        # denial/failure's own audit row if that row were written in
        # the same transaction as the raise that reports it -- exactly
        # backwards for a security audit trail whose whole point is
        # surviving the failure it's recording. Each audit write below
        # therefore gets its own `with connection()` block, committed
        # before the corresponding HTTPException is raised.
        try:
            with connection() as db:
                camera, user_id = _authorized_door_camera(db, camera_id, identity)
        except HTTPException as error:
            # The camera lookup itself may have failed on an unknown/
            # foreign camera_id, in which case there's no real tenant-
            # scoped door to attribute an audit row to -- intentionally
            # not written here, matching authorize_customer_tenant()'s
            # own no-mutation-on-unknown-id convention elsewhere.
            if error.status_code != 404 or 'not configured for door access' in error.detail:
                with connection() as audit_db:
                    record_door_access_event(
                        audit_db, customer_id=identity['customer_id'], camera_id=camera_id,
                        door_name='', relay_channel=None, trigger_type='manual',
                        actor_user_id=None, actor_email=identity['email'],
                        authorization_result='denied' if error.status_code == 403 else 'no_door',
                        relay_result='skipped', success=False, error=error.detail, now=now,
                    )
            raise
        try:
            result = trigger_door(camera, reason=f"manual_unlock:{camera['id']}", actor=identity['email'],
                                  trigger_type='manual', pulse_ms=camera['door_relay_pulse_ms'])
        except Exception as error:
            with connection() as audit_db:
                record_door_access_event(
                    audit_db, customer_id=identity['customer_id'], camera_id=camera['id'], door_name=camera['name'],
                    relay_channel=camera['door_relay_channel'], trigger_type='manual',
                    actor_user_id=user_id, actor_email=identity['email'],
                    authorization_result='authorized', relay_result='failed', success=False,
                    error=str(error), now=now,
                )
            raise HTTPException(status_code=502, detail='The door relay could not be reached.') from error
        relay_result = 'activated' if result.activated else ('suppressed' if result.suppressed_reason else 'failed')
        with connection() as audit_db:
            record_door_access_event(
                audit_db, customer_id=identity['customer_id'], camera_id=camera['id'], door_name=camera['name'],
                relay_channel=camera['door_relay_channel'], trigger_type='manual',
                actor_user_id=user_id, actor_email=identity['email'],
                authorization_result='authorized', relay_result=relay_result, success=result.activated,
                error=result.suppressed_reason, now=now,
            )
        if not result.activated:
            detail = 'This door was just unlocked -- please wait a moment before trying again.' if result.suppressed_reason == 'cooldown' else 'The door could not be unlocked.'
            raise HTTPException(status_code=409, detail=detail)
        return {'message': f"{camera['name']} unlocked.", 'door_name': camera['name'], 'channel': result.channel}

    @app.get('/api/customer/cameras/{camera_id}/door-config')
    def get_door_config(request: Request, camera_id: str) -> dict:
        # Read access matches every other camera-detail read in this
        # codebase (either role, no can_unlock/can_settings grant
        # required just to SEE the current configuration) -- only
        # mutating it (below) and unlocking (above) require the
        # stronger checks.
        identity = _customer_identity(request)
        with connection() as db:
            camera = db.execute(
                'SELECT id,name,door_access_enabled,door_relay_channel,door_relay_pulse_ms FROM cameras WHERE id=? AND customer_id=?',
                (camera_id, identity['customer_id']),
            ).fetchone()
        if not camera:
            raise HTTPException(status_code=404, detail='Camera not found.')
        return dict(camera)

    @app.post('/api/customer/cameras/{camera_id}/door-config')
    def update_door_config(request: Request, camera_id: str, payload: dict) -> dict:
        # Configuring which camera controls a physical door -- unlike
        # viewing it, or even pressing Unlock once it's configured --
        # is deliberately customer_owner-only, no can_settings/can_
        # unlock delegation: this is the one action that decides
        # whether a relay gets wired to a camera at all, a materially
        # more consequential decision than any single unlock. Fail-
        # closed input validation: an invalid channel/duration is
        # rejected outright rather than silently clamped or ignored --
        # a misconfigured door is a security problem, not a cosmetic
        # one.
        identity = _customer_identity(request)
        if identity.get('role') != 'customer_owner':
            raise HTTPException(status_code=403, detail='Only the account owner can configure door access.')
        enabled = bool(payload.get('door_access_enabled'))
        relay_channel = payload.get('door_relay_channel')
        pulse_ms = payload.get('door_relay_pulse_ms')
        if enabled:
            if relay_channel not in relay_control.VALID_CHANNELS:
                raise HTTPException(status_code=400, detail=f'door_relay_channel must be one of {relay_control.VALID_CHANNELS}.')
            if pulse_ms is not None and (not isinstance(pulse_ms, int) or pulse_ms <= 0):
                raise HTTPException(status_code=400, detail='door_relay_pulse_ms must be a positive number of milliseconds.')
        else:
            # Disabling a door never needs a valid channel/duration --
            # the whole point is turning door control off. Cleared
            # (not merely ignored) so a later GET never reports a
            # stale channel for a camera that is no longer a door.
            relay_channel = None
            pulse_ms = None
        with connection() as db:
            camera = db.execute('SELECT id,name FROM cameras WHERE id=? AND customer_id=?', (camera_id, identity['customer_id'])).fetchone()
            if not camera:
                raise HTTPException(status_code=404, detail='Camera not found.')
            db.execute(
                'UPDATE cameras SET door_access_enabled=?,door_relay_channel=?,door_relay_pulse_ms=? WHERE id=?',
                (1 if enabled else 0, relay_channel, pulse_ms, camera_id),
            )
        audit(
            identity, 'camera.door_access_configured', 'camera', camera_id,
            {'door_access_enabled': enabled, 'door_relay_channel': relay_channel, 'door_relay_pulse_ms': pulse_ms},
        )
        return {
            'message': f"Door access {'enabled' if enabled else 'disabled'} for {camera['name']}.",
            'door_access_enabled': enabled, 'door_relay_channel': relay_channel, 'door_relay_pulse_ms': pulse_ms,
        }

    @app.get('/api/customer/cameras/{camera_id}/door-config/unlock-access')
    def get_unlock_access(request: Request, camera_id: str) -> dict:
        """Camera Settings' own "Viewer access" list -- every customer_
        viewer this account has, and whether each currently holds
        can_unlock=1 for this specific camera. Owner-only, matching every
        other door-config write/read below: managing WHO may unlock a
        real physical door is not something a viewer inspects about
        themselves or anyone else here."""
        identity = _customer_identity(request)
        if identity.get('role') != 'customer_owner':
            raise HTTPException(status_code=403, detail='Only the account owner can manage unlock access.')
        with connection() as db:
            camera = db.execute(
                'SELECT id FROM cameras WHERE id=? AND customer_id=?', (camera_id, identity['customer_id']),
            ).fetchone()
            if not camera:
                raise HTTPException(status_code=404, detail='Camera not found.')
            viewers = db.execute(
                "SELECT u.id AS user_id, u.email, u.name, COALESCE(p.can_unlock,0) AS can_unlock "
                "FROM partner_users u LEFT JOIN customer_camera_permissions p ON p.user_id=u.id AND p.camera_id=? "
                "WHERE u.customer_id=? AND u.role='customer_viewer' ORDER BY u.email",
                (camera_id, identity['customer_id']),
            ).fetchall()
        return {'viewers': [dict(row) for row in viewers]}

    @app.post('/api/customer/cameras/{camera_id}/door-config/unlock-access')
    def set_unlock_access(request: Request, camera_id: str, payload: dict) -> dict:
        """Full-replace semantics for can_unlock ACROSS THIS ONE CAMERA
        ONLY, scoped to this customer's own viewers -- an id in
        `user_ids` that isn't actually one of this customer's own
        customer_viewer users is silently ignored (never trusted from
        the request as a real user to grant or deny), matching this
        codebase's fail-closed, no-cross-tenant-effect convention
        elsewhere. Deliberately never touches can_live/can_playback/
        can_download/can_share/can_alerts/can_settings/can_talk on any
        row -- see camera_access.set_camera_access()'s own docstring for
        the real bug this same care avoids repeating (an unrelated
        change silently resetting an already-granted permission)."""
        identity = _customer_identity(request)
        if identity.get('role') != 'customer_owner':
            raise HTTPException(status_code=403, detail='Only the account owner can manage unlock access.')
        requested_user_ids = {str(item) for item in payload.get('user_ids', [])}
        with connection() as db:
            camera = db.execute(
                'SELECT id,name FROM cameras WHERE id=? AND customer_id=?', (camera_id, identity['customer_id']),
            ).fetchone()
            if not camera:
                raise HTTPException(status_code=404, detail='Camera not found.')
            valid_viewer_ids = {
                row['id'] for row in db.execute(
                    "SELECT id FROM partner_users WHERE customer_id=? AND role='customer_viewer'",
                    (identity['customer_id'],),
                ).fetchall()
            }
            granted_user_ids = requested_user_ids & valid_viewer_ids
            for viewer_id in valid_viewer_ids:
                if viewer_id in granted_user_ids:
                    db.execute(
                        'INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_download,can_share,can_alerts,can_settings,can_talk,can_unlock) '
                        'VALUES(?,?,0,0,0,0,0,0,0,1) '
                        'ON CONFLICT(user_id,camera_id) DO UPDATE SET can_unlock=1',
                        (viewer_id, camera_id),
                    )
                else:
                    db.execute(
                        'UPDATE customer_camera_permissions SET can_unlock=0 WHERE user_id=? AND camera_id=?',
                        (viewer_id, camera_id),
                    )
        audit(
            identity, 'camera.door_unlock_access_updated', 'camera', camera_id,
            {'granted_user_ids': sorted(granted_user_ids)},
        )
        return {
            'message': f"Unlock access updated for {camera['name']}.",
            'granted_user_ids': sorted(granted_user_ids),
        }
