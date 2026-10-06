"""License Plates keep themselves current (2026-10-06): while the License
Plates workspace is open and visible, new plate reads appear on their own.

The real page (rendered by the app) runs in Chromium with a fake clock; the
events API is mocked by a test-controlled server, so every refresh is
counted and new reads, failures and later clips can be injected. Runs
wherever Playwright and Chromium are available; skipped otherwise.
"""
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from test_analytics_workspace_browser import ASSETS, ORIGIN, SNAPSHOT, pages, playwright_instance, webm  # noqa: F401
from test_lpr_table_browser import lpr_page  # noqa: F401

REFRESH_MS = 5000
NOW_MS = int(time.time() * 1000)


def _read(event_id, minutes_ago, plate, *, has_clip=True):
    return {"event_id": event_id, "camera_id": "cam-1", "event_type": "plate", "timestamp_ms": int(NOW_MS - minutes_ago * 60000),
            "confidence": 0.93, "has_clip": has_clip, "has_thumbnail": True,
            "details": {"plate": plate, "has_plate_image": False, "vehicle_type": "Car", "vehicle_color": "Blue",
                        "vehicle_make": None, "vehicle_model": None}}


class Server:
    """The mocked events API: what it returns now, and every request it got."""

    def __init__(self, page_size=None):
        self.events = [_read("p-2", 10, "BBB222"), _read("p-1", 30, "AAA111")]
        self.fail = False
        self.calls = []
        self.page_size = page_size  # None: everything on one page
        self.hold = False  # True: requests stay pending until release()
        self.held = []

    def respond(self, route, url):
        query = parse_qs(urlsplit(url).query)
        self.calls.append(query)
        if self.hold:
            self.held.append((route, query))
            return None
        return self.answer(route, query)

    def release(self):
        """Answers every pending request with the server's state as it is now."""
        held, self.held, self.hold = self.held, [], False
        for route, query in held:
            self.answer(route, query)

    def answer(self, route, query):
        if self.fail:
            return route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "busy"}))
        events = sorted(self.events, key=lambda e: e["timestamp_ms"], reverse=True)
        total = len(events)
        if "before" in query:  # like the real API: strictly older than the cursor
            events = [e for e in events if e["timestamp_ms"] < int(query["before"][0])]
        next_before = None
        if self.page_size and len(events) > self.page_size:
            events = events[:self.page_size]
            next_before = str(events[-1]["timestamp_ms"])
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"events": events, "summary": {"total": total}, "next_before": next_before, "enabled_camera_ids": ["cam-1"]}))


def _open(playwright_instance, html, server, path="/analytics/lpr", clip=None):  # noqa: F811
    try:
        browser = playwright_instance.chromium.launch()
    except Exception as error:
        pytest.skip(f"browser unavailable: {error}")
    page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
    # Time stands still: timers fire only when a test advances the clock.
    page.clock.install(time=NOW_MS / 1000 - 1)
    page.clock.pause_at(NOW_MS / 1000)

    def handle(route):
        url = route.request.url
        tail = url[len(ORIGIN):].split("?")[0]
        if tail == path:
            return route.fulfill(status=200, content_type="text/html", body=html)
        if tail.startswith("/static/") and tail[8:] in ASSETS:
            return route.fulfill(status=200, content_type="text/css" if tail.endswith(".css") else "application/javascript",
                                 body=ASSETS[tail[8:]])
        if tail.startswith("/api/customer/analytics/") and tail.endswith("/events"):
            return server.respond(route, url)
        if tail.endswith("/thumbnail") or tail.endswith("/plate-image"):
            return route.fulfill(status=200, content_type="image/jpeg", body=SNAPSHOT)
        if clip is not None and tail.endswith("/media/url"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"url": "/clips/clip.webm"}))
        if clip is not None and tail.startswith("/clips/"):
            return route.fulfill(status=200, content_type="video/webm", body=clip)
        return route.fulfill(status=404, body="")

    page.route("**/*", handle)
    page.goto(f"{ORIGIN}{path}")
    return browser, page


def _until(page, condition, timeout=8.0, what="condition"):
    """Polls from Python (the page's own timers are on the fake clock),
    letting Playwright deliver route callbacks in between."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        page.evaluate("1")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _settle(page, seconds=0.4):
    """Lets any in-flight request finish without advancing the page clock."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        page.evaluate("1")
        time.sleep(0.02)


def _plates(page):
    return page.evaluate("[...document.querySelectorAll('.aw-lpr-row [data-label=\"Plate\"]')].map(c => c.innerText.trim())")


def _event_ids(page):
    return page.evaluate("[...document.querySelectorAll('.aw-lpr-row')].map(r => r.dataset.event)")


def _set_hidden(page, hidden):
    page.evaluate("""hidden => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => hidden ? 'hidden' : 'visible'});
        document.dispatchEvent(new Event('visibilitychange'));
    }""", hidden)


@pytest.fixture()
def lpr(playwright_instance, lpr_page):  # noqa: F811
    server = Server()
    browser, page = _open(playwright_instance, lpr_page, server)
    _until(page, lambda: len(_event_ids(page)) == 2, what="the initial plate reads")
    yield page, server
    browser.close()


def test_the_initial_load_still_happens(lpr):
    page, server = lpr
    assert len(server.calls) == 1
    assert _plates(page) == ["BBB222", "AAA111"]


def test_new_reads_appear_every_five_seconds_while_open(lpr):
    page, server = lpr
    server.events.append(_read("p-3", 1, "CCC333"))
    page.clock.run_for(REFRESH_MS - 100)
    _settle(page)
    assert len(server.calls) == 1, "refreshed before the 5-second interval"
    page.clock.run_for(100)
    _until(page, lambda: len(server.calls) == 2, what="the first refresh")
    _until(page, lambda: _plates(page)[:1] == ["CCC333"], what="the new read at the top")
    assert _plates(page) == ["CCC333", "BBB222", "AAA111"]
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(server.calls) == 3, what="the second refresh")


def test_repeated_reloads_and_visibility_events_never_add_timers(lpr):
    page, server = lpr
    for value in ("today", "7", "30", "7"):  # each filter change reloads the workspace
        page.select_option("#aw-range", value)
        _until(page, lambda: len(_event_ids(page)) == 2, what="the reload")
    for _ in range(3):
        _set_hidden(page, True)
        _set_hidden(page, False)  # each "visible" refreshes once, at once
    _settle(page)
    base = len(server.calls)
    for tick in range(1, 4):  # 5 s at a time, letting each refresh answer (the next is chained to it)
        page.clock.run_for(REFRESH_MS)
        _settle(page)
        assert len(server.calls) - base == tick, f"expected exactly one refresh per 5 s, got {len(server.calls) - base} after {tick * 5} s"


def test_a_hidden_tab_does_not_poll_and_a_visible_one_refreshes_at_once(lpr):
    page, server = lpr
    _set_hidden(page, False)
    _set_hidden(page, True)
    _settle(page)
    base = len(server.calls)
    page.clock.run_for(6 * REFRESH_MS)
    _settle(page)
    assert len(server.calls) == base, "polled while the tab was hidden"
    server.events.append(_read("p-3", 1, "CCC333"))
    _set_hidden(page, False)  # no clock advance: the refresh is immediate
    _until(page, lambda: len(server.calls) == base + 1, what="the immediate refresh")
    _until(page, lambda: "CCC333" in _plates(page), what="the read that arrived while hidden")


def test_leaving_the_page_stops_polling(lpr):
    page, server = lpr
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    base = len(server.calls)
    page.clock.run_for(4 * REFRESH_MS)
    _settle(page)
    assert len(server.calls) == base


def test_a_page_restored_from_the_back_forward_cache_resumes(lpr):
    page, server = lpr
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.clock.run_for(2 * REFRESH_MS)
    _settle(page)
    base = len(server.calls)
    server.events.append(_read("p-3", 1, "CCC333"))
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    _until(page, lambda: len(server.calls) == base + 1, what="the catch-up refresh")
    _until(page, lambda: "CCC333" in _plates(page), what="the read that arrived while away")
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(server.calls) == base + 2, what="polling resumed")


def test_refreshes_keep_the_current_filters_and_controls(lpr):
    page, server = lpr
    page.select_option("#aw-range", "today")
    _until(page, lambda: len(server.calls) == 2, what="the range reload")
    page.fill("#aw-search", "BBB")
    page.clock.run_for(300)  # the search box's own debounce
    _until(page, lambda: len(server.calls) == 3, what="the search reload")
    filtered = server.calls[-1]
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(server.calls) == 4, what="the refresh")
    refreshed = server.calls[-1]
    for name in ("start_ms", "end_ms", "q", "result"):
        assert refreshed.get(name) == filtered.get(name), name
    assert refreshed.get("q") == ["BBB"] and "before" not in refreshed
    assert page.input_value("#aw-search") == "BBB" and page.input_value("#aw-range") == "today"


def test_refreshes_never_duplicate_rows(lpr):
    page, server = lpr
    for _ in range(3):
        page.clock.run_for(REFRESH_MS)
        _settle(page)
    server.events.append(_read("p-3", 1, "CCC333"))
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(_event_ids(page)) == 3, what="the new read")
    page.clock.run_for(REFRESH_MS)
    _settle(page)
    ids = _event_ids(page)
    assert sorted(ids) == ["p-1", "p-2", "p-3"] and len(ids) == len(set(ids))


def test_a_failed_refresh_keeps_the_table_and_the_next_one_recovers(lpr):
    page, server = lpr
    server.fail = True
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(server.calls) == 2, what="the failing refresh")
    _settle(page)
    assert _plates(page) == ["BBB222", "AAA111"]
    assert page.evaluate("document.querySelector('#aw-results .aw-empty')") is None
    server.fail = False
    server.events.append(_read("p-3", 1, "CCC333"))
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: _plates(page) == ["CCC333", "BBB222", "AAA111"], what="recovery")


def test_a_failed_first_load_is_replaced_by_the_first_successful_refresh(playwright_instance, lpr_page):  # noqa: F811
    server = Server()
    server.fail = True
    browser, page = _open(playwright_instance, lpr_page, server)
    try:
        _until(page, lambda: page.evaluate("!!document.querySelector('#aw-results .aw-empty')") and len(server.calls) == 1,
               what="the failed first load")
        server.fail = False
        page.clock.run_for(REFRESH_MS)
        _until(page, lambda: _plates(page) == ["BBB222", "AAA111"], what="recovery")
    finally:
        browser.close()


def test_a_clip_attached_after_the_read_shows_up_without_a_reload(lpr):
    page, server = lpr
    server.events[0]["has_clip"] = False
    page.select_option("#aw-range", "30")
    _until(page, lambda: "View snapshot" in page.inner_text(".aw-lpr-row >> nth=0"), what="the read without a clip")
    server.events[0]["has_clip"] = True  # the plate-triggered recording finished
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: "Play clip" in page.inner_text(".aw-lpr-row >> nth=0"), what="the clip button")
    assert _event_ids(page) == ["p-2", "p-1"]


def test_an_older_late_read_is_placed_in_time_order(lpr):
    page, server = lpr
    server.events.append(_read("p-0", 20, "MID000"))  # synced late, between the two shown
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(_event_ids(page)) == 3, what="the late read")
    assert _plates(page) == ["BBB222", "MID000", "AAA111"]


def test_other_analytics_workspaces_do_not_poll(playwright_instance, pages):  # noqa: F811
    server = Server()
    browser, page = _open(playwright_instance, pages["/analytics/smart-motion"], server, path="/analytics/smart-motion")
    try:
        _until(page, lambda: len(server.calls) == 1, what="the initial smart-motion load")
        page.clock.run_for(6 * REFRESH_MS)
        _set_hidden(page, False)
        _settle(page)
        assert len(server.calls) == 1
    finally:
        browser.close()


def _timestamps(page):
    return page.evaluate("[...document.querySelectorAll('.aw-lpr-row')].map(r => r.dataset.event)")


def test_a_whole_page_of_new_reads_keeps_loaded_pages_and_an_open_clip(playwright_instance, lpr_page, webm):  # noqa: F811
    """Codex review of b7c7b72: a refreshed first page made entirely of new
    reads (with more pages behind it) replaced the table, dropping the rows
    "Load more" had added and closing a clip open on one of them."""
    server = Server(page_size=3)
    server.events = [_read(f"old-{n}", 10 * n, f"OLD{n:03d}") for n in range(1, 10)]  # 9 reads: three pages
    browser, page = _open(playwright_instance, lpr_page, server, clip=webm)
    try:
        _until(page, lambda: _event_ids(page) == ["old-1", "old-2", "old-3"], what="the first page")
        page.click("#aw-more")  # "Load more"
        _until(page, lambda: len(_event_ids(page)) == 6, what="the second page")
        assert page.is_visible("#aw-more"), "a third page remains"
        older = page.locator('.aw-lpr-row[data-event="old-5"]')
        older.locator(".aw-lpr-open").click()  # play a clip of an older, "Load more" row
        _until(page, lambda: page.evaluate("""() => {
            const row = document.querySelector('.aw-lpr-row[data-event="old-5"]');
            return row.getAttribute('aria-expanded') === 'true' && row.nextElementSibling.classList.contains('inline-media-card');
        }"""), what="the open clip")
        card = page.evaluate_handle("document.querySelector('.inline-media-card')")

        # More new reads than one page: the refreshed first page is all new and has a cursor.
        server.events += [_read(f"new-{n}", -n / 60, f"NEW{n:03d}") for n in range(1, 5)]  # newest is new-4
        page.clock.run_for(REFRESH_MS)
        _until(page, lambda: len(_event_ids(page)) == 10, what="the new reads")
        _settle(page)

        ids = _event_ids(page)
        assert ids == ["new-4", "new-3", "new-2", "new-1", "old-1", "old-2", "old-3", "old-4", "old-5", "old-6"]
        assert len(ids) == len(set(ids))
        stamps = page.evaluate("[...document.querySelectorAll('.aw-lpr-row')].map(r => r.querySelector('[data-label=\"Time\"]').innerText)")
        assert len(stamps) == 10
        # The open clip is the same element, still under the same read.
        assert page.evaluate("""card => {
            const row = document.querySelector('.aw-lpr-row[data-event="old-5"]');
            return card.isConnected && row.getAttribute('aria-expanded') === 'true' && row.nextElementSibling === card;
        }""", card)
        assert page.evaluate("document.querySelectorAll('.inline-media-card').length") == 1
        # Pagination is where it was: "Load more" brings the third page, once.
        assert page.is_visible("#aw-more")
        page.click("#aw-more")
        _until(page, lambda: len(_event_ids(page)) == 13, what="the third page")
        ids = _event_ids(page)
        assert ids[-3:] == ["old-7", "old-8", "old-9"] and len(ids) == len(set(ids))
        assert page.is_hidden("#aw-more")
        assert page.evaluate("""() => document.querySelector('.aw-lpr-row[data-event="old-5"]').nextElementSibling
                                    === document.querySelector('.inline-media-card')""")
    finally:
        browser.close()


def test_catching_up_reads_only_until_it_reaches_what_is_shown(lpr):
    """The refresh reads further pages only while every read on them is new."""
    page, server = lpr
    server.page_size = 2
    server.events += [_read(f"new-{n}", -n / 60, f"NEW{n:03d}") for n in range(1, 4)]  # 3 new reads
    base = len(server.calls)
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(_event_ids(page)) == 5, what="the new reads")
    _settle(page)
    assert _event_ids(page) == ["new-3", "new-2", "new-1", "p-2", "p-1"]
    assert len(server.calls) - base == 2  # page one (all new), then page two (reaches p-2): stop
    assert "before" in server.calls[-1] and "before" not in server.calls[-2]


# ---------------------------------------------------------------- lifecycle while a refresh is in flight
# (Codex review of 94e9a2c.)

def test_pagehide_during_a_pending_refresh_stops_polling_and_leaves_the_page_alone(lpr):
    page, server = lpr
    server.hold = True
    page.clock.run_for(REFRESH_MS)  # the automatic refresh starts...
    _until(page, lambda: len(server.held) == 1, what="the pending refresh")
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")  # ...and the user leaves meanwhile
    server.events.append(_read("p-3", 1, "CCC333"))
    server.release()  # the stale request now completes
    _settle(page)
    base = len(server.calls)
    for _ in range(3):  # well past the next intervals: nothing restarts
        page.clock.run_for(REFRESH_MS)
        _settle(page)
    assert len(server.calls) == base, "polling restarted after pagehide"
    assert _event_ids(page) == ["p-2", "p-1"], "the departed page was changed"

    # Back/forward-cache restore: one immediate catch-up, then exactly one chain.
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    _until(page, lambda: len(server.calls) == base + 1, what="the immediate refresh")
    _until(page, lambda: _event_ids(page) == ["p-3", "p-2", "p-1"], what="the catch-up")
    for tick in range(1, 4):
        page.clock.run_for(REFRESH_MS)
        _settle(page)
        assert len(server.calls) == base + 1 + tick, f"expected one refresh per 5 s after restore (tick {tick})"


def test_a_page_load_pageshow_is_not_a_restore(lpr):
    page, server = lpr
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: false}))")
    _settle(page)
    base = len(server.calls)
    for tick in range(1, 3):
        page.clock.run_for(REFRESH_MS)
        _settle(page)
        assert len(server.calls) == base + tick  # still one chain, no extra refresh


def test_typing_a_search_makes_a_pending_refresh_stale(lpr):
    page, server = lpr
    server.hold = True
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(server.held) == 1, what="the pending refresh")
    assert server.calls[-1].get("q") is None  # the refresh used the old (empty) search
    page.fill("#aw-search", "BBB")  # typed; its load waits 300 ms
    server.events.append(_read("p-3", 1, "CCC333"))  # what the old search would now return
    server.release()
    _settle(page)
    assert _event_ids(page) == ["p-2", "p-1"], "a response for the old search was shown"
    page.clock.run_for(300)
    _until(page, lambda: server.calls[-1].get("q") == ["BBB"], what="the search load")


def test_no_refresh_starts_while_a_typed_search_waits_for_its_debounce(lpr):
    page, server = lpr
    page.clock.run_for(REFRESH_MS - 100)
    _settle(page)
    base = len(server.calls)
    page.fill("#aw-search", "BBB")
    page.clock.run_for(100)  # the refresh tick falls inside the 300 ms debounce
    _settle(page)
    assert len(server.calls) == base, "refreshed with the old search while the new one was pending"
    page.clock.run_for(200)  # the debounce ends: one load, with the new search
    _until(page, lambda: len(server.calls) == base + 1, what="the search load")
    assert server.calls[-1].get("q") == ["BBB"]
    page.clock.run_for(REFRESH_MS)
    _until(page, lambda: len(server.calls) == base + 2, what="polling with the new search")
    assert server.calls[-1].get("q") == ["BBB"]
