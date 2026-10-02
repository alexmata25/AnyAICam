"""Talkdown relay lifecycle under a real uvicorn server (Samsung Step 3
release verification, 2026-09-27). See talk_real_server_check.py for why
this complements the TestClient-based tests in test_talk_audio_relay.py.
"""
import importlib.util
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(
    importlib.util.find_spec("uvicorn") is None or importlib.util.find_spec("websockets") is None,
    reason="needs uvicorn and websockets (both ship in the release image)",
)
def test_talk_relay_lifecycle_under_a_real_server(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, os.path.join(HERE, "talk_real_server_check.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    assert "REAL-SERVER TALK CHECK: PASS" in output, output[-3000:]
    assert result.returncode == 0, output[-3000:]
