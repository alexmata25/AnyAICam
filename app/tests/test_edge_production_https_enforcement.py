"""Regression coverage for the confirmed-live release blocker: with
ANYAICAM_ENV=production stamped by this session's own installer fix, the
Samsung appliance -- plain HTTP, no TLS listener, reached only over a
private LAN/Tailscale network -- started 307-redirecting every request to
https:// and sending Strict-Transport-Security, locking every browser
that ever loaded the page out of the plain-HTTP address it actually needs.

Two independent, previously non-edge-aware mechanisms caused this:

  1. main.py's FORCE_HTTPS module constant defaulted to True for ANY
     ANYAICAM_ENV=production, regardless of ANYAICAM_RUNTIME_ROLE. It now
     goes through _default_force_https(), which is False by default for
     edge_production (production + runtime_role=="edge") and True for
     every other production profile, exactly as before. An operator who
     explicitly sets ANYAICAM_FORCE_HTTPS is always honored on any
     profile.

  2. cloud_security.ProductionSecurityMiddleware unconditionally set
     Strict-Transport-Security whenever settings.production was true. It
     now also excludes edge_production, matching the same scoping
     cloud_config.Settings.edge_production already uses for every other
     HTTPS-termination-dependent check.

Cloud/combined production is unaffected by either fix and keeps forcing
HTTPS and sending HSTS exactly as before.
"""
import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import cloud_security  # noqa: E402
from cloud_security import ProductionSecurityMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402


class DefaultForceHttpsTests(unittest.TestCase):
    """The pure default-computation function -- exercised directly so
    this doesn't need to reimport main.py under different environment
    variables."""

    def test_cloud_production_defaults_to_forcing_https(self):
        self.assertTrue(main._default_force_https("production", "cloud"))

    def test_combined_production_defaults_to_forcing_https(self):
        self.assertTrue(main._default_force_https("production", "combined"))

    def test_edge_production_defaults_to_not_forcing_https(self):
        # The actual fix: a fresh edge appliance's real defaults must not
        # force HTTPS it has no TLS listener to serve.
        self.assertFalse(main._default_force_https("production", "edge"))

    def test_staging_is_unaffected_regardless_of_runtime_role(self):
        self.assertFalse(main._default_force_https("staging", "edge"))
        self.assertFalse(main._default_force_https("staging", "cloud"))

    def test_development_is_unaffected_regardless_of_runtime_role(self):
        self.assertFalse(main._default_force_https("development", "edge"))
        self.assertFalse(main._default_force_https("development", "cloud"))


class ForwardedHttpsMiddlewareTests(unittest.TestCase):
    """Isolated behavior of forwarded_https_middleware itself, driven by
    monkeypatching the already-computed main.FORCE_HTTPS constant --
    proves the actual request-handling behavior for each state, not just
    the default-computation helper above."""

    @staticmethod
    def _make_request(path="/partner-login", scheme="http", headers=None):
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {
            "type": "http", "method": "GET", "path": path, "headers": raw_headers,
            "query_string": b"", "server": ("192.168.0.165", 8000), "scheme": scheme,
            "client": ("192.168.0.50", 12345),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        return Request(scope, receive)

    @staticmethod
    async def _call_next_ok(request):
        return PlainTextResponse("ok")

    def _dispatch(self, **request_kwargs):
        request = self._make_request(**request_kwargs)
        return asyncio.run(main.forwarded_https_middleware(request, self._call_next_ok))

    def test_edge_production_plain_http_is_not_redirected(self):
        # The exact live Samsung symptom: a plain-HTTP LAN/Tailscale
        # request to an edge appliance must reach the app, not bounce to
        # a scheme it never serves.
        with patch.object(main, "FORCE_HTTPS", False):
            response = self._dispatch(headers={"host": "192.168.0.165:8000"})
        self.assertEqual(response.status_code, 200)

    def test_cloud_production_plain_http_is_still_redirected_to_https(self):
        # Must stay exactly as strict as before -- proves the edge fix
        # never leaks into a cloud/combined deployment.
        with patch.object(main, "FORCE_HTTPS", True):
            response = self._dispatch(headers={"host": "portal.anyaicam.com"})
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://portal.anyaicam.com/partner-login")

    def test_health_endpoints_are_never_redirected_even_when_forced(self):
        with patch.object(main, "FORCE_HTTPS", True):
            response = self._dispatch(path="/health", headers={"host": "portal.anyaicam.com"})
        self.assertEqual(response.status_code, 200)


class EdgeProductionHstsTests(unittest.TestCase):
    """cloud_security.ProductionSecurityMiddleware's HSTS header, isolated
    the same way test_login_csrf.py exercises this middleware."""

    @staticmethod
    def _make_request(headers=None):
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {
            "type": "http", "method": "GET", "path": "/partner-login", "headers": raw_headers,
            "query_string": b"", "server": ("192.168.0.165", 8000), "scheme": "http",
            "client": ("192.168.0.50", 12345),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        return Request(scope, receive)

    @staticmethod
    async def _call_next_ok(request):
        return PlainTextResponse("ok")

    def _dispatch_with_settings(self, patched_settings):
        request = self._make_request()
        middleware = ProductionSecurityMiddleware(app=None)
        with patch.object(cloud_security, "settings", patched_settings):
            return asyncio.run(middleware.dispatch(request, self._call_next_ok))

    def test_edge_production_does_not_send_hsts_over_plain_http(self):
        # The exact live Samsung symptom: HSTS on a plain-HTTP-only
        # appliance permanently locks a browser out of it.
        edge = dataclasses.replace(
            cloud_security.settings,
            environment="production", runtime_role="edge",
            csrf_enabled=False, allowed_origins=[],
        )
        response = self._dispatch_with_settings(edge)
        self.assertNotIn("strict-transport-security", response.headers)

    def test_cloud_production_still_sends_hsts(self):
        cloud = dataclasses.replace(
            cloud_security.settings,
            environment="production", runtime_role="cloud",
            csrf_enabled=False, allowed_origins=[],
        )
        response = self._dispatch_with_settings(cloud)
        self.assertEqual(response.headers["strict-transport-security"], "max-age=31536000; includeSubDomains")

    def test_staging_is_unaffected_regardless_of_runtime_role(self):
        staging = dataclasses.replace(
            cloud_security.settings,
            environment="staging", runtime_role="edge",
            csrf_enabled=False, allowed_origins=[],
        )
        response = self._dispatch_with_settings(staging)
        self.assertNotIn("strict-transport-security", response.headers)


if __name__ == "__main__":
    unittest.main()
