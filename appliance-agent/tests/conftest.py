"""The agent decides Linux appliance vs native Windows install from the host
(anyaicam_agent/windows.py IS_WINDOWS). This suite describes the Linux
appliance's behavior, so every test runs with the Linux behavior whatever
host runs it; Windows tests (test_windows_platform.py) switch it on
explicitly for themselves."""
import pytest

from anyaicam_agent import windows


@pytest.fixture(autouse=True)
def linux_appliance_behavior(monkeypatch):
    monkeypatch.setattr(windows, 'IS_WINDOWS', False)
