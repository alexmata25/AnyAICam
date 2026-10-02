"""Regression coverage for the real Playback player-collapse/placeholder-
camera/nested-scroll bug (2026-09-16), found live by the user then
independently confirmed by Codex's own source review. See main.py's own
updated comments on `.playback-workspace-solo .camera-view` and
`#playback-monitor-timeline` for the full root-cause trace, and
PROJECT_CHECKPOINT.md for the incident writeup.

Note on the "1050px breakpoint" Codex's own report mentioned: checked
directly against source -- no `@media(max-width:1050px)` rule touches
Playback's own layout at all (the three real 1050px rules in this
codebase are Dashboard's intelligence grid, the RBAC user-admin page,
and the ops/backup page -- all unrelated). Playback's own real,
confirmed breakpoint is 900px (`.live-workspace,.playback-workspace
{grid-template-columns:1fr}` and `.monitor-timeline{display:none!
important}` both swap there). Tested against the real breakpoint
instead of the one named in the report, since a test asserting behavior
at a boundary that doesn't exist in source would pass or fail by
accident, not by proving anything real.
"""
import pytest

REAL_CAMERA_IDS = ("dfba6a63ec", "dc7a226120", "5c689a0c0e", "55bdd715ea", "41dc80c85e")


@pytest.fixture
def logged_in_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    return page


def _player_box(page):
    return page.locator("#playback-view-frame").bounding_box()


def _timeline_scroll_state(page):
    return page.evaluate(
        "() => { const el = document.getElementById('playback-monitor-timeline'); "
        "return {scrollHeight: el.scrollHeight, clientHeight: el.clientHeight}; }"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 1366, "height": 768}])
def test_player_maintains_a_real_16_9_frame_before_and_during_playback(page, e2e_credentials, viewport):
    """The exact regression: player collapsing to 0x0 the instant
    playback starts (placeholder hidden, video position:absolute and so
    contributing nothing to the old width:auto box). Checked at both
    viewports Codex's own report asked for."""
    email, password = e2e_credentials
    page.set_viewport_size(viewport)
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    page.wait_for_timeout(1000)

    before = _player_box(page)
    assert before["width"] > 200 and before["height"] > 100, f"player too small before playback: {before}"
    ratio_before = before["width"] / before["height"]
    assert 1.6 < ratio_before < 1.95, f"player is not a real ~16:9 frame before playback: ratio={ratio_before}"

    segments = page.locator(".event-segment")
    if segments.count() == 0:
        pytest.skip("no recording segments rendered for this camera/day -- nothing to click to start playback")
    segments.first.click()
    page.wait_for_timeout(1200)

    after = _player_box(page)
    assert after["width"] > 200 and after["height"] > 100, \
        f"REGRESSION: player collapsed during playback (before={before}, after={after})"
    ratio_after = after["width"] / after["height"]
    assert 1.6 < ratio_after < 1.95, f"player lost its ~16:9 shape during playback: ratio={ratio_after}"
    # The frame's own size must not depend on playback state at all --
    # the exact property the old width:auto rule got wrong.
    assert abs(before["width"] - after["width"]) < 2 and abs(before["height"] - after["height"]) < 2, \
        f"player size changed between before/after playback (before={before}, after={after}) -- should be identical"


@pytest.mark.e2e
def test_video_actually_decodes_and_progresses_during_playback(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    segments = page.locator(".event-segment")
    if segments.count() == 0:
        pytest.skip("no recording segments rendered -- nothing to play")
    segments.first.click()
    page.wait_for_function(
        "() => { const v = document.getElementById('playback-video'); return v.readyState >= 2; }",
        timeout=8000,
    )
    page.wait_for_timeout(1500)
    state = page.evaluate(
        "() => { const v = document.getElementById('playback-video'); "
        "return {currentTime: v.currentTime, videoWidth: v.videoWidth, videoHeight: v.videoHeight, paused: v.paused}; }"
    )
    assert state["videoWidth"] > 0 and state["videoHeight"] > 0, f"video never actually decoded a frame: {state}"
    assert not state["paused"], f"video should be playing, not paused: {state}"


@pytest.mark.e2e
def test_primary_timeline_never_has_its_own_nested_scrollbar(page, e2e_credentials):
    """The exact second regression: #playback-monitor-timeline's real
    winning CSS rule forced a fixed 285px box (scrollHeight 370 vs
    clientHeight 283 measured live pre-fix) -- checked directly via
    scrollHeight<=clientHeight, not by eyeballing a screenshot."""
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    page.wait_for_timeout(1000)
    state = _timeline_scroll_state(page)
    assert state["scrollHeight"] <= state["clientHeight"] + 2, \
        f"REGRESSION: primary timeline has its own nested scrollbar: {state}"


@pytest.mark.e2e
def test_playback_shows_exactly_the_five_real_provisioned_cameras_not_placeholders(page, e2e_credentials):
    """Exact set, not just count -- a future regression that swapped in
    5 different wrong ids (or silently dropped one of the 5 real ones)
    should fail this even if the count happened to still be 5."""
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    tiles = page.locator(".playback-camera-tile")
    rendered_ids = {tiles.nth(i).get_attribute("data-camera-id") for i in range(tiles.count())}
    assert rendered_ids == set(REAL_CAMERA_IDS), (
        f"expected exactly the 5 real provisioned cameras, got {rendered_ids} -- "
        "a placeholder (camera_number IS NULL) camera must never appear here again"
    )
    names = {tiles.nth(i).text_content() for i in range(tiles.count())}
    assert names == {"Camera 1", "Camera 2", "Camera 3", "Camera 4", "Camera 5"}


@pytest.mark.e2e
def test_switching_cameras_keeps_the_player_correctly_sized_and_timeline_unscrolled(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    tiles = page.locator(".playback-camera-tile")
    if tiles.count() < 2:
        pytest.skip("fewer than 2 real cameras -- cannot prove camera switching")
    tiles.nth(1).click()
    page.wait_for_timeout(1000)
    box = _player_box(page)
    assert box["width"] > 200 and box["height"] > 100, f"player collapsed after switching cameras: {box}"
    state = _timeline_scroll_state(page)
    assert state["scrollHeight"] <= state["clientHeight"] + 2, f"timeline gained a nested scrollbar after switching cameras: {state}"


@pytest.mark.e2e
def test_switching_dates_keeps_the_player_correctly_sized_and_timeline_unscrolled(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    page.fill("#playback-date-input", "2026-09-15")
    page.wait_for_timeout(1000)
    box = _player_box(page)
    assert box["width"] > 200 and box["height"] > 100, f"player collapsed after switching dates: {box}"
    state = _timeline_scroll_state(page)
    assert state["scrollHeight"] <= state["clientHeight"] + 2, f"timeline gained a nested scrollbar after switching dates: {state}"


@pytest.mark.e2e
def test_empty_state_before_any_selection_still_shows_a_correctly_sized_player(logged_in_page):
    """The placeholder/"No recordings available yet." state -- the
    player must be correctly sized here too, not only once real
    footage exists, since a customer's very first visit (or a
    currently-empty day) renders exactly this state."""
    page = logged_in_page
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    page.wait_for_timeout(800)
    box = _player_box(page)
    assert box["width"] > 200 and box["height"] > 100, f"player is not usable-sized in its initial/empty state: {box}"


@pytest.mark.e2e
def test_primary_toolbar_controls_are_visible_alongside_the_player(logged_in_page):
    page = logged_in_page
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    for control_id in ("skip-back", "timeline-play", "skip-forward", "create-clip", "browse-recordings"):
        locator = page.locator(f"#{control_id}")
        assert locator.count() == 1 and locator.is_visible(), f"primary control #{control_id} is not visible"


@pytest.mark.e2e
def test_page_fits_a_normal_desktop_viewport_with_the_real_regression_fix_in_place(logged_in_page):
    """The original 2026-09-16 usability requirement this whole feature
    started from, re-verified live now that the regression is fixed:
    video + full timeline visible together without page-level scroll
    being required to see both."""
    page = logged_in_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    page.wait_for_timeout(800)
    timeline_box = page.locator("#playback-monitor-timeline").bounding_box()
    timeline_bottom = timeline_box["y"] + timeline_box["height"]
    assert timeline_bottom <= 900, (
        f"timeline section bottom edge ({timeline_bottom}px) must fit within the 900px viewport alongside the video"
    )
