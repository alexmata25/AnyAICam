"""Regression coverage for classify_rtsp_authentication() (provisioning.py):
the fix for OPTIONS-only credential "verification" silently passing a
camera that never actually challenged for auth -- confirmed live during
Samsung onboarding validation (camera at device_key ...f6af answered
OPTIONS 200 unauthenticated, so entered credentials were never actually
exercised). DESCRIBE is now used instead, since it's the request that
actually asks for the real media session description.

A real socket server (a minimal, single-connection RTSP responder run
in a background thread) stands in for the physical camera -- same style
this project already favors over hand-copied mocks (see
test_cloud_recording_mode_disabled.py's own docstring) -- so these tests
exercise the real socket code in provisioning.py, not a mocked
_rtsp_request(). No network dependency: everything binds to 127.0.0.1.
"""

import base64
import hashlib
import socket
import threading

import pytest

from anyaicam_agent import provisioning


class _FakeRtspCamera:
    """Listens on 127.0.0.1:<ephemeral>, accepts exactly one connection
    per handled request, and replies according to `mode`:
      - 'open': 200 to any DESCRIBE, no challenge ever issued.
      - 'basic': challenges with WWW-Authenticate: Basic on the first
        DESCRIBE, then checks the retry's Basic token against
        (username, password) and replies 200 or 401 accordingly.
      - 'digest': same shape, but with a real Digest challenge/response
        check (recomputes the expected response the same way a real
        camera would, using the same algorithm _digest_header() uses).
    """

    REALM = 'test-camera'
    NONCE = 'deadbeefcafefeed0123456789abcdef'

    def __init__(self, mode, username=None, password=None):
        self.mode = mode
        self.username = username
        self.password = password
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
        # Handles up to two sequential connections (initial probe +
        # authenticated retry) on the same ephemeral port, matching
        # classify_rtsp_authentication()'s real two-request flow.
        for _ in range(2):
            try:
                self.sock.settimeout(5)
                conn, _addr = self.sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(5)
                data = conn.recv(4096).decode(errors='ignore')
                self.requests_seen += 1
                conn.sendall(self._response_for(data).encode())
            finally:
                conn.close()

    def _response_for(self, request_text):
        auth_header = None
        for line in request_text.splitlines():
            if line.lower().startswith('authorization:'):
                auth_header = line.split(':', 1)[1].strip()
        if self.mode == 'open':
            return 'RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n'
        if auth_header is None:
            challenge = (f'Basic realm="{self.REALM}"' if self.mode == 'basic'
                         else f'Digest realm="{self.REALM}", nonce="{self.NONCE}"')
            return f'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: {challenge}\r\n\r\n'
        ok = self._credential_accepted(auth_header, request_text)
        return ('RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n' if ok else
                'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n')

    def _credential_accepted(self, auth_header, request_text):
        if self.mode == 'basic':
            token = base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
            return auth_header == f'Basic {token}'
        # digest: recompute the expected response the same way a real
        # camera / _digest_header() would, and compare.
        uri_match = None
        for line in request_text.splitlines():
            if line.startswith('DESCRIBE '):
                uri_match = line.split(' ')[1]
        ha1 = hashlib.md5(f'{self.username}:{self.REALM}:{self.password}'.encode()).hexdigest()
        ha2 = hashlib.md5(f'DESCRIBE:{uri_match}'.encode()).hexdigest()
        expected = hashlib.md5(f'{ha1}:{self.NONCE}:{ha2}'.encode()).hexdigest()
        return f'response="{expected}"' in auth_header


# --------------------------------------------------------------- the four states


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
