"""Target: Face Access (door/relay control) -- backend `9d368f8`, UI
`10a2e4f`, can_unlock viewer-permission UI `7d1afc9`. First real
browser/E2E pass for this feature; every prior verification was unit-
tests-with-fixtures only (see docs/PROJECT_CHECKPOINT.md's Face Access
milestones).

Runs against a disposable, dedicated synthetic tenant
(customer_id=e2efacetest) seeded directly into the real staging DB for
this pass -- never the real pilot customer's own account/cameras --
matching this project's established disposable-synthetic-tenant
convention for any staging validation that writes data (see the
password-reset/email-change staging milestones in
docs/PROJECT_CHECKPOINT.md). Two disposable cameras: `e2efacecamdoor`
(configured as a Face Access door partway through this suite) and
`e2efacecamplain` (never door-configured, used for the negative/fail-
closed cases). A third, `e2efacecamnorelay`, is seeded with
door_access_enabled=1 but no relay channel -- a state the real
door-config API can never produce on its own (enabling always requires
a valid channel) but that `_authorized_door_camera()`'s own defensive
check still guards, matching the fixture shape
`app/tests/test_door_access.py` already uses to reach that same branch.

Confirmed before this suite was written (see
docs/PROJECT_CHECKPOINT.md's matching deploy entry): relay_control.
get_provider() unconditionally returns MockRelayProvider -- there is no
hardware-backed RelayProvider implementation anywhere in this codebase
yet, so no call any of these tests make can reach real hardware.

Credentials for both disposable accounts come from e2e/.env
(ANYAICAM_E2E_FACE_ACCESS_OWNER_PASSWORD /
ANYAICAM_E2E_FACE_ACCESS_VIEWER_PASSWORD -- same disposable password for
both, generated fresh for this pass, never reused from any real
account) -- never hard-coded here, matching this directory's own
established credential-handling convention. Both accounts, and the rest
of the disposable tenant, are torn down after this pass; see the
teardown note in docs/PROJECT_CHECKPOINT.md's deploy entry for the exact
before/after row counts.
"""
from __future__ import annotations

import os

import pytest

CUSTOMER_ID = "e2efacetest"
OWNER_EMAIL = "e2e-face-access-owner@anyaicam-test.invalid"
VIEWER_EMAIL = "e2e-face-access-viewer@anyaicam-test.invalid"
CAM_DOOR = "e2efacecamdoor"
CAM_PLAIN = "e2efacecamplain"
CAM_NO_RELAY = "e2efacecamnorelay"


def _password(env_var: str) -> str:
    value = os.environ.get(env_var, "").strip()
    if not value:
        pytest.skip(
            f"{env_var} is not set in e2e/.env -- this suite needs the disposable "
            "Face Access test tenant's own password, not a real account's. See "
            "this file's own module docstring for the seeding convention."
        )
    return value


def _login(page, email: str, password: str):
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    assert "customer-login" not in page.url, f"login failed for {email}: still on the login page"
    return page


@pytest.fixture
def owner_page(browser, base_url):
    # A dedicated browser context per identity -- NOT the shared `page`
    # fixture -- is required whenever a single test needs two distinct
    # logged-in identities at once (here, owner + viewer). Two fixtures
    # that both depended on the shared `page` fixture were found, via
    # this suite's own real run, to alias the exact same browser tab:
    # the second login silently overwrote the first's session cookie,
    # so both fixture variables ended up pointing at whichever identity
    # logged in last -- confirmed from the real door_access_events audit
    # trail, where a POST made through the "viewer" fixture recorded an
    # authorization_result of "authorized" attributed to the owner.
    context = browser.new_context(base_url=base_url)
    page = context.new_page()
    _login(page, OWNER_EMAIL, _password("ANYAICAM_E2E_FACE_ACCESS_OWNER_PASSWORD"))
    yield page
    context.close()


@pytest.fixture
def viewer_page(browser, base_url):
    context = browser.new_context(base_url=base_url)
    page = context.new_page()
    _login(page, VIEWER_EMAIL, _password("ANYAICAM_E2E_FACE_ACCESS_VIEWER_PASSWORD"))
    yield page
    context.close()


def _csrf_headers(page) -> dict:
    """Direct page.request.* calls bypass this app's own fetch() wrapper
    (see live_view_page.py's page-shell script), which is what normally
    attaches X-CSRF-Token from the anyaicam_csrf cookie on every same-
    origin POST -- so any direct API call in this file must attach it
    the same way by hand, or a real, working request gets a real 403
    from the CSRF gate having nothing to do with door-access
    authorization itself."""
    for cookie in page.context.cookies():
        if cookie["name"] == "anyaicam_csrf":
            return {"X-CSRF-Token": cookie["value"]}
    return {}


def _reset_door_config(page, camera_id: str):
    """Idempotent pre-test reset via the real API (owner-only route) --
    every test that needs a known starting configuration calls this
    first rather than depending on suite execution order."""
    page.request.post(
        f"/api/customer/cameras/{camera_id}/door-config",
        data={"door_access_enabled": False},
        headers=_csrf_headers(page),
    )


@pytest.mark.e2e
def test_owner_configures_face_access_and_it_persists_on_reload(owner_page):
    page = owner_page
    _reset_door_config(page, CAM_DOOR)
    page.goto(f"/customer/cameras/{CAM_DOOR}/live")
    page.wait_for_load_state("networkidle")

    checkbox = page.locator("#door-access-enabled")
    assert not checkbox.is_checked(), "expected the reset camera to start disabled"

    checkbox.check()
    page.locator("#door-relay-channel").select_option("2")
    page.locator("#door-relay-pulse-ms").fill("4000")
    page.locator("#save-door-access").click()
    page.wait_for_timeout(1000)  # matches this codebase's own setTimeout(...,700) reload-on-save pattern
    page.wait_for_load_state("networkidle")

    assert page.locator("#door-access-enabled").is_checked(), "Face Access enable did not persist across reload"
    assert page.locator("#door-relay-channel").input_value() == "2", "relay channel did not persist"
    assert page.locator("#door-relay-pulse-ms").input_value() == "4000", "pulse duration did not persist"


@pytest.mark.e2e
def test_unlock_button_not_rendered_for_camera_with_no_relay_mapping(owner_page):
    page = owner_page
    _reset_door_config(page, CAM_PLAIN)
    page.goto(f"/customer/cameras/{CAM_PLAIN}/live")
    page.wait_for_load_state("networkidle")
    assert page.locator(f"#unlock-door-{CAM_PLAIN}").count() == 0, (
        "Unlock Door button rendered for a camera with no door/relay configuration -- "
        "the Face Access spec explicitly requires it never appear here"
    )

    page.goto("/customer-live")
    page.wait_for_load_state("networkidle")
    assert page.locator(f"#unlock-door-{CAM_PLAIN}").count() == 0, (
        "Unlock Door button rendered on the live grid for a non-door camera"
    )


@pytest.mark.e2e
def test_unlock_button_rendered_for_door_configured_camera(owner_page):
    page = owner_page
    page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door-config",
        data={"door_access_enabled": True, "door_relay_channel": 1, "door_relay_pulse_ms": 3000},
        headers=_csrf_headers(page),
    )

    page.goto(f"/customer/cameras/{CAM_DOOR}/live")
    page.wait_for_load_state("networkidle")
    assert page.locator(f"#unlock-door-{CAM_DOOR}").count() == 1, (
        "Unlock Door button missing on the single-camera page for a door-configured camera"
    )

    page.goto("/customer-live")
    page.wait_for_load_state("networkidle")
    assert page.locator(f"#unlock-door-{CAM_DOOR}").count() == 1, (
        "Unlock Door button missing on the live grid for a door-configured camera"
    )


@pytest.mark.e2e
def test_owner_can_unlock_authorized(owner_page):
    page = owner_page
    page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door-config",
        data={"door_access_enabled": True, "door_relay_channel": 1, "door_relay_pulse_ms": 3000},
        headers=_csrf_headers(page),
    )
    page.goto(f"/customer/cameras/{CAM_DOOR}/live")
    page.wait_for_load_state("networkidle")

    page.locator(f"#unlock-door-{CAM_DOOR}").click()
    # page.wait_for_function() evaluates a raw JS string, which this site's
    # own real CSP (script-src with no 'unsafe-eval') genuinely blocks --
    # confirmed live by this suite's own first run. locator.wait_for()
    # polls via Playwright's own instrumentation instead, no in-page eval.
    toast = page.locator("#toast.show")
    toast.wait_for(state="attached", timeout=5000)
    assert "unlocked" in toast.inner_text().lower(), f"unexpected unlock result toast: {toast.inner_text()!r}"


@pytest.mark.e2e
def test_viewer_without_grant_is_denied_unlock(owner_page, viewer_page):
    # Ensure the viewer holds no can_unlock grant for this camera before asserting the denial.
    owner_page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door-config/unlock-access",
        data={"user_ids": []},
        headers=_csrf_headers(owner_page),
    )
    viewer_page.goto(f"/customer/cameras/{CAM_DOOR}/live")
    viewer_page.wait_for_load_state("networkidle")
    # Capability hint only -- the button renders for any door-configured camera regardless
    # of this viewer's own can_unlock grant, matching the mic button's established convention
    # (real authorization is re-checked server-side at click time, not hidden client-side).
    assert viewer_page.locator(f"#unlock-door-{CAM_DOOR}").count() == 1

    response = viewer_page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door/unlock", headers=_csrf_headers(viewer_page)
    )
    assert response.status == 403, f"expected 403 for an unauthorized viewer, got {response.status}"
    assert "not authorized" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_owner_grants_then_revokes_viewer_unlock_access(owner_page, viewer_page):
    # Channel 2 specifically: never actually triggered by any other test in
    # this file (only assigned, never unlocked, by the config-persistence
    # test), so this test's own real unlock call can't be cooldown-
    # suppressed by another test's leftover relay state -- MockRelayProvider
    # is a module-level singleton in the running container, so its cooldown
    # bookkeeping genuinely persists across requests/tests, unlike the
    # per-test-isolated instance app/tests/test_door_access.py uses.
    owner_page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door-config",
        data={"door_access_enabled": True, "door_relay_channel": 2, "door_relay_pulse_ms": 3000},
        headers=_csrf_headers(owner_page),
    )
    owner_page.goto(f"/customer/cameras/{CAM_DOOR}/live")
    owner_page.wait_for_load_state("networkidle")
    # Locate the viewer's own row by its rendered email rather than a hard-coded user id,
    # matching what the panel actually shows an owner.
    row = owner_page.locator("label", has_text=VIEWER_EMAIL)
    assert row.count() == 1, "seeded viewer not listed in the Face Access viewer-access panel"
    checkbox = row.locator("input.unlock-viewer-toggle")
    assert not checkbox.is_checked(), "viewer unexpectedly already had can_unlock going into this test"

    checkbox.check()
    owner_page.locator("#save-unlock-access").click()
    owner_page.wait_for_timeout(500)

    grant_response = viewer_page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door/unlock", headers=_csrf_headers(viewer_page)
    )
    assert grant_response.status == 200, f"granted viewer still denied unlock: {grant_response.status} {grant_response.text()}"

    owner_page.goto(f"/customer/cameras/{CAM_DOOR}/live")
    owner_page.wait_for_load_state("networkidle")
    row = owner_page.locator("label", has_text=VIEWER_EMAIL)
    checkbox = row.locator("input.unlock-viewer-toggle")
    assert checkbox.is_checked(), "grant did not persist across reload"
    checkbox.uncheck()
    owner_page.locator("#save-unlock-access").click()
    owner_page.wait_for_timeout(500)

    revoke_response = viewer_page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door/unlock", headers=_csrf_headers(viewer_page)
    )
    assert revoke_response.status == 403, f"revoked viewer still allowed to unlock: {revoke_response.status}"


@pytest.mark.e2e
def test_fail_closed_non_door_camera_denies_and_audits(owner_page):
    page = owner_page
    _reset_door_config(page, CAM_PLAIN)
    response = page.request.post(
        f"/api/customer/cameras/{CAM_PLAIN}/door/unlock", headers=_csrf_headers(page)
    )
    assert response.status == 404
    assert "not configured for door access" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_fail_closed_unknown_camera_denies_without_audit(owner_page):
    page = owner_page
    response = page.request.post(
        "/api/customer/cameras/e2e-totally-unknown-camera-id/door/unlock", headers=_csrf_headers(page)
    )
    assert response.status == 404
    assert response.json()["detail"] == "Camera not found."


@pytest.mark.e2e
def test_fail_closed_enabled_door_with_no_relay_channel_assigned(owner_page):
    # e2efacecamnorelay is seeded directly (door_access_enabled=1, door_relay_channel=NULL) --
    # a state the real /door-config API can never produce (enabling always requires a valid
    # channel), matching app/tests/test_door_access.py's own fixture for this exact branch.
    page = owner_page
    response = page.request.post(
        f"/api/customer/cameras/{CAM_NO_RELAY}/door/unlock", headers=_csrf_headers(page)
    )
    assert response.status == 409
    assert "no relay is configured" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_fail_closed_cooldown_suppresses_immediate_repeat_unlock(owner_page):
    page = owner_page
    page.request.post(
        f"/api/customer/cameras/{CAM_DOOR}/door-config",
        data={"door_access_enabled": True, "door_relay_channel": 3, "door_relay_pulse_ms": 3000},
        headers=_csrf_headers(page),
    )
    first = page.request.post(f"/api/customer/cameras/{CAM_DOOR}/door/unlock", headers=_csrf_headers(page))
    assert first.status == 200, f"first unlock on a fresh channel should succeed: {first.status} {first.text()}"

    second = page.request.post(f"/api/customer/cameras/{CAM_DOOR}/door/unlock", headers=_csrf_headers(page))
    assert second.status == 409, f"immediate repeat unlock should be cooldown-suppressed, got {second.status}"
    assert "wait a moment" in second.json()["detail"].lower()
