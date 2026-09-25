"""Playback inline recording player (2026-09-25), in a real browser.

A recording chosen from the list below the timeline expands and plays
right there, in a Live-style card -- the page never jumps to the top
player. One at a time; the top player stays the timeline/scrub player;
the card survives the list's periodic refresh without restarting.

The real Playback page is rendered by the real app for a throwaway
customer; the network is mocked. The "recording" is a short WebM clip the
browser itself generates (canvas + MediaRecorder), so playback is real.

Opt-in (launches real browsers): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import base64
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

ORIGIN = "https://anyaicam.test"
EVENT_MEDIA_JS = (Path(__file__).resolve().parents[1] / "static" / "event_media.js").read_text(encoding="utf-8")
BROWSERS = [("chromium", None), ("msedge", "msedge")]


def _render_playback_page():
    from fastapi.testclient import TestClient

    from database_backend import override_target

    db_path = Path(tempfile.mkdtemp()) / "inline_playback.db"
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
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url=ORIGIN, follow_redirects=False) as client:
            cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("o@e.test", "customer_owner", None, "cust", None)}
            html = client.get("/playback", cookies=cookie).text
    assert "createPlaybackInlinePlayer" in html
    return html


def _clips():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    clips = []
    for index in range(12):
        start = now - timedelta(minutes=5 * (index + 1))
        clip = {"id": f"rec-{index}", "name": f"rec-{index}.mp4",
                "start": start.isoformat(), "end": (start + timedelta(minutes=5)).isoformat()}
        if index == 1:
            clip.update({"id": "evt-1", "kind": "event_clip", "end": (start + timedelta(seconds=10)).isoformat()})
        clips.append(clip)
    return list(reversed(clips))


@pytest.fixture(scope="module")
def page_html():
    return _render_playback_page()


@pytest.fixture(scope="module")
def playwright_instance():
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as instance:
        yield instance


@pytest.fixture(scope="module")
def webm(playwright_instance):
    """~10 s WebM made by the browser itself (no binary fixture needed)."""
    browser = playwright_instance.chromium.launch()
    try:
        page = browser.new_page()
        page.set_content("<canvas width=160 height=90></canvas>")
        data = page.evaluate("""async () => {
            const c = document.querySelector('canvas'), g = c.getContext('2d');
            const rec = new MediaRecorder(c.captureStream(15), {mimeType: 'video/webm'});
            const parts = []; rec.ondataavailable = e => parts.push(e.data);
            let n = 0; const draw = setInterval(() => { g.fillStyle = `hsl(${(n += 7) % 360},70%,50%)`; g.fillRect(0, 0, 160, 90); }, 60);
            rec.start(); await new Promise(r => setTimeout(r, 10000)); rec.stop(); clearInterval(draw);
            await new Promise(r => rec.onstop = r);
            const buf = await new Blob(parts, {type: 'video/webm'}).arrayBuffer();
            let bin = ''; new Uint8Array(buf).forEach(b => bin += String.fromCharCode(b)); return btoa(bin);
        }""")
        return base64.b64decode(data)
    finally:
        browser.close()


def _open(playwright_instance, engine, channel, html, webm, *, mobile=False):
    try:
        browser = playwright_instance.chromium.launch(channel=channel) if channel else playwright_instance.chromium.launch()
    except Exception as error:  # browser not installed on this machine
        pytest.skip(f"{engine} unavailable: {error}")
    options = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True} if mobile else {"viewport": {"width": 1440, "height": 900}}
    context = browser.new_context(**options)
    page = context.new_page()
    clips = _clips()
    media_requests = []

    def handle(route):
        url = route.request.url
        if url.startswith(f"{ORIGIN}/playback"):
            return route.fulfill(status=200, content_type="text/html", body=html)
        if url.startswith(f"{ORIGIN}/static/event_media.js"):
            return route.fulfill(status=200, content_type="application/javascript", body=EVENT_MEDIA_JS)
        if "/media" in url and "/api/customer/recordings/" in url:
            media_requests.append(url)
            return route.fulfill(status=200, content_type="video/webm", body=webm)
        if "/api/customer/recordings/cam-1/dates" in url:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"dates": [clips[-1]["start"][:10]]}))
        if "/api/customer/recordings/cam-1" in url and "/thumbnail" not in url:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"clips": clips}))
        if url.endswith("/api/customer/events/cam-1/evt-1/media/url"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"url": "/api/customer/recordings/cam-1/evt-1-file/media"}))
        if "/api/customer/events/" in url and "/thumbnail" not in url:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"events": []}))
        return route.fulfill(status=404, body="")

    page.route("**/*", handle)
    page.goto(f"{ORIGIN}/playback")
    return browser, page, media_requests


def _row_selector(mobile):
    return "#mobile-recent-events-list [data-mobile-clip]" if mobile else "#playback-clip-list button[data-inline-key]"


def _wait_playing(page, timeout=15000):
    page.wait_for_function("() => { const v = document.querySelector('.inline-media-card video'); return v && !v.paused && v.currentTime > 0.3; }", timeout=timeout)


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_desktop_recording_plays_inline_without_jumping_to_the_top(playwright_instance, page_html, webm, engine, channel):
    browser, page, media = _open(playwright_instance, engine, channel, page_html, webm)
    try:
        rows = page.locator(_row_selector(False))
        rows.nth(5).wait_for(timeout=15000)
        rows.nth(5).scroll_into_view_if_needed()
        before = page.evaluate("() => window.scrollY")
        assert before > 200
        top_src_before = page.evaluate("() => document.getElementById('playback-video').getAttribute('src')")
        rows.nth(5).click()
        _wait_playing(page)
        assert page.evaluate("(sel) => document.querySelectorAll(sel)[5].nextElementSibling.classList.contains('inline-media-card')", _row_selector(False))
        after = page.evaluate("() => window.scrollY")
        assert after > 200 and abs(after - before) < 400, (before, after)  # stayed at the row, no jump to the top
        assert page.evaluate("() => document.getElementById('playback-video').getAttribute('src')") == top_src_before  # top player untouched
        assert page.get_attribute(f"{_row_selector(False)} >> nth=5", "aria-expanded") == "true"

        # A second recording replaces the first cleanly.
        rows.nth(7).click()
        _wait_playing(page)
        assert page.locator(".inline-media-card").count() == 1
        assert page.evaluate("(sel) => document.querySelectorAll(sel)[7].nextElementSibling.classList.contains('inline-media-card')", _row_selector(False))
        assert page.get_attribute(f"{_row_selector(False)} >> nth=5", "aria-expanded") == "false"
        assert len(media) >= 2
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_inline_controls_work(playwright_instance, page_html, webm, engine, channel):
    browser, page, _ = _open(playwright_instance, engine, channel, page_html, webm)
    try:
        page.locator(_row_selector(False)).nth(2).click()
        _wait_playing(page)
        card = page.locator(".inline-media-card")
        card.locator('[data-act="play"]').click()
        assert page.evaluate("() => document.querySelector('.inline-media-card video').paused")
        page.wait_for_function("() => document.querySelector('.inline-media-card [data-act=\"play\"]').getAttribute('aria-label') === 'Play'")
        card.locator('[data-act="play"]').click()
        _wait_playing(page)
        muted = page.evaluate("() => document.querySelector('.inline-media-card video').muted")
        card.locator('[data-act="mute"]').click()
        assert page.evaluate("() => document.querySelector('.inline-media-card video').muted") is (not muted)
        assert card.locator('[data-act="download"]').get_attribute("href") == "/api/customer/recordings/cam-1/rec-2/media"  # third row; the list is newest first
        # Exercise the copy-link fallback (Edge has a real native share sheet).
        page.evaluate("() => Object.defineProperty(Navigator.prototype, 'share', {value: undefined, configurable: true})")
        card.locator('[data-act="share"]').click()
        page.wait_for_function("() => /\\/playback\\?camera=cam-1&t=\\d+/.test(document.querySelector('.inline-media-status').textContent) || /copied/i.test(document.querySelector('.inline-media-status').textContent)")
        assert card.locator('[data-act="fullscreen"]').is_visible()
        # No talk-down on recorded video.
        assert card.locator(".talk-mic").count() == 0
        # The top (timeline) player taking over pauses the inline recording.
        page.evaluate("""() => { const v = document.getElementById('playback-video'); v.src = document.querySelector('.inline-media-card video').currentSrc; v.muted = true; return v.play(); }""")
        page.wait_for_function("() => document.querySelector('.inline-media-card video').paused")
        card.locator('[data-act="close"]').click()
        assert page.locator(".inline-media-card").count() == 0
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_iphone_recording_plays_where_tapped_and_survives_refresh(playwright_instance, page_html, webm, engine, channel):
    browser, page, _ = _open(playwright_instance, engine, channel, page_html, webm, mobile=True)
    try:
        rows = page.locator(_row_selector(True))
        rows.nth(4).wait_for(timeout=15000)
        rows.nth(4).scroll_into_view_if_needed()
        before = page.evaluate("() => window.scrollY")
        rows.nth(4).tap()
        _wait_playing(page)
        assert page.evaluate("(sel) => document.querySelectorAll(sel)[4].nextElementSibling.classList.contains('inline-media-card')", _row_selector(True))
        after = page.evaluate("() => window.scrollY")
        assert before > 200 and abs(after - before) < 500, (before, after)
        video = page.evaluate_handle("() => document.querySelector('.inline-media-card video')")
        t0 = page.evaluate("(v) => v.currentTime", video)
        time.sleep(6.5)  # past the mobile list's 5 s refresh
        still = page.evaluate("(v) => ({connected: v.isConnected, paused: v.paused, t: v.currentTime, cards: document.querySelectorAll('.inline-media-card').length})", video)
        assert still["connected"] and not still["paused"] and still["t"] > t0 and still["cards"] == 1, still
        # Desktop-only date/timeline controls stay hidden on the phone.
        assert not page.is_visible("#playback-date-input") and not page.is_visible("#playback-monitor-timeline")
        buttons = page.evaluate("() => [...document.querySelectorAll('.inline-media-card .camera-tool:not([hidden])')].map(b => b.getBoundingClientRect().width)")
        assert buttons and min(buttons) >= 44
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS[:1], ids=["chromium"])
def test_event_clip_plays_inline_with_download(playwright_instance, page_html, webm, engine, channel):
    browser, page, _ = _open(playwright_instance, engine, channel, page_html, webm)
    try:
        row = page.locator('#playback-clip-list button[data-inline-key="event:evt-1"]')
        row.click()
        _wait_playing(page)
        assert row.evaluate("r => r.nextElementSibling && r.nextElementSibling.classList.contains('inline-media-card')")
        download = page.locator('.inline-media-card [data-act="download"]')
        assert download.is_visible() and download.get_attribute("href") == "/api/customer/recordings/cam-1/evt-1-file/media"
    finally:
        browser.close()
