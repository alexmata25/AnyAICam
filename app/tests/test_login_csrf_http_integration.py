import dataclasses
import hashlib
import hmac as hmac_mod
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from base64 import urlsafe_b64encode
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request as UrlRequest, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Same convention as test_login_csrf.py: pin the target DB and CSRF env
# before the first `import partner_db` (pulled in transitively).
_IMPORT_TIME_DB = Path(tempfile.gettempdir()) / 'anyaicam-login-csrf-http.db'
_IMPORT_TIME_DB.unlink(missing_ok=True)
os.environ.setdefault('ANYAICAM_DATABASE_BACKEND', 'sqlite')
os.environ['ANYAICAM_PARTNER_DB'] = str(_IMPORT_TIME_DB)
os.environ.setdefault('ANYAICAM_CSRF_ENABLED', 'true')

# Reserve a free loopback port and lock the origin allowlist to it *before*
# cloud_config.settings (a frozen, import-time singleton) is constructed.
# The socket stays open and is handed directly to uvicorn (Server.serve(sockets=...))
# instead of being closed and reopened by port number later -- closing and
# reopening leaves a window where another process on the host can grab the
# same ephemeral port before the server thread gets to it.
_bound_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_bound_socket.bind(('127.0.0.1', 0))
PORT = _bound_socket.getsockname()[1]
ORIGIN = f'http://127.0.0.1:{PORT}'
os.environ.setdefault('ANYAICAM_ALLOWED_ORIGINS', ORIGIN)

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

import cloud_security
from cloud_config import settings
from cloud_security import _MAX_CSRF_FORM_BODY_BYTES


async def _login_get(request):
    return HTMLResponse('<form method="post" action="/login"></form>')


async def _login_post(request):
    # Stands in for the real /login handler, which itself does
    # `email: str = Form(...), password: str = Form(...)` (app/main.py).
    # Parsing form() here -- the same body FastAPI's own Form(...)
    # resolution would read -- is the thing under test: if the CSRF
    # middleware upstream already drained the body via .stream() without
    # replaying it, these come back empty even though the client sent
    # real values.
    form = await request.form()
    email = form.get('email')
    password = form.get('password')
    return PlainTextResponse(f'email={email} password={password}')


app = Starlette(routes=[
    Route('/login', _login_get, methods=['GET']),
    Route('/login', _login_post, methods=['POST']),
])
app.add_middleware(cloud_security.ProductionSecurityMiddleware)


def _make_cookie(name, value, origin):
    host = origin.split('://', 1)[1].split(':')[0]
    return Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=host, domain_specified=True, domain_initial_dot=False,
        path='/', path_specified=True, secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )


class _ServerThread(threading.Thread):
    """Runs the real ASGI app over a real TCP socket, so Set-Cookie
    quoting and Cookie-header round-tripping go through the actual stdlib
    http.cookies machinery -- not a hand-built Request object."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self._sock = sock
        config = uvicorn.Config(app, log_level='warning')
        self.server = uvicorn.Server(config)

    def run(self):
        import asyncio
        # Pass the already-bound socket straight through (the same mechanism
        # uvicorn documents for sharing sockets with a Gunicorn worker) so
        # there's no separate bind() here that could race with anything else
        # on the host for this port number.
        asyncio.run(self.server.serve(sockets=[self._sock]))

    def wait_ready(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self.server, 'started', False):
                return
            time.sleep(0.05)
        raise RuntimeError('test server did not start in time')

    def stop(self):
        self.server.should_exit = True
        self.join(timeout=5)
        self._sock.close()


class LoginCsrfHttpIntegrationTests(unittest.TestCase):
    # cloud_config.settings is a process-wide frozen-dataclass singleton
    # constructed the first time any module imports cloud_config. When this
    # file runs alongside other app/tests modules that import cloud_config
    # before this file's own os.environ writes take effect, the cached
    # singleton could have csrf_enabled=False or the wrong allowed_origins
    # regardless of this file's env setup. Swap in a patched copy for the
    # duration of the whole test class, same pattern as test_login_csrf.py.
    @classmethod
    def setUpClass(cls):
        patched = dataclasses.replace(
            cloud_security.settings,
            csrf_enabled=True,
            allowed_origins=[ORIGIN],
        )
        cls._settings_patch = patch.object(cloud_security, 'settings', patched)
        cls._settings_patch.start()
        cls.thread = _ServerThread(_bound_socket)
        cls.thread.start()
        cls.thread.wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.thread.stop()
        cls._settings_patch.stop()

    def _get_login(self, jar):
        opener = build_opener(HTTPCookieProcessor(jar))
        opener.open(UrlRequest(f'{ORIGIN}/login', headers={'Origin': ORIGIN})).close()

    def _jar_cookie_value(self, jar, name='anyaicam_csrf'):
        # Read the value exactly as a browser's document.cookie / curl's
        # cookie jar would hand it to client-side JS -- quotes included if
        # the server happened to send any.
        for cookie in jar:
            if cookie.name == name:
                return cookie.value
        return None

    def _post_login(self, jar, *, form_token=None, header_token=None, raw_body=None):
        opener = build_opener(HTTPCookieProcessor(jar))
        headers = {'Origin': ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded'}
        if header_token is not None:
            headers['X-CSRF-Token'] = header_token
        if raw_body is not None:
            body = raw_body.encode()
        else:
            body = f'csrf_token={form_token}'.encode() if form_token is not None else b''
        req = UrlRequest(f'{ORIGIN}/login', data=body, headers=headers, method='POST')
        try:
            resp = opener.open(req)
            return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def test_get_cookie_then_post_form_field_round_trip_passes(self):
        # The exact repro: fresh cookie jar, GET /login, copy the cookie
        # value into the hidden form field the way the login page's own JS
        # does, POST /login. Regression-tests the real Set-Cookie/Cookie
        # header round trip that hand-built Request objects skip.
        jar = CookieJar()
        self._get_login(jar)
        token = self._jar_cookie_value(jar)
        self.assertIsNotNone(token)
        status, body = self._post_login(jar, form_token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'email=None password=None')

    def test_get_cookie_then_post_header_round_trip_passes(self):
        # Same round trip via the pre-existing X-CSRF-Token header flow
        # (the app's global fetch wrapper reads document.cookie the same
        # way) -- must stay working.
        jar = CookieJar()
        self._get_login(jar)
        token = self._jar_cookie_value(jar)
        status, body = self._post_login(jar, header_token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'email=None password=None')

    def test_legacy_padded_quoted_cookie_still_verifies(self):
        # Simulates a cookie issued by the pre-fix server: base64 padding
        # intact, so Python's stdlib http.cookies quoted it on the way out,
        # and the client still holds the quoted value in its store.
        expires = int(time.time()) + 3600
        payload = f'{expires}:csrf'
        signature = hmac_mod.new(settings.app_secrets[0].encode(), payload.encode(), hashlib.sha256).hexdigest()
        legacy_token = urlsafe_b64encode(f'{payload}:{signature}'.encode()).decode()
        self.assertTrue(legacy_token.endswith('='), 'fixture must exercise the padded case')
        quoted = f'"{legacy_token}"'

        jar = CookieJar()
        jar.set_cookie(_make_cookie('anyaicam_csrf', quoted, ORIGIN))
        status, body = self._post_login(jar, form_token=quoted)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'email=None password=None')

    def test_tampered_token_is_still_rejected(self):
        jar = CookieJar()
        self._get_login(jar)
        token = self._jar_cookie_value(jar)
        tampered = token[:-2] + ('xx' if not token.endswith('xx') else 'yy')
        status, body = self._post_login(jar, form_token=tampered)
        self.assertEqual(status, 403)
        self.assertIn(b'CSRF validation failed', body)

    def test_form_csrf_fallback_preserves_body_for_downstream_form_fields(self):
        # Regression for the BaseHTTPMiddleware body-consumption trap: the
        # CSRF middleware's own await request.form() (used to pull
        # csrf_token out of the urlencoded body) must not leave the
        # downstream handler's own form() read with an empty body.
        # Starlette's BaseHTTPMiddleware only replays a body downstream
        # that the middleware read via .body(); a bare .form() call reads
        # via .stream() instead, which is NOT replayed, so a naive
        # implementation makes every real login submission look empty to
        # the /login handler even after the CSRF check itself passes.
        jar = CookieJar()
        self._get_login(jar)
        token = self._jar_cookie_value(jar)
        raw_body = f'email=test%40example.invalid&password=notarealpassword&csrf_token={token}'
        status, body = self._post_login(jar, raw_body=raw_body)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'email=test@example.invalid password=notarealpassword')

    def test_oversized_cookieless_body_is_rejected_without_buffering(self):
        # No anyaicam_csrf cookie at all, so the CSRF check can never pass --
        # proven here by declaring a Content-Length far beyond
        # _MAX_CSRF_FORM_BODY_BYTES and never actually sending that many
        # bytes. If the middleware tried to read/buffer the body before
        # checking for the cookie, this would hang waiting for bytes that
        # never arrive; getting a prompt 403 instead proves the
        # reject-before-buffer ordering.
        host, port_str = ORIGIN.split('://', 1)[1].split(':')
        port = int(port_str)
        declared_length = _MAX_CSRF_FORM_BODY_BYTES * 100
        request_head = (
            f'POST /login HTTP/1.1\r\n'
            f'Host: {host}:{port}\r\n'
            f'Origin: {ORIGIN}\r\n'
            f'Content-Type: application/x-www-form-urlencoded\r\n'
            f'Content-Length: {declared_length}\r\n'
            f'Connection: close\r\n'
            f'\r\n'
        ).encode()
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(request_head)
            # Deliberately do not send the declared_length bytes of body.
            sock.settimeout(5)
            response = b''
            try:
                # "Connection: close" means the server closes the socket
                # after writing the response, so read until EOF to capture
                # the full response (status line, headers, and body).
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            except TimeoutError:
                self.fail('server did not respond promptly -- it likely blocked reading the body')
        status_line = response.split(b'\r\n', 1)[0]
        self.assertIn(b'403', status_line)
        self.assertIn(b'CSRF validation failed', response)

    def test_chunked_form_under_limit_passes_and_preserves_body(self):
        # No Content-Length at all (Transfer-Encoding: chunked) must not be
        # treated as disqualifying on its own -- only an over-cap body is.
        jar = CookieJar()
        self._get_login(jar)
        token = self._jar_cookie_value(jar)
        cookie_header = f'anyaicam_csrf={token}'
        form_body = f'email=test%40example.invalid&password=notarealpassword&csrf_token={token}'.encode()
        host, port_str = ORIGIN.split('://', 1)[1].split(':')
        port = int(port_str)
        chunk = b'%x\r\n' % len(form_body) + form_body + b'\r\n0\r\n\r\n'
        request_head = (
            f'POST /login HTTP/1.1\r\n'
            f'Host: {host}:{port}\r\n'
            f'Origin: {ORIGIN}\r\n'
            f'Cookie: {cookie_header}\r\n'
            f'Content-Type: application/x-www-form-urlencoded\r\n'
            f'Transfer-Encoding: chunked\r\n'
            f'Connection: close\r\n'
            f'\r\n'
        ).encode()
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(request_head + chunk)
            sock.settimeout(5)
            response = b''
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                response += piece
        status_line = response.split(b'\r\n', 1)[0]
        self.assertTrue(status_line.startswith(b'HTTP/1.1 200'), status_line)
        self.assertIn(b'email=test@example.invalid password=notarealpassword', response)

    def test_oversized_chunked_body_is_rejected_without_unbounded_buffering(self):
        # A valid, matching cookie this time -- isolates the size-cap check
        # from the no-cookie short-circuit that
        # test_oversized_cookieless_body_is_rejected_without_buffering covers.
        jar = CookieJar()
        self._get_login(jar)
        token = self._jar_cookie_value(jar)
        cookie_header = f'anyaicam_csrf={token}'
        host, port_str = ORIGIN.split('://', 1)[1].split(':')
        port = int(port_str)
        request_head = (
            f'POST /login HTTP/1.1\r\n'
            f'Host: {host}:{port}\r\n'
            f'Origin: {ORIGIN}\r\n'
            f'Cookie: {cookie_header}\r\n'
            f'Content-Type: application/x-www-form-urlencoded\r\n'
            f'Transfer-Encoding: chunked\r\n'
            f'Connection: close\r\n'
            f'\r\n'
        ).encode()
        # 9 complete 8192-byte chunks = 73728 bytes, over the 65536 cap. Send
        # them as complete, validly-encoded chunks and then simply stop --
        # no terminating 0-length chunk -- to prove the server responded
        # before demanding the rest of the stream.
        filler = b'a' * 8192
        one_chunk = b'2000\r\n' + filler + b'\r\n'  # 0x2000 == 8192
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(request_head)
            for _ in range(9):
                sock.sendall(one_chunk)
            sock.settimeout(5)
            response = b''
            try:
                while True:
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    response += piece
            except TimeoutError:
                self.fail('server did not respond promptly -- it likely blocked reading the chunked body')
        status_line = response.split(b'\r\n', 1)[0]
        self.assertTrue(status_line.startswith(b'HTTP/1.1 403'), status_line)
        self.assertIn(b'CSRF validation failed', response)


if __name__ == '__main__':
    unittest.main()
