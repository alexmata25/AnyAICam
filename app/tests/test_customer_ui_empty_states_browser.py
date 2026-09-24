"""Cross-browser customer UI review fixes (2026-09-24).

Found by a Chromium / Microsoft Edge / WebKit sweep of every customer-
facing page (desktop 1280x800 and phone 390x844) against a real local
server on a throwaway database:

- Live view (/) connected a hard-coded cameras 1..4, not the cameras the
  page actually rendered: an account with fewer cameras threw
  "video is null" (and the layout restore after it never ran), and
  cameras numbered above 4 never started streaming.
- Camera analytics and alerts (/customer-app-settings) with no cameras
  yet requested /api/customer/cameras//app-settings and then crashed
  reading data.features.
- Events (/events) scrolled the whole page sideways on a phone.

Opt-in (launches real browsers and a local server):
ANYAICAM_RUN_BROWSER_TESTS=1. The server-rendered checks at the bottom
always run.
"""
import os
import sqlite3

import pytest

from test_customer_password_reset_browser import BROWSERS, EMAIL, VIEWPORTS, _launch, _no_horizontal_overflow, playwright_instance, server  # noqa: F401

PASSWORD = "Ui-review-password-2026"
HLS_STUB = """window.__hlsLoaded=[];window.Hls=class{static isSupported(){return true}
constructor(){}loadSource(s){window.__hlsLoaded.push(s)}attachMedia(){}on(){}};Hls.Events={MANIFEST_PARSED:'m',ERROR:'e'};"""

browser_only = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)


@pytest.fixture()
def signed_in_customer(server):
    from partner_db import password_hash

    conn = sqlite3.connect(server["db_path"])
    for table in ("user_sessions", "account_lockouts", "partner_users", "customers"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES('partner-1','Partner','approved','2026-01-01')")
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Customer',?,'active','2026-01-01')", (EMAIL,))
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
        "VALUES('u1','partner-1',?,'Customer','customer_owner',?,1,'cust-1','2026-01-01','active','all')",
        (EMAIL, password_hash(PASSWORD)),
    )
    conn.commit(); conn.close()
    return server


def _signed_in_page(browser, origin, viewport):
    context = browser.new_context(viewport=VIEWPORTS[viewport])
    page = context.new_page()
    page.goto(f"{origin}/customer-login.html")
    page.fill("#email", EMAIL)
    page.fill("#password", PASSWORD)
    page.press("#password", "Enter")
    page.wait_for_url(lambda url: "customer-login" not in url, timeout=15000)
    page.route("https://cdn.jsdelivr.net/npm/hls.js@latest", lambda route: route.fulfill(body=HLS_STUB, content_type="application/javascript"))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    return page, errors


@browser_only
@pytest.mark.parametrize("viewport", list(VIEWPORTS), ids=list(VIEWPORTS))
@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_a_new_account_with_no_cameras_renders_cleanly(signed_in_customer, playwright_instance, engine, channel, viewport):
    origin = signed_in_customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        page, errors = _signed_in_page(browser, origin, viewport)
        blank_camera_requests = []
        page.on("request", lambda request: blank_camera_requests.append(request.url) if "/cameras//" in request.url else None)
        for path in ("/", "/customer-app-settings", "/events"):
            page.goto(f"{origin}{path}")
            page.wait_for_load_state("load")
            page.wait_for_timeout(800)
            assert _no_horizontal_overflow(page), path
        assert page.goto(f"{origin}/customer-app-settings") and page.locator("#no-cameras-notice").is_visible()
        page.wait_for_timeout(500)
        assert page.inner_text("#entitlement-lpr") == "No camera yet"
        assert not blank_camera_requests
        assert not errors, errors
    finally:
        browser.close()


@browser_only
@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_live_view_connects_exactly_the_rendered_cameras(signed_in_customer, playwright_instance, engine, channel, monkeypatch):
    import main

    monkeypatch.setattr(main, "get_camera_numbers", lambda *args, **kwargs: [1, 2, 6])
    origin = signed_in_customer["origin"]
    browser = _launch(playwright_instance, engine, channel)
    try:
        page, errors = _signed_in_page(browser, origin, "desktop")
        page.goto(f"{origin}/")
        page.wait_for_function("() => (window.__hlsLoaded || []).length >= 3", timeout=10000)
        assert sorted(page.evaluate("() => window.__hlsLoaded")) == [f"/static/hls/camera{n}.m3u8" for n in (1, 2, 6)]
        assert not errors, errors
    finally:
        browser.close()


# ------------------------------------------------ server-rendered (always)


def test_app_settings_escapes_the_account_email_and_explains_the_empty_state(monkeypatch):
    """The recipient email is placed in an HTML attribute; it must be
    escaped even though registration validates it."""
    import customer_platform

    source = open(customer_platform.__file__, encoding="utf-8").read()
    assert 'value="{escape(user.get("email") or "")}"' in source
    assert 'id="no-cameras-notice"' in source
    assert "if(!response.ok||!data.features)" in source


def test_live_view_no_longer_hard_codes_cameras_one_to_four():
    import main

    source = open(main.__file__, encoding="utf-8").read()
    assert "for(let n=1;n<=4;n++)connectCamera(n);" not in source
