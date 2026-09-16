"""AnyAiCam e2e (Playwright) shared configuration.

Foundation only, established 2026-09-15 -- see
docs/AUTONOMOUS_VALIDATION_PERMISSIONS.md for the permission model this
whole directory operates under, and README.md for how to run it.

Three things this file is responsible for, and nothing else:

1. Resolving ANYAICAM_E2E_BASE_URL/USERNAME/PASSWORD from e2e/.env (via
   python-dotenv, never overriding a real environment variable that's
   already set -- see load_dotenv(override=False) below) or the real
   process environment, with NO default credential values and NO
   credential ever hard-coded here or anywhere else in this directory.
2. A hard safety rail: an explicit ALLOWED_STAGING_HOSTS allowlist, not
   a production blocklist (2026-09-16 -- an independent review found the
   original blocklist's case-sensitive substring check let
   `APP.ANYAICAM.COM` straight through; reproduced locally, fixed here,
   regression-tested in test_conftest_safety_rail.py). Any host not on
   the allowlist -- production, a typo, an unrecognized environment --
   fails closed by default. Refuses to run ANY test unless a human has
   explicitly opted in via ANYAICAM_E2E_ALLOW_PRODUCTION=true, and even
   then only for the one specific real production hostname. This is not
   a soft skip -- it's a session-startup failure, because the cost of
   silently letting an unattended run touch the wrong host is much
   higher than the cost of a loud, early stop.
3. Console-message and failed-network-request capture on every test, so
   a failure's own artifacts (see README.md's pytest invocation for the
   companion --screenshot/--video/--tracing flags) always come with what
   the browser's own console and network tab would have shown a human.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from dotenv import load_dotenv

E2E_ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = E2E_ROOT / "artifacts"
CONSOLE_NETWORK_DIR = ARTIFACTS_DIR / "console_network"

# Real environment variables (e.g. a CI secret store) always win over
# anything in the local .env file -- override=False is what guarantees
# that, matching this project's own established .env-is-a-local-
# convenience-not-an-override convention (deploy/.env.*.example).
load_dotenv(E2E_ROOT / ".env", override=False)

DEFAULT_STAGING_BASE_URL = "https://portal-staging.anyaicam.com"

# Explicit allowlist, not a production blocklist -- fails closed for
# ANY unrecognized host (a typo, a new environment, production, a
# lookalike domain), not just the one name it happens to know about.
# Hostnames only, lowercase, no scheme/port/path -- compared against
# urlparse(...).hostname, which Python itself already lowercases, so
# this is robust to case by construction (see extract_hostname() and
# test_conftest_safety_rail.py's own capitalization/variant coverage).
ALLOWED_STAGING_HOSTS = frozenset({"portal-staging.anyaicam.com"})

# Read directly from the real production container's own ANYAICAM_DOMAIN
# env var (confirmed live via SSM during the 2026-09-15 capability audit)
# -- NOT deploy/.env.production.example's stale `portal.anyaicam.com`
# placeholder, which does not even resolve. Never added to
# ALLOWED_STAGING_HOSTS -- reachable only via the explicit
# ANYAICAM_E2E_ALLOW_PRODUCTION opt-in below.
PRODUCTION_DOMAIN = "app.anyaicam.com"

BASE_URL = os.environ.get("ANYAICAM_E2E_BASE_URL", DEFAULT_STAGING_BASE_URL).strip()
ALLOW_PRODUCTION = os.environ.get("ANYAICAM_E2E_ALLOW_PRODUCTION", "false").strip().lower() == "true"


def extract_hostname(url: str) -> str | None:
    """Lowercase hostname only (no scheme/port/path/userinfo/query) --
    urlparse(...).hostname is already lowercased by Python itself, so
    `APP.ANYAICAM.COM`, `App.AnyAiCam.Com`, etc. all normalize to the
    same string this compares against ALLOWED_STAGING_HOSTS/
    PRODUCTION_DOMAIN. Returns None for a URL with no parseable host at
    all (treated as not-allowed, never as staging, by the caller)."""
    return urlparse(url).hostname


def is_host_allowed(hostname: str | None, allow_production: bool) -> bool:
    """Pure decision function, deliberately separate from pytest_configure()
    below so it can be unit-tested directly (no pytest-hook machinery, no
    subprocess) -- see test_conftest_safety_rail.py."""
    if hostname in ALLOWED_STAGING_HOSTS:
        return True
    return hostname == PRODUCTION_DOMAIN and allow_production


def pytest_configure(config):
    hostname = extract_hostname(BASE_URL)
    if not is_host_allowed(hostname, ALLOW_PRODUCTION):
        raise pytest.UsageError(
            f"Refusing to start: ANYAICAM_E2E_BASE_URL ({BASE_URL!r}, host={hostname!r}) is not "
            f"an approved staging host. Allowed: {sorted(ALLOWED_STAGING_HOSTS)}. The real "
            f"production host ({PRODUCTION_DOMAIN}) is reachable only via a deliberate, explicit "
            "opt-in (ANYAICAM_E2E_ALLOW_PRODUCTION=true in e2e/.env, set by a human, for one "
            "already-authorized read-only pass) -- automated modification of production is never "
            "allowed regardless of this flag. Leave ANYAICAM_E2E_BASE_URL unset for all normal use "
            "(it defaults to staging)."
        )
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    CONSOLE_NETWORK_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def artifacts_dir():
    """Exposed as a fixture (rather than requiring `import conftest` from
    sibling test files, which depends on pytest's import-mode/sys.path
    setup) so every test can reach the same screenshot/console-network
    output location the same way."""
    return ARTIFACTS_DIR


@pytest.fixture(scope="session")
def console_network_dir():
    return CONSOLE_NETWORK_DIR


@pytest.fixture(scope="session")
def base_url():
    """Overrides pytest-playwright's own `base_url` fixture so every test
    can use page.goto("/relative/path") -- context/page are constructed
    with this as Browser.new_context(base_url=...)."""
    return BASE_URL


@pytest.fixture(scope="session")
def e2e_credentials():
    """(email, password) for a dedicated e2e staging test tenant -- never
    the real pilot customer's own login. Cleanly skips (not errors) any
    test that needs this when the local .env hasn't been filled in yet,
    with the exact next step, not a stack trace."""
    email = os.environ.get("ANYAICAM_E2E_USERNAME", "").strip()
    password = os.environ.get("ANYAICAM_E2E_PASSWORD", "")
    if not email or not password:
        pytest.skip(
            "ANYAICAM_E2E_USERNAME/ANYAICAM_E2E_PASSWORD are not set. Copy "
            "e2e/.env.example to e2e/.env and fill in a dedicated staging "
            "test-tenant login (never the real pilot customer's own "
            "credentials) -- see docs/AUTONOMOUS_VALIDATION_PERMISSIONS.md."
        )
    return email, password


@pytest.fixture(autouse=True)
def console_and_network_capture(page, request):
    """Attached to every test automatically. Collects console messages at
    warning/error level and any failed or >=400 network response, then
    (only on test failure, to keep the common case artifact-free) writes
    them to e2e/artifacts/console_network/<test-name>.json alongside
    whatever screenshot/video/trace pytest-playwright's own --screenshot/
    --video/--tracing flags already captured (see README.md)."""
    console_messages: list[dict] = []
    failed_requests: list[dict] = []

    def _on_console(message):
        if message.type in ("error", "warning"):
            console_messages.append({"type": message.type, "text": message.text, "location": message.location})

    def _on_request_failed(req):
        failed_requests.append({"url": req.url, "method": req.method, "failure": req.failure})

    def _on_response(response):
        if response.status >= 400:
            failed_requests.append({"url": response.url, "status": response.status, "status_text": response.status_text})

    page.on("console", _on_console)
    page.on("requestfailed", _on_request_failed)
    page.on("response", _on_response)

    yield {"console_messages": console_messages, "failed_requests": failed_requests}

    failed = request.node.rep_call.failed if hasattr(request.node, "rep_call") else False
    if failed and (console_messages or failed_requests):
        # Created here, not just once in pytest_configure() -- pytest-
        # playwright's own --output flag recreates/clears the artifacts
        # root at test-run time, which silently deleted this subdirectory
        # out from under the one-time startup mkdir (confirmed live
        # 2026-09-15: a real FileNotFoundError on this exact write).
        CONSOLE_NETWORK_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in request.node.name)
        out_path = CONSOLE_NETWORK_DIR / f"{safe_name}.json"
        out_path.write_text(
            json.dumps({"console_messages": console_messages, "failed_requests": failed_requests}, indent=2),
            encoding="utf-8",
        )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Standard pytest recipe for making a test's pass/fail outcome
    available to a fixture's own teardown (request.node.rep_call above) --
    pytest has no built-in way to ask "did the test I'm tearing down for
    actually pass?" without this."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)
