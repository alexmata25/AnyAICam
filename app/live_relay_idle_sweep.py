"""Phase 6e (docs/AI_HANDOFF.md Sec 8): cloud-side idle-relay auto-stop.

Narrowed scope per the approved Phase 6e plan: idle-relay auto-stop,
concurrency/idempotency hardening around it, and failure-state
observability for it only. Reconnect, segment-drop, credential-renewal,
and CDN/HLS-gap behavior (already approved and already built) are
deliberately unchanged -- this module never touches
live_relay_uploader.py, live_playlist.py, or live_cdn_signing.py.

Idle detection has no new heartbeat/polling signal of its own: a camera
is "idle" once it has zero live_view_sessions rows in state 'requested'
-- derived entirely from state live_view_sessions.py already maintains
via its existing start/stop/lazy-expiry transitions.

State lifecycle, tracked in the new live_relay_idle_tracking table (one
row per currently-idle-or-already-handled camera):

  (no row)
     |  camera observed with zero 'requested' sessions (Phase B, idempotent)
     v
  idle_since=<t0>, stop_queued_at=NULL
     |  30s grace period elapses, still zero active sessions (Phase C)
     v
  idle_since=<t0>, stop_queued_at=<t1>   -- exactly one stop_live_relay
     |                                      queued here, ever, for this cycle
     |  row is retained inert -- stop_queued_at IS NOT NULL permanently
     |  excludes this row from every later Phase C claim attempt
     v
  (row persists until either:)
     - a new viewer starts -> start_live_view() deletes the row in the
       same transaction as the new session + start_live_relay command,
       establishing a fresh lifecycle a later idle period can re-track
       from scratch; or
     - Phase C's own claim finds an active viewer that raced ahead of
       Phase A/B's possibly-stale snapshot -> deletes the row itself,
       queuing no stop, so a normal fresh cycle can begin later.

At most one automatic stop_live_relay is ever queued per idle cycle --
guaranteed by the atomic conditional `UPDATE ... WHERE stop_queued_at IS
NULL` claim in Phase C (only the transaction that flips NULL -> non-NULL
wins), not by any application-level locking.
"""

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta

from appliance_protocol import sanitize_appliance_payload
from camera_mapping import resolve_camera_number
from partner_db import audit, connection

logger = logging.getLogger(__name__)

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()

IDLE_GRACE_PERIOD_SECONDS = 30
IDLE_SWEEP_INTERVAL_SECONDS = 10
COMMAND_EXPIRES_MINUTES = 5  # matches live_view_sessions.py's own queued-command TTL

live_relay_idle_sweep_state = {"worker_status": "not_started", "last_tick_at": None, "last_error": None}


def _discover_idle_candidates(db) -> list:
    """Cameras with a live_view_sessions history but currently zero
    active ('requested') sessions. A possibly-stale read -- Phase C's own
    re-check inside its claiming transaction is the real correctness
    boundary, not this one."""
    rows = db.execute(
        "SELECT camera_id FROM live_view_sessions "
        "GROUP BY camera_id "
        "HAVING SUM(CASE WHEN state='requested' THEN 1 ELSE 0 END)=0"
    ).fetchall()
    return [row['camera_id'] for row in rows]


def _track_idle_camera(db, camera_id: str, now: datetime) -> None:
    """Idempotent: a camera already tracked (idle_since already set, with
    or without stop_queued_at) is left untouched -- INSERT OR IGNORE
    never resets an existing clock and never re-opens an already-
    completed idle cycle."""
    camera = db.execute('SELECT appliance_id FROM cameras WHERE id=?', (camera_id,)).fetchone()
    if not camera or not camera['appliance_id']:
        return  # camera deleted/unassigned since its last session -- nothing to track
    db.execute(
        'INSERT OR IGNORE INTO live_relay_idle_tracking(camera_id,appliance_id,idle_since,stop_queued_at) '
        'VALUES(?,?,?,NULL)',
        (camera_id, camera['appliance_id'], now.isoformat()),
    )


def _due_claim_candidates(db, now: datetime) -> list:
    """Every tracked camera whose grace period has elapsed and whose idle
    cycle has not yet produced a stop -- read from the tracking table
    itself, independent of this tick's own (possibly stale, possibly
    narrower) Phase A discovery. A camera that was idle-tracked on an
    earlier tick and has since gained an active viewer is deliberately
    still included here: it is exactly what Phase C's own re-check (in
    _claim_and_stop_if_still_idle) exists to catch and clean up."""
    cutoff = (now - timedelta(seconds=IDLE_GRACE_PERIOD_SECONDS)).isoformat()
    rows = db.execute(
        'SELECT camera_id FROM live_relay_idle_tracking WHERE stop_queued_at IS NULL AND idle_since<=?',
        (cutoff,),
    ).fetchall()
    return [row['camera_id'] for row in rows]


def _queue_stop_live_relay(db, *, appliance_id: str, camera_number: int, camera_id: str, now: datetime) -> None:
    """Same appliance_commands INSERT shape live_view_sessions.py's own
    _queue_relay_command() uses (itself matching appliance_cloud.py's
    queue_command()) -- reimplemented here rather than imported, matching
    this feature area's own established precedent of a small, duplicated
    per-module helper rather than a cross-module import for this exact
    piece of logic."""
    command_id = secrets.token_hex(7)
    expires = now + timedelta(minutes=COMMAND_EXPIRES_MINUTES)
    payload = sanitize_appliance_payload({'camera_number': camera_number, 'camera_id': camera_id})
    db.execute(
        'INSERT INTO appliance_commands(id,appliance_id,command,payload_json,status,created_at,expires_at,created_by) '
        'VALUES(?,?,?,?,?,?,?,?)',
        (command_id, appliance_id, 'stop_live_relay', json.dumps(payload), 'pending', now.isoformat(), expires.isoformat(), 'live-relay-idle-sweep'),
    )


def _claim_and_stop_if_still_idle(db, camera_id: str, now: datetime) -> str:
    """The sole correctness boundary. An atomic conditional UPDATE claims
    the right to queue this idle cycle's one stop_live_relay -- only the
    transaction that flips stop_queued_at from NULL to non-NULL wins;
    every other concurrent or later attempt sees rowcount 0 and does
    nothing. The immediate re-check of live_view_sessions inside this
    same, now-row-locked transaction is what catches a viewer that
    started after Phase A/B's snapshot went stale.

    Returns a short outcome string for tests/observability:
    'not_due', 'active_viewer', 'no_relay_slot', or 'stopped'.
    """
    cutoff = (now - timedelta(seconds=IDLE_GRACE_PERIOD_SECONDS)).isoformat()
    cursor = db.execute(
        'UPDATE live_relay_idle_tracking SET stop_queued_at=? '
        'WHERE camera_id=? AND idle_since<=? AND stop_queued_at IS NULL',
        (now.isoformat(), camera_id, cutoff),
    )
    if cursor.rowcount != 1:
        return 'not_due'  # not yet due, already claimed, or already handled

    active = db.execute(
        "SELECT COUNT(*) AS n FROM live_view_sessions WHERE camera_id=? AND state='requested'",
        (camera_id,),
    ).fetchone()
    if active['n'] > 0:
        # A viewer is active -- Phase A/B's snapshot was stale. Remove the
        # tracking row entirely (not just the claim) so a genuine future
        # idle period is free to start a fresh cycle.
        db.execute('DELETE FROM live_relay_idle_tracking WHERE camera_id=?', (camera_id,))
        return 'active_viewer'

    camera = db.execute('SELECT id,customer_id,appliance_id FROM cameras WHERE id=?', (camera_id,)).fetchone()
    if not camera or not camera['appliance_id']:
        return 'no_relay_slot'  # camera deleted/unassigned -- claim stands, nothing to queue

    camera_number = resolve_camera_number(db, camera_id, camera['appliance_id'], camera['customer_id'])
    if camera_number is None:
        return 'no_relay_slot'  # no assigned relay slot -- claim stands, nothing to queue

    _queue_stop_live_relay(
        db, appliance_id=camera['appliance_id'], camera_number=camera_number, camera_id=camera_id, now=now,
    )
    return 'stopped'


def run_idle_sweep_tick(now: datetime = None) -> None:
    """One full sweep pass: Phase A (discover newly-idle cameras) feeds
    Phase B (start/continue tracking, idempotent); Phase C (claim + stop
    if still idle) then runs separately, over every tracking row that is
    actually due -- read fresh from live_relay_idle_tracking itself, not
    reused from Phase A's candidate list. This matters: a camera tracked
    on an earlier tick that has since gained an active viewer is exactly
    the case Phase A's own discovery query now excludes (it only has
    zero active sessions from a stale point of view once a session is
    active), yet it is still due for Phase C's claim-and-recheck, which
    is what actually cleans up its now-stale tracking row.

    Each camera's Phase B / Phase C step runs in its own short
    transaction, matching this project's existing per-call transaction
    granularity (see live_view_sessions.py) rather than one long
    transaction across every camera."""
    now = now or datetime.now()

    with connection() as db:
        newly_idle = _discover_idle_candidates(db)
    for camera_id in newly_idle:
        with connection() as db:
            _track_idle_camera(db, camera_id, now)

    with connection() as db:
        due = _due_claim_candidates(db, now)
    for camera_id in due:
        with connection() as db:
            outcome = _claim_and_stop_if_still_idle(db, camera_id, now)

        if outcome == 'stopped':
            logger.info('live_relay.idle_stop_queued camera_id=%s idle_seconds=%s', camera_id, IDLE_GRACE_PERIOD_SECONDS)
            audit({'email': 'system', 'role': 'live-relay-idle-sweep'}, 'system.live_view_idle_stopped', 'camera', camera_id, {})


async def live_relay_idle_sweep_worker() -> None:
    """Cloud-side counterpart to live_relay_uploader.py's own
    live_relay_worker() -- same lifespan()-wired, tick-and-sleep shape
    and defensive internal RUNTIME_ROLE re-check, gated to the other side
    of this feature (cloud/combined vs. that worker's edge/combined)."""
    if RUNTIME_ROLE not in {"cloud", "combined"}:
        live_relay_idle_sweep_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    live_relay_idle_sweep_state["worker_status"] = "running"
    logger.info("live_relay.idle_sweep_worker_started")
    while True:
        try:
            await asyncio.to_thread(run_idle_sweep_tick)
            live_relay_idle_sweep_state["last_tick_at"] = datetime.now().isoformat()
            live_relay_idle_sweep_state["last_error"] = None
            await asyncio.sleep(IDLE_SWEEP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            live_relay_idle_sweep_state["last_error"] = str(error)
            logger.warning("live_relay.idle_sweep_iteration_failed error=%s", error)
            await asyncio.sleep(IDLE_SWEEP_INTERVAL_SECONDS)
