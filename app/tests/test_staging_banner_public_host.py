"""The STAGING ENVIRONMENT banner follows the request host (2026-10-07).

The staging deployment answers on two hostnames: portal-staging.anyaicam.com
and app.anyaicam.com (STAGING_SECONDARY_PUBLIC_HOST), the address customers
sign in at. Customers on app.anyaicam.com were shown "STAGING ENVIRONMENT ·
Test data and services only" on every signed-in page. The banner now stays on
portal-staging and is hidden on app.anyaicam.com; ANYAICAM_ENV, and everything
it drives (Stripe mode, database, trusted hosts), is unchanged.

Settings is built with explicit constructor arguments (dataclasses.replace),
never by touching os.environ, as in test_cloud_config_edge_production.py.
"""
import dataclasses
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from cloud_config import STAGING_SECONDARY_PUBLIC_HOST, Settings  # noqa: E402

BANNER = "STAGING ENVIRONMENT"
STAGING_HOST = "portal-staging.anyaicam.com"


def _settings(environment):
    return dataclasses.replace(Settings(), environment=environment)


def test_public_host_is_app_anyaicam_com():
    assert STAGING_SECONDARY_PUBLIC_HOST == "app.anyaicam.com"


@pytest.mark.parametrize("host", ["app.anyaicam.com", "APP.AnyAiCam.com", "app.anyaicam.com:443", "app.anyaicam.com."])
def test_staging_hides_banner_on_the_customer_host(host):
    assert _settings("staging").shows_staging_banner(host) is False


@pytest.mark.parametrize("host", [STAGING_HOST, f"{STAGING_HOST}:443", "localhost:8000", "", None])
def test_staging_keeps_banner_everywhere_else(host):
    assert _settings("staging").shows_staging_banner(host) is True


@pytest.mark.parametrize("environment", ["production", "development", "local"])
@pytest.mark.parametrize("host", [STAGING_HOST, "app.anyaicam.com", None])
def test_non_staging_never_shows_banner(environment, host):
    assert _settings(environment).shows_staging_banner(host) is False


def test_the_environment_itself_stays_staging():
    settings = _settings("staging")
    settings.shows_staging_banner("app.anyaicam.com")
    assert settings.staging is True
    assert settings.environment == "staging"
    assert STAGING_SECONDARY_PUBLIC_HOST in settings.effective_trusted_hosts


def _render_shell(monkeypatch, host, environment="staging"):
    monkeypatch.setattr(main, "cloud_settings", _settings(environment))
    request = Request({"type": "http", "method": "GET", "path": "/dashboard", "query_string": b"",
                       "headers": [(b"host", host.encode())]})
    token = main.REQUEST_CONTEXT.set(request)
    try:
        return main.page_shell("Dashboard", "dashboard", "<p>page body</p>")
    finally:
        main.REQUEST_CONTEXT.reset(token)


def test_page_shell_hides_banner_on_app_anyaicam_com(monkeypatch):
    html = _render_shell(monkeypatch, "app.anyaicam.com")
    assert BANNER not in html
    assert "page body" in html


def test_page_shell_keeps_banner_on_portal_staging(monkeypatch):
    html = _render_shell(monkeypatch, STAGING_HOST)
    assert BANNER in html
    assert "Test data and services only" in html


def test_page_shell_shows_no_banner_in_production(monkeypatch):
    assert BANNER not in _render_shell(monkeypatch, STAGING_HOST, environment="production")
