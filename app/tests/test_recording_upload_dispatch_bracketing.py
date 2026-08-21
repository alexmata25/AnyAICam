"""Diagnostic-only bracketing around the per-camera dispatch sequence in
recording_upload_worker()'s main loop: known_camera_numbers_done,
camera_identity_done, to_thread_dispatch_begin, and
relay_camera_once_entered (the literal first line of
_relay_camera_once() itself). Follows scan_tick_begin and
http_call_begin/returned -- together these three rounds of bracketing
were built to locate exactly where a real, still-unresolved appliance
hang occurs: scan #1 begins, the config-refresh HTTP call both begins
and returns successfully, and then nothing else ever happens. The
decisive pair this round is meant to observe is to_thread_dispatch_begin
appearing with no matching relay_camera_once_entered ever following --
proof the asyncio.to_thread() call is queued waiting for a thread pool
worker rather than actually executing.

These tests only verify the new log lines and their ordering; no
credential/S3/cutoff/cap/timing behavior is touched by this change.
"""

import asyncio
import logging

import pytest

import recording_uploader as ru


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _log_messages(caplog):
    return [r.message for r in caplog.records]


# ------------------------------------------------- _relay_camera_once entry


def test_relay_camera_once_logs_entered_as_its_first_action(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    calls = []

    def _fake_ensure_session(camera_number, camera_id):
        calls.append("ensure_session")
        return None  # early return right after -- nothing past this should run

    monkeypatch.setattr(ru, "_ensure_session", _fake_ensure_session)

    ru._relay_camera_once(1, "camera-1-id")

    messages = _log_messages(caplog)
    assert "recording_upload.relay_camera_once_entered camera=1" in messages
    # The entered log must be recorded before _ensure_session is even called --
    # confirmed by it being unconditional and appearing regardless of what
    # _ensure_session does, including returning None immediately.
    assert calls == ["ensure_session"]


# ------------------------------------------- worker loop dispatch sequence


async def _run_worker_briefly(caplog, timeout_seconds=1.0):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    task = asyncio.create_task(ru.recording_upload_worker())
    await asyncio.sleep(timeout_seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.anyio
async def test_known_camera_numbers_and_identity_logged_per_camera(monkeypatch, caplog):
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 999.0)  # only need one iteration
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 999999.0)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1, 2])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": f"cam-{n}-id"})
    monkeypatch.setattr(ru, "_relay_camera_once", lambda n, cid: None)  # no real processing

    await _run_worker_briefly(caplog)

    messages = _log_messages(caplog)
    assert "recording_upload.known_camera_numbers_done camera=1" in messages
    assert "recording_upload.known_camera_numbers_done camera=2" in messages
    assert "recording_upload.camera_identity_done camera=1 found=True" in messages
    assert "recording_upload.camera_identity_done camera=2 found=True" in messages
    assert "recording_upload.to_thread_dispatch_begin camera=1" in messages
    assert "recording_upload.to_thread_dispatch_begin camera=2" in messages


@pytest.mark.anyio
async def test_camera_with_no_identity_skips_dispatch_entirely(monkeypatch, caplog):
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 999.0)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 999999.0)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [3])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: None)  # unknown camera

    await _run_worker_briefly(caplog)

    messages = _log_messages(caplog)
    assert "recording_upload.known_camera_numbers_done camera=3" in messages
    assert "recording_upload.camera_identity_done camera=3 found=False" in messages
    assert not any("to_thread_dispatch_begin" in m for m in messages)  # continue skipped it
    assert not any("relay_camera_once_entered" in m for m in messages)


@pytest.mark.anyio
async def test_dispatch_begin_precedes_relay_camera_once_entered_on_a_real_healthy_call(monkeypatch, caplog):
    """The baseline this whole diagnostic round depends on: when a
    to_thread() dispatch DOES succeed, dispatch_begin logs before
    entered does, using the real (unmocked) _relay_camera_once and a
    real asyncio.to_thread() call -- only _ensure_session is mocked, to
    keep this fast and network-free."""
    monkeypatch.setattr(ru, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_ENABLED", True)
    monkeypatch.setattr(ru, "SCAN_SECONDS", 999.0)
    monkeypatch.setattr(ru, "CONFIG_REFRESH_SECONDS", 999999.0)
    monkeypatch.setattr(ru, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(ru, "_known_camera_numbers", lambda: [1])
    monkeypatch.setattr(ru, "_camera_identity", lambda n: {"camera_id": "cam-1-id"})
    monkeypatch.setattr(ru, "_ensure_session", lambda camera_number, camera_id: None)  # fast early-return

    await _run_worker_briefly(caplog)

    messages = _log_messages(caplog)
    dispatch_idx = messages.index("recording_upload.to_thread_dispatch_begin camera=1")
    entered_idx = messages.index("recording_upload.relay_camera_once_entered camera=1")
    assert dispatch_idx < entered_idx
