"""Target: dashboard/camera visibility. Real route confirmed from source
(app/main.py: @app.get("/dashboard")). Selectors confirmed live
2026-09-16 against the real test account: one
<div class="dashboard-camera-name">Camera N</div> +
<div class="dashboard-camera-detail" id="dashboard-detail-N">Online ·
Recording active</div> pair per real camera (5, matching the 5 real
cameras). Real system stats visible too (System health, CPU, Memory,
Storage) plus a Smart alerts panel with real notification rows -- see
PROJECT_CHECKPOINT.md's 2026-09-14 note that this widget was previously
disconnected from real data; confirmed live here that it now shows
real "Motion detected on Camera 1" rows, not empty/dead.
"""
import pytest


@pytest.fixture
def dashboard_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto("/dashboard")
    page.wait_for_selector(".dashboard-camera-name")
    return page


@pytest.mark.e2e
def test_dashboard_loads_after_login(dashboard_page):
    page = dashboard_page
    assert page.url.endswith("/dashboard")
    assert page.title() == "Dashboard · AnyAiCam"


@pytest.mark.e2e
def test_dashboard_shows_the_customers_real_cameras(dashboard_page):
    page = dashboard_page
    names = page.locator(".dashboard-camera-name")
    assert names.count() == 5, "expected exactly the 5 real cameras this customer has"
    visible_names = {names.nth(i).text_content() for i in range(names.count())}
    assert visible_names == {"Camera 1", "Camera 2", "Camera 3", "Camera 4", "Camera 5"}


@pytest.mark.e2e
def test_dashboard_camera_cards_show_a_real_status_detail(dashboard_page):
    page = dashboard_page
    details = page.locator(".dashboard-camera-detail")
    assert details.count() == 5
    for i in range(details.count()):
        text = details.nth(i).text_content()
        assert text and text.strip(), f"camera detail row {i} must not be blank"


@pytest.mark.e2e
def test_clicking_a_dashboard_camera_navigates_toward_its_live_view(dashboard_page):
    """Confirmed live 2026-09-16: this lands on /camera/{number} (a
    separate, shorter route from Live View's own /customer/cameras/
    {id}/live -- not a bug, just a different real route for the same
    destination; this test asserts what actually happens, not an
    assumption carried over from test_live_view.py)."""
    page = dashboard_page
    page.locator(".dashboard-camera-name").first.click()
    for _ in range(20):
        if "/camera/" in page.url:
            break
        page.wait_for_timeout(300)
    assert "/camera/" in page.url, f"expected to navigate toward a per-camera view, still on {page.url}"
