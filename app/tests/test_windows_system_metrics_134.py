"""W-3 (Windows 1.3.4 parity, 2026-10-27): system_metrics() must report real
CPU and memory use on Windows.

It read only os.getloadavg() and /proc/meminfo. Neither exists on Windows,
so cpu_percent and memory_percent were always 0: the "System CPU usage is
high" health alert (>= 85 %) could never fire, and no load-based safeguard
could see the PC's load. psutil (pinned in requirements-windows.txt) is
used where the Linux sources are missing; Linux keeps its sources.
"""
import subprocess
import sys
import types

import pytest

import main


class _FakePsutil(types.ModuleType):
    def __init__(self, cpu=(0.0, 73.5), memory=61.2):
        super().__init__("psutil")
        self._cpu, self.cpu_calls, self._memory = list(cpu), [], memory

    def cpu_percent(self, interval=None):
        self.cpu_calls.append(interval)
        return self._cpu.pop(0) if len(self._cpu) > 1 else self._cpu[0]

    def virtual_memory(self):
        return types.SimpleNamespace(percent=self._memory)


@pytest.fixture(autouse=True)
def _fresh_cpu_state(monkeypatch):
    monkeypatch.setattr(main, "_PSUTIL_CPU_PRIMED", False)
    monkeypatch.setattr(main, "_psutil_cpu_last", None)


def _no_linux_sources(monkeypatch):
    monkeypatch.delattr(main.os, "getloadavg", raising=False)
    real_open = main.Path.open

    def no_proc(self, *args, **kwargs):
        if str(self).replace("\\", "/") == "/proc/meminfo":
            raise FileNotFoundError(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(main.Path, "open", no_proc)


def test_without_linux_sources_cpu_and_memory_come_from_psutil(monkeypatch):
    _no_linux_sources(monkeypatch)
    fake = _FakePsutil(cpu=(0.0, 73.5), memory=61.2)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(main, "_PSUTIL_CPU_PRIMED", False, raising=False)
    metrics = main.system_metrics()
    assert metrics["cpu_percent"] == 73.5  # not psutil's meaningless first 0.0
    assert metrics["memory_percent"] == 61.2
    assert len(fake.cpu_calls) == 2  # one priming call, then a measured one


def test_the_priming_happens_once(monkeypatch):
    _no_linux_sources(monkeypatch)
    fake = _FakePsutil(cpu=(0.0, 40.0, 55.0))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(main, "_PSUTIL_CPU_PRIMED", False, raising=False)
    main.system_metrics()
    monkeypatch.setattr(main, "_psutil_cpu_last", (main.time.monotonic() - 5, 40.0))
    assert main.system_metrics()["cpu_percent"] == 55.0
    assert fake.cpu_calls[-1] is None  # later calls never block


def test_a_call_right_after_a_reading_reuses_it_instead_of_measuring_nothing(monkeypatch):
    """cpu_percent(None) over a few microseconds is noise (often 0 %)."""
    _no_linux_sources(monkeypatch)
    fake = _FakePsutil(cpu=(0.0, 40.0, 0.0))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    main.system_metrics()
    assert main.system_metrics()["cpu_percent"] == 40.0
    assert len(fake.cpu_calls) == 2


def test_without_psutil_either_the_values_stay_zero_not_an_error(monkeypatch):
    _no_linux_sources(monkeypatch)
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil -> ImportError
    monkeypatch.setattr(main, "_PSUTIL_CPU_PRIMED", False, raising=False)
    metrics = main.system_metrics()
    assert metrics["cpu_percent"] == 0 and metrics["memory_percent"] == 0


def test_linux_sources_are_still_used_when_present(monkeypatch):
    monkeypatch.setattr(main.os, "getloadavg", lambda: (2.0, 1.0, 1.0), raising=False)
    monkeypatch.setattr(main.os, "cpu_count", lambda: 4)
    fake = _FakePsutil(cpu=(99.0,))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert main.system_metrics()["cpu_percent"] == 50.0
    assert fake.cpu_calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows counters")
def test_windows_reports_real_non_zero_cpu_and_memory():
    busy = subprocess.Popen([sys.executable, "-c", "import time\nend=time.time()+3\nwhile time.time()<end: pass"])
    try:
        metrics = main.system_metrics()
        metrics = main.system_metrics()
    finally:
        busy.wait(timeout=30)
    assert 0 < metrics["memory_percent"] <= 100
    assert 0 < metrics["cpu_percent"] <= 100
