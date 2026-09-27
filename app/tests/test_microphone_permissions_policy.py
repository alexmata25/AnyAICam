"""The browser microphone must stay usable on the VMS's own pages
(2026-09-27): the press-and-hold Talk button (getUserMedia) and AACO voice
input (SpeechRecognition) both need it, including inside the same-origin
AAC Voice Call screen iframe. A Permissions-Policy of `microphone=()`
disables it for every origin -- including our own -- before any permission
prompt appears, so neither the security middleware nor main.py's fallback
may ever send that.
"""
from pathlib import Path

from fastapi.testclient import TestClient

import main

APP_DIR = Path(__file__).resolve().parent.parent


def test_responses_allow_the_microphone_for_the_vms_itself():
    client = TestClient(main.app, base_url="https://app.anyaicam.com")
    policy = client.get("/health").headers.get("permissions-policy", "")
    assert "microphone=(self)" in policy
    assert "microphone=()" not in policy


def test_neither_policy_source_ever_blocks_the_microphone_everywhere():
    for name in ("main.py", "cloud_security.py"):
        source = (APP_DIR / name).read_text(encoding="utf-8")
        assert "microphone=()" not in source, name
        assert "microphone=(self)" in source, name
