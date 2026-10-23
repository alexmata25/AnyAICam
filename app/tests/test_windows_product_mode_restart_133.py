"""Windows 1.3.3 D3 (Blue, 2026-10-22): a persisted Local<->Hybrid change must reach the running Windows VMS.

Sandbox F-H / U132-L / U132-H on 1.3.3 126863e3: the cloud's restart_vms (`AnyAiCamVMS.exe restart`) restarted the
service 14 s BEFORE edge sync persisted the new mode, and the Windows VMS had no self-restart, so after a switch to
Hybrid it kept the Local flags and never started MediaMTX/P2P. The VMS now asks its own WinSW wrapper for `restart!`
once the persisted mode makes a running flag stale. Linux behaviour (Docker marker, SIGTERM) is unchanged."""
import asyncio
import subprocess
import sys

import pytest

import product_mode as pm

FLAG = "ANYAICAM_LIVE_RELAY_ENABLED"


class Completed:
    def __init__(self, returncode=0):
        self.returncode, self.stdout, self.stderr = returncode, "", ""


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "product_mode.json")
    monkeypatch.setattr(pm, "_RESOLVED_AT_START", {})
    monkeypatch.setattr(pm, "_UNSUPPORTED_LOGGED", None)
    monkeypatch.setattr(pm, "_WINDOWS_RESTART_REQUESTED_AT", None)
    monkeypatch.setattr(pm, "_DOCKER_MARKER", tmp_path / "no-dockerenv")
    for name in (pm.PRODUCT_MODE_ENV, pm.SELF_RESTART_ENV, FLAG):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(pm.BOOTSTRAP_MODE_ENV, "local")
    # Never a real SIGTERM / hard exit from these tests.
    monkeypatch.setattr(pm.os, "kill", lambda *a: pytest.fail("os.kill must not be used for a Windows service restart"))
    monkeypatch.setattr(pm.threading, "Timer", lambda *a, **k: pytest.fail("no hard-exit timer on Windows"))


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """An installed Windows VMS: <root>\\runtime\\python\\python.exe and <root>\\service\\AnyAiCamVMS.exe."""
    root = tmp_path / "AnyAiCam"
    python = root / "runtime" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    wrapper = root / "service" / "AnyAiCamVMS.exe"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(b"")
    monkeypatch.setattr(pm, "_WINDOWS", True)
    monkeypatch.setattr(pm.sys, "executable", str(python))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return Completed(0)

    monkeypatch.setattr(pm.subprocess, "run", run)
    return wrapper, calls


def start_local_process_then_persist_hybrid():
    assert pm.resolve_cloud_flag(FLAG) is False  # what the module does at import, in Local
    assert pm.persist_mode("hybrid") is True     # edge sync, AFTER restart_vms already restarted the service


def test_the_wrapper_is_found_from_the_bundled_interpreter(installed):
    wrapper, _ = installed
    assert pm._windows_service_wrapper() == wrapper.resolve()


def test_no_wrapper_outside_an_installed_windows_vms(installed, monkeypatch):
    wrapper, _ = installed
    wrapper.unlink()
    assert pm._windows_service_wrapper() is None
    wrapper.write_bytes(b"")
    monkeypatch.setattr(pm, "_WINDOWS", False)  # Linux: never, even if such a file existed
    assert pm._windows_service_wrapper() is None


def test_d3_local_to_hybrid_restarts_the_windows_service_with_restart_bang(installed, caplog):
    wrapper, calls = installed
    start_local_process_then_persist_hybrid()
    with caplog.at_level("WARNING", logger="anyaicam.product_mode"):
        assert pm.restart_if_running_flags_stale() == [FLAG]
    assert calls == [[str(wrapper.resolve()), "restart!"]]
    assert any("Windows service" in r.getMessage() for r in caplog.records)


def test_d3_hybrid_to_local_is_applied_the_same_way(installed):
    wrapper, calls = installed
    pm.persist_mode("hybrid")
    assert pm.resolve_cloud_flag(FLAG) is True
    pm.persist_mode("local")
    assert pm.restart_if_running_flags_stale() == [FLAG]
    assert calls == [[str(wrapper.resolve()), "restart!"]]


def test_the_restart_is_requested_once_while_it_is_under_way(installed, monkeypatch):
    _, calls = installed
    now = [1000.0]
    monkeypatch.setattr(pm.time, "monotonic", lambda: now[0])
    start_local_process_then_persist_hybrid()
    for _ in range(5):  # sync cycles while WinSW is stopping the service
        assert pm.restart_if_running_flags_stale() == [FLAG]
    assert len(calls) == 1
    now[0] += pm.WINDOWS_RESTART_RETRY_SECONDS + 1  # still running and stale long after: ask again
    pm.restart_if_running_flags_stale()
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [Completed(1), OSError("access denied"), subprocess.TimeoutExpired("x", 60)])
def test_a_failed_restart_request_keeps_the_vms_running_and_is_retried(installed, monkeypatch, failure):
    _, calls = installed

    def run(args, **kwargs):
        calls.append(args)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(pm.subprocess, "run", run)
    start_local_process_then_persist_hybrid()
    with pytest.raises(RuntimeError):  # edge_camera_sync logs product_mode_restart_failed and keeps going
        pm.restart_if_running_flags_stale()
    assert pm._WINDOWS_RESTART_REQUESTED_AT is None
    with pytest.raises(RuntimeError):
        pm.restart_if_running_flags_stale()  # next cycle tries again
    assert len(calls) == 2


def test_self_restart_can_still_be_turned_off_on_windows(installed, monkeypatch):
    _, calls = installed
    monkeypatch.setenv(pm.SELF_RESTART_ENV, "false")
    start_local_process_then_persist_hybrid()
    assert pm.restart_if_running_flags_stale() == [FLAG]
    assert calls == []


def test_an_unchanged_mode_never_restarts_the_service(installed):
    _, calls = installed
    assert pm.resolve_cloud_flag(FLAG) is False
    pm.persist_mode("local")
    assert pm.restart_if_running_flags_stale() == []
    assert calls == []


def test_the_restarted_service_is_current_so_it_never_loops(installed, monkeypatch):
    _, calls = installed
    start_local_process_then_persist_hybrid()
    monkeypatch.setattr(pm, "_RESOLVED_AT_START", {})  # the new process after WinSW started it again
    monkeypatch.setattr(pm, "_WINDOWS_RESTART_REQUESTED_AT", None)
    assert pm.resolve_cloud_flag(FLAG) is True
    assert pm.restart_if_running_flags_stale() == []
    assert calls == []


def test_the_sync_worker_restarts_the_windows_service_after_persisting_hybrid(installed, monkeypatch):
    import edge_camera_sync

    wrapper, calls = installed
    assert pm.resolve_cloud_flag(FLAG) is False
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "sync_provisioned_cameras",
                        lambda: {"status": "ok", "product_mode_restart_required": pm.persist_mode("hybrid")})
    monkeypatch.setattr(edge_camera_sync, "refresh_analytics_camera_map", lambda: None)

    async def stop_after_one_cycle(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(edge_camera_sync.asyncio, "sleep", stop_after_one_cycle)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(edge_camera_sync.camera_configuration_sync_worker())
    assert calls == [[str(wrapper.resolve()), "restart!"]]


def test_linux_is_unchanged_without_a_docker_marker(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pm, "_WINDOWS", False)
    monkeypatch.setattr(pm.subprocess, "run", lambda *a, **k: pytest.fail("no WinSW on Linux"))
    start_local_process_then_persist_hybrid()
    with caplog.at_level("WARNING", logger="anyaicam.product_mode"):
        assert pm.restart_if_running_flags_stale() == [FLAG]
    assert any("self_restart=unavailable" in r.getMessage() for r in caplog.records)


def test_linux_container_still_ends_its_process_with_sigterm(tmp_path, monkeypatch):
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    monkeypatch.setattr(pm, "_DOCKER_MARKER", marker)
    monkeypatch.setattr(pm, "_WINDOWS", False)
    monkeypatch.setattr(pm.subprocess, "run", lambda *a, **k: pytest.fail("no WinSW on Linux"))
    killed, timers = [], []

    class Timer:
        def __init__(self, seconds, function, args=()):
            timers.append((seconds, function, args))
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(pm.threading, "Timer", Timer)
    monkeypatch.setattr(pm.os, "kill", lambda pid, sig: killed.append(sig))
    start_local_process_then_persist_hybrid()
    pm.restart_if_running_flags_stale()
    assert killed == [pm.signal.SIGTERM]
    assert timers == [(pm.SELF_RESTART_GRACE_SECONDS, pm.os._exit, (75,))]
    assert pm._WINDOWS_RESTART_REQUESTED_AT is None
