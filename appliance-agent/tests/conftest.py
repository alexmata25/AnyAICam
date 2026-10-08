"""Suite-wide defaults for the appliance agent tests.

* The agent decides Linux appliance vs native Windows install from the host
  (anyaicam_agent/windows.py IS_WINDOWS). This suite describes the Linux
  appliance's behavior, so every test runs with the Linux behavior whatever
  host runs it; Windows tests (test_windows_platform.py,
  test_windows_link_requests.py) switch it on explicitly for themselves.
* The agent's local "AnyAiCam Setup" page (link_server.py) is off for every
  test unless a test starts a LinkServer itself (always on an ephemeral port),
  so the suite never binds 127.0.0.1:8790 on the machine running it.
"""
import pytest

from anyaicam_agent import windows


@pytest.fixture(autouse=True)
def linux_appliance_behavior(monkeypatch):
    monkeypatch.setattr(windows, 'IS_WINDOWS', False)


@pytest.fixture(autouse=True)
def no_default_setup_page(monkeypatch):
    monkeypatch.setenv('ANYAICAM_LINK_PORT', '0')
