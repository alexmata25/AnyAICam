"""Spoken Front Door greeting (2026-09-27): edge TTS played through the
camera's two-way audio relay, with the Talk path's safety rules."""
import shutil

import pytest

import aac_voice_call_greeting as greeting


class FakeRelay:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []
        self.stopped = False
        self.error_reason = None if ok else "camera_auth_failed"

    def start(self):
        return self.ok

    def send_pcm16(self, pcm):
        self.sent.append(pcm)

    def stop(self):
        self.stopped = True


def _request(camera_id="cam-front"):
    return greeting.GreetingRequest(camera_id=camera_id, customer_id="cust", event_id="evt", text="Hello, how can I help you?")


def _provider(*, camera=None, talk_active=False, cooldown=0, relay=None, synthesize=None):
    relay = relay or FakeRelay()
    opened = []

    def factory(cam, rate):
        opened.append((cam["id"], rate))
        return relay

    provider = greeting.IsapiTtsGreetingProvider(
        synthesize=synthesize or (lambda text: (b"\x01\x00" * 16000, 16000)),  # one second at 16 kHz
        camera_lookup=lambda camera_id: camera if camera is not None else {"id": camera_id, "talk_down_supported": 1},
        talk_active=lambda camera_id: talk_active,
        auth_cooldown=lambda camera_id: cooldown,
        relay_factory=factory,
        background=False,
        sleep=lambda seconds: None,
    )
    return provider, relay, opened


def test_greeting_is_synthesized_and_played_through_the_camera_relay():
    provider, relay, opened = _provider()
    result = provider.speak(_request())
    assert result.delivered and result.suppressed_reason is None
    assert opened == [("cam-front", 16000)]
    assert b"".join(relay.sent) == b"\x01\x00" * 16000  # every sample, in order
    assert len(relay.sent) == 10  # paced in 100 ms chunks
    assert relay.stopped


@pytest.mark.parametrize("kwargs,reason", [
    ({"camera": {"id": "cam-front", "talk_down_supported": 0}}, "camera_not_talk_capable"),
    ({"camera": {"id": "cam-front", "talk_down_supported": None}}, "camera_not_talk_capable"),
    ({"cooldown": 42}, "camera_auth_cooldown"),
    ({"talk_active": True}, "customer_talk_active"),
])
def test_camera_is_never_contacted_when_unsafe(kwargs, reason):
    provider, relay, opened = _provider(**kwargs)
    result = provider.speak(_request())
    assert not result.delivered and result.suppressed_reason == reason
    assert opened == [] and relay.sent == []


def test_unknown_camera_is_skipped():
    provider = greeting.IsapiTtsGreetingProvider(camera_lookup=lambda camera_id: None, background=False)
    assert provider.speak(_request()).suppressed_reason == "camera_not_found"


def test_tts_failure_is_reported_honestly_and_releases_the_camera():
    def broken(text):
        raise RuntimeError("no engine")

    provider, relay, opened = _provider(synthesize=broken)
    assert provider.speak(_request()).suppressed_reason == "tts_unavailable"
    assert opened == []
    provider._synthesize = lambda text: (b"\x00\x00" * 800, 8000)
    assert provider.speak(_request()).delivered  # not stuck as "already playing"


def test_camera_open_failure_stops_cleanly_and_allows_the_next_greeting():
    provider, relay, opened = _provider(relay=FakeRelay(ok=False))
    provider.speak(_request())
    assert relay.sent == [] and relay.stopped
    assert provider._speaking == set()


def test_homeowner_talk_starting_mid_greeting_cuts_it_short():
    state = {"calls": 0}

    def talk_active(camera_id):
        state["calls"] += 1
        return state["calls"] > 4  # pre-check + 3 chunks, then the homeowner presses Talk

    provider, relay, opened = _provider()
    provider._talk_active = talk_active
    assert provider.speak(_request()).delivered
    assert len(relay.sent) == 3 and relay.stopped


def test_one_greeting_at_a_time_per_camera():
    provider, relay, opened = _provider()
    provider._speaking.add("cam-front")
    assert provider.speak(_request()).suppressed_reason == "greeting_already_playing"
    assert provider.speak(_request("cam-side")).delivered


def test_default_provider_selection(monkeypatch):
    monkeypatch.setenv("ANYAICAM_GREETING_AUDIO_PROVIDER", "mock")
    assert isinstance(greeting._default_provider(), greeting.MockGreetingAudioProvider)
    monkeypatch.setenv("ANYAICAM_GREETING_AUDIO_PROVIDER", "isapi_tts")
    assert isinstance(greeting._default_provider(), greeting.IsapiTtsGreetingProvider)
    monkeypatch.setenv("ANYAICAM_GREETING_AUDIO_PROVIDER", "auto")
    monkeypatch.setattr(greeting.shutil, "which", lambda name: None)
    assert isinstance(greeting._default_provider(), greeting.MockGreetingAudioProvider)
    monkeypatch.setattr(greeting.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    assert isinstance(greeting._default_provider(), greeting.IsapiTtsGreetingProvider)


@pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="espeak-ng not installed (it is in the release image)")
def test_real_espeak_produces_speech_audio():
    pcm, rate = greeting.synthesize_speech("Hello, how can I help you?")
    assert rate >= 8000 and len(pcm) / 2 / rate > 0.8
