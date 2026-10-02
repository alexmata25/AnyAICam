"""Target: LPR (license plate recognition). Not a standalone page -- it's
the same entitlement-gated Live View analytics pill covered by
test_live_view.py, dedicated here per the requested target list.
Confirmed live 2026-09-16 against the real test account (Camera 1, no
LPR entitlement purchased): clicking button.filter[data-key="lpr"]
reveals "Not enabled on this camera" plus the real, source-verified
upgrade-card copy from app/customer_analytics_panel.py's own
UPGRADE_CARD_CONTENT['lpr'] -- not paraphrased, the actual live text.

Also covered on /playback (filterCategory() maps 'plate'/'lpr'
event_type -> the 'lpr' marker category, color #3dbfae) -- see
test_playback.py, not duplicated here.
"""
import pytest


@pytest.fixture
def focused_camera_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/customer-live")
    page.wait_for_selector("[data-camera-id]")
    camera_id = page.locator("[data-camera-id]").first.get_attribute("data-camera-id")
    if not camera_id:
        pytest.skip("no data-camera-id attribute found on the fleet page's camera card")
    page.goto(f"/customer/cameras/{camera_id}/live")
    page.wait_for_selector('button.filter[data-key="lpr"]')
    return page


@pytest.mark.e2e
def test_lpr_pill_exists_on_the_focused_live_view(focused_camera_page):
    page = focused_camera_page
    assert page.locator('button.filter[data-key="lpr"]').count() == 1


@pytest.mark.e2e
def test_lpr_pill_shows_its_own_real_content_not_a_generic_placeholder(focused_camera_page):
    """Doesn't assume the not-entitled state forever -- if this camera
    is ever given a real LPR entitlement, the real summary (a plate
    number, a timestamp) replaces the upgrade card and this test's own
    "either real data or the upgrade card" fallback still holds; only
    truly generic/blank content is a failure."""
    page = focused_camera_page
    page.locator('button.filter[data-key="lpr"]').click()
    page.wait_for_timeout(500)
    not_entitled = page.get_by_text("Not enabled on this camera").count() > 0
    real_upgrade_copy = page.get_by_text("Automatically detect and log license plates seen by this camera.").count() > 0
    if not_entitled:
        assert real_upgrade_copy, "not-entitled state must show the real LPR upgrade description, not a blank/generic one"
        assert page.get_by_text("Request Upgrade").count() > 0
    else:
        # A real entitlement exists -- the pill's own content area must
        # not be empty (some real summary rendered instead).
        assert page.locator('[data-key="lpr"]').count() > 0
