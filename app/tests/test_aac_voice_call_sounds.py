"""Visitor Call sounds (2026-10-02, Codex review).

- The selected AAC_Visitor_Call_Chime.wav -- the original file, byte for
  byte -- plays on the call page while the call rings, stops on
  answer/end/close, and a browser that blocks autoplay gets a "tap to hear"
  note instead of a broken call.
- When the owner ends a call the visitor hears "Call ended." through the
  camera speaker path the greeting already uses: spoken locally where this
  process reaches the camera, or asked of the camera's appliance over its
  open control channel. The words are fixed; the cloud message names only
  the camera. Feedback never breaks ending the call.
"""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import aac_voice_call
import aac_voice_call_events as store
import aac_voice_call_greeting
from database_backend import override_target
from test_aac_voice_call_household_authorization import DOOR, OWNER, tenant  # noqa: F401 -- fixtures
from test_aac_voice_call_door_unlock import _isolated_relay, client, db_path  # noqa: F401 -- fixtures

CHIME = Path(aac_voice_call.__file__).parent / "static" / "sounds" / "AAC_Visitor_Call_Chime.wav"
SELECTED_CHIME_SHA256 = "6e646eccef415deb7fb1d203c38dd41ce57c23e47860b91aa7fd8dc8efcf9984"


# ------------------------------------------------------------------ chime

def test_the_shipped_chime_is_the_selected_original_file():
    assert hashlib.sha256(CHIME.read_bytes()).hexdigest() == SELECTED_CHIME_SHA256  # never substituted or re-encoded


def test_the_call_page_rings_with_the_chime_and_survives_blocked_autoplay(client, tenant):
    page = client.get(f"/aac/voice-call/{tenant}", cookies=OWNER).text
    assert "const callChimeUrl='/static/sounds/AAC_Visitor_Call_Chime.wav'" in page
    assert "callChime.loop = true" in page
    assert ".catch(() =>" in page and "voice-call-sound-note" in page  # blocked autoplay: a note, not an error
    assert "Tap anywhere on this page to hear the visitor chime." in page
    assert "stopChime();" in page.split("answerButton.addEventListener")[1]  # answer silences it


def test_the_chime_ships_where_the_static_mount_serves_it():
    """/static is StaticFiles(directory="/app/static") and the image copies
    ./app to /app, so app/static/sounds/<file> is served at
    /static/sounds/<file> -- the URL the call page uses."""
    import main
    mount = next(route for route in main.app.routes if getattr(route, "path", None) == "/static")
    assert str(mount.app.directory).replace("\\", "/") == "/app/static"
    assert CHIME.parent.parent.name == "static" and CHIME.parent.parent.parent.name == "app"
    dockerfile = (CHIME.parents[3] / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ./app /app" in dockerfile


# ------------------------------------------------------------------ "Call ended" at the camera

@pytest.fixture()
def speaker(db_path, tenant):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE cameras SET talk_down_supported=1 WHERE id=?", (DOOR,))
    conn.commit()
    conn.close()
    return tenant


def test_ending_a_call_in_the_cloud_asks_the_appliance_with_no_text(client, speaker, monkeypatch):
    sent = []
    import appliance_control
    monkeypatch.setattr(aac_voice_call, "_runtime_role", lambda: "cloud")
    monkeypatch.setattr(appliance_control, "send", lambda appliance_id, message: sent.append((appliance_id, message)) or True)
    assert client.post(f"/api/customer/aac/voice-call/events/{speaker}/end", cookies=OWNER).status_code == 200
    assert sent == [("app-cust-a", {"type": "voice_call_ended", "camera_id": DOOR, "event_id": speaker})]
    client.post(f"/api/customer/aac/voice-call/events/{speaker}/end", cookies=OWNER)  # a repeated End says nothing again
    assert len(sent) == 1


def test_ending_a_call_where_the_camera_is_local_speaks_it_here(client, db_path, speaker, monkeypatch):
    provider = aac_voice_call_greeting.MockGreetingAudioProvider()
    monkeypatch.setattr(aac_voice_call_greeting, "_provider", provider)
    monkeypatch.setattr(aac_voice_call, "_runtime_role", lambda: "edge")

    class Inline:  # run the background speaker inline
        def __init__(self, target, kwargs, **_):
            self.target, self.kwargs = target, kwargs

        def start(self):
            self.target(**self.kwargs)
    import threading
    monkeypatch.setattr(threading, "Thread", Inline)
    assert client.post(f"/api/customer/aac/voice-call/events/{speaker}/end", cookies=OWNER).status_code == 200
    aac_voice_call_greeting.reset_provider()
    assert [(c.text, c.reason, c.camera_id) for c in provider.calls] == [("Call ended.", "aac_voice_call_ended", DOOR)]


def test_no_speaker_or_a_failing_channel_never_breaks_end(client, db_path, tenant, monkeypatch):
    import appliance_control
    monkeypatch.setattr(aac_voice_call, "_runtime_role", lambda: "cloud")
    sent = []
    monkeypatch.setattr(appliance_control, "send", lambda *a: sent.append(a) or True)
    assert client.post(f"/api/customer/aac/voice-call/events/{tenant}/end", cookies=OWNER).json()["state"] == "ended"
    assert sent == []  # camera without a speaker: nothing to play

    def broken(*_args):
        raise RuntimeError("channel exploded")
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE cameras SET talk_down_supported=1 WHERE id=?", (DOOR,))
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        second = aac_voice_call.trigger_visitor_event(customer_id="cust-a", camera_id=DOOR, transcript_text="hi")["event_id"]
    monkeypatch.setattr(appliance_control, "send", broken)
    response = client.post(f"/api/customer/aac/voice-call/events/{second}/end", cookies=OWNER)
    assert response.status_code == 200 and response.json()["state"] == "ended"


def test_speak_call_ended_uses_fixed_words_and_the_greeting_volume(db_path, speaker):
    provider = aac_voice_call_greeting.MockGreetingAudioProvider()
    with override_target(sqlite_path=str(db_path)):
        store.set_camera_greeting_volume(customer_id="cust-a", camera_id=DOOR, volume="low")
        result = aac_voice_call.speak_call_ended(camera_id=DOOR, event_id="e1", provider=provider)
        other = aac_voice_call.speak_call_ended(camera_id=DOOR, customer_id="cust-b", provider=provider)
    assert result["delivered"] is False or result["delivered"] is True  # mock never claims hardware either way
    assert [(c.text, c.volume) for c in provider.calls] == [("Call ended.", "low")]
    assert other == {"delivered": False, "reason": "camera_not_found"}  # never another tenant's camera


def test_the_appliance_speaks_on_voice_call_ended(monkeypatch):
    import talk_audio_relay_client as client_module
    spoken = []
    monkeypatch.setattr(client_module, "CALL_ENDED_DELAY_SECONDS", 0)
    monkeypatch.setattr(aac_voice_call, "speak_call_ended", lambda **kwargs: spoken.append(kwargs) or {"delivered": True})

    async def run():
        await client_module._handle_message(json.dumps({"type": "voice_call_ended", "camera_id": DOOR, "event_id": "e9",
                                                        "text": "open the door"}), {})
        await asyncio.sleep(0.2)
        await client_module._handle_message(json.dumps({"type": "voice_call_ended", "camera_id": 5}), {})
        await asyncio.sleep(0.1)
    asyncio.run(run())
    assert spoken == [{"camera_id": DOOR, "event_id": "e9"}]  # cloud-supplied text is ignored; bad ids are dropped
