"""Real event-to-recording linkage gap found live on Ryzen (2026-09-22),
traced end to end: event creation -> event timestamp/window -> Event-mode
clip creation -> linked_recording population -> Events/Investigate
payload.

Root cause (the save_yolo_events() family -- person/car/vehicle/ppe/
plate): linked_recording_for() is called synchronously, at the exact
moment of detection, before persist_event_recording() (scheduled
separately, via its own asyncio task) has even started building this
camera's Event-mode clip -- which requires waiting for the post-roll
window to actually elapse in wall-clock time, then running a real
ffmpeg concat. For an Event-mode camera specifically, no local file
ever covers "now" at the instant of detection (linked_recording_for()'s
own "does an already-existing file cover this instant" check is only
ever true by construction for Continuous mode's always-recording
segments), so that first, eager lookup reliably found nothing -- and
with no retry mechanism anywhere, the event's linked_recording stayed
null forever even once the real clip existed moments later.

Fix: _backfill_ai_event_linked_recording() is scheduled alongside
persist_event_recording() (same trigger condition: an Event-mode
camera), waits the same window plus a real margin for the ffmpeg
concat to finish, then re-resolves linked_recording_for() -- now
against a real, completed clip -- and patches every event in that
detection batch (by id) whose linked_recording is still falsy via
_patch_analytics_events_linked_recording(), a small locked, in-place
JSON update using the exact same file-locking shape append_analytics_
event() itself already uses.

The companion bug for plain "motion" events (a different code path,
store_motion_event(), which never included linked_recording in its own
analytics_events.json mirror dict at all) is covered separately in
test_motion_event_media_wiring.py.
"""
import asyncio
from datetime import datetime

import main


def test_patch_only_updates_events_with_no_existing_linked_recording(tmp_path, monkeypatch):
    events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    main.save_json_list(events_file, [
        {"id": "a", "linked_recording": None},
        {"id": "b", "linked_recording": "/recordings/camera1/existing.mkv#t=0.0,5.0"},
        {"id": "c"},  # field entirely absent, same as null
    ])

    main._patch_analytics_events_linked_recording(["a", "b", "c"], "/recordings/camera1/new.mkv#t=1.0,6.0")

    events = {event["id"]: event for event in main.load_json_list(events_file)}
    assert events["a"]["linked_recording"] == "/recordings/camera1/new.mkv#t=1.0,6.0"
    assert events["b"]["linked_recording"] == "/recordings/camera1/existing.mkv#t=0.0,5.0", \
        "an already-real linked_recording must never be overwritten with a later guess"
    assert events["c"]["linked_recording"] == "/recordings/camera1/new.mkv#t=1.0,6.0"


def test_patch_only_touches_the_targeted_ids(tmp_path, monkeypatch):
    events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    main.save_json_list(events_file, [
        {"id": "a", "linked_recording": None},
        {"id": "unrelated", "linked_recording": None},
    ])

    main._patch_analytics_events_linked_recording(["a"], "/recordings/camera1/new.mkv#t=0.0,5.0")

    events = {event["id"]: event for event in main.load_json_list(events_file)}
    assert events["a"]["linked_recording"] == "/recordings/camera1/new.mkv#t=0.0,5.0"
    assert events["unrelated"]["linked_recording"] is None


def test_patch_is_a_no_op_when_nothing_needs_updating(tmp_path, monkeypatch):
    events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    main.save_json_list(events_file, [{"id": "a", "linked_recording": "/recordings/camera1/already-set.mkv#t=0.0,5.0"}])

    main._patch_analytics_events_linked_recording(["a"], "/recordings/camera1/different.mkv#t=1.0,6.0")

    events = main.load_json_list(events_file)
    assert events[0]["linked_recording"] == "/recordings/camera1/already-set.mkv#t=0.0,5.0"


# ------------------------------------------------- _backfill_ai_event_linked_recording


def test_backfill_patches_the_event_once_the_clip_is_found(tmp_path, monkeypatch):
    events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    main.save_json_list(events_file, [{"id": "evt-1", "linked_recording": None}])
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"pre_roll_seconds": 5, "post_roll_seconds": 5})
    monkeypatch.setattr(
        main, "linked_recording_for",
        lambda camera_number, start, end: "/recordings/camera1/camera1_2026-09-22_00-00-00.mkv#t=5.0,10.0",
    )

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    asyncio.run(main._backfill_ai_event_linked_recording(1, ["evt-1"], datetime.now()))

    events = main.load_json_list(events_file)
    assert events[0]["linked_recording"] == "/recordings/camera1/camera1_2026-09-22_00-00-00.mkv#t=5.0,10.0"


def test_backfill_patches_every_event_id_from_the_same_detection_batch(tmp_path, monkeypatch):
    events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    main.save_json_list(events_file, [
        {"id": "evt-person", "linked_recording": None},
        {"id": "evt-ppe", "linked_recording": None},
        {"id": "evt-unrelated", "linked_recording": None},
    ])
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"pre_roll_seconds": 5, "post_roll_seconds": 5})
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: "/recordings/camera1/found.mkv#t=1.0,2.0")

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    asyncio.run(main._backfill_ai_event_linked_recording(1, ["evt-person", "evt-ppe"], datetime.now()))

    events = {event["id"]: event for event in main.load_json_list(events_file)}
    assert events["evt-person"]["linked_recording"] == "/recordings/camera1/found.mkv#t=1.0,2.0"
    assert events["evt-ppe"]["linked_recording"] == "/recordings/camera1/found.mkv#t=1.0,2.0"
    assert events["evt-unrelated"]["linked_recording"] is None


def test_backfill_leaves_the_event_unchanged_when_the_clip_still_does_not_exist(tmp_path, monkeypatch):
    """A real build failure, no buffered sources, or a camera that
    somehow isn't in Event mode after all -- this must never raise,
    and must never invent a fake link. The event simply keeps its
    original (still null) value, exactly like before this fix."""
    events_file = tmp_path / "analytics_events.json"
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    main.save_json_list(events_file, [{"id": "evt-2", "linked_recording": None}])
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"pre_roll_seconds": 5, "post_roll_seconds": 5})
    monkeypatch.setattr(main, "linked_recording_for", lambda camera_number, start, end: None)

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    asyncio.run(main._backfill_ai_event_linked_recording(1, ["evt-2"], datetime.now()))  # must not raise

    events = main.load_json_list(events_file)
    assert events[0]["linked_recording"] is None


def test_backfill_waits_at_least_past_the_configured_post_roll_window(monkeypatch):
    """Proves the wait is real and scales with this camera's own
    configured post_roll_seconds -- the same window persist_event_
    recording() itself waits past -- not a short, fixed delay that
    would look at the clip before it actually exists."""
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"pre_roll_seconds": 5, "post_roll_seconds": 20})
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    asyncio.run(main._backfill_ai_event_linked_recording(1, ["evt-3"], datetime.now()))

    assert slept, "must actually wait before looking for the clip"
    assert slept[0] >= 20, "must wait at least the configured post_roll_seconds, not a short fixed delay"
