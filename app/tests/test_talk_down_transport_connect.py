"""OnvifBackchannelTransport.connect() protocol-stage error handling.

Production evidence (real Camera 2, 268c9738ff): transport.connect()
was being called correctly (the fix in eba04d4 worked), but connect()
itself crashed with a bare "list index out of range" instead of a
diagnosable failure. Root cause: four separate places in this module
indexed into str.splitlines()/str.split() without checking for the
empty-result case -- an empty response (the camera closing the
connection before sending ANY bytes back, which the real RTSP
mechanics in this module can legitimately produce at any of the three
RTSP stages) makes splitlines() return [], and [0] on that raises
IndexError; a 401 response with no matching WWW-Authenticate header
line produces the same shape of crash one level deeper, inside the
digest-challenge extraction.

These tests drive OnvifBackchannelTransport.connect() against a fake
socket that hands back exactly the malformed/edge-case response shapes
that used to crash, using no real network at all -- socket.
create_connection() is monkeypatched to return the fake, so this is
pure/fast and needs no Docker. Each failure case is asserted to raise
a clean ConnectionError tagged with the exact protocol stage
(stage=describe_response / setup_response / session_extraction /
record_response) rather than an unguarded IndexError, and one full
happy-path case proves the fix didn't change well-formed-response
behavior at all.
"""

import pytest

import talk_down_transport as transport


class _ScriptedSocket:
    """Fake socket.socket for connect(): sendall() inspects the outgoing
    RTSP method (the first word of the request line) and queues up the
    next scripted response for that method; recv() drains it. A queued
    response of b"" simulates the camera closing the connection with no
    data at all -- the exact shape that used to crash with IndexError.
    Each method's response list is consumed in order, so a 401-then-
    retry exchange can be scripted as two entries for the same method.
    """

    def __init__(self, responses):
        self._responses = {method: list(chunks) for method, chunks in responses.items()}
        self._pending = b""

    def settimeout(self, *_args, **_kwargs):
        pass

    def sendall(self, data):
        method = data.decode().split(" ", 1)[0]
        queue = self._responses.get(method, [])
        self._pending = queue.pop(0) if queue else b""

    def recv(self, size):
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def close(self):
        pass


def _make_transport(monkeypatch, responses):
    fake_socket = _ScriptedSocket(responses)
    monkeypatch.setattr(transport.socket, "create_connection", lambda *a, **k: fake_socket)
    return transport.OnvifBackchannelTransport(host="10.0.0.1", port=80, username="user", password="pass", rtsp_path="/onvif/media")


_DESCRIBE_OK = (
    b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n\r\n"
    b"v=0\r\nm=audio 0 RTP/AVP 0\r\na=control:trackID=2\r\n"
)
_SETUP_OK = (
    b"RTSP/1.0 200 OK\r\nCSeq: 2\r\nSession: abc123;timeout=60\r\n"
    b"Transport: RTP/AVP;unicast;client_port=5000-5001;server_port=6000-6001\r\n\r\n"
)
_RECORD_OK = b"RTSP/1.0 200 OK\r\nCSeq: 3\r\nSession: abc123;timeout=60\r\n\r\n"


def test_connect_raises_clean_error_when_describe_gets_no_response(monkeypatch):
    t = _make_transport(monkeypatch, {"DESCRIBE": [b""]})
    with pytest.raises(ConnectionError) as excinfo:
        t.connect()
    assert "stage=describe_response" in str(excinfo.value)


def test_connect_raises_clean_error_when_setup_gets_no_response(monkeypatch):
    t = _make_transport(monkeypatch, {"DESCRIBE": [_DESCRIBE_OK], "SETUP": [b""]})
    with pytest.raises(ConnectionError) as excinfo:
        t.connect()
    assert "stage=setup_response" in str(excinfo.value)


def test_connect_raises_clean_error_when_record_gets_no_response(monkeypatch):
    t = _make_transport(monkeypatch, {"DESCRIBE": [_DESCRIBE_OK], "SETUP": [_SETUP_OK], "RECORD": [b""]})
    with pytest.raises(ConnectionError) as excinfo:
        t.connect()
    assert "stage=record_response" in str(excinfo.value)


def test_connect_raises_clean_error_when_setup_response_has_no_session_header(monkeypatch):
    setup_no_session = (
        b"RTSP/1.0 200 OK\r\nCSeq: 2\r\n"
        b"Transport: RTP/AVP;unicast;client_port=5000-5001;server_port=6000-6001\r\n\r\n"
    )
    t = _make_transport(monkeypatch, {"DESCRIBE": [_DESCRIBE_OK], "SETUP": [setup_no_session]})
    with pytest.raises(ConnectionError) as excinfo:
        t.connect()
    assert "stage=session_extraction" in str(excinfo.value)


def test_authenticated_request_survives_401_with_no_www_authenticate_header(monkeypatch):
    # A 401 status line but no WWW-Authenticate header at all -- the
    # exact shape that used to crash inside the digest-challenge
    # extraction with "list index out of range", one level below
    # connect()'s own status-line checks. Rather than crashing, the
    # empty challenge now produces a syntactically valid (if wrong)
    # retry, which the camera rejects again -- a normal, diagnosable
    # DESCRIBE failure, not a crash.
    unauthorized_no_header = b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n"
    t = _make_transport(monkeypatch, {"DESCRIBE": [unauthorized_no_header, unauthorized_no_header]})
    with pytest.raises(ConnectionError) as excinfo:
        t.connect()
    assert "stage=describe_response" in str(excinfo.value)


def test_connect_succeeds_with_well_formed_responses(monkeypatch):
    t = _make_transport(monkeypatch, {"DESCRIBE": [_DESCRIBE_OK], "SETUP": [_SETUP_OK], "RECORD": [_RECORD_OK]})
    t.connect()  # must not raise
    assert t._session_id == "abc123"
    assert t._rtp_remote == ("10.0.0.1", 6000)


def test_connect_succeeds_after_a_valid_401_digest_challenge_and_retry(monkeypatch):
    # A real, well-formed digest challenge (realm/nonce/qop) -- exactly
    # what the real Camera 2's RTSP port 554 was confirmed to return on
    # an unauthenticated DESCRIBE -- followed by the authenticated
    # retry succeeding. No test elsewhere in this file previously drove
    # a *successful* 401->digest-retry->200 cycle end to end through
    # connect(); the existing no-WWW-Authenticate-header test only
    # covers the failure path. This is the "existing digest 401 ->
    # authenticated retry still works" regression proof for the
    # RTSP-port fix in talk_audio_relay_client.py -- that fix only
    # changes which port connect() is called against, never anything in
    # this module, so this test uses the same port=80 default as every
    # other test in this file; the port itself is irrelevant to what's
    # being proven here.
    challenge = b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Digest realm="IPCam", nonce="abc123nonce", qop="auth"\r\n\r\n'
    t = _make_transport(monkeypatch, {"DESCRIBE": [challenge, _DESCRIBE_OK], "SETUP": [_SETUP_OK], "RECORD": [_RECORD_OK]})
    t.connect()  # must not raise
    assert t._session_id == "abc123"
