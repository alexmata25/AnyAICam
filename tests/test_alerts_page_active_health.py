"""Focused tests for the /alerts page active-health-only fix
(fix/alerts-page-active-health-only).

Root cause (established investigation): the /alerts page's own handler,
def alerts() (app/main.py, route /alerts), is a separate code path from
dashboard_intelligence_api() -- the earlier Smart Alerts widget fix
(fix/smart-alerts-active-only, HEALTH_ISSUE_ALERT_TYPES) never touched it.
Its health_alerts list rendered every persisted, non-"motion" row from the
last 30 entries of IN_APP_ALERTS_FILE as its own permanent-looking card,
with no regard for "read" status or whether the underlying health_issues
condition had since recovered -- so old Stream Offline/Recording Stopped
rows kept filling the page even while the "{len(events)} event(s)" pill
(sourced independently from load_motion_events()) correctly showed 0.

The fix reuses HEALTH_ISSUE_ALERT_TYPES (added on the sibling
fix/smart-alerts-active-only branch this branch is built from) to drop
persisted rows of those types from health_alerts, and instead renders
"active" cards straight from the live, self-healing health_issues dict --
the same source dashboard_intelligence_api() already uses for its own
active_issues -- so a card only appears while the condition is ongoing.

As with the other main.py-embedded logic in this repo, app/main.py can't
be imported directly here. AlertsPageHealthCardBehaviorTests extracts every
fragment (HEALTH_ISSUE_ALERT_TYPES, the alert_cards initializer, the
health_alerts filter, the pre-existing per-alert render loop, and the new
active-issue render loop) verbatim from app/main.py and executes them
together for genuine behavioral proof, rather than a hand-copied
duplicate. UntouchedSubsystemsSourceTests confirms IN_APP_ALERTS_FILE,
in_app_alerts(), /api/alerts, append_in_app_alert(), health_monitor(), and
the motion/event card path were not touched by this fix.
"""

import unittest
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _extract(source: str, start_marker: str, end_marker: str, include_end: bool = True) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    if include_end:
        end += len(end_marker)
    return source[start:end]


class AlertsPageHealthCardBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")

        constant_src = _extract(source, "HEALTH_ISSUE_ALERT_TYPES = {", "\n}")

        alert_cards_init_src = _extract(source, "    alert_cards = []", "    alert_cards = []")

        health_alerts_filter_src = _extract(
            source,
            "    health_alerts = [\n",
            "    ]",
        )

        existing_loop_src = _extract(
            source,
            "    for alert in health_alerts:",
            'alert_cards.insert(0, f\'<article class="feature-card">'
            '<div class="feature-icon">!</div>'
            '<h2>{escape(alert.get("event_type", "health").replace("_", " ").title())}</h2>'
            '<p>{escape(alert.get("message", "System health alert"))}</p>'
            '<span class="coming">In-app alert</span></article>\')',
        )

        active_issue_loop_src = _extract(
            source,
            "    for issue in health_issues.values():",
            'alert_cards.insert(0, f\'<article class="feature-card">'
            '<div class="feature-icon">!</div>'
            '<h2>{escape(str(issue.get("type", "health")).replace("_", " ").title())}</h2>'
            '<p>{escape(issue.get("message", "System health alert"))}</p>'
            '<span class="coming">Active</span></article>\')',
        )

        function_src = (
            "from html import escape\n\n"
            "def compute_alerts_page_health_cards(in_app_alerts_result, health_issues):\n"
            + health_alerts_filter_src.replace(
                "in_app_alerts(30)", "in_app_alerts_result"
            )
            + "\n\n"
            + alert_cards_init_src
            + "\n\n"
            + existing_loop_src
            + "\n\n"
            + active_issue_loop_src
            + "\n    return alert_cards\n"
        )

        namespace = {}
        for snippet, label in (
            (constant_src, "HEALTH_ISSUE_ALERT_TYPES"),
            (function_src, "compute_alerts_page_health_cards"),
        ):
            exec(compile(snippet, f"app/main.py (extracted {label})", "exec"), namespace)

        cls.HEALTH_ISSUE_ALERT_TYPES = namespace["HEALTH_ISSUE_ALERT_TYPES"]
        cls.compute_alerts_page_health_cards = staticmethod(
            namespace["compute_alerts_page_health_cards"]
        )

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

    def test_recovered_persisted_health_alerts_do_not_render_as_cards(self):
        alerts = [
            {"event_type": "stream_offline", "read": False, "camera": 1, "message": "Camera 1 stream is offline."},
            {"event_type": "recording_stopped", "read": False, "camera": 2, "message": "Camera 2 recording worker stopped."},
            {"event_type": "recording_stopped", "read": True, "camera": 3, "message": "Camera 3 recording worker stopped."},
            {"event_type": "recording_stopped", "read": False, "camera": 4, "message": "Camera 4 recording worker stopped."},
        ]
        cards = self.compute_alerts_page_health_cards({"alerts": alerts}, {})
        self.assertEqual(cards, [])

    def test_currently_active_health_issue_renders_as_a_card(self):
        health_issues = {
            "camera-1-offline": {
                "type": "stream_offline",
                "camera": 1,
                "message": "Camera 1 stream is offline.",
                "severity": "critical",
            }
        }
        cards = self.compute_alerts_page_health_cards({"alerts": []}, health_issues)
        self.assertEqual(len(cards), 1)
        self.assertIn("Stream Offline", cards[0])
        self.assertIn("Camera 1 stream is offline.", cards[0])
        self.assertIn('<span class="coming">Active</span>', cards[0])

    def test_recovered_and_active_together_only_the_active_one_renders(self):
        alerts = [
            {"event_type": "stream_offline", "read": False, "camera": 1, "message": "Camera 1 stream is offline (old)."},
            {"event_type": "recording_stopped", "read": False, "camera": 2, "message": "Camera 2 recording worker stopped (old)."},
        ]
        health_issues = {
            "camera-2-recording": {
                "type": "recording_stopped",
                "camera": 2,
                "message": "Camera 2 recording worker stopped.",
                "severity": "critical",
            }
        }
        cards = self.compute_alerts_page_health_cards({"alerts": alerts}, health_issues)
        self.assertEqual(len(cards), 1)
        self.assertIn('<span class="coming">Active</span>', cards[0])
        self.assertNotIn("(old)", "".join(cards))

    def test_non_health_alert_types_still_render_as_in_app_alert_cards(self):
        alerts = [
            {"event_type": "person", "read": False, "camera": 1, "message": "Person detected on Camera 1."},
            {"event_type": "car", "read": False, "camera": 2, "message": "Vehicle detected on Camera 2."},
        ]
        cards = self.compute_alerts_page_health_cards({"alerts": alerts}, {})
        self.assertEqual(len(cards), 2)
        joined = "".join(cards)
        self.assertIn("Person detected on Camera 1.", joined)
        self.assertIn("Vehicle detected on Camera 2.", joined)
        self.assertIn('<span class="coming">In-app alert</span>', joined)

    def test_no_health_issues_and_no_persisted_health_alerts_renders_nothing(self):
        cards = self.compute_alerts_page_health_cards({"alerts": []}, {})
        self.assertEqual(cards, [])


class UntouchedSubsystemsSourceTests(unittest.TestCase):
    """Confirms IN_APP_ALERTS_FILE, in_app_alerts(), /api/alerts,
    append_in_app_alert(), health_monitor(), and motion event generation
    were not modified by this fix -- it only changes how def alerts()
    (the /alerts page) derives its own local card list."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_in_app_alerts_reader_is_unfiltered_and_unchanged(self):
        self.assertIn("def in_app_alerts(limit: int = 100) -> dict:", self.source)
        self.assertIn("alerts.append(json.loads(line))", self.source)

    def test_append_only_persistence_helper_unchanged(self):
        self.assertIn("def append_in_app_alert(alert: dict) -> None:", self.source)
        self.assertIn(
            'with IN_APP_ALERTS_FILE.open("a", encoding="utf-8") as alert_file:',
            self.source,
        )

    def test_alerts_history_api_route_unchanged(self):
        self.assertIn('@app.get("/api/alerts")', self.source)

    def test_append_call_sites_unchanged(self):
        # One def plus three call sites (motion alert delivery, the
        # object-detection loop, and health_monitor()) -- unchanged in
        # number and in the fact that each is dispatched via
        # asyncio.to_thread(append_in_app_alert, {...}), not called directly.
        self.assertEqual(self.source.count("append_in_app_alert"), 4)
        self.assertEqual(self.source.count("append_in_app_alert,"), 3)

    def test_motion_card_rendering_path_unchanged(self):
        self.assertIn(
            "events = sorted(load_motion_events(), "
            'key=lambda event: event.get("start_time") or event.get("timestamp", ""), '
            "reverse=True)[:20]",
            self.source,
        )
        self.assertIn(
            'alert_cards.append(f\'<article class="feature-card">{thumbnail}'
            "<h2>Motion · Camera {event.get(\"camera\", \"—\")}</h2>",
            self.source,
        )

    def test_alerts_route_still_present_and_unmoved(self):
        self.assertIn('@app.get("/alerts", response_class=HTMLResponse)', self.source)
        self.assertIn("def alerts() -> str:", self.source)


if __name__ == "__main__":
    unittest.main()
