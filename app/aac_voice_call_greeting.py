"""AAC Voice Call -- proactive greeting audio dispatch (2026-09-23).

Mirrors relay_control.py's own RelayProvider/MockRelayProvider shape
deliberately -- the exact same "the interface is ready, there is no
hardware-backed implementation yet" idiom this codebase already uses
for the one other place a piece of software logic ends in "make a real
sound/action happen at the camera" (relay_control.py's own docstring:
"channel selection, pulse duration, debounce, cooldown, and a hard
dry-run/test mode... modeled here as a plain, hardware-free interface
... plus one concrete implementation that a later hardware milestone
can sit behind without any caller changing").

Text-to-speech synthesis and the appliance-side ONVIF backchannel path
that would carry synthesized audio to a camera's physical speaker are
NOT implemented anywhere in this codebase (talk_audio_relay.py's own
real, working transport is driven by a browser's live microphone via a
customer_talk_sessions WebSocket -- a human-initiated session, not a
server-initiated one; there is no TTS engine dependency anywhere in
requirements.txt, and the deployment target is the Linux appliance,
not this Windows dev machine, so bolting on a Windows-only voice
engine here would not even be the right fix). GreetingAudioProvider is
the seam a real implementation -- TTS synthesis feeding
talk_audio_relay.py's existing session/relay machinery -- plugs into
later, exactly like RelayProvider is the seam a real GPIO/serial relay
board plugs into. Until that real work happens, MockGreetingAudioProvider
is the only provider anywhere in this codebase, matching
MockRelayProvider's own precedent exactly: it records every call (so
higher-level orchestration -- debounce, notification, listening-window
open -- can be fully exercised and tested for real) and NEVER claims
physical delivery. capability()['hardware_connected'] is always False
here; speak_greeting()'s own caller (aac_voice_call.py) never claims a
visitor actually heard anything spoken out loud -- only that the
greeting was dispatched through this real interface.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class GreetingRequest:
    """One request to speak a greeting at a camera. Immutable, matching
    RelayRequest's own precedent -- a provider or test can inspect
    exactly what was asked for without it changing under it."""

    camera_id: str
    customer_id: str
    event_id: str
    text: str
    reason: str = "aac_voice_call_greeting"

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise ValueError("Greeting text must not be empty.")


@dataclass(frozen=True)
class GreetingResult:
    """What actually happened for one GreetingRequest. `delivered` is
    True only when a provider believes it actually (or, for the mock,
    notionally) dispatched the greeting -- never conflated with "a
    visitor physically heard it," which no provider in this codebase
    can honestly claim yet (see this module's own docstring)."""

    camera_id: str
    delivered: bool
    suppressed_reason: str | None = None
    at: float = field(default_factory=time.monotonic)


class GreetingAudioProvider(Protocol):
    """The one interface aac_voice_call.py depends on for speaking a
    greeting. A future TTS + talk_audio_relay.py-backed implementation
    satisfies this same Protocol; no caller depends on anything beyond
    it, exactly matching VisitorIntentClassifier's own established
    drop-in-replacement discipline in aac_voice_call_intent.py."""

    def speak(self, request: GreetingRequest) -> GreetingResult: ...

    def capability(self) -> dict: ...


class MockGreetingAudioProvider:
    """Development/test provider: records every call it receives (for
    assertions) and NEVER touches any real audio hardware or TTS engine
    -- there is neither to touch yet. Thread-safe, matching
    MockRelayProvider's own precedent, since a real appliance could in
    principle greet more than one entrance camera concurrently."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[GreetingRequest] = []
        self.results: list[GreetingResult] = []

    def capability(self) -> dict:
        return {
            "provider": "mock",
            "hardware_connected": False,
            "tts_engine": None,
            "note": "Development/test provider only. No real text-to-speech synthesis or camera-speaker audio is produced by this provider.",
        }

    def reset(self) -> None:
        """Test-only: clears recorded calls."""
        with self._lock:
            self.calls.clear()
            self.results.clear()

    def speak(self, request: GreetingRequest) -> GreetingResult:
        with self._lock:
            self.calls.append(request)
            result = GreetingResult(camera_id=request.camera_id, delivered=True)
            self.results.append(result)
            return result


_provider: GreetingAudioProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> GreetingAudioProvider:
    """Same lazy-singleton pattern as relay_control.get_provider() --
    so cooldown/call-history state (tracked per-instance on
    MockGreetingAudioProvider, for test assertions) is stable across
    calls within one process, and a test can still swap it out via
    monkeypatch.setattr(aac_voice_call_greeting, "_provider", ...)."""
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = MockGreetingAudioProvider()
        return _provider


def reset_provider() -> None:
    """Test-only: drops the singleton so the next get_provider() call
    builds a fresh one -- matching relay_control.reset_provider()."""
    global _provider
    with _provider_lock:
        _provider = None
