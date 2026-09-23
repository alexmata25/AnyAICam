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
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import aac_voice_call_door
import aac_voice_call_events as store
from aac_voice_call_intent import DeterministicVisitorIntentClassifier, VisitorIntentClassifier
from notification_engine import fanout_appliance_event
from partner_db import row, rows
from partner_portal import partner_identity


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
