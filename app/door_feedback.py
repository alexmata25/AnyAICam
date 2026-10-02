"""Audible Face Access feedback at the door (2026-10-01).

The person at the door should be able to tell three things apart:

- DENIED  -- AnyAiCam did not authorize them: one long, low tone.
- GRANTED -- authorized AND the door's access-control output accepted the
             unlock command: two short, high beeps.
- FAULT   -- authorized, but the unlock command did not run (no relay
             configured, relay/controller error, appliance-side refusal):
             three short descending tones, so it is never mistaken for
             either of the others.

What the hardware can actually prove (and what this module never claims):
the supported cameras play audio through their two-way-audio speaker (the
same ISAPI backchannel the visitor greeting already uses,
aac_voice_call_greeting.IsapiTtsGreetingProvider); none of them exposes a
separate hardware buzzer this software drives. On the door side, a relay
output reports only that the unlock pulse was sent -- no supported door has
a door-position or strike/maglock sensor wired back to AnyAiCam. So GRANTED
means "the configured access-control output was activated", never "the door
physically opened", and a simulated (no hardware) unlock is a FAULT.

Feedback is off for every door until its owner turns it on
(cameras.door_feedback_enabled), and plays only on a talk-capable camera.
Tone generation and the outcome decision are pure functions; playback is a
thin adapter over the existing camera speaker path.
"""
from __future__ import annotations

import logging
import math
import struct
import threading
import time

logger = logging.getLogger("anyaicam.door_feedback")

DENIED, GRANTED, FAULT = "denied", "granted", "fault"
SAMPLE_RATE = 8000
DENIED_COOLDOWN_SECONDS = 10.0

# (frequency Hz, duration s) segments; frequency 0 is silence.
PATTERNS = {
    DENIED: ((330, 0.9),),
    GRANTED: ((1320, 0.12), (0, 0.10), (1320, 0.12)),
    FAULT: ((990, 0.15), (0, 0.06), (660, 0.15), (0, 0.06), (440, 0.30)),
}

_AUTHORIZED = {"authorized"}
_RELAY_ACTIVATED = {"activated"}
# A door that was unlocked moments ago and ignored the repeat is not a fault
# the person needs to hear about.
_SILENT_RELAY_RESULTS = {"suppressed", "suppressed_cooldown", "cooldown", "duplicate"}


def outcome_for(authorization_result: str, relay_result: str) -> str | None:
    """The tone for one door attempt, or None for nothing to play."""
    if authorization_result in _AUTHORIZED:
        if relay_result in _RELAY_ACTIVATED:
            return GRANTED
        if relay_result in _SILENT_RELAY_RESULTS:
            return None
        return FAULT
    if authorization_result in {"not_authorized", "unknown_person", "denied", "denied_schedule", "denied_pin", "locked_out"}:
        return DENIED
    return None


def tone_pcm(outcome: str, *, rate: int = SAMPLE_RATE, volume: float = 0.6) -> bytes:
    """16-bit little-endian mono PCM for an outcome's pattern, with short
    fades so the camera speaker doesn't click."""
    if outcome not in PATTERNS:
        raise ValueError(f"unknown feedback outcome {outcome!r}")
    amplitude = int(32767 * max(0.0, min(volume, 1.0)))
    fade = int(rate * 0.008)
    samples: list[int] = []
    for frequency, seconds in PATTERNS[outcome]:
        count = int(rate * seconds)
        for index in range(count):
            if not frequency:
                samples.append(0)
                continue
            envelope = min(1.0, index / fade if fade else 1.0, (count - index) / fade if fade else 1.0)
            samples.append(int(amplitude * envelope * math.sin(2 * math.pi * frequency * index / rate)))
    return struct.pack(f"<{len(samples)}h", *samples)


_last_denied: dict[str, float] = {}
_lock = threading.Lock()


def _should_play(camera: dict, outcome: str, now: float) -> str | None:
    if not camera.get("door_feedback_enabled"):
        return "feedback_off"
    if camera.get("talk_down_supported") != 1:
        return "camera_has_no_speaker"
    if outcome == DENIED:
        with _lock:
            last = _last_denied.get(camera["id"], 0.0)
            if now - last < DENIED_COOLDOWN_SECONDS:
                return "denied_cooldown"
            _last_denied[camera["id"]] = now
    return None


def _default_provider(pcm: bytes, rate: int):
    from aac_voice_call_greeting import IsapiTtsGreetingProvider
    return IsapiTtsGreetingProvider(synthesize=lambda _text: (pcm, rate))


def play(camera: dict, outcome: str | None, *, provider_factory=_default_provider, clock=time.monotonic) -> dict:
    """Plays an outcome's tone on the door camera's speaker (in the
    background) when that door has feedback on. Never raises."""
    if not outcome:
        return {"played": False, "reason": "no_outcome"}
    try:
        reason = _should_play(camera, outcome, clock())
        if reason:
            return {"played": False, "reason": reason}
        from aac_voice_call_greeting import GreetingRequest
        pcm = tone_pcm(outcome)
        result = provider_factory(pcm, SAMPLE_RATE).speak(
            GreetingRequest(camera_id=camera["id"], customer_id=str(camera.get("customer_id") or ""),
                            event_id=f"door-feedback-{outcome}", text=f"door feedback: {outcome}", reason="door_feedback"))
        delivered = bool(getattr(result, "delivered", False))
        return {"played": delivered, "reason": None if delivered else getattr(result, "suppressed_reason", "not_played")}
    except Exception as error:
        logger.warning("door_feedback.play_failed camera_id=%s outcome=%s error=%s", camera.get("id"), outcome, type(error).__name__)
        return {"played": False, "reason": "error"}


def play_for_camera(camera_id: str, outcome: str | None) -> dict:
    """play() for a door known only by id (the recognition path's camera
    context carries no speaker/feedback columns). Never raises."""
    if not outcome:
        return {"played": False, "reason": "no_outcome"}
    try:
        # The plain connection: read only, and never the first thing to
        # initialize a database target (see household_users._lookup).
        from database_backend import connect
        with connect() as db:
            row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        return play(dict(row), outcome) if row else {"played": False, "reason": "camera_not_found"}
    except Exception as error:
        logger.warning("door_feedback.lookup_failed camera_id=%s error=%s", camera_id, type(error).__name__)
        return {"played": False, "reason": "error"}
