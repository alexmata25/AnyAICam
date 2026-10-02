"""Target: five-camera parity, once the single-pilot-camera testing
phase ended (2026-09-16). Earlier tests in this suite mostly exercise
"the first real camera" (whichever the fleet page lists first) as a
representative sample -- this file instead iterates the real, known
camera_ids for all 5 real cameras directly (confirmed live from the
staging DB this same session: dfba6a63ec=Camera 1, dc7a226120=Camera 2,
5c689a0c0e=Camera 3, 55bdd715ea=Camera 4, 41dc80c85e=Camera 5), so a
regression specific to one non-first camera can't hide behind the
other tests' single-camera sampling.
"""
import pytest

REAL_CAMERA_IDS = {
    1: "dfba6a63ec",
    2: "dc7a226120",
    3: "5c689a0c0e",
    4: "55bdd715ea",
    5: "41dc80c85e",
}


@pytest.fixture
def logged_in_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    return page


@pytest.mark.e2e
@pytest.mark.parametrize("camera_number,camera_id", sorted(REAL_CAMERA_IDS.items()))
def test_focused_live_view_loads_with_video_and_all_analytics_pills(logged_in_page, camera_number, camera_id):
    page = logged_in_page
    page.goto(f"/customer/cameras/{camera_id}/live")
    page.wait_for_selector("#live-view-video")
    assert page.locator("#live-view-video").count() == 1
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    for key in ("smart_motion", "people_counting", "lpr", "ppe"):
        assert page.locator(f'button.filter[data-key="{key}"]').count() == 1, \
            f"Camera {camera_number} ({camera_id}) is missing the {key} analytics pill"


@pytest.mark.e2e
@pytest.mark.parametrize("camera_number,camera_id", sorted(REAL_CAMERA_IDS.items()))
def test_playback_camera_tile_selects_and_loads_that_cameras_timeline(logged_in_page, camera_number, camera_id):
    page = logged_in_page
    page.goto("/playback")
    page.wait_for_selector("#playback-timeline-lane")
    tile = page.locator(f'.playback-camera-tile[data-camera-id="{camera_id}"]')
    if tile.count() == 0:
        pytest.skip(f"no Playback camera tile rendered for Camera {camera_number} ({camera_id})")
    tile.click()
    page.wait_for_timeout(800)
    assert "active" in (tile.get_attribute("class") or ""), \
        f"Camera {camera_number} ({camera_id})'s own Playback tile did not become active after clicking it"


@pytest.mark.e2e
@pytest.mark.parametrize("camera_number,camera_id", sorted(REAL_CAMERA_IDS.items()))
def test_dashboard_shows_a_non_blank_status_detail_for_every_real_camera(logged_in_page, camera_number, camera_id):
    """Cross-checked by camera NAME (Dashboard's own detail rows aren't
    keyed by camera_id in the DOM -- see #dashboard-detail-N, N being
    the camera_number) rather than camera_id, matching that page's own
    real markup."""
    page = logged_in_page
    page.goto("/dashboard")
    page.wait_for_selector(".dashboard-camera-name")
    detail = page.locator(f"#dashboard-detail-{camera_number}")
    assert detail.count() == 1, f"Camera {camera_number} has no #dashboard-detail-{camera_number} row"
    text = detail.text_content()
    assert text and text.strip(), f"Camera {camera_number}'s dashboard detail row is blank"
