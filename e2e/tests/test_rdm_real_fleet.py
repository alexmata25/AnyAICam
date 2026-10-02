"""RDM entitlement ON/OFF verification against the REAL 5-camera fleet
(not the synthetic harness in test_rdm_entitlement_harness.py).
Explicitly authorized 2026-09-16: the user clarified there are no real
AnyAiCam customers or real customer billing yet -- this staging account
is development/test data, not a launched product's real billing
records. See e2e/scripts/provision_real_fleet_subscriptions.py (grants
real analytics_subscriptions, licensed_quantity=5, matching the real
fleet size exactly) and toggle_real_fleet_entitlement.py (flips
entitlements through the real assign_entitlement()/remove_entitlement()
product functions, never raw SQL).

Toggling itself is applied out-of-band (via the same SSM+docker-exec
mechanism the rest of this session's live infrastructure work already
uses) before each test runs -- this file only VERIFIES the real,
customer-facing Live View pill state after each toggle, exactly the
product surface a real customer/partner would observe.
"""
import pytest

REAL_CAMERAS = {
    1: "dfba6a63ec",
    2: "dc7a226120",
    3: "5c689a0c0e",
    4: "55bdd715ea",
    5: "41dc80c85e",
}
ANALYTIC_KEYS = ("smart_motion", "people_counting", "lpr", "ppe")


@pytest.fixture
def logged_in_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    return page


def _pill_state(page, analytic_key: str) -> str:
    """A fixed page.wait_for_timeout(400) here occasionally raced the
    click-triggered render live against staging (found during real RDM
    fleet testing 2026-09-16 -- a direct, cache-bypassing API check of
    the exact same camera/analytic proved the underlying entitlement
    data was always correct even on the runs where this looked wrong,
    so the race was in this test's own wait strategy, not the app).
    Polls for the panel to actually settle instead of guessing a fixed
    delay, the same fix shape already applied elsewhere in this suite
    for the identical class of problem (e.g. thumbnail decode races)."""
    page.locator(f'button.filter[data-key="{analytic_key}"]').click()
    page.wait_for_function(
        "() => { const t = document.getElementById('live-analytics-panel').textContent; "
        "return t && t.trim().length > 0 && !t.includes('Loading'); }",
        timeout=5000,
    )
    panel_text = page.locator("#live-analytics-panel").text_content() or ""
    return "not_entitled" if "Not enabled on this camera" in panel_text else "entitled"


@pytest.mark.e2e
@pytest.mark.parametrize("camera_number,camera_id", sorted(REAL_CAMERAS.items()))
@pytest.mark.parametrize("analytic_key", ANALYTIC_KEYS)
def test_all_four_analytics_entitled_on_all_five_real_cameras(logged_in_page, camera_number, camera_id, analytic_key):
    """The 'everything ON' known-good starting state this whole RDM
    pass builds from -- 20 real assertions (5 cameras x 4 analytics)."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{camera_id}/live")
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    state = _pill_state(page, analytic_key)
    assert state == "entitled", (
        f"Camera {camera_number} ({camera_id})'s {analytic_key} pill should show the real/entitled state -- got {state}"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("camera_number,camera_id", sorted(REAL_CAMERAS.items()))
def test_lpr_disabled_fleet_wide_is_isolated_from_other_analytics(logged_in_page, camera_number, camera_id):
    """Verifies the state AFTER RDM turned LPR off fleet-wide (applied
    out-of-band before this test runs -- see the toggle script):
    LPR must show not-entitled on every real camera, while the other 3
    analytics -- untouched by this toggle -- must still show entitled.
    Proves RDM's OFF action is both real (LPR actually flips) and
    correctly scoped (nothing else flips with it)."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{camera_id}/live")
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    assert _pill_state(page, "lpr") == "not_entitled", f"Camera {camera_number}: LPR should be disabled fleet-wide"
    for other_key in ("smart_motion", "people_counting", "ppe"):
        assert _pill_state(page, other_key) == "entitled", (
            f"Camera {camera_number}: {other_key} must remain entitled -- disabling LPR must not affect it"
        )


@pytest.mark.e2e
@pytest.mark.parametrize("camera_number,camera_id", sorted(REAL_CAMERAS.items()))
def test_lpr_and_ppe_disabled_in_combination_isolated_from_the_other_two(logged_in_page, camera_number, camera_id):
    """Combination toggle (applied out-of-band before this test runs):
    LPR and PPE both off fleet-wide, Smart Motion and People Counting
    both still on -- proves a multi-feature RDM change composes
    correctly rather than only ever being tested one at a time."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{camera_id}/live")
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    for off_key in ("lpr", "ppe"):
        assert _pill_state(page, off_key) == "not_entitled", f"Camera {camera_number}: {off_key} should be disabled"
    for on_key in ("smart_motion", "people_counting"):
        assert _pill_state(page, on_key) == "entitled", f"Camera {camera_number}: {on_key} should remain entitled"


@pytest.mark.e2e
@pytest.mark.parametrize("camera_number,camera_id", sorted(REAL_CAMERAS.items()))
@pytest.mark.parametrize("analytic_key", ANALYTIC_KEYS)
def test_all_four_analytics_restored_to_entitled_after_the_rdm_off_on_cycle(logged_in_page, camera_number, camera_id, analytic_key):
    """Final state, applied out-of-band before this test runs: every
    analytic turned back ON for every real camera after the individual
    and combination OFF tests above -- restoring the known-good
    'fully enabled five-camera' baseline this whole engagement built
    toward, and proving RDM's ON action works identically after a
    prior OFF, not just from a fresh/never-toggled state."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{camera_id}/live")
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    state = _pill_state(page, analytic_key)
    assert state == "entitled", (
        f"Camera {camera_number} ({camera_id})'s {analytic_key} pill should be restored to entitled -- got {state}"
    )
