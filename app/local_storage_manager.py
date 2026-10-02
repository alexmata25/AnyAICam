"""Automatic local recording storage management: continuously monitors
the filesystem backing this appliance's local recordings folder and, once
free space drops to the configured reserved threshold, deletes the oldest
eligible local recording files -- across every camera in true chronological
order, never one camera at a time -- until free space is restored.

Scope, deliberately narrow (v1): only recordings belonging to a camera
whose cloud_recording_mode is NOT 'continuous' are ever eligible for
automatic deletion. For Local and Hybrid tiers, the local copy IS the
system of record (Hybrid uploads only event clips/thumbnails -- see
event_media_uploader.py -- never the continuous segments this module
manages), so scheduled/pressure-driven local deletion is the intended
lifecycle. For the Continuous/Cloud tier, the customer is explicitly
paying for AWS to be the system of record; verifying a specific local
file has actually finished uploading before it would be safe to delete
is real, separate work this module does not attempt in v1 -- Continuous-
tier camera files are simply never touched here. If that tier's local
disk fills up as a result, this module still reports 'critical' and
fires a real storage_problem notification rather than deleting anything
it cannot prove is safe to lose -- "never allow silent recording
failure" without "silently delete a paying customer's only copy" is the
explicit trade-off this makes.

Two independent, fail-closed-by-default flags:
  ANYAICAM_LOCAL_STORAGE_MANAGEMENT_ENABLED (default false) -- gates the
    entire worker, including monitoring/reporting. Off means this module
    does nothing at all, the same fail-closed default every other opt-in
    worker in this codebase already uses.
  ANYAICAM_LOCAL_STORAGE_AUTO_DELETE_ENABLED (default false) -- gates
    ONLY the deletion action. Monitoring/state-reporting can run (and be
    verified safe) with this off; a real Ryzen rollout is expected to
    enable monitoring first, confirm the reported disk numbers and state
    classification look right for a real period of time, and only then
    separately enable deletion -- matching the explicit "build and test
    without deleting any real Ryzen recordings... only enable automatic
    deletion after the deletion logic and safeguards have been verified"
    instruction this module was built under.

Cross-process reporting: this worker runs inside the VMS app process,
not the separate appliance-agent control-plane process that actually
sends the heartbeat carrying disk_capacity/disk_used/recording_used to
the cloud (see metrics.py in that package). Rather than teaching this
module to speak the appliance's control-plane auth protocol a second
time, it writes its own small, self-contained state file
(STATE_DIR/local_storage_state.json) that the agent's own heartbeat
gathering reads if present -- the same cross-process file convention
already established in the other direction by live_relay_commands.json
(appliance-agent writes, this VMS process reads)."""

import json
import logging
import appliance_config_cache
import os
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("anyaicam.local_storage_manager")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
MANAGEMENT_ENABLED = os.environ.get("ANYAICAM_LOCAL_STORAGE_MANAGEMENT_ENABLED", "false").strip().lower() == "true"
AUTO_DELETE_ENABLED = os.environ.get("ANYAICAM_LOCAL_STORAGE_AUTO_DELETE_ENABLED", "false").strip().lower() == "true"
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
STATE_DIR = Path(os.environ.get("ANYAICAM_STATE_DIR", "/var/lib/anyaicam"))
CREDENTIAL_FILE = STATE_DIR / "credential.json"

SCAN_SECONDS = max(10.0, float(os.environ.get("ANYAICAM_LOCAL_STORAGE_SCAN_SECONDS", "60.0")))
CONFIG_REFRESH_SECONDS = max(60.0, float(os.environ.get("ANYAICAM_LOCAL_STORAGE_CONFIG_REFRESH_SECONDS", "300.0")))
# A completed segment must be at least this old before it's eligible for
# ANYTHING here (deletion candidacy) -- guards the file FFmpeg may still
# be actively writing, the same safety margin CLOUD_UPLOAD_MIN_FILE_AGE_
# SECONDS already provides for the separate upload pipeline. This is what
# makes "continue recording during cleanup" true by construction: the
# currently-open segment is never old enough to be a candidate at all.
MIN_FILE_AGE_SECONDS = max(30, int(os.environ.get("ANYAICAM_LOCAL_STORAGE_MIN_FILE_AGE_SECONDS", "120")))
# Deleting exactly down to the reserved floor would immediately re-trigger
# cleanup on the very next scan as normal recording continues -- a small
# extra margin (restore to reserved + this many points) avoids that
# thrash, deleting a few more files per cleanup pass than the bare
# minimum instead of running every single scan indefinitely.
CLEANUP_TARGET_MARGIN_PERCENT = max(0, int(os.environ.get("ANYAICAM_LOCAL_STORAGE_CLEANUP_TARGET_MARGIN_PERCENT", "2")))
# Hard ceiling on one cleanup pass -- bounds worst-case pass duration and
# guards against ever deleting the entire backlog in one tick if policy
# configuration is somehow wrong; the next scan simply continues if more
# is still needed.
MAX_DELETIONS_PER_TICK = max(1, int(os.environ.get("ANYAICAM_LOCAL_STORAGE_MAX_DELETIONS_PER_TICK", "500")))

local_storage_manager_state: dict = {
    "worker_status": "disabled", "storage_state": None, "free_percent": None,
    "last_scan_at": None, "last_cleanup_at": None, "last_error": None,
}

_lock = threading.Lock()
_camera_map: dict[str, dict] = {}  # camera_id -> {"camera_number": int, "recording_mode": str|None}


# --------------------------------------------------------------- appliance identity / control plane
# Same shape as every other appliance-side worker in this codebase
# (live_relay_uploader.py, recording_uploader.py, webrtc_publisher.py) --
# deliberately duplicated, not imported, per that established convention.

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
        "User-Agent": "AnyAiCam-LocalStorageManager/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }


def _control_plane_get(path: str) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    request = urllib.request.Request(CLOUD_URL + path, headers=_control_plane_headers(appliance_id, credential), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("local_storage.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("local_storage.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _control_plane_post(path: str, payload: dict) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    headers = {"Content-Type": "application/json", **_control_plane_headers(appliance_id, credential)}
    request = urllib.request.Request(CLOUD_URL + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("local_storage.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("local_storage.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _refresh_camera_map() -> None:
    response = appliance_config_cache.get_or_fetch(lambda: _control_plane_get("/api/appliance/configuration"))
    if not isinstance(response, dict):
        return
    cameras = response.get("cameras")
    if not isinstance(cameras, list):
        return
    mapping: dict[str, dict] = {}
    for item in cameras:
        if not isinstance(item, dict):
            continue
        camera_id = item.get("id")
        camera_number = item.get("camera_number")
        if not isinstance(camera_id, str) or not camera_id.strip():
            continue
        if isinstance(camera_number, bool) or not isinstance(camera_number, int):
            continue
        mapping[camera_id] = {"camera_number": camera_number, "recording_mode": item.get("recording_mode")}
    with _lock:
        global _camera_map
        _camera_map = mapping


def _local_storage_policy() -> dict:
    """The effective reserved/warning free-space percentages AND local
    recording retention target (days) for this appliance's own customer
    -- resolved the same way every other RDM policy override already is
    (customer_cloud_policy's own precedent), but fetched here via the
    control plane's storage_policy field (appliance_cloud.
    appliance_configuration()) rather than direct DB access, since this
    worker runs on the edge. Falls back to the system defaults (never
    crashes/blocks) if the cloud is unreachable -- a transient outage
    must never stop local disk monitoring.

    local_retention_days is deliberately separate from the existing
    Hybrid AWS/S3 cloud retention entitlement (customer_cloud_policy /
    event_media_policy) -- it governs only how long a segment is allowed
    to occupy THIS appliance's local disk, not how long the customer's
    cloud copy (if any) is kept."""
    from local_storage_policy import DEFAULT_RESERVED_FREE_PERCENT, DEFAULT_WARNING_FREE_PERCENT, DEFAULT_LOCAL_RETENTION_DAYS

    response = appliance_config_cache.get_or_fetch(lambda: _control_plane_get("/api/appliance/configuration"))
    policy = response.get("storage_policy") if isinstance(response, dict) else None
    if not isinstance(policy, dict):
        return {
            "reserved_free_percent": DEFAULT_RESERVED_FREE_PERCENT,
            "warning_free_percent": DEFAULT_WARNING_FREE_PERCENT,
            "local_retention_days": DEFAULT_LOCAL_RETENTION_DAYS,
        }
    reserved = policy.get("reserved_free_percent")
    warning = policy.get("warning_free_percent")
    retention = policy.get("local_retention_days")
    return {
        "reserved_free_percent": int(reserved) if isinstance(reserved, (int, float)) else DEFAULT_RESERVED_FREE_PERCENT,
        "warning_free_percent": int(warning) if isinstance(warning, (int, float)) else DEFAULT_WARNING_FREE_PERCENT,
        "local_retention_days": int(retention) if isinstance(retention, (int, float)) and retention > 0 else DEFAULT_LOCAL_RETENTION_DAYS,
    }


# --------------------------------------------------------------- disk usage + state classification

def disk_usage_percent(recordings_folder: Path) -> dict:
    """{total_bytes, used_bytes, free_bytes, free_percent, used_percent}
    for the filesystem backing recordings_folder -- shutil.disk_usage,
    the exact same stdlib call already used three times elsewhere in
    this codebase for this identical purpose (main.py), not a new
    dependency or measurement approach."""
    usage = shutil.disk_usage(recordings_folder)
    free_percent = (usage.free / usage.total * 100) if usage.total else 0.0
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": free_percent,
        "used_percent": 100.0 - free_percent,
    }


def classify_storage_state(free_percent: float, *, reserved_free_percent: int, warning_free_percent: int, cleanup_in_progress: bool) -> str:
    """One of 'healthy' / 'warning' / 'cleanup_active' / 'critical'.
    cleanup_in_progress is an explicit input (not re-derived from
    free_percent) because 'cleanup_active' is about WHAT THIS WORKER IS
    DOING right now, not just a third threshold band -- a cleanup pass
    that is actively running is reported as such even for the single
    scan tick where its own deletions may have already pulled
    free_percent back above the reserved floor."""
    if cleanup_in_progress:
        return "cleanup_active"
    if free_percent <= reserved_free_percent:
        return "critical"
    if free_percent <= warning_free_percent:
        return "warning"
    return "healthy"


# --------------------------------------------------------------- eligible-file enumeration

def _eligible_camera_numbers() -> dict[int, str]:
    """camera_number -> camera_id, restricted to cameras whose
    cloud_recording_mode is NOT 'continuous' -- see module docstring for
    why Continuous/Cloud-tier cameras are never automatic-deletion
    candidates in v1. A camera with no known recording_mode (None) is
    treated as eligible -- the same "no mode set" default every other
    per-camera cloud_recording_mode check in this codebase already
    treats as Local/Hybrid-equivalent (never as an implicit Continuous
    upgrade)."""
    with _lock:
        camera_map = dict(_camera_map)
    return {
        info["camera_number"]: camera_id
        for camera_id, info in camera_map.items()
        if info.get("recording_mode") != "continuous"
    }


def enumerate_eligible_recordings(recordings_folder: Path, *, recording_start_fn, now: float | None = None) -> list[dict]:
    """Every completed (age-gated), eligible-camera local .mkv file,
    each as {path, camera_number, camera_id, started_at (datetime),
    size_bytes}, sorted oldest-first ACROSS every eligible camera
    together -- never grouped or round-robined per camera. This single
    global chronological ordering is what "delete recordings
    chronologically across cameras rather than allowing one camera to
    consume the disk indefinitely" means in practice: every eligible
    camera's oldest segment competes on equal footing for which gets
    deleted first, so a camera producing more data simply supplies more
    of the oldest candidates (and is trimmed proportionally), rather
    than one camera's backlog being ignored while another's is
    repeatedly swept.

    recording_start_fn(path, camera_number) -> datetime | None is
    injected (main.py's own recording_start()) rather than imported --
    main.py is what imports this module to wire its background task, so
    importing back would be a circular import; the same dependency-
    injection shape already used for camera_url_fn elsewhere in this
    codebase's edge-side workers."""
    now = now if now is not None else time.time()
    cutoff = now - MIN_FILE_AGE_SECONDS
    candidates: list[dict] = []
    for camera_number, camera_id in _eligible_camera_numbers().items():
        camera_folder = recordings_folder / f"camera{camera_number}"
        if not camera_folder.is_dir():
            continue
        for path in camera_folder.glob("*.mkv"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size <= 0 or stat.st_mtime > cutoff:
                continue
            started_at = recording_start_fn(path, camera_number)
            if started_at is None:
                continue
            candidates.append({
                "path": path, "camera_number": camera_number, "camera_id": camera_id,
                "started_at": started_at, "size_bytes": stat.st_size, "mtime": stat.st_mtime,
            })
    candidates.sort(key=lambda item: item["started_at"])
    return candidates


# --------------------------------------------------------------- deletion + DB/audit sync

def _delete_matching_recording_row(db, *, camera_id: str, s3_key: str) -> None:
    """Removes the recordings catalog row this local file was cataloged
    under (see main.py's _catalog_local_recordings_for_camera()), if one
    exists -- the exact fix "keep database recording records
    synchronized with deleted video files so Playback doesn't show
    broken recordings" calls for. A row is only ever deleted here for a
    camera this module already restricted deletion eligibility to
    (never 'continuous') -- for those tiers the local file IS the only
    copy, so removing its catalog row can never orphan a still-valid S3
    presigned URL (see _customer_recording_url()'s own local-fallback
    logic, which this restores the correctness of)."""
    if not camera_id:
        return
    db.execute("DELETE FROM recordings WHERE camera_id=? AND s3_key=?", (camera_id, s3_key))


def _write_cleanup_log(db, *, candidate: dict, trigger_free_percent: float, reason: str) -> None:
    db.execute(
        "INSERT INTO local_storage_cleanup_log(id,camera_number,camera_id,file_name,recording_started_at,size_bytes,deleted_at,trigger_free_percent,reason) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            secrets.token_hex(12), candidate["camera_number"], candidate["camera_id"], candidate["path"].name,
            candidate["started_at"].isoformat(), candidate["size_bytes"], datetime.now().isoformat(),
            trigger_free_percent, reason,
        ),
    )


def delete_one_recording(candidate: dict, *, cloud_recording_s3_key_fn, reason: str = "auto_cleanup_low_disk") -> bool:
    """Deletes exactly one local recording file plus its matching
    recordings catalog row and cleanup-log entry, all after the file is
    confirmed gone -- never removes catalog/audit state ahead of the
    real deletion it's supposed to describe. Returns False (and leaves
    everything untouched) if the file is already gone by the time this
    runs (a legitimate race -- nothing else in this codebase deletes
    local recordings, but this stays defensive rather than assuming
    exclusivity) or if deletion fails for any other reason; never
    raises.

    reason distinguishes, in the audit log, a free-space-reserve
    deletion ('auto_cleanup_low_disk') from a retention-target deletion
    ('auto_cleanup_retention_expired') -- run_cleanup_pass() picks
    whichever actually applied to this specific candidate."""
    from partner_db import connection

    path = candidate["path"]
    s3_key = cloud_recording_s3_key_fn(path, candidate["camera_number"])
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        logger.warning("local_storage.delete_failed path=%s error=%s", path, error)
        return False

    with connection() as db:
        _delete_matching_recording_row(db, camera_id=candidate["camera_id"], s3_key=s3_key)
        _write_cleanup_log(db, candidate=candidate, trigger_free_percent=local_storage_manager_state.get("free_percent"), reason=reason)

    logger.info(
        "local_storage.deleted camera_number=%s file=%s size_bytes=%s recording_started_at=%s",
        candidate["camera_number"], path.name, candidate["size_bytes"], candidate["started_at"].isoformat(),
    )
    return True


def run_cleanup_pass(recordings_folder: Path, *, recording_start_fn, cloud_recording_s3_key_fn, policy: dict) -> dict:
    """One full cleanup pass, enforcing BOTH independent triggers the
    real Ryzen lab appliance's 7-day-local / 10%-reserve requirement
    calls for -- "whichever condition requires cleanup first should
    win":

      1. Free-space reserve: deletes oldest-first, across every eligible
         camera, until free space is restored above reserved_free_percent
         (plus CLEANUP_TARGET_MARGIN_PERCENT, to avoid immediately
         re-triggering).
      2. Local retention target (policy["local_retention_days"], None ==
         no age limit -- most customers): deletes every eligible
         recording older than that many days, REGARDLESS of current free
         space, since a customer who set a retention target wants old
         local footage gone even on a mostly-empty disk.

    Candidates are still tried in one single oldest-first order (never
    two separate passes) -- since the list is chronological, every
    retention-expired candidate is necessarily tried before any
    still-within-retention candidate, so this remains exactly one linear
    scan. A given candidate is deleted for whichever of the two reasons
    actually applies to it (recorded in the audit log via delete_one_
    recording's reason).

    Stops when neither condition applies to the next candidate, there
    are no more eligible candidates, or MAX_DELETIONS_PER_TICK is
    reached. Always synchronous and directly callable (no asyncio) so
    it's fully testable and so the worker loop can run it via asyncio.
    to_thread, matching webrtc_publisher.py's own established pattern
    for real, potentially-slow filesystem/DB work inside an async worker
    sharing this process's event loop.

    exhausted (in the returned dict) reflects ONLY the free-space
    reserve condition, by design -- a retention target that still has
    candidates queued (deferred to the next tick by MAX_DELETIONS_PER_
    TICK, say) is never itself a silent-recording-failure risk the way
    running out of disk is, so it must never alone force the worker into
    'critical'."""
    target_free_percent = policy["reserved_free_percent"] + CLEANUP_TARGET_MARGIN_PERCENT
    retention_days = policy.get("local_retention_days")
    retention_cutoff = datetime.now() - timedelta(days=retention_days) if retention_days else None

    deleted = 0
    freed_bytes = 0
    usage = disk_usage_percent(recordings_folder)
    if usage["free_percent"] > target_free_percent and retention_cutoff is None:
        return {"deleted": 0, "freed_bytes": 0, "final_free_percent": usage["free_percent"], "exhausted": False}

    candidates = enumerate_eligible_recordings(recordings_folder, recording_start_fn=recording_start_fn)
    exhausted = False
    for candidate in candidates:
        if deleted >= MAX_DELETIONS_PER_TICK:
            break
        past_retention = retention_cutoff is not None and candidate["started_at"] < retention_cutoff
        if past_retention:
            reason = "auto_cleanup_retention_expired"
        else:
            usage = disk_usage_percent(recordings_folder)
            if usage["free_percent"] > target_free_percent:
                # Neither trigger applies to this candidate, and every
                # remaining candidate is newer still (list is oldest-
                # first) -- nothing left to do this pass.
                break
            reason = "auto_cleanup_low_disk"
        if delete_one_recording(candidate, cloud_recording_s3_key_fn=cloud_recording_s3_key_fn, reason=reason):
            deleted += 1
            freed_bytes += candidate["size_bytes"]
    else:
        # The for-loop ran to completion without an internal `break` --
        # every eligible candidate was tried. Distinct from breaking out
        # early (target reached, or MAX_DELETIONS_PER_TICK hit with more
        # candidates still queued for the next tick).
        usage = disk_usage_percent(recordings_folder)
        exhausted = usage["free_percent"] <= target_free_percent

    return {"deleted": deleted, "freed_bytes": freed_bytes, "final_free_percent": usage["free_percent"], "exhausted": exhausted}


# --------------------------------------------------------------- cross-process state + notification
#
# Cross-process handoff to the appliance-agent's own heartbeat gathering
# is now via GET /api/appliance/local-storage-state (main.py's own
# local_storage_state_endpoint(), read by metrics.py over localhost) --
# NOT a shared state file. Confirmed live on Ryzen: STATE_DIR
# (/var/lib/anyaicam) is mounted READ-ONLY inside this container by
# design (the containerized VMS app may read the appliance's own
# credential/identity files there, but must never write into that
# directory), so a file this module wrote into STATE_DIR failed on
# every single tick with a real, permanent "Read-only file system"
# error -- not a transient/best-effort failure at all, a structural one.
# local_storage_manager_state (the plain in-process dict already updated
# by the worker loop below) is itself the only "state" this module needs
# to publish; the HTTP endpoint reads it directly, live, with no
# intermediate file to go stale or fail to write.


def _notify_critical_storage(free_percent: float, exhausted: bool) -> None:
    """Reuses the existing appliance-events -> fanout_appliance_event()
    pipeline every camera detection event already flows through (POST
    /api/appliance/events, already wired to notification fanout in
    appliance_cloud.py) -- no new cloud-side notification plumbing
    needed, only a correctly-shaped event from the edge. event_type
    'storage_problem' matches notification_preferences.py's own
    customer-facing EVENT_TYPES label exactly (see notification_engine.
    py's SUPPORTED set fix in this same change)."""
    message = (
        "Local recording storage is critically low and automatic cleanup could not free enough space."
        if exhausted else
        "Local recording storage is critically low."
    )
    _control_plane_post("/api/appliance/events", {
        "events": [{
            "id": f"storage-problem-{secrets.token_hex(8)}",
            "event_type": "storage_problem",
            "camera_id": None,
            "timestamp": datetime.now().isoformat(),
            "severity": "critical",
            "message": f"{message} Free space: {free_percent:.1f}%.",
        }]
    })


# --------------------------------------------------------------- worker

async def local_storage_manager_worker(recordings_folder: Path, *, recording_start_fn, cloud_recording_s3_key_fn) -> None:
    """Top-level background task -- same shape as every other opt-in
    worker in this codebase (role/flag gate, sleep-forever when
    disabled, scan-and-sleep loop when enabled, exceptions logged and
    retried next cycle, never crash the process)."""
    import asyncio

    if RUNTIME_ROLE not in {"edge", "combined"} or not MANAGEMENT_ENABLED:
        local_storage_manager_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)

    local_storage_manager_state["worker_status"] = "running"
    logger.info("local_storage.worker_started auto_delete_enabled=%s", AUTO_DELETE_ENABLED)
    last_config_refresh = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_config_refresh >= CONFIG_REFRESH_SECONDS:
                await asyncio.to_thread(_refresh_camera_map)
                last_config_refresh = now

            policy = await asyncio.to_thread(_local_storage_policy)
            usage = await asyncio.to_thread(disk_usage_percent, recordings_folder)
            disk_needs_cleanup = usage["free_percent"] <= policy["reserved_free_percent"]
            retention_configured = policy.get("local_retention_days") is not None
            attempt_cleanup = disk_needs_cleanup or retention_configured

            cleanup_result = None
            if attempt_cleanup and AUTO_DELETE_ENABLED:
                cleanup_result = await asyncio.to_thread(
                    run_cleanup_pass, recordings_folder,
                    recording_start_fn=recording_start_fn, cloud_recording_s3_key_fn=cloud_recording_s3_key_fn, policy=policy,
                )
                if cleanup_result["deleted"] > 0:
                    local_storage_manager_state["last_cleanup_at"] = datetime.now().isoformat()
                final_free_percent = cleanup_result["final_free_percent"]
                # run_cleanup_pass()'s own exhausted is already scoped to
                # the free-space reserve condition alone (see its
                # docstring) -- a retention target alone never sets it.
                exhausted = cleanup_result["exhausted"]
            else:
                final_free_percent = usage["free_percent"]
                # Only the free-space reserve condition can make a scan
                # "exhausted" (and therefore forced to 'critical' below)
                # when no cleanup pass ran at all -- a configured
                # retention target, even with auto-delete off, is never
                # itself a silent-recording-failure risk the way running
                # out of disk is.
                exhausted = disk_needs_cleanup and not AUTO_DELETE_ENABLED

            state = classify_storage_state(
                final_free_percent, reserved_free_percent=policy["reserved_free_percent"],
                warning_free_percent=policy["warning_free_percent"],
                cleanup_in_progress=bool(cleanup_result and cleanup_result["deleted"] > 0),
            )
            # A cleanup pass that ran but could not restore enough space
            # (nothing left it was allowed to delete, or auto-delete is
            # disabled while genuinely needed) is 'critical' regardless
            # of classify_storage_state()'s own threshold math -- "never
            # allow silent recording failure" takes precedence over the
            # plain percentage band.
            if exhausted:
                state = "critical"

            local_storage_manager_state["storage_state"] = state
            local_storage_manager_state["free_percent"] = final_free_percent
            local_storage_manager_state["last_scan_at"] = datetime.now().isoformat()
            local_storage_manager_state["last_error"] = None

            if state == "critical":
                await asyncio.to_thread(_notify_critical_storage, final_free_percent, exhausted)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            local_storage_manager_state["last_error"] = str(error)
            logger.warning("local_storage.worker_iteration_failed error=%s", error)
        await asyncio.sleep(SCAN_SECONDS)
