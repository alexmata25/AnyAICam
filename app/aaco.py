"""Provider-independent, fail-closed AACO command boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Literal, Protocol

Operation = Literal["live_view", "playback", "event_search", "camera_status", "playback_navigation", "event_navigation", "unlock_door"]


@dataclass(frozen=True)
class AacoCommand:
    operation: Operation
    camera_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    event_type: str | None = None
    offset_minutes: int | None = None


@dataclass(frozen=True)
class Clarification:
    message: str


class RecordingResolver(Protocol):
    """Storage-neutral resolver. AACO never creates media as a fallback."""
    def find_existing(self, *, camera_id: str, start: datetime, end: datetime) -> object: ...


class VmsBoundary(Protocol):
    def authorized_camera(self, identity: dict, camera_id: str) -> dict | None: ...
    def live_view(self, identity: dict, camera_id: str) -> object: ...
    def playback(self, identity: dict, camera_id: str, start: datetime, end: datetime) -> object: ...
    def search_events(self, identity: dict, *, event_type: str | None, camera_id: str | None, start: datetime, end: datetime) -> object: ...
    def previous_event(self, identity: dict, camera_id: str, before: datetime) -> object: ...
    def camera_status(self, identity: dict) -> object: ...
    def unlock_door(self, identity: dict, door_id: str) -> object: ...


def _camera_token(value: str) -> str:
    """Encode a display-name lookup as a constrained opaque command token."""
    return f"camera-name:{' '.join(value.lower().split())}"


def _requested_time(now: datetime, hour_text: str, minute_text: str, ampm: str | None) -> datetime | None:
    hour, minute = int(hour_text), int(minute_text)
    if minute > 59 or hour > 23 or (hour == 0 and ampm):
        return None
    if ampm:
        if hour > 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    return (now - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


# 2026-09-23: naturalness broadening. Every pattern below still resolves
# to exactly the same, unchanged Operation set _execute() already
# enforces -- this widens how many ways a customer can *ask* for an
# existing action, never what actions exist. The unlock_door boundary
# in particular is untouched below this point: broadening which
# phrases route to it changes nothing about its own safety, since the
# ambiguous-door Clarification and vms.unlock_door()'s own fail-closed
# authorization (see execute()'s comment) both still run identically
# regardless of which phrase got a customer there.
#
# _normalize() handles conversational *framing* only (a leading "AACO,"/
# "hey AACO", polite wrappers like "can you please"/"I'd like to", a
# trailing "?" or "please", and folding request-verb synonyms like
# "pull up"/"bring up"/"let me see" onto the grammar's own canonical
# "show") -- it never rewrites a camera/door *name* a customer actually
# supplies, only the request framing around it. Every pattern after
# normalization can therefore stay just as narrow and auditable as
# before; the flexibility lives in one shared, testable place instead
# of being duplicated (or, worse, inconsistently applied) into each
# individual rule below.
_LEADING_AACO_ADDRESS = re.compile(r"^(?:hey )?aaco[,]?\s+")
_SHOW_SYNONYM_WANT_PATTERNS = (
    re.compile(r"^i want to see\s+"),
    re.compile(r"^i'd like to see\s+"),
    re.compile(r"^i would like to see\s+"),
    re.compile(r"^let me see\s+"),
    re.compile(r"^can i see\s+"),
    re.compile(r"^could i see\s+"),
)
_LEADING_FILLER_PATTERNS = (
    re.compile(r"^(?:can|could|would) you (?:please )?"),
    re.compile(r"^please\s+"),
    re.compile(r"^i(?:'d| would) like to\s+"),
    re.compile(r"^i want to\s+"),
)
_SHOW_SYNONYM_PLAIN_PATTERNS = (
    re.compile(r"^show me\s+"),
    re.compile(r"^pull up\s+"),
    re.compile(r"^bring up\s+"),
    re.compile(r"^display\s+"),
    re.compile(r"^view\s+"),
)


def _normalize(text: str) -> str:
    value = " ".join(text.lower().split())
    value = re.sub(r"\?\s*$", "", value)
    value = re.sub(r"\s+please\s*$", "", value)
    value = _LEADING_AACO_ADDRESS.sub("", value, count=1)
    for pattern in _SHOW_SYNONYM_WANT_PATTERNS:
        new_value = pattern.sub("show ", value, count=1)
        if new_value != value:
            return new_value.strip()
    changed = True
    while changed:
        changed = False
        for pattern in _LEADING_FILLER_PATTERNS:
            new_value = pattern.sub("", value, count=1)
            if new_value != value:
                value, changed = new_value, True
    for pattern in _SHOW_SYNONYM_PLAIN_PATTERNS:
        new_value = pattern.sub("show ", value, count=1)
        if new_value != value:
            value = new_value
            break
    return value.strip()


_CAMERA_WORD = re.compile(r"\bcameras?\b")
_CAMERA_STATUS_SIGNAL = re.compile(r"\b(?:offline|down|not working|unavailable|disconnected|status)\b")
_RETURN_LIVE_PHRASES = {
    "return to live", "return live", "go live", "go back to live",
    "back to live", "switch to live", "resume live", "go to live view",
    "return to live view", "go back to live view",
}
_PREVIOUS_EVENT_PHRASES = {
    "show previous event", "previous event", "show the previous event",
    "go to the previous event", "go to previous event", "last event",
    "show the last event", "show last event", "go back to the previous event",
}
_LET_ME_IN_PHRASES = {"let me in", "can i come in"}


class DeterministicLanguageAdapter:
    """A small safe grammar; a future LLM may only emit ``AacoCommand``.

    Deterministic does not mean rigid: _normalize() absorbs how a
    customer actually phrases a request (politeness wrapping, a leading
    "AACO,", verb synonyms) before any pattern below ever sees the
    text, so many natural phrasings of the same request resolve to the
    exact same AacoCommand -- see test_aaco.py's
    test_many_natural_phrasings_resolve_to_the_same_action for the
    proof this is meant to satisfy."""

    def parse(self, text: str, *, now: datetime, context: dict | None = None) -> AacoCommand | Clarification:
        value = _normalize(text)
        if _CAMERA_WORD.search(value) and _CAMERA_STATUS_SIGNAL.search(value):
            return AacoCommand("camera_status")
        if value in _RETURN_LIVE_PHRASES:
            if not context or not context.get("camera_id"):
                return Clarification("Select an authorized camera before returning to live.")
            return AacoCommand("live_view", camera_id=context["camera_id"])
        if value in _PREVIOUS_EVENT_PHRASES:
            if not context or not context.get("camera_id") or not context.get("event_at"):
                return Clarification("Select an event before asking for the previous event.")
            return AacoCommand("event_navigation", camera_id=context["camera_id"], end=context["event_at"])
        match = re.fullmatch(r"(?:go back|rewind|back up) (\d+|ten|twenty|thirty) minutes?", value)
        if match:
            if not context or not context.get("camera_id") or not context.get("playback_at"):
                return Clarification("Select a camera and playback time before navigating.")
            minutes = {"ten": 10, "twenty": 20, "thirty": 30}.get(match.group(1))
            minutes = minutes if minutes is not None else int(match.group(1))
            at = context["playback_at"] - timedelta(minutes=minutes)
            return AacoCommand("playback_navigation", camera_id=context["camera_id"], start=at, end=at + timedelta(minutes=1), offset_minutes=minutes)
        # Canonical values match the real Investigate-page filter categories
        # (see main.py's _aaco_event_category()) -- "car" is intentionally
        # left mapping to itself here (normalized downstream at the VMS
        # boundary, unchanged) rather than duplicating that normalization.
        event_words = {
            "person": "person", "vehicle": "vehicle", "car": "car",
            "motion": "motion", "lpr": "lpr", "plate": "lpr", "license plate": "lpr",
            "people counting": "people_counting", "intrusion": "intrusion",
        }
        match = re.fullmatch(
            r"(?:show |any |were there any |display )?"
            r"(person|vehicle|car|motion|lpr|plate|license plate|people counting|intrusion) events? "
            r"(?:from |in )?(?:the )?(?:last|past) (\d+) hours?",
            value,
        )
        if match:
            return AacoCommand("event_search", event_type=event_words[match.group(1)], start=now - timedelta(hours=int(match.group(2))), end=now)
        # “yesterday at 3:15 PM” and “from 3:15 yesterday” are accepted.
        # An unqualified time is treated as 24-hour local time.
        match = re.fullmatch(r"show camera (\d+) (?:yesterday at|from) (\d{1,2}):(\d{2})(?: ?([ap]m))?(?: yesterday)?", value)
        if match:
            start = _requested_time(now, match.group(2), match.group(3), match.group(4))
            if not start:
                return Clarification("Use a valid playback time, for example 3:15 PM.")
            return AacoCommand("playback", camera_id=f"camera-{match.group(1)}", start=start, end=start + timedelta(minutes=5))
        match = re.fullmatch(r"show camera (\d+)", value)
        if match:
            return AacoCommand("live_view", camera_id=f"camera-{match.group(1)}")
        match = re.fullmatch(r"show (?:the )?([a-z0-9][a-z0-9 &'_-]{0,80})", value)
        if match:
            if match.group(1).strip() in {"camera", "cameras"}:
                return Clarification("Tell me which authorized camera you want to see.")
            return AacoCommand("live_view", camera_id=_camera_token(match.group(1)))
        door_match = re.fullmatch(r"(?:open|unlock) (?:the )?([a-z0-9][a-z0-9 &'_-]{0,80})", value)
        door_name = "door" if value in _LET_ME_IN_PHRASES else (door_match.group(1).strip() if door_match else None)
        if door_name is not None:
            if door_name in {"door", "doors"}:
                return Clarification("Tell me which authorized door you want to unlock.")
            return AacoCommand("unlock_door", camera_id=_camera_token(door_name))
        return Clarification("I can show an authorized camera, playback, events, offline cameras, the previous event, unlock an authorized door, or navigate current playback.")


def execute(command: AacoCommand, *, identity: dict, vms: VmsBoundary) -> object:
    """Only approved command types cross the VMS boundary."""
    if command.operation == "camera_status":
        return vms.camera_status(identity)
    if command.operation == "event_search":
        if not command.start or not command.end:
            raise ValueError("Event range required.")
        return vms.search_events(identity, event_type=command.event_type, camera_id=command.camera_id, start=command.start, end=command.end)
    if command.operation == "unlock_door":
        # Deliberately bypasses the generic authorized_camera() gate
        # below -- that gate only proves live/playback fleet membership,
        # never the stricter, door-specific can_unlock grant a physical
        # unlock requires. vms.unlock_door() performs its own complete,
        # fail-closed authorization (mirroring the existing manual
        # "Unlock Door" button's own check) and is the only path an
        # unlock_door command may ever take to the VMS boundary.
        if not command.camera_id:
            raise ValueError("Door required.")
        return vms.unlock_door(identity, command.camera_id)
    if not command.camera_id or not vms.authorized_camera(identity, command.camera_id):
        raise PermissionError("Camera is unavailable.")
    if command.operation == "live_view":
        return vms.live_view(identity, command.camera_id)
    if command.operation == "event_navigation":
        if not command.end:
            raise ValueError("Event context required.")
        return vms.previous_event(identity, command.camera_id, command.end)
    if command.operation in {"playback", "playback_navigation"}:
        if not command.start or not command.end:
            raise ValueError("Playback range required.")
        return vms.playback(identity, command.camera_id, command.start, command.end)
    raise ValueError("Unsupported AACO command.")
