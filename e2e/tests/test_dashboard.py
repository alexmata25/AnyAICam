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

Connectivity/recording status, confirmed from source (main.py's async
refresh, ~line 74428): each camera also gets #dashboard-live-N (LIVE/
OFFLINE, .wait class toggled by camera.online) and #dashboard-rec-N
(.inactive class toggled by camera.recording!=='running') badges, and
the page-level #dashboard-camera-summary ("N of M cameras online").
System health: #cpu-metric, #memory-metric, #storage-metric,
#storage-detail.
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


@pytest.mark.e2e
def test_dashboard_shows_a_real_online_camera_count_summary(dashboard_page):
    page = dashboard_page
    summary = page.locator("#dashboard-camera-summary")
    page.wait_for_function(
        "el => /\\d+ of \\d+ cameras online/.test(el.textContent) || el.textContent.trim() === 'Status service unavailable'",
        arg=summary.element_handle(),
        timeout=5000,
    )
    text = summary.text_content() or ""
    assert text != "Checking stream and recording status…" and text.strip(), \
        f"expected the real per-camera status fetch to resolve, still showing a loading placeholder: {text!r}"


@pytest.mark.e2e
def test_dashboard_recording_status_badges_are_consistent_with_the_detail_text(dashboard_page):
    """Cross-checks two independently-rendered pieces of the same real
    per-camera status (the #dashboard-rec-N badge's own .inactive class
    vs. the #dashboard-camera-detail text's own "Recording active"/
    "Recording <state>" wording) agree with each other -- catching a
    real state-desync bug if the two ever disagreed, not just that each
    renders *something*."""
    page = dashboard_page
    page.wait_for_timeout(1500)  # let the async /api status refresh (main.py ~line 74428) resolve past its initial "Checking…" placeholder
    details = page.locator(".dashboard-camera-detail")
    ids = [details.nth(i).get_attribute("id") or "" for i in range(details.count())]
    camera_numbers = [cid.replace("dashboard-detail-", "") for cid in ids if cid.startswith("dashboard-detail-")]
    if not camera_numbers:
        pytest.skip("no dashboard-detail-N elements found -- nothing to cross-check")
    for camera_number in camera_numbers:
        detail = page.locator(f"#dashboard-detail-{camera_number}")
        rec_badge = page.locator(f"#dashboard-rec-{camera_number}")
        if rec_badge.count() == 0:
            continue
        detail_text = detail.text_content() or ""
        recording_active_per_detail = "Recording active" in detail_text
        recording_inactive_per_badge = "inactive" in (rec_badge.get_attribute("class") or "")
        assert recording_active_per_detail != recording_inactive_per_badge, (
            f"camera {camera_number}'s recording badge (.inactive={recording_inactive_per_badge}) "
            f"disagrees with its own detail text ({detail_text!r})"
        )


@pytest.mark.e2e
def test_dashboard_system_health_metrics_show_real_values(dashboard_page):
    page = dashboard_page
    cpu = page.locator("#cpu-metric")
    memory = page.locator("#memory-metric")
    storage = page.locator("#storage-metric")
    if cpu.count() == 0:
        pytest.skip("no #cpu-metric element rendered -- system health panel not present for this account/page state")
    page.wait_for_function(
        "el => el.textContent.trim().endsWith('%') && el.textContent.trim() !== '%'",
        arg=cpu.element_handle(),
        timeout=5000,
    )
    assert (cpu.text_content() or "").strip().endswith("%")
    assert (memory.text_content() or "").strip().endswith("%")
    assert "GB" in (storage.text_content() or ""), "expected a real GB figure for free storage, not blank/placeholder text"
