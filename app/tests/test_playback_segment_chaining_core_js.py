"""pytest wrapper around the real Node-executed unit tests for the
segment-chaining planner core (_planNextChainedClip()/CHAIN_GAP_
TOLERANCE_SECONDS), embedded directly in _render_customer_playback()'s
own <script> in main.py. See test_playback_segment_chaining_core.mjs
for what's actually proven.

Same node-availability tolerance as this suite's other .mjs-backed
tests: skipped, not failed, if `node` isn't on PATH in this
environment.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"
JS_TEST = Path(__file__).resolve().parent / "test_playback_segment_chaining_core.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed in this environment")
def test_segment_chaining_core_javascript_behavior():
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
        "one or more segment-chaining JS unit tests failed -- see stdout/stderr above"
    )
