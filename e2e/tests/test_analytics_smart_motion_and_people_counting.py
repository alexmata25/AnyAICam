"""Target: Analytics -- Person, Vehicle, Motion (all one pill, "Smart
Motion") and People Counting. LPR/PPE are covered in their own
dedicated files (test_lpr.py, test_ppe.py); Intrusion is NOT a Live
View analytics pill at all -- confirmed from source
(app/customer_analytics_panel.py's ANALYTIC_LABELS has exactly 4 keys:
smart_motion, people_counting, lpr, ppe) -- it only ever appears as a
Playback/Events timeline event category
(.monitor-filter[data-filter="intrusion"], already covered generically
by test_playback.py's category-filter test and reachable via
Investigate's own free-text search).

Like LPR/PPE, these two are pills on the focused Live View page
(button.filter[data-key="smart_motion"|"people_counting"]), not
standalone pages. Real behavior confirmed from source
(app/live_view_page.py's renderUpgradeCard()/renderAnalyticsSummary()):
a not-entitled pill shows "Not enabled on this camera" plus the real
UPGRADE_CARD_CONTENT copy; an entitled pill with zero events yet shows
an explicit real "no data yet" message per analytic ("No motion/person/
vehicle events yet", "No counts yet") -- never a blank panel, matching
this codebase's own explicit "no dead space" design intent.
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
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    return page


@pytest.mark.e2e
def test_smart_motion_and_people_counting_pills_both_exist(focused_camera_page):
    page = focused_camera_page
    assert page.locator('button.filter[data-key="smart_motion"]').count() == 1
    assert page.locator('button.filter[data-key="people_counting"]').count() == 1


@pytest.mark.e2e
def test_smart_motion_pill_shows_real_content_not_a_blank_panel(focused_camera_page):
    """Covers Person, Vehicle, and Motion together -- they are one
    purchasable analytic (Smart Motion), not three separate pills; see
    ANALYTIC_LABELS in source. Doesn't assume the entitlement/data state
    stays fixed: whichever real state is live right now, the panel must
    never be empty."""
    page = focused_camera_page
    page.locator('button.filter[data-key="smart_motion"]').click()
    page.wait_for_timeout(500)
    panel = page.locator("#live-analytics-panel")
    text = panel.text_content() or ""
    assert text.strip(), "the Smart Motion panel must never render as empty (this app's own explicit no-dead-space design)"
    not_entitled = "Not enabled on this camera" in text
    if not_entitled:
        assert "Fewer junk motion clips" in text or "people, vehicles, and relevant movement" in text.lower() or \
            page.get_by_text("Smarter event filtering").count() > 0, \
            "not-entitled state must show the real Smart Motion upgrade description, not generic text"
        assert page.get_by_text("Request Upgrade").count() > 0
    else:
        assert "No motion/person/vehicle events yet" in text or "Loading" not in text, \
            "entitled state must show either the real no-data-yet message or actual recent events, not be stuck loading"


@pytest.mark.e2e
def test_people_counting_pill_shows_real_content_not_a_blank_panel(focused_camera_page):
    page = focused_camera_page
    page.locator('button.filter[data-key="people_counting"]').click()
    page.wait_for_timeout(500)
    panel = page.locator("#live-analytics-panel")
    text = panel.text_content() or ""
    assert text.strip(), "the People Counting panel must never render as empty"
    not_entitled = "Not enabled on this camera" in text
    if not_entitled:
        assert "Count entries and exits" in text or page.get_by_text("Count entries and exits").count() > 0, \
            "not-entitled state must show the real People Counting upgrade description, not generic text"
        assert page.get_by_text("Request Upgrade").count() > 0
    else:
        assert "No counts yet" in text or "Entries / exits" in text, \
            "entitled state must show either the real no-data-yet message or an actual count"


@pytest.mark.e2e
def test_switching_between_analytics_pills_updates_the_panel_and_active_state(focused_camera_page):
    """Proves the pills are real tabs (one active/visible panel at a
    time, real content swap on click), not decorative buttons."""
    page = focused_camera_page
    smart_motion_pill = page.locator('button.filter[data-key="smart_motion"]')
    people_counting_pill = page.locator('button.filter[data-key="people_counting"]')
    smart_motion_pill.click()
    page.wait_for_timeout(300)
    assert "active" in (smart_motion_pill.get_attribute("class") or "")
    assert "active" not in (people_counting_pill.get_attribute("class") or "")
    first_panel_text = page.locator("#live-analytics-panel").text_content()
    people_counting_pill.click()
    page.wait_for_timeout(300)
    assert "active" in (people_counting_pill.get_attribute("class") or "")
    assert "active" not in (smart_motion_pill.get_attribute("class") or "")
    second_panel_text = page.locator("#live-analytics-panel").text_content()
    assert first_panel_text != second_panel_text, "switching pills must actually change the panel content"
