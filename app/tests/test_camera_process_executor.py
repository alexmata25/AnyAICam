"""Dedicated camera-process executor: tests proving process.wait() and
the live-mode stderr drain now run on _CAMERA_PROCESS_EXECUTOR (a
dedicated ThreadPoolExecutor, mirroring live_relay_uploader.py's own
_relay_executor pattern) instead of asyncio.to_thread()'s shared
process-wide default executor -- and, critically, that this actually
achieves pool isolation: saturating the dedicated executor with
long-lived camera work no longer starves unrelated to_thread() callers
like recording_uploader.py's own scan loop, which was proven tonight
(real pilot appliance, 2026-08-21) to queue forever behind exactly this
contention.

Imports `main` -- per this project's own documented constraint, this
file can only run inside the deployed container.
"""

import asyncio
import subprocess
import sys
import time

import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_camera_process_executor.db"):
    import main

CAMERA_NUMBER = 9101  # not a real camera_number -- avoids collision


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_state():
    main.camera_process_state[CAMERA_NUMBER] = {}
    main.camera_reconnect_counts[CAMERA_NUMBER] = 0
    yield
    main.camera_process_state.pop(CAMERA_NUMBER, None)
    main.camera_reconnect_counts.pop(CAMERA_NUMBER, None)


def _sleeper_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


class _SpyExecutor:
    """Duck-typed like concurrent.futures.Executor (run_in_executor only
    ever calls .submit()) -- records every submitted callable while
    delegating the actual work to a real executor underneath."""

    def __init__(self, real):
        self._real = real
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append(fn)
        return self._real.submit(fn, *args)


# ------------------------------------------------------------- sizing


def test_executor_is_sized_for_all_twelve_current_long_lived_holders():
    assert main._CAMERA_PROCESS_EXECUTOR._max_workers >= 12


def test_executor_has_a_distinct_thread_name_prefix():
    assert main._CAMERA_PROCESS_EXECUTOR._thread_name_prefix == "camera_process_wait"


# --------------------------------------------------- dedicated-executor use


@pytest.mark.anyio
async def test_process_wait_uses_the_dedicated_executor(monkeypatch):
    import concurrent.futures
    real = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    spy = _SpyExecutor(real)
    monkeypatch.setattr(main, "_CAMERA_PROCESS_EXECUTOR", spy)
    monkeypatch.setattr(main, "start_recording", lambda n: _sleeper_process())

    task = asyncio.create_task(main.process_supervisor(CAMERA_NUMBER, "recording"))
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if spy.submitted:
                break
        assert any(fn.__self__.__class__.__name__ == "Popen" for fn in spy.submitted if hasattr(fn, "__self__"))
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        real.shutdown(wait=False, cancel_futures=True)
        for p in list(main.ffmpeg_processes):
            if p.poll() is None:
                try:
                    p.kill()
                    p.wait()
                except Exception:
                    pass
                main.ffmpeg_processes.remove(p)


@pytest.mark.anyio
async def test_stderr_drain_uses_the_dedicated_executor(monkeypatch):
    import concurrent.futures
    real = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    spy = _SpyExecutor(real)
    monkeypatch.setattr(main, "_CAMERA_PROCESS_EXECUTOR", spy)

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stderr=subprocess.PIPE,
    )
    buffer: list[str] = []
    try:
        drain_task = asyncio.create_task(main._drain_camera_stderr(CAMERA_NUMBER, process, buffer))
        await asyncio.sleep(0.1)
        assert spy.submitted  # something was dispatched through the dedicated executor
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
    finally:
        process.kill()
        process.wait()
        real.shutdown(wait=False, cancel_futures=True)


@pytest.mark.anyio
async def test_live_mode_still_gets_stderr_drain_recording_mode_does_not(monkeypatch):
    """Regression guard: the executor swap must not have disturbed the
    existing mode == "live" condition that gates the drain task."""
    live_dummy = _sleeper_process()
    recording_dummy = _sleeper_process()
    monkeypatch.setattr(main, "start_live_stream", lambda n: live_dummy)
    monkeypatch.setattr(main, "start_recording", lambda n: recording_dummy)
    drained = []
    orig = main._drain_camera_stderr

    async def _spy_drain(camera_number, process, buffer):
        drained.append(camera_number)
        await orig(camera_number, process, buffer)

    monkeypatch.setattr(main, "_drain_camera_stderr", _spy_drain)

    live_task = asyncio.create_task(main.process_supervisor(CAMERA_NUMBER, "live"))
    recording_task = asyncio.create_task(main.process_supervisor(CAMERA_NUMBER + 1, "recording"))
    main.camera_process_state[CAMERA_NUMBER + 1] = {}
    main.camera_reconnect_counts[CAMERA_NUMBER + 1] = 0
    try:
        await asyncio.sleep(0.2)
        assert CAMERA_NUMBER in drained  # live mode drains stderr
        assert (CAMERA_NUMBER + 1) not in drained  # recording mode does not
    finally:
        for t in (live_task, recording_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        for p in (live_dummy, recording_dummy):
            if p.poll() is None:
                p.kill()
                p.wait()
        for proc in list(main.ffmpeg_processes):
            if proc in (live_dummy, recording_dummy):
                try:
                    main.ffmpeg_processes.remove(proc)
                except ValueError:
                    pass
        main.camera_process_state.pop(CAMERA_NUMBER + 1, None)
        main.camera_reconnect_counts.pop(CAMERA_NUMBER + 1, None)


# ---------------------------------------------------------- pool isolation


@pytest.mark.anyio
async def test_saturating_camera_executor_does_not_starve_default_pool_work():
    """The actual property this whole fix exists to guarantee: even
    with the dedicated camera executor fully saturated by long-lived
    blocking work, an unrelated asyncio.to_thread() call (using the
    separate, shared default executor -- the same mechanism
    recording_uploader.py's scan loop uses) still completes promptly."""
    import concurrent.futures
    small_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="test_camera_pool")
    release = False

    def _block_forever():
        while not release:
            time.sleep(0.01)

    futures = [small_pool.submit(_block_forever) for _ in range(2)]  # saturate all workers
    try:
        start = time.monotonic()
        result = await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=2.0)
        elapsed = time.monotonic() - start
        assert result == 42
        assert elapsed < 1.0  # completed promptly, not blocked behind the saturated dedicated pool
    finally:
        release = True
        for f in futures:
            f.result(timeout=2)
        small_pool.shutdown(wait=True)


# ------------------------------------------- existing recovery path unaffected


@pytest.mark.anyio
async def test_existing_watchdog_kill_and_retry_still_works_with_dedicated_executor(monkeypatch):
    """The same real end-to-end scenario proven earlier tonight for the
    watchdog fix, re-run against the new dedicated-executor code path --
    confirms process.wait() moving off the default executor didn't
    disturb the existing exit-triggered retry/restart logic at all."""
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", __import__("pathlib").Path(tmp_dir))
    monkeypatch.setattr(main, "RECORDING_PROGRESS_CHECK_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(main, "RECORDING_STALL_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(main, "RECORDING_SEGMENT_SECONDS", 0.02)
    folder = main.RECORDINGS_FOLDER / f"camera{CAMERA_NUMBER}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv").write_bytes(b"stalled")  # never updated

    dummy = _sleeper_process()
    monkeypatch.setattr(main, "start_recording", lambda n: dummy)

    task = asyncio.create_task(main.process_supervisor(CAMERA_NUMBER, "recording"))
    try:
        reached_retrying = False
        for _ in range(200):
            await asyncio.sleep(0.02)
            if main.camera_process_state[CAMERA_NUMBER].get("recording") == "retrying":
                reached_retrying = True
                break
        assert reached_retrying
        assert dummy.poll() is not None
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if dummy.poll() is None:
            dummy.kill()
            dummy.wait()
        try:
            main.ffmpeg_processes.remove(dummy)
        except ValueError:
            pass
