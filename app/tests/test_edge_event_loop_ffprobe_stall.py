"""Edge web-server stall / restart loop (2026-09-24, confirmed live on Ryzen).

linked_recording_for() used to ffprobe EVERY recording on a camera, newest
first, until one covered the event time. When none did -- routine on an
Event-mode camera, whose clip is only written after the detection -- that
was the camera's whole history (thousands of files at ~0.8s per probe,
serialized by the single appliance-wide _ffprobe_semaphore). The in-memory
duration cache is empty after every restart, and
_backfill_ai_event_linked_recording() / persist_event_recording() made those
calls directly on the asyncio event loop, so the whole VMS web server
(/health included) froze for minutes, Docker marked it unhealthy, it was
restarted, the cache was cold again, and the loop repeated.
"""
import ast
import asyncio
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import main

EVENT_TIME = datetime(2026, 9, 24, 12, 0, 0)


def _make_recordings(folder: Path, camera_number: int, starts: list[datetime]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for start in starts:
        (folder / f"camera{camera_number}_{start:%Y-%m-%d_%H-%M-%S}.mkv").write_bytes(b"")


@pytest.fixture()
def recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path)
    probes: list[str] = []
    durations: dict[str, float] = {}

    def fake_probe(path: Path):
        probes.append(path.name)
        return durations.get(path.name, 10.0)

    monkeypatch.setattr(main, "_probe_recording_duration_seconds", fake_probe)
    return tmp_path, probes, durations


def test_an_uncovered_event_probes_only_the_files_that_could_cover_it(recordings):
    """The live failure: two days of 5-minute segments (576 files), none
    covering the event. The old loop probed all 576."""
    root, probes, _durations = recordings
    starts = [EVENT_TIME - timedelta(minutes=5 * i) - timedelta(seconds=30) for i in range(576)]
    starts += [EVENT_TIME + timedelta(minutes=5 * i) for i in range(1, 50)]  # later files never need a probe
    _make_recordings(root / "camera1", 1, starts)

    assert main.linked_recording_for(1, EVENT_TIME) is None  # every file here is 10s long -- nothing covers it
    lookback_files = main.LINKED_RECORDING_MAX_LOOKBACK_SECONDS // 300 + 1
    assert 0 < len(probes) <= lookback_files
    assert all(name < f"camera1_{EVENT_TIME:%Y-%m-%d_%H-%M-%S}" for name in probes)


def test_the_covering_recording_is_still_found(recordings):
    root, probes, durations = recordings
    starts = [EVENT_TIME - timedelta(minutes=5 * i) - timedelta(seconds=30) for i in range(40)]
    _make_recordings(root / "camera2", 2, starts)
    covering = f"camera2_{starts[0]:%Y-%m-%d_%H-%M-%S}.mkv"
    durations[covering] = 300.0

    link = main.linked_recording_for(2, EVENT_TIME)
    assert link is not None and link.startswith(f"/recordings/camera2/{covering}#t=")
    assert probes == [covering]


def test_the_backfill_never_blocks_the_event_loop(monkeypatch):
    """A slow linked_recording_for() (the ffprobe scan) must not stop
    other coroutines -- i.e. the web server -- from running."""
    released = threading.Event()

    def slow_linked_recording_for(*_args, **_kwargs):
        released.wait(1.0)
        return "/recordings/camera1/x.mkv#t=0,1"

    real_asyncio = asyncio

    class FastSleepAsyncio:
        def __getattr__(self, name):
            return getattr(real_asyncio, name)

        @staticmethod
        async def sleep(_seconds):
            await real_asyncio.sleep(0)

    monkeypatch.setattr(main, "linked_recording_for", slow_linked_recording_for)
    monkeypatch.setattr(main, "_patch_analytics_events_linked_recording", lambda *_args: None)
    monkeypatch.setattr(main, "_local_recording_settings", lambda _camera: {"pre_roll_seconds": 5, "post_roll_seconds": 5})
    monkeypatch.setattr(main, "asyncio", FastSleepAsyncio())

    async def scenario():
        gaps = []

        async def heartbeat():
            last = time.monotonic()
            while not released.is_set():
                await real_asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        beat = real_asyncio.create_task(heartbeat())
        await main._backfill_ai_event_linked_recording(1, ["evt"], EVENT_TIME)
        released.set()
        await beat
        return max(gaps)

    assert asyncio.run(scenario()) < 0.5


FFPROBE_WORK = {"linked_recording_for", "_probe_recording_duration_seconds", "_probe_motion_clip_candidates", "_catalog_local_recordings_for_camera"}


def _async_functions_reaching(targets: set[str]) -> list[str]:
    """async defs in main.py that call, directly on the event loop, any
    sync function which (transitively) runs `targets`. A function passed
    to asyncio.to_thread()/run_in_executor() is an ast.Name argument, not
    an ast.Call, so offloaded work is never reported."""
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)

    def direct_callees(node):
        return {call.func.id for call in ast.walk(node) if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}

    reaching = set(targets)
    grew = True
    while grew:
        grew = False
        for name, node in functions.items():
            if isinstance(node, ast.FunctionDef) and name not in reaching and direct_callees(node) & reaching:
                reaching.add(name)
                grew = True
    return [
        f"{name}:{call.lineno} -> {call.func.id}"
        for name, node in functions.items() if isinstance(node, ast.AsyncFunctionDef)
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in reaching
    ]


def test_no_async_function_runs_ffprobe_work_on_the_event_loop():
    """people_counting_worker() (every line crossing on a busy counting
    camera), _backfill_ai_event_linked_recording() and
    persist_event_recording() all did -- directly or via a helper."""
    assert _async_functions_reaching(FFPROBE_WORK) == []
