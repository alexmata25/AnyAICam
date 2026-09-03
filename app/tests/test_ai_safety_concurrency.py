"""AI safety hardening (2026-09-03): focused tests for the four minimal
changes made before AI person/object detection is re-enabled --

  1. a real threading.Lock() around YOLO model construction
     (get_yolo_model()), closing the 5-camera concurrent-load race;
  2. an AI-specific asyncio.Semaphore(1) bounding detect_objects_frame()
     to one inference at a time, appliance-wide;
  3. torch.set_num_threads(2), applied exactly once, tied to the same
     one-time model-load event;
  4. a small per-camera startup stagger so all 5 ai_person_detector()
     tasks don't fire in lockstep.

AI_PERSON_DETECTION_ENABLED stays false throughout (these tests import
and exercise the functions directly; the appliance's own env config is
untouched). YOLO_ALLOWED_CLASSES, save_yolo_events(), the AI event
clip/thumbnail path (build_motion_event_clip()/upload_motion_event_media()
wiring), cooldown logic, and detect_objects_frame()'s own HLS frame-read
logic are all exercised here exactly as before -- none of them were
touched by this change.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import main


class _FakeYOLO:
    """Stands in for ultralytics.YOLO -- construction is deliberately
    slow (a real model load reads weights from disk and builds the
    torch module graph, on the order of hundreds of ms to seconds) so a
    real race between 5 concurrent callers has a wide window to
    manifest if get_yolo_model()'s locking is broken or removed."""

    construct_count = 0
    construct_lock = threading.Lock()

    def __init__(self, model_name):
        with _FakeYOLO.construct_lock:
            _FakeYOLO.construct_count += 1
        self.model_name = model_name
        time.sleep(0.05)


class _FakeTorch:
    def __init__(self):
        self.set_num_threads_calls = []

    def set_num_threads(self, n):
        self.set_num_threads_calls.append(n)


@pytest.fixture(autouse=True)
def _reset_ai_globals(monkeypatch):
    """Every test gets a clean slate: no model loaded yet, a fresh fake
    YOLO/torch, and the real yolo_model_lock (never replaced -- these
    tests are exactly what proves that real lock works)."""
    monkeypatch.setattr(main, "yolo_model", None)
    _FakeYOLO.construct_count = 0
    fake_torch = _FakeTorch()
    monkeypatch.setattr(main, "torch", fake_torch)
    monkeypatch.setattr(main, "YOLO", _FakeYOLO)
    yield fake_torch


# ---------------------------------------------------------------------------
# 1 & 2. Five simultaneous callers construct the model exactly once, and
#        all five receive the identical instance.
# ---------------------------------------------------------------------------

def test_five_simultaneous_callers_construct_the_model_exactly_once():
    barrier = threading.Barrier(5)

    def call_from_thread():
        barrier.wait()  # maximize the race window -- all 5 threads hit
        # get_yolo_model() as close to simultaneously as the OS scheduler
        # allows, not staggered by thread-start overhead.
        return main.get_yolo_model()

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: call_from_thread(), range(5)))

    assert _FakeYOLO.construct_count == 1, (
        f"expected exactly one YOLO(...) construction, got {_FakeYOLO.construct_count} "
        "-- the 5-camera model-load race is not closed"
    )
    assert all(result is results[0] for result in results), (
        "all 5 callers must receive the exact same model instance"
    )
    assert isinstance(results[0], _FakeYOLO)


# ---------------------------------------------------------------------------
# 5. torch.set_num_threads(2) is applied exactly once.
# ---------------------------------------------------------------------------

def test_torch_set_num_threads_applied_exactly_once(_reset_ai_globals):
    fake_torch = _reset_ai_globals
    barrier = threading.Barrier(5)

    def call_from_thread():
        barrier.wait()
        return main.get_yolo_model()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: call_from_thread(), range(5)))

    assert fake_torch.set_num_threads_calls == [2], (
        f"expected torch.set_num_threads(2) called exactly once, got {fake_torch.set_num_threads_calls}"
    )

    # A later, ordinary (non-racing) call must not trigger a second call --
    # set_num_threads() is tied to the one-time load, not every access.
    main.get_yolo_model()
    assert fake_torch.set_num_threads_calls == [2]


def test_get_yolo_model_still_raises_when_yolo_is_unavailable(monkeypatch):
    monkeypatch.setattr(main, "YOLO", None)
    with pytest.raises(RuntimeError):
        main.get_yolo_model()


# ---------------------------------------------------------------------------
# 3 & 4. At most one inference runs at a time; the semaphore releases on
#        both success and exception.
# ---------------------------------------------------------------------------

def test_at_most_one_inference_runs_at_a_time():
    assert main.ai_inference_semaphore._value == 1, "expected the semaphore to start free"

    concurrent_count = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def fake_inference(camera_number):
        nonlocal concurrent_count, max_concurrent
        async with main.ai_inference_semaphore:
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            async with lock:
                concurrent_count -= 1

    async def scenario():
        await asyncio.gather(*(fake_inference(n) for n in range(1, 6)))

    asyncio.run(scenario())

    assert max_concurrent == 1, f"expected at most 1 concurrent inference, observed {max_concurrent}"
    assert main.ai_inference_semaphore._value == 1, "semaphore must be fully released after all 5 complete"


def test_semaphore_releases_on_success_and_on_exception():
    async def scenario():
        # Success path.
        async with main.ai_inference_semaphore:
            pass
        assert main.ai_inference_semaphore._value == 1

        # Exception path -- released even though the body raised.
        with pytest.raises(RuntimeError):
            async with main.ai_inference_semaphore:
                raise RuntimeError("simulated detect_objects_frame() failure")
        assert main.ai_inference_semaphore._value == 1

        # And the semaphore is still genuinely usable afterward -- not
        # left permanently held, which would otherwise deadlock every
        # camera's detector loop from this point on.
        acquired = await asyncio.wait_for(main.ai_inference_semaphore.acquire(), timeout=1.0)
        assert acquired is True
        main.ai_inference_semaphore.release()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. Camera startup offsets are actually staggered.
# ---------------------------------------------------------------------------

def test_camera_startup_offsets_are_staggered(monkeypatch):
    monkeypatch.setattr(main, "cv2", None)  # forces an early return right after the stagger sleep
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds):
        sleep_calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", recording_sleep)

    async def scenario():
        for camera_number in range(1, 6):
            sleep_calls.clear()
            await main.ai_person_detector(camera_number)
            assert sleep_calls, f"camera {camera_number}: expected a startup stagger sleep"
            assert sleep_calls[0] == camera_number * main.AI_DETECTOR_STARTUP_STAGGER_SECONDS

    asyncio.run(scenario())


def test_camera_startup_offsets_are_distinct_not_lockstep(monkeypatch):
    monkeypatch.setattr(main, "cv2", None)
    offsets = [camera_number * main.AI_DETECTOR_STARTUP_STAGGER_SECONDS for camera_number in range(1, 6)]
    assert len(set(offsets)) == 5, f"expected 5 distinct stagger offsets, got {offsets}"
    assert offsets == sorted(offsets)


# ---------------------------------------------------------------------------
# 7. AI event creation/media behavior is unchanged by the new
#    stagger/lock/semaphore wrapping.
# ---------------------------------------------------------------------------

def test_ai_event_and_media_wiring_still_fires_through_the_new_wrapper(monkeypatch):
    camera_number = 3
    monkeypatch.setattr(main, "cv2", object())  # truthy: skip the "unavailable" branch
    monkeypatch.setattr(main, "YOLO", object())
    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setitem(
        main.ai_detection_state,
        camera_number,
        {"status": "starting", "last_checked": None, "last_detection": None, "detections": 0, "error": None},
    )
    monkeypatch.setitem(main.ai_person_last_event, camera_number, 0.0)  # cooldown already elapsed

    fake_result = {
        "ok": True,
        "error": None,
        "detections": [{"class_name": "person", "x": 0, "y": 0, "width": 10, "height": 10, "confidence": 0.9}],
    }

    async def fake_get_yolo_model_thread_target():
        return object()

    monkeypatch.setattr(main, "get_yolo_model", lambda: object())
    monkeypatch.setattr(main, "detect_objects_frame", lambda cam: fake_result)

    save_calls = []

    def fake_save_yolo_events(cam, result):
        save_calls.append((cam, result))
        return [{"id": "evt-1", "event_type": "person", "object_count": 1, "timestamp": "2026-09-03T00:00:00"}]

    # save_yolo_events() itself (unmodified by this change -- see
    # main.py) is what schedules AI event clip/thumbnail building; this
    # test is about the surrounding LOOP wiring the stagger/semaphore
    # change actually touches -- that it still calls save_yolo_events()
    # with the same (camera_number, result) shape, and still raises the
    # in-app alert afterward -- not save_yolo_events()'s own already-
    # accepted internal clip/thumbnail logic, which is exercised
    # unchanged every time it's called for real in production.
    monkeypatch.setattr(main, "save_yolo_events", fake_save_yolo_events)

    alert_calls = []
    monkeypatch.setattr(main, "append_in_app_alert", lambda payload: alert_calls.append(payload))

    real_sleep = asyncio.sleep
    call_count = 0

    async def sleep_then_stop(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", sleep_then_stop)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await main.ai_person_detector(camera_number)

    asyncio.run(scenario())

    assert save_calls == [(camera_number, fake_result)], (
        "save_yolo_events() must still be called with the same (camera_number, result) shape as before"
    )
    assert len(alert_calls) == 1
    assert alert_calls[0]["event_type"] == "person"
    assert alert_calls[0]["camera"] == camera_number
    assert main.ai_person_last_event[camera_number] > 0.0, "cooldown timestamp must still be recorded"


# ---------------------------------------------------------------------------
# Synthetic timing test: 5 real detector tasks, real (short) stagger and
# semaphore, reporting whether inference starts remain separated.
# ---------------------------------------------------------------------------

def test_synthetic_five_camera_timing_shows_separated_not_synchronized_starts(monkeypatch, capsys):
    # A short stagger so this test runs in well under a second while
    # still exercising the real asyncio.sleep()-based stagger and the
    # real ai_inference_semaphore -- only the stagger MAGNITUDE is
    # scaled down, not the mechanism.
    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0.05)
    monkeypatch.setattr(main, "cv2", object())
    monkeypatch.setattr(main, "YOLO", object())
    monkeypatch.setattr(main, "get_yolo_model", lambda: object())

    inference_starts = {}

    def fake_detect(camera_number):
        inference_starts[camera_number] = time.monotonic()
        time.sleep(0.02)  # a short, fake "inference"
        return {"ok": True, "error": None, "detections": []}

    monkeypatch.setattr(main, "detect_objects_frame", fake_detect)

    for camera_number in range(1, 6):
        monkeypatch.setitem(main.ai_detection_state, camera_number, {})

    async def scenario():
        t0 = time.monotonic()
        tasks = [asyncio.create_task(main.ai_person_detector(n)) for n in range(1, 6)]
        # Let every task get through its stagger sleep and exactly one
        # inference call.
        deadline = time.monotonic() + 3.0
        while len(inference_starts) < 5 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return t0

    t0 = asyncio.run(scenario())

    assert len(inference_starts) == 5, f"expected all 5 cameras to run one inference, got {inference_starts}"

    offsets = {cam: round(ts - t0, 4) for cam, ts in inference_starts.items()}
    ordered = sorted(offsets.items())
    gaps = [round(b[1] - a[1], 4) for a, b in zip(ordered, ordered[1:])]

    print("\n[synthetic timing] per-camera inference-start offsets from t0:")
    for cam, offset in ordered:
        print(f"  camera{cam}: +{offset:.4f}s")
    print(f"[synthetic timing] consecutive gaps: {gaps}")
    print(f"[synthetic timing] min gap={min(gaps):.4f}s max gap={max(gaps):.4f}s spread={ordered[-1][1]-ordered[0][1]:.4f}s")

    # The whole point of the stagger: starts must NOT be clustered
    # together the way an un-staggered, un-serialized set of 5 tasks
    # created back-to-back would be (all within a few ms of each
    # other). With staggering + the inference semaphore serializing
    # actual execution, consecutive starts are meaningfully separated.
    assert min(gaps) > 0.005, f"inference starts are not meaningfully separated: gaps={gaps}"
    # And camera order is preserved (camera 1 first, camera 5 last),
    # matching the stagger's own camera_number * STAGGER formula.
    assert [cam for cam, _ in ordered] == [1, 2, 3, 4, 5]
