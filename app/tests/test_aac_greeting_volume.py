"""Greeting-only volume for AAC Voice Call (2026-09-29).

The greeting audio is levelled inside the greeting provider, before the
shared camera relay: normalised to a per-level speech loudness with a peak
limit, default "medium" (~9 dB quieter than raw eSpeak). The camera speaker
volume, homeowner Talk, the microphone and recordings are never touched.
The per-entrance-camera level is owner-configured in the cloud and synced
to the edge with the rest of the Voice Call configuration."""
import array
import math

import pytest

import aac_voice_call_events as store
import aac_voice_call_greeting as greeting
from test_aac_voice_call_cloud_edge_flow import (  # noqa: F401  (fixtures)
    EDGE_CAMERA, _auth_headers, _seed_cloud, _sync_edge, cloud_client, cloud_db, edge_db, edge_env,
)
from database_backend import override_target


def _tone(amplitude, seconds=1.0, rate=8000):
    return array.array("h", (int(amplitude * math.sin(2 * math.pi * 440 * n / rate)) for n in range(int(seconds * rate)))).tobytes()


def _speech_dbfs(pcm, reference=None):
    """Speech RMS over the samples the INPUT counts as speech (the gate is
    applied to the reference, never re-applied after scaling)."""
    ref = array.array("h", reference if reference is not None else pcm)
    values = array.array("h", pcm)
    samples = [values[i] for i, v in enumerate(ref) if abs(v) > greeting._SPEECH_GATE]
    return 20 * math.log10(math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768)


def _peak_dbfs(pcm):
    return 20 * math.log10(max(abs(v) for v in array.array("h", pcm)) / 32768)


@pytest.mark.parametrize("volume", ["low", "medium", "high"])
def test_levelling_hits_each_target_and_respects_the_peak_limit(volume):
    loud = _tone(26000)  # louder than eSpeak's raw output
    out = greeting.level_greeting_pcm(loud, volume)
    assert abs(_speech_dbfs(out, loud) - greeting.GREETING_VOLUME_LEVELS[volume]) < 0.3
    assert _peak_dbfs(out) <= greeting.GREETING_PEAK_LIMIT_DBFS + 0.1


def test_levels_are_ordered_and_medium_is_about_9_db_below_raw_espeak():
    raw = _tone(5000)  # ~ -19.3 dBFS speech RMS, what the Ryzen's eSpeak produced
    levels = {v: _speech_dbfs(greeting.level_greeting_pcm(raw, v), raw) for v in ("low", "medium", "high")}
    assert levels["low"] < levels["medium"] < levels["high"]
    assert 7.5 <= _speech_dbfs(raw) - levels["medium"] <= 10.5


def test_peak_limit_wins_over_the_loudness_target_for_spiky_audio():
    spiky = array.array("h", [0] * 7990 + [30000] * 10 + [2000] * 8000).tobytes()
    out = greeting.level_greeting_pcm(spiky, "high")
    assert _peak_dbfs(out) <= greeting.GREETING_PEAK_LIMIT_DBFS + 0.1


def test_silence_empty_and_unknown_levels_are_safe():
    silence = bytes(1600)
    assert greeting.level_greeting_pcm(silence, "high") == silence
    assert greeting.level_greeting_pcm(b"", "low") == b""
    raw = _tone(4200)
    assert greeting.level_greeting_pcm(raw, "bogus") == greeting.level_greeting_pcm(raw, "medium")


def test_provider_levels_only_the_greeting_audio_it_plays():
    raw = _tone(4200, seconds=0.3)
    sent = []

    class Relay:
        error_reason = None
        def start(self): return True
        def send_pcm16(self, chunk): sent.append(chunk)
        def stop(self): pass

    provider = greeting.IsapiTtsGreetingProvider(
        synthesize=lambda text: (raw, 8000), camera_lookup=lambda cid: {"id": cid, "talk_down_supported": 1},
        talk_active=lambda cid: False, auth_cooldown=lambda cid: 0, relay_factory=lambda camera, rate: Relay(),
        background=False, sleep=lambda s: None,
    )
    result = provider.speak(greeting.GreetingRequest(camera_id="cam-1", customer_id="cust-1", event_id="e1", text="Hi", volume="low"))
    assert result.delivered
    played = b"".join(sent)
    assert played == greeting.level_greeting_pcm(raw, "low") and played != raw


def test_greeting_request_defaults_to_medium():
    assert greeting.GreetingRequest(camera_id="c", customer_id="u", event_id="e", text="Hi").volume == "medium"


def test_store_validates_and_resolves_volume(cloud_db):
    _seed_cloud(cloud_db)
    with override_target(sqlite_path=str(cloud_db)):
        store.set_entrance_camera(customer_id="cust-1", camera_id="cam-1", enabled=True)
        assert store.resolve_greeting_volume(customer_id="cust-1", camera_id="cam-1") == "medium"  # NULL = medium
        store.set_camera_greeting_volume(customer_id="cust-1", camera_id="cam-1", volume="HIGH")
        assert store.resolve_greeting_volume(customer_id="cust-1", camera_id="cam-1") == "high"
        with pytest.raises(ValueError):
            store.set_camera_greeting_volume(customer_id="cust-1", camera_id="cam-1", volume="max")
        # another tenant's camera id never changes this tenant's row
        store.set_camera_greeting_volume(customer_id="cust-other", camera_id="cam-1", volume="low")
        assert store.resolve_greeting_volume(customer_id="cust-1", camera_id="cam-1") == "high"


def test_configuration_carries_the_volume_to_the_edge(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    with override_target(sqlite_path=str(cloud_db)):
        store.set_entrance_camera(customer_id="cust-1", camera_id="cam-1", enabled=True)
        store.set_camera_greeting_volume(customer_id="cust-1", camera_id="cam-1", volume="low")
        response = cloud_client.get("/api/appliance/configuration", headers=_auth_headers())
    assert response.json()["aac_voice_call"]["entrance_cameras"][0]["greeting_volume"] == "low"


def test_edge_mirrors_valid_volume_and_drops_invalid(edge_db, edge_env, monkeypatch):
    _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA], "aac_voice_call": {
        "entrance_cameras": [{"camera_id": "cam-1", "greeting_text": None, "greeting_volume": "high"}], "site_greetings": []}})
    with override_target(sqlite_path=str(edge_db)):
        assert store.resolve_greeting_volume(customer_id="cust-1", camera_id="cam-1") == "high"
    _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA], "aac_voice_call": {
        "entrance_cameras": [{"camera_id": "cam-1", "greeting_volume": "11"}], "site_greetings": []}})
    with override_target(sqlite_path=str(edge_db)):
        assert store.resolve_greeting_volume(customer_id="cust-1", camera_id="cam-1") == "medium"


def test_older_cloud_without_the_field_means_medium(edge_db, edge_env, monkeypatch):
    _sync_edge(edge_db, monkeypatch, {"cameras": [EDGE_CAMERA], "aac_voice_call": {
        "entrance_cameras": [{"camera_id": "cam-1", "greeting_text": "Hi"}], "site_greetings": []}})
    with override_target(sqlite_path=str(edge_db)):
        assert store.resolve_greeting_volume(customer_id="cust-1", camera_id="cam-1") == "medium"
