"""Closing pass on the customer-facing UI/product punch list. Static
source checks only (app/main.py and app/live_view_page.py both depend on
FastAPI -- same constraint as the rest of this suite; see
test_provisioning_service.py's module docstring).
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
LIVE_VIEW_SOURCE = (ROOT / "app" / "live_view_page.py").read_text(encoding="utf-8")


class ShowPasswordToggleInCanonicalSourceTests(unittest.TestCase):
    """Was previously applied only to the running Samsung file, outside
    git -- must live in canonical source so a future clean deploy doesn't
    silently drop it."""

    def test_toggle_markup_and_script_are_present_in_git_source(self):
        self.assertIn('id="login-password-toggle"', MAIN_SOURCE)
        self.assertIn("login-password-toggle", MAIN_SOURCE)
        self.assertIn("aria-pressed", MAIN_SOURCE)

    def test_toggle_never_submits_or_logs_the_password_value(self):
        toggle_start = MAIN_SOURCE.index("'login-password-toggle'")
        snippet = MAIN_SOURCE[toggle_start:toggle_start + 600]
        self.assertNotIn("fetch(", snippet)
        self.assertNotIn("console.log", snippet)


class MobilePlaybackTests(unittest.TestCase):
    """Desktop timeline/scrubber hidden on phones; a plain tappable
    recent-events list (clips + events, most recent first) shown instead;
    desktop keeps the full timeline."""

    def test_responsive_rule_hides_the_desktop_timeline_below_640px(self):
        self.assertIn("@media (max-width:640px){.monitor-timeline{display:none}", MAIN_SOURCE)

    def test_responsive_rule_hides_the_mobile_list_on_desktop(self):
        self.assertIn("@media (min-width:641px){.mobile-recent-events{display:none}}", MAIN_SOURCE)

    def test_mobile_list_is_populated_from_the_same_clips_and_events_data(self):
        start = MAIN_SOURCE.index("function renderMobileRecentEvents")
        body = MAIN_SOURCE[start:start + 1800]
        self.assertIn("mobile-recent-events-list", body)
        self.assertIn("clips]", body)  # [...clips].reverse()...
        self.assertIn("playClip(cameraId,clip)", body)

    def test_render_timeline_calls_the_mobile_renderer_so_both_stay_in_sync(self):
        start = MAIN_SOURCE.index("function renderTimeline(cameraId,clips,events){{")
        body = MAIN_SOURCE[start:start + 200]
        self.assertIn("renderMobileRecentEvents(cameraId,clips,events);", body)


class FalseMotionFilteringSafeDefaultTests(unittest.TestCase):
    """Item 5: only the safe, no-real-camera-tuning-required default is
    implemented here (rejecting near-total-frame changes, i.e. lighting/
    exposure shifts) -- see the punch-list report for what still needs
    real footage."""

    def test_motion_max_changed_ratio_constant_exists_with_a_conservative_default(self):
        self.assertIn('MOTION_MAX_CHANGED_RATIO = float(os.environ.get("MOTION_MAX_CHANGED_RATIO", "0.85"))', MAIN_SOURCE)

    def test_motion_detected_condition_uses_the_upper_bound(self):
        self.assertIn("0.08 <= changed_ratio <= MOTION_MAX_CHANGED_RATIO", MAIN_SOURCE)
        # the old unbounded check must be gone, not just supplemented
        self.assertNotIn("changed_ratio >= 0.08\n                    )", MAIN_SOURCE)


class OfflineStaleStateTests(unittest.TestCase):
    """Item 8 (this round): a not_configured/appliance_offline check runs
    BEFORE starting a live session, so the placeholder shows an honest
    reason instead of a generic 'Connecting...' (or, worse, nothing) for a
    camera that was never going to come up."""

    def test_status_route_exists_and_is_customer_role_scoped(self):
        start = LIVE_VIEW_SOURCE.index("def customer_camera_status")
        body = LIVE_VIEW_SOURCE[start:start + 1600]
        self.assertIn("{'customer_owner', 'customer_viewer'}", body)
        self.assertIn("'not_configured'", body)
        self.assertIn("'appliance_offline'", body)

    def test_not_configured_is_derived_from_camera_number_being_unset(self):
        start = LIVE_VIEW_SOURCE.index("def customer_camera_status")
        body = LIVE_VIEW_SOURCE[start:start + 1600]
        self.assertIn("camera.get('camera_number') is None", body)

    def test_status_is_checked_before_starting_a_live_session(self):
        self.assertIn("async function checkCameraStatusThenStart(){{", LIVE_VIEW_SOURCE)
        self.assertIn("checkCameraStatusThenStart();", LIVE_VIEW_SOURCE)
        start = LIVE_VIEW_SOURCE.index("async function checkCameraStatusThenStart(){{")
        body = LIVE_VIEW_SOURCE[start:start + 700]
        self.assertIn("return}}", body)  # short-circuits instead of always starting a session
        self.assertIn("startSession();", body)


class SettingsCategoriesAlreadyFixedTests(unittest.TestCase):
    """Regression guard only -- this was fixed by the earlier sidebar-nav
    work and merged into this branch's base; confirms it's still here."""

    def test_implemented_settings_categories_constant_present(self):
        self.assertIn('IMPLEMENTED_SETTINGS_CATEGORIES = {"Events & alerts"}', MAIN_SOURCE)


if __name__ == "__main__":
    unittest.main()
