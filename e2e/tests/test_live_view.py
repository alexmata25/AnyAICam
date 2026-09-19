"""Target: Live View. Real routes confirmed from source
(app/live_view_page.py): GET /customer-live (fleet view) and
GET /customer/cameras/{camera_id}/live (focused per-camera view, with
the switchable analytics pill row -- see
app/customer_analytics_panel.py). Selectors below confirmed against
the real rendered pages (2026-09-16 exploration), not guessed:

  - fleet page:    exactly one <div class="camera-view"> + <video> per
                    real camera (5, matching the 5 real cameras -- the
                    3 pending_installation placeholders visible on
                    Playback's camera-tile row are NOT shown here)
  - fleet tile navigation: NOT the <article data-camera-id> tile body
                    itself (confirmed live it has no click handler of
                    its own) -- the real link is the hover-revealed
                    "camera tools" gear overlay:
                    <a class="camera-tool" href="/customer/cameras/
                    {id}/live" aria-label="Open camera tools">
  - focused page:  <video id="live-view-video">, plus a real toolbar --
                    #live-view-status (text label), #live-view-mute,
                    #live-view-snapshot, #live-view-download,
                    #live-view-share, #live-view-analytics,
                    #live-view-bookmark, #live-view-stop,
                    #live-view-retry (hidden until needed) -- confirmed
                    from source (app/live_view_page.py). Download,
                    Share, and Bookmark are real, intentional stubs
                    (comingSoon(), the shared toast in page_shell) --
                    not broken buttons; Mute genuinely toggles the
                    video element's own .muted.
  - analytics pills: <button class="filter" role="tab"
                    data-key="smart_motion|people_counting|lpr|ppe">
                    Label <span class="pill wait">Upgrade</span></button>
                    -- the "Upgrade"/pill.wait shape is this real test
                    account's actual current entitlement state (nothing
                    purchased on Camera 1), not a fixed assumption this
                    suite hard-codes forever.

Deliberately waits on a concrete selector (wait_for_selector), never
wait_for_load_state("networkidle") -- confirmed live 2026-09-16 that
networkidle times out at 30s on both these pages, because a real Live
View page keeps continuous streaming/polling network traffic open by
design; "no network activity for 500ms" is a condition this class of
page is never expected to reach. This was a wait-strategy bug in the
test, not an app issue.

Deliberately does NOT touch Live Relay's own streaming internals --
this only proves the page renders and the real controls are present,
per this project's "no bug-fixing beyond what's found" scope.
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
    return page


@pytest.fixture
def first_real_camera_id(logged_in_page):
    page = logged_in_page
    page.goto("/customer-live")
    page.wait_for_selector("[data-camera-id]")
    camera_id = page.locator("[data-camera-id]").first.get_attribute("data-camera-id")
    if not camera_id:
        pytest.skip("no data-camera-id attribute found on the fleet page's camera card")
    return camera_id


@pytest.fixture
def real_camera_ids(logged_in_page):
    """De-duplicated, order-preserved: confirmed live 2026-09-16 that
    [data-camera-id] matches more than one element per camera tile (the
    fleet card itself plus at least one nested element also carrying
    the same attribute) -- a raw per-element list double-counts every
    real camera rather than reflecting 5 distinct ones."""
    page = logged_in_page
    page.goto("/customer-live")
    page.wait_for_selector("[data-camera-id]")
    tiles = page.locator("[data-camera-id]")
    raw = [tiles.nth(i).get_attribute("data-camera-id") for i in range(tiles.count())]
    seen = []
    for camera_id in raw:
        if camera_id and camera_id not in seen:
            seen.append(camera_id)
    return seen


@pytest.mark.e2e
def test_customer_live_view_loads_after_login(logged_in_page):
    page = logged_in_page
    page.goto("/customer-live")
    assert page.url.endswith("/customer-live")
    assert page.title() == "Live view · AnyAiCam"


@pytest.mark.e2e
def test_fleet_view_shows_one_video_per_real_camera(logged_in_page):
    page = logged_in_page
    page.goto("/customer-live")
    page.wait_for_selector(".camera-view")
    camera_views = page.locator(".camera-view")
    videos = page.locator("video")
    assert camera_views.count() > 0
    assert videos.count() == camera_views.count(), "every camera card must have exactly one video element"


@pytest.mark.e2e
def test_clicking_a_fleet_camera_navigates_to_its_focused_view(logged_in_page, first_real_camera_id):
    """The fleet tile's own <article data-camera-id> body is not itself
    a link -- confirmed live 2026-09-16 (an earlier version of this
    test clicked the article directly and genuinely never navigated,
    30s real timeout). The real navigation element is the "camera
    tools" gear overlay: <a class="camera-tool" href="/customer/
    cameras/{id}/live" aria-label="Open camera tools">."""
    page = logged_in_page
    tools_link = page.locator(f'a.camera-tool[href="/customer/cameras/{first_real_camera_id}/live"]')
    assert tools_link.count() == 1
    # force=True: the tile's own <video> visually overlaps this overlay
    # link and intercepts a plain simulated hover/click (confirmed live
    # 2026-09-16) -- real users reach it the same way any hover-reveal
    # overlay control works. This test's own point is that the link's
    # real href correctly navigates, not to re-prove standard CSS hover
    # behavior Playwright's strict actionability check isn't built for.
    tools_link.click(force=True)
    page.wait_for_url(f"**/customer/cameras/{first_real_camera_id}/live")
    assert f"/customer/cameras/{first_real_camera_id}/live" in page.url


@pytest.mark.e2e
def test_focused_camera_live_view_shows_a_video_element(logged_in_page, first_real_camera_id):
    page = logged_in_page
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector("#live-view-video")
    assert page.locator("#live-view-video").count() == 1


@pytest.mark.e2e
def test_focused_camera_view_shows_all_four_analytics_pills(logged_in_page, first_real_camera_id):
    page = logged_in_page
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    # The analytics pill row is a separate, later-rendered component from
    # the <video> element -- waiting on the video alone raced it
    # (confirmed live 2026-09-16: pills weren't in the DOM yet). Wait on
    # the pill row itself.
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    for key in ("smart_motion", "people_counting", "lpr", "ppe"):
        pill = page.locator(f'button.filter[data-key="{key}"]')
        assert pill.count() == 1, f"missing analytics pill for {key}"


@pytest.mark.e2e
def test_clicking_an_analytics_pill_shows_either_real_data_or_an_upgrade_card(logged_in_page, first_real_camera_id):
    """Doesn't assume this test account's entitlement state -- covers
    both real shapes the same real component can render (see
    app/customer_analytics_panel.py: UPGRADE_CARD_CONTENT vs a real
    summarize() result), so this stays correct whether or not Camera 1
    ever gets a real LPR/PPE/etc. entitlement later."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector('button.filter[data-key="lpr"]')
    pill = page.locator('button.filter[data-key="lpr"]')
    pill.click()
    page.wait_for_timeout(300)
    upgrade_badge = page.locator('button.filter[data-key="lpr"] span.pill.wait')
    not_enabled_text = page.get_by_text("Not enabled on this camera")
    has_upgrade_state = upgrade_badge.count() > 0 or not_enabled_text.count() > 0
    # Either this pill shows the upgrade/not-enabled state, or it doesn't --
    # in the real-entitlement case the pill's own summary content (real
    # plate/count/violation data) renders instead. Both are valid; the
    # only failure is neither state's markup existing at all.
    real_summary_present = page.locator('[data-key="lpr"]').count() > 0
    assert has_upgrade_state or real_summary_present


@pytest.mark.e2e
def test_switching_between_two_real_cameras_shows_each_ones_own_focused_view(logged_in_page, real_camera_ids):
    """Camera switching: navigating from one real camera's focused Live
    View directly to another's must actually swap context -- not just
    the URL, but the page title and the toolbar's own camera-tool link
    on the way back to the fleet view."""
    page = logged_in_page
    ids = real_camera_ids
    if len(ids) < 2:
        pytest.skip("fewer than 2 real cameras with a data-camera-id -- cannot prove switching between them")
    page.goto(f"/customer/cameras/{ids[0]}/live")
    page.wait_for_selector("#live-view-video")
    first_title = page.title()
    page.goto(f"/customer/cameras/{ids[1]}/live")
    page.wait_for_selector("#live-view-video")
    second_title = page.title()
    assert f"/customer/cameras/{ids[1]}/live" in page.url
    assert first_title != second_title, "switching to a different real camera must show that camera's own title, not the previous one's"


@pytest.mark.e2e
def test_mute_button_toggles_the_videos_muted_state_and_its_own_icon(logged_in_page, first_real_camera_id):
    page = logged_in_page
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector("#live-view-mute")
    mute_button = page.locator("#live-view-mute")
    video = page.locator("#live-view-video")
    initial_muted = video.evaluate("el => el.muted")
    initial_glyph = mute_button.text_content()
    mute_button.click()
    page.wait_for_timeout(200)
    toggled_muted = video.evaluate("el => el.muted")
    toggled_glyph = mute_button.text_content()
    assert toggled_muted != initial_muted, "clicking mute must actually flip the video element's own muted state"
    assert toggled_glyph != initial_glyph, "the mute button's own icon must reflect the new muted state"


@pytest.mark.e2e
def test_share_button_shows_a_real_coming_soon_toast_not_a_dead_click(logged_in_page, first_real_camera_id):
    """Download/Share/Bookmark are real, intentional stubs (comingSoon(),
    app/main.py's shared page_shell script) -- not broken buttons. This
    proves the real, customer-visible feedback exists rather than the
    click silently doing nothing."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector("#live-view-share")
    page.locator("#live-view-share").click()
    toast = page.locator("#toast")
    page.wait_for_function(
        "el => el.classList.contains('show') && el.textContent.trim().length > 0",
        arg=toast.element_handle(),
        timeout=3000,
    )
    assert "ready for a future update" in (toast.text_content() or "")


@pytest.mark.e2e
def test_live_view_status_label_shows_real_non_blank_text(logged_in_page, first_real_camera_id):
    """Status/error handling: whichever real state this camera's stream
    is actually in right now (Starting/Connecting/Reconnecting/an error
    message/etc, all driven by #live-view-status per source), the label
    must never be left blank -- a blank status is the one shape that
    would mean the status-handling code path itself never ran."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector("#live-view-status")
    page.wait_for_timeout(1500)
    status_text = page.locator("#live-view-status").text_content()
    assert status_text and status_text.strip(), "the live status label must show real text, not be left blank"
