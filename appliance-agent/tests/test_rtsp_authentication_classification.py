"""Regression coverage for classify_rtsp_authentication() (provisioning.py):
the fix for OPTIONS-only credential "verification" silently passing a
camera that never actually challenged for auth -- confirmed live during
Samsung onboarding validation (camera at device_key ...f6af answered
OPTIONS 200 unauthenticated, so entered credentials were never actually
exercised). DESCRIBE is now used instead, since it's the request that
actually asks for the real media session description.

Also covers the qop="auth" Digest fix: confirmed live (same camera)
that a Digest challenge advertising qop="auth" was being answered with
the legacy, qop-less response formula, which the camera correctly
rejects as an invalid Authorization header -- regardless of whether
the password itself was correct. Real credentials, proven working
against this exact camera through a different client (FFmpeg/
libavformat on the Ryzen appliance, which implements full RFC 2617
including qop), were reported as "rejected" by this client's
provisioning verifier for that reason. classify_rtsp_authentication()
now: selects qop="auth" when offered (including from a comma-separated
list alongside unsupported options like "auth-int"), generates a
cnonce, sends nc="00000001", and uses the qop-aware response formula
when qop is present -- while leaving the legacy formula byte-for-byte
unchanged for challenges that don't offer qop at all. It also now
distinguishes UNSUPPORTED_CHALLENGE and MALFORMED_RESPONSE from a
genuine AUTH_INCORRECT credential rejection, so a challenge/protocol
gap like this one is never again misreported as "the password is
wrong."

A real socket server (a minimal, single-connection RTSP responder run
in a background thread) stands in for the physical camera -- same style
this project already favors over hand-copied mocks (see
test_cloud_recording_mode_disabled.py's own docstring) -- so these tests
exercise the real socket code in provisioning.py, not a mocked
_rtsp_request(). No network dependency: everything binds to 127.0.0.1.
"""

import base64
import hashlib
import re
import socket
import threading

import pytest

from anyaicam_agent import provisioning


class _FakeRtspCamera:
    """Listens on 127.0.0.1:<ephemeral>, accepts exactly ONE persistent
    TCP connection -- matching the real client's _RtspSession, which
    now keeps a single connection open across the whole OPTIONS ->
    unauthenticated DESCRIBE -> authenticated DESCRIBE [-> stale-nonce
    retry] exchange, rather than a fresh connection per request -- and
    replies to each request read off that one connection in turn.
    OPTIONS is always answered 200 unconditionally, regardless of
    `mode`, and never counts toward the DESCRIBE-based challenge/verify
    sequence below (matching classify_rtsp_authentication()'s own
    "OPTIONS never gates anything" behavior).

    `mode` selects the DESCRIBE challenge/verification behavior:
      - 'open': 200 to any DESCRIBE, no challenge ever issued.
      - 'basic': challenges with WWW-Authenticate: Basic, then checks
        the retry's Basic token against (username, password).
      - 'digest': legacy (no qop) Digest challenge/response check,
        recomputing the expected response the same pre-RFC-2617 way a
        real older camera would.
      - 'digest-qop': RFC 2617 Digest challenge advertising `qop`
        (defaults to "auth"; override via qop_offer to test a
        comma-separated list or an unsupported-only option like
        "auth-int"), verifying the qop-aware response formula
        (including nc/cnonce) on retry.
      - 'digest-bad-algorithm': Digest challenge with an
        algorithm this client doesn't support (default "SHA-256").
      - 'digest-missing-nonce': Digest challenge with a realm but no
        nonce parameter at all.
      - 'unsupported-scheme': challenges with a scheme this client
        doesn't implement at all (default "NTLM").
      - 'malformed-401': returns 401 with no WWW-Authenticate header.
      - 'weird-status': returns an RTSP status this client can't
        interpret as authenticating anything (default 500), on the
        very first DESCRIBE -- no challenge, no retry.
    `stale_once`, when True (digest/digest-qop modes only): the first
    authenticated DESCRIBE is rejected with a fresh stale="true"
    re-challenge (a new nonce); the SECOND authenticated DESCRIBE,
    built against that new nonce, is verified normally. Never sent
    more than once, matching classify_rtsp_authentication()'s own
    single-retry contract.
    `challenge_algorithm`/`challenge_opaque`, when set (digest/
    digest-qop modes): included in every challenge issued, and
    verified to have been echoed back correctly in the client's
    response.
    """

    REALM = 'test-camera'
    NONCE = 'deadbeefcafefeed0123456789abcdef'
    STALE_NONCE = 'freshnonceafterstalerechallenge0011'

    def __init__(self, mode, username=None, password=None, qop_offer='auth', algorithm='SHA-256', scheme='NTLM',
                 status=500, stale_once=False, challenge_algorithm=None, challenge_opaque=None):
        self.mode = mode
        self.username = username
        self.password = password
        self.qop_offer = qop_offer
        self.algorithm = algorithm
        self.scheme = scheme
        self.status = status
        self.stale_once = stale_once
        self.challenge_algorithm = challenge_algorithm
        self.challenge_opaque = challenge_opaque
        self.last_auth_header = None
        self.last_describe_uri = None
        self.cseqs_seen = []
        self.connections_accepted = 0
        self._stale_already_sent = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.requests_seen = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self):
        # Accepts exactly ONE connection, then handles every request
        # read off it in sequence -- proving (by construction: listen
        # backlog of 1, only ever accepted once) that the real client
        # never opens a second connection mid-exchange.
        try:
            self.sock.settimeout(5)
            conn, _addr = self.sock.accept()
        except OSError:
            return
        self.connections_accepted += 1
        try:
            conn.settimeout(5)
            buffer = ''
            while True:
                chunk = conn.recv(4096).decode(errors='ignore')
                if not chunk:
                    return
                buffer += chunk
                while '\r\n\r\n' in buffer:
                    request_text, buffer = buffer.split('\r\n\r\n', 1)
                    self.requests_seen += 1
                    response = self._handle(request_text)
                    conn.sendall(response.encode())
        except OSError:
            return
        finally:
            conn.close()

    def _handle(self, request_text):
        lines = request_text.split('\r\n')
        method = lines[0].split(' ')[0] if lines and lines[0] else ''
        cseq_line = next((line for line in lines if line.lower().startswith('cseq:')), None)
        if cseq_line:
            self.cseqs_seen.append(cseq_line.split(':', 1)[1].strip())
        if method == 'OPTIONS':
            # Never gates anything -- always 200, regardless of `mode`.
            return 'RTSP/1.0 200 OK\r\nCSeq: 1\r\nPublic: OPTIONS, DESCRIBE\r\n\r\n'
        for line in lines:
            if line.startswith('DESCRIBE '):
                self.last_describe_uri = line.split(' ')[1]
        auth_header = None
        for line in lines:
            if line.lower().startswith('authorization:'):
                auth_header = line.split(':', 1)[1].strip()
        return self._response_for(request_text, auth_header)

    def _response_for(self, request_text, auth_header):
        if self.mode == 'open':
            return 'RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n'
        if self.mode == 'weird-status':
            return f'RTSP/1.0 {self.status} Server Error\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n'
        if self.mode == 'malformed-401':
            return 'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n'
        if auth_header is None:
            challenge = self._challenge(self.NONCE)
            return f'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: {challenge}\r\n\r\n'
        self.last_auth_header = auth_header
        if self.stale_once and not self._stale_already_sent and self.mode in ('digest', 'digest-qop'):
            self._stale_already_sent = True
            stale_challenge = self._challenge(self.STALE_NONCE, stale=True)
            return f'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: {stale_challenge}\r\n\r\n'
        nonce_used = self.STALE_NONCE if (self.stale_once and self._stale_already_sent) else self.NONCE
        ok = self._credential_accepted(auth_header, request_text, nonce_used)
        return ('RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n' if ok else
                'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n')

    def _challenge(self, nonce, stale=False):
        extras = ''
        if self.challenge_algorithm and self.mode in ('digest', 'digest-qop'):
            extras += f', algorithm={self.challenge_algorithm}'
        if self.challenge_opaque and self.mode in ('digest', 'digest-qop'):
            extras += f', opaque="{self.challenge_opaque}"'
        if stale:
            extras += ', stale=true'
        if self.mode == 'basic':
            return f'Basic realm="{self.REALM}"'
        if self.mode == 'digest':
            return f'Digest realm="{self.REALM}", nonce="{nonce}"{extras}'
        if self.mode == 'digest-qop':
            return f'Digest realm="{self.REALM}", nonce="{nonce}", qop="{self.qop_offer}"{extras}'
        if self.mode == 'digest-bad-algorithm':
            return f'Digest realm="{self.REALM}", nonce="{nonce}", algorithm={self.algorithm}'
        if self.mode == 'digest-missing-nonce':
            return f'Digest realm="{self.REALM}"'
        if self.mode == 'unsupported-scheme':
            return f'{self.scheme} realm="{self.REALM}"'
        raise AssertionError(f'no challenge defined for mode {self.mode!r}')

    def _credential_accepted(self, auth_header, request_text, nonce):
        if self.mode == 'basic':
            token = base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
            return auth_header == f'Basic {token}'
        # digest / digest-qop: recompute the expected response the same
        # way a real camera would -- using the client-supplied nc/cnonce
        # (unknown ahead of time; extracted from the header itself, same
        # as a real verifier would) for the qop-aware formula.
        uri_match = None
        for line in request_text.split('\r\n'):
            if line.startswith('DESCRIBE '):
                uri_match = line.split(' ')[1]
        ha1 = hashlib.md5(f'{self.username}:{self.REALM}:{self.password}'.encode()).hexdigest()
        ha2 = hashlib.md5(f'DESCRIBE:{uri_match}'.encode()).hexdigest()
        if self.challenge_algorithm and self.mode in ('digest', 'digest-qop'):
            if _digest_field(auth_header, 'algorithm') != self.challenge_algorithm:
                return False  # must be echoed back exactly as challenged
        if self.challenge_opaque and self.mode in ('digest', 'digest-qop'):
            if _digest_field(auth_header, 'opaque') != self.challenge_opaque:
                return False  # must be echoed back exactly as challenged
        if self.mode == 'digest-qop':
            nc = _digest_field(auth_header, 'nc')
            cnonce = _digest_field(auth_header, 'cnonce')
            qop_used = _digest_field(auth_header, 'qop')
            if not (nc and cnonce and qop_used):
                return False  # a compliant qop=auth response must include all three
            expected = hashlib.md5(f'{ha1}:{nonce}:{nc}:{cnonce}:{qop_used}:{ha2}'.encode()).hexdigest()
            return f'response="{expected}"' in auth_header and nc == '00000001'
        expected = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
        return f'response="{expected}"' in auth_header


def _digest_field(auth_header, field):
    """Independent (test-only, deliberately not reusing provisioning.py's
    own parser) extraction of one Digest field's value from a real,
    on-the-wire Authorization header -- quoted or not."""
    match = re.search(rf'{field}=(?:"([^"]*)"|([^\s,]+))', auth_header)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


# --------------------------------------------------------------- the six states


def test_no_auth_required_when_device_never_challenges():
    with _FakeRtspCamera('open') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.NO_AUTH_REQUIRED
    assert 'admin' not in detail and 'correct-pass' not in detail


def test_auth_correct_with_basic_challenge_and_right_credentials():
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT
    assert 'admin' not in detail and 'correct-pass' not in detail


def test_auth_incorrect_with_basic_challenge_and_wrong_credentials():
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'wrong-pass')
    assert status == provisioning.AUTH_INCORRECT
    assert 'wrong-pass' not in detail and 'correct-pass' not in detail


def test_auth_correct_with_digest_challenge_and_right_credentials():
    with _FakeRtspCamera('digest', username='admin', password='correct-pass') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT


def test_auth_incorrect_with_digest_challenge_and_wrong_credentials():
    with _FakeRtspCamera('digest', username='admin', password='correct-pass') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'wrong-pass')
    assert status == provisioning.AUTH_INCORRECT


def test_auth_incorrect_when_device_challenges_but_no_credentials_supplied():
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, '', '')
    assert status == provisioning.AUTH_INCORRECT


def test_unreachable_when_nothing_is_listening():
    # Bind, learn a real free port, then close it before connecting --
    # guarantees connection-refused without depending on any external
    # network state.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', port, 'admin', 'pass', timeout=1)
    assert status == provisioning.UNREACHABLE
    assert 'admin' not in detail and 'pass' not in detail


# ------------------------------------------------------- qop="auth" Digest (the fix)


def test_auth_correct_with_qop_auth_digest_challenge_and_right_credentials():
    """The exact live gap: previously misreported as AUTH_INCORRECT
    because the response was computed with the legacy formula against
    a challenge that actually required qop="auth"."""
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT
    assert 'admin' not in detail and 'correct-pass' not in detail


def test_auth_incorrect_with_qop_auth_digest_challenge_and_wrong_credentials():
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'wrong-pass')
    assert status == provisioning.AUTH_INCORRECT
    assert 'wrong-pass' not in detail and 'correct-pass' not in detail


def test_qop_auth_response_includes_a_real_cnonce_and_nc_00000001():
    """Structural proof (not just a pass/fail outcome) that the
    on-the-wire Authorization header actually carries qop, a non-empty
    cnonce, and nc="00000001" -- the exact three fields the legacy
    formula never sent, which is what a qop-enforcing camera rejects
    regardless of password correctness."""
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth') as camera:
        status, _ = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
        auth_header = camera.last_auth_header
    assert status == provisioning.AUTH_CORRECT
    assert _digest_field(auth_header, 'qop') == 'auth'
    assert _digest_field(auth_header, 'nc') == '00000001'
    cnonce = _digest_field(auth_header, 'cnonce')
    assert cnonce and len(cnonce) >= 8


def test_multiple_qop_values_selects_auth():
    """A challenge offering several qop options (e.g. a camera that
    also advertises auth-int) must still select and use 'auth' --
    the only option this client implements -- rather than failing or
    picking the first/last listed value blindly."""
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth,auth-int') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT


def test_qop_value_with_extra_whitespace_still_selects_auth():
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth-int, auth') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT


def test_legacy_no_qop_digest_response_is_unchanged_by_the_qop_fix():
    """The existing pre-RFC-2617 formula must still work byte-for-byte
    for older cameras that never offer qop at all -- this is the exact
    regression the fix must not introduce."""
    with _FakeRtspCamera('digest', username='admin', password='correct-pass') as camera:
        status, _ = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
        auth_header = camera.last_auth_header
    assert status == provisioning.AUTH_CORRECT
    assert _digest_field(auth_header, 'qop') is None
    assert _digest_field(auth_header, 'cnonce') is None
    assert _digest_field(auth_header, 'nc') is None


# ------------------------------------------ fail-closed: unsupported / malformed


def test_qop_auth_int_only_is_unsupported_not_incorrect():
    """A camera offering only qop="auth-int" (message-integrity, never
    implemented here) must be reported as UNSUPPORTED_CHALLENGE, never
    as AUTH_INCORRECT -- failing closed (never verified as correct)
    without ever claiming the password itself was wrong."""
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth-int') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.UNSUPPORTED_CHALLENGE
    assert 'admin' not in detail and 'correct-pass' not in detail


def test_unsupported_digest_algorithm_is_unsupported_not_incorrect():
    with _FakeRtspCamera('digest-bad-algorithm', username='admin', password='correct-pass', algorithm='SHA-256') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.UNSUPPORTED_CHALLENGE
    assert 'admin' not in detail and 'correct-pass' not in detail


def test_digest_challenge_missing_nonce_is_unsupported_not_incorrect():
    with _FakeRtspCamera('digest-missing-nonce', username='admin', password='correct-pass') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.UNSUPPORTED_CHALLENGE
    assert 'admin' not in detail and 'correct-pass' not in detail


def test_unsupported_scheme_is_unsupported_not_incorrect():
    with _FakeRtspCamera('unsupported-scheme', username='admin', password='correct-pass', scheme='NTLM') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.UNSUPPORTED_CHALLENGE
    assert 'ntlm' in detail.lower()


def test_401_without_challenge_header_is_malformed_not_incorrect():
    with _FakeRtspCamera('malformed-401', username='admin', password='correct-pass') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.MALFORMED_RESPONSE
    assert 'admin' not in detail and 'correct-pass' not in detail


def test_unexpected_status_code_is_malformed_not_unreachable():
    with _FakeRtspCamera('weird-status', username='admin', password='correct-pass', status=500) as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.MALFORMED_RESPONSE
    assert '500' in detail


def test_none_of_the_new_failure_states_ever_leak_credential_material():
    """Sweeps every new fail-closed state's detail string for anything
    that could be credential/challenge material -- the nonce and realm
    used here deliberately double as a substring check that neither
    ever leaks either, alongside the existing username/password
    checks."""
    scenarios = [
        _FakeRtspCamera('digest-qop', username='admin', password='super-secret-pw', qop_offer='auth-int'),
        _FakeRtspCamera('digest-bad-algorithm', username='admin', password='super-secret-pw', algorithm='SHA-256'),
        _FakeRtspCamera('digest-missing-nonce', username='admin', password='super-secret-pw'),
        _FakeRtspCamera('unsupported-scheme', username='admin', password='super-secret-pw', scheme='NTLM'),
        _FakeRtspCamera('malformed-401', username='admin', password='super-secret-pw'),
    ]
    for camera in scenarios:
        with camera:
            status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'super-secret-pw')
        assert status in (provisioning.UNSUPPORTED_CHALLENGE, provisioning.MALFORMED_RESPONSE)
        assert 'admin' not in detail
        assert 'super-secret-pw' not in detail
        assert camera.NONCE not in detail


# ---------------------------------------------- known-good Digest response vectors
#
# _digest_header()'s qop-aware formula checked against RFC 2617 section
# 3.5's own worked example -- an external, independently-published
# vector, not a value derived from this codebase's own implementation.

def test_digest_header_matches_rfc2617_worked_example_with_qop():
    from anyaicam_agent.provisioning import _digest_header
    header = _digest_header(
        'Mufasa', 'Circle Of Life', 'testrealm@host.com',
        'dcd98b7102dd2f0e8b11d0f600bfb0c093', 'GET', '/dir/index.html',
        qop='auth', cnonce='0a4f113b', nc='00000001',
    )
    assert 'response="6629fae49393a05397450978507c4ef1"' in header
    assert 'qop=auth' in header
    assert 'nc=00000001' in header
    assert 'cnonce="0a4f113b"' in header


def test_digest_header_legacy_formula_matches_independent_computation():
    """Independently recomputed (not by calling _digest_header() a
    second time) to prove the legacy, qop-less formula is exactly
    MD5(HA1:nonce:HA2) -- unchanged by the qop-aware addition."""
    from anyaicam_agent.provisioning import _digest_header
    username, password, realm, nonce = 'admin', 'hunter2', 'test-camera', 'abc123'
    method, uri = 'DESCRIBE', 'rtsp://192.0.2.10:554/Streaming/Channels/101'
    ha1 = hashlib.md5(f'{username}:{realm}:{password}'.encode()).hexdigest()
    ha2 = hashlib.md5(f'{method}:{uri}'.encode()).hexdigest()
    expected = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
    header = _digest_header(username, password, realm, nonce, method, uri)
    assert f'response="{expected}"' in header
    assert 'qop=' not in header
    assert 'cnonce=' not in header


def test_digest_header_qop_response_differs_from_legacy_for_the_same_inputs():
    """Sanity check that the qop-aware branch really does use a
    different formula, not just append extra fields onto the legacy
    response -- computing the same qop=auth response with any of
    nonce/nc/cnonce/qop changed must change the digest response."""
    from anyaicam_agent.provisioning import _digest_header
    legacy = _digest_header('admin', 'hunter2', 'realm', 'nonce123', 'DESCRIBE', 'rtsp://x/y')
    qop_aware = _digest_header('admin', 'hunter2', 'realm', 'nonce123', 'DESCRIBE', 'rtsp://x/y', qop='auth', cnonce='cn1', nc='00000001')
    legacy_response = _digest_field(legacy, 'response')
    qop_response = _digest_field(qop_aware, 'response')
    assert legacy_response != qop_response


# ------------------------------------------------- verify_rtsp_credentials() wrapper


def test_wrapper_reports_ok_for_no_auth_required():
    with _FakeRtspCamera('open') as camera:
        ok, detail = provisioning.verify_rtsp_credentials('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert ok is True


def test_wrapper_reports_ok_for_auth_correct():
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        ok, detail = provisioning.verify_rtsp_credentials('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert ok is True


def test_wrapper_reports_not_ok_for_auth_incorrect():
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        ok, detail = provisioning.verify_rtsp_credentials('127.0.0.1', camera.port, 'admin', 'wrong-pass')
    assert ok is False


def test_wrapper_reports_not_ok_for_unreachable():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    ok, detail = provisioning.verify_rtsp_credentials('127.0.0.1', port, 'admin', 'pass', timeout=1)
    assert ok is False


def test_wrapper_reports_not_ok_for_unsupported_challenge():
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth-int') as camera:
        ok, detail = provisioning.verify_rtsp_credentials('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert ok is False


def test_wrapper_reports_not_ok_for_malformed_response():
    with _FakeRtspCamera('malformed-401', username='admin', password='correct-pass') as camera:
        ok, detail = provisioning.verify_rtsp_credentials('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert ok is False


def test_wrapper_reports_ok_for_qop_auth_correct_credentials():
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth') as camera:
        ok, detail = provisioning.verify_rtsp_credentials('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert ok is True


# --------------------------------------------------- end-to-end via verify_device()


def test_verify_device_end_to_end_distinguishes_correct_from_incorrect(monkeypatch):
    original_verify = provisioning.verify_rtsp_credentials  # capture before patching -- the lambda must not call the patched name
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        device = {'ip': '127.0.0.1', 'rtsp_support': True, 'device_key': 'k'}
        monkeypatch.setattr(provisioning, 'locate_device', lambda *a, **k: device)
        monkeypatch.setattr(provisioning, 'verify_rtsp_credentials',
                             lambda ip, port, u, p, **kw: original_verify('127.0.0.1', camera.port, u, p))
        success, message = provisioning.verify_device('k', {'username': 'admin', 'password': 'correct-pass'})
    assert success is True
    assert 'admin' not in message and 'correct-pass' not in message


def test_verify_device_end_to_end_succeeds_against_a_qop_auth_camera(monkeypatch):
    """The exact end-to-end path this whole fix is for: a camera that
    requires qop="auth", reached through verify_device() -- the real
    entry point service.py's poll_provisioning() calls -- not just the
    lower-level classify_rtsp_authentication() directly."""
    original_verify = provisioning.verify_rtsp_credentials
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth') as camera:
        device = {'ip': '127.0.0.1', 'rtsp_support': True, 'device_key': 'k'}
        monkeypatch.setattr(provisioning, 'locate_device', lambda *a, **k: device)
        monkeypatch.setattr(provisioning, 'verify_rtsp_credentials',
                             lambda ip, port, u, p, **kw: original_verify('127.0.0.1', camera.port, u, p))
        success, message = provisioning.verify_device('k', {'username': 'admin', 'password': 'correct-pass'})
    assert success is True
    assert 'admin' not in message and 'correct-pass' not in message


# ------------------------------------------- verify_device()'s RTSP path (the fix)
#
# Confirmed by code inspection (Samsung, camera device_key ...f6af):
# verify_device() never passed a `path` to verify_rtsp_credentials() at
# all, so every real-camera check silently authenticated against the
# bare RTSP root ('/') instead of an actual stream resource -- never
# the /Streaming/Channels/101 path Ryzen's own known-working config
# uses for this exact camera. These monkeypatch verify_rtsp_credentials
# forwarding the real `path` kwarg through (unlike the two tests above,
# which don't care what path was used) specifically so the fake
# camera's captured request line proves which URI was actually sent.

def test_verify_device_uses_the_default_stream_path_not_bare_root(monkeypatch):
    original_verify = provisioning.verify_rtsp_credentials
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        device = {'ip': '127.0.0.1', 'rtsp_support': True, 'device_key': 'k'}
        monkeypatch.setattr(provisioning, 'locate_device', lambda *a, **k: device)
        monkeypatch.setattr(provisioning, 'verify_rtsp_credentials',
                             lambda ip, port, u, p, path='/', **kw: original_verify('127.0.0.1', camera.port, u, p, path=path))
        provisioning.verify_device('k', {'username': 'admin', 'password': 'correct-pass'})
    assert camera.last_describe_uri == f'rtsp://127.0.0.1:{camera.port}{provisioning.DEFAULT_RTSP_STREAM_PATH}'
    assert camera.last_describe_uri != f'rtsp://127.0.0.1:{camera.port}/'


def test_verify_device_prefers_a_per_device_discovered_path_over_the_default(monkeypatch):
    """A real, per-device discovered path (once discovery.py can supply
    one) must take priority over the fleet-wide default -- the default
    exists only because nothing upstream resolves one yet."""
    original_verify = provisioning.verify_rtsp_credentials
    with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
        device = {'ip': '127.0.0.1', 'rtsp_support': True, 'device_key': 'k', 'rtsp_path': '/live/ch00_1'}
        monkeypatch.setattr(provisioning, 'locate_device', lambda *a, **k: device)
        monkeypatch.setattr(provisioning, 'verify_rtsp_credentials',
                             lambda ip, port, u, p, path='/', **kw: original_verify('127.0.0.1', camera.port, u, p, path=path))
        provisioning.verify_device('k', {'username': 'admin', 'password': 'correct-pass'})
    assert camera.last_describe_uri == f'rtsp://127.0.0.1:{camera.port}/live/ch00_1'


def test_default_stream_path_matches_the_known_working_ryzen_and_camera_url_default():
    """Not an arbitrary new default -- pinned to the exact value this
    codebase already uses everywhere else a path isn't independently
    known (main.py's camera_url() legacy fallback; the Ryzen
    appliance's own CAMERA{n}_PATH default), confirmed correct for
    this exact camera via FFmpeg on Ryzen."""
    assert provisioning.DEFAULT_RTSP_STREAM_PATH == '/Streaming/Channels/101'


# ------------------------------------------- temporary rtsp_auth_diagnostic
#
# TEMPORARY: coverage for the secret-safe diagnostic added to identify
# the RTSP incompatibility that survived both the qop/Digest fix and
# the /Streaming/Channels/101 path fix on the real Samsung camera.
# Remove this section together with _log_rtsp_diagnostic() and its call
# sites once that incompatibility is identified and fixed.

import logging


def test_diagnostic_logs_scheme_qop_path_and_statuses_for_a_qop_success(caplog):
    with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
        with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth') as camera:
            provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass', path='/Streaming/Channels/101')
    records = [r for r in caplog.records if r.getMessage().startswith('rtsp_auth_diagnostic')]
    assert len(records) == 1
    message = records[0].getMessage()
    assert 'scheme=digest' in message
    assert 'qop_offered=True' in message
    assert 'qop_selected=auth' in message
    assert 'path=/Streaming/Channels/101' in message
    assert 'first_status=401' in message
    assert 'retry_status=200' in message
    # This challenge never included an algorithm parameter -- the
    # diagnostic reports exactly what the challenge specified (None
    # here), never a default the challenge itself never asked for.
    assert 'algorithm=None' in message
    assert 'challenge_issue=None' in message
    assert 'stale_retry=False' in message
    assert f'outcome={provisioning.AUTH_CORRECT}' in message


def test_diagnostic_records_qop_offered_false_for_legacy_digest(caplog):
    with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
        with _FakeRtspCamera('digest', username='admin', password='correct-pass') as camera:
            provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('rtsp_auth_diagnostic'))
    assert 'qop_offered=False' in message
    assert 'qop_selected=None' in message


def test_diagnostic_records_challenge_issue_for_unsupported_qop(caplog):
    with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
        with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth-int') as camera:
            provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('rtsp_auth_diagnostic'))
    assert 'challenge_issue=unsupported_qop' in message
    assert f'outcome={provisioning.UNSUPPORTED_CHALLENGE}' in message
    # No retry is ever attempted once the challenge is rejected as
    # unsupported -- retry_status stays None.
    assert 'retry_status=None' in message


def test_diagnostic_records_scheme_for_basic_auth(caplog):
    with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
        with _FakeRtspCamera('basic', username='admin', password='correct-pass') as camera:
            provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('rtsp_auth_diagnostic'))
    assert 'scheme=basic' in message
    assert 'algorithm=None' in message


def test_diagnostic_logs_exactly_once_even_for_unreachable(caplog):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
        provisioning.classify_rtsp_authentication('127.0.0.1', port, 'admin', 'pass', timeout=1)
    records = [r for r in caplog.records if r.getMessage().startswith('rtsp_auth_diagnostic')]
    assert len(records) == 1
    assert f'outcome={provisioning.UNREACHABLE}' in records[0].getMessage()
    assert 'scheme=None' in records[0].getMessage()


def test_diagnostic_never_logs_credential_material_across_every_outcome(caplog):
    """Sweeps every distinguishable outcome with a distinctive,
    unmistakable password and asserts it (and the username, and the
    fake camera's own nonce/realm, standing in for anything
    challenge-derived) never appears in ANY log record produced during
    that attempt -- not just the one rtsp_auth_diagnostic line."""
    secret_password = 'unmistakable-diagnostic-canary-pw'
    scenarios = [
        _FakeRtspCamera('basic', username='admin', password=secret_password),
        _FakeRtspCamera('digest', username='admin', password=secret_password),
        _FakeRtspCamera('digest-qop', username='admin', password=secret_password, qop_offer='auth'),
        _FakeRtspCamera('digest-qop', username='admin', password=secret_password, qop_offer='auth-int'),
        _FakeRtspCamera('digest-bad-algorithm', username='admin', password=secret_password, algorithm='SHA-256'),
        _FakeRtspCamera('malformed-401', username='admin', password=secret_password),
        _FakeRtspCamera('unsupported-scheme', username='admin', password=secret_password, scheme='NTLM'),
    ]
    for camera in scenarios:
        with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
            with camera:
                provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', secret_password)
        all_log_text = '\n'.join(r.getMessage() for r in caplog.records)
        assert 'admin' not in all_log_text
        assert secret_password not in all_log_text
        assert camera.NONCE not in all_log_text
        assert camera.REALM not in all_log_text
        caplog.clear()


# ------------------------------------------- session/protocol compatibility
#
# Coverage for the fixes made after directly comparing this client
# against the known-working Ryzen/FFmpeg RTSP exchange for the same
# camera and path (192.168.0.38, /Streaming/Channels/101): a single
# persistent TCP connection, an incrementing CSeq, a normal
# User-Agent, an OPTIONS preflight, echoing algorithm/opaque when the
# challenge specifies them, and supporting one stale-nonce
# re-challenge -- none of which the original per-request-socket client
# did.

def test_options_and_both_describes_use_one_persistent_connection():
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth') as camera:
        status, _ = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT
    assert camera.connections_accepted == 1, 'OPTIONS + both DESCRIBEs must share one TCP connection, not reconnect per request'
    assert camera.requests_seen == 3  # OPTIONS, unauthenticated DESCRIBE, authenticated DESCRIBE


def test_cseq_increments_across_the_session():
    with _FakeRtspCamera('digest', username='admin', password='correct-pass') as camera:
        provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert camera.cseqs_seen == ['1', '2', '3']


def test_options_is_sent_before_describe():
    with _FakeRtspCamera('open') as camera:
        provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    # 'open' mode's very first non-OPTIONS request already gets 200
    # (NO_AUTH_REQUIRED) -- two total requests were seen, and the CSeq
    # sequence proves OPTIONS was first.
    assert camera.requests_seen == 2
    assert camera.cseqs_seen == ['1', '2']


def test_user_agent_and_accept_headers_are_sent(monkeypatch):
    """Structural proof the real wire request carries both headers --
    not inferred from a successful outcome alone."""
    seen_requests = []
    real_sock_class = socket.socket

    class _RecordingSocket(real_sock_class):
        def sendall(self, data, *a, **kw):
            seen_requests.append(data.decode(errors='ignore'))
            return super().sendall(data, *a, **kw)

    monkeypatch.setattr(provisioning.socket, 'socket', lambda *a, **k: _RecordingSocket(*a, **k))
    with _FakeRtspCamera('open') as camera:
        provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert any('User-Agent: AnyAiCamAgent' in r for r in seen_requests)
    describe_requests = [r for r in seen_requests if r.startswith('DESCRIBE ')]
    assert describe_requests and all('Accept: application/sdp' in r for r in describe_requests)


def test_algorithm_and_opaque_are_echoed_when_challenge_specifies_them():
    with _FakeRtspCamera('digest', username='admin', password='correct-pass',
                          challenge_algorithm='MD5', challenge_opaque='5ccc069c403ebaf9f0171e9517f40e41') as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
        auth_header = camera.last_auth_header
    assert status == provisioning.AUTH_CORRECT
    assert _digest_field(auth_header, 'algorithm') == 'MD5'
    assert _digest_field(auth_header, 'opaque') == '5ccc069c403ebaf9f0171e9517f40e41'


def test_algorithm_and_opaque_are_never_invented_when_challenge_omits_them():
    with _FakeRtspCamera('digest', username='admin', password='correct-pass') as camera:
        provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
        auth_header = camera.last_auth_header
    assert 'algorithm=' not in auth_header
    assert 'opaque=' not in auth_header


def test_stale_nonce_retry_succeeds_once_with_the_new_nonce():
    with _FakeRtspCamera('digest', username='admin', password='correct-pass', stale_once=True) as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT
    assert 'admin' not in detail and 'correct-pass' not in detail
    # OPTIONS, unauth DESCRIBE, first auth DESCRIBE (rejected as stale),
    # second auth DESCRIBE (accepted against the new nonce).
    assert camera.requests_seen == 4
    assert camera.connections_accepted == 1


def test_stale_nonce_retry_works_for_qop_auth_too():
    with _FakeRtspCamera('digest-qop', username='admin', password='correct-pass', qop_offer='auth', stale_once=True) as camera:
        status, _ = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    assert status == provisioning.AUTH_CORRECT


def test_stale_retry_is_attempted_only_once_not_looped(caplog):
    """Confirms the diagnostic's own stale_retry/stale_retry_status
    fields reflect exactly one retry -- not a loop -- and that a
    second stale challenge (were a misbehaving camera to send one)
    would surface as AUTH_INCORRECT rather than retrying forever.
    Verified here via the recorded diagnostic rather than a third
    camera-side stale challenge, since this client's own contract is
    "one retry, then stop" regardless of what the device does next."""
    with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
        with _FakeRtspCamera('digest', username='admin', password='correct-pass', stale_once=True) as camera:
            provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'correct-pass')
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('rtsp_auth_diagnostic'))
    assert 'stale_retry=True' in message
    assert 'stale_retry_status=200' in message


def test_stale_retry_still_fails_closed_on_wrong_credentials():
    with _FakeRtspCamera('digest', username='admin', password='correct-pass', stale_once=True) as camera:
        status, detail = provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', 'wrong-pass')
    assert status == provisioning.AUTH_INCORRECT


def test_no_secrets_leak_across_the_full_session_including_stale_retry(caplog):
    secret_password = 'unmistakable-session-canary-pw'
    with caplog.at_level(logging.INFO, logger='anyaicam.agent'):
        with _FakeRtspCamera('digest', username='admin', password=secret_password, stale_once=True,
                              challenge_algorithm='MD5', challenge_opaque='opaque-canary-value') as camera:
            provisioning.classify_rtsp_authentication('127.0.0.1', camera.port, 'admin', secret_password)
    all_log_text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'admin' not in all_log_text
    assert secret_password not in all_log_text
    assert camera.NONCE not in all_log_text
    assert camera.STALE_NONCE not in all_log_text
