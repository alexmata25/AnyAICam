"""AAC Voice Call visitor response (2026-09-28): capture the visitor's
answer from the camera's own recording buffer after the greeting,
transcribe it on the edge, forward it, and let the cloud attach it to the
one authoritative session (intent, escalation, never a door action)."""
import array
import math
from datetime import datetime, timedelta, timezone

import pytest

import aac_voice_call_listen as listen
from test_aac_voice_call_cloud_edge_flow import (  # noqa: F401  (fixtures)
    _cloud_rows, _enable_cloud_entrance, _event_payload, _post_event, _seed_cloud, _seed_edge_entrance_camera,
    aac_voice_call, aac_voice_call_greeting, analytics_sync, cloud_client, cloud_db, edge_db, edge_env, greeting_provider,
    override_target, relay_provider, sync_env,
)


def _tone(seconds=1.0, amplitude=6000, rate=16000):
    return array.array("h", (int(amplitude * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate)))).tobytes()


class FakeTranscriber:
    def __init__(self, text="hi i have a package for you", confidence=0.83):
        self.text, self.confidence, self.calls = text, confidence, []

    def transcribe(self, pcm, rate):
        self.calls.append((len(pcm), rate))
        return listen.Transcript(self.text, self.confidence, "fake") if self.text else None


# ---------------------------------------------------------------- capture and gating

def test_capture_cuts_the_listening_window_out_of_the_right_buffer_segments(tmp_path):
    for stamp in ("2026-09-28_03-13-00", "2026-09-28_03-13-30", "2026-09-28_03-14-00"):
        (tmp_path / f"buf5_{stamp}.mkv").write_bytes(b"x")
    calls = []

    def runner(cmd, **kwargs):
        with open(cmd[cmd.index("-i") + 1], encoding="utf-8") as listing_file:
            calls.append((cmd, listing_file.read()))
        return type("R", (), {"stdout": b"\x01\x00" * 16000 * 8})()

    start = datetime(2026, 9, 28, 3, 13, 25, tzinfo=timezone.utc)
    pcm = listen.capture_window(tmp_path, start, 8.0, runner=runner)
    assert pcm and len(pcm) == 16000 * 8 * 2
    cmd, listing = calls[0]
    assert "buf5_2026-09-28_03-13-00.mkv" in listing and "buf5_2026-09-28_03-13-30.mkv" in listing
    assert "03-14-00" not in listing  # window 03:13:25-03:13:33 needs only the first two
    assert cmd[cmd.index("-ss") + 1] == "25.00" and cmd[cmd.index("-t") + 1] == "8.00"
    assert "-vn" in cmd and cmd[cmd.index("-ar") + 1] == "16000"


def test_capture_fails_soft_without_a_buffer_or_audio(tmp_path):
    start = datetime(2026, 9, 28, 3, 13, 25, tzinfo=timezone.utc)
    assert listen.capture_window(tmp_path / "missing", start, 8.0, runner=lambda *a, **k: None) is None
    (tmp_path / "buf5_2026-09-28_03-13-00.mkv").write_bytes(b"x")
    assert listen.capture_window(tmp_path, start, 8.0, runner=lambda *a, **k: type("R", (), {"stdout": b""})()) is None


def test_speech_gate_skips_silence_and_passes_a_voice_level_signal():
    assert listen.speech_present(_tone(2.0))
    assert not listen.speech_present(b"\x00\x00" * 32000)
    assert not listen.speech_present(_tone(2.0, amplitude=40))  # steady low hum


def test_listen_once_transcribes_speech_and_skips_silence_or_missing_engine(tmp_path):
    fake = FakeTranscriber()
    start = datetime.now(timezone.utc)
    got = listen.listen_once(camera_number=5, window_start=start, buffer_root=tmp_path, transcriber=fake,
                             capture=lambda d, s, n: _tone(3.0))
    assert got.text == "hi i have a package for you" and fake.calls == [(96000, 16000)]
    assert listen.listen_once(camera_number=5, window_start=start, buffer_root=tmp_path, transcriber=fake,
                              capture=lambda d, s, n: b"\x00\x00" * 48000) is None
    assert listen.listen_once(camera_number=5, window_start=start, buffer_root=tmp_path, transcriber=fake,
                              capture=lambda d, s, n: None) is None
    assert len(fake.calls) == 1  # never called on silence or missing audio


def test_schedule_listen_waits_for_the_greeting_plus_window_then_forwards(tmp_path):
    clock = {"t": datetime(2026, 9, 28, 3, 13, 0, tzinfo=timezone.utc)}
    slept, windows, forwarded = [], [], []

    def capture(buffer_dir, start, seconds):
        windows.append((str(buffer_dir), start, seconds))
        return _tone(3.0)

    assert listen.schedule_listen(camera_number=5, greeting_seconds=4.0, buffer_root=tmp_path, on_transcript=forwarded.append,
                                  now=lambda: clock["t"], sleep=slept.append, background=False,
                                  transcriber=FakeTranscriber(), capture=capture)
    buffer_dir, start, seconds = windows[0]
    assert buffer_dir.endswith("camera5\\_event_buffer") or buffer_dir.endswith("camera5/_event_buffer")
    assert start == clock["t"] + timedelta(seconds=4.0 + listen.LISTEN_START_DELAY_SECONDS)
    assert slept == [pytest.approx(4.0 + listen.LISTEN_START_DELAY_SECONDS + listen.LISTEN_SECONDS + listen.BUFFER_FLUSH_SECONDS)]
    assert [t.text for t in forwarded] == ["hi i have a package for you"]


def test_listening_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(listen, "LISTEN_ENABLED", False)
    assert listen.schedule_listen(camera_number=5, greeting_seconds=1, buffer_root=tmp_path, on_transcript=print) is False


def test_without_the_engine_or_model_the_transcriber_is_unavailable(monkeypatch, tmp_path):
    assert listen.VoskTranscriber(str(tmp_path / "no-model")).available() is False


# ---------------------------------------------------------------- edge: greet, then forward the answer

@pytest.fixture()
def immediate_listen(monkeypatch):
    """schedule_listen that runs synchronously with a fixed transcript."""
    seen = {}

    def fake_schedule(*, camera_number, greeting_seconds, buffer_root, on_transcript, **kwargs):
        seen.update(camera_number=camera_number, greeting_seconds=greeting_seconds, buffer_root=buffer_root)
        on_transcript(listen.Transcript("hi i have a package for you", 0.8, "fake"))
        return True

    monkeypatch.setattr(listen, "schedule_listen", fake_schedule)
    return seen


class SpeakingProvider(aac_voice_call_greeting.MockGreetingAudioProvider):
    def speak(self, request):
        super().speak(request)
        return aac_voice_call_greeting.GreetingResult(camera_id=request.camera_id, delivered=True, duration_seconds=3.4)


def test_edge_forwards_the_visitor_answer_linked_to_its_trigger(edge_db, edge_env, monkeypatch, immediate_listen, tmp_path):
    _seed_edge_entrance_camera(edge_db, edge_env, monkeypatch)
    forwarded = []
    with override_target(sqlite_path=str(edge_db)):
        result = aac_voice_call.handle_edge_person_detected(
            customer_id="cust-1", camera_id="cam-1", camera_number=1, forward_event=forwarded.append,
            greeting_provider=SpeakingProvider(), buffer_root=tmp_path,
        )
    assert result["listening"] is True
    assert immediate_listen == {"camera_number": 1, "greeting_seconds": 3.4, "buffer_root": tmp_path}
    trigger, answer = forwarded
    assert trigger["event_type"] == "aac_voice_call"
    assert answer["event_type"] == "aac_voice_call_utterance" and answer["voice_call_local_event_id"] == trigger["id"]
    assert answer["transcript_text"] == "hi i have a package for you" and answer["camera"] == 1


def test_edge_does_not_listen_when_the_greeting_did_not_play(edge_db, edge_env, monkeypatch, immediate_listen, tmp_path):
    _seed_edge_entrance_camera(edge_db, edge_env, monkeypatch)

    class Silent(aac_voice_call_greeting.MockGreetingAudioProvider):
        def speak(self, request):
            return aac_voice_call_greeting.GreetingResult(camera_id=request.camera_id, delivered=False, suppressed_reason="camera_not_talk_capable")

    forwarded = []
    with override_target(sqlite_path=str(edge_db)):
        result = aac_voice_call.handle_edge_person_detected(
            customer_id="cust-1", camera_id="cam-1", camera_number=1, forward_event=forwarded.append,
            greeting_provider=Silent(), buffer_root=tmp_path,
        )
    assert result["listening"] is False and [e["event_type"] for e in forwarded] == ["aac_voice_call"]


def test_sync_payload_carries_the_answer_and_never_sends_a_generic_notification(monkeypatch):
    event = {"id": "aacvc-1-reply", "camera": 1, "event_type": "aac_voice_call_utterance", "timestamp": "2026-09-28T03:14:00",
             "confidence": 0.8, "voice_call_local_event_id": "aacvc-1", "transcript_text": "package for you", "stt_engine": "vosk"}
    payload = analytics_sync._build_payload(event)
    assert payload["detections"] == [{"voice_call_local_event_id": "aacvc-1", "transcript_text": "package for you", "stt_engine": "vosk"}]
    sent = []
    monkeypatch.setattr(analytics_sync, "_control_plane_post", lambda path, body: sent.append(path))
    analytics_sync._forward_notification(event, "cam-1")
    assert sent == []


# ---------------------------------------------------------------- cloud: attach to the one session

def _answer(local_event_id="aacvc-1", text="hi i have a package for you", reply_id="aacvc-1-reply"):
    return {"local_event_id": reply_id, "event_type": "aac_voice_call_utterance", "confidence": 0.8, "object_count": 1,
            "event_timestamp": datetime.now().isoformat(),
            "detections": [{"voice_call_local_event_id": local_event_id, "transcript_text": text, "stt_engine": "vosk"}]}


def test_cloud_attaches_the_answer_to_the_session_and_classifies_intent(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    assert _post_event(cloud_client, cloud_db, _event_payload()).status_code == 200
    response = _post_event(cloud_client, cloud_db, _answer())
    assert response.status_code == 200 and response.json()["voice_call"] == "accepted"
    session = _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")[0]
    assert session["transcript_text"] == "hi i have a package for you" and session["intent"] == "delivery"
    notifications = _cloud_rows(cloud_db, "SELECT event_type FROM notifications")
    assert notifications == [{"event_type": "aac_voice_call"}]  # no extra notification for a normal delivery


def test_an_urgent_answer_escalates_through_the_existing_path(cloud_client, cloud_db, relay_provider):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    _post_event(cloud_client, cloud_db, _event_payload())
    _post_event(cloud_client, cloud_db, _answer(text="help please call nine one one there is an emergency"))
    session = _cloud_rows(cloud_db, "SELECT * FROM aac_voice_call_events")[0]
    assert session["escalated_at"]
    assert len(_cloud_rows(cloud_db, "SELECT id FROM notifications")) == 2
    assert relay_provider.calls == []  # nothing a visitor says ever reaches door/relay code


def test_a_replayed_answer_is_recorded_once(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    _post_event(cloud_client, cloud_db, _event_payload())
    answer = _answer()
    _post_event(cloud_client, cloud_db, answer)
    assert _post_event(cloud_client, cloud_db, answer).json()["status"] == "duplicate"  # identical retry
    assert _cloud_rows(cloud_db, "SELECT utterance_count FROM aac_voice_call_events")[0]["utterance_count"] == 1


def test_an_answer_for_an_unknown_or_foreign_trigger_is_ignored(cloud_client, cloud_db):
    _seed_cloud(cloud_db)
    _enable_cloud_entrance(cloud_db)
    _post_event(cloud_client, cloud_db, _event_payload())
    response = _post_event(cloud_client, cloud_db, _answer(local_event_id="aacvc-does-not-exist", reply_id="r2"))
    assert response.status_code == 200 and response.json()["voice_call"] == "skipped"
    assert _cloud_rows(cloud_db, "SELECT transcript_text FROM aac_voice_call_events")[0]["transcript_text"] is None
