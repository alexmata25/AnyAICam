"""Talkdown completion pass (2026-09-26) regression tests.

Covers what the pass added on top of the existing relay/client suites:
the cloud tells the browser when talk is live ("ready") or why it
failed ("error" + a 45xx close), the appliance acknowledges or reports
each session, a camera that rejects its login is not retried (cooldown,
and no extra /close digest attempt), and a failed local start ends its
session row immediately. Everything runs offline against fakes -- no
camera, RTSP or HTTP endpoint is ever contacted.
"""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_talk_completion_pass.db"):
    import appliance_cloud
    import talk_sessions
    import talk_audio_relay
    import talk_audio_relay_client as client_module
    import partner_portal
    from partner_db import connection

from test_talk_audio_relay import (
    _appliance_headers,
    _owner_cookie,
    _seed_appliance_credential,
    _seed_camera,
    _seed_tenant,
    _session_state,
    _start_session,
)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_talk_completion_pass.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database

        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        talk_sessions.register_talk_session_routes(app)
        talk_audio_relay.register_talk_audio_relay_routes(app)
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture(autouse=True)
def _isolated_state():
    for state in (talk_audio_relay._appliance_channels, talk_audio_relay._active_relays,
                  talk_audio_relay._camera_auth_failures, client_module._sessions,
                  client_module._last_start_error, client_module._camera_auth_failures):
        state.clear()
    yield
    for state in (talk_audio_relay._appliance_channels, talk_audio_relay._active_relays,
                  talk_audio_relay._camera_auth_failures, client_module._sessions,
                  client_module._last_start_error, client_module._camera_auth_failures):
        state.clear()


def _seed(db_path, with_appliance_credential=True):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_tenant(db)
            _seed_camera(db, "cam-1", talk_down_supported=1)
            if with_appliance_credential:
                _seed_appliance_credential(db, "appl-1", "cred")


OWNER = {partner_portal.SESSION_COOKIE: _owner_cookie()}


def _eventual_state(db_path, session_id, timeout=5.0):
    """The server finishes its teardown just after the socket closes."""
    deadline = time.monotonic() + timeout
    while _session_state(db_path, session_id) != "stopped" and time.monotonic() < deadline:
        time.sleep(0.05)
    return _session_state(db_path, session_id)


# ---------------------------------------------------------------- cloud relay

def test_appliance_ack_tells_the_browser_talk_is_live(client, db_path):
    _seed(db_path)
    session_id = _start_session(client, "cam-1", OWNER)
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies=OWNER) as customer_ws:
            assert appliance_ws.receive_json()["type"] == "start"
            appliance_ws.send_text(json.dumps({"type": "started", "session_id": session_id}))
            assert customer_ws.receive_json() == {"type": "ready"}
            customer_ws.send_bytes(b"\x01\x02")
            assert appliance_ws.receive_json()["type"] == "audio"


def test_appliance_error_is_shown_to_the_browser_and_ends_the_session(client, db_path):
    _seed(db_path)
    session_id = _start_session(client, "cam-1", OWNER)
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies=OWNER) as customer_ws:
            assert appliance_ws.receive_json()["type"] == "start"
            appliance_ws.send_text(json.dumps({"type": "error", "session_id": session_id, "reason": "camera_auth_failed"}))
            message = customer_ws.receive_json()
            assert message["type"] == "error"
            assert message["reason"] == "camera_auth_failed"
            assert "login" in message["message"]
            closed = customer_ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4502
        # the relay told the appliance to stop its side too
        assert appliance_ws.receive_json() == {"type": "stop", "session_id": session_id}
    assert _session_state(db_path, session_id) == "stopped"


def test_appliance_cannot_act_on_another_appliances_session(client, db_path):
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-2", "cust-1", "site-1", "AIC-TEST0002", "2026-08-21T00:00:00"))
            _seed_appliance_credential(db, "appl-2", "cred2")
    session_id = _start_session(client, "cam-1", OWNER)
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws, \
            client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-2", "cred2")) as other_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies=OWNER) as customer_ws:
            assert appliance_ws.receive_json()["type"] == "start"
            other_ws.send_text(json.dumps({"type": "error", "session_id": session_id, "reason": "camera_auth_failed"}))
            appliance_ws.send_text(json.dumps({"type": "started", "session_id": session_id}))
            assert customer_ws.receive_json() == {"type": "ready"}  # the foreign error was ignored
    assert talk_audio_relay._active_relays == {}


def test_unknown_error_reason_is_normalised(client, db_path):
    _seed(db_path)
    session_id = _start_session(client, "cam-1", OWNER)
    with client.websocket_connect("/api/appliance/talk/channel", headers=_appliance_headers("appl-1", "cred")) as appliance_ws:
        with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies=OWNER) as customer_ws:
            appliance_ws.receive_json()
            appliance_ws.send_text(json.dumps({"type": "error", "session_id": session_id, "reason": "<script>"}))
            assert customer_ws.receive_json()["reason"] == "camera_talk_unavailable"


class _FailingLocalRelay:
    def __init__(self, camera, sample_rate, session_id):
        self.error = "x"
        self.error_reason = "camera_auth_cooldown"

    def start(self):
        return False

    def send_pcm16(self, frame):
        raise AssertionError("no audio after a failed start")

    def stop(self):
        pass


def test_local_start_failure_reports_reason_and_stops_the_session(client, db_path, monkeypatch):
    _seed(db_path, with_appliance_credential=False)
    monkeypatch.setattr(talk_audio_relay, "_LocalIsapiTalkRelay", _FailingLocalRelay)
    session_id = _start_session(client, "cam-1", OWNER)
    with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies=OWNER) as customer_ws:
        message = customer_ws.receive_json()
        assert message["type"] == "error" and message["reason"] == "camera_auth_cooldown"
        closed = customer_ws.receive()
        assert closed["code"] == 4503
    # previously left 'requested' until the expiry sweep
    assert _eventual_state(db_path, session_id) == "stopped"
    assert talk_audio_relay._active_relays == {}


def test_local_start_success_sends_ready(client, db_path, monkeypatch):
    _seed(db_path, with_appliance_credential=False)

    class OkRelay(_FailingLocalRelay):
        def start(self):
            return True

        def send_pcm16(self, frame):
            pass

    monkeypatch.setattr(talk_audio_relay, "_LocalIsapiTalkRelay", OkRelay)
    session_id = _start_session(client, "cam-1", OWNER)
    with client.websocket_connect(f"/api/customer/talk/sessions/{session_id}/audio", cookies=OWNER) as customer_ws:
        assert customer_ws.receive_json() == {"type": "ready"}
        customer_ws.send_bytes(b"\x00\x00")
        customer_ws.close()
    assert _eventual_state(db_path, session_id) == "stopped"


# ---------------------------------------------------------------- local ISAPI relay: lockout protection

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _camera_with_target(monkeypatch):
    monkeypatch.setattr(talk_audio_relay._LocalIsapiTalkRelay, "_target", lambda self: {
        "host": "192.0.2.10", "username": "u", "password": "p", "channel_id": "1",
    })
    return {"id": "cam-lock", "name": "Door"}


def test_camera_login_rejection_starts_a_cooldown_and_skips_close(monkeypatch):
    import requests

    calls = []

    def fake_put(url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        return _FakeResponse(401)

    monkeypatch.setattr(requests, "put", fake_put)
    camera = _camera_with_target(monkeypatch)
    relay = talk_audio_relay._LocalIsapiTalkRelay(camera, 48000, "sess-1")
    assert relay.start() is False
    relay.thread.join(timeout=5)
    assert relay.error_reason == "camera_auth_failed"
    # exactly one login attempt: no /close digest retry after a 401
    assert calls == ["open"]
    assert talk_audio_relay.camera_auth_cooldown_remaining("cam-lock") > 0

    # a second press during the cooldown never contacts the camera
    second = talk_audio_relay._LocalIsapiTalkRelay(camera, 48000, "sess-2")
    assert second.start() is False
    assert second.error_reason == "camera_auth_cooldown"
    assert calls == ["open"]


def test_cooldown_expires(monkeypatch):
    talk_audio_relay.note_camera_auth_failure("cam-x")
    assert talk_audio_relay.camera_auth_cooldown_remaining("cam-x") > 0
    talk_audio_relay._camera_auth_failures["cam-x"] = time.monotonic() - talk_audio_relay.CAMERA_AUTH_COOLDOWN_SECONDS - 1
    assert talk_audio_relay.camera_auth_cooldown_remaining("cam-x") == 0
    assert "cam-x" not in talk_audio_relay._camera_auth_failures


def test_non_auth_camera_error_still_closes_the_channel(monkeypatch):
    import requests

    calls = []

    def fake_put(url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        return _FakeResponse(500)

    monkeypatch.setattr(requests, "put", fake_put)
    relay = talk_audio_relay._LocalIsapiTalkRelay(_camera_with_target(monkeypatch), 48000, "sess-1")
    assert relay.start() is False
    relay.thread.join(timeout=5)
    assert relay.error_reason == "camera_talk_unavailable"
    assert calls == ["open", "close"]
    assert talk_audio_relay.camera_auth_cooldown_remaining("cam-lock") == 0


def test_missing_credentials_reason(monkeypatch):
    monkeypatch.setattr(talk_audio_relay._LocalIsapiTalkRelay, "_target", lambda self: None)
    relay = talk_audio_relay._LocalIsapiTalkRelay({"id": "cam-n"}, 48000, "s")
    assert relay.start() is False
    assert relay.error_reason == "no_camera_credentials"


def test_auth_error_detection():
    assert talk_audio_relay.is_camera_auth_error("DESCRIBE failed (stage=describe_response): RTSP/1.0 401 Unauthorized")
    assert talk_audio_relay.is_camera_auth_error("camera talk open returned HTTP 403")
    assert not talk_audio_relay.is_camera_auth_error("camera talk open returned HTTP 500")
    assert not talk_audio_relay.is_camera_auth_error("port 4010 refused")


# ---------------------------------------------------------------- appliance client replies

CAMERA_MAP = {1: {"camera_id": "cam-1"}}


class _FakeProcess:
    def __init__(self):
        self.stdin = None
        self.stdout = None

    def terminate(self):
        pass


class _FakeTransport:
    connect_error = None
    connects = 0

    def __init__(self, **kwargs):
        pass

    def connect(self):
        type(self).connects += 1
        if type(self).connect_error:
            raise type(self).connect_error

    def close(self):
        pass


@pytest.fixture()
def fake_camera(monkeypatch):
    _FakeTransport.connect_error = None
    _FakeTransport.connects = 0
    monkeypatch.setattr(client_module, "_camera_transport_credentials", lambda n: ("192.0.2.10", 554, "/", "u", "p"))
    monkeypatch.setattr(client_module, "_build_transport", lambda *a: _FakeTransport())
    monkeypatch.setattr(client_module.subprocess, "Popen", lambda *a, **k: _FakeProcess())

    async def no_drain(session_id, send=None):
        return None

    monkeypatch.setattr(client_module, "_drain_transcoded_audio", no_drain)
    return _FakeTransport


def _run_start(camera_id="cam-1", session_id="sess-1"):
    sent = []

    async def send(text):
        sent.append(json.loads(text))

    async def go():
        await client_module._handle_message(json.dumps({"type": "start", "session_id": session_id, "camera_id": camera_id, "metadata": {}, "sample_rate": 48000}), CAMERA_MAP, send)

    asyncio.run(go())
    return sent


def test_client_acknowledges_a_started_session(fake_camera):
    assert _run_start() == [{"type": "started", "session_id": "sess-1"}]


def test_client_reports_unknown_camera(fake_camera):
    assert _run_start(camera_id="cam-other") == [{"type": "error", "session_id": "sess-1", "reason": "unknown_camera"}]


def test_client_reports_missing_credentials(fake_camera, monkeypatch):
    monkeypatch.setattr(client_module, "_camera_transport_credentials", lambda n: None)
    assert _run_start()[0]["reason"] == "no_camera_credentials"


def test_client_rtsp_401_starts_cooldown_and_is_not_retried(fake_camera):
    fake_camera.connect_error = ConnectionError("DESCRIBE failed (stage=describe_response): RTSP/1.0 401 Unauthorized")
    assert _run_start(session_id="s1")[0]["reason"] == "camera_auth_failed"
    assert fake_camera.connects == 1
    assert _run_start(session_id="s2")[0]["reason"] == "camera_auth_cooldown"
    assert fake_camera.connects == 1  # the camera was not contacted again


def test_client_unreachable_camera(fake_camera):
    fake_camera.connect_error = TimeoutError("timed out")
    assert _run_start()[0]["reason"] == "camera_unreachable"
    assert client_module._camera_auth_cooldown_remaining("cam-1") == 0


def test_client_duplicate_start_sends_nothing(fake_camera):
    assert _run_start()[0]["type"] == "started"
    assert _run_start() == []  # already running: neither a second ack nor an error


def test_client_without_send_callback_is_backward_compatible(fake_camera):
    asyncio.run(client_module._handle_message(json.dumps({"type": "start", "session_id": "sess-9", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    assert "sess-9" in client_module._sessions


# ---------------------------------------------------------------- browser client

def test_talk_js_surfaces_every_failure():
    from live_view_page import _TALK_MIC_JS

    for expected in ("startFailureMessage", "microphoneFailureMessage", "socket.onmessage", "'ready'", "'error'", "code >= 4000", "classList.remove('live')"):
        assert expected in _TALK_MIC_JS, expected
