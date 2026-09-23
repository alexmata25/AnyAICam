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
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import aac_voice_call_door
import aac_voice_call_events as store
import aac_voice_call_greeting
from aac_voice_call_intent import DeterministicVisitorIntentClassifier, NaturalLanguageVisitorIntentClassifier, VisitorIntentClassifier
from notification_engine import fanout_appliance_event
from partner_db import row, rows
from partner_portal import partner_identity

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


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"}:
        raise HTTPException(status_code=403, detail="Customer account required.")
    return identity


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


def _camera_tenant_context(db, camera_number: int) -> dict | None:
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
    detection context (main.py's detection loop), not an HTTP route."""
    record = db.execute(
        "SELECT id,customer_id,site_id,name FROM cameras WHERE camera_number=?",
        (camera_number,),
    ).fetchone()
    return dict(record) if record else None


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
    camera = _authorized_camera(customer_id, camera_id)
    if not store.is_entrance_camera(customer_id, camera_id):
        raise HTTPException(status_code=400, detail="This camera is not configured as an AAC Voice Call entrance camera.")

    classifier = classifier or DeterministicVisitorIntentClassifier()
    intent_result = classifier.classify(transcript_text)

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
    if not store.is_entrance_camera(customer_id, camera_id):
        return {"triggered": False, "skipped_reason": "not_entrance_camera"}
    if not store.check_and_stamp_cooldown(customer_id=customer_id, camera_id=camera_id, cooldown_seconds=cooldown_seconds):
        return {"triggered": False, "skipped_reason": "cooldown"}

    camera = _authorized_camera(customer_id, camera_id)

    event_id = store.create_voice_call_event(
        customer_id=customer_id,
        site_id=camera["site_id"],
        camera_id=camera_id,
        trigger_detection_event_id=trigger_detection_event_id,
        thumbnail_s3_key=thumbnail_s3_key,
        trigger_source="detection",
        actor=actor,
    )

    greeting_text = store.resolve_greeting_text(customer_id=customer_id, camera_id=camera_id, site_id=camera["site_id"])
    try:
        provider = greeting_provider or aac_voice_call_greeting.get_provider()
        provider.speak(aac_voice_call_greeting.GreetingRequest(
            camera_id=camera_id, customer_id=customer_id, event_id=event_id, text=greeting_text,
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

    return {
        "triggered": True,
        "event_id": event_id,
        "greeting_text": greeting_text,
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
        camera = _authorized_camera(customer_id, event["camera_id"])
        store.mark_escalated(event_id=event_id, customer_id=customer_id, actor=actor)
        store.close_listening_window(event_id=event_id, customer_id=customer_id, actor=actor)
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
    greeting_text: str | None = None


class SiteGreetingPayload(BaseModel):
    greeting_text: str


class AnswerPayload(BaseModel):
    pass


class UnlockConfirmPayload(BaseModel):
    confirm_token: str


def register_aac_voice_call_routes(app: FastAPI, shell: Callable) -> None:
    @app.get("/api/customer/aac/voice-call/entrance-cameras")
    def list_entrance_cameras(request: Request) -> dict:
        identity = _customer_identity(request)
        return {"cameras": store.list_entrance_cameras(identity["customer_id"])}

    @app.post("/api/customer/aac/voice-call/entrance-cameras/{camera_id}")
    def set_entrance_camera(request: Request, camera_id: str, enabled: bool = True) -> dict:
        """customer_owner-only, matching subscription-portal's own
        established owner-vs-viewer convention for account-level
        configuration actions (a viewer can use the feature, not
        configure which cameras participate in it)."""
        identity = _customer_identity(request)
        if identity["role"] != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can configure entrance cameras.")
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
        event = store.get_voice_call_event(event_id=event_id, customer_id=identity["customer_id"])
        if not event:
            raise HTTPException(status_code=404, detail="AAC Voice Call event not found.")
        return event

    @app.post("/api/customer/aac/voice-call/events/{event_id}/answer")
    def answer_event(request: Request, event_id: str, payload: AnswerPayload = None) -> dict:
        identity = _customer_identity(request)
        event = store.get_voice_call_event(event_id=event_id, customer_id=identity["customer_id"])
        if not event:
            raise HTTPException(status_code=404, detail="AAC Voice Call event not found.")
        # partner_identity()'s signed session token carries email/role/
        # customer_id only -- never a partner_users.id -- so the real row
        # id (required: answered_by_user_id is a FOREIGN KEY into
        # partner_users) is resolved the same way main.py's own customer
        # notification-read routes already do.
        user = row("SELECT id FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?", (identity["email"], identity["customer_id"]))
        store.mark_answered(event_id=event_id, customer_id=identity["customer_id"], answered_by_user_id=user["id"] if user else None, actor=identity)
        return {"message": "Call answered.", "event_id": event_id}

    @app.post("/api/customer/aac/voice-call/events/{event_id}/end")
    def end_event_route(request: Request, event_id: str) -> dict:
        identity = _customer_identity(request)
        event = store.get_voice_call_event(event_id=event_id, customer_id=identity["customer_id"])
        if not event:
            raise HTTPException(status_code=404, detail="AAC Voice Call event not found.")
        store.end_call(event_id=event_id, customer_id=identity["customer_id"], actor=identity)
        return {"message": "Call ended.", "event_id": event_id}

    @app.post("/api/customer/aac/voice-call/events/{event_id}/dismiss")
    def dismiss_event(request: Request, event_id: str) -> dict:
        identity = _customer_identity(request)
        event = store.get_voice_call_event(event_id=event_id, customer_id=identity["customer_id"])
        if not event:
            raise HTTPException(status_code=404, detail="AAC Voice Call event not found.")
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
        return aac_voice_call_door.request_unlock(event_id=event_id, customer_id=identity["customer_id"], identity=identity)

    @app.post("/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm")
    def door_unlock_confirm(request: Request, event_id: str, payload: UnlockConfirmPayload) -> dict:
        """Step 2 -- the actual real unlock, gated on the exact
        confirmation token step 1 issued (single-use, short-lived,
        server-enforced) plus a fresh re-check of the approving user's
        live can_unlock permission. See aac_voice_call_door.py's
        confirm_unlock() for the complete authorization/audit trail."""
        identity = _customer_identity(request)
        return aac_voice_call_door.confirm_unlock(
            event_id=event_id, customer_id=identity["customer_id"], identity=identity, confirm_token=payload.confirm_token,
        )

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
        event = store.get_voice_call_event(event_id=event_id, customer_id=identity["customer_id"])
        if not event:
            raise HTTPException(status_code=404, detail="AAC Voice Call event not found.")
        camera = row("SELECT id,name FROM cameras WHERE id=? AND customer_id=?", (event["camera_id"], identity["customer_id"]))
        camera_name = (camera or {}).get("name") or "Entrance camera"

        import os

        talk_audio_enabled = os.environ.get("ANYAICAM_TALK_AUDIO_ENABLED", "false").strip().lower() == "true"
        audio_status = (
            "Two-way audio transport is enabled on this deployment."
            if talk_audio_enabled
            else "Two-way audio transport is not enabled on this deployment yet -- the microphone button below captures audio in your browser, but it is not confirmed to reach the camera's speaker."
        )

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

        content = f'''<header class="topbar"><div><p class="eyebrow">AAC Voice Call</p><h1>Visitor at {esc(camera_name)}</h1></div>
<a class="ghost-button" href="/alerts">Back to alerts</a></header>
<section class="panel">
  <p><strong>Recognized intent:</strong> {esc((event.get("intent") or "unknown").replace("_", " "))} ({round((event.get("intent_confidence") or 0) * 100)}% confidence)</p>
  {f'<p><strong>Transcript:</strong> {esc(event["transcript_text"])}</p>' if event.get("transcript_text") else ""}
  <p><strong>Call state:</strong> <span id="voice-call-state">{esc(event.get("state") or "triggered")}</span></p>
  <p class="health-detail">{esc(audio_status)}</p>
</section>
<section class="panel">
  <iframe src="/customer/cameras/{esc(event["camera_id"], quote=True)}/live" style="width:100%;min-height:480px;border:0;border-radius:8px" title="Live camera"></iframe>
</section>
<section class="panel dialog-actions">
  <button class="action-button" id="voice-call-answer" type="button">Answer</button>
  <button class="ghost-button" id="voice-call-end" type="button">End call</button>
  {'<button class="ghost-button" id="voice-call-unlock" type="button">Unlock Door</button>' if show_unlock_button else ''}
</section>
{'<p id="voice-call-unlock-status" class="health-detail"></p>' if show_unlock_button else ''}'''
        scripts = f'''<script>
const eventId={event_id!r};
document.getElementById('voice-call-answer').addEventListener('click', async () => {{
  const response = await fetch(`/api/customer/aac/voice-call/events/${{eventId}}/answer`, {{method: 'POST'}});
  const data = await response.json();
  if (!response.ok) {{ showToast(data.detail || 'Could not answer this call.'); return; }}
  document.getElementById('voice-call-state').textContent = 'answered';
  showToast('Call answered.');
}});
document.getElementById('voice-call-end').addEventListener('click', async () => {{
  const response = await fetch(`/api/customer/aac/voice-call/events/${{eventId}}/end`, {{method: 'POST'}});
  const data = await response.json();
  if (!response.ok) {{ showToast(data.detail || 'Could not end this call.'); return; }}
  document.getElementById('voice-call-state').textContent = 'ended';
  showToast('Call ended.');
}});
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
