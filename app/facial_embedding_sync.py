"""AAC edge/cloud enrollment sync -- Phase 2.

Resolves the largest of the three split-topology gaps identified in
the Phase 1 Codex review: enrolled embeddings only ever existed in
whichever single database the matching code ran against, so a split
edge/cloud deployment (enrollment managed on the cloud customer portal,
matching run on the edge appliance against the camera's own frames) had
no way for an edge appliance to ever see what was enrolled.

This is the OPPOSITE sync direction from every other worker in this
codebase (analytics_sync.py, recording_uploader.py: edge -> cloud).
Enrollment is cloud-authoritative; this worker periodically PULLS the
current, complete facial_people/facial_embeddings/facial_watchlists/
facial_watchlist_members snapshot for this appliance's own customer_id
from the cloud (GET /api/appliance/facial-directory in
appliance_cloud.py, authenticated via the SAME authenticate_appliance()
every other appliance route already uses -- no new identity/auth
system) and mirrors it into this appliance's own LOCAL copy of those
same tables, which facial_people.py/facial_events.py already read from
via the ordinary database_backend.connect() every other part of this
codebase uses.

Full-replace, not incremental: each successful sync DELETEs this
customer's existing local facial_people/facial_embeddings/
facial_watchlists/facial_watchlist_members rows and INSERTs the fresh
snapshot, inside one transaction. This is deliberately simple, and
correctly handles the case that actually matters most for biometric
data: a person deleted on the cloud (which Phase 1's
facial_people.delete_person() already hard-deletes there) must not
linger as a stale, still-matchable embedding on an edge appliance
forever -- a full-replace makes that automatic, with no separate
deletion-propagation logic to get wrong. facial_events (event history)
and facial_rules/facial_settings (per-deployment configuration) are
never touched by this worker -- only the four enrollment-data tables
above are ever replaced.

Tenant isolation: the cloud route scopes its response to the
authenticated appliance's own customer_id, exactly like every other
appliance-scoped cloud route (GET /api/appliance/configuration, etc.)
-- this worker has no ability to request or receive another tenant's
enrollment data.

Disabled by default (ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED=false).
"""

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from database_backend import connect
import product_mode

logger = logging.getLogger("anyaicam.facial_embedding_sync")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
# 2026-09-21: governed by product_mode.py (Local defaults this off,
# Hybrid defaults it on) -- an explicit ANYAICAM_FACIAL_EMBEDDING_SYNC_
# ENABLED still always wins, unchanged from before product modes
# existed. This is the cloud-sync direction only, independent of
# ANYAICAM_FACIAL_RECOGNITION_ENABLED (facial_recognition.py), which
# gates the paid Face Access entitlement itself and is NOT governed by
# product mode -- a Local customer can buy Face Access without that
# implying Hybrid.
FACIAL_EMBEDDING_SYNC_ENABLED = product_mode.resolve_cloud_flag("ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED")
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
STATE_DIR = Path(os.environ.get("ANYAICAM_STATE_DIR", "/var/lib/anyaicam"))
CREDENTIAL_FILE = STATE_DIR / "credential.json"

SYNC_INTERVAL_SECONDS = max(60.0, float(os.environ.get("ANYAICAM_FACIAL_EMBEDDING_SYNC_INTERVAL_SECONDS", "300.0")))
# Conditional sync (2026-09-24): the cloud returns a directory_version
# (content hash) with every directory; sending back the version this
# appliance already applied gets a tiny "unchanged" reply instead of every
# embedding again (~28.5 KB each), and skips the local delete-and-reinsert
# too. A full, unconditional sync still happens at least this often as a
# safety net, and after every process start.
FULL_RESYNC_SECONDS = max(SYNC_INTERVAL_SECONDS, float(os.environ.get("ANYAICAM_FACIAL_EMBEDDING_FULL_RESYNC_SECONDS", "21600")))

facial_embedding_sync_state: dict = {
    "worker_status": "disabled",
    "last_sync_at": None,
    "last_error": None,
    "last_summary": None,
}

_state_lock = threading.Lock()
# What this process last applied locally -- see FULL_RESYNC_SECONDS.
_applied_directory: dict = {"customer_id": None, "version": None, "applied_at": None}


def reset_sync_state() -> None:
    """Forget the applied version so the next sync is a full one."""
    with _state_lock:
        _applied_directory.update(customer_id=None, version=None, applied_at=None)


def _load_appliance_identity() -> tuple[str, str] | None:
    """Duplicated from analytics_sync.py's own identical helper,
    deliberately -- this codebase's established convention for its
    sync-worker modules is independent duplication of this exact
    shape, not a shared import (see analytics_sync.py's own
    _refresh_camera_map() docstring for why)."""
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


def _control_plane_get(path: str) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    headers = {
        "User-Agent": "AnyAiCam-FacialEmbeddingSync/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }
    request = urllib.request.Request(CLOUD_URL + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("facial_embedding_sync.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("facial_embedding_sync.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _replace_local_directory(customer_id: str, directory: dict) -> dict:
    """The one write path: replaces this customer_id's local
    facial_people/facial_embeddings/facial_watchlists/
    facial_watchlist_members rows with the given snapshot, inside one
    transaction. Never touches facial_events/facial_rules/
    facial_settings. Returns a small summary dict for logging/state."""
    people = [item for item in directory.get("people", []) if isinstance(item, dict)]
    embeddings = [item for item in directory.get("embeddings", []) if isinstance(item, dict)]
    watchlists = [item for item in directory.get("watchlists", []) if isinstance(item, dict)]
    memberships = [item for item in directory.get("watchlist_members", []) if isinstance(item, dict)]
    now = datetime.now().isoformat()
    with connect() as db:
        # Children before parents, matching the schema's own FK
        # dependency order -- see db_migrations.py's facial_* block.
        db.execute("DELETE FROM facial_watchlist_members WHERE watchlist_id IN (SELECT id FROM facial_watchlists WHERE customer_id=?)", (customer_id,))
        db.execute("DELETE FROM facial_embeddings WHERE customer_id=?", (customer_id,))
        db.execute("DELETE FROM facial_watchlists WHERE customer_id=?", (customer_id,))
        db.execute("DELETE FROM facial_people WHERE customer_id=?", (customer_id,))
        for person in people:
            db.execute(
                "INSERT INTO facial_people(id,customer_id,site_id,external_reference,display_name,status,notes,created_at,updated_at,created_by) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    person.get("id"), customer_id, person.get("site_id"), person.get("external_reference"),
                    person.get("display_name"), person.get("status", "active"), person.get("notes"),
                    person.get("created_at", now), person.get("updated_at", now), person.get("created_by"),
                ),
            )
        for embedding in embeddings:
            db.execute(
                "INSERT INTO facial_embeddings(id,person_id,customer_id,engine,engine_version,embedding_json,source_image_path,quality,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    embedding.get("id"), embedding.get("person_id"), customer_id, embedding.get("engine"),
                    embedding.get("engine_version", ""), embedding.get("embedding_json"),
                    None,  # the enrollment image/crop itself never syncs to the edge -- only the embedding vector does
                    embedding.get("quality"), embedding.get("created_at", now),
                ),
            )
        for watchlist in watchlists:
            db.execute(
                "INSERT INTO facial_watchlists(id,customer_id,site_id,name,classification,description,created_at,updated_at,created_by) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    watchlist.get("id"), customer_id, watchlist.get("site_id"), watchlist.get("name"),
                    watchlist.get("classification", "alert"), watchlist.get("description"),
                    watchlist.get("created_at", now), watchlist.get("updated_at", now), watchlist.get("created_by"),
                ),
            )
        for membership in memberships:
            db.execute(
                "INSERT INTO facial_watchlist_members(watchlist_id,person_id,added_at,added_by) VALUES(?,?,?,?)",
                (membership.get("watchlist_id"), membership.get("person_id"), membership.get("added_at", now), membership.get("added_by")),
            )
    return {"people": len(people), "embeddings": len(embeddings), "watchlists": len(watchlists), "watchlist_members": len(memberships)}


def sync_facial_directory() -> dict:
    """Runs one full sync. Never raises -- an unreachable/misconfigured
    cloud, or a missing appliance identity, is logged and reported in
    facial_embedding_sync_state, and simply leaves the previous local
    snapshot in place (fail-safe: a transient outage never deletes
    already-synced enrollment data without anything to replace it
    with) rather than deleting local data at the START of a failed
    fetch."""
    identity = _load_appliance_identity()
    if identity is None:
        return {"status": "no_identity"}
    with _state_lock:
        applied = dict(_applied_directory)
    conditional = bool(
        applied["version"] and applied["applied_at"] is not None
        and time.monotonic() - applied["applied_at"] < FULL_RESYNC_SECONDS
    )
    path = "/api/appliance/facial-directory"
    if conditional:
        path += "?if_version=" + urllib.parse.quote(str(applied["version"]), safe="")
    response = _control_plane_get(path)
    if not isinstance(response, dict) or "customer_id" not in response:
        return {"status": "fetch_failed"}
    customer_id = str(response.get("customer_id") or "").strip()
    if not customer_id:
        return {"status": "fetch_failed"}
    if response.get("unchanged") is True:
        if conditional and customer_id == applied["customer_id"] and response.get("directory_version") == applied["version"]:
            # Local tables already hold exactly this snapshot -- no data
            # transferred, nothing rewritten, recognition keeps running.
            return {"status": "unchanged"}
        # An "unchanged" reply that doesn't match what this process applied
        # (e.g. the appliance was re-assigned to a different customer)
        # can't be trusted -- drop the version so the next sync is full.
        reset_sync_state()
        return {"status": "fetch_failed"}
    summary = _replace_local_directory(customer_id, response)
    version = str(response.get("directory_version") or "").strip() or None
    with _state_lock:
        # An older cloud sends no directory_version: stay unconditional.
        _applied_directory.update(customer_id=customer_id, version=version, applied_at=time.monotonic() if version else None)
    summary["status"] = "synced"
    summary.update(engine_compatibility(response.get("embeddings")))
    return summary


def _active_engine_key() -> tuple[str, str]:
    import facial_recognition
    engine = facial_recognition.get_engine()
    return str(getattr(engine, "name", "unknown")), str(getattr(engine, "version", "0"))


def engine_compatibility(embeddings, active: tuple[str, str] | None = None) -> dict:
    """How many synced enrollment embeddings this appliance's active face
    engine can actually match against. Matching only ever compares
    embeddings from the same engine and version, so enrollments made on a
    cloud running a different engine (e.g. Haar there, ArcFace here, or
    the reverse after switching ANYAICAM_FACE_ENGINE on one side only)
    sync fine but can never produce a known match -- previously with no
    sign of it anywhere. Read-only; never changes or deletes enrollments."""
    items = [item for item in embeddings or [] if isinstance(item, dict)]
    if not items:
        return {"embeddings_for_active_engine": 0}
    active = active or _active_engine_key()
    matching = sum(1 for item in items if (str(item.get("engine") or ""), str(item.get("engine_version") or "")) == active)
    result = {"active_engine": f"{active[0]}/{active[1]}", "embeddings_for_active_engine": matching}
    if matching == 0:
        enrolled_with = sorted({f"{item.get('engine')}/{item.get('engine_version') or ''}" for item in items})
        result.update(engine_mismatch=True, enrolled_engines=enrolled_with)
        logger.warning(
            "facial_embedding_sync.no_enrollments_for_active_engine active=%s enrolled_with=%s -- "
            "known people cannot be recognized until they are enrolled with the active engine",
            result["active_engine"], ",".join(enrolled_with),
        )
    return result


async def facial_embedding_sync_worker() -> None:
    import asyncio

    if RUNTIME_ROLE not in {"edge", "combined"} or not FACIAL_EMBEDDING_SYNC_ENABLED:
        facial_embedding_sync_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    facial_embedding_sync_state["worker_status"] = "running"
    logger.info("facial_embedding_sync.worker_started")
    while True:
        try:
            summary = await asyncio.to_thread(sync_facial_directory)
            with _state_lock:
                facial_embedding_sync_state["last_sync_at"] = datetime.now().isoformat()
                facial_embedding_sync_state["last_summary"] = summary
                facial_embedding_sync_state["last_error"] = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            with _state_lock:
                facial_embedding_sync_state["last_error"] = str(error)
            logger.warning("facial_embedding_sync.worker_iteration_failed error=%s", error)
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
