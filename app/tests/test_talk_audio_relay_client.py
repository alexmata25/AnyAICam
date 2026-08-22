"""Push-to-talk audio transport -- appliance relay client tests.

Pure/mocked only: no real WebSocket, no real ffmpeg subprocess, no
real camera/network. Runs directly on this host, no Docker needed
(neither this module nor talk_down_transport.py imports main.py).
"""

import asyncio
import base64
import json
import time

import pytest

import talk_audio_relay_client as client
import talk_down_transport as transport


class _FakeProcess:
    """Stands in for subprocess.Popen -- records every byte written to
    stdin, and stdout.read() returns pre-canned chunks then empty bytes."""

    def __init__(self, stdout_chunks=None):
        self.stdin = _FakeStream()
        self._stdout_chunks = list(stdout_chunks or [])
        self.stdout = _FakeReadableStream(self._stdout_chunks)
        self.terminated = False

    def terminate(self):
        self.terminated = True


class _FakeStream:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeReadableStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def read(self, size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeTransport(transport.TalkDownTransport):
    def __init__(self):
        self.sent = []
        self.closed = False

    def connect(self):
        pass

    def send(self, encoded_audio):
        self.sent.append(encoded_audio)

    def close(self):
        self.closed = True


class _OrderTrackingTransport(transport.TalkDownTransport):
    """Records connect()/send()/close() call order, and can simulate a
    real RTSP handshake failure (e.g. the camera refusing SETUP/RECORD)
    by raising out of connect() -- send() asserts connect() already
    happened, so any test using this transport would itself fail loudly
    if the fix regressed and send() were ever reached first."""

    def __init__(self, fail_connect=False):
        self.calls = []
        self.closed = False
        self._fail_connect = fail_connect

    def connect(self):
        self.calls.append("connect")
        if self._fail_connect:
            raise ConnectionError("simulated RTSP DESCRIBE/SETUP/RECORD failure")

    def send(self, encoded_audio):
        assert "connect" in self.calls, "send() must never be called before connect() has succeeded"
        self.calls.append(("send", encoded_audio))

    def close(self):
        self.calls.append("close")
        self.closed = True


@pytest.fixture(autouse=True)
def _isolated_state():
    client._sessions.clear()
    client.talk_audio_relay_state["active_sessions"] = 0
    yield
    client._sessions.clear()
    client.talk_audio_relay_state["active_sessions"] = 0


CAMERA_MAP = {1: {"camera_id": "cam-1"}, 2: {"camera_id": "cam-2"}}


# ------------------------------------------------------------- RTP packetization (pure)

def test_build_rtp_packet_header_shape():
    packet = transport.build_rtp_packet(b"\x01\x02\x03", sequence_number=5, timestamp=1000, ssrc=0xDEADBEEF)
    assert len(packet) == 12 + 3
    assert packet[0] == 0x80  # version 2, no padding/extension/csrc
    assert packet[1] == 0  # payload type 0 (PCMU), marker bit clear
    assert int.from_bytes(packet[2:4], "big") == 5
    assert int.from_bytes(packet[4:8], "big") == 1000
    assert int.from_bytes(packet[8:12], "big") == 0xDEADBEEF
    assert packet[12:] == b"\x01\x02\x03"


def test_build_rtp_packet_sequence_wraps_at_16_bits():
    packet = transport.build_rtp_packet(b"", sequence_number=70000, timestamp=0, ssrc=0)
    assert int.from_bytes(packet[2:4], "big") == 70000 % 65536


# ------------------------------------------------------------- digest auth (reused/proven mechanism)

def test_digest_authorization_with_qop_auth():
    challenge = {"realm": "Camera", "nonce": "abc123", "qop": "auth", "algorithm": "MD5"}
    header = transport._digest_authorization(challenge, "user", "pass", "DESCRIBE", "rtsp://host/", "cnonce1", "00000001")
    assert 'username="user"' in header
    assert "qop=auth" in header
    assert "nc=00000001" in header
    assert 'cnonce="cnonce1"' in header


def test_digest_authorization_without_qop():
    challenge = {"realm": "Camera", "nonce": "abc123"}
    header = transport._digest_authorization(challenge, "user", "pass", "DESCRIBE", "rtsp://host/", "cnonce1", "00000001")
    assert "qop=" not in header


def test_backchannel_control_found_in_audio_section_only():
    sdp = """v=0
m=video 0 RTP/AVP 96
a=control:trackID=1
m=audio 0 RTP/AVP 0
a=control:trackID=2
"""
    assert transport.OnvifBackchannelTransport._find_backchannel_control(sdp) == "trackID=2"


def test_backchannel_control_none_when_no_audio_section():
    sdp = "v=0\nm=video 0 RTP/AVP 96\na=control:trackID=1\n"
    assert transport.OnvifBackchannelTransport._find_backchannel_control(sdp) is None


# ------------------------------------------------------------- SDP control-URI resolution (real Camera 2 fix: absolute vs. relative a=control:)
#
# Real Camera 2 (192.168.0.38) evidence: its SDP a=control: for the
# audio section is an already-absolute "rtsp://192.168.0.38:554/
# trackID=2", not a relative fragment. The previous code
# (f"{base_uri}/{control}") always concatenated, producing
# "rtsp://192.168.0.38:554//rtsp://192.168.0.38:554/trackID=2" --
# SETUP tolerated it (lenient camera), RECORD did not.

def test_resolve_control_uri_absolute_rtsp_is_used_as_is():
    resolved = transport._resolve_control_uri("rtsp://192.168.0.38:554/", "rtsp://192.168.0.38:554/trackID=2")
    assert resolved == "rtsp://192.168.0.38:554/trackID=2"
    assert "//rtsp://" not in resolved  # no doubling, the exact production bug


def test_resolve_control_uri_absolute_rtsps_is_used_as_is():
    resolved = transport._resolve_control_uri("rtsp://host:554/", "rtsps://host:322/trackID=2")
    assert resolved == "rtsps://host:322/trackID=2"


def test_resolve_control_uri_plain_relative_fragment():
    resolved = transport._resolve_control_uri("rtsp://host:554/Streaming/Channels/101", "trackID=2")
    assert resolved == "rtsp://host:554/Streaming/Channels/101/trackID=2"


def test_resolve_control_uri_leading_slash_relative_path():
    resolved = transport._resolve_control_uri("rtsp://host:554/Streaming/Channels/101", "/trackID=2")
    assert resolved == "rtsp://host:554/trackID=2"  # replaces the base's own path entirely


def test_resolve_control_uri_no_double_slash_when_base_has_trailing_slash():
    # The bare "/" default rtsp_path -- the previous code produced a
    # double slash here too, for the plain-relative case, whenever a
    # camera had no explicit rtsp_path override.
    resolved = transport._resolve_control_uri("rtsp://host:554/", "trackID=2")
    assert resolved == "rtsp://host:554/trackID=2"
    assert "//" not in resolved.split("://", 1)[1]


def test_resolve_control_uri_same_result_with_or_without_base_trailing_slash():
    with_slash = transport._resolve_control_uri("rtsp://host:554/", "trackID=2")
    without_slash = transport._resolve_control_uri("rtsp://host:554", "trackID=2")
    assert with_slash == without_slash == "rtsp://host:554/trackID=2"


def test_resolve_control_uri_none_control_falls_back_to_base_uri_unchanged():
    assert transport._resolve_control_uri("rtsp://host:554/Streaming/Channels/101", None) == "rtsp://host:554/Streaming/Channels/101"


# ------------------------------------------------------------- message dispatch / session lifecycle

def test_start_message_creates_a_session_with_no_video_flags_in_ffmpeg_command(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: _FakeTransport())

    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))

    assert "sess-1" in client._sessions
    command = captured["command"]
    assert command[0] == "ffmpeg"
    assert "-i" in command and "pipe:0" in command
    # Audio-only: no video-related flags, no reference to any camera's
    # RTSP video URL, no -vn needed because there is no video input to
    # begin with.
    assert not any("rtsp://" in str(part) for part in command)
    assert "-vcodec" not in command and "-vf" not in command


def test_start_is_idempotent_for_an_already_running_session(monkeypatch):
    calls = []
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: calls.append(1) or _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: _FakeTransport())

    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    assert len(calls) == 1  # never restarts an already-live session


def test_unknown_camera_id_never_starts_a_session(monkeypatch):
    calls = []
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: calls.append(1) or _FakeProcess())
    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-unknown", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    assert calls == []
    assert "sess-1" not in client._sessions


def test_audio_message_writes_decoded_pcm_to_the_right_sessions_stdin(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: _FakeTransport())

    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    pcm_b64 = base64.b64encode(b"\x01\x02\x03\x04").decode()
    asyncio.run(client._handle_message(json.dumps({"type": "audio", "session_id": "sess-1", "pcm_b64": pcm_b64}), CAMERA_MAP))

    assert client._sessions["sess-1"]["process"].stdin.written == b"\x01\x02\x03\x04"


def test_audio_for_unknown_session_is_silently_dropped():
    # No session started at all -- must not raise.
    pcm_b64 = base64.b64encode(b"\x01").decode()
    asyncio.run(client._handle_message(json.dumps({"type": "audio", "session_id": "sess-ghost", "pcm_b64": pcm_b64}), CAMERA_MAP))
    assert "sess-ghost" not in client._sessions


def test_stop_message_ends_only_that_session(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    fake_transports = {}

    def fake_build_transport(host, username, password, metadata):
        t = _FakeTransport()
        fake_transports[host] = t
        return t

    monkeypatch.setattr(client, "_build_transport", fake_build_transport)

    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-A", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-B", "camera_id": "cam-2", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    assert set(client._sessions) == {"sess-A", "sess-B"}

    asyncio.run(client._handle_message(json.dumps({"type": "stop", "session_id": "sess-A"}), CAMERA_MAP))

    assert "sess-A" not in client._sessions
    assert "sess-B" in client._sessions  # completely unaffected by session A's stop
    assert client._sessions["sess-B"]["process"].terminated is False


def test_stop_closes_the_transport_and_terminates_the_process(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    fake_transport = _FakeTransport()
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: fake_transport)

    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    process = client._sessions["sess-1"]["process"]
    asyncio.run(client._handle_message(json.dumps({"type": "stop", "session_id": "sess-1"}), CAMERA_MAP))

    assert process.terminated is True
    assert process.stdin.closed is True
    assert fake_transport.closed is True


def test_drain_forwards_transcoded_chunks_to_the_right_transport_only(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess(stdout_chunks=[b"\xaa" * 160, b"\xbb" * 160]))
    fake_transport = _FakeTransport()
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: fake_transport)

    # Calls _start_session() directly rather than going through
    # _handle_message() -- the "start" dispatch already schedules its
    # own _drain_transcoded_audio() via create_task() (covered by
    # test_start_message_creates_a_session_with_no_video_flags_in_ffmpeg_command
    # and friends), so also driving a second, separate drain here would
    # race the two against the same fake stdout stream. This test's own
    # focus is the drain function's forwarding behavior in isolation.
    client._start_session("sess-1", "cam-1", {}, 48000, CAMERA_MAP)
    asyncio.run(client._drain_transcoded_audio("sess-1"))

    assert fake_transport.sent == [b"\xaa" * 160, b"\xbb" * 160]


# ------------------------------------------------------------- RTSP port resolution (real Camera 2 fix: ONVIF SOAP port != RTSP port)
#
# Confirmed live against the real Camera 2 (192.168.0.38): DESCRIBE on
# port 80 (ANYAICAM_ONVIF_PORT, the ONVIF/SOAP port) gets TCP-accepted-
# then-closed-with-zero-bytes; port 554 answers with a real RTSP status
# line (401 Unauthorized, the expected digest challenge), and ONVIF's
# own GetStreamUri independently reports 554 for this camera's stream.
# _build_transport() never made an ONVIF SOAP call itself, so
# ANYAICAM_ONVIF_PORT was only ever being reused (incorrectly) as the
# RTSP port here.

def test_build_transport_never_uses_an_onvif_port_variable():
    # AST-based, not substring-based: _build_transport()'s own docstring
    # explains the fix by name ("...never ANYAICAM_ONVIF_PORT...") --
    # a plain substring check on the raw source would false-positive on
    # that prose, since "ANYAICAM_ONVIF_PORT" itself contains
    # "ONVIF_PORT". Parsing just this function's body as an AST and
    # collecting actual Name references (never docstring text) is the
    # precise version of "this code path never touches ONVIF_PORT".
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(client._build_transport)))
    referenced_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "ONVIF_PORT" not in referenced_names


def test_rtsp_port_defaults_to_554_with_no_metadata_override():
    built = client._build_transport("10.0.0.1", "user", "pass", {})
    assert built.port == 554
    assert built.port != 80


def test_discovered_rtsp_port_in_metadata_overrides_the_default():
    built = client._build_transport("10.0.0.1", "user", "pass", {"rtsp_port": 8554})
    assert built.port == 8554


def test_invalid_rtsp_port_metadata_falls_back_to_the_default():
    for bad_value in (None, "554", True, -1, 0, 3.5):
        built = client._build_transport("10.0.0.1", "user", "pass", {"rtsp_port": bad_value})
        assert built.port == 554, f"malformed rtsp_port {bad_value!r} should fall back to the default, not be trusted as-is"


# ------------------------------------------------------------- transport.connect() (fix for the production send()-before-connect() bug)

def test_connect_happens_before_any_send(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess(stdout_chunks=[b"\xaa" * 160]))
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    fake_transport = _OrderTrackingTransport()
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: fake_transport)

    started = client._start_session("sess-1", "cam-1", {}, 48000, CAMERA_MAP)
    assert started is True
    asyncio.run(client._drain_transcoded_audio("sess-1"))

    assert fake_transport.calls[0] == "connect"
    assert any(isinstance(c, tuple) and c[0] == "send" for c in fake_transport.calls)


def test_connect_failure_creates_no_active_session(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: _OrderTrackingTransport(fail_connect=True))

    started = client._start_session("sess-1", "cam-1", {}, 48000, CAMERA_MAP)

    assert started is False
    assert "sess-1" not in client._sessions
    assert client.talk_audio_relay_state["active_sessions"] == 0


def test_ffmpeg_and_transport_are_cleaned_up_on_connect_failure(monkeypatch):
    fake_process = _FakeProcess()
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: fake_process)
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    fake_transport = _OrderTrackingTransport(fail_connect=True)
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: fake_transport)

    client._start_session("sess-1", "cam-1", {}, 48000, CAMERA_MAP)

    assert fake_process.terminated is True
    assert fake_process.stdin.closed is True
    assert fake_transport.closed is True
    assert "close" in fake_transport.calls


def test_no_drain_task_starts_when_connect_fails(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: _OrderTrackingTransport(fail_connect=True))

    drain_calls = []

    async def fake_drain(session_id):
        drain_calls.append(session_id)

    monkeypatch.setattr(client, "_drain_transcoded_audio", fake_drain)

    async def run():
        await client._handle_message(
            json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}),
            CAMERA_MAP,
        )
        await asyncio.sleep(0)  # give any (wrongly) created task a chance to run before we assert none did

    asyncio.run(run())

    assert drain_calls == []
    assert "sess-1" not in client._sessions


def test_drain_task_starts_when_connect_succeeds(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: _OrderTrackingTransport())

    drain_calls = []

    async def fake_drain(session_id):
        drain_calls.append(session_id)

    monkeypatch.setattr(client, "_drain_transcoded_audio", fake_drain)

    async def run():
        await client._handle_message(
            json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}),
            CAMERA_MAP,
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    assert drain_calls == ["sess-1"]
    assert "sess-1" in client._sessions


def test_duplicate_start_is_idempotent_and_connects_only_once(monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    fake_transport = _OrderTrackingTransport()
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: fake_transport)

    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))

    assert fake_transport.calls.count("connect") == 1
    assert "sess-1" in client._sessions


def test_successful_start_feeds_audio_and_stops_cleanly(monkeypatch):
    fake_process = _FakeProcess()
    monkeypatch.setattr(client.subprocess, "Popen", lambda *a, **k: fake_process)
    monkeypatch.setattr(client, "_camera_credentials", lambda n: ("10.0.0.1", "user", "pass"))
    fake_transport = _OrderTrackingTransport()
    monkeypatch.setattr(client, "_build_transport", lambda *a, **k: fake_transport)

    asyncio.run(client._handle_message(json.dumps({"type": "start", "session_id": "sess-1", "camera_id": "cam-1", "metadata": {}, "sample_rate": 48000}), CAMERA_MAP))
    assert "sess-1" in client._sessions
    assert fake_transport.calls == ["connect"]

    pcm_b64 = base64.b64encode(b"\x01\x02\x03\x04").decode()
    asyncio.run(client._handle_message(json.dumps({"type": "audio", "session_id": "sess-1", "pcm_b64": pcm_b64}), CAMERA_MAP))
    assert fake_process.stdin.written == b"\x01\x02\x03\x04"

    asyncio.run(client._handle_message(json.dumps({"type": "stop", "session_id": "sess-1"}), CAMERA_MAP))
    assert "sess-1" not in client._sessions
    assert fake_process.terminated is True
    assert fake_process.stdin.closed is True
    assert fake_transport.closed is True


# ------------------------------------------------------------- worker gating

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_worker_does_nothing_while_disabled(monkeypatch):
    monkeypatch.setattr(client, "TALK_AUDIO_ENABLED", False)
    task = asyncio.ensure_future(client.talk_audio_relay_client_worker())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert client.talk_audio_relay_state["channel_status"] == "disabled"
    assert client._sessions == {}


# ------------------------------------------------------------- no camera-number/model hardcoding

def test_no_camera_number_or_model_hardcoded():
    import inspect
    import re
    source = inspect.getsource(client) + inspect.getsource(transport)
    for model in ("CMIP3342WI", "CMIP1042W"):
        assert model not in source
    assert not re.search(r"camera_number\s*(==|in)\s*[\(\d]", source)


# ------------------------------------------------------------- structural isolation

def test_client_never_touches_recording_analytics_yolo_hls():
    import ast
    import inspect
    forbidden = {
        "ai_person_detector", "motion_detector", "save_yolo_events", "append_analytics_event",
        "analytics_rules_engine", "start_recording", "start_live_stream", "recording_uploader", "analytics_sync",
    }
    tree = ast.parse(inspect.getsource(client))
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            referenced.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            referenced.add(node.module)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    overlap = referenced & forbidden
    assert not overlap, f"talk_audio_relay_client.py unexpectedly references {overlap}"
