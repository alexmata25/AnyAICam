"""Target: camera status. Real routes confirmed from source (app/main.py:
@app.get("/camera-health"); app/live_view_page.py:
@app.get("/api/customer/cameras/{camera_id}/status")).

Real finding (2026-09-16, this real test account): /camera-health shows
"Your current role does not include view_analytics. Ask an
administrator to update your access." -- this account's real role
lacks the view_analytics permission this page requires. Not a bug (a
real, working authorization gate) -- the test below covers both real
shapes this page can render (the access-denied message, or real
per-camera status rows if the account ever has the permission) rather
than assuming one forever.
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


@pytest.mark.e2e
def test_camera_health_page_loads_after_login(logged_in_page):
    page = logged_in_page
    page.goto("/camera-health")
    assert page.url.endswith("/camera-health")
    assert page.title() == "Camera health · AnyAiCam"


@pytest.mark.e2e
def test_camera_health_shows_either_real_status_rows_or_a_clear_permission_message(logged_in_page):
    page = logged_in_page
    page.goto("/camera-health")
    page.wait_for_load_state("networkidle")
    access_denied = page.get_by_text("does not include", exact=False)
    # Real per-camera status rows would render as visible camera names;
    # checked broadly (no single confirmed selector for the authorized
    # case yet -- this test account cannot reach it) rather than a guess.
    has_denial_message = access_denied.count() > 0
    has_any_camera_name = any(
        page.get_by_text(f"Camera {n}", exact=False).count() > 0 for n in range(1, 6)
    )
    assert has_denial_message or has_any_camera_name, (
        "expected either the real access-control denial message or real per-camera rows -- got neither"
    )
    if has_denial_message:
        assert page.get_by_text("view_analytics").count() > 0, "denial message should name the specific missing permission"
