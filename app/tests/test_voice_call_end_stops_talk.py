"""End call while Talk is active (2026-09-30 physical-test finding).

End call released only the call's own microphone while Talk kept streaming
from its clone, and the owner pressed End 11 times. The executed Node test
(js/voice_call_end_talk.test.mjs) runs the real shipped wireTalkMic() and
Voice Call controls together; the checks below cover what the server
renders. Skipped where node is not installed, like the other JS tests.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import aac_voice_call
from live_view_page import _TALK_MIC_JS

_RUNNER = Path(__file__).parent / "js" / "voice_call_end_talk.test.mjs"


def test_end_call_while_talk_is_active_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed in this environment")
    with tempfile.TemporaryDirectory() as tmp:
        talk = Path(tmp) / "talk_mic.js"
        talk.write_text(_TALK_MIC_JS, encoding="utf-8")
        controls = Path(tmp) / "call_controls.js"
        controls.write_text(aac_voice_call._CALL_CONTROLS_JS, encoding="utf-8")
        result = subprocess.run([node, str(_RUNNER), str(talk), str(controls)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "0 passed" not in result.stdout


def test_talk_mic_registers_its_stop_for_end_call():
    assert "window.anyaicamTalkStops" in _TALK_MIC_JS
    assert ".push(() => stop())" in _TALK_MIC_JS


def test_end_call_stops_talk_before_telling_the_server():
    js = aac_voice_call._CALL_CONTROLS_JS
    handler = js[js.index("endButton.addEventListener('click'"):]
    assert handler.index("if (callEnded) return;") < handler.index("finishCallLocally();") < handler.index("fetch(")
    finish = js[js.index("function finishCallLocally()"):js.index("if (callEnded) finishCallLocally();")]
    for step in ("stopCallTalk();", "releaseCallMicrophone();", "answerButton.disabled = true", "endButton.disabled = true", "mic.disabled = true", "note.hidden = false"):
        assert step in finish, step


def test_page_script_uses_the_shared_controls():
    source = Path(aac_voice_call.__file__).read_text(encoding="utf-8")
    assert "''' + _CALL_CONTROLS_JS + f'''" in source
    assert "const callInitiallyOver=" in source
    # Rendered disabled, with "Call ended." visible, for a call that is already over.
    assert "{' disabled' if call_over else ''}>Answer</button>" in source
    assert "{' disabled' if call_over else ''}>End call</button>" in source
    # The closed note says why (2026-10-02): ended, missed, or connection lost.
    assert "{'' if call_over else ' hidden'}><strong>{esc(_closed_label(event))}</strong>" in source
    assert 'call_over = (event.get("state") or "") in ("ended", "dismissed", "missed")' in source


def test_disabled_call_buttons_look_disabled():
    """Found on staging: a disabled Answer button still looked fully active."""
    source = Path(aac_voice_call.__file__).read_text(encoding="utf-8")
    assert ".dialog-actions button:disabled{{opacity:.4;cursor:not-allowed" in source
