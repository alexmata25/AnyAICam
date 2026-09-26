"""Focused Live View's switchable analytics row + upgrade cards for
non-licensed cameras.

app/customer_analytics_panel.py has no FastAPI/DB dependency -- fully
behavioral. app/live_view_page.py does depend on FastAPI (same constraint
as the rest of this suite -- see test_provisioning_service.py's module
docstring), so its route/JS wiring is verified by reading its source.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from customer_analytics_panel import (  # noqa: E402
    ANALYTIC_KEYS,
    ANALYTIC_LABELS,
    UPGRADE_CARD_CONTENT,
    analytics_row_state,
    enabled_analytics,
    event_types_for_analytic,
    summarize,
)

LIVE_VIEW_SOURCE = (ROOT / "app" / "live_view_page.py").read_text(encoding="utf-8")


class EnabledAnalyticsTests(unittest.TestCase):
    def test_no_subscriptions_means_nothing_enabled(self):
        self.assertEqual(enabled_analytics([]), [])

    def test_only_active_non_cancelled_subscriptions_count(self):
        rows = [
            {"analytic_key": "lpr", "status": "active"},
            {"analytic_key": "ppe", "status": "cancelled"},
        ]
        self.assertEqual(enabled_analytics(rows), ["lpr"])

    def test_pending_status_still_counts_as_enabled(self):
        # onboard_customer() inserts analytics_subscriptions with
        # status='pending' at purchase time -- a camera should show real
        # analytics as soon as it's purchased, not only after some later
        # activation step flips the status.
        rows = [{"analytic_key": "smart_motion", "status": "pending"}]
        self.assertEqual(enabled_analytics(rows), ["smart_motion"])

    def test_unknown_analytic_keys_are_ignored_not_crashed_on(self):
        rows = [{"analytic_key": "some_future_analytic", "status": "active"}]
        self.assertEqual(enabled_analytics(rows), [])

    def test_order_is_stable_regardless_of_subscription_order(self):
        rows = [
            {"analytic_key": "ppe", "status": "active"},
            {"analytic_key": "smart_motion", "status": "active"},
            {"analytic_key": "lpr", "status": "active"},
        ]
        self.assertEqual(enabled_analytics(rows), ["smart_motion", "lpr", "ppe"])


class AnalyticsRowStateTests(unittest.TestCase):
    """The row must never be empty: every camera shows all four
    analytics, flagged enabled or not."""

    def test_no_subscriptions_still_returns_all_four_flagged_disabled(self):
        state = analytics_row_state([])
        self.assertEqual(len(state), len(ANALYTIC_KEYS))
        self.assertTrue(all(item["enabled"] is False for item in state))
        self.assertEqual({item["key"] for item in state}, set(ANALYTIC_KEYS))

    def test_mixed_subscriptions_flag_only_the_purchased_ones(self):
        rows = [{"analytic_key": "lpr", "status": "active"}]
        state = analytics_row_state(rows)
        by_key = {item["key"]: item["enabled"] for item in state}
        self.assertTrue(by_key["lpr"])
        self.assertFalse(by_key["ppe"])
        self.assertFalse(by_key["people_counting"])
        self.assertFalse(by_key["smart_motion"])

    def test_all_four_analytics_purchased_flags_all_enabled(self):
        rows = [{"analytic_key": key, "status": "active"} for key in ANALYTIC_KEYS]
        state = analytics_row_state(rows)
        self.assertTrue(all(item["enabled"] for item in state))


class UpgradeCardContentTests(unittest.TestCase):
    def test_every_analytic_has_upgrade_card_content(self):
        self.assertEqual(set(UPGRADE_CARD_CONTENT.keys()), set(ANALYTIC_KEYS))

    def test_every_upgrade_card_has_a_description_and_at_least_one_benefit(self):
        for key, content in UPGRADE_CARD_CONTENT.items():
            self.assertTrue(content["description"], key)
            self.assertGreaterEqual(len(content["benefits"]), 1, key)


class EventTypesForAnalyticTests(unittest.TestCase):
    def test_smart_motion_covers_motion_person_and_vehicle(self):
        self.assertEqual(event_types_for_analytic("smart_motion"), ("motion", "person", "vehicle"))

    def test_lpr_and_people_counting_and_ppe_are_their_own_single_type(self):
        self.assertEqual(event_types_for_analytic("lpr"), ("lpr",))
        self.assertEqual(event_types_for_analytic("people_counting"), ("people_counting",))
        self.assertEqual(event_types_for_analytic("ppe"), ("ppe",))

    def test_unknown_analytic_raises(self):
        with self.assertRaises(ValueError):
            event_types_for_analytic("not_a_real_analytic")


class SummarizeTests(unittest.TestCase):
    def test_lpr_summary_reads_the_latest_plate_and_confidence(self):
        events = [
            {"event_type": "lpr", "confidence": 91, "event_timestamp": "2026-01-01T12:00:05",
             "detections_json": '{"plate": "ABC123"}'},
            {"event_type": "lpr", "confidence": 80, "event_timestamp": "2026-01-01T11:00:00",
             "detections_json": '{"plate": "XYZ999"}'},
        ]
        summary = summarize("lpr", events)
        self.assertEqual(summary["latest_plate"], "ABC123")
        self.assertEqual(summary["latest_confidence"], 91)
        self.assertEqual(len(summary["recent"]), 2)

    def test_lpr_summary_with_no_events_is_empty_not_an_error(self):
        summary = summarize("lpr", [])
        self.assertIsNone(summary["latest_plate"])
        self.assertEqual(summary["recent"], [])

    def test_people_counting_summary_reads_the_latest_count(self):
        events = [{"event_type": "people_counting", "object_count": 3,
                   "event_timestamp": "2026-01-01T12:00:00", "detections_json": '{"entries": 5, "exits": 2}'}]
        summary = summarize("people_counting", events)
        self.assertEqual(summary["latest_count"], 3)
        self.assertEqual(summary["entries"], 5)
        self.assertEqual(summary["exits"], 2)

    def test_ppe_summary_reads_the_latest_status(self):
        events = [{"event_type": "ppe", "event_timestamp": "2026-01-01T12:00:00",
                   "detections_json": '{"status": "compliant"}'}]
        summary = summarize("ppe", events)
        self.assertEqual(summary["latest_status"], "compliant")

    def test_smart_motion_summary_includes_recent_mixed_event_types(self):
        events = [
            {"event_type": "person", "event_timestamp": "2026-01-01T12:00:00", "confidence": 90, "detections_json": "{}"},
            {"event_type": "vehicle", "event_timestamp": "2026-01-01T11:00:00", "confidence": 85, "detections_json": "{}"},
        ]
        summary = summarize("smart_motion", events)
        self.assertEqual(len(summary["recent"]), 2)
        self.assertEqual(summary["recent"][0]["event_type"], "person")

    def test_summarize_never_crashes_on_malformed_detections_json(self):
        events = [{"event_type": "lpr", "event_timestamp": "2026-01-01T12:00:00", "detections_json": "not json"}]
        summary = summarize("lpr", events)
        self.assertIsNone(summary["latest_plate"])

    def test_unknown_analytic_raises(self):
        with self.assertRaises(ValueError):
            summarize("not_a_real_analytic", [])


class FocusedLiveViewWiringTests(unittest.TestCase):
    """app/live_view_page.py depends on FastAPI (see module docstring)."""

    def test_analytics_section_is_never_hardcoded_hidden_forever(self):
        self.assertIn('id="live-analytics-section" hidden', LIVE_VIEW_SOURCE)
        # It starts hidden in the markup, but the JS always reveals it
        # once analytics data loads (never an empty row -- see
        # analytics_row_state()).
        self.assertIn("analyticsSection.hidden=false;", LIVE_VIEW_SOURCE)

    def test_only_one_analytics_panel_is_shown_at_a_time(self):
        self.assertIn("activeAnalytic=key;", LIVE_VIEW_SOURCE)
        self.assertIn("pill.classList.toggle('active',isActive)", LIVE_VIEW_SOURCE)

    def test_disabled_analytics_render_an_upgrade_card_not_a_blank_panel(self):
        self.assertIn("if(!analyticsByKey[key].enabled){{renderUpgradeCard(key);return}}", LIVE_VIEW_SOURCE)
        self.assertIn("function renderUpgradeCard(key){{", LIVE_VIEW_SOURCE)
        for cta in ("Request Upgrade", "Add to This Camera", "Learn More"):
            self.assertIn(cta, LIVE_VIEW_SOURCE)

    def test_video_element_is_not_touched_by_analytics_switching(self):
        select_start = LIVE_VIEW_SOURCE.index("async function selectAnalytic(key){{")
        select_end = LIVE_VIEW_SOURCE.index("\n  }}\n", select_start)
        body = LIVE_VIEW_SOURCE[select_start:select_end]
        self.assertNotIn("video.", body)
        self.assertNotIn("hls.", body)

    def test_double_click_and_double_tap_open_focused_view_from_the_grid(self):
        # Updated for the Camera Hub grid redesign (tight/flush tiles,
        # controls overlaid and hidden until hover/tap instead of an
        # always-visible row below the video): double-click/double-tap
        # still opens the focused view, now via an inline arrow function
        # (matching window.location.href= -- see test_customer_
        # analytics_integration.py's own test_grid_double_click_
        # navigates_instead_of_calling_fullscreen_locally) rather than
        # the old named openFocused() -- the touchend handler also now
        # takes an `event` parameter so a tap that lands on one of the
        # overlay's own camera-tool buttons doesn't also trigger the
        # tile's fade-in-controls toggle.
        self.assertIn("window.location.href=`/customer/cameras/${{tile.dataset.cameraId}}/live`;", LIVE_VIEW_SOURCE)
        self.assertIn("tile.addEventListener('touchend',event=>{{", LIVE_VIEW_SOURCE)
        self.assertIn("if(now-lastTap<350){{", LIVE_VIEW_SOURCE)

    def test_analytics_routes_are_permission_scoped_to_customer_roles(self):
        for fn_name in ("customer_camera_analytics_enabled", "customer_camera_analytics_summary"):
            start = LIVE_VIEW_SOURCE.index(f"def {fn_name}")
            body = LIVE_VIEW_SOURCE[start:start + 900]
            self.assertIn("{'customer_owner', 'customer_viewer'}", body)

    def test_analytics_summary_route_never_returns_more_than_the_camera_scoped_query(self):
        start = LIVE_VIEW_SOURCE.index("def customer_camera_analytics_summary")
        body = LIVE_VIEW_SOURCE[start:start + 1600]
        self.assertIn("WHERE camera_id=? AND customer_id=?", body)


if __name__ == "__main__":
    unittest.main()
