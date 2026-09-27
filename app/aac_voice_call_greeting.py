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
            _provider = _default_provider()
        return _provider


def reset_provider() -> None:
    """Test-only: drops the singleton so the next get_provider() call
    builds a fresh one -- matching relay_control.reset_provider()."""
    global _provider
    with _provider_lock:
        _provider = None


# ---------------------------------------------------------------- spoken greeting on the edge (2026-09-27)
# The real provider: offline text-to-speech on the appliance (espeak-ng,
# installed in the release image -- no cloud round trip, so the greeting
# still works through an internet outage) played through the camera's
# own speaker over the same local ISAPI two-way-audio relay the
# homeowner's live Talk uses (talk_audio_relay._LocalIsapiTalkRelay).
#
# Safety, matching the Talk path: only cameras confirmed talk-capable
# (talk_down_supported == 1) are contacted; a camera in its login-failure
# cooldown is never contacted; a homeowner already talking through that
# camera always wins (the greeting is skipped, or cut short, never mixed
# in); one greeting at a time per camera. speak() synthesizes and checks
# all of that synchronously (a fraction of a second) and then plays the
# audio on a background thread, so the detection loop is never held for
# the seconds the greeting takes to say. `delivered` therefore means
# "handed to the camera speaker"; the camera's own open/playback result
# is logged under anyaicam.aac_voice_call_greeting.
import io
import logging
import os
import shutil
import subprocess
import wave

logger = logging.getLogger("anyaicam.aac_voice_call_greeting")

GREETING_VOICE = os.environ.get("ANYAICAM_GREETING_TTS_VOICE", "en-us")
GREETING_WORDS_PER_MINUTE = int(os.environ.get("ANYAICAM_GREETING_TTS_WPM", "150"))
GREETING_CHUNK_SECONDS = 0.1


def synthesize_speech(text: str, *, voice: str = GREETING_VOICE, words_per_minute: int = GREETING_WORDS_PER_MINUTE) -> tuple[bytes, int]:
    """Text -> (PCM16 mono bytes, sample rate) with espeak-ng. The text is
    passed as one argv item, never through a shell."""
    binary = shutil.which("espeak-ng")
    if not binary:
        raise RuntimeError("espeak-ng is not installed")
    wav = subprocess.run(
        [binary, "-v", voice, "-s", str(int(words_per_minute)), "--stdout", "--", text[:500]],
        capture_output=True, timeout=15, check=True,
    ).stdout
    with wave.open(io.BytesIO(wav)) as reader:
        if reader.getsampwidth() != 2 or reader.getnchannels() != 1:
            raise RuntimeError("unexpected espeak-ng audio format")
        return reader.readframes(reader.getnframes()), reader.getframerate()


def _talk_camera(camera_id: str) -> dict | None:
    from database_backend import connect

    with connect() as db:
        found = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return dict(found) if found else None


def _camera_in_customer_talk(camera_id: str) -> bool:
    import talk_audio_relay

    return any(item.get("camera_id") == camera_id for item in list(talk_audio_relay._active_relays.values()))


def _open_relay(camera: dict, sample_rate: int):
    import talk_audio_relay

    return talk_audio_relay._LocalIsapiTalkRelay(camera, sample_rate, session_id="aac-greeting")


def _auth_cooldown(camera_id: str) -> int:
    import talk_audio_relay

    return talk_audio_relay.camera_auth_cooldown_remaining(camera_id)


class IsapiTtsGreetingProvider:
    def __init__(self, *, synthesize=synthesize_speech, camera_lookup=_talk_camera,
                 talk_active=_camera_in_customer_talk, auth_cooldown=_auth_cooldown,
                 relay_factory=_open_relay, background=True, sleep=time.sleep) -> None:
        self._synthesize = synthesize
        self._camera_lookup = camera_lookup
        self._talk_active = talk_active
        self._auth_cooldown = auth_cooldown
        self._relay_factory = relay_factory
        self._background = background
        self._sleep = sleep
        self._lock = threading.Lock()
        self._speaking: set[str] = set()

    def capability(self) -> dict:
        return {
            "provider": "isapi_tts",
            "hardware_connected": True,
            "tts_engine": "espeak-ng",
            "note": "Offline edge text-to-speech played through the camera's own two-way audio speaker.",
        }

    def _skip(self, request: GreetingRequest, reason: str) -> GreetingResult:
        logger.info("aac_greeting camera_id=%s event_id=%s skipped=%s", request.camera_id, request.event_id, reason)
        return GreetingResult(camera_id=request.camera_id, delivered=False, suppressed_reason=reason)

    def speak(self, request: GreetingRequest) -> GreetingResult:
        camera = self._camera_lookup(request.camera_id)
        if not camera:
            return self._skip(request, "camera_not_found")
        if camera.get("talk_down_supported") != 1:
            return self._skip(request, "camera_not_talk_capable")
        if self._auth_cooldown(request.camera_id):
            return self._skip(request, "camera_auth_cooldown")
        if self._talk_active(request.camera_id):
            return self._skip(request, "customer_talk_active")
        with self._lock:
            if request.camera_id in self._speaking:
                return self._skip(request, "greeting_already_playing")
            self._speaking.add(request.camera_id)
        try:
            pcm, rate = self._synthesize(request.text)
        except Exception as error:
            with self._lock:
                self._speaking.discard(request.camera_id)
            logger.warning("aac_greeting camera_id=%s tts_failed=%s", request.camera_id, type(error).__name__)
            return self._skip(request, "tts_unavailable")
        if self._background:
            threading.Thread(target=self._play, args=(request, camera, pcm, rate),
                             name=f"aac-greeting-{request.camera_id}", daemon=True).start()
        else:
            self._play(request, camera, pcm, rate)
        return GreetingResult(camera_id=request.camera_id, delivered=True)

    def _play(self, request: GreetingRequest, camera: dict, pcm: bytes, rate: int) -> None:
        relay = None
        try:
            relay = self._relay_factory(camera, rate)
            if not relay.start():
                logger.warning("aac_greeting camera_id=%s event_id=%s camera_open_failed=%s",
                               request.camera_id, request.event_id, getattr(relay, "error_reason", None))
                return
            chunk = max(2, int(rate * GREETING_CHUNK_SECONDS) * 2)
            played = 0
            for offset in range(0, len(pcm), chunk):
                if self._talk_active(request.camera_id):
                    logger.info("aac_greeting camera_id=%s interrupted=customer_talk", request.camera_id)
                    break
                relay.send_pcm16(pcm[offset:offset + chunk])
                played += len(pcm[offset:offset + chunk])
                self._sleep(GREETING_CHUNK_SECONDS)  # real-time pacing; the relay queue drops stale audio
            self._sleep(0.5)  # let the camera finish playing the tail
            logger.info("aac_greeting camera_id=%s event_id=%s played_seconds=%.1f",
                        request.camera_id, request.event_id, played / 2.0 / rate)
        except Exception as error:
            logger.warning("aac_greeting camera_id=%s playback_failed=%s", request.camera_id, type(error).__name__)
        finally:
            if relay is not None:
                try:
                    relay.stop()
                except Exception:
                    pass
            with self._lock:
                self._speaking.discard(request.camera_id)


def _default_provider() -> GreetingAudioProvider:
    """ANYAICAM_GREETING_AUDIO_PROVIDER: "auto" (default) speaks for real
    when espeak-ng is installed (the release image), else the mock;
    "mock" and "isapi_tts" force one."""
    choice = os.environ.get("ANYAICAM_GREETING_AUDIO_PROVIDER", "auto").strip().lower()
    if choice == "isapi_tts" or (choice == "auto" and shutil.which("espeak-ng")):
        return IsapiTtsGreetingProvider()
    return MockGreetingAudioProvider()
