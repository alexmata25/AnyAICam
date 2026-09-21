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


def test_clip_build_exception_is_logged_not_silently_swallowed(
    monkeypatch, tmp_path, capsys, background_loop
):
    """This is the test that would have caught the real, live defect
    found in production on 2026-09-13: build_motion_event_clip()
    raising was previously completely unguarded. The coroutine is
    scheduled via asyncio.run_coroutine_threadsafe() (see the big
    comment at its call site in main.py), and nothing ever retrieves
    the resulting concurrent.futures.Future's result/exception --
    unlike asyncio.create_task(), whose Task at least logs "exception
    was never retrieved" when garbage-collected, an unretrieved Future
    here logs nothing at all. A real Camera 1 detection produced a
    thumbnail and a correctly-tagged local record (event_clip pointed
    at the expected path) but the clip file itself never appeared,
    with zero trace anywhere in the logs -- exactly the silent-failure
    shape this test reproduces and now requires to be logged."""
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        raise RuntimeError("ffmpeg exited with code 1")

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    result_holder = {}

    def worker():
        result_holder["events"] = main.save_yolo_events(165, _fake_result("person"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert "events" in result_holder and len(result_holder["events"]) == 1, \
        "a clip-build failure must not break analytics event creation itself"
    event_id = result_holder["events"][0]["id"]

    captured_text = ""

    def _clip_failure_logged():
        nonlocal captured_text
        captured_text += capsys.readouterr().out
        return "clip build failed" in captured_text

    assert _wait_until(_clip_failure_logged, timeout=5), (
        "expected the clip-build exception to be logged, not silently "
        f"swallowed; captured stdout so far: {captured_text!r}"
    )
    assert event_id in captured_text, "diagnostic log must include the event id"
    assert "165" in captured_text, "diagnostic log must include the camera number"
    assert "RuntimeError" in captured_text, "diagnostic log must include the exception type"
    assert "ffmpeg exited with code 1" in captured_text, "diagnostic log must include the exception message"


def test_ai_person_detector_captures_the_loop_exactly_once(monkeypatch):
    monkeypatch.setattr(main, "_ai_event_media_loop", None)
    # This is a loop-capture unit test, not a startup-stagger test.  Its
    # synthetic high camera number would otherwise sleep for an arbitrary
    # production stagger before reaching the intentionally unavailable
    # detector branch below.
    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0)
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


# ----------------------------------------- 2026-09-20: Event-mode recording persistence


def test_ai_classified_detection_also_persists_an_event_recording_when_camera_is_in_event_mode(
    monkeypatch, tmp_path, fake_uploader, background_loop
):
    """Smart Motion/person/vehicle-triggered events must behave
    consistently with basic motion for a camera in Event mode: this is
    the same persist_event_recording() call store_motion_event()'s own
    basic-motion path already schedules, now also reachable from the
    AI/YOLO classification path."""
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"mode": "event"})

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    persisted = []

    async def fake_persist_event_recording(camera_number, start, end, *, detector=None, trigger_id=None):
        persisted.append((camera_number, start, end, detector, trigger_id))

    monkeypatch.setattr(main, "persist_event_recording", fake_persist_event_recording)

    main.save_yolo_events(170, _fake_result("car"))

    assert _wait_until(lambda: len(persisted) == 1), \
        "persist_event_recording() must actually be scheduled and run on the background loop"
    assert persisted[0][0] == 170
    # 2026-09-21 observability fix: the AI/YOLO path must identify itself
    # as such, distinctly from the basic-motion path's "basic_motion".
    assert persisted[0][3] == "ai_detection"
    assert persisted[0][4] is not None


def test_ai_classified_detection_does_not_persist_a_recording_for_a_continuous_mode_camera(
    monkeypatch, tmp_path, fake_uploader, background_loop
):
    """The exact opposite case -- a Continuous-mode camera's AI-event
    path must schedule zero new work beyond the existing clip build/
    upload, matching this same guarantee on the basic motion path."""
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"mode": "continuous"})

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    persisted = []

    async def fake_persist_event_recording(camera_number, start, end, *, detector=None, trigger_id=None):
        persisted.append((camera_number, start, end))

    monkeypatch.setattr(main, "persist_event_recording", fake_persist_event_recording)

    main.save_yolo_events(171, _fake_result("car"))

    assert _wait_until(lambda: len(fake_uploader) == 1), "the existing clip/upload path must still run"
    assert persisted == [], "a Continuous-mode camera must never get an Event-mode recording persisted"


def test_ai_detection_dedup_does_not_suppress_persist_event_recording_during_sustained_activity(
    monkeypatch, tmp_path, fake_uploader, background_loop
):
    """Codex review item, confirmed by code inspection (2026-09-22):
    persist_event_recording() scheduling used to sit INSIDE `if not
    is_duplicate:` -- the same per-camera dedup gate that (correctly)
    skips building a second, near-identical Hybrid clip for a burst of
    rapidly repeated detections. During sustained activity (a person
    lingering in frame, producing several qualifying scans a few seconds
    apart), every scan after the first was should_merge()-classified as a
    duplicate and its persist_event_recording() call was dropped entirely
    -- even though that function has its own independent, correct merge/
    extend logic that a suppressed call never gets the chance to run. Net
    effect: a genuinely multi-minute event's LOCAL recording was silently
    truncated to just the first scan's own pre-roll+post-roll window.

    Two real, back-to-back save_yolo_events() calls (close enough in wall-
    clock time that the second is a genuine should_merge() duplicate for
    the Hybrid path) must still each schedule persist_event_recording() --
    that function's own should_start_new_event_recording() is what decides
    extend-vs-new, not this dedup gate."""
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"mode": "event"})

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    persisted = []

    async def fake_persist_event_recording(camera_number, start, end, *, detector=None, trigger_id=None):
        persisted.append((camera_number, detector, trigger_id))

    monkeypatch.setattr(main, "persist_event_recording", fake_persist_event_recording)

    main.save_yolo_events(172, _fake_result("car"))
    main.save_yolo_events(172, _fake_result("car"))  # same burst -- a Hybrid dedup duplicate

    assert _wait_until(lambda: len(persisted) == 2), (
        "both scans in the same burst must schedule persist_event_recording() "
        "-- the Hybrid dedup gate must not suppress the second call"
    )
    assert _wait_until(lambda: len(fake_uploader) == 1), (
        "the Hybrid clip/upload path itself must still be deduplicated -- "
        "only the FIRST scan builds/uploads a clip"
    )
    assert persisted[0][0] == persisted[1][0] == 172
    # Distinct trigger ids: persist_event_recording()'s own merge logic
    # (not this dedup) is what should decide whether the second call
    # extends the first recording or starts a new one.
    assert persisted[0][2] != persisted[1][2]
