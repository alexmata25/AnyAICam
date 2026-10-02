"""Automatic physical Face Access: every condition, in one place (2026-10-02,
Codex Face Recognition / Face Access audit; owner policies approved
2026-10-02).

Recognition and Face Access authorization are separate. A recognized face
always produces its FR event; it can open a door automatically only when
EVERY check below passes, at the boundary to the physical access-control
service (door_access.CameraDoorProvider). Each refusal is audited and never
stops event generation.

1. Production gate (Codex blocker 1 and follow-up). Automatic facial unlock
   of a real access-control door needs ANYAICAM_FACE_ACCESS_PHYSICAL_UNLOCK_
   ENABLED=true AND the general FR / Face Access flags AND ANYAICAM_ENV=
   production AND an explicitly set appliance ANYAICAM_RUNTIME_ROLE (edge or
   combined) -- see physical_unlock_denial(). Default off: development, test,
   staging, cloud and mock evaluation can never reach a physical adapter.
2. Access-approved engine (blocker 2, owner policy "no Haar physical
   unlocking"). The match must come from the approved production engine
   (ArcFace via ONNX: ACCESS_APPROVED_ENGINES), proven by the observation's
   own engine/version AND the engine instance that produced it. Haar, a
   requested engine that failed and fell back, or missing/mismatched
   provenance all fail closed. Thresholds are unchanged.
3. Observation validity (blocker 2). The approved engine already applies
   its own detection-score threshold; here the observation must be well
   formed -- a finite quality in (0, 1], a real face box, a finite
   non-empty embedding. No new numeric quality minimum is introduced.
4. Cloud-origin grant freshness (blocker 4, owner policy). A facial_rules
   row with origin='cloud' authorizes only while the last successful
   authoritative directory sync is at most CLOUD_GRANT_MAX_AGE_SECONDS
   (15 minutes) old. The sync time is stored durably with the snapshot
   (face_access_grant_sync), so a restart with an old snapshot on disk
   stays denied until a new sync succeeds. Local-origin rules are not
   cloud grants and are unaffected.
5. Replay and re-arm (blocker 3), durable in the database:
   - one physical attempt per facial event id, ever
     (face_access_dispatches, primary key) -- a replayed/duplicate event
     never pulses again, across restarts;
   - after an accepted unlock, the same person re-arms at that door only
     when the rule's cooldown has passed AND they have been out of view
     for at least the re-arm gap since that unlock
     (face_access_presence). The gap is the rule's cooldown, never less
     than the existing FR re-report window (FACIAL_DEBOUNCE_SECONDS), so
     someone still standing at the door after it relocks does not unlock
     it again, while a genuinely new arrival does.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta

PHYSICAL_UNLOCK_ENV = "ANYAICAM_FACE_ACCESS_PHYSICAL_UNLOCK_ENABLED"
CLOUD_GRANT_MAX_AGE_SECONDS = 15 * 60
# (engine name, engine version) pairs approved to authorize a physical door.
ACCESS_APPROVED_ENGINES = frozenset({("onnx_yunet_arcface", "1")})

DDL = (
    "CREATE TABLE IF NOT EXISTS face_access_grant_sync(customer_id TEXT PRIMARY KEY,synced_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS face_access_dispatches(facial_event_id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,"
    "camera_id TEXT NOT NULL,person_id TEXT,rule_id TEXT,result TEXT NOT NULL,reason TEXT,created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS face_access_presence(camera_id TEXT NOT NULL,person_id TEXT NOT NULL,customer_id TEXT NOT NULL,"
    "last_seen_at TEXT NOT NULL,last_unlock_at TEXT,max_gap_since_unlock REAL NOT NULL DEFAULT 0,PRIMARY KEY(camera_id,person_id))",
    "CREATE TABLE IF NOT EXISTS face_access_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,facial_event_id TEXT,customer_id TEXT,"
    "camera_id TEXT,person_id TEXT,rule_id TEXT,result TEXT NOT NULL,reason TEXT,created_at TEXT NOT NULL)",
)


def ensure_tables(db) -> None:
    for statement in DDL:
        db.execute(statement)


def as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    return datetime.now()


# ------------------------------------------------------------------ 1. gate

# Runtime roles that ARE the appliance next to the door (the repository's
# ANYAICAM_RUNTIME_ROLE convention, as in appliance_activation.py).
APPROVED_RUNTIME_ROLES = frozenset({"edge", "combined"})


def physical_unlock_denial() -> str | None:
    """None only when this process may physically unlock a door
    automatically (2026-10-02, Codex follow-up). ALL of:
    - the dedicated ANYAICAM_FACE_ACCESS_PHYSICAL_UNLOCK_ENABLED=true;
    - the general Face Access enablement (FR on and
      ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED on);
    - ANYAICAM_ENV=production;
    - ANYAICAM_RUNTIME_ROLE explicitly set to an approved appliance role
      (edge/combined). The setting's own default ('edge' when unset) is NOT
      accepted here: a missing or unknown role fails closed, as does cloud.
    Read live on every attempt; never cached."""
    if os.environ.get(PHYSICAL_UNLOCK_ENV, "").strip().lower() != "true":
        return "face_access_physical_disabled"
    import facial_recognition
    import relay_control
    if not facial_recognition.FACIAL_RECOGNITION_ENABLED or not relay_control.FACIAL_ACCESS_CONTROL_ENABLED:
        return "face_access_not_enabled"
    if os.environ.get("ANYAICAM_ENV", "").strip().lower() != "production":
        return "face_access_not_production"
    if os.environ.get("ANYAICAM_RUNTIME_ROLE", "").strip().lower() not in APPROVED_RUNTIME_ROLES:
        return "face_access_runtime_role_not_approved"
    return None


def physical_unlock_enabled() -> bool:
    return physical_unlock_denial() is None


# ------------------------------------------------------------------ 2-3. engine and observation

def engine_denial(observation, engine) -> str | None:
    """None only with positive evidence the access-approved engine made this
    observation."""
    if observation is None or engine is None:
        return "engine_provenance_missing"
    claimed = (str(getattr(observation, "engine", "") or ""), str(getattr(observation, "engine_version", "") or ""))
    if claimed not in ACCESS_APPROVED_ENGINES:
        return "engine_not_access_approved"
    try:
        import facial_engine_onnx
        approved_class = facial_engine_onnx.ArcFaceOnnxEngine
    except Exception:
        return "engine_unavailable"
    if not isinstance(engine, approved_class):
        return "engine_provenance_mismatch"
    if (str(getattr(engine, "name", "")), str(getattr(engine, "version", ""))) != claimed:
        return "engine_provenance_mismatch"
    try:
        if not engine.capability().get("available"):
            return "engine_unavailable"
    except Exception:
        return "engine_unavailable"
    return None


def quality_denial(observation) -> str | None:
    if observation is None:
        return "observation_missing"
    quality = getattr(observation, "quality", None)
    if not isinstance(quality, (int, float)) or isinstance(quality, bool) or not math.isfinite(quality) or not 0 < quality <= 1:
        return "observation_quality_invalid"
    box = getattr(observation, "bbox", None)
    if box is None or getattr(box, "width", 0) <= 0 or getattr(box, "height", 0) <= 0:
        return "observation_quality_invalid"
    embedding = getattr(observation, "embedding", None)
    try:
        if not embedding or not all(math.isfinite(float(value)) for value in embedding):
            return "observation_quality_invalid"
    except (TypeError, ValueError):
        return "observation_quality_invalid"
    return None


# ------------------------------------------------------------------ 4. cloud grant freshness

def mark_grants_synced(db, *, customer_id: str, now=None) -> None:
    """Called in the same transaction that applies an authoritative
    directory snapshot (or verifies it unchanged)."""
    ensure_tables(db)
    stamp = as_datetime(now).isoformat() if now is not None else datetime.now().isoformat()
    db.execute("INSERT INTO face_access_grant_sync(customer_id,synced_at) VALUES(?,?) "
               "ON CONFLICT(customer_id) DO UPDATE SET synced_at=excluded.synced_at", (customer_id, stamp))


def cloud_grant_denial(db, *, customer_id: str, origin: str | None, now) -> str | None:
    if (origin or "local") != "cloud":
        return None
    ensure_tables(db)
    found = db.execute("SELECT synced_at FROM face_access_grant_sync WHERE customer_id=?", (customer_id,)).fetchone()
    if not found:
        return "cloud_grant_never_synced"
    age = (as_datetime(now) - as_datetime(found["synced_at"])).total_seconds()
    if age > CLOUD_GRANT_MAX_AGE_SECONDS or age < -60:  # a clock far behind the stamp is not "fresh" either
        return "cloud_grant_stale"
    return None


# ------------------------------------------------------------------ 5. replay and re-arm

def rearm_gap_seconds(cooldown_seconds) -> float:
    import facial_recognition
    return max(float(cooldown_seconds or 0), float(facial_recognition.FACIAL_DEBOUNCE_SECONDS))


def note_presence(db, *, customer_id: str, camera_id: str, person_id: str, now) -> None:
    """Every accepted sighting of a known person at a door camera (before FR
    event debouncing), so continuous presence is visible across relock."""
    ensure_tables(db)
    stamp = as_datetime(now)
    found = db.execute("SELECT last_seen_at,last_unlock_at,max_gap_since_unlock FROM face_access_presence WHERE camera_id=? AND person_id=?",
                       (camera_id, person_id)).fetchone()
    if not found:
        db.execute("INSERT INTO face_access_presence(camera_id,person_id,customer_id,last_seen_at) VALUES(?,?,?,?)",
                   (camera_id, person_id, customer_id, stamp.isoformat()))
        return
    gap = max(0.0, (stamp - as_datetime(found["last_seen_at"])).total_seconds())
    max_gap = max(float(found["max_gap_since_unlock"] or 0), gap) if found["last_unlock_at"] else 0.0
    db.execute("UPDATE face_access_presence SET last_seen_at=?,max_gap_since_unlock=? WHERE camera_id=? AND person_id=?",
               (max(stamp, as_datetime(found["last_seen_at"])).isoformat(), max_gap, camera_id, person_id))


def rearm_denial(db, *, camera_id: str, person_id: str | None, cooldown_seconds, now) -> str | None:
    if not person_id:
        return None
    ensure_tables(db)
    found = db.execute("SELECT last_unlock_at,max_gap_since_unlock FROM face_access_presence WHERE camera_id=? AND person_id=?",
                       (camera_id, person_id)).fetchone()
    if not found or not found["last_unlock_at"]:
        return None
    since = (as_datetime(now) - as_datetime(found["last_unlock_at"])).total_seconds()
    if since < float(cooldown_seconds or 0):
        return "cooldown"
    if float(found["max_gap_since_unlock"] or 0) < rearm_gap_seconds(cooldown_seconds):
        return "not_rearmed_still_present"
    return None


def already_dispatched(db, *, facial_event_id: str) -> bool:
    ensure_tables(db)
    return db.execute("SELECT 1 FROM face_access_dispatches WHERE facial_event_id=?", (facial_event_id,)).fetchone() is not None


def claim_dispatch(db, *, facial_event_id: str, customer_id: str, camera_id: str, person_id: str | None,
                   rule_id: str | None, now) -> bool:
    """True for the first physical attempt for this facial event; False for
    any replay, forever (durable primary key)."""
    ensure_tables(db)
    return bool(db.execute(
        "INSERT INTO face_access_dispatches(facial_event_id,customer_id,camera_id,person_id,rule_id,result,created_at) "
        "VALUES(?,?,?,?,?,'claimed',?) ON CONFLICT(facial_event_id) DO NOTHING",
        (facial_event_id, customer_id, camera_id, person_id, rule_id, as_datetime(now).isoformat())).rowcount)


def finish_dispatch(db, *, facial_event_id: str, camera_id: str, person_id: str | None, result: str, reason: str | None, now) -> None:
    db.execute("UPDATE face_access_dispatches SET result=?,reason=? WHERE facial_event_id=?", (result, reason, facial_event_id))
    if result == "unlocked" and person_id:
        db.execute("UPDATE face_access_presence SET last_unlock_at=?,max_gap_since_unlock=0 WHERE camera_id=? AND person_id=?",
                   (as_datetime(now).isoformat(), camera_id, person_id))


def record_attempt(db, *, facial_event_id: str | None, customer_id: str | None, camera_id: str | None, person_id: str | None,
                   rule_id: str | None, result: str, reason: str | None, now) -> None:
    """Accepted and refused automatic attempts alike."""
    ensure_tables(db)
    db.execute("INSERT INTO face_access_attempts(facial_event_id,customer_id,camera_id,person_id,rule_id,result,reason,created_at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (facial_event_id, customer_id, camera_id, person_id, rule_id, result, reason, as_datetime(now).isoformat()))
