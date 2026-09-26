"""P0 #5 remediation, Phase 2/4/5 (2026-09-05, Codex review): real,
executed verification of the mobile Playback page's own
scheduleMobileEventPoll()/isMobileEventPending() async state machine
(app/tests/js/mobile_event_poll.test.mjs), covering exactly the
failure modes the review identified: a transient network error or
non-2xx response must not permanently stop polling; switching cameras
must abort the previous camera's in-flight request and a late/stale
response from it must never repaint the newly-selected camera; polling
must run the full 120-second window (media becoming ready between the
old ~60s ceiling and the real 120s deadline must still be picked up);
and reaching the deadline with events still pending must settle to an
explicit fallback render rather than leaving "Processing…" up forever.

String-content assertions on the rendered HTML (see
test_events_playback_autoplay.py and friends) can prove the markup and
constants exist, but they cannot exercise the actual async
retry/cancellation/timing interleavings these bugs depended on -- only
running the real code with controllable fetch/AbortController/timers
can (same rationale as test_talk_mic_pointer_lifecycle_js.py, this
file's own sibling). This extracts the real polling snippet live from
an actual _render_customer_playback() render (never a hand-copied
duplicate that could drift from what ships) and runs it under Node.

Phase 5 (2026-09-05): unlike test_talk_mic_pointer_lifecycle_js.py's
"skip, never fail, when node isn't installed" policy -- correct there
because the production Docker image deliberately carries no Node
runtime (this fix is a code-only frontend change that must not force
an image rebuild) -- this suite additionally refuses to silently skip
in CI. Setting ANYAICAM_REQUIRE_JS_TESTS=true (expected in a CI/test
image that DOES provision Node -- never the production runtime image)
turns a missing `node` binary into a hard test FAILURE instead of a
skip, so "Node isn't installed here" can never quietly present as
"this behavior is covered" the way an unconditional skip would. Local
development and the production image are unaffected: without that env
var, a missing node still skips exactly like the existing talk_mic
suite always has.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import main

_JS_TEST_RUNNER = Path(__file__).parent / "js" / "mobile_event_poll.test.mjs"


def _extract_mobile_poll_snippet() -> str:
    """Pulls the real, rendered scheduleMobileEventPoll()/
    isMobileEventPending() source out of an actual
    _render_customer_playback() call -- the exact text between the
    MOBILE_EVENT_PENDING_WINDOW_MS constant and renderMobileRecentEvents()'s
    own definition (which the JS test replaces with a spy, since it
    touches the DOM and isn't what this suite is testing -- see that
    file's own renderMobileRecentEvents stub)."""
    cameras = [{"id": "cam-1", "name": "Front Door", "camera_number": 1}]
    fake_request = MagicMock()
    fake_request.query_params = {}
    html = main._render_customer_playback(cameras, fake_request)
    start = html.index("const MOBILE_EVENT_PENDING_WINDOW_MS=")
    end = html.index("function renderMobileRecentEvents(")
    assert start > 0 and end > start, "expected polling snippet markers not found in rendered Playback HTML"
    return html[start:end]


def test_mobile_event_poll_state_machine_is_race_and_retry_safe():
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

    snippet = _extract_mobile_poll_snippet()
    with tempfile.TemporaryDirectory() as tmp:
        snippet_path = Path(tmp) / "mobile_poll_snippet.js"
        snippet_path.write_text(snippet, encoding="utf-8")

        result = subprocess.run(
            [node, str(_JS_TEST_RUNNER), str(snippet_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, (
        "mobile event-poll state machine JS tests failed "
        f"(exit {result.returncode}):\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
