"""Household (invited user) and owner logout in a real browser (2026-10-03).

Found on staging: an invited Household member who clicked Log out got a
plain page reading "CSRF validation failed". The shared shell's Log out is
a plain <form method="post" action="/logout">; a script copies the
anyaicam_csrf cookie into its hidden csrf_token field on submit. That
script bound itself with querySelectorAll('.logout-form') at the point it
ran -- right after the sidebar form, BEFORE the mobile navigation's own
Log out form was in the document -- so the phone-width Log out posted an
empty token and CSRF protection (correctly) refused it.

Journey, per viewport, with CSRF enforcement ON exactly as in staging:
invitation link -> /customer/join -> password -> /customer-login.html ->
sign in -> household pages -> Log out -> signed out (session revoked,
protected pages ask for sign-in again). The owner's Log out is checked
the same way. Real local server (uvicorn, main.app) on a throwaway
database; no real account or email. Opt-in: ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import dataclasses
import os
import socket
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

OWNER_EMAIL = "owner-logout@example.test"
OWNER_PASSWORD = "Owner-password-2026"
MEMBER_EMAIL = "member-logout@example.test"
MEMBER_PASSWORD = "Member-password-2026"
VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "phone": {"width": 390, "height": 844}}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    import uvicorn

    import cloud_security
    import main
    from database_backend import override_target
    from partner_db import initialize_database

    db_path = Path(tempfile.mkdtemp()) / "household_logout.db"
    port = _free_port()
    origin = f"http://localhost:{port}"
    patches = pytest.MonkeyPatch()
    # CSRF enforcement ON, as in staging; only this test server's origin is added.
    patches.setattr(cloud_security, "settings", dataclasses.replace(
        cloud_security.settings, csrf_enabled=True,
        allowed_origins=[*cloud_security.settings.allowed_origins, origin],
    ))
    patches.setattr(main, "CUSTOMER_LOGOUT_DESTINATION", f"{origin}/customer-login.html?signed_out=1")
    with override_target(sqlite_path=db_path):
        initialize_database()
    state = {}

    def run():
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
    yield {"origin": origin, "db_path": db_path}
    state["server"].should_exit = True
    thread.join(timeout=10)
    patches.undo()


def _reset_and_invite(server) -> str:
    """A fresh owner and one waiting invitation; returns the raw link token."""
    import household_users
    from database_backend import override_target
    from partner_db import connection, password_hash

    for limiter in (household_users._invite_limiter, household_users._join_ip_limiter):
        limiter.events.clear()
    conn = sqlite3.connect(server["db_path"])
    for table in ("user_sessions", "invitations", "customer_camera_permissions", "customer_user_permissions",
                  "identity_grants", "account_lockouts", "partner_users", "customers"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES('partner-1','Partner','approved','2026-01-01')")
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Home',?,'active','2026-01-01')",
                 (OWNER_EMAIL,))
    conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                 "VALUES('owner-1','partner-1',?,'Alex Owner','customer_owner',?,1,'cust-1','2026-01-01','active','all')",
                 (OWNER_EMAIL, password_hash(OWNER_PASSWORD)))
    conn.commit()
    conn.close()
    with override_target(sqlite_path=server["db_path"]):
        with connection() as db:
            _, raw = household_users.create_invitation(
                db, owner={"customer_id": "cust-1", "email": OWNER_EMAIL, "id": "owner-1", "name": "Alex Owner"},
                name="Maria", email=MEMBER_EMAIL, permissions={"all_cameras": True}, now=datetime.now())
    return raw


@pytest.fixture(scope="module")
def browser():
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as instance:
        launched = instance.chromium.launch()
        yield launched
        launched.close()


def _sign_in(page, origin, email, password):
    page.goto(f"{origin}/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_url(lambda url: "customer-login" not in url, timeout=15000)


def _log_out(page, viewport_name):
    """Clicks the Log out the person actually sees at this width."""
    selector = ".mobile-logout button" if viewport_name == "phone" else "#logout-form button"
    button = page.locator(selector)
    assert button.is_visible(), f"no visible Log out at {viewport_name} width"
    with page.expect_navigation():
        button.click()


def _assert_signed_out(page, origin, server, *, revoked_email):
    assert "CSRF validation failed" not in page.content()
    assert page.url.startswith(f"{origin}/customer-login.html"), page.url
    conn = sqlite3.connect(server["db_path"])
    live = conn.execute("SELECT COUNT(*) FROM user_sessions s JOIN partner_users u ON u.id=s.user_id "
                        "WHERE u.email=? AND s.revoked_at IS NULL", (revoked_email,)).fetchone()[0]
    conn.close()
    assert live == 0, "the session must be revoked on the server, not only forgotten by the browser"
    for protected in ("/customer/household", "/customer-account", "/customer-live"):
        page.goto(f"{origin}{protected}")
        assert "customer-login" in page.url, f"{protected} still open after logout: {page.url}"


@pytest.mark.parametrize("viewport_name", ["desktop", "phone"])
def test_an_invited_household_member_signs_up_browses_and_logs_out(server, browser, viewport_name):
    origin = server["origin"]
    raw = _reset_and_invite(server)
    context = browser.new_context(viewport=VIEWPORTS[viewport_name])
    page = context.new_page()
    try:
        page.goto(f"{origin}/customer/join?token={raw}")
        page.fill("#join-password", MEMBER_PASSWORD)
        page.fill("#join-confirm", MEMBER_PASSWORD)
        page.click("#join-form button.submit")
        page.wait_for_url(lambda url: "customer-login" in url, timeout=15000)
        _sign_in(page, origin, MEMBER_EMAIL, MEMBER_PASSWORD)
        for path in ("/customer-account", "/customer-live"):  # normal household navigation
            response = page.goto(f"{origin}{path}")
            assert response.status == 200 and "customer-login" not in page.url, path
        _log_out(page, viewport_name)
        _assert_signed_out(page, origin, server, revoked_email=MEMBER_EMAIL)
    finally:
        context.close()


@pytest.mark.parametrize("viewport_name", ["desktop", "phone"])
def test_the_owner_logs_out_the_same_way(server, browser, viewport_name):
    origin = server["origin"]
    _reset_and_invite(server)
    context = browser.new_context(viewport=VIEWPORTS[viewport_name])
    page = context.new_page()
    try:
        _sign_in(page, origin, OWNER_EMAIL, OWNER_PASSWORD)
        page.goto(f"{origin}/customer/household")
        assert "customer-login" not in page.url
        _log_out(page, viewport_name)
        _assert_signed_out(page, origin, server, revoked_email=OWNER_EMAIL)
    finally:
        context.close()
