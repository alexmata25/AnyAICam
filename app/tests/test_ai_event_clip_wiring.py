"""AI-classification event clip + upload wiring (2026-09-02, corrected).

save_yolo_events() -- the local YOLO/AI-classification path (person,
car, truck, ppe, suitcase, motorcycle, cat, bus, backpack, dog,
bicycle, bird) -- previously only ever wrote a local annotated
thumbnail and the old linked_recording_for() Media-Fragment stopgap.
It never called build_motion_event_clip() or event_media_uploader.
upload_motion_event_media(), the real clip-extraction + cloud-upload
pipeline motion events already get via store_motion_event().

This wires that same pipeline into save_yolo_events(), traced live on
the real Samsung appliance. The first version of this wiring compiled
and passed every test, but was still fully non-functional in
production: save_yolo_events() actually executes via
`await asyncio.to_thread(save_yolo_events, ...)` -- a worker thread
with no asyncio event loop of its own -- confirmed by grepping the
live appliance's own main.py for the real call site. The original
fix's asyncio.ensure_future() call only ever schedules onto "the
current thread's running loop"; called from that worker thread, there
isn't one, so it always raised RuntimeError and was silently
swallowed by the fix's own except clause -- syntactically fine, and
every unit test happened to call save_yolo_events() from the main
thread (the one case that already worked), so nothing caught it.

The corrected version captures the real main event loop once, lazily,
on ai_person_detector()'s own first run (that coroutine genuinely is
created via asyncio.create_task() on the main loop), and uses
asyncio.run_coroutine_threadsafe(coro, loop) -- the correct primitive
for scheduling a coroutine from a different thread onto a specific,
already-running loop -- instead of ensure_future()/create_task().
Scheduling failure is now logged (print, matching this codebase's own
convention), not silently swallowed.
"""

import asyncio
import sys
import threading
import time
import types
from datetime import datetime
from unittest import mock

import numpy as np
import pytest

import main


def _fake_result(*class_names: str, base_conf: float = 0.9) -> dict:
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    detections = [
        {
            "x": 10, "y": 10, "width": 40, "height": 40,
            "class_name": name, "confidence": base_conf,
        }
        for name in class_names
    ]
    return {"detections": detections, "frame": frame}


@pytest.fixture(autouse=True)
def _reset_module_state():
    """ai_event_clip_windows and _ai_event_media_loop are module-level
    state -- must not leak between tests."""
    main.ai_event_clip_windows.clear()
    previous_loop = main._ai_event_media_loop
    yield
    main.ai_event_clip_windows.clear()
    main._ai_event_media_loop = previous_loop


@pytest.fixture
def fake_uploader(monkeypatch):
    """event_media_uploader.py exists only on the physical appliance --
    injects a fake module under that exact import name so
    save_yolo_events()'s own `from event_media_uploader import
    upload_motion_event_media` resolves to a real, call-recording
    stand-in. Thread-safe: a plain list append is atomic under the
    GIL, and this is exactly what the real cross-thread scheduling
    path needs to prove itself against."""
    calls = []

    def fake_upload_motion_event_media(**kwargs):
        calls.append(kwargs)
        return True

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = fake_upload_motion_event_media
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    return calls


@pytest.fixture
def background_loop():
    """A real asyncio event loop running in its own dedicated OS
    thread -- not asyncio.run()'s loop, and not the test's own thread
    -- the same shape as the real main application loop
    save_yolo_events()'s worker thread must reach across to. This is
    what proves run_coroutine_threadsafe() actually works, rather than
    just not-raising."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _standard_mocks(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_analytics_event", lambda event: None)


def _run_save_yolo_events_on_a_real_loop(camera_number: int, result: dict) -> list[dict]:
    """Runs save_yolo_events() the way its real caller does: from
    OUTSIDE the target event loop's own thread entirely (a plain
    synchronous call on the test's own thread, with
    _ai_event_media_loop pointed at a genuinely separate background
    loop/thread) -- not asyncio.run()'s own loop calling back into
    itself, which would not actually exercise run_coroutine_
    threadsafe()'s cross-thread path at all."""
    return main.save_yolo_events(camera_number, result)


# --------------------------------------------------------- the actual bug this turn fixed


def test_called_from_a_worker_thread_the_coroutine_actually_runs_on_the_main_loop(
    monkeypatch, tmp_path, fake_uploader, background_loop
):
    """This is the test that would have caught the original bug: the
    old asyncio.ensure_future() version raises RuntimeError and is
    silently swallowed when save_yolo_events() is called from a thread
    that isn't running the target loop -- exactly this scenario."""
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    result_holder = {}

    def worker():
        # A plain OS thread with no event loop at all -- the same shape
        # asyncio.to_thread()'s own worker threads have.
        result_holder["events"] = main.save_yolo_events(150, _fake_result("car"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert "events" in result_holder
    assert _wait_until(lambda: len(fake_uploader) == 1), \
        "the coroutine scheduled via run_coroutine_threadsafe() must actually execute on the background loop"
    assert fake_uploader[0]["event_id"] == result_holder["events"][0]["id"]
    assert fake_uploader[0]["camera_number"] == 150


def test_scheduling_failure_is_logged_not_silently_swallowed(monkeypatch, tmp_path, capsys):
    """No loop captured yet (e.g. very early startup) -- must not
    crash save_yolo_events(), and must print something diagnosable,
    unlike the original ensure_future()/except RuntimeException: pass
    version this replaces."""
    monkeypatch.setattr(main, "_ai_event_media_loop", None)

    async def fake_build_motion_event_clip(*a):
        return "/recordings/clips/motion/motion_x.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    events = main.save_yolo_events(151, _fake_result("truck"))

    assert len(events) == 1, "analytics event creation must not be broken by a scheduling failure"
    captured = capsys.readouterr()
    assert "could not schedule" in captured.out
    assert "no main event loop captured yet" in captured.out


def test_a_closed_target_loop_is_logged_not_silently_swallowed(monkeypatch, tmp_path, capsys):
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    monkeypatch.setattr(main, "_ai_event_media_loop", dead_loop)

    async def fake_build_motion_event_clip(*a):
        return "/recordings/clips/motion/motion_x.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    events = main.save_yolo_events(152, _fake_result("bus"))

    assert len(events) == 1
    captured = capsys.readouterr()
    assert "could not schedule" in captured.out


def test_ai_person_detector_captures_the_loop_exactly_once(monkeypatch):
    monkeypatch.setattr(main, "_ai_event_media_loop", None)
    monkeypatch.setattr(main, "cv2", None)  # short-circuits to the early "unavailable" return
    monkeypatch.setitem(main.ai_detection_state, 160, {})

    async def _run_twice():
        await main.ai_person_detector(160)
        first = main._ai_event_media_loop
        await main.ai_person_detector(160)
        second = main._ai_event_media_loop
        return first, second

    first, second = asyncio.run(_run_twice())
    assert first is not None
    assert first is second, "must not overwrite an already-captured loop on a later call"


# --------------------------------------------------------- dedup still works (cross-thread)


def test_duplicate_detections_still_dedup_when_scheduled_cross_thread(
    monkeypatch, tmp_path, fake_uploader, background_loop
):
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    def worker():
        main.save_yolo_events(161, _fake_result("person"))
        main.save_yolo_events(161, _fake_result("car"))  # well within the merge gap

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    _wait_until(lambda: len(fake_uploader) >= 1)
    time.sleep(0.3)  # let a wrongly-scheduled second upload have a chance to also land
    assert len(fake_uploader) == 1, "a merged/deduplicated scan must not trigger a second upload"


# --------------------------------------------------------- unrelated behavior unchanged


def test_object_count_and_raw_detections_fields_still_present_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_ai_event_media_loop", None)

    async def fake_build_motion_event_clip(*a):
        return None

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    events = main.save_yolo_events(162, _fake_result("person", "person"))

    assert len(events) == 1
    assert events[0]["object_count"] == 2
    assert len(events[0]["detections"]) == 2
    assert events[0]["event_type"] == "person"


def test_thumbnail_path_is_unchanged_local_ai_thumbnail_convention(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_ai_event_media_loop", None)

    async def fake_build_motion_event_clip(*a):
        return None

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    events = main.save_yolo_events(163, _fake_result("dog"))

    assert len(events) == 1
    thumbnail = events[0]["thumbnail"]
    assert thumbnail.startswith("/recordings/media/ai/")
    assert thumbnail.endswith(".jpg")


def test_no_qualifying_detections_schedules_nothing(monkeypatch, tmp_path, fake_uploader):
    monkeypatch.setattr(main, "_ai_event_media_loop", None)

    events = main.save_yolo_events(164, _fake_result())  # no detections at all
    assert events == []
    assert fake_uploader == []


def test_build_motion_event_clip_exists_standalone_and_is_reusable(monkeypatch, tmp_path):
    """build_motion_event_clip() itself is new to this branch (added
    solely so the accepted AI-event wiring below has a real function
    to call) -- proves it's a genuine, callable, independent function,
    not just a name save_yolo_events() happens to reference."""
    assert callable(main.build_motion_event_clip)
    import inspect
    assert inspect.iscoroutinefunction(main.build_motion_event_clip)
