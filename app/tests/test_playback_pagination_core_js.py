"""pytest wrapper around the real Node-executed unit tests for the
customer Playback "Load older recordings" pagination logic
(fetchClipsMetadata()/handleLoadOlderClick()/ensureClipsLoaded()),
embedded directly in _render_customer_playback()'s own <script> in
main.py. See test_playback_pagination_core.mjs for what's actually
proven -- in particular the two real defects it exists to catch:
a transient fetch error being mistaken for end-of-history, and a
camera switch mid-fetch corrupting a different camera's cached
recording list.

Same node-availability tolerance as this suite's other .mjs-backed
tests: skipped, not failed, if `node` isn't on PATH in this
environment.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"
JS_TEST = Path(__file__).resolve().parent / "test_playback_pagination_core.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed in this environment")
def test_playback_pagination_core_javascript_behavior():
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
        "one or more Playback pagination JS unit tests failed -- see stdout/stderr above"
    )
