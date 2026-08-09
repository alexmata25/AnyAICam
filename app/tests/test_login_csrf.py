import asyncio
import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Match the convention every other partner_db-touching test file uses: point
# ANYAICAM_PARTNER_DB at a writable temp path before the first `import
# partner_db` (pulled in transitively by cloud_security), since that import
# triggers schema initialization against whatever target is configured then.
_IMPORT_TIME_DB = Path(tempfile.gettempdir()) / 'anyaicam-login-csrf-test.db'
_IMPORT_TIME_DB.unlink(missing_ok=True)
os.environ.setdefault('ANYAICAM_DATABASE_BACKEND', 'sqlite')
os.environ['ANYAICAM_PARTNER_DB'] = str(_IMPORT_TIME_DB)
os.environ.setdefault('ANYAICAM_CSRF_ENABLED', 'true')
os.environ.setdefault('ANYAICAM_ALLOWED_ORIGINS', 'http://localhost:8000,http://127.0.0.1:8000')

from starlette.requests import Request
from starlette.responses import PlainTextResponse

import cloud_security
from cloud_security import ProductionSecurityMiddleware
from token_security import sign


def make_request(headers, body=b''):
    """Build a bare Request for exercising the middleware in isolation,
    without booting the full ASGI app or a real HTTP client."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        'type': 'http', 'method': 'POST', 'path': '/login', 'headers': raw_headers,
        'query_string': b'', 'server': ('localhost', 8000), 'scheme': 'http',
        'client': ('127.0.0.1', 12345),
    }
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        consumed = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


async def call_next_ok(request):
    return PlainTextResponse('ok')


def dispatch(headers, body=b''):
    request = make_request(headers, body)
    middleware = ProductionSecurityMiddleware(app=None)
    return asyncio.run(middleware.dispatch(request, call_next_ok))


class LoginCsrfTests(unittest.TestCase):
    # cloud_config.settings is a process-wide frozen-dataclass singleton
    # constructed the first time any module imports cloud_config. When this
    # file runs alongside other app/tests modules that import cloud_config
    # (e.g. via database_backend) before ANYAICAM_CSRF_ENABLED is set, the
    # cached singleton would otherwise have csrf_enabled=False regardless of
    # this file's own os.environ writes. Swap in a patched copy for the
    # duration of each test, so results don't depend on test discovery/import
    # order.
    def setUp(self):
        patched = dataclasses.replace(
            cloud_security.settings,
            csrf_enabled=True,
            allowed_origins=['http://localhost:8000', 'http://127.0.0.1:8000'],
        )
        self._settings_patch = patch.object(cloud_security, 'settings', patched)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)

    def test_missing_csrf_token_is_rejected(self):
        # No cookie, no header, no form field at all.
        response = dispatch({'origin': 'http://localhost:8000'})
        self.assertEqual(response.status_code, 403)

    def test_mismatched_csrf_token_is_rejected(self):
        cookie = sign('csrf', 28800)
        tampered = cookie[:-2] + ('xx' if not cookie.endswith('xx') else 'yy')
        self.assertNotEqual(cookie, tampered)
        response = dispatch({
            'origin': 'http://localhost:8000',
            'cookie': f'anyaicam_csrf={cookie}',
            'x-csrf-token': tampered,
        })
        self.assertEqual(response.status_code, 403)

    def test_valid_html_form_csrf_field_passes_middleware(self):
        # Mirrors a plain <form method="post"> submission from the real
        # /login page: no X-CSRF-Token header, token carried in the
        # urlencoded body's csrf_token field instead.
        token = sign('csrf', 28800)
        body = f'email=test%40example.invalid&password=secret&csrf_token={token}'.encode()
        response = dispatch({
            'origin': 'http://localhost:8000',
            'cookie': f'anyaicam_csrf={token}',
            'content-type': 'application/x-www-form-urlencoded',
            'content-length': str(len(body)),
        }, body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'ok')

    def test_valid_x_csrf_token_header_still_passes_middleware(self):
        # Existing fetch/API header-based double-submit flow must be untouched.
        token = sign('csrf', 28800)
        response = dispatch({
            'origin': 'http://localhost:8000',
            'cookie': f'anyaicam_csrf={token}',
            'x-csrf-token': token,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'ok')

    def test_login_page_wires_hidden_csrf_field_to_cookie(self):
        # Ties the isolated middleware behavior above back to the real
        # production login template without booting the full app.
        source = (ROOT / 'main.py').read_text(encoding='utf-8')
        self.assertIn('action="/login" id="login-form"', source)
        self.assertIn('<input type="hidden" name="csrf_token" value="">', source)
        self.assertIn("anyaicam_csrf=([^;]*)", source)
        self.assertIn("this.csrf_token.value=decodeURIComponent(match[1])", source)


if __name__ == '__main__':
    unittest.main()
