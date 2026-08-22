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
        self.sent_requests = []  # every raw request line+headers actually sent, in order

    def settimeout(self, *_args, **_kwargs):
        pass

    def sendall(self, data):
        text = data.decode()
        self.sent_requests.append(text)
        method = text.split(" ", 1)[0]
        queue = self._responses.get(method, [])
        self._pending = queue.pop(0) if queue else b""

    def recv(self, size):
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def close(self):
        pass


def _cseq_of(sent_request_text: str) -> int:
    line = next(line for line in sent_request_text.splitlines() if line.lower().startswith("cseq:"))
    return int(line.split(":", 1)[1].strip())


def _make_transport(monkeypatch, responses):
    fake_socket = _ScriptedSocket(responses)
    monkeypatch.setattr(transport.socket, "create_connection", lambda *a, **k: fake_socket)
    built = transport.OnvifBackchannelTransport(host="10.0.0.1", port=80, username="user", password="pass", rtsp_path="/onvif/media")
    built._test_fake_socket = fake_socket  # test-only handle, not used by production code
    return built


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
    # Also the "existing successful DESCRIBE -> SETUP -> RECORD path"
    # regression proof for the SDP control-URI fix: this fixture's SDP
    # uses the plain relative fragment shape ("trackID=2"), and
    # _resolve_control_uri() must still produce the exact same SETUP
    # request URI it always did for that shape.
    t = _make_transport(monkeypatch, {"DESCRIBE": [_DESCRIBE_OK], "SETUP": [_SETUP_OK], "RECORD": [_RECORD_OK]})
    t.connect()  # must not raise
    assert t._session_id == "abc123"
    assert t._rtp_remote == ("10.0.0.1", 6000)
    setup_request_line = next(r for r in t._test_fake_socket.sent_requests if r.startswith("SETUP")).splitlines()[0]
    assert setup_request_line.startswith("SETUP rtsp://10.0.0.1:80/onvif/media/trackID=2 RTSP/1.0")


def test_connect_uses_an_absolute_sdp_control_uri_directly_no_doubling(monkeypatch):
    # The exact real Camera 2 shape: SDP a=control: is already an
    # absolute rtsp:// URI. Pre-fix, connect() blindly concatenated it
    # onto the base URI (f"{base}/{control}"), producing
    # ".../onvif/media//rtsp://10.0.0.1:80/onvif/media/trackID=2" --
    # SETUP still returned 200 (a lenient camera), RECORD then got no
    # response at all. This proves the actual bytes sent for SETUP now
    # carry the absolute control URI untouched, with no doubling.
    describe_absolute_control = (
        b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n\r\n"
        b"v=0\r\nm=audio 0 RTP/AVP 0\r\na=control:rtsp://10.0.0.1:80/onvif/media/trackID=2\r\n"
    )
    t = _make_transport(monkeypatch, {"DESCRIBE": [describe_absolute_control], "SETUP": [_SETUP_OK], "RECORD": [_RECORD_OK]})
    t.connect()  # must not raise
    assert t._session_id == "abc123"
    setup_request_line = next(r for r in t._test_fake_socket.sent_requests if r.startswith("SETUP")).splitlines()[0]
    assert setup_request_line.startswith("SETUP rtsp://10.0.0.1:80/onvif/media/trackID=2 RTSP/1.0")
    assert "//rtsp://" not in setup_request_line


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


# ------------------------------------------------------------- CSeq monotonicity (real Camera 2 fix: SETUP's hardcoded literal reused DESCRIBE's retry CSeq)
#
# Real Camera 2 (192.168.0.38) evidence: DESCRIBE's initial attempt used
# CSeq 1, got a 401, and its digest retry used CSeq 2 and succeeded.
# SETUP's own hardcoded literal (2) then collided with the CSeq the
# retry had already used on the same connection -- SETUP still
# succeeded (this camera tolerated it for that one request), but it's a
# real RFC 2326 SS12.17 violation, confirmed independently by Live555's
# RTSPClient::resendCommand() (`request->cseq() = ++fCSeq`) and
# FFmpeg's rtsp.c (`rt->seq++` on every send, including retries): every
# actual request on a connection, including digest retries, must get a
# distinct, strictly increasing CSeq.

def _www_authenticate_challenge(cseq_echo: int) -> bytes:
    return (
        f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq_echo}\r\n'
        f'WWW-Authenticate: Digest realm="IPCam", nonce="abc123nonce", qop="auth"\r\n\r\n'
    ).encode()


def test_every_request_gets_a_strictly_increasing_cseq(monkeypatch):
    t = _make_transport(monkeypatch, {"DESCRIBE": [_DESCRIBE_OK], "SETUP": [_SETUP_OK], "RECORD": [_RECORD_OK]})
    t.connect()
    cseqs = [_cseq_of(r) for r in t._test_fake_socket.sent_requests]
    assert cseqs == sorted(cseqs)
    assert len(cseqs) == len(set(cseqs)), f"duplicate CSeq found: {cseqs}"


def test_401_digest_retry_consumes_a_new_cseq(monkeypatch):
    t = _make_transport(monkeypatch, {"DESCRIBE": [_www_authenticate_challenge(1), _DESCRIBE_OK], "SETUP": [_SETUP_OK], "RECORD": [_RECORD_OK]})
    t.connect()
    describe_requests = [r for r in t._test_fake_socket.sent_requests if r.startswith("DESCRIBE")]
    assert len(describe_requests) == 2
    initial_cseq, retry_cseq = (_cseq_of(r) for r in describe_requests)
    assert retry_cseq == initial_cseq + 1
    assert retry_cseq != initial_cseq


def test_setup_continues_from_the_cseq_describes_retry_already_used(monkeypatch):
    # The exact real Camera 2 shape: DESCRIBE needs a 401->retry (two
    # requests, CSeq N and N+1); SETUP's own first attempt must then
    # start at N+2, never reuse N+1.
    t = _make_transport(monkeypatch, {"DESCRIBE": [_www_authenticate_challenge(1), _DESCRIBE_OK], "SETUP": [_SETUP_OK], "RECORD": [_RECORD_OK]})
    t.connect()
    describe_cseqs = [_cseq_of(r) for r in t._test_fake_socket.sent_requests if r.startswith("DESCRIBE")]
    setup_cseqs = [_cseq_of(r) for r in t._test_fake_socket.sent_requests if r.startswith("SETUP")]
    assert len(setup_cseqs) == 1
    assert setup_cseqs[0] == max(describe_cseqs) + 1
    assert setup_cseqs[0] not in describe_cseqs


def test_no_duplicate_cseq_across_a_full_sequence_with_retries_at_every_stage(monkeypatch):
    # The worst case: every one of DESCRIBE, SETUP, and RECORD needs its
    # own 401->digest-retry cycle on the same connection.
    t = _make_transport(monkeypatch, {
        "DESCRIBE": [_www_authenticate_challenge(1), _DESCRIBE_OK],
        "SETUP": [_www_authenticate_challenge(1), _SETUP_OK],
        "RECORD": [_www_authenticate_challenge(1), _RECORD_OK],
    })
    t.connect()  # must not raise
    cseqs = [_cseq_of(r) for r in t._test_fake_socket.sent_requests]
    assert len(cseqs) == 6  # 2 requests per stage x 3 stages
    assert cseqs == sorted(cseqs)
    assert len(cseqs) == len(set(cseqs)), f"duplicate CSeq found across the full sequence: {cseqs}"
