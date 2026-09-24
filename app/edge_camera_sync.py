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

import product_mode

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


def _reconcile_analytics_rules(db, appliance_id: str, cloud_rules: list, now: str) -> int:
    """Fully replaces this appliance's LOCAL customer_analytics_rules
    mirror with exactly what this poll's response just reported enabled
    for it (2026-09-21) -- the cloud->appliance half of the rule-
    delivery path documented as missing in docs/intrusion-line-
    crossing-customer-ui-gap.md. Reuses the existing GET /api/appliance/
    configuration poll -- no new endpoint, no new cadence, no second
    uncontrolled channel -- and the exact same `appliance_id` ownership
    boundary sync_provisioned_cameras() already trusts for cameras.

    Deliberately NOT sync_provisioned_cameras()'s own "never delete a
    stale camera locally" policy (see this module's own docstring for
    why a camera briefly missing from a poll is treated as a possible
    transient gap, not a real deprovision): appliance_configuration()
    only ever reports a rule here while it is enabled=1 in the cloud, so
    a rule's absence from this list always means exactly one thing --
    it is no longer enabled for this appliance -- and continuing to
    enforce a rule the customer just disabled or deleted (e.g. an
    intrusion alert still firing for a zone they removed) is a real
    customer-facing correctness bug, not a safe fail-open default.
    Local rows for cameras belonging to a DIFFERENT appliance are never
    touched (the DELETE below is scoped to `WHERE appliance_id=?`,
    matching every other query in this reconciliation).

    Written into the exact same `customer_analytics_rules` table name/
    schema the cloud customer portal writes into (not a second, parallel
    table) -- the same 1:1 table-mirroring convention already used for
    customers/sites/appliances/cameras above, and per explicit
    instruction, never the legacy analytics_rules.json file."""
    cloud_ids: set[str] = set()
    for item in cloud_rules:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id") or "").strip()
        camera_id = str(item.get("camera_id") or "").strip()
        rule_type = str(item.get("rule_type") or "").strip()
        if not rule_id or not camera_id or not rule_type:
            continue
        cloud_ids.add(rule_id)
        geometry = item.get("geometry")
        db.execute(
            "INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at,created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,NULL) "
            "ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,site_id=excluded.site_id,appliance_id=excluded.appliance_id,"
            "camera_id=excluded.camera_id,rule_type=excluded.rule_type,name=excluded.name,direction=excluded.direction,"
            "geometry_json=excluded.geometry_json,enabled=1,updated_at=excluded.updated_at",
            (
                rule_id, item.get("customer_id"), item.get("site_id"), appliance_id, camera_id,
                rule_type, item.get("name") or "", item.get("direction"),
                json.dumps(geometry if isinstance(geometry, list) else []),
                now, item.get("updated_at") or now,
            ),
        )
    existing_ids = {
        row["id"] for row in db.execute(
            "SELECT id FROM customer_analytics_rules WHERE appliance_id=?", (appliance_id,)
        ).fetchall()
    }
    for stale_id in existing_ids - cloud_ids:
        db.execute("DELETE FROM customer_analytics_rules WHERE id=?", (stale_id,))
    return len(cloud_ids)


def _reconcile_aac_voice_call(db, appliance_id: str, cloud_config: dict, now: str) -> dict:
    """Mirrors the cloud-owned AAC Voice Call configuration for THIS
    appliance's own cameras into the local aac_voice_call_entrance_
    cameras / aac_voice_call_site_greetings tables (2026-09-24) -- the
    config-down half of the Voice Call cloud/edge split. The edge's
    detection hook (aac_voice_call.handle_edge_person_detected()) reads
    only these local copies, so the entrance-camera check and the
    greeting keep working through a cloud/internet outage.

    Same full-replace policy as _reconcile_analytics_rules() above:
    appliance_configuration() only reports ENABLED entrance cameras, so
    absence means "no longer an entrance camera" and the local row is
    removed -- a camera the customer turned off must stop greeting
    visitors. Scoped to cameras whose local appliance_id is this
    appliance; rows for any other appliance's cameras, and any camera the
    cloud names that isn't this appliance's, are never touched."""
    local_cameras = {
        item["id"]: dict(item)
        for item in db.execute("SELECT id,customer_id,site_id FROM cameras WHERE appliance_id=?", (appliance_id,)).fetchall()
    }
    wanted_cameras: set[str] = set()
    entrance_items = cloud_config.get("entrance_cameras")
    for item in entrance_items if isinstance(entrance_items, list) else []:
        if not isinstance(item, dict):
            continue
        camera_id = str(item.get("camera_id") or "").strip()
        camera = local_cameras.get(camera_id)
        if not camera:
            continue
        greeting_text = str(item.get("greeting_text") or "").strip() or None
        wanted_cameras.add(camera_id)
        db.execute(
            "INSERT INTO aac_voice_call_entrance_cameras(camera_id,customer_id,enabled,configured_at,configured_by,greeting_text) "
            "VALUES(?,?,1,?,?,?) "
            "ON CONFLICT(camera_id) DO UPDATE SET customer_id=excluded.customer_id,enabled=1,greeting_text=excluded.greeting_text",
            (camera_id, camera["customer_id"], now, "cloud-sync", greeting_text),
        )
    for camera_id in local_cameras:
        if camera_id not in wanted_cameras:
            db.execute("DELETE FROM aac_voice_call_entrance_cameras WHERE camera_id=?", (camera_id,))

    site_customers = {camera["site_id"]: camera["customer_id"] for camera in local_cameras.values() if camera.get("site_id")}
    wanted_sites: set[str] = set()
    greeting_items = cloud_config.get("site_greetings")
    for item in greeting_items if isinstance(greeting_items, list) else []:
        if not isinstance(item, dict):
            continue
        site_id = str(item.get("site_id") or "").strip()
        greeting_text = str(item.get("greeting_text") or "").strip()
        if site_id not in site_customers or not greeting_text:
            continue
        wanted_sites.add(site_id)
        db.execute(
            "INSERT INTO aac_voice_call_site_greetings(customer_id,site_id,greeting_text,updated_at,updated_by) VALUES(?,?,?,?,?) "
            "ON CONFLICT(customer_id,site_id) DO UPDATE SET greeting_text=excluded.greeting_text",
            (site_customers[site_id], site_id, greeting_text, now, "cloud-sync"),
        )
    for site_id, customer_id in site_customers.items():
        if site_id not in wanted_sites:
            db.execute("DELETE FROM aac_voice_call_site_greetings WHERE customer_id=? AND site_id=?", (customer_id, site_id))
    return {"entrance_cameras": len(wanted_cameras), "site_greetings": len(wanted_sites)}


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
    # analytics_rules (2026-09-21): a missing/malformed field here is
    # treated as "this control-plane response didn't carry rule data
    # this cycle" -- local rule state is simply left untouched (None
    # signals "skip" to the reconciliation call below), exactly the
    # same fail-safe posture as this function's own malformed_response
    # bail-out for cameras, but scoped to just this one field rather
    # than aborting the whole cycle's camera sync over it.
    cloud_rules = response.get("analytics_rules")
    cloud_rules = cloud_rules if isinstance(cloud_rules, list) else None
    # aac_voice_call (2026-09-24): same "missing/malformed means skip,
    # leave local state untouched" posture as analytics_rules -- an older
    # control plane that doesn't send this field can never wipe the
    # edge's local entrance-camera configuration.
    cloud_aac_voice_call = response.get("aac_voice_call")
    cloud_aac_voice_call = cloud_aac_voice_call if isinstance(cloud_aac_voice_call, dict) else None

    # product_mode (2026-09-21): this appliance's real, entitlement-
    # derived Local/Hybrid mode, from the same already-polled response --
    # no separate endpoint or cadence. persist_mode() itself no-ops (and
    # returns False) for an empty/unrecognized value or a value equal to
    # what's already persisted -- an appliance whose customer has no
    # active camera-slot entitlement yet, or whose mode hasn't changed,
    # writes nothing new here. A True return means the mode actually
    # changed (e.g. a Hybrid upgrade purchase just completed) -- flags
    # for the caller that this process needs a restart to pick up the
    # new mode's flag defaults (see product_mode.resolve_cloud_flag()),
    # since every governed flag is still read once at import time.
    restart_required = product_mode.persist_mode(str(response.get("product_mode") or ""))

    from partner_db import connection
    now = datetime.now().isoformat()
    synced = 0
    credentials_moved = 0
    with connection() as db:
        # cameras.customer_id/site_id carry a FOREIGN KEY (see partner_db.
        # py's schema), and so does the recordings table's own customer_
        # id/site_id/appliance_id (written by main.py's _catalog_local_
        # recordings_for_camera() whenever a new recording file is
        # discovered, for both Event-mode and Continuous-mode cameras
        # alike). Confirmed live on Ryzen (2026-09-20): with no local
        # customers/sites/appliances row, that INSERT 500'd every time a
        # genuinely new file appeared -- an appliance-wide Playback
        # defect, not specific to any one camera or recording mode.
        #
        # response['identity'] (appliance_cloud.py's appliance_
        # configuration()) carries this exact appliance's own real
        # partner/customer/site/appliance rows -- not fabricated
        # placeholders, the actual cloud records this appliance's own
        # cryptographically-issued activation identity already
        # authorizes it to read (customer_id/site_id/appliance_id come
        # from that same identity for the camera upsert below). Written
        # here, in FK dependency order (partner -> customer -> site ->
        # appliance), before the camera upsert loop, so referential
        # integrity is genuinely satisfied rather than bypassed.
        identity_payload = response.get("identity") if isinstance(response.get("identity"), dict) else {}
        partner_data = identity_payload.get("partner")
        customer_data = identity_payload.get("customer")
        site_data = identity_payload.get("site")
        appliance_data = identity_payload.get("appliance")

        if partner_data and partner_data.get("id"):
            db.execute(
                "INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,approval_status=excluded.approval_status",
                (partner_data["id"], partner_data.get("name") or "Partner", partner_data.get("approval_status") or "approved", "real", now),
            )
        if customer_data and customer_data.get("id") and customer_data.get("email"):
            db.execute(
                "INSERT INTO customers(id,partner_id,name,company,email,phone,status,trial_status,billing_status,source,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET partner_id=excluded.partner_id,name=excluded.name,company=excluded.company,"
                "email=excluded.email,phone=excluded.phone,status=excluded.status,trial_status=excluded.trial_status,billing_status=excluded.billing_status",
                (
                    customer_data["id"], customer_data.get("partner_id"), customer_data.get("name") or "Customer",
                    customer_data.get("company"), customer_data["email"], customer_data.get("phone"),
                    customer_data.get("status") or "active", customer_data.get("trial_status"), customer_data.get("billing_status"),
                    "real", now,
                ),
            )
        if site_data and site_data.get("id") and site_data.get("customer_id"):
            db.execute(
                "INSERT INTO sites(id,customer_id,name,address,site_type,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,name=excluded.name,address=excluded.address,site_type=excluded.site_type",
                (site_data["id"], site_data["customer_id"], site_data.get("name") or "Site", site_data.get("address"), site_data.get("site_type"), now),
            )
        if appliance_data and appliance_data.get("id") and appliance_data.get("customer_id") and appliance_data.get("site_id") and appliance_data.get("cloud_id"):
            db.execute(
                "INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,site_id=excluded.site_id,cloud_id=excluded.cloud_id,partner_id=excluded.partner_id",
                (appliance_data["id"], appliance_data["customer_id"], appliance_data["site_id"], appliance_data["cloud_id"], appliance_data.get("partner_id"), now),
            )

        # Transitional fallback only: an older control-plane deployment
        # that hasn't shipped the identity payload yet would otherwise
        # leave cameras.customer_id/site_id (still sourced from this
        # appliance's own activation identity, exactly as before)
        # pointing at parent rows this pass had no data to materialize --
        # never the normal path once both sides of a deployment are current.
        if not (partner_data and customer_data and site_data and appliance_data):
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
                "lpr_enabled,ppe_enabled,local_recording_mode,local_recording_pre_roll_seconds,"
                "local_recording_post_roll_seconds,local_recording_merge_gap_seconds,local_recording_max_event_seconds,"
                "talk_down_supported,talk_down_metadata,talk_down_verified_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,camera_number=excluded.camera_number,"
                "status=excluded.status,device_key=excluded.device_key,onvif_endpoint=excluded.onvif_endpoint,"
                "resolution=excluded.resolution,cloud_recording_mode=excluded.cloud_recording_mode,"
                "people_counting_enabled=excluded.people_counting_enabled,"
                "smart_motion_enabled=excluded.smart_motion_enabled,lpr_enabled=excluded.lpr_enabled,"
                "ppe_enabled=excluded.ppe_enabled,"
                "local_recording_mode=excluded.local_recording_mode,"
                "local_recording_pre_roll_seconds=excluded.local_recording_pre_roll_seconds,"
                "local_recording_post_roll_seconds=excluded.local_recording_post_roll_seconds,"
                "local_recording_merge_gap_seconds=excluded.local_recording_merge_gap_seconds,"
                "local_recording_max_event_seconds=excluded.local_recording_max_event_seconds,"
                "talk_down_supported=CASE WHEN excluded.talk_down_supported IS NOT NULL THEN excluded.talk_down_supported ELSE cameras.talk_down_supported END,"
                "talk_down_metadata=CASE WHEN excluded.talk_down_supported IS NOT NULL THEN excluded.talk_down_metadata ELSE cameras.talk_down_metadata END,"
                "talk_down_verified_at=CASE WHEN excluded.talk_down_supported IS NOT NULL THEN excluded.talk_down_verified_at ELSE cameras.talk_down_verified_at END",
                (
                    camera_id, identity["customer_id"], identity["site_id"], identity["appliance_id"],
                    item.get("name") or "Camera", camera_number, item.get("status"), device_key,
                    item.get("onvif_endpoint"), item.get("resolution"), item.get("recording_mode"),
                    item.get("people_counting_enabled"), item.get("smart_motion_enabled"),
                    item.get("lpr_enabled"), item.get("ppe_enabled"),
                    item.get("local_recording_mode"), item.get("local_recording_pre_roll_seconds"),
                    item.get("local_recording_post_roll_seconds"), item.get("local_recording_merge_gap_seconds"),
                    item.get("local_recording_max_event_seconds"),
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

        # Runs inside this SAME transaction, after every camera above has
        # already been upserted -- customer_analytics_rules' own FOREIGN
        # KEY(camera_id) REFERENCES cameras(id) is only ever satisfiable
        # once that camera's row genuinely exists locally, and (per
        # appliance_configuration()'s own scoping) every rule reported
        # here belongs to a camera that was in this exact same
        # cloud_cameras list moments ago.
        rules_synced = _reconcile_analytics_rules(db, identity["appliance_id"], cloud_rules, now) if cloud_rules is not None else None
        # Same transaction, same ordering reason as the rules above: the
        # entrance-camera rows reference cameras upserted just above.
        aac_voice_call_synced = (
            _reconcile_aac_voice_call(db, identity["appliance_id"], cloud_aac_voice_call, now)
            if cloud_aac_voice_call is not None else None
        )

    result = {
        "status": "ok", "synced": synced, "credentials_moved": credentials_moved,
        "product_mode_restart_required": restart_required, "rules_synced": rules_synced,
        "aac_voice_call_synced": aac_voice_call_synced,
    }
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
