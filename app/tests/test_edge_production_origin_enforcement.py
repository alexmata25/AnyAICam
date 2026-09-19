"""Regression coverage for the confirmed-live release blocker: the real
Partner Portal Admin login POST from a browser at
http://192.168.0.165:8000/partner.html was rejected with "Origin is not
allowed", because ANYAICAM_ALLOWED_ORIGINS was never set by the installer
and the untouched default (http://localhost:8000) matches no origin a
browser actually loads an edge appliance's own pages from -- the exact
same shape of bug as effective_trusted_hosts (see
test_cloud_config_edge_production.py): an edge appliance has no fixed
address to enumerate in advance, unlike cloud's single fixed public
domain.

Settings.effective_allowed_origins (cloud_config.py) is the fix:
edge_production with allowed_origins still at its exact untouched default
gets ["*"], which cloud_security.ProductionSecurityMiddleware's origin
check (cloud_security.py) treats as "any origin accepted". Cloud/combined
production, staging, and any profile with an explicit ANYAICAM_ALLOWED_
ORIGINS override are all completely unaffected.
"""
import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cloud_security  # noqa: E402
from cloud_security import ProductionSecurityMiddleware  # noqa: E402
from cloud_config import Settings  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402


STRONG_SECRET = "a" * 40


def _edge_production(**overrides):
    kwargs = dict(
        environment="production", runtime_role="edge",
        https_only=False, secure_cookies=False, csrf_enabled=False,
        app_secrets=[STRONG_SECRET],
        # allowed_origins uses field(default_factory=...) (cloud_config.py),
        # which -- unlike most other Settings fields -- re-reads
        # os.environ on every construction that omits this kwarg, not
        # only once at module import. Passing it explicitly here (the
        # exact untouched-default shape) keeps this helper's "untouched
        # default" scenario deterministic regardless of what any other
        # test module in the same process has done to os.environ, per
        # this file's own convention: explicit constructor arguments
        # always override baked-in defaults.
        allowed_origins=["http://localhost:8000"],
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _cloud_production(**overrides):
    kwargs = dict(
        environment="production", runtime_role="cloud",
        allowed_origins=["https://portal.anyaicam.com"],
        app_secrets=[STRONG_SECRET],
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class EffectiveAllowedOriginsTests(unittest.TestCase):
    def test_edge_production_with_untouched_default_allows_any_origin(self):
        self.assertEqual(_edge_production().effective_allowed_origins, ["*"])

    def test_cloud_production_with_configured_origin_is_unaffected(self):
        self.assertEqual(
            _cloud_production().effective_allowed_origins,
            ["https://portal.anyaicam.com"],
        )

    def test_cloud_production_with_untouched_default_is_unaffected(self):
        self.assertEqual(
            Settings(
                environment="production", runtime_role="cloud", app_secrets=[STRONG_SECRET],
                allowed_origins=["http://localhost:8000"],  # see _edge_production()'s comment above
            ).effective_allowed_origins,
            ["http://localhost:8000"],
        )

    def test_edge_production_honors_an_explicit_override(self):
        self.assertEqual(
            _edge_production(allowed_origins=["http://vms.internal.example"]).effective_allowed_origins,
            ["http://vms.internal.example"],
        )

    def test_staging_is_unaffected_regardless_of_runtime_role(self):
        self.assertEqual(
            Settings(
                environment="staging", runtime_role="edge",
                allowed_origins=["http://localhost:8000"],  # see _edge_production()'s comment above
            ).effective_allowed_origins,
            ["http://localhost:8000"],
        )


class ProductionSecurityMiddlewareOriginTests(unittest.TestCase):
    """Exercises the real ProductionSecurityMiddleware.dispatch() origin
    gate with concrete Origin header values, isolated the same way
    test_login_csrf.py exercises this same middleware."""

    @staticmethod
    def _make_request(origin, method="POST"):
        headers = {"origin": origin} if origin else {}
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        scope = {
            "type": "http", "method": method, "path": "/login", "headers": raw_headers,
            "query_string": b"", "server": ("192.168.0.165", 8000), "scheme": "http",
            "client": ("192.168.0.50", 12345),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        return Request(scope, receive)

    @staticmethod
    async def _call_next_ok(request):
        return PlainTextResponse("ok")

    def _dispatch(self, patched_settings, origin, method="POST"):
        request = self._make_request(origin, method)
        middleware = ProductionSecurityMiddleware(app=None)
        with patch.object(cloud_security, "settings", patched_settings):
            return asyncio.run(middleware.dispatch(request, self._call_next_ok))

    def test_edge_production_accepts_the_appliances_own_lan_origin(self):
        # The exact live failure: the real Admin login POST from the
        # appliance's own LAN address.
        response = self._dispatch(_edge_production(csrf_enabled=False), "http://192.168.0.165:8000")
        self.assertEqual(response.status_code, 200)

    def test_edge_production_accepts_the_appliances_own_tailscale_origin(self):
        response = self._dispatch(_edge_production(csrf_enabled=False), "http://100.123.115.65:8000")
        self.assertEqual(response.status_code, 200)

    def test_edge_production_accepts_an_arbitrary_origin(self):
        # Proves the fix is structural (edge_production + untouched
        # default), never an allowlist entry for one specific address.
        response = self._dispatch(_edge_production(csrf_enabled=False), "http://some-other-lan-host.example:8000")
        self.assertEqual(response.status_code, 200)

    def test_cloud_production_still_rejects_a_lan_looking_origin(self):
        # Must stay exactly as strict as before -- proves the edge fix
        # never leaks into a cloud/combined deployment.
        response = self._dispatch(_cloud_production(), "http://192.168.0.165:8000")
        self.assertEqual(response.status_code, 403)
        self.assertIn("not allowed", response.body.decode().lower())

    def test_cloud_production_accepts_its_own_configured_origin(self):
        response = self._dispatch(_cloud_production(), "https://portal.anyaicam.com")
        self.assertEqual(response.status_code, 200)

    def test_requests_without_an_origin_header_are_unaffected(self):
        # Ordinary same-origin browser navigation/form posts don't carry
        # an Origin header at all -- neither profile should ever gate on
        # this check for those.
        response = self._dispatch(_cloud_production(), origin=None)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
