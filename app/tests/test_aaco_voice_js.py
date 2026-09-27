"""Executed behavioural test of AACO voice input (window.aacoAttachVoice in
aaco_web._AACO_CLIENT_CORE_JS), shared by the floating AACO panel and the
/aaco workspace (2026-09-27). Runs the real shipped JS under Node with a
fake SpeechRecognition (tests/js/aaco_voice.test.mjs): spoken sentences
go through the same input and form submit as typing, silence retries
once, every recognition error gives a clear message and leaves typing
usable, and a late event from a replaced session can no longer stop a
new one. Skipped where Node isn't installed (the release image has none);
the string-level tests in test_aaco_floating_widget.py run everywhere.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from aaco_web import _AACO_CLIENT_CORE_JS

_RUNNER = Path(__file__).parent / "js" / "aaco_voice.test.mjs"


def test_aaco_voice_input_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed in this environment")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "aaco_core.js"
        source.write_text(_AACO_CLIENT_CORE_JS, encoding="utf-8")
        result = subprocess.run([node, str(_RUNNER), str(source)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_aaco_workspace_offers_voice_through_the_shared_implementation():
    from aaco_web import _workspace

    page = _workspace()
    assert 'id="aaco-mic"' in page and "disabled>🎤</button>" in page
    assert "window.aacoAttachVoice(form,input,status,document.getElementById('aaco-mic'))" in page
    assert page.count("window.aacoAttachVoice=function") == 1  # defined once, in the shared core
    assert ".aaco-mic-listening{" in page


def test_the_floating_panel_uses_the_same_shared_voice_implementation():
    from aaco_web import render_aaco_command_panel

    panel = render_aaco_command_panel(id_prefix="t", on_result_js_fn="noop")
    assert "window.aacoAttachVoice(form,input,status,micButton);" in panel
    assert "new SpeechRecognitionCtor" not in panel.split("window.aacoAttachVoice=function")[0].split("<script>")[-1]
