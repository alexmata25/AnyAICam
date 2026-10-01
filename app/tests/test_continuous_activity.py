"""Continuous activity is ONE event (owner decision 2026-10-01).

Physical test on Ryzen b3546bd: people active in the Living Room for ~2
minutes produced a new person card, clip, recording and notification every
~30 s (the AI save cooldown), because "same real event" (8 s) was only ever
checked between saved detections. Now an activity opens with the first
detection and every scan that still sees something moving extends it; its
one clip and Event-mode recording are built when it ends (or at the 5-minute
cap). Covered here: continuous people, a moving vehicle, a parked
(stationary) vehicle, mixed / new object classes, stopping and restarting,
and the cap -- through the pure tracker, save_yolo_events() and the real
detector loop."""
import asyncio
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

import ai_activity
import main
from test_ai_event_clip_wiring import _standard_mocks, _wait_until, background_loop, fake_uploader  # noqa: F401

T = datetime(2026, 10, 1, 1, 30, 0)


# ------------------------------------------------------------ the pure rules

def test_continuation_gap_is_the_merge_gap_but_never_less_than_two_scans():
    assert ai_activity.continuation_gap(8, 5) == 10
    assert ai_activity.continuation_gap(20, 5) == 20


def test_sightings_inside_the_gap_extend_one_activity_and_a_pause_ends_it():
    tracker = ai_activity.ActivityTracker()
    lag = timedelta(seconds=9)                                 # frames run ~9 s behind the appliance clock
    activity = tracker.open(1, "a", start=T - timedelta(seconds=5), moment=T, observed_at=T + lag)
    for s in (6, 12, 18):                                    # someone moving about, a scan every 6 s
        assert tracker.seen(1, T + timedelta(seconds=s), gap_seconds=10, max_seconds=300, observed_at=T + timedelta(seconds=s) + lag)
        # between scans the activity is NOT due to close, despite the frame lag
        assert tracker.close_due(activity, T + timedelta(seconds=s + 5) + lag, gap_seconds=10, max_seconds=300) is not None
    assert activity.last_seen == T + timedelta(seconds=18)
    assert tracker.continuing(1, T + timedelta(seconds=40), gap_seconds=10, max_seconds=300) is None   # 22 s quiet: over
    assert tracker.close_due(activity, T + timedelta(seconds=27) + lag, gap_seconds=10, max_seconds=300) == T + timedelta(seconds=28) + lag
    assert tracker.close_due(activity, T + timedelta(seconds=28) + lag, gap_seconds=10, max_seconds=300) is None


def test_the_cap_ends_an_activity_that_never_pauses():
    tracker = ai_activity.ActivityTracker()
    activity = tracker.open(1, "a", start=T, moment=T, observed_at=T)
    for s in range(6, 301, 6):
        tracker.seen(1, T + timedelta(seconds=s), gap_seconds=10, max_seconds=300, observed_at=T + timedelta(seconds=s))
    assert tracker.continuing(1, T + timedelta(seconds=306), gap_seconds=10, max_seconds=300) is None
    assert tracker.close_due(activity, T + timedelta(seconds=300), gap_seconds=10, max_seconds=300) is None


def test_a_closed_activity_is_never_extended_and_cameras_are_independent():
    tracker = ai_activity.ActivityTracker()
    first = tracker.open(1, "a", start=T, moment=T)
    tracker.open(2, "b", start=T, moment=T)
    tracker.close(first)
    assert not tracker.seen(1, T + timedelta(seconds=3), gap_seconds=10, max_seconds=300)
    assert tracker.is_open(2) and not tracker.is_open(1)


def test_an_open_activity_keeps_its_footage_from_before_its_start():
    tracker = ai_activity.ActivityTracker()
    tracker.open(1, "a", start=T, moment=T)
    assert tracker.in_flight_windows(1, lead_seconds=60) == [(T - timedelta(seconds=60), datetime.max)]
    assert tracker.in_flight_windows(2, lead_seconds=60) == []


# ------------------------------------------------------------ save_yolo_events()

def _scan(*classes, ago: float):
    """A detection frame that happened `ago` seconds before now."""
    detections = [{"x": 10 + 50 * i, "y": 10, "width": 40, "height": 40, "class_name": c, "confidence": 0.9}
                  for i, c in enumerate(classes)]
    return {"detections": detections, "frame": np.zeros((120, 400, 3), dtype=np.uint8),
            "frame_captured_at": datetime.now() - timedelta(seconds=ago)}


@pytest.fixture
def wired(monkeypatch, tmp_path, fake_uploader, background_loop):  # noqa: F811
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (10.0, 300.0))
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera: {"mode": "event"})
    builds, persisted = [], []

    async def fake_build(event_id, camera_number, start, end):
        builds.append((event_id, start, end))
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    async def fake_persist(camera_number, start, end, *, detector=None, trigger_id=None):
        persisted.append((trigger_id, start, end))

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build)
    monkeypatch.setattr(main, "persist_event_recording", fake_persist)
    _standard_mocks(monkeypatch, tmp_path)
    main.ai_activities.reset()
    main.ai_event_clip_windows.clear()
    yield main, builds, persisted, fake_uploader
    main.ai_activities.reset()


def _end_activities(monkeypatch):
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (0.0, 300.0))


def test_continuous_people_are_one_card_one_clip_one_recording(wired, monkeypatch):
    main_, builds, persisted, uploads = wired
    cards = []
    for ago in (40, 34, 28, 22):                                   # a person moving about for 18 s
        cards += main_.save_yolo_events(5, _scan("person", ago=ago))
    assert [c["event_type"] for c in cards] == ["person"]          # one card, one notification
    _end_activities(monkeypatch)
    assert _wait_until(lambda: len(uploads) == 1 and len(persisted) == 1, timeout=10)
    owner, start, end = builds[0]
    assert owner == cards[0]["id"]
    assert (end - start).total_seconds() >= 18                      # the clip covers the whole activity
    assert persisted[0][1:] == (start, end)                         # and so does its one recording


def test_a_moving_vehicle_is_one_event(wired, monkeypatch):
    main_, builds, persisted, uploads = wired
    cards = []
    for ago in (30, 25, 20):
        cards += main_.save_yolo_events(6, _scan("car", ago=ago))
    assert [c["event_type"] for c in cards] == ["car"]
    _end_activities(monkeypatch)
    assert _wait_until(lambda: len(uploads) == 1, timeout=10)


def test_a_new_kind_of_object_gets_its_own_card_sharing_the_activitys_clip(wired, monkeypatch):
    main_, builds, persisted, uploads = wired
    person = main_.save_yolo_events(7, _scan("person", ago=40))
    car = main_.save_yolo_events(7, _scan("person", "car", ago=34))
    again = main_.save_yolo_events(7, _scan("car", "person", ago=28))
    dog = main_.save_yolo_events(7, _scan("dog", ago=22))
    assert [c["event_type"] for c in person + car + again + dog] == ["person", "car", "dog"]
    assert car[0]["media_parent_event_id"] == person[0]["id"] == dog[0]["media_parent_event_id"]
    _end_activities(monkeypatch)
    assert _wait_until(lambda: len(uploads) == 1, timeout=10)
    time.sleep(0.3)
    assert [u["event_id"] for u in uploads] == [person[0]["id"]]    # one clip for all of it


def test_stopping_and_restarting_is_two_events(wired, monkeypatch):
    main_, builds, persisted, uploads = wired
    first = main_.save_yolo_events(8, _scan("person", ago=55))
    second = main_.save_yolo_events(8, _scan("person", ago=20))   # 35 s later: a new visit
    assert first and second and first[0]["id"] != second[0]["id"]
    _end_activities(monkeypatch)
    assert _wait_until(lambda: len(uploads) == 2, timeout=10)


def test_the_cap_starts_a_new_event_for_activity_that_never_stops(wired, monkeypatch):
    main_, builds, persisted, uploads = wired
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (10.0, 20.0))   # a 20 s cap stands in for 300 s
    cards = []
    for ago in (50, 44, 38, 32, 26):                                # never quiet for 10 s
        cards += main_.save_yolo_events(9, _scan("person", ago=ago))
    assert [c["event_type"] for c in cards] == ["person", "person"]   # capped once
    _end_activities(monkeypatch)
    assert _wait_until(lambda: len(uploads) == 2, timeout=10)


def test_the_cooldown_still_paces_saved_detections_while_an_activity_is_open():
    """The loop resets its cooldown when an activity is open, so PPE / Face
    Access / Voice Call keep running at today's pace instead of every scan."""
    import inspect
    source = inspect.getsource(main.ai_person_detector)
    assert "if events or ai_activities.is_open(camera_number):" in source


# ------------------------------------------------------------ the real detector loop

def _frame(moving: bool, at: datetime):
    x = 300 + (int(at.timestamp()) % 60) * 10 if moving else 300
    return {"ok": True, "frame": np.zeros((720, 1280, 3), dtype=np.uint8), "error": None, "frame_captured_at": at,
            "detections": [{"class_name": "car", "confidence": 0.9, "x": x, "y": 200, "width": 300, "height": 200}]}


def _run_loop(monkeypatch, camera, frames, saved):
    """Drive ai_person_detector() through exactly len(frames) iterations."""
    queue = list(frames)
    monkeypatch.setattr(main, "YOLO", object())
    monkeypatch.setattr(main, "get_yolo_model", lambda: None)
    monkeypatch.setattr(main, "AI_DETECTOR_STARTUP_STAGGER_SECONDS", 0)
    monkeypatch.setattr(main.lpr, "LPR_ENABLED", False)
    monkeypatch.setattr(main, "detect_objects_frame", lambda camera_number: queue.pop(0))
    monkeypatch.setattr(main, "_ai_activity_limits", lambda c: (10.0, 300.0))
    main.ai_detection_state.setdefault(camera, {"status": "starting", "last_checked": None, "last_detection": None, "detections": 0, "error": None})
    main.ai_person_last_event[camera] = 0.0

    def fake_save(camera_number, result):
        saved.append(result["frame_captured_at"])
        main.ai_activities.open(camera_number, f"own-{len(saved)}", start=result["frame_captured_at"], moment=result["frame_captured_at"])
        return [{"id": f"own-{len(saved)}", "event_type": "car", "object_count": 1, "timestamp": result["frame_captured_at"].isoformat()}]

    monkeypatch.setattr(main, "save_yolo_events", fake_save)
    monkeypatch.setattr(main, "append_in_app_alert", lambda alert: None)
    real_sleep = asyncio.sleep

    async def stop_after_each(seconds):
        if not queue:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_each)
    try:
        asyncio.run(main.ai_person_detector(camera))
    except asyncio.CancelledError:
        pass


def test_a_parked_car_does_not_keep_an_activity_open(monkeypatch):
    main.ai_activities.reset()
    main.ai_stationary_memory = __import__("stationary_objects").StationaryMemory()
    saved = []
    frames = [_frame(False, T + timedelta(seconds=6 * i)) for i in range(5)]    # the same parked car, 24 s
    _run_loop(monkeypatch, 41, frames, saved)
    activity = main.ai_activities.get(41, "own-1")
    assert saved == [T]                                      # reported once
    assert activity is not None and activity.last_seen == T  # never extended by the repeats
    main.ai_activities.reset()


def test_a_moving_car_keeps_its_activity_open(monkeypatch):
    main.ai_activities.reset()
    main.ai_stationary_memory = __import__("stationary_objects").StationaryMemory()
    saved = []
    frames = [_frame(True, T + timedelta(seconds=6 * i)) for i in range(5)]     # driving through
    _run_loop(monkeypatch, 42, frames, saved)
    activity = main.ai_activities.get(42, "own-1")
    assert activity is not None and activity.last_seen == T + timedelta(seconds=24)
    assert len(saved) == 1                                    # the cooldown still paces saved detections
    main.ai_activities.reset()


def test_a_finaliser_closed_without_finishing_runs_nothing(monkeypatch):
    """Found in the suite: a finaliser abandoned when its loop shut down was
    closed by the garbage collector inside ThreadPoolExecutor.submit() on a
    thread that held the executor's lock; its finally-block's
    asyncio.to_thread() re-entered submit() and deadlocked. Closed without
    finishing, it must run nothing -- no thread, no await, no tracker lock."""
    main.ai_activities.reset()
    activity = main.ai_activities.open(43, "own", start=T, moment=T)
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (3600.0, 3600.0))   # still waiting

    def forbidden(*a, **k):
        raise AssertionError("closing an unfinished finaliser must not start threads")

    monkeypatch.setattr(main.asyncio, "to_thread", forbidden)
    monkeypatch.setattr(main.event_media_sharing, "owner_finished", forbidden)

    class _Parked:
        def __await__(self):
            yield                                                # parked, like a real wait

    async def never(seconds):
        await _Parked()

    coroutine = main._finalize_ai_activity(43, activity, None, sleep=never)
    coroutine.send(None)                                         # runs up to its first wait
    coroutine.close()                                            # what the garbage collector does
    assert main.ai_activities.is_open(43)                        # untouched (no lock taken)
    main.ai_activities.reset()
