"""AAC Voice Call -- routes and vertical-slice orchestration, Phase 1.

    Person at front door -> speech/visitor intent detected -> homeowner
    receives a visitor-call alert -> homeowner opens live camera ->
    two-way audio conversation starts -> optional door unlock later.

This module wires exactly the first three arrows of that chain end to
end (trigger -> intent -> notification) plus a thin "open the call
screen" page for the fourth. It deliberately reuses, rather than
duplicates, three pieces of infrastructure already proven elsewhere in
this codebase:

  - detection_events / detection_event_media (main.py) is the existing
    real-time analytics pipeline a real "person detected at the
    entrance" trigger belongs to. Phase 1 accepts an OPTIONAL
    trigger_detection_event_id (see aac_voice_call_events.
    create_voice_call_event()) so a real detection can already be
    linked once Phase 2's appliance-side wiring exists, without a
    schema change later. The trigger route this module exposes today
    is a simulated/customer-triggered one -- see simulate_trigger()'s
    own docstring for exactly why, and what Phase 2 still owes.

  - notification_engine.fanout_appliance_event() is the existing,
    already-correct notification fan-out (in-app row + email/SMS,
    quiet hours, per-viewer camera permissions, all already built) --
    this module never inserts into `notifications` directly. Adding
    'aac_voice_call' to that module's own SUPPORTED set (and to
    notification_preferences.EVENT_TYPES, so a customer can control
    email/SMS for it) is the only change fanout_appliance_event()
    itself needed; the event_type value flows through this module's own
    aac_voice_call_events table unchanged.

  - live_view_page.py's existing Live camera view (video panel, the
    already-real press-and-hold talk-mic UI wired to talk_sessions.py/
    talk_audio_relay.py) is what the AAC Voice Call "call screen"
    embeds via an <iframe>, rather than a second video/audio stack --
    see voice_call_screen()'s own docstring for exactly what is real
    (the video, and the talk-mic button/UI) versus what is not (the
    appliance-side ONVIF backchannel transport those button presses
    ultimately need is real code, but ANYAICAM_TALK_AUDIO_ENABLED
    defaults false and its own hardware-validation status is
    unconfirmed -- see talk_audio_relay_client.py/talk_down_discovery.py's
    own module docstrings). This module does not claim or fake anything
    about that readiness; it only ever reuses whatever the Live page
    itself already renders.

Tenant isolation follows facial_recognition_ui.py's own established
discipline for a new module: every route resolves customer_id from
partner_identity() (never current_user(), the legacy local-VMS auth --
the exact mismatch this session's own review found and fixed twice
already elsewhere), and aac_voice_call_events.py's own functions take
customer_id as an explicit, required argument on every query.

Phase 5 (door unlock) is intentionally NOT implemented or called from
anywhere in this module. request_door_unlock() below exists only as a
prepared interface, per the product spec's own instruction -- keeping
actual Z-Wave lock integration a later phase. It is modeled directly on
relay_control.py's own RelayProvider/RelayRequest/RelayResult
abstraction (the existing, real precedent in this codebase for "the
interface is ready, there is no hardware-backed implementation yet")
rather than door_access.py's Face-Access-specific manual-unlock flow,
since that flow's own authorization model (can_unlock permission,
automatic-unlock evaluation from a matched face) is a different
product concept from a homeowner-authorized unlock mid-voice-call.

UPDATE (2026-09-23): the paragraph above describes this module's own
history, not its current state -- door unlock is now real (see
aac_voice_call_door.py's own module docstring; request_door_unlock()
just below remains superseded/uncalled, exactly as before). This same
date also adds Phase 2's own "owed" piece: real proactive triggering.
handle_person_detected() is what main.py's detection-loop hook now
calls for a genuine appliance person-detection (see that hook's own
comment in main.py), and record_visitor_utterance() is the listening-
window's own real, natural-language-classified continue-vs-escalate
step -- see both functions' own docstrings, and aac_voice_call_
greeting.py's module docstring for the one honestly-still-not-real
piece (actual text-to-speech synthesis and camera-speaker delivery).

CLOUD/EDGE SPLIT (2026-09-24): in a split deployment the cloud owns the
one authoritative, homeowner-facing Voice Call session, and the edge
only does what must happen locally. handle_edge_person_detected() is
the edge half: entrance-camera check (config synced down from the cloud
by edge_camera_sync.py), local cooldown, local greeting, then the
trigger is queued to the cloud through the existing analytics-sync
channel (durable, retried after an outage) -- it never creates a local
aac_voice_call_events row, notification, or listening window.
ingest_edge_visitor_event() is the cloud half, called by appliance_
cloud.py's analytics-event route. handle_person_detected() remains the
path for a process that is itself the coordinator (combined role, or an
edge with no cloud sync, i.e. Local mode) and for the simulate route.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import aac_voice_call_door
import aac_voice_call_events as store
import aac_voice_call_greeting
from aac_voice_call_intent import DeterministicVisitorIntentClassifier, NaturalLanguageVisitorIntentClassifier, VisitorIntentClassifier
from notification_engine import fanout_appliance_event
from partner_db import row, rows
from partner_portal import partner_identity

logger = logging.getLogger("anyaicam.aac_voice_call")

# 2026-09-23 proactive flow: how long one camera stays in its own
# debounce/cooldown window after a real trigger before it is willing to
# greet again -- long enough that one visitor standing at the door
# doesn't hear/trigger a flood of repeated greetings and notifications,
# short enough that a genuinely new visitor minutes later still gets
# greeted. See aac_voice_call_events.check_and_stamp_cooldown() for the
# atomic claim this gates.
DEFAULT_GREETING_COOLDOWN_SECONDS = 300.0

# How many of the visitor's own utterances the listening window accepts
# before escalating to the homeowner even if intent never resolved
# confidently -- a real conversation should not loop forever with
# neither side reaching a resolution.
MAX_UTTERANCES_BEFORE_ESCALATION = 3

# The analytics-event type an edge Voice Call trigger travels to the
# cloud as (analytics_sync.py -> POST /api/appliance/analytics/{camera_id}
# /events -> ingest_edge_visitor_event()).
EDGE_EVENT_TYPE = "aac_voice_call"
# The visitor's answer, transcribed on the edge (aac_voice_call_listen.py)
# and attached by the cloud to the session the trigger created.
EDGE_UTTERANCE_EVENT_TYPE = "aac_voice_call_utterance"


def _schedule_visitor_listening(greeting_result, camera_number, buffer_root, on_transcript) -> bool:
    """Listen for the visitor's answer after a greeting that actually
    played. Needs the camera's recording buffer (buffer_root) -- a caller
    without one (tests, the simulate route) simply doesn't listen."""
    if buffer_root is None or camera_number is None or not getattr(greeting_result, "delivered", False):
        return False
    try:
        import aac_voice_call_listen

        return aac_voice_call_listen.schedule_listen(
            camera_number=camera_number,
            greeting_seconds=float(getattr(greeting_result, "duration_seconds", None) or 0.0),
            buffer_root=buffer_root,
            on_transcript=on_transcript,
        )
    except Exception as error:
        print(f"AAC Voice Call listening skipped (non-fatal) for camera {camera_number}: {error}")
        return False


def _record_local_transcript(customer_id: str, event_id: str, transcript) -> None:
    try:
        record_visitor_utterance(customer_id=customer_id, event_id=event_id, transcript_text=transcript.text,
                                 actor={"email": "aac-voice-call-listener", "role": "system"})
    except HTTPException as error:
        print(f"AAC Voice Call transcript not recorded for {event_id}: {error.detail}")


def ingest_edge_visitor_utterance(*, customer_id: str, camera_id: str, trigger_detection_event_id: str | None, transcript_text: str) -> dict:
    """Cloud side of an edge transcript: attach it to the one session the
    trigger created, through record_visitor_utterance() (intent,
    escalation, and never any door action)."""
    transcript_text = (transcript_text or "").strip()[:1000]
    if not transcript_text:
        return {"status": "skipped", "skipped_reason": "empty_transcript"}
    if not trigger_detection_event_id:
        return {"status": "skipped", "skipped_reason": "session_not_found"}
    session = store.get_event_by_trigger_detection(customer_id=customer_id, detection_event_id=trigger_detection_event_id)
    if not session:
        # This trigger joined the camera's live call (one call per visit):
        # its words belong to that call.
        session = store.active_call_for_camera(customer_id=customer_id, camera_id=camera_id)
    if not session or session.get("camera_id") != camera_id:
        return {"status": "skipped", "skipped_reason": "session_not_found"}
    try:
        outcome = record_visitor_utterance(customer_id=customer_id, event_id=session["id"], transcript_text=transcript_text,
                                           actor={"email": "edge-voice-call-listener", "role": "system"})
    except HTTPException as error:
        return {"status": "skipped", "skipped_reason": "not_listening", "event_id": session["id"], "detail": error.detail}
    return {"status": "accepted", "event_id": session["id"], "outcome": outcome}

# An edge trigger that reaches the cloud later than this (the edge was
# offline and its queued event was only delivered once connectivity came
# back) is recorded as a missed visitor: the homeowner still learns
# someone came by, but is never told "someone is at your door" about a
# visitor who left long ago, and no listening window is opened for a
# conversation that can no longer happen. Same length as the greeting
# cooldown, i.e. roughly one visit.
STALE_EDGE_TRIGGER_SECONDS = 300.0


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"}:
        raise HTTPException(status_code=403, detail="Customer account required.")
    return identity


# Simulation routes (simulate-trigger, simulate-person-detected,
# simulate-visitor-utterance) fabricate visitor events and transcripts.
# They are development/test tools, never customer production controls
# (2026-10-02, Codex review): off unless this environment opts in, and
# then for the account owner only.
SIMULATION_ENV = "ANYAICAM_AAC_VC_SIMULATION_ENABLED"


def _require_simulation(identity: dict) -> None:
    import os
    if os.environ.get(SIMULATION_ENV, "").strip().lower() not in {"1", "true", "yes"} or identity.get("role") != "customer_owner":
        raise HTTPException(status_code=404, detail="Not found.")


def camera_permitted(identity: dict, camera_id: str) -> bool:
    """Visitor Call per-camera authorization (2026-10-02, Codex review).

    A Visitor Call is the entrance camera's live view plus its visitor's
    words, so it needs exactly what Live needs: the account owner, or a
    household member/viewer whose camera grant includes Live for this
    camera (customer_camera_permissions.can_live, or camera_access_mode
    'all'). Same decision as live_view_page._authorized_camera() via
    camera_access.is_camera_authorized(); read fresh on every call, so a
    revoked grant takes effect immediately. The caller has already scoped
    the camera to identity['customer_id']."""
    from camera_access import is_camera_authorized
    role = identity.get("role")
    if role == "customer_owner":
        return True
    if role != "customer_viewer" or not identity.get("email") or not identity.get("customer_id"):
        return False
    user = row("SELECT id,camera_access_mode,account_status FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?",
               (identity["email"], identity["customer_id"]))
    if not user or (user.get("account_status") or "active") != "active":  # suspended/removed: no access
        return False
    permitted = {r["camera_id"] for r in rows(
        "SELECT p.camera_id FROM customer_camera_permissions p JOIN cameras c ON c.id=p.camera_id "
        "WHERE p.user_id=? AND p.can_live=1 AND c.customer_id=?", (user["id"], identity["customer_id"]))}
    return is_camera_authorized(camera_id, role="customer_viewer", access_mode=user.get("camera_access_mode") or "selected",
                                permitted_camera_ids=permitted)


def _authorized_event(identity: dict, event_id: str) -> dict:
    """Every Visitor Call read or action goes through here: tenant scope
    (customer_id in the WHERE clause) AND the signed-in person's camera
    grant. Both failures answer the same 404, so an ID never reveals that
    a call exists on a camera this person may not see."""
    store.reconcile(customer_id=identity["customer_id"])  # expired/abandoned calls first (lifecycle)
    event = store.get_voice_call_event(event_id=event_id, customer_id=identity["customer_id"])
    if not event or not camera_permitted(identity, event["camera_id"]):
        raise HTTPException(status_code=404, detail="AAC Voice Call event not found.")
    return event


# Camera-side "Call ended" (2026-10-02): when the owner ends a Visitor Call
# the visitor hears it, through the camera speaker path the greeting
# already uses (espeak-ng TTS -> ISAPI two-way audio,
# aac_voice_call_greeting). The text is fixed here: a cloud message only
# ever names the camera, never what to say.
CALL_ENDED_TEXT = "Call ended."

# The selected Visitor Call chime (AAC_Visitor_Call_Chime.wav, the
# original file, never re-encoded): played by the call page while the call
# rings.
CHIME_FILENAME = "AAC_Visitor_Call_Chime.wav"


def _closed_label(event: dict) -> str:
    if event.get("state") == "missed":
        return "Missed call." if event.get("end_reason") != "unanswered_timeout" else "Missed call: nobody answered in time."
    if event.get("end_reason") == "connection_lost":
        return "Call ended: the connection was lost."
    return "Call ended."


def speak_call_ended(*, camera_id: str, event_id: str = "", customer_id: str | None = None, provider=None) -> dict:
    """Plays CALL_ENDED_TEXT on a camera this process reaches directly (the
    edge, or a combined process). Never raises."""
    try:
        camera = row("SELECT id,customer_id FROM cameras WHERE id=?", (camera_id,))
        if not camera or (customer_id and camera["customer_id"] != customer_id):
            return {"delivered": False, "reason": "camera_not_found"}
        volume = store.resolve_greeting_volume(customer_id=camera["customer_id"], camera_id=camera_id)
        result = (provider or aac_voice_call_greeting.get_provider()).speak(aac_voice_call_greeting.GreetingRequest(
            camera_id=camera_id, customer_id=camera["customer_id"], event_id=event_id or "", text=CALL_ENDED_TEXT,
            reason="aac_voice_call_ended", volume=volume,
        ))
        return {"delivered": bool(getattr(result, "delivered", False)), "reason": getattr(result, "suppressed_reason", None)}
    except Exception as error:  # feedback must never break ending the call
        logger.warning("aac_voice_call.call_ended_feedback_failed camera_id=%s error=%s", camera_id, type(error).__name__)
        return {"delivered": False, "reason": "error"}


def _runtime_role() -> str:
    from cloud_config import settings as _settings
    return _settings.runtime_role


def _call_ended_feedback(event: dict, identity: dict) -> str:
    """After the owner ends a call: tell the camera's appliance over its
    open control channel (appliance_control, the same real-time path the
    portal's door unlock uses) or, when this process reaches the camera
    itself, speak it here. Never blocks or fails the End request."""
    try:
        camera = row("SELECT id,appliance_id,talk_down_supported FROM cameras WHERE id=? AND customer_id=?",
                     (event["camera_id"], identity["customer_id"]))
        if not camera or camera.get("talk_down_supported") != 1:
            return "camera_has_no_speaker"
        if _runtime_role() == "cloud":
            import appliance_control
            sent = appliance_control.send(camera["appliance_id"], {"type": "voice_call_ended", "camera_id": camera["id"],
                                                                   "event_id": event["id"]})
            return "sent_to_appliance" if sent else "appliance_not_connected"
        import threading
        threading.Thread(target=speak_call_ended, kwargs={"camera_id": camera["id"], "event_id": event["id"],
                                                          "customer_id": identity["customer_id"]},
                         name=f"aac-call-ended-{camera['id']}", daemon=True).start()
        return "spoken_locally"
    except Exception as error:
        logger.warning("aac_voice_call.call_ended_feedback_failed event_id=%s error=%s", event.get("id"), type(error).__name__)
        return "error"


def _authorized_camera(customer_id: str, camera_id: str) -> dict:
    """Tenant-scoped camera lookup -- customer_id is part of the WHERE
    clause, matching aac_voice_call_events.get_voice_call_event()'s own
    discipline (a camera belonging to a different customer 404s
    indistinguishably from a nonexistent id)."""
    camera = row("SELECT id,customer_id,site_id,name FROM cameras WHERE id=? AND customer_id=?", (camera_id, customer_id))
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found.")
    return camera


def _authorized_site(customer_id: str, site_id: str) -> dict:
    site = row("SELECT id,customer_id,name FROM sites WHERE id=? AND customer_id=?", (site_id, customer_id))
    if not site:
        raise HTTPException(status_code=404, detail="Site not found.")
    return site


def _camera_tenant_context(db, camera_number: int, appliance_id: str | None) -> dict | None:
    """Resolves an appliance-local camera_number to this feature's own
    tenant-scoped identity (id/customer_id/site_id/name) -- a small,
    deliberately-local duplicate of facial_events.py's own private
    _camera_tenant_context() rather than importing that module's
    internal helper: each detection-hook module owns its own camera-
    number resolution for its own use, matching this codebase's
    existing convention of door_access.door_camera() and facial_events.
    _camera_tenant_context() being separate, not-shared lookups despite
    doing a similar thing. Returns None for an unknown camera_number --
    never raises, since this is called from a non-request background
    detection context (main.py's detection loop), not an HTTP route.

    camera_number is only unique per appliance, so the lookup is scoped
    to this appliance's own cameras (appliance_activation.active_
    appliance_id()). No appliance identity, or more than one row for
    the same appliance + camera_number, resolves no camera (logged for
    the ambiguous case) rather than greeting/notifying an arbitrary
    tenant's camera."""
    if not appliance_id:
        return None
    matches = db.execute(
        "SELECT id,customer_id,site_id,name FROM cameras WHERE camera_number=? AND appliance_id=?",
        (camera_number, appliance_id),
    ).fetchall()
    if len(matches) > 1:
        logger.warning(
            "aac_voice_call.ambiguous_camera_number appliance_id=%s camera_number=%s matches=%s -- resolving no camera",
            appliance_id, camera_number, len(matches),
        )
        return None
    return dict(matches[0]) if matches else None


def _visitor_message(intent: str, camera_name: str) -> str:
    labels = {
        "greeting": "Someone said hello",
        "presence_check": "Someone is asking if anyone is home",
        "delivery": "A delivery visitor",
        "maintenance": "A maintenance/service visitor",
        "visitor": "A visitor",
        "unknown": "Someone",
    }
    return f"{labels.get(intent, 'Someone')} is at {camera_name}."


def trigger_visitor_event(
    *,
    customer_id: str,
    camera_id: str,
    transcript_text: str = "",
    trigger_detection_event_id: str | None = None,
    thumbnail_s3_key: str | None = None,
    classifier: VisitorIntentClassifier | None = None,
    actor: dict | None = None,
) -> dict:
    """The real Phase 1 vertical slice, as a plain function so it is
    directly unit/integration-testable without an HTTP/auth round trip
    -- both simulate_trigger() below and the test suite call this same
    function. Raises HTTPException(400) if camera_id is not a
    configured, enabled AAC Voice Call entrance camera for this
    customer -- "only cameras explicitly configured as an entrance
    camera should participate" is enforced here, at the one choke
    point every trigger (simulated today, appliance-sourced once Phase
    2 exists) must pass through."""
    import feature_entitlements
    feature_entitlements.require(customer_id, feature_entitlements.VOICE_CALL)
    camera = _authorized_camera(customer_id, camera_id)
    if not store.is_entrance_camera(customer_id, camera_id):
        raise HTTPException(status_code=400, detail="This camera is not configured as an AAC Voice Call entrance camera.")

    classifier = classifier or DeterministicVisitorIntentClassifier()
    intent_result = classifier.classify(transcript_text)

    # One live call per camera (2026-10-02): a visitor still ringing or
    # talking on this camera is the same visit -- never a second ring.
    active = store.active_call_for_camera(customer_id=customer_id, camera_id=camera_id)
    if active:
        return {"event_id": active["id"], "joined_active_call": True, "intent": active.get("intent"),
                "intent_confidence": active.get("intent_confidence"), "notifications_created": 0,
                "notification_id": active.get("notification_id")}

    event_id = store.create_voice_call_event(
        customer_id=customer_id,
        site_id=camera["site_id"],
        camera_id=camera_id,
        trigger_detection_event_id=trigger_detection_event_id,
        transcript_text=transcript_text or None,
        intent=intent_result.intent,
        intent_confidence=intent_result.confidence,
        thumbnail_s3_key=thumbnail_s3_key,
        actor=actor,
    )
    store.mark_superseded(customer_id=customer_id, camera_id=camera_id, new_event_id=event_id)

    appliance = {"customer_id": customer_id, "site_id": camera["site_id"]}
    event = {
        "id": event_id,
        "camera_id": camera_id,
        "event_type": "aac_voice_call",
        "timestamp": datetime.now().isoformat(),
        "message": _visitor_message(intent_result.intent, camera["name"] or "your entrance camera"),
        "severity": "info",
    }
    notifications_created = fanout_appliance_event(appliance, event)

    notification_id = None
    if notifications_created:
        # fanout_appliance_event() creates one notifications row per
        # eligible recipient and returns only a count -- every row it
        # creates shares this event_id/event_type, so a follow-up
        # lookup (not a second insert) finds them. The call screen only
        # needs ONE reference for its own convenience link back; the
        # full per-recipient fan-out remains the notifications table's
        # own responsibility, unchanged.
        created_row = row(
            "SELECT id FROM notifications WHERE event_id=? AND event_type='aac_voice_call' ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        )
        notification_id = created_row["id"] if created_row else None
        store.mark_notified(event_id=event_id, customer_id=customer_id, notification_id=notification_id or "", actor=actor)

    return {
        "event_id": event_id,
        "intent": intent_result.intent,
        "intent_confidence": intent_result.confidence,
        "notifications_created": notifications_created,
        "notification_id": notification_id,
    }


def handle_person_detected(
    *,
    customer_id: str,
    camera_id: str,
    trigger_detection_event_id: str | None = None,
    thumbnail_s3_key: str | None = None,
    cooldown_seconds: float = DEFAULT_GREETING_COOLDOWN_SECONDS,
    greeting_provider: object | None = None,
    actor: dict | None = None,
    camera_number: int | None = None,
    buffer_root=None,
) -> dict:
    """The real proactive trigger this phase adds: a person was detected
    on a camera -- either a genuine appliance detection (see main.py's
    detection-loop hook, which resolves camera_number to camera_id/
    customer_id via _camera_tenant_context() above and calls this
    function directly) or the customer-triggered simulate-person-
    detected route below, mirroring trigger_visitor_event()/
    simulate_trigger()'s own precedent for exactly this "real
    orchestration, simulated upstream trigger source until the
    appliance-side wiring is separately hardware-validated" pattern.

    Never raises for "not configured" or "still cooling down" -- both
    are normal, expected outcomes for a background detection hook that
    must not crash the wider detection loop (see main.py's own PPE/
    facial-recognition hooks for the same non-fatal posture) -- the
    caller reads result["triggered"] and result.get("skipped_reason")
    instead. Only "person detected -> greet -> notify -> open listening
    window" happens here; door authorization is never touched by this
    function or anything it calls (aac_voice_call_greeting.speak() has
    no relay_control dependency at all)."""
    import feature_entitlements
    if not feature_entitlements.allowed(customer_id, feature_entitlements.VOICE_CALL):
        return {"triggered": False, "skipped_reason": "not_entitled"}
    if not store.is_entrance_camera(customer_id, camera_id):
        return {"triggered": False, "skipped_reason": "not_entrance_camera"}
    if not store.check_and_stamp_cooldown(customer_id=customer_id, camera_id=camera_id, cooldown_seconds=cooldown_seconds):
        return {"triggered": False, "skipped_reason": "cooldown"}

    camera = _authorized_camera(customer_id, camera_id)

    active = store.active_call_for_camera(customer_id=customer_id, camera_id=camera_id)
    if active:  # the same visit is still ringing/talking: no second call
        return {"triggered": False, "skipped_reason": "call_in_progress", "event_id": active["id"]}

    event_id = store.create_voice_call_event(
        customer_id=customer_id,
        site_id=camera["site_id"],
        camera_id=camera_id,
        trigger_detection_event_id=trigger_detection_event_id,
        thumbnail_s3_key=thumbnail_s3_key,
        trigger_source="detection",
        actor=actor,
    )
    store.mark_superseded(customer_id=customer_id, camera_id=camera_id, new_event_id=event_id)

    greeting_text = store.resolve_greeting_text(customer_id=customer_id, camera_id=camera_id, site_id=camera["site_id"])
    greeting_result = None
    try:
        provider = greeting_provider or aac_voice_call_greeting.get_provider()
        greeting_result = provider.speak(aac_voice_call_greeting.GreetingRequest(
            camera_id=camera_id, customer_id=customer_id, event_id=event_id, text=greeting_text,
            volume=store.resolve_greeting_volume(customer_id=customer_id, camera_id=camera_id),
        ))
        # Only stamped on a successful dispatch -- an exception here
        # means the greeting was never actually sent anywhere, and
        # greeted_at must stay honest about that (matches this
        # codebase's own "never pretend the door opened" posture,
        # applied to "never pretend the greeting was spoken"). A failed
        # dispatch still never blocks the homeowner notification below,
        # which is the more important real-world outcome of the two --
        # same non-fatal posture as main.py's own PPE/facial-
        # recognition/LPR detection hooks.
        store.stamp_greeted(event_id=event_id, customer_id=customer_id, greeting_text_used=greeting_text, actor=actor)
    except Exception as error:
        print(f"AAC Voice Call greeting dispatch skipped (non-fatal) for camera {camera_id}: {error}")

    appliance = {"customer_id": customer_id, "site_id": camera["site_id"]}
    notify_event = {
        "id": event_id,
        "camera_id": camera_id,
        "event_type": "aac_voice_call",
        "timestamp": datetime.now().isoformat(),
        "message": f"Someone is at {camera['name'] or 'your entrance camera'}.",
        "severity": "info",
    }
    notifications_created = fanout_appliance_event(appliance, notify_event)

    notification_id = None
    if notifications_created:
        created_row = row(
            "SELECT id FROM notifications WHERE event_id=? AND event_type='aac_voice_call' ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        )
        notification_id = created_row["id"] if created_row else None
        store.mark_notified(event_id=event_id, customer_id=customer_id, notification_id=notification_id or "", actor=actor)

    store.open_listening_window(event_id=event_id, customer_id=customer_id, actor=actor)
    listening = _schedule_visitor_listening(
        greeting_result, camera_number, buffer_root,
        lambda transcript: _record_local_transcript(customer_id, event_id, transcript),
    )

    return {
        "triggered": True,
        "listening": listening,
        "event_id": event_id,
        "greeting_text": greeting_text,
        "notifications_created": notifications_created,
        "notification_id": notification_id,
    }


def cloud_coordinates_voice_calls() -> bool:
    """True when this process is an edge appliance whose Voice Call
    sessions are owned by the cloud: RUNTIME_ROLE=edge with analytics
    sync to the cloud enabled (Hybrid). False for a combined process (it
    is its own cloud) and for an edge with no cloud sync (Local mode),
    where this process's own local portal is the coordinator and
    handle_person_detected() runs the whole flow locally."""
    import analytics_sync

    return analytics_sync.RUNTIME_ROLE == "edge" and bool(analytics_sync.ANALYTICS_SYNC_ENABLED)


def handle_edge_person_detected(
    *,
    customer_id: str,
    camera_id: str,
    camera_number: int,
    forward_event: Callable[[dict], None],
    confidence: float | None = None,
    cooldown_seconds: float = DEFAULT_GREETING_COOLDOWN_SECONDS,
    greeting_provider: object | None = None,
    now: datetime | None = None,
    buffer_root=None,
) -> dict:
    """Edge half of the cloud/edge split (see this module's docstring):
    everything that must happen locally for a person at an entrance
    camera, and nothing that belongs to the cloud's session.

    Uses only locally synced state -- entrance-camera enablement and
    greeting text arrive from the cloud via edge_camera_sync.py -- so the
    greeting keeps working through a cloud/internet outage. The trigger
    is handed to forward_event (main.py passes append_analytics_event(),
    the durable local record analytics_sync.py forwards and retries), so
    an outage delays the cloud's event instead of losing it. Never
    creates a local aac_voice_call_events row, notification, or
    listening window: the cloud creates the one authoritative session
    when the event arrives (ingest_edge_visitor_event()). Never touches
    door/relay code. Never raises for "not configured"/"cooling down"."""
    if not store.is_entrance_camera(customer_id, camera_id):
        return {"triggered": False, "skipped_reason": "not_entrance_camera"}
    camera = row("SELECT id,site_id FROM cameras WHERE id=? AND customer_id=?", (camera_id, customer_id))
    if not camera:
        return {"triggered": False, "skipped_reason": "camera_not_found"}
    if not store.check_and_stamp_cooldown(customer_id=customer_id, camera_id=camera_id, cooldown_seconds=cooldown_seconds):
        return {"triggered": False, "skipped_reason": "cooldown"}

    now = now or datetime.now()
    local_event_id = f"aacvc-{uuid.uuid4().hex}"
    greeting_text = store.resolve_greeting_text(customer_id=customer_id, camera_id=camera_id, site_id=camera["site_id"])
    greeting_delivered = False
    result = None
    try:
        provider = greeting_provider or aac_voice_call_greeting.get_provider()
        result = provider.speak(aac_voice_call_greeting.GreetingRequest(
            camera_id=camera_id, customer_id=customer_id, event_id=local_event_id, text=greeting_text,
            volume=store.resolve_greeting_volume(customer_id=customer_id, camera_id=camera_id),
        ))
        greeting_delivered = bool(getattr(result, "delivered", False))
    except Exception as error:
        # A failed greeting never blocks the homeowner being told a
        # visitor is at the door -- the trigger is still forwarded, just
        # honestly marked as not greeted.
        print(f"AAC Voice Call greeting dispatch skipped (non-fatal) for camera {camera_id}: {error}")

    forward_event({
        "id": local_event_id,
        "camera": camera_number,
        "event_type": EDGE_EVENT_TYPE,
        "timestamp": now.isoformat(),
        "confidence": confidence,
        "object_count": 1,
        "greeting_text_used": greeting_text,
        "greeting_delivered": greeting_delivered,
        "mock": False,
    })

    def _forward_transcript(transcript) -> None:
        forward_event({
            "id": f"{local_event_id}-reply",
            "camera": camera_number,
            "event_type": EDGE_UTTERANCE_EVENT_TYPE,
            "timestamp": datetime.now().isoformat(),
            "confidence": transcript.confidence,
            "object_count": 1,
            "voice_call_local_event_id": local_event_id,
            "transcript_text": transcript.text,
            "stt_engine": transcript.engine,
            "mock": False,
        })

    listening = _schedule_visitor_listening(result, camera_number, buffer_root, _forward_transcript)
    return {
        "triggered": True,
        "listening": listening,
        "local_event_id": local_event_id,
        "greeting_text": greeting_text,
        "greeting_delivered": greeting_delivered,
    }


def _edge_trigger_is_stale(event_timestamp: str, now: datetime) -> bool:
    """An unparseable timestamp, or one in the future (clock skew), is
    treated as fresh -- the homeowner is never denied a live alert over
    a formatting or clock difference."""
    try:
        occurred = datetime.fromisoformat(str(event_timestamp))
    except (TypeError, ValueError):
        return False
    if occurred.tzinfo is not None:
        occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)
        now = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    return (now - occurred).total_seconds() > STALE_EDGE_TRIGGER_SECONDS


def ingest_edge_visitor_event(
    *,
    customer_id: str,
    camera_id: str,
    detection_event_id: str,
    event_timestamp: str,
    greeting_text_used: str | None = None,
    greeting_delivered: bool = False,
    now: datetime | None = None,
) -> dict:
    """Cloud half of the cloud/edge split: turns one edge Voice Call
    trigger (already stored as detection_events row detection_event_id by
    appliance_cloud.py's analytics-event route, which resolved customer_
    id/camera_id from the authenticated appliance) into the ONE
    authoritative aac_voice_call_events session -- then notifies the
    homeowner and opens the listening window, exactly like
    handle_person_detected() does for a coordinator-local trigger.

    Idempotent on detection_event_id (lookup first, plus the partial
    UNIQUE index from migration 20260924_aac_voice_call_edge_trigger as
    the race backstop), so a retried/replayed delivery never creates a
    second session or a second notification. The cloud's own entrance-
    camera config is authoritative: a camera the customer disabled after
    the edge last synced is recorded as a detection only, with no call.
    A trigger delivered later than STALE_EDGE_TRIGGER_SECONDS becomes a
    missed-visitor record instead of a live call."""
    existing = store.get_event_by_trigger_detection(customer_id=customer_id, detection_event_id=detection_event_id)
    if existing:
        return {"status": "duplicate", "event_id": existing["id"]}
    import feature_entitlements
    if not feature_entitlements.allowed(customer_id, feature_entitlements.VOICE_CALL):
        # A detection only: no call, no homeowner notification (an appliance
        # that had not yet synced its entrance cameras away).
        return {"status": "skipped", "skipped_reason": "not_entitled"}
    if not store.is_entrance_camera(customer_id, camera_id):
        return {"status": "skipped", "skipped_reason": "not_entrance_camera"}
    camera = row("SELECT id,site_id,name FROM cameras WHERE id=? AND customer_id=?", (camera_id, customer_id))
    if not camera:
        return {"status": "skipped", "skipped_reason": "camera_not_found"}

    stale = _edge_trigger_is_stale(event_timestamp, now or datetime.now())
    if not stale:
        active = store.active_call_for_camera(customer_id=customer_id, camera_id=camera_id)
        if active:  # same visit already ringing/talking: its transcripts join it
            return {"status": "joined_active_call", "event_id": active["id"]}
    try:
        event_id = store.create_voice_call_event(
            customer_id=customer_id,
            site_id=camera["site_id"],
            camera_id=camera_id,
            trigger_detection_event_id=detection_event_id,
            event_timestamp=event_timestamp,
            trigger_source="detection",
        )
    except Exception:
        # Lost a race with a concurrent delivery of the same detection --
        # the UNIQUE index rejected the second insert; the winner's
        # session is the authoritative one.
        existing = store.get_event_by_trigger_detection(customer_id=customer_id, detection_event_id=detection_event_id)
        if existing:
            return {"status": "duplicate", "event_id": existing["id"]}
        raise
    store.mark_superseded(customer_id=customer_id, camera_id=camera_id, new_event_id=event_id)

    if greeting_delivered and greeting_text_used:
        store.stamp_greeted(event_id=event_id, customer_id=customer_id, greeting_text_used=greeting_text_used)

    camera_name = camera["name"] or "your entrance camera"
    notifications_created = fanout_appliance_event(
        {"customer_id": customer_id, "site_id": camera["site_id"]},
        {
            "id": event_id,
            "camera_id": camera_id,
            "event_type": EDGE_EVENT_TYPE,
            "timestamp": event_timestamp,
            "message": f"You missed a visitor at {camera_name}." if stale else f"Someone is at {camera_name}.",
            "severity": "info",
        },
    )
    notification_id = None
    if notifications_created:
        created_row = row(
            "SELECT id FROM notifications WHERE event_id=? AND event_type='aac_voice_call' ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        )
        notification_id = created_row["id"] if created_row else None
        store.mark_notified(event_id=event_id, customer_id=customer_id, notification_id=notification_id or "")

    if stale:
        store.mark_missed(event_id=event_id, customer_id=customer_id)
    else:
        store.open_listening_window(event_id=event_id, customer_id=customer_id)

    return {
        "status": "accepted",
        "event_id": event_id,
        "stale": stale,
        "notifications_created": notifications_created,
        "notification_id": notification_id,
    }


def record_visitor_utterance(
    *,
    customer_id: str,
    event_id: str,
    transcript_text: str,
    classifier: VisitorIntentClassifier | None = None,
    actor: dict | None = None,
) -> dict:
    """Step 2 of the proactive flow: the visitor said something during
    an open listening window. Classifies intent with
    NaturalLanguageVisitorIntentClassifier (broad natural-phrasing
    coverage, NOT a fixed phrase list -- see that class's own
    docstring), records it, and decides continue-vs-escalate.

    This function -- and everything it calls -- NEVER reads any door-
    unlock/relay_control code path, and never will by construction:
    escalation only ever creates a second, ordinary homeowner
    notification through the exact same fanout_appliance_event() path
    handle_person_detected() already used, the same real mechanism a
    human then reviews and acts on through the existing, separately-
    reviewed aac_voice_call_door.py two-step confirmed-unlock flow --
    nothing a visitor says can ever unlock a door on its own, no matter
    how it is classified or how urgent it sounds."""
    event = store.get_voice_call_event(event_id=event_id, customer_id=customer_id)
    if not event:
        raise HTTPException(status_code=404, detail="AAC Voice Call event not found.")
    if not event.get("listening_opened_at") or event.get("listening_closed_at"):
        raise HTTPException(status_code=409, detail="This call is not currently listening for a response.")
    if event["state"] in ("ended", "dismissed"):
        raise HTTPException(status_code=409, detail="This call has already ended.")

    classifier = classifier or NaturalLanguageVisitorIntentClassifier()
    intent_result = classifier.classify(transcript_text)
    urgent = NaturalLanguageVisitorIntentClassifier.has_urgent_signal(transcript_text)

    utterance_count = store.record_visitor_utterance(
        event_id=event_id,
        customer_id=customer_id,
        transcript_text=transcript_text,
        intent=intent_result.intent,
        intent_confidence=intent_result.confidence,
        actor=actor,
    )

    escalated = False
    # Urgent always escalates immediately -- a distress/emergency signal
    # is never held back to "give the visitor another chance". An
    # unresolved (UNKNOWN) intent instead gets up to
    # MAX_UTTERANCES_BEFORE_ESCALATION tries to clarify before
    # escalating -- a single mumbled/unclear utterance should not by
    # itself page the homeowner; several in a row without ever
    # resolving should. A CONFIDENTLY recognized intent (delivery,
    # maintenance, etc.) never escalates through this path at all --
    # the homeowner already got the initial notification and can check
    # in whenever they choose.
    should_escalate = urgent or (intent_result.intent == "unknown" and utterance_count >= MAX_UTTERANCES_BEFORE_ESCALATION)
    if should_escalate and not event.get("escalated_at"):
        # store.mark_escalated()'s own return value -- not the
        # `not event.get("escalated_at")` check just above, which reads
        # a snapshot taken before this call and cannot see a concurrent
        # winner -- is the real, atomic claim. Two near-simultaneous
        # record_visitor_utterance() calls for the same event (a
        # duplicate/replayed request, two open tabs) can both reach
        # this line; at most one ever gets True back, so the second,
        # real homeowner notification below can never fire twice for
        # the same escalation.
        if store.mark_escalated(event_id=event_id, customer_id=customer_id, actor=actor):
            store.close_listening_window(event_id=event_id, customer_id=customer_id, actor=actor)
            camera = _authorized_camera(customer_id, event["camera_id"])
            appliance = {"customer_id": customer_id, "site_id": camera["site_id"]}
            escalate_event = {
                "id": event_id,
                "camera_id": event["camera_id"],
                "event_type": "aac_voice_call",
                "timestamp": datetime.now().isoformat(),
                "message": f"A visitor at {camera['name'] or 'your entrance camera'} needs your attention.",
                "severity": "warning",
            }
            fanout_appliance_event(appliance, escalate_event)
            escalated = True

    return {
        "event_id": event_id,
        "intent": intent_result.intent,
        "intent_confidence": intent_result.confidence,
        "utterance_count": utterance_count,
        "escalated": escalated,
    }


def request_door_unlock(*, customer_id: str, camera_id: str, event_id: str, requested_by: str) -> None:
    """SUPERSEDED (2026-09-23): Phase 5's real, owner-approved door-
    unlock flow is now implemented in aac_voice_call_door.py
    (request_unlock()/confirm_unlock()), wired into this module's own
    routes below -- never through this specific function, which remains
    exactly as it was (unimplemented, uncalled) so nothing depends on
    this particular signature. See aac_voice_call_door.py's own module
    docstring for the real design: a server-enforced two-step
    confirmation, re-authorization at both steps, and dispatch through
    the same door_access.py/relay_control.py primitives the manual
    Live-page Unlock Door button already uses -- never a second,
    AAC-Voice-Call-only door-control mechanism."""
    raise NotImplementedError(
        "This specific function is superseded -- see aac_voice_call_door.py's request_unlock()/confirm_unlock()."
    )


class SimulateTriggerPayload(BaseModel):
    camera_id: str
    transcript_text: str = ""
    thumbnail_s3_key: str | None = None


class SimulatePersonDetectedPayload(BaseModel):
    camera_id: str
    thumbnail_s3_key: str | None = None


class VisitorUtterancePayload(BaseModel):
    transcript_text: str


class CameraGreetingPayload(BaseModel):
    greeting_text: str | None = Field(default=None, max_length=500)


class GreetingVolumePayload(BaseModel):
    volume: str


class SiteGreetingPayload(BaseModel):
    greeting_text: str = Field(min_length=1, max_length=500)


class AnswerPayload(BaseModel):
    pass


class UnlockConfirmPayload(BaseModel):
    confirm_token: str


# Answer / End call controls for the Voice Call screen (2026-09-30), kept as
# a plain module constant so tests run the exact shipped code under Node.
# End call ends everything the call started on this phone, in this order:
# every active Talk session on the page is stopped (wireTalkMic registers
# its own stop() in window.anyaicamTalkStops -- the same teardown a normal
# release runs, which closes the audio WebSocket and so ends transmission
# to the camera), the microphone opened at Answer is released, Answer/End
# and the mic are disabled and "Call ended" is shown -- all before the
# server is told, so a slow or failed request never leaves the mic open.
# Repeated presses do nothing. Found in the 2026-09-30 physical test: End
# released only the call's own microphone while Talk kept streaming from
# its clone, and the owner pressed End 11 times.
_CALL_CONTROLS_JS = r"""
let callEnded = callInitiallyOver;
const answerButton = document.getElementById('voice-call-answer');
const endButton = document.getElementById('voice-call-end');
function releaseCallMicrophone() {
  if (window.aacCallMicStream) {
    window.aacCallMicStream.getTracks().forEach(track => track.stop());
    window.aacCallMicStream = null;
  }
}
function stopCallTalk() {
  (window.anyaicamTalkStops || []).forEach(stopTalk => { try { stopTalk(); } catch (e) {} });
}
// Visitor chime (2026-10-02): loops while the call rings, stops on answer,
// end or any close. Browsers may block sound until the person interacts
// with the page; then a note asks for a tap -- the call itself keeps working.
// The call page defines callInitialState/callHeartbeatSeconds/callChimeUrl;
// an embedding without them (e.g. a test harness) gets the original
// controls only -- no chime, no timers.
const callLifecycle = typeof callInitialState === 'string';
const callChime = (callLifecycle && typeof Audio === 'function') ? new Audio(callChimeUrl) : null;
if (callChime) { callChime.loop = true; callChime.preload = 'auto'; }
let chimeWanted = false;
function startChime() {
  if (!callChime || callEnded) return;
  chimeWanted = true;
  let attempt;
  try { attempt = callChime.play(); } catch (e) { attempt = Promise.reject(e); }
  Promise.resolve(attempt).then(() => {
    const note = document.getElementById('voice-call-sound-note'); if (note) note.hidden = true;
  }).catch(() => {
    const note = document.getElementById('voice-call-sound-note'); if (note && chimeWanted) note.hidden = false;
  });
}
function stopChime() {
  chimeWanted = false;
  if (callChime) { try { callChime.pause(); callChime.currentTime = 0; } catch (e) {} }
  const note = document.getElementById('voice-call-sound-note'); if (note) note.hidden = true;
}
if (typeof document.addEventListener === 'function') {
  document.addEventListener('pointerdown', () => { if (chimeWanted && callChime && callChime.paused) startChime(); });
}
let heartbeatTimer = null, ringPollTimer = null;
function stopCallTimers() {
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
  if (ringPollTimer) { clearInterval(ringPollTimer); ringPollTimer = null; }
}
function closedMessage(state) {
  return state === 'missed' ? 'Missed call.' : 'Call ended.';
}
function applyServerState(state) {
  if (!state || callEnded) return;
  const label = document.getElementById('voice-call-state');
  if (label) label.textContent = state;
  if (state === 'answered') { stopChime(); startHeartbeat(); return; }
  if (state === 'triggered' || state === 'notified') return;
  const note = document.getElementById('voice-call-ended');
  if (note) note.innerHTML = '<strong>' + closedMessage(state) + '</strong>';
  finishCallLocally(state);
}
async function sendHeartbeat() {
  try {
    const response = await fetch(`/api/customer/aac/voice-call/events/${eventId}/heartbeat`, {method: 'POST'});
    if (response.status === 404) { applyServerState('ended'); return; }
    if (!response.ok) return;  // transient: try again on the next beat
    const data = await response.json();
    if (data.state !== 'answered') applyServerState(data.state);
  } catch (e) { /* offline for a moment: the next beat reconnects */ }
}
function startHeartbeat() {
  if (ringPollTimer) { clearInterval(ringPollTimer); ringPollTimer = null; }
  if (!callLifecycle || heartbeatTimer || callEnded) return;
  sendHeartbeat();
  heartbeatTimer = setInterval(sendHeartbeat, Math.max(5, callHeartbeatSeconds) * 1000);
}
async function pollRinging() {
  try {
    const response = await fetch(`/api/customer/aac/voice-call/events/${eventId}`);
    if (response.status === 404) { applyServerState('ended'); return; }
    if (!response.ok) return;
    applyServerState((await response.json()).state);
  } catch (e) {}
}
if (typeof document.addEventListener === 'function') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || callEnded) return;
    if (heartbeatTimer) sendHeartbeat(); else pollRinging();  // back from the background: reconnect now
  });
}
function finishCallLocally() {
  callEnded = true;
  stopChime();
  stopCallTimers();
  stopCallTalk();
  releaseCallMicrophone();
  if (answerButton) answerButton.disabled = true;
  if (endButton) endButton.disabled = true;
  document.querySelectorAll('.talk-mic').forEach(mic => { mic.disabled = true; });
  const state = document.getElementById('voice-call-state');
  if (state) state.textContent = 'ended';
  const note = document.getElementById('voice-call-ended');
  if (note) note.hidden = false;
}
if (callEnded) finishCallLocally();
else if (callLifecycle && callInitialState === 'answered') startHeartbeat();  // refreshed during a call: resume it
else if (callLifecycle) { startChime(); ringPollTimer = setInterval(pollRinging, 5000); }
// Answer (2026-09-28): turn the live audio on inside this tap (a phone
// only plays sound after a user gesture), record the answer, then ask for
// the microphone once so Talk works without a second prompt.
answerButton.addEventListener('click', async () => {
  if (callEnded) return;
  const video = document.getElementById('live-view-video');
  if (video) {
    video.muted = false;
    try { video.play(); } catch (e) {}
    const muteButton = document.getElementById('live-view-mute');
    if (muteButton) muteButton.textContent = '♫';
  }
  stopChime();
  const response = await fetch(`/api/customer/aac/voice-call/events/${eventId}/answer`, {method: 'POST'});
  const data = await response.json().catch(() => ({}));
  if (response.status === 404) { showToast(data.detail || 'This call has ended.'); applyServerState('ended'); return; }
  if (!response.ok) { showToast(data.detail || 'Could not answer this call.'); return; }
  if (data.state && data.state !== 'answered') { showToast(data.message || 'This call has ended.'); applyServerState(data.state); return; }
  if (callEnded) return;
  document.getElementById('voice-call-state').textContent = 'answered';
  startHeartbeat();
  let micMessage = '';
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      // Ended while the permission prompt was open: never keep this mic.
      if (callEnded) { stream.getTracks().forEach(track => track.stop()); return; }
      window.aacCallMicStream = stream;
      micMessage = ' Tap the microphone under the video to talk.';
    } catch (e) {
      micMessage = ' Microphone blocked: open this page in Safari or Chrome and allow the microphone to talk.';
    }
  } else {
    micMessage = ' This browser cannot use the microphone here: open the page in Safari or Chrome to talk.';
  }
  showToast('Call answered.' + micMessage);
});
window.addEventListener('pagehide', releaseCallMicrophone);
endButton.addEventListener('click', async () => {
  if (callEnded) return;
  finishCallLocally();
  try {
    const response = await fetch(`/api/customer/aac/voice-call/events/${eventId}/end`, {method: 'POST'});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      showToast(data.detail || 'Call ended on this phone, but it could not be saved. Please check your connection.');
      return;
    }
  } catch (e) {
    showToast('Call ended on this phone, but it could not be saved. Please check your connection.');
    return;
  }
  showToast('Call ended.');
});
"""


def register_aac_voice_call_routes(app: FastAPI, shell: Callable) -> None:
    @app.get("/api/customer/aac/voice-call/entrance-cameras")
    def list_entrance_cameras(request: Request) -> dict:
        identity = _customer_identity(request)
        return {"cameras": [camera for camera in store.list_entrance_cameras(identity["customer_id"])
                            if camera_permitted(identity, camera.get("camera_id") or camera.get("id"))]}

    @app.post("/api/customer/aac/voice-call/entrance-cameras/{camera_id}")
    def set_entrance_camera(request: Request, camera_id: str, enabled: bool = True) -> dict:
        """customer_owner-only, matching subscription-portal's own
        established owner-vs-viewer convention for account-level
        configuration actions (a viewer can use the feature, not
        configure which cameras participate in it)."""
        identity = _customer_identity(request)
        if identity["role"] != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can configure entrance cameras.")
        if enabled:  # turning a camera off is always allowed
            import feature_entitlements
            feature_entitlements.require(identity["customer_id"], feature_entitlements.VOICE_CALL)
        camera = _authorized_camera(identity["customer_id"], camera_id)
        store.set_entrance_camera(customer_id=identity["customer_id"], camera_id=camera["id"], enabled=enabled, configured_by=identity.get("email"))
        return {"message": f"{camera['name'] or camera_id} {'enabled' if enabled else 'disabled'} as an AAC Voice Call entrance camera.", "camera_id": camera_id, "enabled": enabled}

    @app.post("/api/customer/aac/voice-call/entrance-cameras/{camera_id}/greeting")
    def set_camera_greeting(request: Request, camera_id: str, payload: CameraGreetingPayload) -> dict:
        """customer_owner-only, same convention as set_entrance_camera()
        just above. greeting_text=null clears the per-camera override,
        falling back to the site default (or the fixed fallback)."""
        identity = _customer_identity(request)
        if identity["role"] != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can configure entrance camera greetings.")
        camera = _authorized_camera(identity["customer_id"], camera_id)
        store.set_camera_greeting_text(customer_id=identity["customer_id"], camera_id=camera["id"], greeting_text=payload.greeting_text, configured_by=identity.get("email"))
        return {"message": f"Greeting updated for {camera['name'] or camera_id}.", "camera_id": camera_id, "greeting_text": payload.greeting_text}

    @app.post("/api/customer/aac/voice-call/entrance-cameras/{camera_id}/greeting-volume")
    def set_camera_greeting_volume(request: Request, camera_id: str, payload: GreetingVolumePayload) -> dict:
        """customer_owner-only. Greeting-only loudness (low/medium/high),
        applied on the appliance to the greeting audio alone -- never the
        camera speaker volume, homeowner Talk, microphone or recordings."""
        identity = _customer_identity(request)
        if identity["role"] != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can configure entrance camera greetings.")
        camera = _authorized_camera(identity["customer_id"], camera_id)
        level = store.normalize_greeting_volume(payload.volume)
        if level is None:
            raise HTTPException(status_code=422, detail="Greeting volume must be Low, Medium or High.")
        store.set_camera_greeting_volume(customer_id=identity["customer_id"], camera_id=camera["id"], volume=level, configured_by=identity.get("email"))
        return {"message": f"Greeting volume for {camera['name'] or camera_id} set to {level.title()}.", "camera_id": camera_id, "greeting_volume": level}

    @app.post("/api/customer/aac/voice-call/sites/{site_id}/greeting")
    def set_site_greeting(request: Request, site_id: str, payload: SiteGreetingPayload) -> dict:
        """customer_owner-only. Sets the DEFAULT greeting every entrance
        camera on this site uses unless it has its own per-camera
        override (see set_camera_greeting() above)."""
        identity = _customer_identity(request)
        if identity["role"] != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can configure site greetings.")
        site = _authorized_site(identity["customer_id"], site_id)
        store.set_site_default_greeting(customer_id=identity["customer_id"], site_id=site["id"], greeting_text=payload.greeting_text, configured_by=identity.get("email"))
        return {"message": f"Default greeting updated for {site['name'] or site_id}.", "site_id": site_id, "greeting_text": payload.greeting_text}

    @app.post("/api/customer/aac/voice-call/simulate-person-detected")
    def simulate_person_detected(request: Request, payload: SimulatePersonDetectedPayload) -> dict:
        """The proactive flow's own honest simulate entrypoint --
        exactly simulate_trigger()'s own precedent just below, applied
        to the NEW "person detected" starting event instead of a
        transcript-already-known trigger: real appliance-side person-
        detection wiring is main.py's own detection-loop hook (see
        handle_person_detected()'s own docstring), which calls the same
        underlying function this route calls; only the upstream trigger
        source differs, and every downstream step (cooldown, greeting
        dispatch, notification fan-out, listening window) is real and
        identical either way."""
        identity = _customer_identity(request)
        _require_simulation(identity)
        return handle_person_detected(
            customer_id=identity["customer_id"],
            camera_id=payload.camera_id,
            thumbnail_s3_key=payload.thumbnail_s3_key,
            actor=identity,
        )

    @app.post("/api/customer/aac/voice-call/simulate-trigger")
    def simulate_trigger(request: Request, payload: SimulateTriggerPayload) -> dict:
        """Phase 1's real trigger entrypoint. Deliberately a customer-
        triggered "simulate" route rather than an appliance-authenticated
        ingestion endpoint: real analytics-pipeline integration (Phase
        2's own "detect a person... using the existing analytics/event
        infrastructure") needs the appliance Bearer/nonce auth scheme
        appliance_cloud.py's real ingestion routes use, which is a
        separate, larger piece of work than this vertical slice's own
        scope -- the product spec explicitly permits "simulated or
        existing person/audio event" for this first milestone. This
        route proves every downstream step (intent classification,
        event creation, notification fan-out, the call screen) for
        real; only the upstream trigger source is not yet the real
        appliance analytics pipeline."""
        identity = _customer_identity(request)
        _require_simulation(identity)
        return trigger_visitor_event(
            customer_id=identity["customer_id"],
            camera_id=payload.camera_id,
            transcript_text=payload.transcript_text,
            thumbnail_s3_key=payload.thumbnail_s3_key,
            actor=identity,
        )

    @app.get("/api/customer/aac/voice-call/events/{event_id}")
    def get_event(request: Request, event_id: str) -> dict:
        identity = _customer_identity(request)
        event = _authorized_event(identity, event_id)
        return event

    @app.post("/api/customer/aac/voice-call/events/{event_id}/answer")
    def answer_event(request: Request, event_id: str, payload: AnswerPayload = None) -> dict:
        identity = _customer_identity(request)
        import feature_entitlements
        feature_entitlements.require(identity["customer_id"], feature_entitlements.VOICE_CALL)
        event = _authorized_event(identity, event_id)
        # partner_identity()'s signed session token carries email/role/
        # customer_id only -- never a partner_users.id -- so the real row
        # id (required: answered_by_user_id is a FOREIGN KEY into
        # partner_users) is resolved the same way main.py's own customer
        # notification-read routes already do.
        user = row("SELECT id FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?", (identity["email"], identity["customer_id"]))
        if store.mark_answered(event_id=event_id, customer_id=identity["customer_id"], answered_by_user_id=user["id"] if user else None, actor=identity):
            return {"message": "Call answered.", "event_id": event_id, "state": "answered"}
        # Duplicate tap, second device, or a call that already closed:
        # harmless, and the answer says what is true.
        current = store.get_voice_call_event(event_id=event_id, customer_id=identity["customer_id"]) or {}
        if current.get("state") == "answered":
            return {"message": "This call is already answered.", "event_id": event_id, "state": "answered"}
        # Already closed: nothing changes, nothing reopens (harmless).
        return {"message": "This call has ended.", "event_id": event_id, "state": current.get("state") or "ended"}

    @app.post("/api/customer/aac/voice-call/events/{event_id}/end")
    def end_event_route(request: Request, event_id: str) -> dict:
        identity = _customer_identity(request)
        event = _authorized_event(identity, event_id)
        if store.end_call(event_id=event_id, customer_id=identity["customer_id"], actor=identity):
            _call_ended_feedback(event, identity)
            return {"message": "Call ended.", "event_id": event_id, "state": "ended"}
        return {"message": "This call had already ended.", "event_id": event_id, "state": "ended"}  # idempotent

    @app.post("/api/customer/aac/voice-call/events/{event_id}/heartbeat")
    def heartbeat_event(request: Request, event_id: str) -> dict:
        """The open call page, every store.HEARTBEAT_SECONDS. Keeps an
        answered call alive across refresh/reconnect; tells the page when
        the call has closed (missed, ended elsewhere, or abandoned)."""
        identity = _customer_identity(request)
        _authorized_event(identity, event_id)
        return {"event_id": event_id, "state": store.heartbeat(event_id=event_id, customer_id=identity["customer_id"]),
                "heartbeat_seconds": store.HEARTBEAT_SECONDS}

    @app.post("/api/customer/aac/voice-call/events/{event_id}/dismiss")
    def dismiss_event(request: Request, event_id: str) -> dict:
        identity = _customer_identity(request)
        event = _authorized_event(identity, event_id)
        store.mark_dismissed(event_id=event_id, customer_id=identity["customer_id"], actor=identity)
        return {"message": "Dismissed.", "event_id": event_id}

    @app.post("/api/customer/aac/voice-call/events/{event_id}/simulate-visitor-utterance")
    def simulate_visitor_utterance(request: Request, event_id: str, payload: VisitorUtterancePayload) -> dict:
        """The listening window's own honest simulate entrypoint --
        same precedent as simulate_trigger()/simulate_person_detected()
        above: real appliance-side audio capture + speech-to-text from
        the camera's own microphone does not exist anywhere in this
        codebase yet (see aac_voice_call_greeting.py's own module
        docstring for the identical gap on the OUTBOUND/greeting side).
        This route proves the real, complete downstream orchestration
        -- natural-language intent classification, transcript
        accumulation, continue-vs-escalate, the second notification on
        escalation -- end to end; only the upstream transcript source
        (a real visitor's spoken words, transcribed) is not yet wired
        to real hardware."""
        identity = _customer_identity(request)
        _require_simulation(identity)
        _authorized_event(identity, event_id)
        return record_visitor_utterance(
            customer_id=identity["customer_id"],
            event_id=event_id,
            transcript_text=payload.transcript_text,
            actor=identity,
        )

    @app.post("/api/customer/aac/voice-call/events/{event_id}/door/unlock-request")
    def door_unlock_request(request: Request, event_id: str) -> dict:
        """Step 1 of the owner-approved door-access flow -- see
        aac_voice_call_door.py's own module docstring for the full
        design. Never dispatches anything; only validates and issues a
        short-lived confirmation token."""
        identity = _customer_identity(request)
        _authorized_event(identity, event_id)
        return aac_voice_call_door.request_unlock(event_id=event_id, customer_id=identity["customer_id"], identity=identity)

    @app.post("/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm")
    def door_unlock_confirm(request: Request, event_id: str, payload: UnlockConfirmPayload) -> dict:
        """Step 2 -- the actual real unlock, gated on the exact
        confirmation token step 1 issued (single-use, short-lived,
        server-enforced) plus a fresh re-check of the approving user's
        live can_unlock permission. See aac_voice_call_door.py's
        confirm_unlock() for the complete authorization/audit trail."""
        identity = _customer_identity(request)
        _authorized_event(identity, event_id)
        return aac_voice_call_door.confirm_unlock(
            event_id=event_id, customer_id=identity["customer_id"], identity=identity, confirm_token=payload.confirm_token,
        )

    @app.get("/customer/voice-call-settings", response_class=HTMLResponse)
    def voice_call_settings(request: Request) -> str:
        """Settings -> Visitor Voice Call: entrance cameras, greeting text
        and greeting volume (aac_voice_call_settings_page.py)."""
        import aac_voice_call_settings_page
        identity = _customer_identity(request)
        content, scripts = aac_voice_call_settings_page.render_settings(identity)
        return shell("Visitor Voice Call", "settings", content, scripts)

    @app.get("/aac/voice-call/{event_id}", response_class=HTMLResponse)
    def voice_call_screen(request: Request, event_id: str) -> str:
        """The AAC Voice Call "call screen" -- Phase 4's UI, kept to the
        smallest real thing that satisfies it: camera video and the
        microphone/talk control are the EXISTING Live single-camera page
        (live_view_page.py), embedded via <iframe> rather than
        reimplemented, exactly matching "reuse the existing Live camera/
        two-way-audio infrastructure instead of creating a completely
        separate streaming system". Visitor thumbnail/state, recognized
        transcript/intent, and end-call are this page's own new UI,
        layered on top.

        What is genuinely real here: the embedded Live page's video feed
        and its press-and-hold talk-mic button/browser-side audio
        capture (live_view_page.py's _TALK_MIC_JS) -- both fully
        implemented, not mocked. What is NOT claimed as complete: whether
        that talk audio actually reaches the camera's speaker end to
        end. talk_audio_relay.py's own module docstring describes a real
        browser -> cloud -> appliance -> ffmpeg -> ONVIF backchannel
        path, but it and its appliance-side workers
        (talk_audio_relay_client.py, talk_down_discovery.py) are
        feature-flagged off by default (ANYAICAM_TALK_AUDIO_ENABLED /
        ANYAICAM_TALK_DOWN_DISCOVERY_ENABLED) and were "recovered from
        an old, unrelated-history branch" with no confirmed real-
        hardware validation noted anywhere in this codebase. This page
        does not hide or fake that -- see the "Audio status" line below,
        which states the real flag state honestly rather than always
        claiming the mic works."""
        identity = _customer_identity(request)
        event = _authorized_event(identity, event_id)
        camera = row("SELECT id,name,talk_down_supported FROM cameras WHERE id=? AND customer_id=?", (event["camera_id"], identity["customer_id"]))
        camera_name = (camera or {}).get("name") or "Entrance camera"
        call_over = (event.get("state") or "") in ("ended", "dismissed", "missed")

        import os

        # Audio status from the camera's real talk capability (2026-09-28).
        # Talk now goes through the appliance's local camera relay
        # (talk_audio_relay._LocalIsapiTalkRelay); the old
        # ANYAICAM_TALK_AUDIO_ENABLED flag only gates the legacy ONVIF
        # client, so keying this line on it told every homeowner that
        # audio was "not enabled" even on a talk-capable camera.
        if (camera or {}).get("talk_down_supported") == 1:
            audio_status = "Tap Answer to hear the visitor, then use the microphone button under the video to speak through the camera's speaker."
        else:
            audio_status = "This camera does not support two-way audio. You can still see and hear the visitor in the live view."

        # Never rendered for a camera that isn't a configured,
        # relay-assigned door, or for a viewer without a real can_unlock
        # grant on it -- matching this codebase's established "no
        # control that would only ever 403" convention (the live-tile
        # Unlock button follows the same rule). request_unlock()/
        # confirm_unlock() independently re-check this same
        # authorization at the moment either route is actually called;
        # this only decides whether the button exists at all.
        show_unlock_button = event["state"] in ("triggered", "notified", "answered") and aac_voice_call_door.can_unlock_from_call(
            customer_id=identity["customer_id"], camera_id=event["camera_id"], identity=identity,
        )

        from html import escape as esc

        # One interface (2026-09-28): the camera's live panel -- video, talk
        # mic and camera tools -- rendered directly into this page instead of
        # an <iframe> of the whole Live page, which on a phone showed a
        # second navigation bar, a second AACO assistant and a second bottom
        # bar inside this one. Same video/talk code and the same per-camera
        # live permission check as the Live page; this page keeps its own
        # Unlock Door button, so the panel's unlock tool is left off.
        import live_view_page
        from partner_db import connection
        live_panel_html, live_panel_scripts = "", ""
        try:
            with connection() as db:
                live_camera = live_view_page._authorized_camera(db, event["camera_id"], identity)
            live_panel_html, live_panel_scripts = live_view_page.camera_live_panel(live_camera, identity, show_unlock_tool=False, show_analytics=False)
        except HTTPException:
            live_panel_html = '<section class="panel"><p class="health-detail">You do not have live access to this camera.</p></section>'

        content = f'''<header class="topbar"><div><p class="eyebrow">AAC Voice Call</p><h1>Visitor at {esc(camera_name)}</h1></div>
<a class="ghost-button" href="/alerts">Back to alerts</a></header>
<section class="panel">
  <p><strong>Recognized intent:</strong> {esc((event.get("intent") or "unknown").replace("_", " "))} ({round((event.get("intent_confidence") or 0) * 100)}% confidence)</p>
  {f'<p><strong>Transcript:</strong> {esc(event["transcript_text"])}</p>' if event.get("transcript_text") else ""}
  <p><strong>Call state:</strong> <span id="voice-call-state">{esc(event.get("state") or "triggered")}</span></p>
  <p class="health-detail">{esc(audio_status)}</p>
</section>
<style>.dialog-actions button:disabled{{opacity:.4;cursor:not-allowed;filter:grayscale(1)}}.call-ended-note{{margin:0 auto 0 0}}</style>
<section class="panel dialog-actions">
  <p id="voice-call-ended" class="call-ended-note" role="status"{'' if call_over else ' hidden'}><strong>{esc(_closed_label(event))}</strong></p>
  <p id="voice-call-sound-note" class="health-detail" hidden>Tap anywhere on this page to hear the visitor chime.</p>
  <button class="action-button" id="voice-call-answer" type="button"{' disabled' if call_over else ''}>Answer</button>
  <button class="ghost-button" id="voice-call-end" type="button"{' disabled' if call_over else ''}>End call</button>
  {'<button class="ghost-button" id="voice-call-unlock" type="button">Unlock Door</button>' if show_unlock_button else ''}
</section>
{live_panel_html}
{'<p id="voice-call-unlock-status" class="health-detail"></p>' if show_unlock_button else ''}'''
        scripts = live_panel_scripts + f'''<script>
const eventId={event_id!r};
const callInitiallyOver={'true' if call_over else 'false'};
const callInitialState={esc(event.get("state") or "triggered")!r};
const callHeartbeatSeconds={store.HEARTBEAT_SECONDS};
const callChimeUrl='/static/sounds/{CHIME_FILENAME}';
''' + _CALL_CONTROLS_JS + f'''
{'''const unlockButton=document.getElementById('voice-call-unlock');
const unlockStatus=document.getElementById('voice-call-unlock-status');
unlockButton.addEventListener('click', async () => {
  unlockButton.disabled=true;
  let requestData;
  try {
    const requestResponse=await fetch(`/api/customer/aac/voice-call/events/${eventId}/door/unlock-request`, {method: 'POST'});
    requestData=await requestResponse.json();
    if (!requestResponse.ok) { showToast(requestData.detail||'Could not start door unlock.'); unlockButton.disabled=false; return; }
  } catch (error) { showToast('Could not reach the server.'); unlockButton.disabled=false; return; }
  const confirmed=window.confirm(`Unlock ${requestData.door_name}? This will physically unlock the door for a short time.`);
  if (!confirmed) { unlockStatus.textContent='Unlock cancelled.'; unlockButton.disabled=false; return; }
  try {
    const confirmResponse=await fetch(`/api/customer/aac/voice-call/events/${eventId}/door/unlock-confirm`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirm_token: requestData.confirm_token}),
    });
    const confirmData=await confirmResponse.json();
    if (!confirmResponse.ok) { unlockStatus.textContent=confirmData.detail||'The door could not be unlocked.'; showToast(unlockStatus.textContent); unlockButton.disabled=false; return; }
    unlockStatus.textContent=confirmData.message||'Door unlocked.';
    showToast(unlockStatus.textContent);
  } catch (error) { unlockStatus.textContent='Could not reach the server.'; showToast(unlockStatus.textContent); }
  unlockButton.disabled=false;
});''' if show_unlock_button else ''}
</script>'''
        return shell("AAC Voice Call", "aac-voice-call", content, scripts)
