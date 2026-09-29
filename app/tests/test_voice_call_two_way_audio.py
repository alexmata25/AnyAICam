"""Two-way audio as a universal, capability-driven product feature
(2026-09-28, from the Front Door physical test):

- live view requests the camera's audio as well as video over P2P;
- Answer turns live audio on and asks for the microphone once;
- on phones Talk is tap-to-start / tap-to-stop;
- the cloud refuses to start Talk, with a clear reason, when the camera's
  appliance talk channel isn't connected;
- the appliance talk channel is on by default (no hidden variable);
- the ordinary Person email is skipped when the same visit produced an
  AAC Voice Call email.
Nothing here is specific to any camera name or address."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from database_backend import override_target
from partner_db import initialize_database

with override_target(sqlite_path="/tmp/test_voice_call_two_way_audio_import.db"):
    import notification_engine
    from partner_db import connection

import live_view_page
import notification_email
import talk_audio_relay
import talk_sessions


# ---------------------------------------------------------------- live audio + Talk UI

def test_p2p_offer_requests_audio_and_bundles_one_transport():
    js = live_view_page._P2P_JS
    assert "pc.addTransceiver('audio', {direction: 'recvonly'})" in js
    assert "pc.addTransceiver('video', {direction: 'recvonly'})" in js
    assert "bundlePolicy: 'max-bundle'" in js
    # every track (video and audio) is played from one stream
    assert "const stream = new MediaStream();" in js and "stream.addTrack(event.track)" in js


def test_talk_is_tap_to_toggle_on_phones_and_hold_on_desktop():
    js = live_view_page._TALK_MIC_JS
    assert "matchMedia('(pointer: coarse)')" in js
    assert "if (held) stop(); else start();" in js
    assert "button.addEventListener('pointerup', (event) => stop(event));" in js  # desktop press-and-hold kept
    assert "window.aacCallMicStream" in js and "callMic.clone()" in js
    assert "Open this page in Safari or Chrome and allow the microphone" in js


def _panel(talk_supported):
    camera = {"id": "cam-x", "name": "Any camera", "camera_number": 7, "talk_down_supported": talk_supported}
    return live_view_page.camera_live_panel(camera, {"role": "customer_owner"})


@pytest.mark.parametrize("supported", [0, None])
def test_no_talk_control_without_detected_talkback(supported):
    html, scripts = _panel(supported)
    assert "talk-mic-cam-x" not in html and 'id="live-view-video"' in html
    assert "if (talkButton) wireTalkMic(" in scripts


def test_talk_control_for_talkback_cameras():
    html, _ = _panel(1)
    assert 'id="talk-mic-cam-x"' in html and "disabled" not in html.split('id="talk-mic-cam-x"')[1].split(">")[0]


def test_voice_call_answer_unmutes_and_requests_the_microphone():
    import inspect
    import aac_voice_call
    source = inspect.getsource(aac_voice_call)
    answer = source[source.index("document.getElementById('voice-call-answer')"):source.index("function releaseCallMicrophone")]
    assert answer.index("video.muted = false") < answer.index("await fetch(")  # unmuted inside the tap, before any await
    assert "window.aacCallMicStream = await navigator.mediaDevices.getUserMedia" in answer
    assert "releaseCallMicrophone();" in source


# ---------------------------------------------------------------- talk channel offline

def test_cloud_refuses_talk_when_the_appliance_talk_channel_is_offline(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(talk_audio_relay, "_appliance_channels", {})
    with pytest.raises(HTTPException) as err:
        talk_sessions._require_appliance_talk_channel({"appliance_id": "appl-1"})
    assert err.value.status_code == 503 and "Camera talk channel offline" in err.value.detail
    monkeypatch.setattr(talk_audio_relay, "_appliance_channels", {"appl-1": object()})
    talk_sessions._require_appliance_talk_channel({"appliance_id": "appl-1"})  # connected: allowed


def test_edge_runtime_keeps_its_local_camera_relay(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    monkeypatch.setattr(talk_audio_relay, "_appliance_channels", {})
    talk_sessions._require_appliance_talk_channel({"appliance_id": "appl-1"})


def test_appliance_talk_channel_is_on_by_default(monkeypatch):
    import importlib
    import talk_audio_relay_client
    monkeypatch.delenv("ANYAICAM_TALK_AUDIO_ENABLED", raising=False)
    assert importlib.reload(talk_audio_relay_client).TALK_AUDIO_ENABLED is True
    monkeypatch.setenv("ANYAICAM_TALK_AUDIO_ENABLED", "false")
    assert importlib.reload(talk_audio_relay_client).TALK_AUDIO_ENABLED is False
    monkeypatch.delenv("ANYAICAM_TALK_AUDIO_ENABLED", raising=False)
    importlib.reload(talk_audio_relay_client)


# ---------------------------------------------------------------- one email per visit

class _FakeChannel:
    def __init__(self, status="sent"):
        self.status, self.calls = status, []

    def send(self, notification, recipient):
        self.calls.append((notification, recipient))
        return {"channel": "fake", "status": self.status, "provider": "fake", "error": None}


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_engine, "NOTIFICATION_CHANNEL_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(notification_email, "MEDIA_WAIT_SECONDS", 0)
    monkeypatch.delenv("ANYAICAM_EMAIL_ALERT_EVENT_TYPES", raising=False)
    channels = {"in_app": _FakeChannel("stored"), "email": _FakeChannel(), "sms": _FakeChannel("preview")}
    monkeypatch.setattr(notification_engine, "CHANNELS", channels)
    with override_target(sqlite_path=tmp_path / "visit.db"):
        initialize_database()
        now = "2026-09-28T00:00:00"
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
            for cam in ("entrance", "yard"):
                db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", (cam, "cust-1", "site-1", "appl-1", cam, now))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                       "VALUES('owner-1','p1','owner@example.test','O','customer_owner','x',1,'cust-1',?)", (now,))
            db.execute(
                "INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,phone_number,sms_enabled,event_types_json,camera_scope,quiet_hours_enabled,quiet_start,quiet_end,delivery_mode,updated_at) "
                "VALUES('owner-1','cust-1','owner@example.test',1,'',0,?,'all',0,'22:00','07:00','immediate',?)",
                (json.dumps(["person", "smart_motion", "aac_voice_call"]), now),
            )
        yield channels


def _voice_call(camera_id, created_at):
    with connection() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(aac_voice_call_events)").fetchall()}
        values = {"id": f"vc-{camera_id}", "customer_id": "cust-1", "camera_id": camera_id, "state": "notified",
                  "created_at": created_at, "updated_at": created_at, "event_timestamp": created_at,
                  "detection_event_id": "det-1", "site_id": "site-1"}
        use = {k: v for k, v in values.items() if k in cols}
        db.execute(f"INSERT INTO aac_voice_call_events({','.join(use)}) VALUES({','.join('?' for _ in use)})", tuple(use.values()))


def _person(camera_id):
    notification_engine.fanout_appliance_event(
        {"customer_id": "cust-1", "site_id": "site-1"},
        {"id": None, "camera_id": camera_id, "event_type": "person", "timestamp": datetime.now().isoformat()},
    )


def test_person_email_is_skipped_when_the_visit_already_has_a_voice_call(cloud):
    _voice_call("entrance", datetime.now().isoformat())
    _person("entrance")
    assert cloud["email"].calls == [] and len(cloud["in_app"].calls) == 1  # still in Smart Alerts
    with connection() as db:
        assert db.execute("SELECT status FROM notification_deliveries WHERE channel='email'").fetchone()[0] == "skipped_voice_call"


def test_person_email_still_sent_without_a_voice_call_or_on_another_camera(cloud):
    _voice_call("entrance", (datetime.now() - timedelta(minutes=10)).isoformat())  # an old call: different visit
    _person("entrance")
    _person("yard")
    assert len(cloud["email"].calls) == 2


def test_held_person_email_is_skipped_at_send_time_once_the_call_exists(cloud, monkeypatch):
    import notification_retry_worker
    monkeypatch.setattr(notification_email, "MEDIA_WAIT_SECONDS", 30)
    monkeypatch.setattr(notification_retry_worker, "CHANNELS", cloud)
    notification_engine.fanout_appliance_event(  # an event with media: email held for its thumbnail
        {"customer_id": "cust-1", "site_id": "site-1"},
        {"id": "det-9", "camera_id": "entrance", "event_type": "person", "timestamp": datetime.now().isoformat()},
    )
    _voice_call("entrance", datetime.now().isoformat())  # the call is created a moment later (the real race)
    stats = notification_retry_worker.send_pending_media_emails(now=datetime.now() + timedelta(seconds=40))
    assert stats["skipped"] == 1 and cloud["email"].calls == []


def _entrance(camera_id, enabled=1):
    with connection() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(aac_voice_call_entrance_cameras)").fetchall()}
        values = {"camera_id": camera_id, "customer_id": "cust-1", "enabled": enabled, "configured_at": "2026-09-28T00:00:00",
                  "configured_by": "owner-1", "greeting_text": "Hello"}
        use = {k: v for k, v in values.items() if k in cols}
        db.execute(f"INSERT INTO aac_voice_call_entrance_cameras({','.join(use)}) VALUES({','.join('?' for _ in use)})", tuple(use.values()))


def _held_person(camera_id, event_id):
    notification_engine.fanout_appliance_event(
        {"customer_id": "cust-1", "site_id": "site-1"},
        {"id": event_id, "camera_id": camera_id, "event_type": "person", "timestamp": datetime.now().isoformat()},
    )


def test_entrance_email_waits_for_the_voice_call_even_when_the_thumbnail_is_early(cloud, monkeypatch):
    """The race: thumbnail ready at +5 s, Voice Call created at ~+7 s. The
    entrance camera's Person email must not go out in between."""
    import notification_retry_worker
    monkeypatch.setattr(notification_email, "MEDIA_WAIT_SECONDS", 180)
    monkeypatch.setattr(notification_email, "media_ready", lambda context: True)
    monkeypatch.setattr(notification_retry_worker, "CHANNELS", cloud)
    _entrance("entrance")
    _held_person("entrance", "det-a")
    early = notification_retry_worker.send_pending_media_emails(now=datetime.now() + timedelta(seconds=5))
    assert early["held"] == 1 and cloud["email"].calls == []
    _voice_call("entrance", datetime.now().isoformat())
    later = notification_retry_worker.send_pending_media_emails(now=datetime.now() + timedelta(seconds=35))
    assert later["skipped"] == 1 and cloud["email"].calls == []


def test_entrance_email_is_sent_after_the_grace_when_no_call_happened(cloud, monkeypatch):
    import notification_retry_worker
    monkeypatch.setattr(notification_email, "MEDIA_WAIT_SECONDS", 180)
    monkeypatch.setattr(notification_email, "media_ready", lambda context: True)
    monkeypatch.setattr(notification_retry_worker, "CHANNELS", cloud)
    _entrance("entrance")
    _held_person("entrance", "det-b")
    stats = notification_retry_worker.send_pending_media_emails(now=datetime.now() + timedelta(seconds=35))
    assert stats["sent"] == 1 and len(cloud["email"].calls) == 1


def test_non_entrance_and_disabled_entrance_cameras_are_never_delayed(cloud, monkeypatch):
    import notification_retry_worker
    monkeypatch.setattr(notification_email, "MEDIA_WAIT_SECONDS", 180)
    monkeypatch.setattr(notification_email, "media_ready", lambda context: True)
    monkeypatch.setattr(notification_retry_worker, "CHANNELS", cloud)
    _entrance("entrance", enabled=0)
    _held_person("yard", "det-c")
    _held_person("entrance", "det-d")
    stats = notification_retry_worker.send_pending_media_emails(now=datetime.now() + timedelta(seconds=3))
    assert stats["sent"] == 2 and len(cloud["email"].calls) == 2
