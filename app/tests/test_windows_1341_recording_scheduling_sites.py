"""Windows 1.3.4.1 R1 (Codex R1 re-review 2026-11-06, owner-approved scope): scheduling an Event-mode recording or its
linked-recording backfill is optional media work that must never interrupt the event path triggering it. Covers the
three sites Codex listed beyond store_motion_event():
  - LPR (run_lpr_scan, worker thread -> run_coroutine_threadsafe): the plate event is appended and later plates of the
    same scan still run when the mode lookup or either submission fails; rejected coroutines are closed; a submitted
    recording that later fails is reported;
  - people counting (main loop -> create_task): crossing rows and the counter-state save survive lookup or scheduling
    failures, and scheduled tasks are retained in clip_tasks;
  - customer analytics rule worker (main loop): fired rule events are persisted and the alarm talk-down still runs.
The mode is resolved once per plate/batch and falls back to LOCAL_RECORDING_MODE ("event", R-REC-1) on a lookup failure.
"""
import asyncio
import concurrent.futures

import numpy as np
import pytest

import main
import customer_analytics_rule_worker as worker
import recording_uploader
from test_customer_analytics_rule_worker import ENTITLED_CAM_1, _seed_rule, _seeded_db, db_path  # noqa: F401 -- fixtures
from test_people_counting_worker_thumbnail import (  # noqa: F401 -- fixture
    _OneCrossingCounter, _fake_frame, _people_counting_setup, _run_one_cycle as _people_counting_cycle)


def _names(log):
    return [name for name, _ in log]


def _lookup_fails(camera_number):
    raise RuntimeError("settings store unavailable")


@pytest.fixture(autouse=True)
def _lpr_pipeline_reset():
    yield
    main.lpr.reset_pipeline_state()  # no confirmed-plate state leaks into later tests


@pytest.fixture()
def structured(monkeypatch):
    events = []
    monkeypatch.setattr(main, "structured_log", lambda event, level="info", **fields: events.append((event, fields)))
    return events


# ------------------------------------------------------------------ LPR (worker thread)


def _two_plates(monkeypatch, tmp_path):
    # Same LPR state setup as test_lpr_independent_cadence.py's autouse _state fixture.
    monkeypatch.setattr(main.lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera: True)
    main.lpr.reset_pipeline_state()
    events = []
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: {"lpr_enabled": True})
    monkeypatch.setattr(main.lpr, "LPR_FULL_RES_FRAMES", False)
    monkeypatch.setattr(main, "append_analytics_event", events.append)
    monkeypatch.setattr(main, "_analytics_media_owner", lambda cam, event_id, now, start=None: None)
    monkeypatch.setattr(main, "linked_recording_for", lambda cam, now: None)
    monkeypatch.setattr(main, "_ai_event_media_loop", object())
    monkeypatch.setattr(main.lpr, "scan_frame", lambda cam, fr, boxes, full_frame=None: [
        {"plate_number": plate, "confidence": 91.0, "region": (5, 5, 40, 15), "crop": fr[0:100, 0:200],
         "vehicle_box": None, "source": "full_frame"} for plate in ("QRS4821", "XYZ7310")])
    return events


def _scan(camera=2):
    return main.run_lpr_scan(camera, {"frame": np.full((720, 1280, 3), 90, np.uint8), "detections": []})


def test_lpr_a_mode_lookup_failure_still_appends_every_plate_and_schedules_the_recording(monkeypatch, tmp_path, structured):
    events = _two_plates(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_local_recording_settings", _lookup_fails)
    scheduled = []

    def fake_threadsafe(coroutine, loop):
        scheduled.append(coroutine.cr_code.co_name)
        coroutine.close()
        return concurrent.futures.Future()

    monkeypatch.setattr(main.asyncio, "run_coroutine_threadsafe", fake_threadsafe)
    created = _scan()
    assert len(created) == 2 and events == created
    assert scheduled == ["persist_event_recording", "_backfill_ai_event_linked_recording"] * 2  # R-REC-1 fallback
    assert _names(structured).count("event_recording.mode_lookup_failed") == 2  # once per plate, not per submission


@pytest.mark.parametrize("error", [RuntimeError("loop closed"), ValueError("not a coroutine"), TypeError("bad loop")])
def test_lpr_a_submission_failure_never_stops_the_plate_event_or_later_plates(monkeypatch, tmp_path, structured, capsys,
                                                                              error):
    events = _two_plates(monkeypatch, tmp_path)
    lookups = []
    monkeypatch.setattr(main, "_local_recording_settings", lambda cam: lookups.append(cam) or {"mode": "event"})
    rejected = []

    def failing_threadsafe(coroutine, loop):
        rejected.append(coroutine)
        raise error

    monkeypatch.setattr(main.asyncio, "run_coroutine_threadsafe", failing_threadsafe)
    created = _scan()
    assert len(created) == 2 and events == created
    assert len(rejected) == 4 and all(coroutine.cr_frame is None for coroutine in rejected)  # closed, never "never awaited"
    assert lookups == [2, 2]  # resolved once per plate
    assert _names(structured).count("event_recording.schedule_failed") == 4
    out = capsys.readouterr().out
    assert "could not schedule Event-mode recording persist" in out and "could not schedule linked_recording backfill" in out


def test_lpr_a_submitted_recording_that_later_fails_is_reported(monkeypatch, tmp_path, capsys):
    _two_plates(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_local_recording_settings", lambda cam: {"mode": "event"})
    futures = []

    def fake_threadsafe(coroutine, loop):
        coroutine.close()
        futures.append(concurrent.futures.Future())
        return futures[-1]

    monkeypatch.setattr(main.asyncio, "run_coroutine_threadsafe", fake_threadsafe)
    assert len(_scan()) == 2
    futures[0].set_exception(OSError("disk full"))
    futures[1].cancel()
    assert "Event-mode recording persist failed: OSError: disk full" in capsys.readouterr().out


def test_lpr_no_media_loop_schedules_nothing_and_needs_no_lookup(monkeypatch, tmp_path):
    events = _two_plates(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_ai_event_media_loop", None)
    monkeypatch.setattr(main, "_local_recording_settings", _lookup_fails)
    assert len(_scan()) == 2 and len(events) == 2


# ------------------------------------------------------------------ people counting (main loop)


def _counting(monkeypatch, *, settings=lambda camera_number: {"mode": "event"}):
    monkeypatch.setattr(main.people_counting, "PeopleCounter", lambda line: _OneCrossingCounter())
    monkeypatch.setattr(main, "_local_recording_settings", settings)
    monkeypatch.setattr(main, "detect_objects_frame", lambda camera_number: {
        "ok": True, "frame": _fake_frame(), "error": None,
        "detections": [{"class_name": "person", "x": 270, "y": 190, "width": 100, "height": 100}]})
    rows, saves, scheduled = [], [], []
    monkeypatch.setattr(main, "append_analytics_event", rows.append)
    monkeypatch.setattr(main, "_people_counting_state_save", lambda camera_number, counter: saves.append(camera_number))

    async def fake_persist(camera_number, start, end, *, detector=None, trigger_id=None):
        return None

    async def fake_backfill(camera_number, event_ids, event_time):
        return None

    monkeypatch.setattr(main, "persist_event_recording", fake_persist)
    monkeypatch.setattr(main, "_backfill_ai_event_linked_recording", fake_backfill)
    return rows, saves, scheduled


def _create_task_failing(monkeypatch, scheduled, failing):
    real_create_task = asyncio.create_task

    def selective(coroutine, *args, **kwargs):
        name = getattr(coroutine, "__name__", "")
        scheduled.append(name)
        if name in failing:
            raise RuntimeError("simulated scheduling failure")
        return real_create_task(coroutine, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "create_task", selective)


def test_people_counting_a_mode_lookup_failure_keeps_rows_and_state_and_falls_back_to_event(monkeypatch,
                                                                                         _people_counting_setup,
                                                                                         structured):
    rows, saves, scheduled = _counting(monkeypatch, settings=_lookup_fails)
    _create_task_failing(monkeypatch, scheduled, failing=set())
    asyncio.run(_people_counting_cycle(3, monkeypatch))
    assert len(rows) == 1 and saves == [3]
    assert scheduled == ["fake_persist", "fake_backfill"]
    assert _names(structured) == ["event_recording.mode_lookup_failed"]


@pytest.mark.parametrize("failing", [{"fake_persist"}, {"fake_backfill"}, {"fake_persist", "fake_backfill"}])
def test_people_counting_a_scheduling_failure_keeps_rows_and_the_counter_state_save(monkeypatch,
                                                                                   _people_counting_setup,
                                                                                   structured, failing):
    rows, saves, scheduled = _counting(monkeypatch)
    _create_task_failing(monkeypatch, scheduled, failing)
    asyncio.run(_people_counting_cycle(3, monkeypatch))
    assert len(rows) == 1 and saves == [3]
    assert scheduled == ["fake_persist", "fake_backfill"]  # a failed recording never skips the backfill attempt
    assert _names(structured).count("event_recording.schedule_failed") == len(failing)


def test_scheduled_media_tasks_are_retained_until_done_and_cancellation_is_untouched():
    async def scenario():
        gate = asyncio.Event()

        async def media():
            await gate.wait()

        task = main.schedule_event_media_task(media(), label="Camera 9 People Counting", what="Event-mode recording persist")
        assert task in main.clip_tasks
        gate.set()
        await task
        await asyncio.sleep(0)
        assert task not in main.clip_tasks

        blocked = main.schedule_event_media_task(asyncio.Event().wait(), label="Camera 9", what="linked_recording backfill")
        await asyncio.sleep(0)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        await asyncio.sleep(0)
        assert blocked.cancelled() and blocked not in main.clip_tasks

    asyncio.run(scenario())


# ------------------------------------------------------------------ customer analytics rule worker (main loop)


def _rule_cycle(monkeypatch, *, settings=lambda camera_number: {"mode": "event"}):
    _seed_rule("cam-1")
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda camera_number: ENTITLED_CAM_1)
    monkeypatch.setattr(main, "detect_objects_frame", lambda camera_number: {
        "ok": True, "frame": np.zeros((480, 640, 3), np.uint8), "error": None, "detections": []})
    monkeypatch.setattr(worker.analytics_rules_engine, "evaluate_rules", lambda *a, **k: [{"rule_id": "rule-1"}])
    monkeypatch.setattr(worker, "evaluate_security_lines", lambda *a, **k: [])
    monkeypatch.setattr(worker, "save_rule_event_thumbnail", lambda *a, **k: None)
    persisted, talkdowns = [], []
    monkeypatch.setattr(worker, "persist_rule_event",
                        lambda camera_number, fired, now, thumbnail: persisted.append(fired) or {"id": f"ev-{len(persisted)}"})
    monkeypatch.setattr(worker, "speak_alarm_talkdowns", lambda camera_id, fired, event_ids: talkdowns.append(event_ids))
    monkeypatch.setattr(main, "_local_recording_settings", settings)

    async def fake_persist(camera_number, start, end, *, detector=None, trigger_id=None):
        return None

    async def fake_backfill(camera_number, event_ids, event_time):
        return None

    monkeypatch.setattr(main, "persist_event_recording", fake_persist)
    monkeypatch.setattr(main, "_backfill_ai_event_linked_recording", fake_backfill)

    async def one_cycle():
        async def fake_sleep(seconds):
            raise asyncio.CancelledError()
        monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
        try:
            await worker.customer_analytics_rule_worker(1)
        except asyncio.CancelledError:
            pass
    return persisted, talkdowns, one_cycle


def test_rule_worker_a_mode_lookup_failure_still_persists_events_and_speaks_the_alarm(monkeypatch, structured):
    persisted, talkdowns, one_cycle = _rule_cycle(monkeypatch, settings=_lookup_fails)
    scheduled = []
    _create_task_failing(monkeypatch, scheduled, failing=set())
    asyncio.run(one_cycle())
    assert persisted == [{"rule_id": "rule-1"}] and talkdowns == [["ev-1"]]
    assert scheduled == ["fake_persist", "fake_backfill"]  # R-REC-1 fallback
    assert "event_recording.mode_lookup_failed" in _names(structured)


@pytest.mark.parametrize("failing", [{"fake_persist"}, {"fake_backfill"}, {"fake_persist", "fake_backfill"}])
def test_rule_worker_a_scheduling_failure_still_persists_events_and_speaks_the_alarm(monkeypatch, structured, failing):
    persisted, talkdowns, one_cycle = _rule_cycle(monkeypatch)
    scheduled = []
    _create_task_failing(monkeypatch, scheduled, failing)
    asyncio.run(one_cycle())
    assert persisted == [{"rule_id": "rule-1"}] and talkdowns == [["ev-1"]]
    assert scheduled == ["fake_persist", "fake_backfill"]
    assert _names(structured).count("event_recording.schedule_failed") == len(failing)
