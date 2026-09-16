"""Target: camera status. Real routes confirmed from source (app/main.py:
@app.get("/camera-health"); app/live_view_page.py:
@app.get("/api/customer/cameras/{camera_id}/status")). Exact per-camera
status-indicator selectors on /camera-health are TODO.
"""
import pytest


@pytest.mark.e2e
def test_camera_health_page_loads_after_login(page, base_url, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/camera-health")
    assert page.url.endswith("/camera-health")


@pytest.mark.e2e
def test_camera_health_shows_a_status_for_every_real_camera(page, base_url, e2e_credentials):
    pytest.skip("status-indicator selector not yet confirmed against a real authenticated session")
