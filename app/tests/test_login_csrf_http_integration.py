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
_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_probe.bind(('127.0.0.1', 0))
PORT = _probe.getsockname()[1]
_probe.close()
ORIGIN = f'http://127.0.0.1:{PORT}'
os.environ.setdefault('ANYAICAM_ALLOWED_ORIGINS', ORIGIN)

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

import cloud_security
from cloud_config import settings


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

    def __init__(self):
        super().__init__(daemon=True)
        config = uvicorn.Config(app, host='127.0.0.1', port=PORT, log_level='warning')
        self.server = uvicorn.Server(config)

    def run(self):
        import asyncio
        asyncio.run(self.server.serve())

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
        cls.thread = _ServerThread()
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

    def assertReachedLoginHandler(self, status, body):
        # The CSRF gate must not have intercepted the request. Any outcome
        # other than the middleware's own 403 means the request passed
        # through to the stand-in handler.
        if status == 403:
            self.assertNotIn(b'CSRF validation failed', body)

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
        self.assertReachedLoginHandler(status, body)

    def test_get_cookie_then_post_header_round_trip_passes(self):
        # Same round trip via the pre-existing X-CSRF-Token header flow
        # (the app's global fetch wrapper reads document.cookie the same
        # way) -- must stay working.
        jar = CookieJar()
        self._get_login(jar)
        token = self._jar_cookie_value(jar)
        status, body = self._post_login(jar, header_token=token)
        self.assertReachedLoginHandler(status, body)

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
        self.assertReachedLoginHandler(status, body)

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


if __name__ == '__main__':
    unittest.main()
