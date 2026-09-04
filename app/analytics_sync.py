"""Tenant-scoped appliance -> cloud analytics-event sync.

This module reads the existing, unmodified local analytics record
(main.py's ANALYTICS_EVENTS_FILE, written by save_yolo_events() /
append_analytics_event() via the existing YOLO/motion-detection
workers) and forwards each event to the cloud's
POST /api/appliance/analytics/{camera_id}/events route, exactly the
way recording_uploader.py forwards completed recordings. It never
writes to, truncates, or otherwise modifies ANALYTICS_EVENTS_FILE or
any thumbnail file, and never calls into ai_person_detector(),
motion_detector(), save_yolo_events(), or append_analytics_event() --
the existing detection path is read-only input to this module, never
a dependency it can affect.

Two guardrails, both enforced structurally rather than by validation
that could be bypassed:

1. Tenant/site/camera identifiers are never invented or trusted from
   local state here. This module only ever sends a camera_id (looked
   up from the same /api/appliance/configuration response
   recording_uploader.py already trusts) to a route that itself
   re-resolves customer_id/site_id/appliance_id/camera_id exclusively
   from the authenticated appliance and the authorized-camera lookup
   on the cloud side -- this module has no ability to assert a tenant
   identity even if it wanted to.

2. The sync cursor is failure-safe. Rather than a single positional
   pointer into ANALYTICS_EVENTS_FILE (which a retry or a locally
   inserted/reordered event could cause to skip past a not-yet-synced
   entry), each event's synced state is tracked independently by its
   own local_event_id in a persisted set (SYNC_STATE_FILE, restart-
   durable, living in the same STATE_DIR credential.json already
   lives in). An id is added to that set only immediately after ITS
   OWN successful-or-idempotent-duplicate response -- never batched,
   never advanced ahead of a failure, and never inferred from another
   event's outcome. A failed event simply remains absent from the set
   and is retried, unchanged, on the next scan. The cloud route is
   idempotent on local_event_id, so the worst case of any bookkeeping
   imprecision here (a corrupt/missing state file read as "nothing
   synced yet", or a bounded-size eviction of an old id) is a harmless
   duplicate POST that the cloud accepts as a no-op -- never a
   silently and permanently skipped event.

Deliberately out of scope for this milestone (matches the approved
design): no S3/STS calls, no changes to the existing local analytics
storage format, no backlog cutoff (these are small structured records,
not multi-day video backlog -- the per-scan cap alone is the bounding
mechanism here), and no new cloud-side authorization-status route --
the cloud route already fails closed (404) on its own disabled flag,
which this module already treats as an ordinary sync failure (not
marked synced, retried next scan).
"""

import asyncio
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("anyaicam.analytics_sync")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
ANALYTICS_SYNC_ENABLED = os.environ.get("ANYAICAM_ANALYTICS_SYNC_ENABLED", "false").strip().lower() == "true"
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
STATE_DIR = Path(os.environ.get("ANYAICAM_STATE_DIR", "/var/lib/anyaicam"))
CREDENTIAL_FILE = STATE_DIR / "credential.json"
# Persisted synced-event-id set (the failure-safe cursor -- see module
# docstring). Overridable purely for tests, matching recording_uploader.py's
# own CUTOFF_FILE precedent -- production always uses the default.
SYNC_STATE_FILE = Path(os.environ.get("ANYAICAM_ANALYTICS_SYNC_STATE_FILE", str(STATE_DIR / "analytics_sync_state.json")))
# RECORDINGS_FOLDER/ANALYTICS_EVENTS_FILE are intentionally hardcoded to
# match main.py's own constants exactly -- this must always agree with
# where save_yolo_events()/append_analytics_event() actually write, not be
# independently configurable. Still overridable via env var purely for
# tests, matching every other file-location constant in this codebase.
RECORDINGS_FOLDER = Path("/app/recordings")
ANALYTICS_EVENTS_FILE = Path(os.environ.get("ANYAICAM_ANALYTICS_EVENTS_FILE", str(RECORDINGS_FOLDER / "analytics_events.json")))
SCAN_SECONDS = max(5.0, float(os.environ.get("ANYAICAM_ANALYTICS_SYNC_SCAN_SECONDS", "30.0")))
CONFIG_REFRESH_SECONDS = max(60.0, float(os.environ.get("ANYAICAM_ANALYTICS_SYNC_CONFIG_REFRESH_SECONDS", "300.0")))
DEFAULT_MAX_EVENTS_PER_SCAN = 20
# Comfortably above ANALYTICS_EVENTS_FILE's own 5000-entry cap (see
# append_analytics_event() in main.py), so a synced id is never evicted
# from this state file while its source event could still exist locally.
# Even a real eviction stays harmless (see module docstring, guardrail 2)
# -- this just keeps that a near-impossibility rather than routine.
MAX_TRACKED_SYNCED_IDS = 6000


def _parse_max_events_per_scan() -> int:
    """Unlike recording_uploader.py's per-scan cap (where unset means
    unlimited -- appropriate for a handful of large video files), this
    cap always has a value. The whole point of this milestone is a
    conservative, bounded first rollout, not a fire-hose of hundreds of
    backlog events the moment sync is first enabled on an appliance
    that has been detecting for days. Unset uses
    DEFAULT_MAX_EVENTS_PER_SCAN. An explicitly set but invalid value
    (non-numeric, zero, or negative) is a configuration mistake, not a
    request to remove the cap, so it fails safe to the tightest
    possible cap (1) instead, logged once so it's diagnosable."""
    raw = os.environ.get("ANYAICAM_ANALYTICS_SYNC_MAX_EVENTS_PER_SCAN", "").strip()
    if not raw:
        return DEFAULT_MAX_EVENTS_PER_SCAN
    try:
        value = int(raw)
    except ValueError:
        logger.warning("analytics_sync.max_events_per_scan_invalid raw=%r failing_safe_to=1", raw)
        return 1
    if value < 1:
        logger.warning("analytics_sync.max_events_per_scan_invalid raw=%r failing_safe_to=1", raw)
        return 1
    return value


MAX_EVENTS_PER_SCAN = _parse_max_events_per_scan()

# Backlog catch-up: MAX_EVENTS_PER_SCAN above stays the deliberately
# conservative steady-state rate (kept at its current production value,
# unchanged by this milestone). When the real backlog grows past
# CATCHUP_THRESHOLD_EVENTS, a scan temporarily uses the larger
# CATCHUP_MAX_EVENTS_PER_SCAN instead -- still one bounded batch per
# scan, never one huge burst -- automatically stepping back down to
# the steady-state rate the instant the backlog drops back under the
# threshold. No manual re-tuning of MAX_EVENTS_PER_SCAN is needed to
# recover from a real outage.
CATCHUP_THRESHOLD_EVENTS = max(1, int(os.environ.get("ANYAICAM_ANALYTICS_SYNC_CATCHUP_THRESHOLD", "100")))
CATCHUP_MAX_EVENTS_PER_SCAN = max(
    MAX_EVENTS_PER_SCAN, int(os.environ.get("ANYAICAM_ANALYTICS_SYNC_CATCHUP_MAX_EVENTS_PER_SCAN", "150"))
)

# Same 5000-entry cap ANALYTICS_EVENTS_FILE itself already enforces
# (see append_analytics_event() in main.py, and MAX_TRACKED_SYNCED_IDS's
# own comment above) -- used here purely to count the TRUE pending
# backlog size each scan (cheap: this file is small, structured JSON,
# never more than 5000 rows), not to select which events to send.
_PENDING_COUNT_CEILING = 5000

# Exponential backoff, applied only when an entire scan's every real
# send attempt failed -- the strongest signal available, without new
# exception-type plumbing through _control_plane_post, that the cloud
# is genuinely unreachable rather than one event being malformed.
# Resets to the base SCAN_SECONDS the instant any event succeeds again.
BACKOFF_MAX_MULTIPLIER = max(1, int(os.environ.get("ANYAICAM_ANALYTICS_SYNC_BACKOFF_MAX_MULTIPLIER", "8")))
_consecutive_scan_failures = 0

# Controlled-rollout scope: which camera_numbers this worker is allowed to
# sync at all. Unset (the default, and every existing test's implicit
# assumption) means "no restriction" -- this must never narrow existing
# behavior for a caller that doesn't set it. A comma-separated allowlist
# (e.g. "1") lets a single camera be validated in production before this
# is widened to the rest -- an event for a camera_number outside this set
# is left pending (same "not yet known" treatment as an unmapped camera),
# never marked synced, never counted against the per-scan cap.
_raw_camera_scope = os.environ.get("ANYAICAM_ANALYTICS_SYNC_CAMERAS", "").strip()
SYNC_CAMERA_SCOPE: frozenset[int] | None = (
    frozenset(int(item) for item in _raw_camera_scope.split(",") if item.strip().isdigit())
    if _raw_camera_scope
    else None
)

# Best-effort notification forwarding: on a successful (or idempotent-
# duplicate) analytics-event sync, also POST the same event to the
# existing, already-built /api/appliance/events route (notification_engine
# .fanout_appliance_event's real trigger -- see that module for the real
# email/in-app delivery this fans out to). Deliberately a SEPARATE flag
# from ANALYTICS_SYNC_ENABLED so the two can be toggled independently if
# ever needed, but both default false -- unset behaves exactly like this
# module did before notification forwarding existed, so every pre-existing
# test (none of which set this) is unaffected.
ANALYTICS_SYNC_NOTIFY_ENABLED = os.environ.get("ANYAICAM_ANALYTICS_SYNC_NOTIFY_ENABLED", "false").strip().lower() == "true"

# LPR ('plate' events) can fire far more often than person/vehicle/
# smart_motion on any camera facing regular traffic. Analytics-
# history sync (searchable in the Event Center/LPR page) is governed
# by ANALYTICS_SYNC_NOTIFY_ENABLED like everything else above -- this
# flag only gates whether a plate ALSO fans out to a notification,
# defaulting off so LPR doesn't create noisy alerts until that's a
# deliberate decision.
LPR_NOTIFY_ENABLED = os.environ.get("ANYAICAM_LPR_NOTIFY_ENABLED", "false").strip().lower() == "true"

analytics_sync_state: dict = {
    "worker_status": "disabled",
    "last_scan_at": None,
    "last_config_refresh_at": None,
    "last_error": None,
    "last_summary": None,
    "pending_count": 0,
    "catchup_mode": False,
    # "unknown" until the first real scan completes -- distinct from
    # "connected"/"syncing"/"offline" so a not-yet-started worker never
    # falsely reports itself as either reachable or unreachable.
    "connectivity": "unknown",
    "consecutive_failures": 0,
}

_lock = threading.Lock()
_camera_map: dict[int, dict] = {}  # camera_number -> {"camera_id":..., "site_id":...}, refreshed periodically
_state_lock = threading.Lock()
_synced_ids_cache: list[str] | None = None  # loaded once per process lifetime, then kept current in memory; see _load_synced_ids()
_unknown_camera_logged: set[int] = set()  # camera_numbers already logged as unrecognized this process lifetime -- avoids re-logging the same pending backlog every scan


def _load_appliance_identity() -> tuple[str, str] | None:
    try:
        data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    appliance_id = str(data.get("appliance_id") or "").strip()
    credential = str(data.get("credential") or "").strip()
    if not appliance_id or not credential:
        return None
    return appliance_id, credential


def _control_plane_headers(appliance_id: str, credential: str) -> dict:
    return {
        "User-Agent": "AnyAiCam-AnalyticsSync/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }


def _control_plane_post(path: str, payload: dict) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    headers = {"Content-Type": "application/json", **_control_plane_headers(appliance_id, credential)}
    request = urllib.request.Request(
        CLOUD_URL + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        logger.info("analytics_sync.http_call_begin path=%s", path)
        with urllib.request.urlopen(request, timeout=10) as response:
            logger.info("analytics_sync.http_call_returned path=%s", path)
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("analytics_sync.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("analytics_sync.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _control_plane_get(path: str) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    request = urllib.request.Request(CLOUD_URL + path, headers=_control_plane_headers(appliance_id, credential), method="GET")
    try:
        logger.info("analytics_sync.http_call_begin path=%s", path)
        with urllib.request.urlopen(request, timeout=10) as response:
            logger.info("analytics_sync.http_call_returned path=%s", path)
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("analytics_sync.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("analytics_sync.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _refresh_camera_map() -> None:
    """Polls the existing, unchanged GET /api/appliance/configuration for
    this appliance's own camera_number -> camera_id/site_id mapping,
    exactly the same call recording_uploader.py already makes
    independently (duplicated here rather than shared, matching this
    project's established convention). Never writes anything; a
    failed/unreachable poll just leaves the previous mapping in place,
    so a transient network blip never stops already-known cameras'
    events from continuing to sync."""
    response = _control_plane_get("/api/appliance/configuration")
    if not isinstance(response, dict):
        return
    cameras = response.get("cameras")
    if not isinstance(cameras, list):
        return
    mapping: dict[int, dict] = {}
    for item in cameras:
        if not isinstance(item, dict):
            continue
        camera_id = item.get("id")
        camera_number = item.get("camera_number")
        site_id = item.get("site_id")
        if not isinstance(camera_id, str) or not camera_id.strip():
            continue
        if isinstance(camera_number, bool) or not isinstance(camera_number, int):
            continue
        if not isinstance(site_id, str) or not site_id.strip():
            continue
        mapping[camera_number] = {"camera_id": camera_id, "site_id": site_id}
    with _lock:
        _camera_map.clear()
        _camera_map.update(mapping)
    analytics_sync_state["last_config_refresh_at"] = datetime.now().isoformat()


def _camera_identity(camera_number: int) -> dict | None:
    with _lock:
        return _camera_map.get(camera_number)


def _load_local_events() -> list[dict]:
    """Read-only. A missing or corrupt ANALYTICS_EVENTS_FILE is read as
    "nothing to sync yet" -- never as a reason to crash, and never
    written back in any form; this function has no write path at all."""
    try:
        data = json.loads(ANALYTICS_EVENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _load_synced_ids() -> list[str]:
    """Reads the persisted synced-event-id list once per process
    lifetime, caching afterward. A missing or corrupt state file is
    read as "nothing synced yet" -- never as a reason to skip real
    events, and never as a reason to crash. The only consequence of
    starting empty is a handful of harmless duplicate POSTs (the cloud
    route is idempotent on local_event_id), never a lost or
    permanently-skipped event."""
    global _synced_ids_cache
    if _synced_ids_cache is not None:
        return _synced_ids_cache
    try:
        data = json.loads(SYNC_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    ids: list[str] = []
    if isinstance(data, dict):
        raw = data.get("synced_event_ids")
        if isinstance(raw, list):
            ids = [str(item) for item in raw if isinstance(item, str) and item.strip()]
    _synced_ids_cache = ids
    return ids


def _persist_synced_id(event_id: str) -> None:
    """Called once per individual event, immediately after that event's
    own successful-or-duplicate response -- never batched, and never
    called for a failed event. This is the failure-safe cursor itself:
    each event's synced state is recorded independently by its own id,
    so a failure on one event can never cause a different event to be
    skipped, and a crash between two events loses at most the ability
    to skip re-sending the one that wasn't yet persisted (a harmless
    duplicate), never the record of one that already was."""
    global _synced_ids_cache
    with _state_lock:
        ids = _load_synced_ids()
        if event_id in ids:
            return
        ids = [*ids, event_id]
        if len(ids) > MAX_TRACKED_SYNCED_IDS:
            ids = ids[-MAX_TRACKED_SYNCED_IDS:]
        _synced_ids_cache = ids
        try:
            SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SYNC_STATE_FILE.write_text(json.dumps({"synced_event_ids": ids}), encoding="utf-8")
        except OSError as error:
            logger.warning("analytics_sync.state_persist_failed event_id=%s error=%s", event_id, error)


def _pending_events(max_count: int) -> list[tuple[dict, str]]:
    """Returns up to max_count (event, camera_id) pairs, oldest-first,
    for local events not yet in the synced-id state whose camera_number
    currently maps to a known camera. ANALYTICS_EVENTS_FILE is
    newest-first; this reverses it so older events sync before newer
    ones. An event whose camera_number isn't (yet) in _camera_map is
    left pending -- not marked synced, not counted against the cap --
    and is retried automatically once _refresh_camera_map picks up
    that camera."""
    synced = set(_load_synced_ids())
    events = list(reversed(_load_local_events()))
    selected: list[tuple[dict, str]] = []
    for event in events:
        if len(selected) >= max_count:
            break
        event_id = str(event.get("id") or "").strip()
        if not event_id or event_id in synced:
            continue
        camera_number = event.get("camera")
        if isinstance(camera_number, bool) or not isinstance(camera_number, int):
            continue
        if SYNC_CAMERA_SCOPE is not None and camera_number not in SYNC_CAMERA_SCOPE:
            continue
        identity = _camera_identity(camera_number)
        if not identity:
            if camera_number not in _unknown_camera_logged:
                _unknown_camera_logged.add(camera_number)
                logger.warning("analytics_sync.camera_not_yet_known camera_number=%s", camera_number)
            continue
        selected.append((event, identity["camera_id"]))
    return selected


def _build_payload(event: dict) -> dict:
    """Fixed six-field allowlist, matching the cloud route's own
    allowlist exactly -- fields read individually from the local
    event, never a pass-through of the raw dict. thumbnail and
    linked_recording (local-filesystem-only concepts) are deliberately
    never read here, so they cannot reach the cloud regardless of what
    the local event contains."""
    detections = event.get("detections")
    payload_detections = detections if isinstance(detections, list) else None
    # PPE's hard_hat_present/safety_vest_present booleans are set as
    # loose extra keys on the local event dict by main.py's PPE hook in
    # save_yolo_events() (ppe.py's own summarize_ppe() output) -- not
    # part of AnalyticsEventModel's fixed schema, so they were never
    # reaching the cloud at all. Forwarded here into this same generic
    # `detections` field (the cloud's detection_events.detections_json
    # column already exists and is already unused for "ppe" rows) so
    # the customer-facing PPE view can show compliant/violation status
    # without a new column, a new endpoint field, or touching the paused
    # LPR pipeline this same mechanism deliberately does NOT extend to.
    if str(event.get("event_type") or "").strip() == "ppe" and payload_detections is None:
        payload_detections = [{
            "hard_hat_present": bool(event.get("hard_hat_present")),
            "safety_vest_present": bool(event.get("safety_vest_present")),
        }]
    return {
        "local_event_id": str(event.get("id") or "").strip(),
        "event_type": str(event.get("event_type") or "").strip(),
        "confidence": event.get("confidence"),
        "object_count": event.get("object_count"),
        "detections": payload_detections,
        "event_timestamp": str(event.get("timestamp") or "").strip(),
    }


# Real detections emit the SPECIFIC vehicle sub-class YOLO produced
# (car/truck/bus/motorcycle/bicycle), but the cloud's existing
# notification fan-out (notification_engine.SUPPORTED) only recognizes
# the generic "vehicle" -- confirmed live: real "car"/"motorcycle"
# events synced to detection_events correctly, but produced zero
# notifications, because "car" is never literally in that SUPPORTED
# set. This is the exact same vehicle-class set already used elsewhere
# in this codebase (see main.py's isVehicle()/`event_type in
# {"vehicle","car","truck","bus","motorcycle","bicycle"}` checks) --
# reused here rather than inventing a second, possibly-inconsistent
# list. Deliberately used ONLY for the notification payload below,
# never for _build_payload() above: the analytics-history record
# (detection_events, via the OTHER cloud route) must keep storing the
# exact, specific class name the detector produced, unchanged -- this
# mapping exists purely to get a vehicle detection into the existing
# vehicle-notification path, not to rewrite what analytics history
# remembers actually happened.
VEHICLE_EVENT_TYPES = frozenset({"car", "truck", "bus", "motorcycle", "bicycle"})

# people_counting_worker() (main.py, edge) writes the literal event_type
# "people_counting_in"/"people_counting_out" per line-crossing direction
# -- confirmed live: real crossing events synced to detection_events
# correctly, but produced zero notifications, same root cause as the
# vehicle sub-classes above: notification_engine.SUPPORTED only
# recognizes the generic "people_counting", never the direction-suffixed
# form. Same reasoning as VEHICLE_EVENT_TYPES: used only for the
# notification payload below, never for _build_payload() above, so
# analytics history keeps the specific in/out direction it actually
# recorded.
PEOPLE_COUNTING_EVENT_TYPES = frozenset({"people_counting_in", "people_counting_out"})


def _notification_event_type(event_type: str) -> str:
    """The event_type the notification route should see: the generic
    "vehicle"/"people_counting" for any of the specific sub-types above,
    or the original event_type unchanged for anything else -- "person",
    "motion", "smart_motion", etc. already match notification_
    engine.SUPPORTED literally and need no translation."""
    if event_type in VEHICLE_EVENT_TYPES:
        return "vehicle"
    if event_type in PEOPLE_COUNTING_EVENT_TYPES:
        return "people_counting"
    if event_type == "plate":
        return "lpr"
    return event_type


def _build_notification_payload(event: dict, camera_id: str) -> dict:
    """Shape expected by the existing POST /api/appliance/events route
    (see appliance_cloud.py's events()/notification_engine.fanout_
    appliance_event -- the real, already-built email/in-app delivery
    this fans out to). Reuses the SAME local_event_id as this event's
    own id here, deliberately: that route's own dedup is `INSERT OR
    IGNORE` keyed on (appliance_id, event_id), so a retried notification
    POST for an event already accepted is a harmless no-op, exactly
    matching the idempotent-duplicate semantics already relied on for
    the analytics-event POST above. Only the fields that route/fanout
    actually reads are ever sent -- no thumbnail/linked_recording (same
    local-filesystem-only exclusion as _build_payload())."""
    payload = {
        "id": str(event.get("id") or "").strip(),
        "event_type": _notification_event_type(str(event.get("event_type") or "").strip()),
        "camera_id": camera_id,
        "timestamp": str(event.get("timestamp") or "").strip(),
    }
    # Smart Motion's event_type alone ("smart_motion") doesn't say what
    # was actually seen -- the cloud's own generic title/message
    # ("Smart Motion detected") already reads event_type, but the real
    # detected class lives in this local event's own triggered_by
    # field (set by store_motion_event() in main.py). The cloud's
    # fanout_appliance_event() already supports a caller-supplied
    # message (falls back to the generic one when absent) -- reusing
    # that existing field rather than adding a new one.
    triggered_by = str(event.get("triggered_by") or "").strip()
    if payload["event_type"] == "smart_motion" and triggered_by:
        payload["message"] = f"Smart Motion: {triggered_by} detected"
    return payload


def _forward_notification(event: dict, camera_id: str) -> None:
    """Best-effort: a notification failure is logged but never affects
    whether the event itself is marked synced (see _sync_pending_events)
    -- the analytics-event POST above is this system's durable record of
    "did this event reach the cloud"; the notification POST is a fan-out
    of an already-recorded event, not a second thing that must succeed
    for the first to count. Never called unless
    ANALYTICS_SYNC_NOTIFY_ENABLED is explicitly true."""
    if str(event.get("event_type") or "") == "plate" and not LPR_NOTIFY_ENABLED:
        return
    payload = _build_notification_payload(event, camera_id)
    if not payload["id"] or not payload["event_type"] or not payload["timestamp"]:
        logger.warning("analytics_sync.notification_payload_malformed event_id=%r", event.get("id"))
        return
    response = _control_plane_post("/api/appliance/events", {"events": [payload]})
    if not isinstance(response, dict) or response.get("status") != "accepted":
        logger.warning("analytics_sync.notification_forward_failed event_id=%s camera_id=%s", payload["id"], camera_id)


def _sync_pending_events() -> dict:
    """Runs one full scan's worth of work synchronously -- this whole
    call is what gets dispatched via asyncio.to_thread from the async
    worker loop below (matching _refresh_camera_map's own precedent),
    so a slow or hanging network call here never blocks the FastAPI
    event loop. Never raises for an individual event's failure; only a
    genuinely unexpected local bug would propagate, which the worker
    loop's own try/except still catches."""
    all_pending = _pending_events(_PENDING_COUNT_CEILING)
    pending_count = len(all_pending)
    catchup_mode = pending_count > CATCHUP_THRESHOLD_EVENTS
    effective_cap = CATCHUP_MAX_EVENTS_PER_SCAN if catchup_mode else MAX_EVENTS_PER_SCAN
    attempted = 0
    synced = 0
    failed = 0
    for event, camera_id in all_pending[:effective_cap]:
        attempted += 1
        payload = _build_payload(event)
        if not payload["local_event_id"] or not payload["event_type"] or not payload["event_timestamp"]:
            # Malformed local event -- fixing this isn't something a retry
            # can do, but it also isn't marked synced: leaving it pending
            # is harmless (it just keeps failing this same check every
            # scan) and safer than inventing a fake synced marker for
            # data that was never actually sent.
            failed += 1
            logger.warning("analytics_sync.local_event_malformed event_id=%r", event.get("id"))
            continue
        response = _control_plane_post(f"/api/appliance/analytics/{camera_id}/events", payload)
        if isinstance(response, dict) and response.get("status") in ("accepted", "duplicate"):
            _persist_synced_id(payload["local_event_id"])
            synced += 1
            if ANALYTICS_SYNC_NOTIFY_ENABLED:
                _forward_notification(event, camera_id)
        else:
            failed += 1
            logger.warning("analytics_sync.event_sync_failed event_id=%s camera_id=%s", payload["local_event_id"], camera_id)
    return {
        "attempted": attempted,
        "synced": synced,
        "failed": failed,
        "pending_count": pending_count,
        "catchup_mode": catchup_mode,
    }


async def analytics_sync_worker() -> None:
    global _consecutive_scan_failures
    if RUNTIME_ROLE not in {"edge", "combined"} or not ANALYTICS_SYNC_ENABLED:
        analytics_sync_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    analytics_sync_state["worker_status"] = "running"
    logger.info("analytics_sync.worker_started")
    last_config_refresh = 0.0
    scan_number = 0
    while True:
        try:
            scan_number += 1
            logger.info("analytics_sync.scan_tick_begin scan_number=%s at=%s", scan_number, datetime.now().isoformat())
            now = time.monotonic()
            if now - last_config_refresh >= CONFIG_REFRESH_SECONDS:
                await asyncio.to_thread(_refresh_camera_map)
                last_config_refresh = now
            summary = await asyncio.to_thread(_sync_pending_events)
            analytics_sync_state["last_scan_at"] = datetime.now().isoformat()
            analytics_sync_state["last_error"] = None
            analytics_sync_state["last_summary"] = summary
            analytics_sync_state["pending_count"] = summary.get("pending_count", 0)
            analytics_sync_state["catchup_mode"] = summary.get("catchup_mode", False)
            if summary["attempted"] > 0 and summary["synced"] == 0:
                _consecutive_scan_failures += 1
                analytics_sync_state["connectivity"] = "offline"
            else:
                _consecutive_scan_failures = 0
                analytics_sync_state["connectivity"] = "syncing" if summary.get("catchup_mode") else "connected"
            analytics_sync_state["consecutive_failures"] = _consecutive_scan_failures
            backoff_multiplier = min(BACKOFF_MAX_MULTIPLIER, 2 ** _consecutive_scan_failures) if _consecutive_scan_failures else 1
            await asyncio.sleep(SCAN_SECONDS * backoff_multiplier)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            analytics_sync_state["last_error"] = str(error)
            logger.warning("analytics_sync.worker_iteration_failed error=%s", error)
            _consecutive_scan_failures += 1
            analytics_sync_state["connectivity"] = "offline"
            analytics_sync_state["consecutive_failures"] = _consecutive_scan_failures
            backoff_multiplier = min(BACKOFF_MAX_MULTIPLIER, 2 ** _consecutive_scan_failures)
            await asyncio.sleep(SCAN_SECONDS * backoff_multiplier)
