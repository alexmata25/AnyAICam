"""Focused tests for the recording-mode process_supervisor() executor-
waste fix.

Background: process_supervisor()'s "recording" mode used to call
`return_code = await asyncio.to_thread(process.wait)` immediately after
starting the ffmpeg process, with no polling loop first -- unlike "live"
mode, which correctly polls `process.poll()` non-blockingly before ever
calling `process.wait()`. Since a healthy recording ffmpeg process runs
indefinitely (5-minute segment rotation, no natural exit), that
to_thread(process.wait) call permanently pinned one of Python's ~12
default-executor worker threads for the entire appliance uptime -- 5
threads gone, one per camera, confirmed live via py-spy and kernel
wchan sampling on this Samsung appliance.

The fix mirrors "live" mode's own safe pattern: a non-blocking
`while process.poll() is None: await asyncio.sleep(5)` loop for
recording mode too (without the live-only playlist-staleness watchdog,
which has no recording equivalent), so `process.wait()` is only ever
called once the process has already exited -- reaping it, not blocking
on it.

This file proves:
  1. Five long-running recording supervisors do not permanently occupy
     five asyncio default-executor threads.
  2. Recording ffmpeg exit is still detected (state transitions to
     "retrying" with the correct exit code).
  3. A failed/exited recording process is still cleaned up
     (ffmpeg_processes bookkeeping) and restarted (starter() called
     again after the existing 10s restart delay).
  4. Event-loop / asyncio.to_thread() capacity is retained -- a probe
     dispatched via to_thread() while all 5 recorders are alive
     completes quickly, not queued behind permanently-busy workers.

Same import/isolation constraints as the other fix-verification test
files this session: imports `main` (must run inside the deployed
container's Python). Does not touch, test, or modify the motion-
detector fix, AI, uploader, analytics sync, or any other feature.
"""

import asyncio
import time

import pytest

import main


class _FakeRecordingProcess:
    """Stands in for the subprocess.Popen object start_recording()
    returns. `.poll()` reports "still running" (None) for a configured
    number of calls, then reports exited. `.wait()` records every call
    with its own timestamp and the current wall-clock elapsed since the
    fake process was constructed -- used to prove wait() is only ever
    called once the process has already exited, never while it's still
    alive."""

    def __init__(self, alive_polls: int, exit_code: int = 0):
        self._alive_polls = alive_polls
        self._poll_count = 0
        self.returncode = None
        self.stderr = None
        self.wait_calls: list[float] = []
        self.terminate_calls = 0
        self._exit_code = exit_code

    def poll(self):
        self._poll_count += 1
        if self._poll_count <= self._alive_polls:
            return None
        self.returncode = self._exit_code
        return self._exit_code

    def wait(self, timeout=None):
        self.wait_calls.append(time.monotonic())
        # If wait() is ever called while poll() would still report
        # "alive", that's exactly the bug this fix removes.
        assert self._poll_count > self._alive_polls, (
            "process.wait() was called while the process was still "
            "running (poll() had not yet reported exit) -- this is the "
            "permanent-thread-occupation bug this fix removes"
        )
        return self._exit_code

    def terminate(self):
        self.terminate_calls += 1


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """process_supervisor()'s polling loop sleeps 5s (and the outer
    restart delay is 10s) in production. Replace asyncio.sleep with an
    immediate yield for these tests only, so 5 supervisors' full
    connect -> poll -> exit -> restart-delay -> restart cycle runs in
    milliseconds instead of tens of real seconds, without changing any
    of process_supervisor()'s own logic or timing constants."""
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)


class _RecordingStateDict(dict):
    """Records every (mode, value) assignment, in order, so a test can
    prove a transient state (like "retrying") occurred at some point --
    even if process_supervisor()'s outer restart loop later overwrites
    it with "connecting"/"running" again before the test gets a chance
    to observe the final value directly."""

    def __init__(self):
        super().__init__()
        self.history: list[tuple] = []

    def __setitem__(self, key, value):
        self.history.append((key, value))
        super().__setitem__(key, value)


def _make_starter(processes_by_camera: dict[int, list]):
    """Each call to the returned starter() pops the next fake process
    queued for that camera -- lets a test observe a restart by queuing
    a second fake process per camera."""

    def starter(camera_number):
        queue = processes_by_camera[camera_number]
        return queue.pop(0)

    return starter


# ---------------------------------------------------------------------------
# 1 & 4. No permanent executor-thread occupation; to_thread() capacity
#        retained while all 5 recording supervisors are alive.
# ---------------------------------------------------------------------------

def test_five_recorders_do_not_occupy_executor_and_capacity_is_retained(monkeypatch):
    cameras = [1, 2, 3, 4, 5]
    # Each process reports "alive" for many poll() calls -- long enough
    # that if wait() were still called immediately (the old bug), it
    # would be caught red-handed by _FakeRecordingProcess.wait()'s own
    # assertion above.
    processes = {cam: _FakeRecordingProcess(alive_polls=50) for cam in cameras}
    monkeypatch.setattr(main, "start_recording", _make_starter({cam: [processes[cam]] for cam in cameras}))
    monkeypatch.setattr(main, "camera_process_state", {cam: {} for cam in cameras})
    monkeypatch.setattr(main, "camera_reconnect_counts", {cam: 0 for cam in cameras})
    monkeypatch.setattr(main, "ffmpeg_processes", [])

    async def scenario():
        tasks = [
            asyncio.create_task(main.process_supervisor(cam, "recording")) for cam in cameras
        ]
        # Let each supervisor start and enter its poll loop.
        for _ in range(5):
            await asyncio.sleep(0)

        # While all 5 are "alive" (poll() still returning None), probe
        # the default executor: this must complete promptly. If the old
        # bug were present, wait() would already have been called and
        # would have raised inside _FakeRecordingProcess.wait() -- any
        # task exception surfaces when the tasks are gathered/cancelled
        # below, so a silent hang here would itself be the failure mode
        # to watch for too.
        start = time.monotonic()
        probe_result = await asyncio.wait_for(
            asyncio.to_thread(lambda: "probe-completed"), timeout=5.0
        )
        elapsed = time.monotonic() - start

        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return probe_result, elapsed, results

    probe_result, elapsed, results = asyncio.run(scenario())

    assert probe_result == "probe-completed"
    assert elapsed < 1.0, f"executor probe took {elapsed:.2f}s -- workers appear occupied"

    # None of the 5 supervisor tasks should have raised (in particular,
    # none of the fake processes' wait()-called-while-alive assertion
    # should have fired).
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result

    # And directly: none of the 5 processes ever had wait() called while
    # this test was probing them (they're still "alive" -- alive_polls=50
    # was never exhausted in this short run).
    for cam in cameras:
        assert processes[cam].wait_calls == [], (
            f"camera {cam}'s process.wait() was called while still running"
        )


# ---------------------------------------------------------------------------
# 2. Recording ffmpeg exit is still detected.
# ---------------------------------------------------------------------------

def test_recording_exit_is_detected(monkeypatch):
    camera_number = 9
    process = _FakeRecordingProcess(alive_polls=3, exit_code=1)
    monkeypatch.setattr(
        main, "start_recording", _make_starter({camera_number: [process, _FakeRecordingProcess(alive_polls=1000)]})
    )
    state = {camera_number: _RecordingStateDict()}
    monkeypatch.setattr(main, "camera_process_state", state)
    monkeypatch.setattr(main, "camera_reconnect_counts", {camera_number: 0})
    monkeypatch.setattr(main, "ffmpeg_processes", [])

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(camera_number, "recording"))
        # Give it enough scheduling turns to: start, poll through
        # alive_polls=3 iterations, detect exit, call wait(), clean up,
        # and reach the post-exit "retrying" state. (With the polling-
        # loop AND restart-delay sleeps both fast in this test, the
        # supervisor may well loop around and start a second process
        # too within this window -- that's fine, the history capture
        # below proves "retrying" occurred regardless of what happens
        # after.)
        for _ in range(30):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert process.wait_calls, "process.wait() should have been called once the process exited"
    assert len(process.wait_calls) == 1, "process.wait() should only be called once per exit"
    assert process.returncode == 1
    assert ("recording", "retrying") in state[camera_number].history, (
        f"expected a 'retrying' state transition after exit; history={state[camera_number].history}"
    )
    assert main.camera_reconnect_counts[camera_number] == 1


# ---------------------------------------------------------------------------
# 3. Failed/exited recording process is still cleaned up and restarted.
# ---------------------------------------------------------------------------

def test_recording_process_cleaned_up_and_restarted(monkeypatch):
    camera_number = 11
    first_process = _FakeRecordingProcess(alive_polls=2, exit_code=0)
    second_process = _FakeRecordingProcess(alive_polls=1000)
    call_log = []

    def starter(cam):
        call_log.append(cam)
        return first_process if len(call_log) == 1 else second_process

    monkeypatch.setattr(main, "start_recording", starter)
    state = {camera_number: {}}
    monkeypatch.setattr(main, "camera_process_state", state)
    monkeypatch.setattr(main, "camera_reconnect_counts", {camera_number: 0})
    ffmpeg_processes = []
    monkeypatch.setattr(main, "ffmpeg_processes", ffmpeg_processes)

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(camera_number, "recording"))
        for _ in range(40):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    # starter() (start_recording) called a second time -- restart-on-exit
    # behavior preserved.
    assert len(call_log) >= 2, "recording process should have been restarted after exiting"

    # Bookkeeping: the FIRST (exited) process was removed from
    # ffmpeg_processes; if a second process is currently running, it was
    # added.
    assert first_process not in ffmpeg_processes, (
        "exited process should have been removed from ffmpeg_processes bookkeeping"
    )


# ---------------------------------------------------------------------------
# Live-mode watchdog behavior is completely untouched by this change.
# ---------------------------------------------------------------------------

def test_live_mode_watchdog_path_unaffected(monkeypatch):
    """Sanity check that this fix is genuinely isolated to the
    recording-mode branch: live mode's own polling/watchdog loop (the
    `if mode == "live":` branch) still runs exactly as before -- the new
    code only lives in the sibling `else:` branch."""
    camera_number = 13
    process = _FakeRecordingProcess(alive_polls=2, exit_code=0)
    monkeypatch.setattr(main, "start_live_stream", lambda cam: process)
    state = {camera_number: _RecordingStateDict()}
    monkeypatch.setattr(main, "camera_process_state", state)
    monkeypatch.setattr(main, "camera_reconnect_counts", {camera_number: 0})
    monkeypatch.setattr(main, "ffmpeg_processes", [])
    monkeypatch.setattr(
        main, "_drain_camera_stderr", lambda cam, proc, buf: asyncio.sleep(0)
    )

    async def scenario():
        task = asyncio.create_task(main.process_supervisor(camera_number, "live"))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert ("live", "retrying") in state[camera_number].history, (
        f"expected a 'retrying' state transition after exit; history={state[camera_number].history}"
    )
    assert process.wait_calls, "live mode should still detect exit and reap the process"
