"""Regression coverage for the confirmed-live release blocker: ANYAICAM_ENV=
production correctly activates cloud_config.Settings.validate()'s hardening
checks (HTTPS-only, secure cookies, CSRF, HTTPS URLs/origins, strong
secrets), but those checks had no concept of ANYAICAM_RUNTIME_ROLE=edge --
an appliance reached over a private LAN/Tailscale network, never the public
internet. Confirmed live on Samsung: correctly stamping ANYAICAM_ENV=
production (this session's own installer fix) made the VMS process refuse
to start at all, because the plain-HTTP, Tailscale-only edge deployment
could never satisfy internet-facing HTTPS/cookie/CSRF requirements it was
never designed to meet.

Settings.edge_production (cloud_config.py) is now the one, narrowly-scoped
exemption: production + RUNTIME_ROLE=edge skips ONLY the HTTPS-termination-
dependent checks (secure cookies, CSRF, HTTPS-scheme URLs/origins, HTTPS-
only mode, HTTPS login URLs). It does NOT skip the strong/non-default
application secret requirement, and it does not affect staging at all,
regardless of runtime_role. Cloud/combined production is completely
unchanged -- every check below proves it still enforces exactly what it
did before.

Settings is a frozen dataclass whose field DEFAULTS read os.environ at
class-definition time (i.e. once, at module import) -- these tests never
touch os.environ or reimport the module; every scenario is built by
passing explicit constructor arguments directly, which always override
those baked-in defaults.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_config import Settings  # noqa: E402


STRONG_SECRET = "a" * 40  # >=32 chars, not one of the known default/placeholder values
WEAK_SECRET = "local-development-secret-change-me"

HTTPS_URL_KWARGS = dict(
    public_portal_url="https://portal.example.test",
    appliance_api_url="https://api.example.test",
    public_website_url="https://www.example.test",
    portal_url="https://portal.example.test",
    api_base_url="https://api.example.test/api/v1",
    password_reset_url="https://portal.example.test/reset-password",
    invitation_url="https://portal.example.test/invite",
    appliance_activation_url="https://portal.example.test/activate",
    production_partner_url="https://partner.example.test",
    production_customer_url="https://customer.example.test",
)


def _cloud_production(**overrides):
    kwargs = dict(
        environment="production",
        runtime_role="cloud",
        https_only=True,
        secure_cookies=True,
        csrf_enabled=True,
        allowed_origins=["https://www.example.test"],
        app_secrets=[STRONG_SECRET],
        **HTTPS_URL_KWARGS,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _edge_production(**overrides):
    # Deliberately the appliance's real defaults: plain HTTP, no secure
    # cookies, no CSRF, localhost URLs -- exactly what a fresh Samsung-
    # style install actually looks like.
    kwargs = dict(
        environment="production",
        runtime_role="edge",
        https_only=False,
        secure_cookies=False,
        csrf_enabled=False,
        app_secrets=[STRONG_SECRET],
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


class CloudProductionRemainsStrictTests(unittest.TestCase):
    """Every one of these must still fail exactly as before -- proves the
    edge exemption never leaks into a cloud/combined deployment."""

    def test_fully_compliant_cloud_production_passes(self):
        _cloud_production().validate()  # must not raise

    def test_cloud_production_still_requires_https_only(self):
        with self.assertRaisesRegex(RuntimeError, "HTTPS-only"):
            _cloud_production(https_only=False).validate()

    def test_cloud_production_still_requires_secure_cookies_and_csrf(self):
        with self.assertRaisesRegex(RuntimeError, "secure cookies and CSRF"):
            _cloud_production(secure_cookies=False).validate()
        with self.assertRaisesRegex(RuntimeError, "secure cookies and CSRF"):
            _cloud_production(csrf_enabled=False).validate()

    def test_cloud_production_still_requires_https_urls(self):
        with self.assertRaisesRegex(RuntimeError, "public URLs must use HTTPS"):
            _cloud_production(portal_url="http://portal.example.test").validate()

    def test_cloud_production_still_requires_https_origins(self):
        with self.assertRaisesRegex(RuntimeError, "origins must use HTTPS"):
            _cloud_production(allowed_origins=["http://www.example.test"]).validate()

    def test_cloud_production_still_requires_https_login_urls(self):
        with self.assertRaisesRegex(RuntimeError, "login URLs must use HTTPS"):
            _cloud_production(production_partner_url="http://partner.example.test").validate()

    def test_cloud_production_still_requires_strong_secrets(self):
        with self.assertRaisesRegex(RuntimeError, "Replace default or short"):
            _cloud_production(app_secrets=[WEAK_SECRET]).validate()
        with self.assertRaisesRegex(RuntimeError, "Replace default or short"):
            _cloud_production(app_secrets=["short"]).validate()

    def test_staging_is_unaffected_by_runtime_role(self):
        # Staging must stay exactly as strict regardless of runtime_role --
        # the edge exemption is scoped to production only.
        with self.assertRaisesRegex(RuntimeError, "secure cookies and CSRF"):
            Settings(
                environment="staging", runtime_role="edge",
                secure_cookies=False, csrf_enabled=False,
                app_secrets=[STRONG_SECRET],
            ).validate()


class EdgeProductionProfileTests(unittest.TestCase):
    """The actual fix: an edge appliance's real, unmodified defaults
    (plain HTTP, no secure cookies, no CSRF, localhost URLs) must be able
    to start in ANYAICAM_ENV=production -- confirmed live-failing on
    Samsung before this fix, confirmed passing after it."""

    def test_edge_production_with_real_defaults_starts(self):
        _edge_production().validate()  # must not raise

    def test_edge_production_does_not_require_https_only(self):
        Settings(
            environment="production", runtime_role="edge",
            https_only=False, secure_cookies=True, csrf_enabled=True,
            app_secrets=[STRONG_SECRET],
        ).validate()

    def test_edge_production_does_not_require_secure_cookies_or_csrf(self):
        _edge_production(secure_cookies=False, csrf_enabled=False).validate()

    def test_edge_production_does_not_require_https_urls_or_origins(self):
        Settings(
            environment="production", runtime_role="edge",
            https_only=False, secure_cookies=False, csrf_enabled=False,
            allowed_origins=["http://100.123.115.65:8000"],
            app_secrets=[STRONG_SECRET],
        ).validate()

    def test_edge_production_still_requires_strong_secrets(self):
        # The one requirement edge NEVER gets to skip.
        with self.assertRaisesRegex(RuntimeError, "Replace default or short"):
            _edge_production(app_secrets=[WEAK_SECRET]).validate()
        with self.assertRaisesRegex(RuntimeError, "Replace default or short"):
            _edge_production(app_secrets=["short"]).validate()

    def test_edge_production_property_is_false_outside_production(self):
        self.assertFalse(Settings(environment="staging", runtime_role="edge").edge_production)
        self.assertFalse(Settings(environment="development", runtime_role="edge").edge_production)

    def test_edge_production_property_is_false_for_cloud_runtime_role(self):
        self.assertFalse(Settings(environment="production", runtime_role="cloud").edge_production)
        self.assertFalse(Settings(environment="production", runtime_role="combined").edge_production)


if __name__ == "__main__":
    unittest.main()
