"""Analytics navigation + workspaces (2026-09-25), in a real browser.

Desktop: the one Analytics sidebar entry opens a flyout listing only the
customer's analytics. Phone: the bottom-bar Analytics entry opens a compact
submenu above the bar. In a workspace, selecting a result expands it in
place and plays its clip right there; another result replaces it; a result
without a clip shows its snapshot and says so. Only real controls; no mic.

The real pages are rendered by the real app for a throwaway customer; the
network is mocked and the clip is a WebM the browser itself generates.

Opt-in (launches real browsers): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import base64
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

ORIGIN = "https://anyaicam.test"
STATIC = Path(__file__).resolve().parents[1] / "static"
ASSETS = {name: (STATIC / name).read_text(encoding="utf-8")
          for name in ("event_media.js", "inline_media.js", "inline_media.css", "analytics_workspace.js")}
BROWSERS = [("chromium", None), ("msedge", "msedge")]


def _snapshot_jpeg():
    """A bright, recognizable 480x270 'snapshot' (what ?size=card serves)."""
    import cv2
    import numpy as np
    image = np.full((270, 480, 3), (60, 170, 230), dtype=np.uint8)
    cv2.rectangle(image, (120, 60), (360, 210), (250, 250, 250), -1)
    return cv2.imencode(".jpg", image)[1].tobytes()


SNAPSHOT = _snapshot_jpeg()
HELD = []
NO_THUMBNAIL, FAILING_THUMBNAIL, SNAPSHOT_ONLY = "ev-6", "ev-7", "ev-3"


def _render_pages():
    from fastapi.testclient import TestClient

    from database_backend import override_target

    db_path = Path(tempfile.mkdtemp()) / "analytics_browser.db"
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        import partner_portal
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust','p1','C','c@e.test','active','x')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s1','cust','Home','x')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-1','cust','s1','Porch','configured',1,'x')")
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                     "VALUES('u','p1','o@e.test','O','customer_owner','x',1,'cust','x','active','all')")
        for key in ("smart_motion", "ppe"):
            conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-1',?,'active','x','x')", (key,))
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url=ORIGIN, follow_redirects=False) as client:
            cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("o@e.test", "customer_owner", None, "cust", None)}
            pages = {path: client.get(path, cookies=cookie).text for path in ("/analytics/smart-motion", "/analytics", "/analytics/ppe")}
    assert "window.__AW=" in pages["/analytics/smart-motion"]
    return pages


def _events():
    now = int(time.time() * 1000)
    events = []
    for index in range(8):
        events.append({"event_id": f"ev-{index}", "camera_id": "cam-1", "event_type": "person" if index % 2 else "car",
                       "timestamp_ms": now - index * 600000, "confidence": 0.8,
                       "has_clip": f"ev-{index}" not in (SNAPSHOT_ONLY, NO_THUMBNAIL),
                       "has_thumbnail": f"ev-{index}" != NO_THUMBNAIL, "details": {"object_count": 1}})
    return {"events": events, "summary": {"total": 8, "by_type": {"person": 4, "car": 4}}, "next_before": None,
            "enabled_camera_ids": ["cam-1"]}


@pytest.fixture(scope="module")
def pages():
    return _render_pages()


@pytest.fixture(scope="module")
def playwright_instance():
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as instance:
        yield instance


@pytest.fixture(scope="module")
def webm(playwright_instance):
    browser = playwright_instance.chromium.launch()
    try:
        page = browser.new_page()
        page.set_content("<canvas width=160 height=90></canvas>")
        data = page.evaluate("""async () => {
            const c = document.querySelector('canvas'), g = c.getContext('2d');
            const rec = new MediaRecorder(c.captureStream(15), {mimeType: 'video/webm'});
            const parts = []; rec.ondataavailable = e => parts.push(e.data);
            let n = 0; const draw = setInterval(() => { g.fillStyle = `hsl(${(n += 7) % 360},70%,50%)`; g.fillRect(0, 0, 160, 90); }, 60);
            rec.start(); await new Promise(r => setTimeout(r, 6000)); rec.stop(); clearInterval(draw);
            await new Promise(r => rec.onstop = r);
            const buf = await new Blob(parts, {type: 'video/webm'}).arrayBuffer();
            let bin = ''; new Uint8Array(buf).forEach(b => bin += String.fromCharCode(b)); return btoa(bin);
        }""")
        return base64.b64decode(data)
    finally:
        browser.close()


def _open(playwright_instance, engine, channel, pages, webm, path, *, mobile=False, hold_thumbnail=None, seen=None):
    try:
        browser = playwright_instance.chromium.launch(channel=channel) if channel else playwright_instance.chromium.launch()
    except Exception as error:  # browser not installed on this machine
        pytest.skip(f"{engine} unavailable: {error}")
    options = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True} if mobile else {"viewport": {"width": 1440, "height": 900}}
    page = browser.new_context(**options).new_page()
    data = _events()

    def handle(route):
        url = route.request.url
        tail = url[len(ORIGIN):].split("?")[0]
        if tail in pages:
            return route.fulfill(status=200, content_type="text/html", body=pages[tail])
        if tail.startswith("/static/") and tail[8:] in ASSETS:
            kind = "text/css" if tail.endswith(".css") else "application/javascript"
            return route.fulfill(status=200, content_type=kind, body=ASSETS[tail[8:]])
        if tail == "/api/customer/analytics/smart_motion/events":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
        if tail.endswith("/thumbnail"):
            event_id = tail.split("/")[-2]
            if seen is not None:
                seen.append(url[len(ORIGIN):])
            if event_id == FAILING_THUMBNAIL:
                return route.fulfill(status=404, body="")
            if event_id == hold_thumbnail:
                HELD.append(route)  # the test releases it to observe the loading state
                return None
            return route.fulfill(status=200, content_type="image/jpeg", body=SNAPSHOT)
        if tail.endswith("/media/url"):
            event_id = tail.split("/")[-3]
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"url": f"/clips/{event_id}.webm"}))
        if tail.startswith("/clips/"):
            return route.fulfill(status=200, content_type="video/webm", body=webm)
        return route.fulfill(status=404, body="")

    page.route("**/*", handle)
    page.goto(f"{ORIGIN}{path}")
    return browser, page


def _wait_playing(page):
    page.wait_for_function("() => { const v = document.querySelector('.inline-media-card video'); return v && !v.paused && v.currentTime > 0.3; }", timeout=15000)


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_desktop_flyout_lists_only_entitled_analytics(playwright_instance, pages, webm, engine, channel):
    browser, page = _open(playwright_instance, engine, channel, pages, webm, "/analytics")
    try:
        toggle = page.locator("[data-nav-flyout-toggle]")
        menu = page.locator(".nav-flyout")
        assert menu.is_hidden()
        toggle.hover()  # a mouse shows the list on hover...
        menu.wait_for(state="visible")
        assert menu.locator("a").all_inner_texts() == ["All analytics", "Smart Motion", "PPE", "Smart Rules"]
        box, anchor = menu.bounding_box(), toggle.bounding_box()
        # ...and it stays open while the pointer crosses the gap into it.
        page.mouse.move(anchor["x"] + anchor["width"] + 4, anchor["y"] + anchor["height"] / 2)
        page.mouse.move(box["x"] + 20, box["y"] + 15)
        page.wait_for_timeout(400)
        assert menu.is_visible()
        page.mouse.move(700, 850)
        menu.wait_for(state="hidden")
        toggle.focus()  # keyboard: Enter toggles it and focuses the first item
        page.keyboard.press("Enter")
        menu.wait_for(state="visible")
        assert page.evaluate("() => document.activeElement.textContent") == "All analytics"
        box, anchor = menu.bounding_box(), toggle.bounding_box()
        assert box["x"] >= anchor["x"] + anchor["width"]  # opens beside the sidebar, not over it
        page.keyboard.press("Escape")
        menu.wait_for(state="hidden")
        assert page.locator(".mobile-analytics-sheet").is_hidden()  # the phone submenu never shows on desktop
        toggle.hover()
        menu.locator("text=PPE").click()
        page.wait_for_url("**/analytics/ppe")
        page.goto(f"{ORIGIN}/analytics/smart-motion")
        page.locator("[data-nav-flyout-toggle]").click()  # a mouse click on the entry opens the overview
        page.wait_for_url("**/analytics")
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_desktop_result_expands_in_place_and_is_replaced(playwright_instance, pages, webm, engine, channel):
    browser, page = _open(playwright_instance, engine, channel, pages, webm, "/analytics/smart-motion")
    try:
        cards = page.locator("#aw-results .aw-card")
        cards.nth(7).wait_for(timeout=15000)
        assert page.locator("#aw-stats").inner_text().split("\n")[:2] == ["Detections", "8"]
        cards.nth(5).scroll_into_view_if_needed()
        before = page.evaluate("() => window.scrollY")
        cards.nth(5).click()
        _wait_playing(page)
        assert page.evaluate("() => document.querySelectorAll('#aw-results .aw-card')[5].nextElementSibling.classList.contains('inline-media-card')")
        # The page only nudges the new player into view -- never to the top.
        page.wait_for_function("() => { const r = document.querySelector('.inline-media-card').getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight; }")
        assert page.evaluate("() => window.scrollY") >= before
        card = page.locator(".inline-media-card")
        acts = card.locator("[data-act]:visible").evaluate_all("els => els.map(e => e.dataset.act)")
        assert acts == ["play", "mute", "fullscreen", "download", "share", "playback", "close"]
        assert card.locator('[data-act="playback"]').get_attribute("href") == "/playback?camera=cam-1&event=ev-5&autoplay=event"
        assert card.locator('[data-act="download"]').get_attribute("href") == "/clips/ev-5.webm"
        assert "mic" not in card.inner_html().lower() and "talk" not in card.inner_html().lower()
        card_box = card.bounding_box()
        assert card_box["width"] > cards.nth(5).bounding_box()["width"] * 1.5  # spans the row, not one grid cell

        card.locator('[data-act="play"]').click()
        assert page.evaluate("() => document.querySelector('.inline-media-card video').paused")
        cards.nth(1).click()
        _wait_playing(page)
        assert page.locator(".inline-media-card").count() == 1
        assert cards.nth(5).get_attribute("aria-expanded") == "false" and cards.nth(1).get_attribute("aria-expanded") == "true"
        page.locator('.inline-media-card [data-act="close"]').click()
        assert page.locator(".inline-media-card").count() == 0
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_result_without_clip_shows_snapshot_and_no_dead_controls(playwright_instance, pages, webm, engine, channel):
    browser, page = _open(playwright_instance, engine, channel, pages, webm, "/analytics/smart-motion")
    try:
        cards = page.locator("#aw-results .aw-card")
        cards.nth(3).wait_for(timeout=15000)
        cards.nth(3).click()
        card = page.locator(".inline-media-card")
        card.wait_for()
        assert card.locator("img").is_visible()
        assert "No video clip was recorded" in card.locator(".inline-media-status").inner_text()
        for act in ("play", "mute", "download"):
            assert card.locator(f'[data-act="{act}"]').is_hidden(), act
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_phone_analytics_submenu_and_inline_playback(playwright_instance, pages, webm, engine, channel):
    browser, page = _open(playwright_instance, engine, channel, pages, webm, "/analytics/smart-motion", mobile=True)
    try:
        toggle = page.locator(".mobile-analytics-toggle")
        assert toggle.is_visible() and page.locator("[data-nav-flyout-toggle]").is_hidden()
        sheet = page.locator("#mobile-analytics-sheet")
        toggle.tap()
        sheet.wait_for(state="visible")
        assert toggle.get_attribute("aria-expanded") == "true"
        assert sheet.locator("a").all_inner_texts() == ["All analytics", "Smart Motion", "PPE", "Smart Rules"]
        assert sheet.bounding_box()["y"] + sheet.bounding_box()["height"] <= page.locator(".mobile-nav").bounding_box()["y"] + 1  # above the bar
        page.mouse.click(195, 200)
        sheet.wait_for(state="hidden")

        cards = page.locator("#aw-results .aw-card")
        cards.nth(2).wait_for(timeout=15000)
        assert cards.nth(0).bounding_box()["width"] > 300  # one card per row on a phone
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        cards.nth(2).tap()
        _wait_playing(page)
        tools = page.locator(".inline-media-card [data-act]:visible")
        assert all(box["height"] >= 40 for box in tools.evaluate_all("els => els.map(e => e.getBoundingClientRect().toJSON())"))
    finally:
        browser.close()


def _brightness(page, selector):
    """Mean brightness (0-255) of what the card preview actually renders."""
    import cv2
    import numpy as np
    shot = page.locator(selector).screenshot()
    return float(cv2.imdecode(np.frombuffer(shot, dtype=np.uint8), cv2.IMREAD_GRAYSCALE).mean())


def _thumb(event_id):
    return f'#aw-results .aw-card[data-event="{event_id}"] .aw-thumb'


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "phone"])
@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_results_show_their_snapshot_before_they_are_opened(playwright_instance, pages, webm, engine, channel, mobile):
    seen = []
    browser, page = _open(playwright_instance, engine, channel, pages, webm, "/analytics/smart-motion", mobile=mobile, seen=seen)
    try:
        for event_id in ("ev-1", "ev-2"):
            page.wait_for_selector(f'{_thumb(event_id)}[data-preview="ready"]', timeout=15000)
            page.wait_for_function("(sel) => { const i = document.querySelector(sel + ' img'); return i.naturalWidth > 0 && getComputedStyle(i).opacity === '1'; }", arg=_thumb(event_id))
            assert _brightness(page, _thumb(event_id)) > 90, event_id  # the snapshot, not a black rectangle
            assert page.locator(f"{_thumb(event_id)} .aw-thumb-note").is_hidden()
        # Card-sized previews of the stored snapshot, the first screenful requested at once.
        assert seen and all(url.endswith("/thumbnail?size=card") for url in seen)
        assert page.locator('#aw-results .aw-card img[loading="eager"]').count() >= 5
        # The card still carries camera, local time and the analytic's own label.
        text = page.locator('#aw-results .aw-card[data-event="ev-1"]').inner_text()
        assert "Person detected" in text and "Porch" in text and ("Today" in text or "Yesterday" in text) and "80% confidence" in text
        # Selecting it expands in place and plays, with the snapshot as the poster meanwhile.
        page.locator('#aw-results .aw-card[data-event="ev-1"]').click()
        assert page.locator(".inline-media-card video").get_attribute("poster") == "/api/customer/events/cam-1/ev-1/thumbnail?size=card"
        _wait_playing(page)
        page.locator('#aw-results .aw-card[data-event="ev-2"]').click()
        _wait_playing(page)
        assert page.locator(".inline-media-card").count() == 1
        assert page.evaluate("() => document.querySelector('#aw-results .aw-card[data-event=\"ev-2\"]').nextElementSibling.classList.contains('inline-media-card')")
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_a_preview_still_loading_is_a_neutral_labelled_state(playwright_instance, pages, webm, engine, channel):
    seen = []
    browser, page = _open(playwright_instance, engine, channel, pages, webm, "/analytics/smart-motion", hold_thumbnail="ev-4", seen=seen)
    try:
        HELD.clear()
        page.wait_for_selector(f'{_thumb("ev-4")}[data-preview="loading"]', timeout=15000)
        for _ in range(100):
            if HELD:
                break
            page.wait_for_timeout(100)
        assert HELD, "the preview request was never made"
        page.wait_for_timeout(500)
        assert page.locator(f'{_thumb("ev-4")}[data-preview="loading"]').count() == 1
        assert page.locator(f"{_thumb('ev-4')} .aw-thumb-note").inner_text() == "Loading preview…"
        assert _brightness(page, _thumb("ev-4")) > 30  # neutral slate placeholder, not the old near-black tile (~15)
        HELD.pop().fulfill(status=200, content_type="image/jpeg", body=SNAPSHOT)
        page.wait_for_selector(f'{_thumb("ev-4")}[data-preview="ready"]', timeout=15000)
        page.wait_for_timeout(400)  # fade-in
        assert _brightness(page, _thumb("ev-4")) > 90
    finally:
        browser.close()


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "phone"])
def test_no_preview_is_said_plainly_never_a_black_box(playwright_instance, pages, webm, mobile):
    browser, page = _open(playwright_instance, "chromium", None, pages, webm, "/analytics/smart-motion", mobile=mobile)
    try:
        for event_id in (NO_THUMBNAIL, FAILING_THUMBNAIL):
            page.wait_for_selector(f'{_thumb(event_id)}[data-preview="none"]', timeout=15000)
            assert page.locator(f"{_thumb(event_id)} .aw-thumb-note").inner_text() == "No preview available", event_id
            assert page.locator(f"{_thumb(event_id)} img").count() == 0
            assert _brightness(page, _thumb(event_id)) > 30, event_id
        # Opening a result with no media at all explains itself too.
        page.locator(f'#aw-results .aw-card[data-event="{NO_THUMBNAIL}"]').click()
        card = page.locator(".inline-media-card")
        card.wait_for()
        assert card.locator(".inline-media-empty").inner_text() == "No preview available"
        assert card.locator("img").count() == 0
    finally:
        browser.close()
