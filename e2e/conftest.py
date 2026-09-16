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
2. A hard safety rail: refuses to run ANY test against a base_url that
   looks like the real production domain unless a human has explicitly
   opted in via ANYAICAM_E2E_ALLOW_PRODUCTION=true. This is not a soft
   skip -- it's a session-startup failure, because the cost of silently
   letting an unattended run touch production is much higher than the
   cost of a loud, early stop.
3. Console-message and failed-network-request capture on every test, so
   a failure's own artifacts (see README.md's pytest invocation for the
   companion --screenshot/--video/--tracing flags) always come with what
   the browser's own console and network tab would have shown a human.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

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
# Read directly from the real production container's own ANYAICAM_DOMAIN
# env var (confirmed live via SSM during the 2026-09-15 capability audit)
# -- NOT deploy/.env.production.example's stale `portal.anyaicam.com`
# placeholder, which does not even resolve.
PRODUCTION_DOMAIN = "app.anyaicam.com"

BASE_URL = os.environ.get("ANYAICAM_E2E_BASE_URL", DEFAULT_STAGING_BASE_URL).strip()
ALLOW_PRODUCTION = os.environ.get("ANYAICAM_E2E_ALLOW_PRODUCTION", "false").strip().lower() == "true"


def pytest_configure(config):
    if PRODUCTION_DOMAIN in BASE_URL and not ALLOW_PRODUCTION:
        raise pytest.UsageError(
            f"Refusing to start: ANYAICAM_E2E_BASE_URL ({BASE_URL!r}) targets the real "
            f"production domain ({PRODUCTION_DOMAIN}). Automated modification of production "
            "is never allowed, and even inspection requires a deliberate, explicit opt-in -- "
            "set ANYAICAM_E2E_ALLOW_PRODUCTION=true in e2e/.env only when a human has "
            "separately authorized a specific read-only production pass. Leave "
            "ANYAICAM_E2E_BASE_URL unset (it defaults to staging) for all normal use."
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
