"""AAC match-event pipeline -- Phase 1.

This is the module main.py's save_yolo_events() calls (see that
function's existing "person" branch, right alongside its ppe.py/lpr.py
hooks) for each detected person crop. It ties together:

  facial_recognition.py   (face detection + embedding + matching math)
  facial_people.py        (enrolled embeddings + watchlist membership)
  relay_control.py        (access-rule evaluation, mock-only in Phase 1)

...and writes real rows into this deployment's own database -- the
existing detection_events/detection_event_media event system
(event_type='facial_recognition'), plus this module's own facial_events
detail table -- via the same database_backend.connect() every other
part of this codebase uses.

Deliberately in-process, not the local-JSON-file + analytics_sync.py
HTTP-forwarding path ppe.py/lpr.py's OWN events use: those exist to
bridge a physically separate edge appliance (local SQLite) to a
separate central cloud deployment (Postgres) that customers actually
browse. AAC event creation instead writes straight into whichever
database this running process is already configured against (edge,
combined, or cloud -- see database_backend.py), which is correct and
complete for a single-database deployment (the current Ryzen/Samsung
on-prem production shape) but does NOT forward through that same
appliance -> cloud HTTP bridge for a split edge/cloud topology. See the
Phase 1 report's "remaining work" for what forwarding AAC events
through analytics_sync.py would take.

Tenant isolation is enforced the same way facial_people.py enforces it:
every query here is scoped by a customer_id resolved from the camera's
OWN row (never trusted from a caller-supplied parameter that didn't
come from that lookup), and the in-memory embedding cache below is
keyed by (customer_id, engine) so one customer's cached enrolled
embeddings are never consulted while matching a different customer's
camera.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

import door_access
import facial_people
import facial_recognition
import relay_control
from database_backend import backend

AAC_THUMBNAIL_FOLDER = Path(os.environ.get("ANYAICAM_AAC_THUMBNAIL_FOLDER", "/app/recordings/aac_faces/events"))

ANALYTIC_KEY = "facial_recognition"

# Cached enrolled-embedding sets are refreshed at most this often per
# (customer_id, engine) -- avoids a full facial_embeddings scan on
# every single detected face while still picking up a newly-enrolled
# person within a bounded, short window. Mirrors analytics_sync.py's
# own CONFIG_REFRESH_SECONDS-gated cache-refresh pattern.
EMBEDDING_CACHE_TTL_SECONDS = max(1.0, float(os.environ.get("ANYAICAM_FACIAL_EMBEDDING_CACHE_TTL_SECONDS", "15")))

# Quantization grid (pixels) used to bucket unknown-face debounce keys
# by coarse position -- see record_facial_events()'s own comment on why
# unknown sightings can't all share one single debounce key.
_UNKNOWN_POSITION_GRID_PX = max(1, int(os.environ.get("ANYAICAM_FACIAL_UNKNOWN_DEBOUNCE_GRID_PX", "80")))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def _hhmm(now) -> str | None:
    """HH:MM for the schedule check in evaluate_access_rules() -- `now`
    may be a real datetime OR a plain ISO string (this module's own
    test suite convention, matching create_match_event()'s existing
    `now.isoformat() if hasattr(now, "isoformat") else str(now)`
    pattern). None (never schedule-restrict) if neither form parses,
    rather than raising and losing a real detection over a malformed
    timestamp."""
    if hasattr(now, "strftime"):
        return now.strftime("%H:%M")
    try:
        from datetime import datetime as _datetime
        return _datetime.fromisoformat(str(now)).strftime("%H:%M")
    except ValueError:
        return None


class _EmbeddingCache:
    """Thread-safe, TTL-refreshed cache of one customer's enrolled
    embeddings + watchlisted person ids. A real appliance may process
    frames from several cameras (several threads) concurrently."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, str], tuple[float, list, frozenset]] = {}

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, db, *, customer_id: str, engine: str, engine_version: str) -> tuple[list, frozenset]:
        # Keyed by (customer_id, engine, engine_version): a cached set
        # loaded under one embedding-format version must never be
        # handed back once the running engine's version changes (e.g.
        # a deploy that upgrades embed_face_crop()) -- see
        # facial_recognition.match_face()'s own engine_version guard for
        # why comparing across versions is a correctness issue, not
        # just a cache-staleness one.
        key = (customer_id, engine, engine_version)
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and (now - cached[0]) < EMBEDDING_CACHE_TTL_SECONDS:
                return cached[1], cached[2]
        embeddings = facial_people.enrolled_embeddings_for_matching(
            db, customer_id=customer_id, engine=engine, engine_version=engine_version
        )
        watchlisted = facial_people.watchlisted_person_ids(db, customer_id=customer_id)
        with self._lock:
            self._entries[key] = (now, embeddings, watchlisted)
        return embeddings, watchlisted


_embedding_cache = _EmbeddingCache()
_debounce = facial_recognition.DuplicateSuppressor()


def reset_state() -> None:
    """Test-only: clears the module-level embedding cache and debounce
    tracker, matching every other analytics module's own reset_state()."""
    _embedding_cache.reset()
    _debounce.reset()


def _camera_tenant_context(db, camera_number: int) -> dict | None:
    row = db.execute(
        "SELECT id,customer_id,site_id,appliance_id,name,door_access_enabled,door_relay_channel,door_relay_pulse_ms "
        "FROM cameras WHERE camera_number=?",
        (camera_number,),
    ).fetchone()
    return dict(row) if row else None


def _is_entitled(db, *, camera_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=? AND status='active'",
        (camera_id, ANALYTIC_KEY),
    ).fetchone()
    return row is not None


def _save_face_thumbnail(face_crop_bgr, *, event_id: str) -> str | None:
    try:
        import cv2
    except ImportError:
        return None
    if face_crop_bgr is None or getattr(face_crop_bgr, "size", 0) == 0:
        return None
    AAC_THUMBNAIL_FOLDER.mkdir(parents=True, exist_ok=True)
    path = AAC_THUMBNAIL_FOLDER / f"{event_id}.jpg"
    try:
        cv2.imwrite(str(path), face_crop_bgr)
    except Exception:
        return None
    return str(path)


def create_match_event(
    db,
    *,
    customer_id: str,
    site_id: str | None,
    camera_id: str,
    appliance_id: str | None,
    match_state: str,
    matched_person: dict | None,
    matched_watchlist: dict | None,
    confidence: float,
    engine: str,
    engine_version: str,
    face_bbox: dict | None,
    face_thumbnail_path: str | None,
    now,
) -> dict:
    """Writes one detection_events row (event_type='facial_recognition',
    the same generic table every other analytic writes into) plus this
    module's own facial_events detail row, in one call. `now` is a
    datetime -- callers pass the same `now` save_yolo_events() already
    computed for the surrounding scan, so a facial match's timestamp is
    never independently skewed from the frame it came from."""
    detection_event_id = _new_id("devt")
    event_id = _new_id("fevt")
    timestamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
    db.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            detection_event_id,
            customer_id,
            site_id,
            appliance_id,
            camera_id,
            detection_event_id,
            "facial_recognition",
            round(float(confidence), 4),
            1,
            json.dumps({"match_state": match_state, "matched_person_name": (matched_person or {}).get("display_name")}),
            timestamp,
            timestamp,
        ),
    )
    db.execute(
        "INSERT INTO facial_events(id,detection_event_id,customer_id,site_id,camera_id,match_state,matched_person_id,matched_person_name,matched_watchlist_id,matched_watchlist_name,confidence,engine,engine_version,face_bbox_json,face_thumbnail_path,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            detection_event_id,
            customer_id,
            site_id,
            camera_id,
            match_state,
            (matched_person or {}).get("id"),
            (matched_person or {}).get("display_name"),
            (matched_watchlist or {}).get("id"),
            (matched_watchlist or {}).get("name"),
            round(float(confidence), 4),
            engine,
            engine_version,
            json.dumps(face_bbox) if face_bbox else None,
            face_thumbnail_path,
            timestamp,
        ),
    )
    return {
        "id": event_id,
        "detection_event_id": detection_event_id,
        "customer_id": customer_id,
        "site_id": site_id,
        "camera_id": camera_id,
        "match_state": match_state,
        "matched_person_id": (matched_person or {}).get("id"),
        "matched_person_name": (matched_person or {}).get("display_name"),
        "matched_watchlist_id": (matched_watchlist or {}).get("id"),
        "matched_watchlist_name": (matched_watchlist or {}).get("name"),
        "confidence": round(float(confidence), 4),
        "engine": engine,
        "engine_version": engine_version,
        "created_at": timestamp,
    }


def evaluate_access_rules(
    db,
    *,
    customer_id: str,
    camera_id: str,
    match_state: str,
    confidence: float,
    matched_person_id: str | None,
    matched_watchlist_id: str | None,
    relay_provider: "relay_control.RelayProvider",
    detection_event_id: str,
    current_time: str | None = None,
) -> list[dict]:
    """Loads this customer's enabled facial_rules (scoped to this
    camera or camera-agnostic, i.e. camera_id IS NULL), evaluates each
    against the match via relay_control.rule_applies() (including the
    rule's own schedule_start/schedule_end when current_time is given
    -- the Face Access "current permitted time/schedule" requirement),
    and triggers relay_provider for every rule that applies. Returns
    one summary dict per rule that applied (whether or not the
    provider actually activated -- e.g. a rule under cooldown still
    appears, with activated=False). An empty facial_rules table (the
    default -- see the Phase 1 report) means this always returns []."""
    rows = db.execute(
        "SELECT * FROM facial_rules WHERE customer_id=? AND enabled=1 AND (camera_id=? OR camera_id IS NULL)",
        (customer_id, camera_id),
    ).fetchall()
    outcomes = []
    for row in rows:
        rule = relay_control.RelayRule(
            id=row["id"],
            trigger_type=row["trigger_type"],
            channel=row["relay_channel"],
            pulse_ms=row["pulse_ms"],
            cooldown_seconds=row["cooldown_seconds"],
            dry_run=bool(row["dry_run"]),
            enabled=bool(row["enabled"]),
            min_confidence=row["min_confidence"],
            watchlist_id=row["watchlist_id"],
            person_id=row["person_id"],
            schedule_start=row["schedule_start"] if "schedule_start" in row.keys() else None,
            schedule_end=row["schedule_end"] if "schedule_end" in row.keys() else None,
        )
        if not relay_control.rule_applies(
            rule,
            match_state=match_state,
            confidence=confidence,
            matched_person_id=matched_person_id,
            matched_watchlist_id=matched_watchlist_id,
            current_time=current_time,
        ):
            continue
        request = relay_control.build_request(rule, reason=f"facial_event:{detection_event_id}")
        result = relay_provider.trigger(request)
        outcomes.append(
            {
                "rule_id": rule.id,
                "channel": result.channel,
                "activated": result.activated,
                "dry_run": result.dry_run,
                "suppressed_reason": result.suppressed_reason,
            }
        )
    return outcomes


def record_facial_events(
    db,
    *,
    camera_number: int,
    person_crop_bgr,
    now,
    relay_provider: "relay_control.RelayProvider | None" = None,
    engine: "facial_recognition.FaceEngine | None" = None,
) -> list[dict]:
    """The one entry point main.py's save_yolo_events() calls. Returns
    the list of created event summary dicts (possibly empty). Never
    raises -- every failure mode (AAC disabled, camera not entitled,
    camera unmapped, no face found, engine unavailable) returns []
    uniformly, matching ppe.detect_ppe()/lpr.recognize_plate()'s own

    `engine` is injectable purely for tests (see
    test_facial_events.py), exactly like facial_recognition.detect_and_
    embed()'s own `engine` parameter -- production code never passes
    this explicitly, and always exercises the real, process-wide
    facial_recognition.get_engine() singleton instead.
    exception-safety contract; the caller wraps this in its own
    try/except regardless, matching those two hooks' own established
    pattern in save_yolo_events()."""
    if not facial_recognition.FACIAL_RECOGNITION_ENABLED:
        return []
    if not facial_recognition.is_camera_enabled(camera_number):
        return []
    context = _camera_tenant_context(db, camera_number)
    if context is None:
        return []
    if not _is_entitled(db, camera_id=context["id"]):
        return []
    settings = facial_people.get_settings(db, customer_id=context["customer_id"])
    observations = facial_recognition.detect_and_embed(person_crop_bgr, engine=engine)
    if not observations:
        return []
    engine = observations[0].engine
    engine_version = observations[0].engine_version
    enrolled, watchlisted = _embedding_cache.get(
        db, customer_id=context["customer_id"], engine=engine, engine_version=engine_version
    )
    created: list[dict] = []
    for observation in observations:
        candidate = facial_recognition.match_face(
            observation.embedding, enrolled, engine=engine, engine_version=observation.engine_version
        )
        match_state, accepted = facial_recognition.classify_match(
            candidate, threshold=settings["min_confidence"], watchlist_person_ids=watchlisted
        )
        if match_state == "unknown" and not settings["unknown_person_events_enabled"]:
            continue
        if accepted is not None:
            # A known/watchlist match is debounced by WHO it is -- the
            # correct behavior is suppressing the same enrolled person
            # being re-reported every frame while they linger in view.
            debounce_key = (context["id"], accepted.person_id)
        else:
            # An unknown face has no identity to key on. Bucketing every
            # unknown sighting on this camera into one single "unknown"
            # key would wrongly suppress a SECOND, different stranger
            # who happens to appear in the same scan (see
            # test_multiple_faces_in_one_frame_each_produce_an_event) --
            # so the bucket also includes a coarse, quantized face
            # position. This still suppresses the same stationary
            # unknown face being re-reported frame after frame (the
            # actual goal of debouncing), while two simultaneously
            # visible strangers at different positions get their own
            # buckets. It is a positional heuristic, not real
            # cross-frame re-identification -- a genuinely new
            # unknown face that happens to land in the same grid cell
            # shortly after a previous one moved away can still be
            # suppressed; that trade-off is intentional for Phase 1.
            grid = _UNKNOWN_POSITION_GRID_PX
            debounce_key = (context["id"], "unknown", observation.bbox.x // grid, observation.bbox.y // grid)
        if not _debounce.check_and_record(debounce_key):
            continue
        matched_person = None
        matched_watchlist = None
        if accepted is not None:
            matched_person = facial_people.get_person(db, customer_id=context["customer_id"], person_id=accepted.person_id)
            if match_state == "watchlist":
                memberships = facial_people.person_watchlist_memberships(
                    db, customer_id=context["customer_id"], person_id=accepted.person_id
                )
                matched_watchlist = memberships[0] if memberships else None
        confidence = accepted.similarity if accepted is not None else (candidate.similarity if candidate else 0.0)
        try:
            crop = person_crop_bgr[
                observation.bbox.y : observation.bbox.y + observation.bbox.height,
                observation.bbox.x : observation.bbox.x + observation.bbox.width,
            ]
        except Exception:
            crop = None
        thumbnail_path = _save_face_thumbnail(crop, event_id=_new_id("thumb"))
        event = create_match_event(
            db,
            customer_id=context["customer_id"],
            site_id=context.get("site_id"),
            camera_id=context["id"],
            appliance_id=context.get("appliance_id"),
            match_state=match_state,
            matched_person=matched_person,
            matched_watchlist=matched_watchlist,
            confidence=confidence,
            engine=engine,
            engine_version=observation.engine_version,
            face_bbox={
                "x": observation.bbox.x,
                "y": observation.bbox.y,
                "width": observation.bbox.width,
                "height": observation.bbox.height,
            },
            face_thumbnail_path=thumbnail_path,
            now=now,
        )
        created.append(event)
        # Face Access -- Door/Relay Control (2026-09-17): everything
        # below is scoped to door_access_enabled cameras only -- a
        # facial-recognition camera that isn't a configured door has no
        # relay to evaluate against and nothing to notify about, per
        # the requirements doc's own framing (modes 1-3 are explicitly
        # about door cameras). A non-door camera's match is still fully
        # recorded above (create_match_event()); it just never reaches
        # this block.
        person_name = (matched_person or {}).get("display_name")
        door_enabled = bool(context.get("door_access_enabled"))
        if door_enabled and match_state in ("known", "watchlist"):
            outcomes = []
            if relay_provider is not None:
                outcomes = evaluate_access_rules(
                    db,
                    customer_id=context["customer_id"],
                    camera_id=context["id"],
                    match_state=match_state,
                    confidence=confidence,
                    matched_person_id=accepted.person_id if accepted else None,
                    matched_watchlist_id=(matched_watchlist or {}).get("id"),
                    relay_provider=relay_provider,
                    detection_event_id=event["detection_event_id"],
                    current_time=_hhmm(now),
                )
                event["relay_outcomes"] = outcomes
                # Persisted separately from the INSERT above (the
                # authorization decision only runs, if at all, after
                # that row already exists) so "recognized identity" and
                # "access granted/denied" are both durable on the same
                # facial_events row, not just returned in-memory and
                # lost the moment this function returns.
                db.execute(
                    "UPDATE facial_events SET access_outcomes_json=? WHERE id=?",
                    (json.dumps(outcomes), event["id"]),
                )
            activated = any(outcome["activated"] for outcome in outcomes)
            if activated:
                # Mode 1: recognized + authorized for automatic entry.
                # The relay already fired inside evaluate_access_rules()
                # above -- this only records the audit row; no
                # customer notification is sent (nothing went wrong,
                # nothing needs a manual decision).
                door_access.record_door_access_event(
                    db, customer_id=context["customer_id"], camera_id=context["id"], door_name=context["name"],
                    relay_channel=context.get("door_relay_channel"), trigger_type="automatic",
                    matched_person_id=accepted.person_id if accepted else None, matched_person_name=person_name,
                    facial_event_id=event["id"], authorization_result="authorized", relay_result="activated",
                    success=True, now=now,
                )
            else:
                # Mode 2: recognized, but not authorized for automatic
                # entry at this door (no facial_rules row matched, or
                # one matched but never actually activates the relay --
                # e.g. dry_run, cooldown). Never auto-unlocks; the
                # notify_message below is what a future notification
                # dispatcher (analytics_sync.py -> appliance_cloud.py's
                # fanout, see that module's own facial_recognition
                # comment) turns into "<name> is at <door>."
                suppressed = outcomes[0].get("suppressed_reason") if outcomes else None
                relay_result = "suppressed" if suppressed else ("dry_run" if outcomes else "skipped")
                door_access.record_door_access_event(
                    db, customer_id=context["customer_id"], camera_id=context["id"], door_name=context["name"],
                    relay_channel=context.get("door_relay_channel"), trigger_type="automatic",
                    matched_person_id=accepted.person_id if accepted else None, matched_person_name=person_name,
                    facial_event_id=event["id"],
                    authorization_result="authorized" if outcomes else "not_authorized",
                    relay_result=relay_result, success=False, now=now,
                )
                event["door_notify_message"] = f"{person_name or 'A recognized person'} is at {context['name']}."
        elif door_enabled and match_state == "unknown":
            # Mode 3: unknown person at a door camera. Never auto-
            # unlocks; always notifies, regardless of unknown_person_
            # events_enabled having already gated whether this event
            # was even created (see the debounce check above -- an
            # unknown-events-disabled customer never reaches this line
            # at all, so there is nothing extra to suppress here).
            door_access.record_door_access_event(
                db, customer_id=context["customer_id"], camera_id=context["id"], door_name=context["name"],
                relay_channel=context.get("door_relay_channel"), trigger_type="automatic",
                matched_person_id=None, matched_person_name=None, facial_event_id=event["id"],
                authorization_result="unknown_person", relay_result="skipped", success=False, now=now,
            )
            event["door_notify_message"] = f"Unknown person at {context['name']}."
    return created


# --------------------------------------------------------------------------
# Query / history (Facial Events + Match detail screens)
# --------------------------------------------------------------------------


def list_events(
    db,
    *,
    customer_id: str,
    site_id: str | None = None,
    camera_id: str | None = None,
    person_id: str | None = None,
    match_state: str | None = None,
    watchlist_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Every filter is additive AND every result is unconditionally
    scoped to customer_id -- the WHERE clause always starts with
    fe.customer_id=?, and no filter parameter can widen a query beyond
    that tenant, no matter what a caller passes for camera_id/person_id/
    watchlist_id (an id belonging to a different customer simply
    matches nothing, same as facial_people.py's own tenant guarantee)."""
    query = (
        "SELECT fe.*,de.event_timestamp FROM facial_events fe "
        "JOIN detection_events de ON de.id=fe.detection_event_id "
        "WHERE fe.customer_id=?"
    )
    params: list = [customer_id]
    if site_id:
        query += " AND fe.site_id=?"
        params.append(site_id)
    if camera_id:
        query += " AND fe.camera_id=?"
        params.append(camera_id)
    if person_id:
        query += " AND fe.matched_person_id=?"
        params.append(person_id)
    if match_state:
        query += " AND fe.match_state=?"
        params.append(match_state)
    if watchlist_id:
        query += " AND fe.matched_watchlist_id=?"
        params.append(watchlist_id)
    if start:
        query += " AND de.event_timestamp>=?"
        params.append(start)
    if end:
        query += " AND de.event_timestamp<=?"
        params.append(end)
    query += " ORDER BY de.event_timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [dict(row) for row in db.execute(query, tuple(params)).fetchall()]


def get_event_detail(db, *, customer_id: str, event_id: str) -> dict | None:
    row = db.execute(
        "SELECT fe.*,de.event_timestamp,de.camera_id AS de_camera_id FROM facial_events fe "
        "JOIN detection_events de ON de.id=fe.detection_event_id "
        "WHERE fe.id=? AND fe.customer_id=?",
        (event_id, customer_id),
    ).fetchone()
    return dict(row) if row else None
