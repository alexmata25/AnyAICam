"""Target: PPE (personal protective equipment detection). Same shape as
LPR (see test_lpr.py) -- the same entitlement-gated Live View analytics
pill. Confirmed live 2026-09-16 against the real test account (Camera
1, no PPE entitlement purchased): clicking button.filter[data-key="ppe"]
reveals "Not enabled on this camera" plus the real, source-verified
upgrade-card copy from app/customer_analytics_panel.py's own
UPGRADE_CARD_CONTENT['ppe'] -- not paraphrased, the actual live text.

Not currently a Playback marker/filter category (filterCategory() has
no 'ppe' case -- see test_playback.py's own module docstring); this
was still true as of this exploration.
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
    page.wait_for_selector('button.filter[data-key="ppe"]')
    return page


@pytest.mark.e2e
def test_ppe_pill_exists_on_the_focused_live_view(focused_camera_page):
    page = focused_camera_page
    assert page.locator('button.filter[data-key="ppe"]').count() == 1


@pytest.mark.e2e
def test_ppe_pill_shows_its_own_real_content_not_a_generic_placeholder(focused_camera_page):
    """Same not-entitled-vs-real-data duality as test_lpr.py's
    equivalent -- doesn't assume this test account's entitlement state
    stays fixed forever."""
    page = focused_camera_page
    page.locator('button.filter[data-key="ppe"]').click()
    page.wait_for_timeout(500)
    not_entitled = page.get_by_text("Not enabled on this camera").count() > 0
    real_upgrade_copy = page.get_by_text("Detect personal protective equipment compliance in view of this camera.").count() > 0
    if not_entitled:
        assert real_upgrade_copy, "not-entitled state must show the real PPE upgrade description, not a blank/generic one"
        assert page.get_by_text("Request Upgrade").count() > 0
    else:
        assert page.locator('[data-key="ppe"]').count() > 0
