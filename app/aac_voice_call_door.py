"""AAC Voice Call -- owner-approved door access (2026-09-23).

Fills in aac_voice_call.py's own request_door_unlock() Phase 5 interface
(prepared but never implemented or called, per Phase 1's explicit
product spec) with a real, explicitly human-confirmed unlock action.

This is deliberately NOT a second door-control mechanism. Authorization
and dispatch both go through the exact same primitives the manual
Live-page "Unlock Door" button already uses:

  - door_access._authorized_door_camera() -- the one place ownership
    (customer_owner implicit; customer_viewer needs an explicit
    can_unlock=1 grant) and door configuration (door_access_enabled,
    a real relay channel assigned) are checked, re-checked here at
    confirm time exactly as door_access.py's own module docstring
    requires ("never trusted from a cached session claim").
  - relay_control.get_provider().trigger() -- the one real dispatch
    path to the underlying Z-Wave/relay hardware.
  - door_access.record_door_access_event() -- the one audit table that
    already covers both the manual and (pre-existing) AACO-triggered
    paths; this module adds a third trigger_type, 'aac_voice_call', and
    populates the new aac_voice_call_event_id column so a door-access
    audit row can be traced back to the specific visitor/call it came
    from, without a second audit table.

The two-step confirmation the product spec requires ("require a clear
confirmation before issuing the real unlock command") is server-
enforced, not UI theater: request_unlock() issues a short-lived,
single-use, hashed-at-rest token (aac_voice_call_unlock_confirmations,
the same shape and atomic-claim discipline as platform_owner.py's own
break_glass_tokens -- see that table's own migration comment for the
TOCTOU race an earlier version of this pattern had and how it was
fixed). confirm_unlock() re-verifies the event's live state AND the
approving user's live can_unlock permission again before consuming the
token and dispatching -- the token proves "this specific request was
made a moment ago by this user", it never substitutes for
re-authorization, and it can never be replayed (atomic single-use
claim) or reused after the event has moved to a terminal state.

Stale-session and transcript-cannot-bypass-authorization, by construction:
  - request_unlock() only accepts events in a non-terminal state
    ('triggered', 'notified', 'answered') -- an 'ended' or 'dismissed'
    call is stale and rejected outright, before a token is ever issued.
  - confirm_unlock() re-checks the SAME state constraint again (a call
    could end between request and confirm) plus re-checks can_unlock
    fresh (a grant could be revoked in that same window).
  - Neither function ever reads transcript_text, intent, or any other
    visitor-supplied/AI-classified field. The approving user's own
    live, database-backed permission is the only input to the
    authorization decision -- there is no code path by which anything
    a visitor said, or anything AACO's classifier inferred, could
    influence whether an unlock is authorized.

Fail-secure: any failure -- unauthorized, wrong tenant, stale call,
expired/already-used/unknown token, or a relay/appliance failure at
dispatch time -- results in a denied/failed audit row and an honest
error response. Nothing here ever reports success without confirming
the underlying relay_control call actually activated.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

import door_access
import relay_control
from partner_db import connection, row

CONFIRMATION_TTL_SECONDS = 60

# Only a call still genuinely in progress may have its door unlocked --
# an 'ended' or 'dismissed' event is exactly the "stale call session"
# the product spec's own regression-test requirement names.
_UNLOCKABLE_STATES = ("triggered", "notified", "answered")


def _hash_token(token: str) -> str:
    # Confirmation tokens are single-use, short-lived, and never
    # displayed/logged after issuance -- a fast, unsalted SHA-256 is
    # sufficient here (unlike a long-lived password/recovery code,
    # there is no meaningful offline-guessing window to defend against
    # a 60-second, single-use, server-generated random token), matching
    # this codebase's own established convention for this exact class
    # of short-lived token elsewhere.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _resolve_acting_user_id(db, *, email: str, customer_id: str) -> str | None:
    user = db.execute(
        "SELECT id FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?",
        (email, customer_id),
    ).fetchone()
    return user["id"] if user else None


def request_unlock(*, event_id: str, customer_id: str, identity: dict) -> dict:
    """Step 1 of 2. Validates the call event (tenant-scoped, not stale)
    and the approving user's real door-unlock authorization for that
    event's camera, then issues a short-lived confirmation token. Does
    NOT dispatch anything to the relay yet -- see confirm_unlock()."""
    with connection() as db:
        event = db.execute(
            "SELECT id,customer_id,camera_id,state FROM aac_voice_call_events WHERE id=? AND customer_id=?",
            (event_id, customer_id),
        ).fetchone()
        if not event:
            raise door_access.HTTPException(status_code=404, detail="AAC Voice Call event not found.")
        if event["state"] not in _UNLOCKABLE_STATES:
            raise door_access.HTTPException(status_code=409, detail="This call has already ended -- door unlock is no longer available.")

        # _authorized_door_camera() raises 404/403/409 exactly matching
        # door_access.py's own manual-unlock route for every failure
        # kind (unknown camera, not a configured door, missing
        # can_unlock grant, no relay assigned) -- propagated unchanged
        # so this route fails the same way for the same reasons.
        camera, user_id = door_access._authorized_door_camera(db, event["camera_id"], identity)

    token = secrets.token_urlsafe(32)
    confirmation_id = uuid.uuid4().hex
    now = datetime.now()
    expires_at = now + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
    with connection() as db:
        db.execute(
            "INSERT INTO aac_voice_call_unlock_confirmations"
            "(id,event_id,customer_id,camera_id,requested_by_user_id,token_hash,created_at,expires_at,used_at) "
            "VALUES(?,?,?,?,?,?,?,?,NULL)",
            (confirmation_id, event_id, customer_id, camera["id"], user_id, _hash_token(token), now.isoformat(), expires_at.isoformat()),
        )

    return {
        "confirmation_id": confirmation_id,
        "confirm_token": token,
        "door_name": camera["name"],
        "expires_in_seconds": CONFIRMATION_TTL_SECONDS,
    }


def confirm_unlock(*, event_id: str, customer_id: str, identity: dict, confirm_token: str) -> dict:
    """Step 2 of 2. Re-verifies everything request_unlock() already
    checked (the event may have ended, or the grant may have been
    revoked, in the window between the two calls), atomically claims
    the confirmation token (single-use; a replayed/reused token is
    rejected, never re-dispatched), and only then calls the real relay.
    Every outcome -- denied, stale, expired/replayed token, relay
    failure, or genuine success -- is audited via
    door_access.record_door_access_event() with trigger_type=
    'aac_voice_call' and aac_voice_call_event_id set, the same table
    and shape the manual and AACO-triggered unlock paths already write
    to."""
    now = datetime.now()

    with connection() as db:
        event = db.execute(
            "SELECT id,customer_id,camera_id,state FROM aac_voice_call_events WHERE id=? AND customer_id=?",
            (event_id, customer_id),
        ).fetchone()
    if not event:
        raise door_access.HTTPException(status_code=404, detail="AAC Voice Call event not found.")
    if event["state"] not in _UNLOCKABLE_STATES:
        raise door_access.HTTPException(status_code=409, detail="This call has already ended -- door unlock is no longer available.")

    try:
        with connection() as db:
            camera, user_id = door_access._authorized_door_camera(db, event["camera_id"], identity)
    except door_access.HTTPException as error:
        with connection() as audit_db:
            door_access.record_door_access_event(
                audit_db, customer_id=customer_id, camera_id=event["camera_id"], door_name="",
                relay_channel=None, trigger_type="aac_voice_call",
                actor_user_id=None, actor_email=identity.get("email"),
                authorization_result="denied" if error.status_code == 403 else "no_door",
                relay_result="skipped", success=False, error=error.detail, now=now,
            )
        raise

    # Atomic single-use claim: the SELECT above (in request_unlock, and
    # implicitly by "does any row with this hash exist" here) can never
    # be the actual claim -- only this UPDATE, gated on used_at IS NULL
    # AND a not-yet-expired token, with the affected-row count as the
    # single source of truth for whether THIS call is the one that
    # consumed it. Two concurrent confirm attempts with the same token
    # (a double-submitted button, a replayed request) can both reach
    # this line; at most one UPDATE ever matches a row.
    token_hash = _hash_token(confirm_token)
    with connection() as db:
        claim = db.execute(
            "UPDATE aac_voice_call_unlock_confirmations SET used_at=? "
            "WHERE event_id=? AND customer_id=? AND camera_id=? AND requested_by_user_id=? "
            "AND token_hash=? AND used_at IS NULL AND expires_at>?",
            (now.isoformat(), event_id, customer_id, camera["id"], user_id, token_hash, now.isoformat()),
        )
        claimed = bool(claim.rowcount)

    if not claimed:
        with connection() as audit_db:
            door_access.record_door_access_event(
                audit_db, customer_id=customer_id, camera_id=camera["id"], door_name=camera["name"],
                relay_channel=camera["door_relay_channel"], trigger_type="aac_voice_call",
                actor_user_id=user_id, actor_email=identity.get("email"),
                authorization_result="authorized", relay_result="skipped", success=False,
                error="Confirmation token invalid, expired, or already used.", now=now,
                aac_voice_call_event_id=event_id,
            )
        raise door_access.HTTPException(status_code=409, detail="This confirmation has expired or was already used -- request a new unlock confirmation.")

    try:
        result = door_access.trigger_door(camera, reason=f"aac_voice_call_unlock:{camera['id']}",
                                          actor=identity.get("email") or "", trigger_type="aac_voice_call",
                                          pulse_ms=camera["door_relay_pulse_ms"])
    except Exception as error:
        with connection() as audit_db:
            door_access.record_door_access_event(
                audit_db, customer_id=customer_id, camera_id=camera["id"], door_name=camera["name"],
                relay_channel=camera["door_relay_channel"], trigger_type="aac_voice_call",
                actor_user_id=user_id, actor_email=identity.get("email"),
                authorization_result="authorized", relay_result="failed", success=False,
                error=str(error), now=now, aac_voice_call_event_id=event_id,
            )
        raise door_access.HTTPException(status_code=502, detail="The door relay could not be reached.") from error

    relay_result = "activated" if result.activated else ("suppressed" if result.suppressed_reason else "failed")
    with connection() as audit_db:
        door_access.record_door_access_event(
            audit_db, customer_id=customer_id, camera_id=camera["id"], door_name=camera["name"],
            relay_channel=camera["door_relay_channel"], trigger_type="aac_voice_call",
            actor_user_id=user_id, actor_email=identity.get("email"),
            authorization_result="authorized", relay_result=relay_result, success=result.activated,
            error=result.suppressed_reason, now=now, aac_voice_call_event_id=event_id,
        )

    if not result.activated:
        detail = "This door was just unlocked -- please wait a moment before trying again." if result.suppressed_reason == "cooldown" else "The door could not be unlocked."
        raise door_access.HTTPException(status_code=409, detail=detail)

    return {"message": f"{camera['name']} unlocked.", "door_name": camera["name"], "channel": result.channel}


def can_unlock_from_call(*, customer_id: str, camera_id: str, identity: dict) -> bool:
    """Read-only check the call screen uses to decide whether to render
    the Unlock Door button at all -- a camera that isn't a configured,
    relay-assigned door, or a viewer without a real can_unlock grant,
    gets no button, matching this codebase's established convention of
    never showing a control that would only ever 403 (see live-tile
    Unlock button, Facial Recognition's own customer-scoping fix
    earlier tonight). This is a UI convenience only; request_unlock()/
    confirm_unlock() above independently re-check the same
    authorization at the moment either route is actually called, never
    trusting this check alone."""
    try:
        with connection() as db:
            door_access._authorized_door_camera(db, camera_id, identity)
        return True
    except door_access.HTTPException:
        return False
