"""Focused tests for the Smart Alerts type-aware active-only fix
(fix/smart-alerts-active-only).

Root cause (established investigation): health_monitor() writes a new,
permanent row to the append-only IN_APP_ALERTS_FILE every ~5 minutes while
a system-health condition (stream_offline/recording_stopped/
reconnect_failures/high_cpu/low_disk_space) persists. Nothing ever marks
those rows read or removes them once the condition recovers, so
dashboard_intelligence_api()'s old unread_alerts = [... if not read ...]
kept resurfacing them forever, even though health_issues (self-healing,
rebuilt every health_monitor() cycle) already knew the condition had
cleared. IN_APP_ALERTS_FILE also carries legitimate motion/person/vehicle
alerts (see the other append_in_app_alert call sites in store_motion_event
and the object-detection loop) via the exact same "unread" mechanism, so
the fix must exclude only the known health-issue event_type values, not
all unread alerts.

As with the other main.py-embedded logic in this repo, app/main.py can't
be imported directly here. HealthAlertFilterBehaviorTests extracts the
exact HEALTH_ISSUE_ALERT_TYPES constant and unread_alerts filter
expression verbatim and executes them for genuine behavioral proof; the
rest are source-inspection tests, the right tool for confirming wiring
and confirming something (persistence, detection generation) was left
alone rather than for proving new computed behavior.
"""

import unittest
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"

CONSTANT_START = "HEALTH_ISSUE_ALERT_TYPES = {"
CONSTANT_END_MARKER = "\n}"
FILTER_START = "    unread_alerts = [\n"
FILTER_END_MARKER = "    ]\n"


def _extract_constant(source: str) -> str:
    start = source.index(CONSTANT_START)
    end = source.index(CONSTANT_END_MARKER, start) + len(CONSTANT_END_MARKER)
    return source[start:end]


def _extract_filter(source: str) -> str:
    start = source.index(FILTER_START)
    end = source.index(FILTER_END_MARKER, start) + len(FILTER_END_MARKER)
    return source[start:end]


class HealthAlertFilterBehaviorTests(unittest.TestCase):
    """Executes the real, extracted constant + filter expression."""

    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")
        constant_src = _extract_constant(source)
        filter_src = _extract_filter(source)
        function_src = (
            "def compute_unread_alerts(alerts):\n"
            + filter_src
            + "    return unread_alerts\n"
        )
        namespace = {}
        exec(compile(constant_src, "app/main.py (HEALTH_ISSUE_ALERT_TYPES)", "exec"), namespace)
        exec(compile(function_src, "app/main.py (unread_alerts filter)", "exec"), namespace)
        cls.compute_unread_alerts = staticmethod(namespace["compute_unread_alerts"])
        cls.HEALTH_ISSUE_ALERT_TYPES = namespace["HEALTH_ISSUE_ALERT_TYPES"]

    def test_known_health_issue_types_match_health_monitor(self):
        self.assertEqual(
            self.HEALTH_ISSUE_ALERT_TYPES,
            {
                "stream_offline",
                "recording_stopped",
                "reconnect_failures",
                "high_cpu",
                "low_disk_space",
            },
        )

    def test_recovered_persisted_health_alert_is_excluded(self):
        alerts = [
            {"event_type": "stream_offline", "read": False, "message": "Camera 2 stream is offline."},
            {"event_type": "recording_stopped", "read": False, "message": "Camera 1 recording worker stopped."},
            {"event_type": "reconnect_failures", "read": False, "message": "Camera 3 reconnect failures."},
        ]
        self.assertEqual(self.compute_unread_alerts(alerts), [])

    def test_unread_motion_person_vehicle_alerts_still_appear(self):
        alerts = [
            {"event_type": "motion", "read": False, "message": "Motion detected on Camera 1."},
            {"event_type": "person", "read": False, "message": "Person detected on Camera 2."},
            {"event_type": "car", "read": False, "message": "Vehicle detected on Camera 3."},
        ]
        self.assertEqual(self.compute_unread_alerts(alerts), alerts)

    def test_mixed_alerts_only_drop_the_health_ones(self):
        motion_alert = {"event_type": "motion", "read": False, "message": "Motion detected."}
        health_alert = {"event_type": "high_cpu", "read": False, "message": "System CPU usage is high."}
        already_read = {"event_type": "motion", "read": True, "message": "Old, already seen."}
        result = self.compute_unread_alerts([motion_alert, health_alert, already_read])
        self.assertEqual(result, [motion_alert])


class ActiveHealthIssuesSourceTests(unittest.TestCase):
    """active_issues (sourced from the live, self-healing health_issues
    dict) must remain the current-problem source, unaffected by this fix."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_active_issues_still_sourced_from_health_issues(self):
        self.assertIn("active_issues = list(health_issues.values())", self.source)

    def test_active_issue_rows_marked_as_health_category(self):
        self.assertIn(
            '"href": f\'/camera/{issue.get("camera")}\' if issue.get("camera") else "/dashboard",\n'
            '            "category": "health",',
            self.source,
        )

    def test_unread_alert_rows_marked_as_event_category(self):
        self.assertIn(
            '"href": f\'/camera/{alert.get("camera")}\' if alert.get("camera") else "/alerts",\n'
            '            "category": "event",',
            self.source,
        )


class PersistedHistoryUntouchedSourceTests(unittest.TestCase):
    """Confirms IN_APP_ALERTS_FILE/in_app_alerts()/append_in_app_alert and
    motion/person/vehicle detection generation are all unchanged -- the
    fix only touches how dashboard_intelligence_api() derives its own
    local unread_alerts view, never the underlying persisted data."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_raw_history_read_is_unfiltered_and_unchanged(self):
        self.assertIn(
            'alerts = in_app_alerts(limit=100).get("alerts", [])',
            self.source,
        )

    def test_append_only_persistence_helpers_unchanged(self):
        self.assertIn('def append_in_app_alert(alert: dict) -> None:', self.source)
        self.assertIn('with IN_APP_ALERTS_FILE.open("a", encoding="utf-8") as alert_file:', self.source)

    def test_history_api_route_unchanged(self):
        self.assertIn('@app.get("/api/alerts")', self.source)

    def test_motion_event_generation_unchanged(self):
        self.assertIn("def store_motion_event(", self.source)
        self.assertIn("def append_motion_event(line: str) -> None:", self.source)
        self.assertIn(
            'with MOTION_EVENTS_FILE.open("a", encoding="utf-8") as event_file:',
            self.source,
        )


class CompactHealthAlertRenderingSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_compact_css_variant_exists(self):
        self.assertIn(
            ".alert-card.compact{grid-template-columns:auto minmax(0,1fr) auto;"
            "gap:8px;padding:7px 10px;align-items:center}",
            self.source,
        )
        self.assertIn(".alert-card.compact .alert-meta{display:none}", self.source)

    def test_javascript_applies_compact_class_to_health_rows(self):
        self.assertIn("${alert.category==='health'?'compact':''}", self.source)


if __name__ == "__main__":
    unittest.main()
