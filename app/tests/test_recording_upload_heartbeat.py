"""Diagnostic-only scan-tick heartbeat: tests for the
recording_upload.scan_tick_begin log line added to the top of
recording_upload_worker()'s main loop. Built in response to a real,
still-unresolved observation on the pilot appliance -- the worker was
confirmed alive (worker_started logged, live threads churning) but
produced zero log activity for well over ten expected 30-second scan
intervals, and this codebase's own "stay silent on a successful,
uneventful tick" convention made a genuinely stuck loop
indistinguishable from a healthy-but-boring one. This one line closes
that gap without touching any other behavior.

Pure regression-guard tests: everything that would make a real network
call or touch a real camera (_refresh_camera_map, _known_camera_numbers)
is mocked out, so these tests only ever exercise the heartbeat/loop
scaffolding itself, never credentials, S3, the cutoff, or the per-scan
cap.
"""

import asyncio
import logging

import pytest

import recording_uploader as ru


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_worker_state(monkeypatch):
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 0.02)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 999999.0)  # never refresh during the test
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [])  # no real camera processing
    ru.recording_upload_state["worker_status"] = "disabled"
    ru.recording_upload_state["last_scan_at"] = None
    ru.recording_upload_state["last_error"] = None
    yield


async def _run_worker_for_n_ticks(caplog, n, timeout_seconds=3.0):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    task = asyncio.create_task(ru.recording_upload_worker())
    try:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            ticks = [r for r in caplog.records if "scan_tick_begin" in r.message]
            if len(ticks) >= n:
                return ticks
            await asyncio.sleep(0.02)
        return [r for r in caplog.records if "scan_tick_begin" in r.message]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.anyio
async def test_heartbeat_logs_on_every_iteration_unconditionally(caplog):
    ticks = await _run_worker_for_n_ticks(caplog, n=3)
    assert len(ticks) >= 3


@pytest.mark.anyio
async def test_scan_number_increments_monotonically_from_one(caplog):
    ticks = await _run_worker_for_n_ticks(caplog, n=4)
    numbers = [int(r.message.split("scan_number=")[1].split(" ")[0]) for r in ticks]
    assert numbers[:4] == [1, 2, 3, 4]


@pytest.mark.anyio
async def test_heartbeat_includes_a_timestamp(caplog):
    ticks = await _run_worker_for_n_ticks(caplog, n=1)
    assert "at=" in ticks[0].message


@pytest.mark.anyio
async def test_disabled_worker_never_logs_a_heartbeat(monkeypatch, caplog):
    """The one-time startup gate is untouched -- a disabled worker
    still sleeps silently forever, exactly as before."""
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", False)
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    task = asyncio.create_task(ru.recording_upload_worker())
    try:
        await asyncio.sleep(0.1)
        ticks = [r for r in caplog.records if "scan_tick_begin" in r.message]
        assert ticks == []
        assert ru.recording_upload_state["worker_status"] == "disabled"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
