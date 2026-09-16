"""Target: dashboard/camera visibility. Real route confirmed from source
(app/main.py: @app.get("/dashboard")). Exact DOM selectors for the camera
tile list are TODO -- confirm on first authenticated run rather than
guess; PROJECT_CHECKPOINT.md notes this page also has a Smart Alerts
widget known to be disconnected from real data (2026-09-14 entry) --
worth an e2e assertion once selectors are known, but not a bug this
foundation fixes.
"""
import pytest


@pytest.mark.e2e
def test_dashboard_loads_after_login(page, base_url, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/dashboard")
    assert page.url.endswith("/dashboard")


@pytest.mark.e2e
def test_dashboard_shows_the_customers_real_cameras(page, base_url, e2e_credentials):
    pytest.skip("camera-tile selector on /dashboard not yet confirmed against a real authenticated session")
