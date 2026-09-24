"""Cross-browser customer password-recovery journey (2026-09-24).

Chromium, Microsoft Edge and WebKit (Safari engine) each drive the REAL
app, served by a real local HTTP server (uvicorn, main.app) on
http://localhost backed by a throwaway test database -- all three
browsers use the identical backend. (Request interception can't be used
here: Playwright cannot fulfill a fetch() with a 3xx, and the login
flow depends on a real 303 redirect.) Journey, per browser and viewport:

  /customer-forgot-password -> submit email -> (captured email link)
  -> /customer-reset-password?token=... -> set new password
  -> redirected to /customer-login.html -> sign in with the new password
  -> lands in the customer portal (not back on the login page)

A test customer exists in the throwaway database only; no production
account, password or email is involved. Opt-in (launches real browsers
and a local server): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import dataclasses
import os
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

EMAIL = "reset-browser@example.test"
NEW_PASSWORD = "Brand-new-password-2026"
BROWSERS = [("chromium", None), ("msedge", "msedge"), ("webkit", None)]
VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "phone": {"width": 390, "height": 844}}


class _CapturingEmailService:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.sent.append({"to": to, "text": text})
        import uuid
        return {"id": f"msg-{uuid.uuid4().hex}", "status": "preview"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    import uvicorn

    import cloud_features
    import cloud_security
    import main
    from database_backend import override_target
    from partner_db import initialize_database

    db_path = Path(tempfile.mkdtemp()) / "reset_browser.db"
    port = _free_port()
    origin = f"http://localhost:{port}"
    capturing = _CapturingEmailService()
    patches = pytest.MonkeyPatch()
    patches.setattr(cloud_features, "get_email_service", lambda: capturing)
    # The app correctly rejects state-changing requests from origins it was
    # not configured for; allow exactly this test server's own origin on top
    # of the current configuration (everything else unchanged).
    patches.setattr(cloud_security, "settings", dataclasses.replace(
        cloud_security.settings, allowed_origins=[*cloud_security.settings.allowed_origins, origin],
    ))
    with override_target(sqlite_path=db_path):
        initialize_database()
    state = {}

    def run():
        # The server thread needs the throwaway database in ITS context;
        # asyncio tasks and threadpool calls inherit it from here.
        with override_target(sqlite_path=db_path):
            config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
            state["server"] = uvicorn.Server(config)
            state["server"].run()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.1)
    yield {"origin": origin, "db_path": db_path, "capturing": capturing, "cloud_features": cloud_features}
    state["server"].should_exit = True
    thread.join(timeout=10)
    patches.undo()


@pytest.fixture()
def customer(server):
    """A fresh test customer (unknown password) for each test."""
    for limiter in (server["cloud_features"]._password_reset_email_limiter, server["cloud_features"]._password_reset_ip_limiter, server["cloud_features"]._password_reset_complete_ip_limiter):
        limiter.events.clear()
    from partner_db import password_hash

    conn = sqlite3.connect(server["db_path"])
    for table in ("user_sessions", "password_reset_tokens", "account_lockouts", "partner_users", "customers"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES('partner-1','Partner','approved','2026-01-01')")
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Customer',?,'active','2026-01-01')", (EMAIL,))
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
        "VALUES('u1','partner-1',?,'Customer','customer_owner',?,1,'cust-1','2026-01-01','active','all')",
        (EMAIL, password_hash("Unknown-old-password-1")),
    )
    conn.commit(); conn.close()
    server["capturing"].sent.clear()
    return server


@pytest.fixture(scope="module")
def playwright_instance():
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as instance:
        yield instance


def _launch(playwright_instance, engine, channel):
    try:
        if engine == "webkit":
            return playwright_instance.webkit.launch()
        return playwright_instance.chromium.launch(channel=channel) if channel else playwright_instance.chromium.launch()
    except Exception as error:
        pytest.skip(f"{engine} unavailable: {error}")


def _no_horizontal_overflow(page) -> bool:
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")


@pytest.mark.parametrize("viewport", list(VIEWPORTS), ids=list(VIEWPORTS))
@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_customer_can_recover_their_account_end_to_end(customer, playwright_instance, engine, channel, viewport):
    origin = customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_context(viewport=VIEWPORTS[viewport]).new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        # 1. Forgot password
        page.goto(f"{origin}/customer-forgot-password")
        assert page.locator("#forgot-email").is_visible()
        assert _no_horizontal_overflow(page)
        page.fill("#forgot-email", EMAIL)
        page.press("#forgot-email", "Enter")
        deadline = time.monotonic() + 10
        while not customer["capturing"].sent and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert customer["capturing"].sent, "no reset email was prepared"
        link = customer["capturing"].sent[-1]["text"].split("\n")[-1].strip()
        assert "/customer-reset-password?token=" in link

        # 2. The emailed link (host is this test server; path/token unchanged)
        parts = urlsplit(link)
        page.goto(f"{origin}{parts.path}?{parts.query}")
        assert page.locator("#reset-password").is_visible()
        assert _no_horizontal_overflow(page)
        page.fill("#reset-password", NEW_PASSWORD)
        page.press("#reset-password", "Enter")
        page.wait_for_url("**/customer-login.html**", timeout=15000)

        # 3. Sign in with the new password
        page.fill("#email", EMAIL)
        page.fill("#password", NEW_PASSWORD)
        page.press("#password", "Enter")  # (the form's first button is the show/hide toggle)
        page.wait_for_url(lambda url: "customer-login" not in url, timeout=15000)
        assert "customer-login" not in page.url
        assert not errors, errors
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_an_invalid_reset_link_shows_an_error_and_stays_on_the_customer_page(customer, playwright_instance, engine, channel):
    origin = customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_context().new_page()
        page.goto(f"{origin}/customer-reset-password?token=bogus.token")
        page.fill("#reset-password", NEW_PASSWORD)
        page.press("#reset-password", "Enter")
        page.wait_for_timeout(1500)
        assert "/customer-reset-password" in page.url
        body = page.inner_text("body").lower()
        assert "invalid" in body or "expired" in body
    finally:
        browser.close()
