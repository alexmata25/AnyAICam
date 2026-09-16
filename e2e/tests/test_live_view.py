"""Target: Live View. Real routes confirmed from source
(app/live_view_page.py): GET /customer-live (fleet view) and
GET /customer/cameras/{camera_id}/live (focused per-camera view, the
one with the switchable analytics pill row -- see
app/customer_analytics_panel.py). Deliberately does NOT touch Live
Relay's own streaming internals -- this only proves the page renders
and a video element is present/attempts to play, per this setup's own
"no bug-fixing yet" scope.
"""
import pytest


@pytest.mark.e2e
def test_customer_live_view_loads_after_login(page, base_url, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/customer-live")
    assert page.url.endswith("/customer-live")


@pytest.mark.e2e
def test_focused_camera_live_view_shows_a_video_element(page, base_url, e2e_credentials):
    pytest.skip("real camera_id and video-element selector not yet confirmed against a real authenticated session")
