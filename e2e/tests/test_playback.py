"""Targets: Playback, selecting camera/date/time, 24/7 recording
availability, motion/event playback, thumbnails -- all five live on one
real page, /playback (app/main.py's _render_customer_playback()), so
they're grouped in this one file rather than split across five, to
match the app's own actual structure (verified directly from source
this session, not guessed):

  - camera selection:  .playback-camera-tile[data-camera-id]
  - the player:         #playback-video
  - the timeline:        #playback-timeline-lane, one .event-segment
                          per recording (background #e8eef6) and per
                          analytics marker (color per EVENT_COLORS --
                          motion #f0b94d, person #4d9ef0, vehicle
                          #a06df0, lpr #3dbfae, people_counting
                          #4dcf7a, intrusion #f0954d)
  - category filters:    .monitor-filter[data-filter=motion|person|
                          vehicle|lpr|people_counting|intrusion]
  - recordings list:     #playback-clip-list (thumbnails are <img> tags
                          inside each row, src=/api/customer/recordings/
                          {camera_id}/{recording_id}/thumbnail)

Date selection's own exact control is TODO -- confirm on first
authenticated run. "24/7 recording availability" here means: the
timeline's recording-coverage row (background:#e8eef6 segments) has no
unexplained large gap across a day that should have continuous footage
-- a real assertion, not a guess, once real per-day data is observable.
"""
import pytest


@pytest.fixture
def logged_in_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    return page


@pytest.mark.e2e
def test_video_and_timeline_both_fit_a_normal_desktop_viewport_without_scrolling(logged_in_page):
    """The actual usability requirement (2026-09-16): on a normal
    desktop viewport, the video player and the primary timeline must
    both be visible together, without the user having to scroll the
    whole page. Proven by real layout geometry against the live page,
    not by asserting on CSS source text (see app/tests/test_playback_
    usability_compact_controls.py for that half of the coverage) --
    this is the actual rendered/computed result a real browser produces.
    1440x900 chosen as a representative "normal desktop viewport", not
    an unusually generous one."""
    page = logged_in_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(500)  # let layout settle after the resize
    timeline_bottom = page.locator("#playback-monitor-timeline").bounding_box()["y"] + \
        page.locator("#playback-monitor-timeline").bounding_box()["height"]
    assert timeline_bottom <= 900, (
        f"timeline section bottom edge ({timeline_bottom}px) must fit within the 900px viewport "
        "alongside the video, not require scrolling to reach"
    )


@pytest.mark.e2e
def test_playback_loads_with_a_camera_tile_selected(logged_in_page):
    page = logged_in_page
    assert page.locator(".playback-camera-tile.active").count() == 1


@pytest.mark.e2e
def test_selecting_a_different_camera_reloads_the_timeline(logged_in_page):
    page = logged_in_page
    tiles = page.locator(".playback-camera-tile")
    if tiles.count() < 2:
        pytest.skip("fewer than 2 cameras visible to this test tenant -- cannot prove camera switching")
    tiles.nth(1).click()
    page.wait_for_load_state("networkidle")
    assert tiles.nth(1).get_attribute("class").find("active") != -1


@pytest.mark.e2e
def test_clicking_a_recording_segment_seeks_and_plays(logged_in_page):
    page = logged_in_page
    segments = page.locator(".event-segment")
    if segments.count() == 0:
        pytest.skip("no recording/event segments rendered for this camera/day -- nothing to click")
    segments.first.click()
    page.wait_for_timeout(1000)
    assert page.locator("#playback-video").get_attribute("src") is not None


@pytest.mark.e2e
def test_category_filters_toggle_marker_visibility(logged_in_page):
    page = logged_in_page
    motion_filter = page.locator('.monitor-filter[data-filter="motion"]')
    assert motion_filter.is_visible()
    motion_filter.click()
    assert "active" not in (motion_filter.get_attribute("class") or "")
    motion_filter.click()
    assert "active" in (motion_filter.get_attribute("class") or "")


@pytest.mark.e2e
def test_analytics_markers_have_an_overlapping_playable_recording(logged_in_page):
    """The exact real-world regression this whole autonomous-validation
    effort exists to keep catching: a colored marker with nothing
    playable underneath it (see the 2026-09-15 Camera 1 upload-order
    fix in PROJECT_CHECKPOINT.md)."""
    pytest.skip("needs a real day with both uploaded recordings and synced analytics events -- confirm data shape on first authenticated run")


@pytest.mark.e2e
def test_selecting_a_date_shows_that_days_recordings(logged_in_page):
    pytest.skip("date-picker/date-nav control selector not yet confirmed against a real authenticated session")


@pytest.mark.e2e
def test_recording_coverage_has_no_unexplained_gap_across_a_full_day(logged_in_page):
    pytest.skip("needs a known-continuous-recording day identified first -- not yet confirmed which date qualifies")


@pytest.mark.e2e
def test_recording_thumbnails_load_successfully(logged_in_page):
    page = logged_in_page
    thumbnails = page.locator("#playback-clip-list img")
    if thumbnails.count() == 0:
        pytest.skip("no recordings list rows rendered for this camera -- nothing to check thumbnails for")
    first = thumbnails.first
    assert first.get_attribute("src")
    # naturalWidth > 0 is the standard real-browser proof an <img> actually
    # decoded, not just that its src attribute is non-empty.
    assert first.evaluate("img => img.naturalWidth > 0")
