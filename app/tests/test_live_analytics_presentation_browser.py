"""Camera detail Analytics section, customer presentation (2026-09-25):
Smart Motion used to render bare rows like "person" +
"2026-09-25T18:08:49.232478" (a raw naive-UTC ISO string). Each result
now shows a friendly label, a viewer-local time, the event's own thumbnail
when one exists, confidence only when stored, and opens the event in
Playback. The upgrade card's buttons are real (View plans -> My
subscription; Add to This Camera -> the existing license-capped route).
Runs the REAL rendered page in real browsers with a mocked network (same
harness as test_live_p2p_upgrade_browser.py).

Opt-in (launches real browsers): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import base64
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

from test_live_p2p_upgrade_browser import BROWSERS, ORIGIN, Harness, _launch, _render_pages  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAJCAIAAAC0SDtlAAAAGklEQVR4nGNgGAWjYBSMglEwCkbBKBgFowAAHcAAAWDuRgUAAAAASUVORK5CYII=")
UPGRADE = {"description": "Count entries and exits.", "benefits": ["Traffic trends"]}
# 2026-09-25 18:08:49 UTC as epoch ms; the page must never show the raw ISO.
T1 = 1790359729232
SMART_MOTION = {
    "latest_timestamp": "2026-09-25T18:08:49.232478", "latest_timestamp_ms": T1,
    "recent": [
        {"event_type": "person", "timestamp": "2026-09-25T18:08:49.232478", "timestamp_ms": T1, "confidence": 0.54,
         "event_id": "evt-clip", "has_clip": True, "has_thumbnail": True},
        {"event_type": "car", "timestamp": "2026-09-25T18:01:10", "timestamp_ms": T1 - 459232, "confidence": None,
         "event_id": "evt-noclip", "has_clip": False, "has_thumbnail": False},
        {"event_type": "<b id=pwn>x</b>", "timestamp": "2026-09-25T17:00:00", "timestamp_ms": T1 - 4129232,
         "event_id": "evt-3", "has_clip": False, "has_thumbnail": False},
    ],
}


class PresentationHarness(Harness):
    def __init__(self, page, html, *, add_status=200):
        self.add_status = add_status
        self.add_posts = 0
        super().__init__(page, html)

    def _handle(self, route, html):
        url = route.request.url
        if url.endswith("/cameras/cam-a/analytics") and route.request.method == "GET":
            return self._json(route, {"analytics": [
                {"key": "smart_motion", "label": "Smart Motion", "enabled": True, "upgrade": UPGRADE},
                {"key": "people_counting", "label": "People Counting", "enabled": False, "upgrade": UPGRADE},
            ]})
        if url.endswith("/analytics/people_counting") and route.request.method == "POST":
            self.add_posts += 1
            if self.add_status != 200:
                return route.fulfill(status=self.add_status, content_type="application/json",
                                     body=json.dumps({"detail": "People Counting is not part of your plan for this site yet."}))
            return self._json(route, {"message": "Analytic enabled for this camera."})
        if url.endswith("/analytics/smart_motion/summary"):
            return self._json(route, SMART_MOTION)
        if url.endswith("/analytics/people_counting/summary"):
            return self._json(route, {"latest_count": 2, "entries": 3, "exits": 1, "latest_timestamp": None, "recent": []})
        if "/api/customer/events/cam-a/evt-clip/thumbnail" in url:
            return route.fulfill(status=200, content_type="image/png", body=PNG)
        return super()._handle(route, html)


@pytest.fixture(scope="module")
def pages():
    return _render_pages()


@pytest.fixture(scope="module")
def playwright_instance():
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as instance:
        yield instance


def _open(browser, html, **kwargs):
    context = browser.new_context(timezone_id="America/Chicago", viewport=kwargs.pop("viewport", {"width": 1440, "height": 900}))
    page = context.new_page()
    harness = PresentationHarness(page, html, **kwargs)
    page.goto(f"{ORIGIN}/page")
    page.wait_for_selector("#live-analytics-section:not([hidden])", timeout=10000)
    page.wait_for_selector("#live-analytics-panel .analytics-event-row", timeout=10000)
    return page, harness


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_smart_motion_rows_are_customer_friendly(playwright_instance, pages, engine, channel):
    browser = _launch(playwright_instance, engine, channel)
    try:
        page, _ = _open(browser, pages["single"])
        text = page.inner_text("#live-analytics-panel")
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text), text  # no raw ISO timestamps
        assert "Person detected" in text and "Car detected" in text
        assert "1:08:49 PM" in text  # 18:08:49 UTC shown in the viewer's zone (Chicago)
        assert "54% confidence" in text and text.count("confidence") == 1  # only where stored
        rows = page.query_selector_all("#live-analytics-panel a.analytics-event-row")
        hrefs = [row.get_attribute("href") for row in rows]
        assert hrefs[0] == "/playback?camera=cam-a&event=evt-clip&autoplay=event"
        assert hrefs[1] == f"/playback?camera=cam-a&t={T1 - 459232}&autoplay=event"
        assert "Play clip" in rows[0].inner_text() and "Open in Playback" in rows[1].inner_text()
        imgs = page.query_selector_all("#live-analytics-panel img.analytics-thumb")
        assert len(imgs) == 1 and imgs[0].get_attribute("src").endswith("/events/cam-a/evt-clip/thumbnail")
        assert page.evaluate("() => document.getElementById('pwn')") is None  # stored values stay text
        if engine == "chromium":
            out = Path(tempfile.gettempdir()) / "anyaicam-ui"
            out.mkdir(exist_ok=True)
            page.eval_on_selector("#live-analytics-section", "e => e.scrollIntoView()")
            page.screenshot(path=str(out / "live-analytics-desktop.png"))
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_rows_fit_a_phone_without_horizontal_scroll(playwright_instance, pages, engine, channel):
    browser = _launch(playwright_instance, engine, channel)
    try:
        page, _ = _open(browser, pages["single"], viewport={"width": 390, "height": 844})
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")
        widths = page.evaluate("() => [...document.querySelectorAll('#live-analytics-panel .analytics-event-row')].map(r => r.getBoundingClientRect().right <= innerWidth + 1)")
        assert all(widths)
        if engine == "chromium":
            out = Path(tempfile.gettempdir()) / "anyaicam-ui"
            out.mkdir(exist_ok=True)
            page.eval_on_selector("#live-analytics-section", "e => e.scrollIntoView()")
            page.screenshot(path=str(out / "live-analytics-mobile.png"))
    finally:
        browser.close()


@pytest.mark.parametrize("add_status", [200, 409])
def test_upgrade_card_buttons_are_real(playwright_instance, pages, add_status):
    browser = _launch(playwright_instance, "chromium", None)
    try:
        page, harness = _open(browser, pages["single"], add_status=add_status)
        page.click("#live-analytics-pills [data-key='people_counting']")
        page.wait_for_selector(".upgrade-card")
        assert page.get_attribute(".upgrade-card a.action-button", "href") == "/subscription-portal"
        assert "Learn More" not in page.inner_text(".upgrade-card")
        page.click("#upgrade-add-people_counting")
        if add_status == 200:
            page.wait_for_function("() => document.getElementById('live-analytics-panel').textContent.includes('Currently inside')")
        else:
            page.wait_for_function("() => document.getElementById('upgrade-result-people_counting').textContent.includes('not part of your plan')")
            assert page.is_enabled("#upgrade-add-people_counting")
        assert harness.add_posts == 1
    finally:
        browser.close()
