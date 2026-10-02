"""AACO free-form input in a real browser (2026-09-26), desktop and phone.

The real /aaco workspace (typed) and the real floating AACO widget on a
customer page (typed and voice) are rendered by the real app for a
throwaway customer; /api/aaco/command runs the same pieces main.py wires --
the free-form adapter chain and the real _ClassicAacoBoundary over seeded
cameras and events. Voice uses a stand-in SpeechRecognition that delivers a
transcript when the mic is tapped (headless browsers have no microphone):
the transcript goes into the same box and through the same request as
typing. Opt-in (launches real browsers): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

ORIGIN = "https://anyaicam.test"
STATIC = Path(__file__).resolve().parents[1] / "static"
IDENTITY = {"role": "customer_owner", "customer_id": "cust", "email": "o@e.test"}
FAKE_SPEECH = """
window.__spokenTranscript = null;
class FakeRecognition {
  start() { setTimeout(() => {
    const result = [{transcript: window.__spokenTranscript}]; result.isFinal = true;
    this.onresult && this.onresult({results: [result]}); this.onend && this.onend();
  }, 50); }
  stop() {} abort() {}
}
window.SpeechRecognition = FakeRecognition; window.webkitSpeechRecognition = FakeRecognition;
"""


@pytest.fixture(scope="module")
def stack():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import partner_portal
    from database_backend import override_target
    db_path = Path(tempfile.mkdtemp()) / "aaco_browser.db"
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        from aaco_web import register_aaco_routes
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust','p1','C','c@e.test','active','x')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s1','cust','Home','x')")
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('a1','cust','s1','AIC-T','x')")
        for camera, name, number in (("cam-1", "Front Entrance", 1), ("cam-2", "Back Lot", 2)):
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (camera, "cust", "s1", "a1", number, name, "configured", "x"))
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                     "VALUES('u','p1','o@e.test','O','customer_owner','x',1,'cust','x','active','all')")
        recent = (datetime.now() - timedelta(minutes=5)).replace(microsecond=0).isoformat()
        for event, camera in (("e1", "cam-1"), ("e2", "cam-2")):
            conn.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (event, "cust", "s1", "a1", camera, event, "person", recent, recent))
        conn.commit()
        conn.close()
        original = partner_portal.partner_identity
        partner_portal.partner_identity = lambda request: IDENTITY
        try:
            api = FastAPI()
            register_aaco_routes(api, lambda *a, **k: "", identity_provider=lambda r: IDENTITY,
                                 vms_factory=lambda r: main._ClassicAacoBoundary(r), language_adapter_factory=main._aaco_language_adapter)
            with TestClient(main.app, base_url=ORIGIN, follow_redirects=False) as pages, TestClient(api) as commands:
                cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("o@e.test", "customer_owner", None, "cust", None)}
                html = {path: pages.get(path, cookies=cookie).text for path in ("/aaco", "/analytics")}
                yield html, commands, db_path
        finally:
            partner_portal.partner_identity = original


def _open(playwright, stack, path, *, mobile, sent, navigated=None):
    html, commands, db_path = stack
    from database_backend import override_target
    browser = playwright.chromium.launch()
    options = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True} if mobile else {"viewport": {"width": 1440, "height": 900}}
    page = browser.new_context(**options).new_page()
    page.add_init_script(FAKE_SPEECH)

    def handle(route):
        url = route.request.url.split("?")[0]
        tail = url[len(ORIGIN):] if url.startswith(ORIGIN) else ""
        if tail in html:
            return route.fulfill(status=200, content_type="text/html", body=html[tail])
        if tail == "/api/aaco/command":
            body = route.request.post_data or "{}"
            sent.append(json.loads(body)["command"])
            # Playwright calls this handler outside the fixture's context, so
            # point the app at the test database again here.
            with override_target(sqlite_path=str(db_path)):
                response = commands.post("/api/aaco/command", content=body, headers={"content-type": "application/json"})
            return route.fulfill(status=response.status_code, content_type="application/json", body=response.text)
        if navigated is not None and route.request.is_navigation_request() and tail not in html:
            navigated.append(route.request.url[len(ORIGIN):])
            return route.fulfill(status=200, content_type="text/html", body="<html><body>destination</body></html>")
        if tail.startswith("/static/") and (STATIC / tail[8:]).is_file():
            kind = "text/css" if tail.endswith(".css") else "application/javascript" if tail.endswith(".js") else "application/octet-stream"
            return route.fulfill(status=200, content_type=kind, body=(STATIC / tail[8:]).read_bytes())
        return route.fulfill(status=404, body="")

    page.route("**/*", handle)
    page.goto(f"{ORIGIN}{path}")
    return browser, page


@pytest.fixture(scope="module")
def playwright():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as instance:
        yield instance


def _no_sideways_scroll(page):
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "phone"])
def test_workspace_understands_typed_free_form_requests(playwright, stack, mobile):
    sent = []
    browser, page = _open(playwright, stack, "/aaco", mobile=mobile, sent=sent)
    try:
        box = page.locator("#aaco-command")
        for text in ("Did anyone come to the front entrance today?", "any people at the front entrance so far today"):
            box.fill(text)
            box.press("Enter")
            page.wait_for_function("(n) => document.querySelectorAll('.aaco-turn.operator').length >= n",
                                   arg=1 + sent.index(text) + 1)
            reply = page.locator(".aaco-turn.operator").last
            assert "Requested events" in reply.inner_text()
            rows = reply.locator(".aaco-result-row").all_inner_texts()
            assert rows and all("Front Entrance" in row for row in rows), rows  # the named camera only
        box.fill("tell me a joke")
        box.press("Enter")
        page.wait_for_function("() => document.querySelectorAll('.aaco-turn.operator').length >= 4")
        assert "I can open live view" in page.locator(".aaco-turn.operator").last.inner_text()
        assert page.get_by_role("button", name="Show me the latest event").is_visible()
        assert _no_sideways_scroll(page)
    finally:
        browser.close()


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "phone"])
def test_widget_voice_and_typed_take_the_same_path(playwright, stack, mobile):
    """The floating widget opens the result it found (for an event search,
    that event in Investigate) -- spoken and typed wordings land on the same
    Front Entrance event, never the Back Lot one."""
    destinations = []
    for how, text in (("voice", "hey aaco did anyone come to the front entrance today"),
                      ("typed", "Were there any visitors at the Front Entrance today?")):
        sent, navigated = [], []
        browser, page = _open(playwright, stack, "/analytics", mobile=mobile, sent=sent, navigated=navigated)
        try:
            page.locator("#aaco-float-toggle").click()
            mic, box = page.locator("#aaco-float-mic"), page.locator("#aaco-float-command")
            assert mic.is_visible() and mic.is_enabled() and box.is_visible()
            assert _no_sideways_scroll(page)
            if how == "voice":
                page.evaluate("(t) => { window.__spokenTranscript = t; }", text)
                mic.click()
            else:
                box.fill(text)
                box.press("Enter")
            page.wait_for_url("**/investigate?**", timeout=15000)
            assert sent == [text]  # the transcript went through the same request as typing
            destinations.append(navigated[-1])
        finally:
            browser.close()
    assert destinations[0] == destinations[1] and "camera=cam-1" in destinations[0]


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "phone"])
def test_widget_asks_a_short_question_for_an_incomplete_spoken_request(playwright, stack, mobile):
    sent = []
    browser, page = _open(playwright, stack, "/analytics", mobile=mobile, sent=sent, navigated=[])
    try:
        page.locator("#aaco-float-toggle").click()
        page.evaluate("() => { window.__spokenTranscript = 'play back three fifteen pm'; }")
        page.locator("#aaco-float-mic").click()
        page.wait_for_function("() => /Which camera should I play back/.test(document.getElementById('aaco-float-status').textContent)")
        assert sent == ["play back three fifteen pm"] and _no_sideways_scroll(page)
    finally:
        browser.close()

