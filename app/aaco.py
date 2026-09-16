"""Provider-independent, fail-closed AACO command boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Literal, Protocol

Operation = Literal["live_view", "playback", "event_search", "camera_status", "playback_navigation", "event_navigation"]


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


class DeterministicLanguageAdapter:
    """A small safe grammar; a future LLM may only emit ``AacoCommand``."""

    def parse(self, text: str, *, now: datetime, context: dict | None = None) -> AacoCommand | Clarification:
        value = " ".join(text.lower().split())
        if value in {"which cameras are offline?", "which cameras are offline"}:
            return AacoCommand("camera_status")
        if value in {"return to live", "return live", "go live"}:
            if not context or not context.get("camera_id"):
                return Clarification("Select an authorized camera before returning to live.")
            return AacoCommand("live_view", camera_id=context["camera_id"])
        if value in {"show previous event", "previous event"}:
            if not context or not context.get("camera_id") or not context.get("event_at"):
                return Clarification("Select an event before asking for the previous event.")
            return AacoCommand("event_navigation", camera_id=context["camera_id"], end=context["event_at"])
        if value.startswith("go back "):
            match = re.fullmatch(r"go back (\d+|ten|twenty|thirty) minutes?", value)
            if not match or not context or not context.get("camera_id") or not context.get("playback_at"):
                return Clarification("Select a camera and playback time before navigating.")
            minutes = {"ten": 10, "twenty": 20, "thirty": 30}.get(match.group(1))
            minutes = minutes if minutes is not None else int(match.group(1))
            at = context["playback_at"] - timedelta(minutes=minutes)
            return AacoCommand("playback_navigation", camera_id=context["camera_id"], start=at, end=at + timedelta(minutes=1), offset_minutes=minutes)
        match = re.fullmatch(r"show (person|vehicle|car) events from the last (\d+) hours?", value)
        if match:
            return AacoCommand("event_search", event_type=match.group(1), start=now - timedelta(hours=int(match.group(2))), end=now)
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
        return Clarification("I can show an authorized camera, playback, events, offline cameras, the previous event, or navigate current playback.")


def execute(command: AacoCommand, *, identity: dict, vms: VmsBoundary) -> object:
    """Only approved command types cross the VMS boundary."""
    if command.operation == "camera_status":
        return vms.camera_status(identity)
    if command.operation == "event_search":
        if not command.start or not command.end:
            raise ValueError("Event range required.")
        return vms.search_events(identity, event_type=command.event_type, camera_id=command.camera_id, start=command.start, end=command.end)
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
