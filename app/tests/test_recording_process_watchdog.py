"""Recording-mode-only stall watchdog: tests for _newest_recording_file(),
_recording_progress_watchdog(), and their integration into
process_supervisor(). Built in response to a real, diagnosed failure on
the pilot appliance: an RTSP session can stay TCP ESTABLISHED while
delivering zero bytes, and ffmpeg has no read timeout on that input, so
the existing exit-triggered supervisor recovery never fires. The
watchdog detects "current recording segment file stopped growing" and
kills the process; every line of the existing retry/restart logic then
runs completely unmodified.

Imports `main` -- per this project's own already-documented constraint
(main.py hardcodes /app/... paths at import time), this file can only
run inside the deployed container, not this WSL host's plain
python3/pytest. Every test explicitly redirects to a throwaway sqlite
file via override_target() before main is imported, so nothing here
ever touches the real production database, even though none of these
tests actually query it.

All timing constants are monkeypatched to sub-second values so the
whole suite runs in well under a second while still exercising the
real relationship between check interval, grace period, and threshold
-- not a mocked-out version of that logic.
"""

import asyncio
import subprocess
import sys

import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_recording_process_watchdog.db"):
    import main


CAMERA_NUMBER = 9001  # not a real camera_number (1..CAMERA_COUNT) -- avoids any collision


@pytest.fixture
def anyio_backend():
    # This container has anyio (a FastAPI/starlette transitive
    # dependency), not pytest-asyncio -- @pytest.mark.anyio is the
    # marker that plugin actually provides.
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "RECORDING_PROGRESS_CHECK_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(main, "RECORDING_STALL_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(main, "RECORDING_SEGMENT_SECONDS", 0.02)
    main.camera_process_state[CAMERA_NUMBER] = {}
    main.camera_reconnect_counts[CAMERA_NUMBER] = 0
    yield tmp_path
    main.camera_process_state.pop(CAMERA_NUMBER, None)
    main.camera_reconnect_counts.pop(CAMERA_NUMBER, None)


def _sleeper_process() -> subprocess.Popen:
    """A real, controllable process standing in for ffmpeg -- alive
    until killed or it naturally exits, exactly like the real thing
    from process_supervisor's point of view."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _write_segment(folder, name: str, data: bytes = b"x"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------ _newest_recording_file


def test_newest_recording_file_returns_lexicographically_last_match(tmp_path):
    folder = tmp_path / f"camera{CAMERA_NUMBER}"
    _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv")
    newest = _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-02_00-00-00.mkv")
    _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_12-00-00.mkv")

    assert main._newest_recording_file(CAMERA_NUMBER) == newest


def test_newest_recording_file_returns_none_when_folder_missing(tmp_path):
    assert main._newest_recording_file(CAMERA_NUMBER) is None


def test_newest_recording_file_ignores_a_different_cameras_files(tmp_path):
    other_folder = tmp_path / "camera9002"
    _write_segment(other_folder, "camera9002_2026-01-01_00-00-00.mkv")

    assert main._newest_recording_file(CAMERA_NUMBER) is None


# --------------------------------------------------- _recording_progress_watchdog


@pytest.mark.anyio
async def test_healthy_growing_recording_is_never_killed(tmp_path):
    folder = tmp_path / f"camera{CAMERA_NUMBER}"
    path = _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv")
    process = _sleeper_process()
    try:
        async def _grow():
            for _ in range(30):
                await asyncio.sleep(0.01)
                with open(path, "ab") as handle:
                    handle.write(b"x")

        grower = asyncio.create_task(_grow())
        watchdog = asyncio.create_task(main._recording_progress_watchdog(CAMERA_NUMBER, process))
        await grower
        await asyncio.sleep(0.05)  # let the watchdog observe the final size at least once more
        watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog

        assert process.poll() is None  # never killed
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.anyio
async def test_stalled_recording_is_not_killed_before_threshold(tmp_path):
    folder = tmp_path / f"camera{CAMERA_NUMBER}"
    _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv")  # never updated
    process = _sleeper_process()
    try:
        watchdog = asyncio.create_task(main._recording_progress_watchdog(CAMERA_NUMBER, process))
        await asyncio.sleep(0.01)  # well under the ~0.04s stall threshold
        assert process.poll() is None  # not killed yet
        watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.anyio
async def test_stalled_recording_is_killed_after_threshold(tmp_path):
    folder = tmp_path / f"camera{CAMERA_NUMBER}"
    _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv")  # never updated
    process = _sleeper_process()
    try:
        await main._recording_progress_watchdog(CAMERA_NUMBER, process)  # awaited directly, returns once it kills
        await asyncio.to_thread(process.wait, 2)  # kill() is async at the OS level; wait for the exit to land
        assert process.poll() is not None  # confirmed killed
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.anyio
async def test_watchdog_never_touches_or_alters_the_recording_file(tmp_path):
    folder = tmp_path / f"camera{CAMERA_NUMBER}"
    path = _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv", data=b"original-bytes")
    original_bytes = path.read_bytes()
    process = _sleeper_process()
    try:
        await main._recording_progress_watchdog(CAMERA_NUMBER, process)
        assert path.exists()
        assert path.read_bytes() == original_bytes  # never moved, renamed, or altered
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.anyio
async def test_watchdog_never_modifies_the_persisted_recording_cutoff(tmp_path):
    """The watchdog lives entirely in main.py and never imports or
    references recording_uploader.py's CUTOFF_FILE -- this is a
    regression guard, not just a design claim: a real cutoff file is
    placed in a completely separate directory the watchdog has no
    reason to ever touch, and its bytes/mtime are confirmed unchanged
    after a full kill cycle."""
    cutoff_dir = tmp_path / "state"
    cutoff_dir.mkdir()
    cutoff_file = cutoff_dir / "recording_upload_cutoff.json"
    cutoff_file.write_text('{"cutoff": "2026-08-21T06:35:58.709000"}')
    original_mtime = cutoff_file.stat().st_mtime
    original_bytes = cutoff_file.read_bytes()

    folder = tmp_path / f"camera{CAMERA_NUMBER}"
    _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv")
    process = _sleeper_process()
    try:
        await main._recording_progress_watchdog(CAMERA_NUMBER, process)
        assert cutoff_file.read_bytes() == original_bytes
        assert cutoff_file.stat().st_mtime == original_mtime
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.anyio
async def test_watchdog_returns_cleanly_if_process_already_exited(tmp_path):
    """No file to observe, process already dead -- the watchdog's own
    `while process.poll() is None` loop condition should simply never
    enter, returning immediately without attempting to kill an
    already-dead process or raising."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert process.poll() is not None

    await main._recording_progress_watchdog(CAMERA_NUMBER, process)  # must not raise or hang


# ------------------------------------------------- process_supervisor integration


@pytest.mark.anyio
async def test_live_mode_never_gets_a_watchdog(tmp_path, monkeypatch):
    calls = []

    async def _spy(camera_number, process):
        calls.append(camera_number)
        return  # immediately returns, as if the process already exited

    monkeypatch.setattr(main, "_recording_progress_watchdog", _spy)
    dummy = _sleeper_process()
    monkeypatch.setattr(main, "start_live_stream", lambda n: dummy)

    task = asyncio.create_task(main.process_supervisor(CAMERA_NUMBER, "live"))
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if main.camera_process_state[CAMERA_NUMBER].get("live") == "running":
                break
        assert main.camera_process_state[CAMERA_NUMBER].get("live") == "running"
        assert calls == []  # never called for live mode
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if dummy.poll() is None:
            dummy.kill()
            dummy.wait()
        try:
            main.ffmpeg_processes.remove(dummy)
        except ValueError:
            pass


@pytest.mark.anyio
async def test_recording_mode_gets_a_watchdog(tmp_path, monkeypatch):
    calls = []

    async def _spy(camera_number, process):
        calls.append(camera_number)
        await asyncio.sleep(10)  # never returns on its own within the test window

    monkeypatch.setattr(main, "_recording_progress_watchdog", _spy)
    dummy = _sleeper_process()
    monkeypatch.setattr(main, "start_recording", lambda n: dummy)

    task = asyncio.create_task(main.process_supervisor(CAMERA_NUMBER, "recording"))
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            if calls:
                break
        assert calls == [CAMERA_NUMBER]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if dummy.poll() is None:
            dummy.kill()
            dummy.wait()
        try:
            main.ffmpeg_processes.remove(dummy)
        except ValueError:
            pass


@pytest.mark.anyio
async def test_existing_supervisor_restart_path_runs_after_watchdog_kill(tmp_path, monkeypatch):
    """The real end-to-end proof: a genuinely stalled recording, run
    through the actual process_supervisor() + real
    _recording_progress_watchdog() (nothing mocked), reaches the
    existing "retrying" state -- proving the existing exit-triggered
    recovery path is what handles it, not new recovery logic."""
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    folder = tmp_path / f"camera{CAMERA_NUMBER}"
    _write_segment(folder, f"camera{CAMERA_NUMBER}_2026-01-01_00-00-00.mkv")  # never updated -> stalled

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
        assert reached_retrying, "existing supervisor retry path never triggered after the watchdog kill"
        assert dummy.poll() is not None  # confirms it was actually killed, not exited on its own
        assert main.camera_reconnect_counts[CAMERA_NUMBER] >= 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if dummy.poll() is None:
            dummy.kill()
            dummy.wait()
        try:
            main.ffmpeg_processes.remove(dummy)
        except ValueError:
            pass
