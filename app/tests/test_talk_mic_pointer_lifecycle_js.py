"""Real, executed verification of wireTalkMic()'s async pointer-lifecycle
handling (app/tests/js/talk_mic_lifecycle.test.mjs), covering the mobile
race this fixes: pointerup running stop() (clearing sessionId) while a
concurrent start() is still awaiting fetch/getUserMedia, which used to
let the resumed start() open a WebSocket at /sessions/null/audio.

String-content assertions on the rendered HTML (see
test_talk_down_foundation.py's own pointer-lifecycle test) can prove the
four release events are wired up, but they cannot exercise the actual
async interleaving that caused the bug -- only running the real code
with controllable timing can. This test extracts _TALK_MIC_JS directly
from live_view_page.py (never a hand-copied duplicate that could drift
from what's actually shipped) and runs it under Node with fake
fetch/getUserMedia/WebSocket/AudioContext implementations the JS test
file drives itself.

Skipped, not failed, wherever `node` isn't installed: the production
Docker image (python:3.12-slim, see Dockerfile) has no Node runtime,
and this fix is deliberately a code-only frontend change that must not
force an image rebuild -- adding Node as a new image dependency just to
run this one test would defeat that. The test still runs for real in
any development environment that has Node (this one included), and the
existing string-content structural test keeps running everywhere
regardless.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from live_view_page import _TALK_MIC_JS

_JS_TEST_RUNNER = Path(__file__).parent / "js" / "talk_mic_lifecycle.test.mjs"


def test_wire_talk_mic_pointer_lifecycle_is_race_free():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed in this environment -- see module docstring")

    with tempfile.TemporaryDirectory() as tmp:
        source_path = Path(tmp) / "talk_mic_source.js"
        source_path.write_text(_TALK_MIC_JS)

        result = subprocess.run(
            [node, str(_JS_TEST_RUNNER), str(source_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, (
        "wireTalkMic() pointer-lifecycle JS tests failed "
        f"(exit {result.returncode}):\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
