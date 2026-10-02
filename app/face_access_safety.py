"""Fail-closed policy and durable replay state for automatic Face Access.

Manual Unlock, Backup Access, AAC Visitor Call, and AACO do not use this
module. It protects only the automatic facial-recognition physical path.
"""
from __future__ import annotations

import math
import os
import time

from database_backend import connect

APPROVED_ACCESS_ENGINE = "onnx_yunet_arcface"
APPROVED_ACCESS_ENGINE_VERSION = "1"


def physical_face_access_gate_open() -> bool:
    """Require production mode, the existing FR rule-evaluation switch,
    and a separate explicit physical Face Access opt-in. Defaults deny."""
    return (
        os.environ.get("ANYAICAM_ENV", "development").strip().lower() == "production"
        and os.environ.get("ANYAICAM_RUNTIME_ROLE", "").strip().lower() in {"edge", "combined"}
        and os.environ.get("ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED", "false").strip().lower() == "true"
        and os.environ.get("ANYAICAM_FACIAL_ACCESS_PRODUCTION_ENABLED", "false").strip().lower() == "true"
    )


def observation_is_access_approved(context: dict | None) -> bool:
    """Check positive engine/version/capability provenance and a usable
    quality value. No similarity/confidence threshold is introduced here."""
    if not isinstance(context, dict):
        return False
    try:
        quality = float(context["quality"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (
        context.get("engine") == APPROVED_ACCESS_ENGINE
        and context.get("engine_version") == APPROVED_ACCESS_ENGINE_VERSION
        and context.get("active_engine") == APPROVED_ACCESS_ENGINE
        and context.get("active_engine_version") == APPROVED_ACCESS_ENGINE_VERSION
        and context.get("engine_available") is True
        and math.isfinite(quality)
        and quality > 0.0
    )


def note_person_presence(*, customer_id: str, camera_id: str, person_id: str, now_epoch: float | None = None) -> float | None:
    """Persist each known-person observation and return the gap from the
    previous observation. Used to require a detector absence/re-arm before
    another automatic dispatch, including across process restarts."""
    now = float(time.time() if now_epoch is None else now_epoch)
    with connect() as db:
        row = db.execute(
            "SELECT last_seen_at FROM facial_access_presence WHERE customer_id=? AND camera_id=? AND person_id=?",
            (customer_id, camera_id, person_id),
        ).fetchone()
        previous = float(row["last_seen_at"]) if row is not None else None
        db.execute(
            "INSERT INTO facial_access_presence(customer_id,camera_id,person_id,last_seen_at) VALUES(?,?,?,?) "
            "ON CONFLICT(customer_id,camera_id,person_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (customer_id, camera_id, person_id, now),
        )
    return None if previous is None else now - previous


def reserve_physical_dispatch(context: dict, *, cooldown_seconds: float, rearm_seconds: float,
                              now_epoch: float | None = None) -> tuple[bool, str | None]:
    """Atomically consume a facial event and reserve the person/rule
    cooldown before a hardware call. Reservations and unique event IDs are
    durable, so replay after a restart cannot pulse a second time. A failed
    hardware attempt remains consumed (safe at-most-once behavior)."""
    required = ("customer_id", "camera_id", "rule_id", "person_id", "facial_event_id")
    if not isinstance(context, dict) or any(not str(context.get(key) or "").strip() for key in required):
        return False, "missing_face_access_context"
    now = float(time.time() if now_epoch is None else now_epoch)
    cooldown = max(0.0, float(cooldown_seconds))
    rearm = max(cooldown, max(0.0, float(rearm_seconds)))
    gap_raw = context.get("presence_gap_seconds")
    try:
        gap = None if gap_raw is None else float(gap_raw)
    except (TypeError, ValueError, OverflowError):
        gap = None

    with connect() as db:
        # The unique event insert is the idempotency claim. Do it first in
        # the transaction so concurrent dispatches serialize on the DB.
        try:
            claimed = db.execute(
                "INSERT OR IGNORE INTO facial_access_dispatch_events(facial_event_id,customer_id,camera_id,rule_id,person_id,attempted_at,result) "
                "VALUES(?,?,?,?,?,?,?)",
                (context["facial_event_id"], context["customer_id"], context["camera_id"], context["rule_id"],
                 context["person_id"], now, "reserved"),
            ).rowcount
        except Exception:
            return False, "dispatch_state_unavailable"
        if claimed != 1:
            return False, "duplicate_facial_event"

        key = (context["customer_id"], context["camera_id"], context["rule_id"], context["person_id"])
        row = db.execute(
            "SELECT reserved_at,facial_event_id FROM facial_access_dispatches "
            "WHERE customer_id=? AND camera_id=? AND rule_id=? AND person_id=?",
            key,
        ).fetchone()
        if row is not None:
            previous = float(row["reserved_at"])
            if now < previous:
                result = "clock_moved_backwards"
            elif now - previous < cooldown:
                result = "cooldown"
            elif gap is None or gap < rearm:
                result = "person_still_present"
            else:
                result = None
        else:
            result = None

        if result is None:
            if row is None:
                try:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO facial_access_dispatches(customer_id,camera_id,rule_id,person_id,facial_event_id,reserved_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (*key, context["facial_event_id"], now),
                    ).rowcount
                    if inserted != 1:
                        result = "concurrent_dispatch"
                except Exception:
                    result = "concurrent_dispatch"
            else:
                changed = db.execute(
                    "UPDATE facial_access_dispatches SET facial_event_id=?,reserved_at=? "
                    "WHERE customer_id=? AND camera_id=? AND rule_id=? AND person_id=? AND reserved_at=? AND facial_event_id=?",
                    (context["facial_event_id"], now, *key, row["reserved_at"], row["facial_event_id"]),
                ).rowcount
                if changed != 1:
                    result = "concurrent_dispatch"

        db.execute(
            "UPDATE facial_access_dispatch_events SET result=? WHERE facial_event_id=?",
            ("reserved" if result is None else result, context["facial_event_id"]),
        )
        return result is None, result
