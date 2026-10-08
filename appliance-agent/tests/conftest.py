"""The agent's local "AnyAiCam Setup" page (link_server.py) is off for every
test unless a test starts a LinkServer itself (always on an ephemeral port),
so the suite never binds 127.0.0.1:8790 on the machine running it."""
import pytest


@pytest.fixture(autouse=True)
def no_default_setup_page(monkeypatch):
    monkeypatch.setenv('ANYAICAM_LINK_PORT', '0')
