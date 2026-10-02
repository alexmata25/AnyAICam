"""RDM (per-camera analytics entitlement) ON/OFF verification, against a
wholly synthetic test customer -- never the real pilot customer's own
account. See e2e/scripts/provision_rdm_test_harness.py for how that
customer/site/camera/subscriptions were created, and
e2e/scripts/toggle_rdm_entitlement.py for how entitlements are actually
flipped (always through the real product functions,
assign_entitlement()/remove_entitlement() in
customer_analytics_panel.py -- never raw SQL against
camera_analytics_entitlements).

This synthetic camera has no real Ryzen appliance behind it
(appliance_id is NULL by design -- see the provisioning script's own
docstring for why attaching it to the real appliance would risk
destabilizing real camera discovery). That means these tests verify the
real, product-real CLOUD/UI half of the entitlement path -- the exact
thing a customer/partner actually sees -- not appliance-side runtime
enforcement. A full source search already established, with certainty,
that no appliance-side code reads camera_analytics_entitlements at all
(see PROJECT_CHECKPOINT.md's 2026-09-16 RDM entry); that conclusion
does not change based on any live camera's footage, so it is not
re-tested here.

Credentials are for a synthetic, no-real-data test account (zero real
customer video, zero real detections) -- lower stakes than the primary
e2e_credentials fixture, but still real, working login credentials, so
still not printed in any test's own assertion-failure output.
"""
import json
import os
import subprocess
import time

import pytest

STAGING_INSTANCE_ID = "i-0a082abd812929bb4"


def _toggle(action: str, analytic_key: str) -> None:
    """Flips one entitlement on the RDM test harness camera through the
    real product functions (see e2e/scripts/toggle_rdm_entitlement.py),
    synchronously, by dispatching it on the staging EC2 host the same
    way every other real-data check in this project's e2e suite already
    does (SSM RunShellScript against portal-green). Raises if the
    command doesn't finish successfully -- a toggle that silently
    no-ops must fail the test, not be mistaken for "nothing changed."""
    send = subprocess.run(
        [
            "aws", "ssm", "send-command",
            "--instance-ids", STAGING_INSTANCE_ID,
            "--document-name", "AWS-RunShellScript",
            "--parameters", json.dumps({"commands": [f"docker exec portal-green python3 /tmp/toggle_rdm.py {action} {analytic_key}"]}),
            "--output", "text", "--query", "Command.CommandId",
        ],
        capture_output=True, text=True, check=True,
    )
    command_id = send.stdout.strip()
    for _ in range(20):
        time.sleep(1)
        status = subprocess.run(
            ["aws", "ssm", "get-command-invocation", "--command-id", command_id,
             "--instance-id", STAGING_INSTANCE_ID, "--query", "Status", "--output", "text"],
            capture_output=True, text=True,
        ).stdout.strip()
        if status == "Success":
            return
        if status in {"Failed", "Cancelled", "TimedOut"}:
            raise RuntimeError(f"toggle {action} {analytic_key} failed: SSM status={status}")
    raise TimeoutError(f"toggle {action} {analytic_key} did not complete in time")

RDM_TEST_EMAIL = "rdm-test-harness@staging.anyaicam.internal"
RDM_TEST_PASSWORD = os.environ.get("ANYAICAM_RDM_TEST_HARNESS_PASSWORD", "")
RDM_TEST_CAMERA_ID = "rdm-test-harness-camera-1"

ANALYTIC_KEYS = ("smart_motion", "people_counting", "lpr", "ppe")

pytestmark = pytest.mark.skipif(
    not RDM_TEST_PASSWORD,
    reason="ANYAICAM_RDM_TEST_HARNESS_PASSWORD not set -- see e2e/scripts/provision_rdm_test_harness.py's own output",
)


@pytest.fixture
def rdm_harness_page(page):
    page.goto("/customer-login.html")
    page.fill("#email", RDM_TEST_EMAIL)
    page.fill("#password", RDM_TEST_PASSWORD)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    page.goto(f"/customer/cameras/{RDM_TEST_CAMERA_ID}/live")
    page.wait_for_selector('button.filter[data-key="smart_motion"]')
    return page


def _pill_state(page, analytic_key: str) -> str:
    """Returns 'entitled' or 'not_entitled' for one analytic pill, by
    clicking it and reading the real rendered panel -- the exact same
    check test_lpr.py/test_ppe.py/test_analytics_smart_motion_and_
    people_counting.py already use against the real pilot customer."""
    page.locator(f'button.filter[data-key="{analytic_key}"]').click()
    page.wait_for_timeout(400)
    panel_text = page.locator("#live-analytics-panel").text_content() or ""
    return "not_entitled" if "Not enabled on this camera" in panel_text else "entitled"


@pytest.mark.e2e
@pytest.mark.parametrize("analytic_key", ANALYTIC_KEYS)
def test_baseline_camera_starts_with_no_entitlements(rdm_harness_page, analytic_key):
    """Confirms the synthetic camera's real starting state (nothing
    granted yet by the provisioning script) before any RDM toggle test
    runs -- if this fails, a previous run's toggle wasn't cleaned up."""
    assert _pill_state(rdm_harness_page, analytic_key) == "not_entitled"


@pytest.mark.e2e
@pytest.mark.parametrize("analytic_key", ANALYTIC_KEYS)
def test_individual_entitlement_on_then_off_is_correctly_isolated(page, analytic_key):
    """The real RDM ON/OFF path, one capability at a time: turning ONE
    analytic on must make ONLY that one show real/entitled state, never
    the others (isolation), and turning it back off must fully restore
    the not-entitled baseline (no stuck state)."""
    other_keys = [key for key in ANALYTIC_KEYS if key != analytic_key]
    _toggle("on", analytic_key)
    try:
        login_page = page
        login_page.goto("/customer-login.html")
        login_page.fill("#email", RDM_TEST_EMAIL)
        login_page.fill("#password", RDM_TEST_PASSWORD)
        login_page.click("form#login button.submit")
        login_page.wait_for_load_state("networkidle")
        login_page.goto(f"/customer/cameras/{RDM_TEST_CAMERA_ID}/live")
        login_page.wait_for_selector('button.filter[data-key="smart_motion"]')

        assert _pill_state(login_page, analytic_key) == "entitled", \
            f"turning {analytic_key} ON must make it show the entitled/real-data state"
        for other_key in other_keys:
            assert _pill_state(login_page, other_key) == "not_entitled", \
                f"turning {analytic_key} ON must not also entitle {other_key} (isolation)"
    finally:
        _toggle("off", analytic_key)

    verify_page = page
    verify_page.goto(f"/customer/cameras/{RDM_TEST_CAMERA_ID}/live")
    verify_page.wait_for_selector('button.filter[data-key="smart_motion"]')
    assert _pill_state(verify_page, analytic_key) == "not_entitled", \
        f"turning {analytic_key} back OFF must fully restore the not-entitled baseline"


@pytest.mark.e2e
def test_multiple_entitlements_on_in_combination_then_restored(page):
    """Two analytics on at once (smart_motion + lpr): both must show
    entitled, the other two must not -- proving combinations aren't
    special-cased or cross-wired -- then both are turned back off and
    the full not-entitled baseline is verified restored."""
    on_keys = ("smart_motion", "lpr")
    off_keys = tuple(key for key in ANALYTIC_KEYS if key not in on_keys)
    for key in on_keys:
        _toggle("on", key)
    try:
        login_page = page
        login_page.goto("/customer-login.html")
        login_page.fill("#email", RDM_TEST_EMAIL)
        login_page.fill("#password", RDM_TEST_PASSWORD)
        login_page.click("form#login button.submit")
        login_page.wait_for_load_state("networkidle")
        login_page.goto(f"/customer/cameras/{RDM_TEST_CAMERA_ID}/live")
        login_page.wait_for_selector('button.filter[data-key="smart_motion"]')

        for key in on_keys:
            assert _pill_state(login_page, key) == "entitled", f"{key} should be entitled in this combination"
        for key in off_keys:
            assert _pill_state(login_page, key) == "not_entitled", f"{key} should stay not-entitled in this combination"
    finally:
        for key in on_keys:
            _toggle("off", key)

    verify_page = page
    verify_page.goto(f"/customer/cameras/{RDM_TEST_CAMERA_ID}/live")
    verify_page.wait_for_selector('button.filter[data-key="smart_motion"]')
    for key in ANALYTIC_KEYS:
        assert _pill_state(verify_page, key) == "not_entitled", f"{key} must be back to not-entitled after cleanup"


@pytest.mark.e2e
def test_all_four_entitlements_on_simultaneously_then_fully_restored(page):
    """All 4 capabilities on at once -- the "fully enabled" shape this
    whole engagement's known-good baseline is framed around -- then all
    4 restored to the original not-entitled baseline."""
    for key in ANALYTIC_KEYS:
        _toggle("on", key)
    try:
        login_page = page
        login_page.goto("/customer-login.html")
        login_page.fill("#email", RDM_TEST_EMAIL)
        login_page.fill("#password", RDM_TEST_PASSWORD)
        login_page.click("form#login button.submit")
        login_page.wait_for_load_state("networkidle")
        login_page.goto(f"/customer/cameras/{RDM_TEST_CAMERA_ID}/live")
        login_page.wait_for_selector('button.filter[data-key="smart_motion"]')

        for key in ANALYTIC_KEYS:
            assert _pill_state(login_page, key) == "entitled", f"{key} should be entitled with all 4 on"
    finally:
        for key in ANALYTIC_KEYS:
            _toggle("off", key)

    verify_page = page
    verify_page.goto(f"/customer/cameras/{RDM_TEST_CAMERA_ID}/live")
    verify_page.wait_for_selector('button.filter[data-key="smart_motion"]')
    for key in ANALYTIC_KEYS:
        assert _pill_state(verify_page, key) == "not_entitled", f"{key} must be restored to not-entitled after full cleanup"
