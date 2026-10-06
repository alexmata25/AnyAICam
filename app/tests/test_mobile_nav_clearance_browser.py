"""License Plates "Load more" on phones (2026-10-06, staging finding): the
fixed phone bottom bar wraps to two (or more) rows on customer pages with the
Analytics entry, but the page reserved a fixed 92 px, so the bar covered the
last control and it could not be tapped. The page now keeps the bar's real
height (and whatever lies below it) free at the bottom.

The real page (rendered by the app) in Chromium; the events API is mocked
with two pages. Runs wherever Playwright and Chromium are available.
"""
import json

import pytest

from test_analytics_workspace_browser import ASSETS, ORIGIN, playwright_instance  # noqa: F401
from test_lpr_table_browser import lpr_page  # noqa: F401

PHONES = [(390, 844), (360, 740), (638, 900)]
FIRST, SECOND = 30, 10


def _read(n):
    return {"event_id": f"p-{n}", "camera_id": "cam-1", "event_type": "plate", "timestamp_ms": 1790000000000 - n * 60000,
            "confidence": 0.9, "has_clip": False, "has_thumbnail": False,
            "details": {"plate": f"P{n:04d}", "has_plate_image": False, "vehicle_type": "Car", "vehicle_color": "Blue",
                        "vehicle_make": None, "vehicle_model": None}}


def _open(playwright_instance, html, width, height):  # noqa: F811
    try:
        browser = playwright_instance.chromium.launch()
    except Exception as error:
        pytest.skip(f"browser unavailable: {error}")
    phone = width <= 760
    page = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone).new_page()

    def handle(route):
        url = route.request.url
        tail = url[len(ORIGIN):].split("?")[0]
        if tail == "/analytics/lpr":
            return route.fulfill(status=200, content_type="text/html", body=html)
        if tail.startswith("/static/") and tail[8:] in ASSETS:
            return route.fulfill(status=200, content_type="text/css" if tail.endswith(".css") else "application/javascript",
                                 body=ASSETS[tail[8:]])
        if tail == "/api/customer/analytics/lpr/events":
            older = "before=" in url
            events = [_read(n) for n in (range(FIRST, FIRST + SECOND) if older else range(FIRST))]
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"events": events, "summary": {"total": FIRST + SECOND}, "next_before": None if older else "cursor"}))
        return route.fulfill(status=404, body="")

    page.route("**/*", handle)
    page.goto(f"{ORIGIN}/analytics/lpr")
    page.wait_for_selector("#aw-more:not([hidden])")
    return browser, page


def _at_bottom(page):
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_function("() => Math.ceil(scrollY + innerHeight) >= document.documentElement.scrollHeight - 1")
    return page.evaluate("""() => {
        const more = document.getElementById('aw-more').getBoundingClientRect();
        const bar = document.querySelector('.mobile-nav');
        const shown = bar && getComputedStyle(bar).display !== 'none';
        const hit = document.elementFromPoint(more.left + more.width / 2, more.top + more.height / 2);
        return {top: more.top, bottom: more.bottom, inner: innerHeight, shown,
                barTop: shown ? bar.getBoundingClientRect().top : null, barHeight: shown ? bar.getBoundingClientRect().height : 0,
                hitIsButton: hit && hit.id === 'aw-more',
                padding: getComputedStyle(document.querySelector('main.content')).paddingBottom};
    }""")


@pytest.mark.parametrize("width,height", PHONES, ids=[f"{w}x{h}" for w, h in PHONES])
def test_load_more_is_fully_visible_and_tappable_above_the_phone_bar(playwright_instance, lpr_page, width, height):  # noqa: F811
    browser, page = _open(playwright_instance, lpr_page, width, height)
    try:
        geometry = _at_bottom(page)
        assert geometry["shown"], "the phone bottom bar is shown at this width"
        assert geometry["top"] >= 0 and geometry["bottom"] <= geometry["barTop"], geometry  # whole button above the bar
        assert geometry["hitIsButton"], "something covers the button's centre"
        page.tap("#aw-more")  # a real tap at the button
        page.wait_for_function(f"() => document.querySelectorAll('.aw-lpr-row').length === {FIRST + SECOND}")
        assert page.is_hidden("#aw-more")
    finally:
        browser.close()


def test_the_clearance_follows_the_bar_when_the_viewport_changes(playwright_instance, lpr_page):  # noqa: F811
    browser, page = _open(playwright_instance, lpr_page, 390, 844)
    try:
        first = _at_bottom(page)
        page.set_viewport_size({"width": 360, "height": 740})
        page.wait_for_function("() => true")
        second = _at_bottom(page)
        assert second["bottom"] <= second["barTop"] and second["hitIsButton"], second
        assert first["padding"] != second["padding"] or first["barHeight"] == second["barHeight"]
    finally:
        browser.close()


def test_desktop_layout_is_unchanged(playwright_instance, lpr_page):  # noqa: F811
    browser, page = _open(playwright_instance, lpr_page, 1280, 800)
    try:
        geometry = _at_bottom(page)
        assert not geometry["shown"]
        assert geometry["padding"] == "48px"  # the desktop shell's own bottom padding
        assert geometry["bottom"] <= geometry["inner"] and geometry["hitIsButton"]
    finally:
        browser.close()
