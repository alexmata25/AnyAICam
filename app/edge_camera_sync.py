"""Cloud->edge camera-configuration sync (2026-09-12).

BUG this closes: cloud-side camera provisioning (POST /api/customer/
cameras/provision) already worked correctly end to end -- the customer
provisions a camera, the appliance-agent verifies it against the real
device via RTSP DESCRIBE, and the cloud records a real cameras/
camera_credentials row. But nothing ever propagated that provisioned
camera onto the edge appliance's OWN local database. main.py's
_provisioned_camera_stream() (camera_url()'s real source) reads only the
LOCAL cameras/camera_credentials tables -- on a freshly-provisioned
appliance those were empty, so camera_url() always raised
CameraNotConfiguredError and process_supervisor() never started FFmpeg
for that camera_number at all: no stream, no HLS output, no recording,
surfacing to the customer as "vms_stream_offline" -- a state that looks
like an RTSP/auth failure but is actually "the edge never learned this
camera exists."

This module is the fix: it runs only when RUNTIME_ROLE is edge/combined
(the cloud instance of this same app/ codebase has nothing local to
reconcile -- it IS the authoritative source), and does two things,
both idempotent and restart-safe:

1. Camera metadata (id, camera_number, device_key, onvif_endpoint,
   name, status, recording_mode, people_counting_enabled,
   smart_motion_enabled, lpr_enabled, ppe_enabled) -- pulled from
   the existing, unchanged GET /api/appliance/configuration, the same
   endpoint analytics_sync.py and recording_uploader.py already poll for
   their own purposes, deliberately never extended to carry credentials
   (see its own 'camera_credentials_included': False). Upserted into the
   local `cameras` table, keyed by the SAME id the cloud already
   assigned -- never a locally-generated id -- so every other place in
   this codebase that already references a camera by this id (Live View
   queueing, media-uri submission, appliance_camera_status reporting)
   keeps working unmodified.

2. Credentials -- never fetched from any cloud API (see this module's
   own commit message / PROJECT_CHECKPOINT.md for why a repeatable
   plaintext-credential-fetch endpoint was deliberately rejected).
   Instead, main.py's own POST /api/local/provisioned-camera-credential
   (loopback-only, called by this exact appliance's own agent right
   after its one-time RTSP DESCRIBE verification succeeds -- see that
   route's docstring) already encrypted and stored the credential in
   `pending_camera_credentials`, keyed by device_key, the moment it
   arrived -- before the cloud had necessarily assigned this camera a
   camera_id yet. Once step 1 above has learned that camera_id (matched
   by device_key), this step moves the already-encrypted blob from
   pending_camera_credentials into the real camera_credentials table
   (keyed by camera_id, exactly what _provisioned_camera_stream() reads)
   and deletes the pending row -- a pure ciphertext relocation. No
   plaintext credential is ever read, held, or written by this module.

Removal/deprovisioning is explicitly NOT handled here: a camera that
disappears from the cloud's configuration response is left exactly as
it is locally (name/device_key/onvif_endpoint/credential all untouched).
Silently deleting a local camera the moment a cloud sync cycle stops
reporting it (which could just as easily mean a transient network/cloud
hiccup as a real deprovision) is a distinct, real design decision this
fix deliberately does not make -- see PROJECT_CHECKPOINT.md.

Cross-appliance isolation: on a genuine edge appliance there is only
ever one appliance's own identity (persisted_identity()'s appliance_id),
and GET /api/appliance/configuration is itself already scoped
server-side to `WHERE appliance_id=?` for the calling (authenticated)
appliance only -- this module never sees, and could not act on, another
appliance's cameras even if one existed in the same local database (the
test suite proves this by seeding a second appliance's cameras locally
and asserting they're never touched by a sync run for the first).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

logger = logging.getLogger("anyaicam.edge_camera_sync")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
SYNC_INTERVAL_SECONDS = int(os.environ.get("ANYAICAM_CAMERA_CONFIG_SYNC_INTERVAL_SECONDS", "60"))

sync_state: dict = {"last_run_at": None, "last_error": None, "last_synced_count": 0}


def _control_plane_headers(appliance_id: str, credential: str) -> dict:
    # Deliberately duplicated (not imported) from recording_uploader.py's
    # identical helper -- the same explicit per-module scope decision
    # that file's own docstring already made relative to
    # live_relay_uploader.py, kept consistent here.
    import secrets
    return {
        "User-Agent": "AnyAiCam-EdgeCameraSync/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }


def _control_plane_get(path: str, appliance_id: str, credential: str) -> dict | None:
    if not CLOUD_URL:
        return None
    request = urllib.request.Request(
        CLOUD_URL + path, headers=_control_plane_headers(appliance_id, credential), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("edge_camera_sync.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        logger.warning("edge_camera_sync.control_plane_unreachable path=%s error=%s", path, error)
        return None


def sync_provisioned_cameras() -> dict:
    """One reconciliation pass. Safe to call repeatedly (idempotent) and
    safe to call after any restart (reads only durable local/cloud
    state, no in-memory-only assumptions). Returns a small stats dict
    for logging/tests -- never anything credential-shaped."""
    if RUNTIME_ROLE not in ("edge", "combined"):
        return {"status": "not_applicable", "runtime_role": RUNTIME_ROLE}

    from appliance_activation import load_persisted_identity
    identity = load_persisted_identity()
    if not identity:
        return {"status": "not_activated"}

    response = _control_plane_get("/api/appliance/configuration", identity["appliance_id"], identity["credential"])
    if not isinstance(response, dict):
        return {"status": "unreachable"}
    cloud_cameras = response.get("cameras")
    if not isinstance(cloud_cameras, list):
        return {"status": "malformed_response"}

    from partner_db import connection
    now = datetime.now().isoformat()
    synced = 0
    credentials_moved = 0
    with connection() as db:
        # cameras.customer_id/site_id carry a FOREIGN KEY (see partner_db.
        # py's schema) written for the cloud's own multi-tenant admin/
        # partner CRUD flows, where a customers/sites row always exists
        # before a camera does. On a genuine edge appliance the local
        # database only ever mirrors this one appliance's own cameras --
        # it was never meant to hold a full local replica of the tenant
        # hierarchy (customers/sites/partners), and building one here
        # would mean fabricating placeholder customer/site records this
        # module has no real data for. The customer_id/site_id values
        # themselves are not fabricated -- they come from this exact
        # appliance's own cryptographically-issued activation identity
        # (load_persisted_identity()) -- only the referential-integrity
        # check against local parent rows that don't exist here is
        # skipped, scoped to this one connection only.
        db.execute("PRAGMA foreign_keys=OFF")
        for item in cloud_cameras:
            if not isinstance(item, dict):
                continue
            camera_id = str(item.get("id") or "").strip()
            if not camera_id:
                continue
            camera_number = item.get("camera_number")
            device_key = item.get("device_key")
            talk_down = item.get("talk_down")
            talk_down_supported = None
            talk_down_metadata = None
            talk_down_verified_at = None
            if isinstance(talk_down, dict) and "supported" in talk_down:
                talk_down_supported = 1 if talk_down.get("supported") else 0
                metadata = talk_down.get("metadata")
                talk_down_metadata = json.dumps(metadata) if metadata else None
                talk_down_verified_at = now
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,device_key,"
                "onvif_endpoint,resolution,cloud_recording_mode,people_counting_enabled,smart_motion_enabled,"
                "lpr_enabled,ppe_enabled,talk_down_supported,talk_down_metadata,talk_down_verified_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,camera_number=excluded.camera_number,"
                "status=excluded.status,device_key=excluded.device_key,onvif_endpoint=excluded.onvif_endpoint,"
                "resolution=excluded.resolution,cloud_recording_mode=excluded.cloud_recording_mode,"
                "people_counting_enabled=excluded.people_counting_enabled,"
                "smart_motion_enabled=excluded.smart_motion_enabled,lpr_enabled=excluded.lpr_enabled,"
                "ppe_enabled=excluded.ppe_enabled,"
                "talk_down_supported=CASE WHEN excluded.talk_down_supported IS NOT NULL THEN excluded.talk_down_supported ELSE cameras.talk_down_supported END,"
                "talk_down_metadata=CASE WHEN excluded.talk_down_supported IS NOT NULL THEN excluded.talk_down_metadata ELSE cameras.talk_down_metadata END,"
                "talk_down_verified_at=CASE WHEN excluded.talk_down_supported IS NOT NULL THEN excluded.talk_down_verified_at ELSE cameras.talk_down_verified_at END",
                (
                    camera_id, identity["customer_id"], identity["site_id"], identity["appliance_id"],
                    item.get("name") or "Camera", camera_number, item.get("status"), device_key,
                    item.get("onvif_endpoint"), item.get("resolution"), item.get("recording_mode"),
                    item.get("people_counting_enabled"), item.get("smart_motion_enabled"),
                    item.get("lpr_enabled"), item.get("ppe_enabled"),
                    talk_down_supported, talk_down_metadata, talk_down_verified_at, now,
                ),
            )
            synced += 1

            if not device_key:
                continue
            already_has_credential = db.execute(
                "SELECT 1 FROM camera_credentials WHERE camera_id=?", (camera_id,)
            ).fetchone()
            if already_has_credential:
                continue
            pending = db.execute(
                "SELECT encrypted_blob FROM pending_camera_credentials WHERE device_key=?", (device_key,)
            ).fetchone()
            if not pending:
                continue
            # Ciphertext relocation only -- this process never decrypts,
            # inspects, or logs the value it is moving.
            db.execute(
                "INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(camera_id) DO NOTHING",
                (camera_id, pending["encrypted_blob"], now, now),
            )
            db.execute("DELETE FROM pending_camera_credentials WHERE device_key=?", (device_key,))
            credentials_moved += 1

    result = {"status": "ok", "synced": synced, "credentials_moved": credentials_moved}
    sync_state["last_run_at"] = now
    sync_state["last_error"] = None
    sync_state["last_synced_count"] = synced
    return result


async def camera_configuration_sync_worker() -> None:
    if RUNTIME_ROLE not in ("edge", "combined"):
        while True:
            await asyncio.sleep(3600)
    logger.info("edge_camera_sync.worker_started")
    while True:
        try:
            result = await asyncio.to_thread(sync_provisioned_cameras)
            if result.get("status") not in ("ok", "not_activated"):
                logger.debug("edge_camera_sync.cycle_result result=%s", result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            sync_state["last_error"] = str(error)
            logger.warning("edge_camera_sync.worker_iteration_failed error=%s", error)
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
