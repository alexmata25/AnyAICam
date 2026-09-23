"""AAC Voice Call data-access layer, Phase 1.

Every function here is tenant-scoped by customer_id, matching
facial_recognition_ui.py's own established discipline for a new
feature module: no query below ever reads a customer_id from anywhere
but an explicit, caller-supplied, already-authorized argument. Callers
(aac_voice_call.py) resolve that customer_id from partner_identity()
-- never current_user() (the legacy local-VMS auth) -- exactly the
auth-mismatch class this session's own review found and fixed twice
already (subscription-portal, mobile-devices) for other features; this
module is written correctly from the start rather than needing that
same fix later.

State machine (tracked in application code, not a DB CHECK constraint,
matching customer_talk_sessions.state's own precedent):

    triggered -> notified -> (answered | missed | dismissed) -> ended

`answered` is a separate boolean (not just state=='answered') because
a call can be answered and later end normally (state becomes 'ended'
only once call_ended_at is set) -- `answered` must keep recording that
the homeowner DID pick up, independent of how the call later finished.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from partner_db import audit, connection, row, rows


def is_entrance_camera(customer_id: str, camera_id: str) -> bool:
    """The one gate every trigger must pass: only a camera explicitly
    enrolled here (and still enabled) participates in AAC Voice Call.
    Never a hardcoded camera list, never inferred from camera count."""
    record = row(
        "SELECT enabled FROM aac_voice_call_entrance_cameras WHERE camera_id=? AND customer_id=?",
        (camera_id, customer_id),
    )
    return bool(record and record["enabled"])


def set_entrance_camera(*, customer_id: str, camera_id: str, enabled: bool, configured_by: str | None = None) -> None:
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "INSERT INTO aac_voice_call_entrance_cameras(camera_id,customer_id,enabled,configured_at,configured_by) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(camera_id) DO UPDATE SET enabled=excluded.enabled,configured_at=excluded.configured_at,configured_by=excluded.configured_by",
            (camera_id, customer_id, 1 if enabled else 0, now, configured_by),
        )


def list_entrance_cameras(customer_id: str) -> list[dict]:
    return rows(
        "SELECT camera_id,enabled,configured_at,configured_by FROM aac_voice_call_entrance_cameras WHERE customer_id=? ORDER BY configured_at",
        (customer_id,),
    )


def create_voice_call_event(
    *,
    customer_id: str,
    site_id: str,
    camera_id: str,
    trigger_detection_event_id: str | None = None,
    transcript_text: str | None = None,
    intent: str | None = None,
    intent_confidence: float | None = None,
    thumbnail_s3_key: str | None = None,
    event_timestamp: str | None = None,
    actor: dict | None = None,
) -> str:
    """Creates the event row in the 'triggered' state. Does not create a
    notification -- see mark_notified()/notification_engine.fanout_
    appliance_event(), a deliberate separate step so a caller can
    classify intent (possibly updating transcript/intent after this
    call via record_transcript_and_intent()) before deciding to notify."""
    event_id = uuid.uuid4().hex
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "INSERT INTO aac_voice_call_events("
            "id,customer_id,site_id,camera_id,state,trigger_detection_event_id,"
            "transcript_text,intent,intent_confidence,thumbnail_s3_key,"
            "event_timestamp,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, customer_id, site_id, camera_id, "triggered", trigger_detection_event_id,
                transcript_text, intent, intent_confidence, thumbnail_s3_key,
                event_timestamp or now, now, now,
            ),
        )
    audit(actor or {}, "aac_voice_call.triggered", "aac_voice_call_event", event_id, {"camera_id": camera_id, "intent": intent})
    return event_id


def record_transcript_and_intent(
    *, event_id: str, customer_id: str, transcript_text: str, intent: str, intent_confidence: float, actor: dict | None = None
) -> None:
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "UPDATE aac_voice_call_events SET transcript_text=?,intent=?,intent_confidence=?,updated_at=? "
            "WHERE id=? AND customer_id=?",
            (transcript_text, intent, intent_confidence, now, event_id, customer_id),
        )
    audit(actor or {}, "aac_voice_call.intent_classified", "aac_voice_call_event", event_id, {"intent": intent, "confidence": intent_confidence})


def mark_notified(*, event_id: str, customer_id: str, notification_id: str, actor: dict | None = None) -> None:
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "UPDATE aac_voice_call_events SET state='notified',notification_id=?,updated_at=? WHERE id=? AND customer_id=?",
            (notification_id, now, event_id, customer_id),
        )
    audit(actor or {}, "aac_voice_call.notified", "aac_voice_call_event", event_id, {"notification_id": notification_id})


def mark_answered(*, event_id: str, customer_id: str, answered_by_user_id: str, actor: dict | None = None) -> None:
    """2026-09-23 fix: the UPDATE below is now guarded to only fire from
    'triggered'/'notified' -- without this, answering an already-ended
    or already-dismissed call silently re-stamped answered_at/
    call_started_at to "now" and flipped state back to 'answered',
    resurrecting a call the homeowner (or the other party) had already
    closed out, and double-answering an already-answered call reset its
    timing fields with no record anything was amiss. Reached via
    independent review, no live incident. Matches mark_dismissed()'s
    own pre-existing state-guard precedent just below; silently no-ops
    (same as that function) rather than raising, since the caller
    (aac_voice_call.py) already 404s on a nonexistent/unowned event and
    has no other error path wired for "wrong state" yet -- a stale
    button press on an already-finished call is a normal, harmless race
    (e.g. two devices open on the same account), not a caller bug."""
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "UPDATE aac_voice_call_events SET state='answered',answered=1,answered_at=?,answered_by_user_id=?,call_started_at=?,updated_at=? "
            "WHERE id=? AND customer_id=? AND state IN ('triggered','notified')",
            (now, answered_by_user_id, now, now, event_id, customer_id),
        )
    audit(actor or {}, "aac_voice_call.answered", "aac_voice_call_event", event_id, {"answered_by_user_id": answered_by_user_id})


def mark_dismissed(*, event_id: str, customer_id: str, actor: dict | None = None) -> None:
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "UPDATE aac_voice_call_events SET state='dismissed',updated_at=? WHERE id=? AND customer_id=? AND state IN ('triggered','notified')",
            (now, event_id, customer_id),
        )
    audit(actor or {}, "aac_voice_call.dismissed", "aac_voice_call_event", event_id, {})


def end_call(*, event_id: str, customer_id: str, actor: dict | None = None) -> None:
    """2026-09-23 fix: guarded against re-ending an already-terminal
    call (same defect class as mark_answered() just above -- an
    unguarded UPDATE let a stale/duplicate "End call" press re-stamp
    call_ended_at on a call already ended or already dismissed).
    Deliberately NOT restricted to state='answered' only: the call
    screen's own "End call" button is always available alongside
    "Answer" (see aac_voice_call.py's voice_call_screen()), so ending
    a call the homeowner never answered -- "saw it was a delivery,
    closed the screen" -- is a real, intended flow, not a bug; only
    re-processing an already-dismissed/already-ended call is guarded
    against here."""
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "UPDATE aac_voice_call_events SET state='ended',call_ended_at=?,updated_at=? "
            "WHERE id=? AND customer_id=? AND state NOT IN ('dismissed','ended')",
            (now, now, event_id, customer_id),
        )
    audit(actor or {}, "aac_voice_call.ended", "aac_voice_call_event", event_id, {})


def get_voice_call_event(*, event_id: str, customer_id: str) -> dict | None:
    """Tenant-scoped single-row lookup -- customer_id is part of the
    WHERE clause, not just checked after the fact, so a mismatched id
    returns None indistinguishably from a nonexistent one (never leaks
    whether some OTHER customer's event id happens to exist)."""
    return row("SELECT * FROM aac_voice_call_events WHERE id=? AND customer_id=?", (event_id, customer_id))


def list_voice_call_events_for_customer(customer_id: str, *, limit: int = 50) -> list[dict]:
    return rows(
        "SELECT * FROM aac_voice_call_events WHERE customer_id=? ORDER BY created_at DESC LIMIT ?",
        (customer_id, limit),
    )
