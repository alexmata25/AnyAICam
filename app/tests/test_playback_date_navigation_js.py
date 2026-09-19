"""pytest wrapper around the real Node-executed unit tests for the
Previous/Next Day date-shift core (shiftedDateString()), embedded
directly in _render_customer_playback()'s own <script> in main.py.
See test_playback_date_navigation.mjs for what's actually proven,
including the two 2026 America/Chicago DST transition days.

Same node-availability tolerance as test_playback_segment_chaining.py
and test_playback_analytics_lanes_js.py: skipped, not failed, if `node`
isn't on PATH in this environment.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"
JS_TEST = Path(__file__).resolve().parent / "test_playback_date_navigation.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed in this environment")
def test_date_navigation_core_javascript_behavior():
    result = subprocess.run(
        ["node", str(JS_TEST), str(MAIN_PY)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
    assert result.returncode == 0, (
        "one or more date-navigation JS unit tests failed -- see stdout/stderr above"
    )
