"""P0 #5 remediation round 2 (2026-09-05, Codex second review): real,
executed verification of the desktop Events page's own
scheduleEventPoll()/reconcileDesktopEvent()/settleExpiredDesktopEvents()
reconciliation loop (app/tests/js/desktop_event_poll.test.mjs),
covering exactly the failure modes the second review identified:
exception recovery, HTTP 500 recovery, a never-resolving fetch bounded
by its own timeout, a real elapsed-time deadline (not an attempt-count
ceiling), a final failed request right at the deadline still settling,
ready-event reconciliation, multiple new events preserving newest-
first order, duplicate event IDs never producing a second row, and
only one timer/fetch ever being in flight at a time.

String-content assertions on the rendered HTML (see
test_p05_desktop_events_parity.py) can prove the constants and markup
exist, but they cannot exercise the actual async retry/timeout/
deadline/DOM-mutation behavior these bugs depended on -- only running
the real code with controllable fetch/AbortController/timers/DOM can
(same rationale as test_p05_mobile_poll_js.py and
test_talk_mic_pointer_lifecycle_js.py, this file's own siblings).

This extracts two pieces live from an actual _render_customer_events()
render (never a hand-copied duplicate that could drift from what
ships): the shared prelude (rows/apply()/wireEventThumbPlayer(), which
reconcileDesktopEvent() and scheduleEventPoll() both depend on) and the
polling functions themselves. Runs them under a small hand-rolled DOM
implementation (deliberately not a general CSS engine or a new project
dependency such as jsdom -- matching this codebase's own dependency-
light testing philosophy; only the exact selector shapes this code
actually uses are supported).

Phase 5 (2026-09-05): same ANYAICAM_REQUIRE_JS_TESTS=true CI policy as
test_p05_mobile_poll_js.py -- skips gracefully when node isn't
installed (matching the production runtime image, which deliberately
carries no Node dependency), but hard-FAILs instead when that env var
is set and node is still missing, so a CI/test image that forgets to
provision Node can't present "Node missing" as passing coverage.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main

_JS_TEST_RUNNER = Path(__file__).parent / "js" / "desktop_event_poll.test.mjs"


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


def _extract_desktop_poll_sources():
    """Pulls the real, rendered prelude (rows/apply()/wireEventThumbPlayer())
    and polling-function source out of an actual _render_customer_events()
    call, for one seeded pending event -- matching
    test_access_control_nav_and_page.py's own established render-and-
    extract pattern."""
    cameras = [{"id": "cam-1", "name": "Front Door", "camera_number": 1}]
    events_list = [{
        "id": "ev-1", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "motion", "timestamp": "2026-09-02T00:38:58", "confidence": 0.9,
        "thumbnail": None, "has_event_clip": False,
    }]
    with patch.object(main, "_customer_playback_cameras", lambda request: cameras), \
         patch.object(main, "_customer_detection_events", lambda request, **kwargs: events_list):
        html = main._render_customer_events(_fake_request())

    p_start = html.index("(function(){\n  const search=")
    d_start = html.index("const EVENT_PENDING_WINDOW_MS=")
    d_end = html.index("</script>", d_start)
    assert p_start > 0 and d_start > p_start and d_end > d_start, "expected desktop polling markers not found in rendered Events HTML"
    return html[p_start:d_start], html[d_start:d_end]


def test_desktop_event_poll_reconciliation_is_race_and_timeout_safe():
    import pytest

    node = shutil.which("node")
    if not node:
        if os.environ.get("ANYAICAM_REQUIRE_JS_TESTS", "").lower() == "true":
            pytest.fail(
                "node is required (ANYAICAM_REQUIRE_JS_TESTS=true) but not "
                "installed in this environment -- this must be a CI/test "
                "image that provisions Node, never the production runtime "
                "image; see this module's own docstring."
            )
        pytest.skip("node not installed in this environment -- see module docstring")

    prelude, snippet = _extract_desktop_poll_sources()
    with tempfile.TemporaryDirectory() as tmp:
        prelude_path = Path(tmp) / "desktop_prelude.js"
        snippet_path = Path(tmp) / "desktop_poll_snippet.js"
        prelude_path.write_text(prelude, encoding="utf-8")
        snippet_path.write_text(snippet, encoding="utf-8")

        result = subprocess.run(
            [node, str(_JS_TEST_RUNNER), str(prelude_path), str(snippet_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, (
        "desktop event-poll reconciliation JS tests failed "
        f"(exit {result.returncode}):\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
