"""Phase 6e (docs/AI_HANDOFF.md Sec 8): cloud-side idle-relay auto-stop.

Narrowed scope per the approved Phase 6e plan: idle-relay auto-stop,
concurrency/idempotency hardening around it, and failure-state
observability for it only. Reconnect, segment-drop, credential-renewal,
and CDN/HLS-gap behavior (already approved and already built) are
deliberately unchanged -- this module never touches
live_relay_uploader.py, live_cdn_signing.py, or the appliance side.

VIEWER DEMAND (2026-09-24 fix). A camera's relay upload is wanted only
while at least one live_view_sessions row has *relay demand*:

    state='requested' AND expires_at > now AND
    (requested_at within RELAY_STARTUP_GRACE_SECONDS   -- the P2P/relay race
     OR last_seen_at within VIEWER_ACTIVITY_TIMEOUT_SECONDS)  -- relay heartbeat

last_seen_at is refreshed by record_relay_viewer_activity(), called from
every relay playlist fetch (live_playlist.py) -- HLS players re-fetch the
live playlist every segment (~2s), so a viewer actually watching relay
video keeps it fresh, while a closed/crashed tab, or a viewer that won
over P2P/WireGuard (never fetches the relay playlist), stops refreshing
it and releases the relay within the timeout.

Root cause this replaces: demand used to be "any row in state
'requested'", ignoring expires_at -- and 'requested' rows are only ever
flipped to 'expired' lazily, inside the start/stop routes. A viewer that
left without a stop call therefore held the camera "active" forever
unless somebody else happened to start/stop a view, and a P2P-connected
viewer held the relay open for the whole 30-minute session. Measured on
staging: ~4,000 relay segment uploads/hour around the clock with no
viewer playlist activity at all.

State lifecycle, tracked in live_relay_idle_tracking (one row per
currently-idle-or-already-handled camera):

  (no row)
     |  camera observed with no relay demand (Phase B, idempotent)
     v
  idle_since=<t0>, stop_queued_at=NULL
     |  30s grace period elapses, still no demand (Phase C)
     v
  idle_since=<t0>, stop_queued_at=<t1>   -- one stop_live_relay queued
     |
     |  segments still arriving RESTOP_AFTER_SECONDS after the stop with
     |  still no demand (stop missed/expired undelivered, appliance
     |  restarted with a stale "active" state) -> Phase D re-queues one
     |  more stop and moves stop_queued_at forward (at most one per
     |  RESTOP_AFTER_SECONDS)
     v
  (row persists until either:)
     - a new viewer starts -> start_live_view() deletes the row in the
       same transaction as the new session + start_live_relay command;
     - a viewer with a live session fetches the relay playlist after the
       relay was stopped -> record_relay_viewer_activity() deletes the row
       and queues start_live_relay (resume, e.g. a tab returning from the
       background); or
     - Phase C's own claim finds demand that raced ahead of Phase A/B's
       possibly-stale snapshot -> deletes the row itself, queuing no stop.

Every stop/start queue is guarded by an atomic conditional UPDATE/DELETE
on the tracking row (only the transaction that changes it wins), never by
application-level locking.
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

# A just-started session counts as relay demand for this long even before
# its first playlist fetch -- covers the browser's parallel P2P/relay race
# (the relay must be running for the relay attempt to succeed at all).
RELAY_STARTUP_GRACE_SECONDS = 60
# A relay viewer that hasn't fetched the playlist for this long is gone
# (tab closed/crashed/backgrounded, or it switched to P2P/WireGuard).
VIEWER_ACTIVITY_TIMEOUT_SECONDS = 45
# record_relay_viewer_activity() writes at most once per viewer session per
# this interval, however often the player polls (~every 2s).
HEARTBEAT_WRITE_INTERVAL_SECONDS = 10
# An actively-watching viewer's session lease is extended to at least this
# far ahead on each heartbeat write -- same value as live_view_sessions.
# SESSION_DURATION_SECONDS, so a viewer watching longer than one session
# window never loses their stream mid-view.
SESSION_LEASE_SECONDS = 1800
# How long after a queued stop the relay may still be seen uploading before
# another stop is queued (command delivery is poll-based, ~1 minute).
RESTOP_AFTER_SECONDS = 120
# Cameras whose segments arrived this recently are "currently uploading".
SEGMENT_ACTIVITY_WINDOW_SECONDS = 60

live_relay_idle_sweep_state = {"worker_status": "not_started", "last_tick_at": None, "last_error": None}


def _demand_condition(now: datetime) -> tuple[str, tuple]:
    """SQL predicate (+ params) for one live_view_sessions row having
    relay demand at `now` -- see the module docstring."""
    return (
        "state='requested' AND expires_at>? AND (requested_at>=? OR last_seen_at>=?)",
        (
            now.isoformat(),
            (now - timedelta(seconds=RELAY_STARTUP_GRACE_SECONDS)).isoformat(),
            (now - timedelta(seconds=VIEWER_ACTIVITY_TIMEOUT_SECONDS)).isoformat(),
        ),
    )


def camera_has_relay_demand(db, camera_id: str, now: datetime) -> bool:
    condition, params = _demand_condition(now)
    row = db.execute(
        f"SELECT COUNT(*) AS n FROM live_view_sessions WHERE camera_id=? AND {condition}",
        (camera_id, *params),
    ).fetchone()
    return row['n'] > 0


def _expire_stale_sessions(db, now: datetime) -> None:
    """The same UPDATE live_view_sessions.py's _sweep_expired_sessions()
    runs lazily on start/stop, run here on every tick too, so an expired
    session's state is correct even when nobody starts or stops a view.
    Only ever moves 'requested' -> 'expired'; a customer's 'stopped' is
    terminal and never touched."""
    db.execute(
        "UPDATE live_view_sessions SET state='expired',stopped_at=? WHERE state='requested' AND expires_at<?",
        (now.isoformat(), now.isoformat()),
    )


def _discover_idle_candidates(db, now: datetime, uploading_camera_ids=()) -> list:
    """Cameras with a live_view_sessions history -- plus any camera whose
    relay segments are currently arriving, even with no session history
    in this database -- that have no relay demand right now. A possibly-
    stale read; Phase C's own re-check inside its claiming transaction is
    the real correctness boundary."""
    condition, params = _demand_condition(now)
    rows = db.execute(
        "SELECT camera_id FROM live_view_sessions "
        "GROUP BY camera_id "
        f"HAVING SUM(CASE WHEN {condition} THEN 1 ELSE 0 END)=0",
        params,
    ).fetchall()
    candidates = [row['camera_id'] for row in rows]
    for camera_id in uploading_camera_ids:
        if camera_id not in candidates and not camera_has_relay_demand(db, camera_id, now):
            candidates.append(camera_id)
    return candidates


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
    earlier tick and has since gained demand is deliberately still
    included here: it is exactly what Phase C's own re-check (in
    _claim_and_stop_if_still_idle) exists to catch and clean up."""
    cutoff = (now - timedelta(seconds=IDLE_GRACE_PERIOD_SECONDS)).isoformat()
    rows = db.execute(
        'SELECT camera_id FROM live_relay_idle_tracking WHERE stop_queued_at IS NULL AND idle_since<=?',
        (cutoff,),
    ).fetchall()
    return [row['camera_id'] for row in rows]


def _queue_relay_command(db, *, command: str, appliance_id: str, camera_number: int, camera_id: str, now: datetime, created_by: str) -> None:
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
        (command_id, appliance_id, command, json.dumps(payload), 'pending', now.isoformat(), expires.isoformat(), created_by),
    )


def _queue_stop_live_relay(db, *, appliance_id: str, camera_number: int, camera_id: str, now: datetime) -> None:
    _queue_relay_command(
        db, command='stop_live_relay', appliance_id=appliance_id, camera_number=camera_number,
        camera_id=camera_id, now=now, created_by='live-relay-idle-sweep',
    )


def _relay_slot(db, camera_id: str):
    """(appliance_id, camera_number) for a camera's relay, or None."""
    camera = db.execute('SELECT id,customer_id,appliance_id FROM cameras WHERE id=?', (camera_id,)).fetchone()
    if not camera or not camera['appliance_id']:
        return None
    camera_number = resolve_camera_number(db, camera_id, camera['appliance_id'], camera['customer_id'])
    if camera_number is None:
        return None
    return camera['appliance_id'], camera_number


def _claim_and_stop_if_still_idle(db, camera_id: str, now: datetime) -> str:
    """The sole correctness boundary. An atomic conditional UPDATE claims
    the right to queue this idle cycle's stop_live_relay -- only the
    transaction that flips stop_queued_at from NULL to non-NULL wins;
    every other concurrent or later attempt sees rowcount 0 and does
    nothing. The immediate re-check of relay demand inside this same,
    now-row-locked transaction is what catches a viewer that started (or
    resumed watching) after Phase A/B's snapshot went stale.

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

    if camera_has_relay_demand(db, camera_id, now):
        # Demand exists -- Phase A/B's snapshot was stale. Remove the
        # tracking row entirely (not just the claim) so a genuine future
        # idle period is free to start a fresh cycle.
        db.execute('DELETE FROM live_relay_idle_tracking WHERE camera_id=?', (camera_id,))
        return 'active_viewer'

    slot = _relay_slot(db, camera_id)
    if slot is None:
        return 'no_relay_slot'  # camera deleted/unassigned/no relay slot -- claim stands, nothing to queue

    appliance_id, camera_number = slot
    _queue_stop_live_relay(db, appliance_id=appliance_id, camera_number=camera_number, camera_id=camera_id, now=now)
    return 'stopped'


def _restop_if_still_uploading(db, camera_id: str, previous_stop_at: str, last_segment_at: float | None, now: datetime) -> bool:
    """Phase D: a stop was queued at previous_stop_at, yet this camera's
    segments were still arriving RESTOP_AFTER_SECONDS later with still no
    demand -- the stop was missed (e.g. expired undelivered while the
    appliance was offline) or the relay came back. Re-queues exactly one
    more stop, guarded by the same atomic conditional UPDATE discipline
    (moving stop_queued_at forward), so repeats are spaced at least
    RESTOP_AFTER_SECONDS apart."""
    if last_segment_at is None:
        return False
    try:
        stop_epoch = datetime.fromisoformat(previous_stop_at).timestamp()
    except (TypeError, ValueError):
        return False
    if last_segment_at <= stop_epoch + RESTOP_AFTER_SECONDS:
        return False  # nothing arrived after the stop had time to apply
    if camera_has_relay_demand(db, camera_id, now):
        return False
    cursor = db.execute(
        'UPDATE live_relay_idle_tracking SET stop_queued_at=? WHERE camera_id=? AND stop_queued_at=?',
        (now.isoformat(), camera_id, previous_stop_at),
    )
    if cursor.rowcount != 1:
        return False
    slot = _relay_slot(db, camera_id)
    if slot is None:
        return False
    appliance_id, camera_number = slot
    _queue_stop_live_relay(db, appliance_id=appliance_id, camera_number=camera_number, camera_id=camera_id, now=now)
    return True


def record_relay_viewer_activity(db, *, camera_id: str, customer_id: str, requested_by: str, now: datetime) -> dict:
    """Called by the relay playlist route on every fetch (live_playlist.
    py), inside the viewer's already-authorized request. Two effects:

    1. Heartbeat: refreshes last_seen_at on this viewer's live sessions
       for this camera (throttled to once per HEARTBEAT_WRITE_INTERVAL_
       SECONDS) and extends their lease to at least now+SESSION_LEASE_
       SECONDS, so someone actively watching keeps relay demand and never
       times out mid-view.
    2. Resume: if this viewer has a live session but the camera's relay
       was idle-stopped (e.g. the tab sat in the background past the
       activity timeout), removes the tracking row and queues one
       start_live_relay -- the atomic conditional DELETE means concurrent
       playlist fetches queue at most one start.

    Returns {'heartbeat': bool, 'resumed': bool} for tests/observability."""
    heartbeat_cutoff = (now - timedelta(seconds=HEARTBEAT_WRITE_INTERVAL_SECONDS)).isoformat()
    lease_until = (now + timedelta(seconds=SESSION_LEASE_SECONDS)).isoformat()
    cursor = db.execute(
        "UPDATE live_view_sessions SET last_seen_at=?,"
        "expires_at=CASE WHEN expires_at<? THEN ? ELSE expires_at END "
        "WHERE camera_id=? AND customer_id=? AND lower(requested_by)=lower(?) AND state='requested' AND expires_at>? "
        "AND (last_seen_at IS NULL OR last_seen_at<?)",
        (now.isoformat(), lease_until, lease_until, camera_id, customer_id, requested_by, now.isoformat(), heartbeat_cutoff),
    )
    heartbeat = cursor.rowcount > 0

    has_live_session = db.execute(
        "SELECT 1 FROM live_view_sessions WHERE camera_id=? AND customer_id=? AND lower(requested_by)=lower(?) "
        "AND state='requested' AND expires_at>? LIMIT 1",
        (camera_id, customer_id, requested_by, now.isoformat()),
    ).fetchone()
    if not has_live_session:
        return {'heartbeat': heartbeat, 'resumed': False}

    tracking = db.execute(
        'SELECT stop_queued_at FROM live_relay_idle_tracking WHERE camera_id=?', (camera_id,),
    ).fetchone()
    if not tracking:
        return {'heartbeat': heartbeat, 'resumed': False}
    if tracking['stop_queued_at'] is None:
        # Idle clock was running but the viewer is back -- cancel it.
        db.execute('DELETE FROM live_relay_idle_tracking WHERE camera_id=? AND stop_queued_at IS NULL', (camera_id,))
        return {'heartbeat': heartbeat, 'resumed': False}
    claimed = db.execute(
        'DELETE FROM live_relay_idle_tracking WHERE camera_id=? AND stop_queued_at=?',
        (camera_id, tracking['stop_queued_at']),
    )
    if claimed.rowcount != 1:
        return {'heartbeat': heartbeat, 'resumed': False}
    slot = _relay_slot(db, camera_id)
    if slot is None:
        return {'heartbeat': heartbeat, 'resumed': False}
    appliance_id, camera_number = slot
    _queue_relay_command(
        db, command='start_live_relay', appliance_id=appliance_id, camera_number=camera_number,
        camera_id=camera_id, now=now, created_by='live-relay-resume',
    )
    return {'heartbeat': heartbeat, 'resumed': True}


def _default_segment_activity() -> dict:
    """camera_id -> epoch seconds of the most recent relay segment the
    cloud received, from appliance_cloud.py's in-process live manifest
    store. Best-effort: an unavailable store simply disables the
    uploading-camera discovery and Phase D re-stop, never the sweep."""
    try:
        from appliance_cloud import live_manifest_store
        return live_manifest_store.last_segment_times()
    except Exception as error:
        logger.debug("live_relay.segment_activity_unavailable error=%s", error)
        return {}


def run_idle_sweep_tick(now: datetime = None, segment_activity=None) -> None:
    """One full sweep pass:

      expiry  -- mark expired 'requested' sessions 'expired'
      Phase A -- discover cameras with no relay demand (session history,
                 plus any camera whose segments are currently arriving)
      Phase B -- start/continue tracking them (idempotent)
      Phase C -- claim + stop every tracked camera past its grace period
                 that still has no demand
      Phase D -- re-stop a camera still uploading well after its stop

    segment_activity: optional camera_id -> last-segment epoch mapping
    (tests inject it; production reads the live manifest store).

    Each camera's step runs in its own short transaction, matching this
    project's existing per-call transaction granularity (see
    live_view_sessions.py) rather than one long transaction across every
    camera."""
    now = now or datetime.now()
    activity = _default_segment_activity() if segment_activity is None else dict(segment_activity)
    uploading = [
        camera_id for camera_id, last_at in activity.items()
        if last_at is not None and last_at >= now.timestamp() - SEGMENT_ACTIVITY_WINDOW_SECONDS
    ]

    with connection() as db:
        _expire_stale_sessions(db, now)

    with connection() as db:
        newly_idle = _discover_idle_candidates(db, now, uploading)
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

    restop_cutoff = (now - timedelta(seconds=RESTOP_AFTER_SECONDS)).isoformat()
    with connection() as db:
        stopped = db.execute(
            'SELECT camera_id,stop_queued_at FROM live_relay_idle_tracking WHERE stop_queued_at IS NOT NULL AND stop_queued_at<=?',
            (restop_cutoff,),
        ).fetchall()
    for row in stopped:
        with connection() as db:
            restopped = _restop_if_still_uploading(db, row['camera_id'], row['stop_queued_at'], activity.get(row['camera_id']), now)
        if restopped:
            logger.warning('live_relay.restop_queued camera_id=%s reason=segments_after_stop', row['camera_id'])
            audit({'email': 'system', 'role': 'live-relay-idle-sweep'}, 'system.live_view_idle_restopped', 'camera', row['camera_id'], {})


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
