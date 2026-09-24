"""Cross-browser check of the Focused Live View analytics section
(2026-09-24): values from stored events (OCR'd plate text, person names,
appliance-reported event types) are HTML-escaped, LPR confidence is shown
as a percentage, and facial recognition has its own summary -- run on the
REAL rendered single-camera page in Chromium, Microsoft Edge and WebKit,
all served from the same harness origin with the same mocked network
(see test_live_p2p_upgrade_browser.py for the harness).

Opt-in (launches real browsers): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

from test_live_p2p_upgrade_browser import BROWSERS, ORIGIN, Harness, _launch, _render_pages  # noqa: E402

HOSTILE_PLATE = '<img src=x onerror="window.__pwned=1">'
HOSTILE_NAME = '<b id="injected">Eve</b>'
UPGRADE = {"description": "d", "benefits": ["b"]}


class AnalyticsHarness(Harness):
    def _handle(self, route, html):
        url = route.request.url
        if url.endswith("/cameras/cam-a/analytics"):
            return self._json(route, {"analytics": [
                {"key": "lpr", "label": "LPR", "enabled": True, "upgrade": UPGRADE},
                {"key": "facial_recognition", "label": "AAC Facial Recognition", "enabled": True, "upgrade": UPGRADE},
            ]})
        if url.endswith("/analytics/lpr/summary"):
            return self._json(route, {"latest_plate": HOSTILE_PLATE, "latest_confidence": 0.87, "latest_timestamp": "t", "recent": [{}]})
        if url.endswith("/analytics/facial_recognition/summary"):
            return self._json(route, {"latest_state": "known", "recent": [{"person": HOSTILE_NAME, "state": "known", "timestamp": "2026-09-24T10:00:00"}]})
        return super()._handle(route, html)


@pytest.fixture(scope="module")
def pages():
    return _render_pages()


@pytest.fixture(scope="module")
def playwright_instance():
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as instance:
        yield instance


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_analytics_values_render_as_text_never_markup(playwright_instance, pages, engine, channel):
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_page()
        AnalyticsHarness(page, pages["single"])
        page.goto(f"{ORIGIN}/page")
        page.wait_for_selector("#live-analytics-section:not([hidden])", timeout=10000)
        page.wait_for_function("() => document.getElementById('live-analytics-panel').textContent.includes('87%')", timeout=10000)
        panel_text = page.inner_text("#live-analytics-panel")
        assert HOSTILE_PLATE in panel_text  # shown literally, as text
        assert page.evaluate("() => window.__pwned") is None
        assert page.evaluate("() => document.querySelectorAll('#live-analytics-panel img').length") == 0

        page.click("#live-analytics-pills [data-key='facial_recognition']")
        page.wait_for_function("() => document.getElementById('live-analytics-panel').textContent.includes('Eve')", timeout=10000)
        assert HOSTILE_NAME in page.inner_text("#live-analytics-panel")
        assert page.evaluate("() => document.getElementById('injected')") is None
    finally:
        browser.close()
