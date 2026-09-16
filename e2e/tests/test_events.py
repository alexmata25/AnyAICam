"""Target: Events / Smart Alerts. Real route confirmed from source
(app/main.py: @app.get("/events")). Selectors confirmed live 2026-09-16
against the real test account (23,902 real events):

  <tr data-event-camera data-event-type data-event-id
      data-event-timestamp data-event-has-clip="0|1"
      data-media-state="processing|...">
    <td>time</td><td>Camera N</td>
    <td class="event-thumbnail-cell">...</td>
    <td><span class="pill">Type</span></td><td>confidence%</td>
    <td class="event-action-cell">
      <a class="download" href="/customer/cameras/{id}/live">Live view</a>
      <span class="download event-action-pending" ...>Playback</span>  (pending)
      -- OR a real Playback <a> once has-clip=1 --
    </td>
  </tr>
  camera filters: #events-camera-filters (per-camera checkboxes)
  search:         #events-search

**Real finding, not a test bug** (see PROJECT_CHECKPOINT.md's own
2026-09-16 entry for full detail): the event-media (clip/thumbnail)
pipeline has a genuine backlog across ALL 5 cameras, not a camera-
scope restriction -- of 23,902 real events, only ~9.7% ever got a
media row, and even this account's own most recent events (literally
minutes old) don't have one yet. This looks like a CPU/throughput
bottleneck on the Ryzen appliance, not a simple config bug -- fixing
it would mean touching Ryzen, which requires separate authorization
per docs/AUTONOMOUS_VALIDATION_PERMISSIONS.md, not attempted here.
Tests below cover what IS real and verifiable today (the list, the
filters, the search, and the UI's own correct handling of the
pending/not-ready state) and skip cleanly -- not falsely -- when no
ready clip exists to click through.
"""
import pytest


@pytest.fixture
def events_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/events")
    page.wait_for_selector("[data-event-id]")
    return page


@pytest.mark.e2e
def test_events_page_loads_after_login(events_page):
    page = events_page
    assert page.url.endswith("/events")
    assert page.title() == "Events · AnyAiCam"


@pytest.mark.e2e
def test_events_shows_the_customers_real_recent_activity(events_page):
    page = events_page
    rows = page.locator("[data-event-id]")
    assert rows.count() > 0, "expected at least one real event row"


@pytest.mark.e2e
def test_events_camera_filter_checkboxes_exist_for_every_real_camera(events_page):
    """5, not 8 -- updated 2026-09-16 alongside the Playback regression
    fix (PROJECT_CHECKPOINT.md's own entry): _customer_playback_cameras()
    -- shared by Events, Investigate, Alerts, and Playback alike -- now
    excludes pending_installation placeholder cameras (camera_number IS
    NULL) the same way Live View's and Dashboard's own camera lists
    already did, resolving the previously-documented 8-vs-5 cross-page
    inconsistency for real rather than leaving it as a known gap."""
    page = events_page
    checkboxes = page.locator("#events-camera-filters input[type=checkbox]")
    assert checkboxes.count() == 5
    for i in range(checkboxes.count()):
        assert checkboxes.nth(i).is_checked(), "camera filters default to all-checked (nothing hidden by default)"


@pytest.mark.e2e
def test_unchecking_a_camera_filter_hides_that_cameras_events(events_page):
    page = events_page
    first_checkbox = page.locator("#events-camera-filters input[type=checkbox]").first
    camera_num = first_checkbox.get_attribute("data-camera")
    rows_for_camera_before = page.locator(f'[data-event-camera="{camera_num}"]:visible').count()
    if rows_for_camera_before == 0:
        pytest.skip(f"no visible events for camera {camera_num} in the current window -- nothing to prove got hidden")
    first_checkbox.click()
    page.wait_for_timeout(300)
    rows_for_camera_after = page.locator(f'[data-event-camera="{camera_num}"]:visible').count()
    assert rows_for_camera_after == 0, f"unchecking camera {camera_num}'s filter must hide all of its events"


@pytest.mark.e2e
def test_search_box_narrows_the_visible_events(events_page):
    page = events_page
    total = page.locator("[data-event-id]:visible").count()
    search = page.locator("#events-search")
    assert search.count() == 1
    search.fill("zzz_no_such_event_should_match_zzz")
    page.wait_for_timeout(300)
    filtered = page.locator("[data-event-id]:visible").count()
    assert filtered < total, "an unmatchable search term must narrow the visible rows, proving search runs against real data"


@pytest.mark.e2e
def test_a_pending_event_shows_processing_state_not_a_broken_link(events_page):
    """The real, current, honest state for most events right now (see
    module docstring's real-backlog finding) -- proves the UI handles
    "not ready yet" correctly rather than showing a dead/broken link."""
    page = events_page
    pending = page.locator('[data-media-state="processing"]').first
    if pending.count() == 0:
        pytest.skip("no event currently in the processing window -- nothing to check")
    thumbnail_cell = pending.locator(".event-thumbnail-cell")
    assert "Processing" in (thumbnail_cell.text_content() or "")
    playback_action = pending.locator(".event-action-cell .event-action-pending")
    assert playback_action.count() == 1
    assert playback_action.get_attribute("aria-disabled") == "true"


@pytest.mark.e2e
def test_opening_a_ready_event_plays_its_recording(events_page):
    """The actual requested end-to-end check -- skipped cleanly (not
    falsely failed) when no event in the current visible window has a
    ready clip, which is the real, current state of this account's
    data per the module docstring's backlog finding. Written so it
    starts asserting real behavior the moment any event does have
    has-clip=1, without needing to be rewritten."""
    page = events_page
    ready_row = page.locator('[data-event-has-clip="1"]').first
    if ready_row.count() == 0:
        pytest.skip("no event with a ready clip in the current visible window -- see the real backlog finding in this file's own module docstring")
    ready_row.locator(".event-action-cell a").last.click()
    page.wait_for_timeout(1000)
    assert "/playback" in page.url or page.locator("video").count() > 0
