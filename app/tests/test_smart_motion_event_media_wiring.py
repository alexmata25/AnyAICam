"""Smart Motion's own independent event-media wiring (2026-09-14).

Real evidence during the Smart Motion customer-facing validation showed
195 real event_type='smart_motion' analytics events and zero associated
detection_event_media rows -- Smart Motion analytics events were being
created and cloud-synced correctly (detection/correlation/sync/
Investigate/analytics-panel/notifications all separately PROVEN), but
never entered the proven event-media pipeline at all.

store_motion_event() already schedules build_motion_event_clip() +
event_media_uploader.upload_motion_event_media() for the base Motion
event it creates (see test_motion_event_media_wiring.py), and
save_yolo_events() already does the identical thing, independently, for
each qualifying AI-classification event (see test_ai_event_clip_wiring.py)
-- each keyed by its own event id. Smart Motion was the one analytics
event type store_motion_event() created (via smart_motion.classify_motion())
but never scheduled a media task for at all.

This file proves the fix: when classify_motion() returns a truthy
correlation, the Smart Motion analytics event now ALSO gets a clip
build + upload scheduled via the exact same two functions, keyed by
its own id (smart_event["id"]) -- never the base Motion event's id and
never sharing/transferring ownership of the base Motion event's own,
already-independently-scheduled media.
"""

import asyncio
import sys
import types
from datetime import datetime
from unittest import mock

import pytest

import main


@pytest.fixture
def fake_uploader(monkeypatch):
    """event_media_uploader.py exists only on the physical appliance --
    injects a fake module under that exact import name so both
    store_motion_event()'s `from event_media_uploader import
    upload_motion_event_media` call sites (base Motion's own, and this
    fix's Smart Motion one) resolve to a real, call-recording stand-in."""
    calls = []

    def fake_upload_motion_event_media(**kwargs):
        calls.append(kwargs)
        return True

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = fake_upload_motion_event_media
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    return calls


def _standard_motion_mocks(monkeypatch, *, analytics_events=None):
    monkeypatch.setattr(main, "get_alert_rule", lambda camera_number: mock.Mock(enabled=False, event_types=[]))
    monkeypatch.setattr(main, "append_motion_event", lambda line: None)

    if analytics_events is None:
        analytics_events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: analytics_events.append(event))

    async def fake_create_motion_thumbnail(*args, **kwargs):
        return "/recordings/media/motion/fake.jpg"

    monkeypatch.setattr(main, "create_motion_thumbnail", fake_create_motion_thumbnail)
    return analytics_events


def _classify_as(monkeypatch, classification):
    """classify_motion() is what turns a plain base-Motion event into
    a Smart Motion analytics event too -- forcing a truthy return here
    is the only way to exercise the new media-scheduling code at all,
    exactly matching how store_motion_event() itself gates it."""
    monkeypatch.setattr(main.smart_motion, "classify_motion", lambda camera_number: classification)


async def _store_and_drain(camera_number, start_time, end_time, score=50.0, frame=b"fake-jpeg-bytes"):
    await main.store_motion_event(
        camera_number=camera_number,
        start_time=start_time,
        end_time=end_time,
        score=score,
        frame=frame,
    )
    pending = list(main.clip_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _by_event_type(analytics_events, event_type):
    matches = [event for event in analytics_events if event["event_type"] == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type!r} analytics event, got {len(matches)}"
    return matches[0]


# --------------------------------------------------------- own id, own scheduling


def test_smart_motion_event_schedules_media_using_its_own_event_id(monkeypatch, fake_uploader):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    clip_calls = []

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        clip_calls.append(event_id)
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(1, now, now))

    motion_event = _by_event_type(analytics_events, "motion")
    smart_event = _by_event_type(analytics_events, "smart_motion")

    # Two independent clip-build calls: one for base Motion (its own
    # id), one for Smart Motion (its own, different id).
    assert len(clip_calls) == 2
    assert motion_event["id"] in clip_calls
    assert smart_event["id"] in clip_calls
    assert motion_event["id"] != smart_event["id"]

    # The Smart Motion analytics event itself is completely unaffected --
    # it keeps its own event_id (smart_event["id"]), motion_event_id
    # (linking back to the base Motion event), triggered_by and rule
    # name/metadata exactly as classify_motion() produced them.
    assert smart_event["motion_event_id"] == motion_event["id"]
    assert smart_event["triggered_by"] == "person"
    assert smart_event["rule_name"] == "Smart Motion (person)"


def test_correct_camera_and_event_metadata_passed_into_the_media_pipeline(monkeypatch, fake_uploader):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "vehicle")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    start = datetime(2026, 9, 14, 9, 30, 0)
    end = datetime(2026, 9, 14, 9, 30, 12)
    asyncio.run(_store_and_drain(3, start, end))

    smart_event = _by_event_type(analytics_events, "smart_motion")

    smart_upload_calls = [call for call in fake_uploader if call["event_id"] == smart_event["id"]]
    assert len(smart_upload_calls) == 1
    upload = smart_upload_calls[0]
    assert upload["camera_number"] == 3
    assert upload["event_start"] == start
    assert upload["event_end"] == end
    assert upload["clip_url"] == f"/recordings/clips/motion/motion_{smart_event['id']}.mp4"
    assert upload["thumbnail_url"] == "/recordings/media/motion/fake.jpg"


def test_no_duplicate_smart_motion_media_scheduling_for_a_single_event(monkeypatch, fake_uploader):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    clip_calls = []

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        clip_calls.append(event_id)
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(2, now, now))

    smart_event = _by_event_type(analytics_events, "smart_motion")

    # Exactly one clip-build call and exactly one upload call for the
    # Smart Motion event's own id -- never scheduled twice for a
    # single classify_motion() result.
    assert clip_calls.count(smart_event["id"]) == 1
    smart_upload_calls = [call for call in fake_uploader if call["event_id"] == smart_event["id"]]
    assert len(smart_upload_calls) == 1


# --------------------------------------------------------- independence from base Motion / YOLO


def test_base_motion_and_smart_motion_media_ownership_is_independent(monkeypatch, fake_uploader):
    """Both media artifacts exist side by side, each uploaded under its
    own id -- Smart Motion must never share or take over the base
    Motion event's own already-scheduled clip/upload, and vice versa."""
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(4, now, now))

    motion_event = _by_event_type(analytics_events, "motion")
    smart_event = _by_event_type(analytics_events, "smart_motion")

    assert len(fake_uploader) == 2
    event_ids_uploaded = {call["event_id"] for call in fake_uploader}
    assert event_ids_uploaded == {motion_event["id"], smart_event["id"]}

    motion_upload = next(call for call in fake_uploader if call["event_id"] == motion_event["id"])
    smart_upload = next(call for call in fake_uploader if call["event_id"] == smart_event["id"])
    assert motion_upload["clip_url"] != smart_upload["clip_url"]


def test_smart_motion_media_scheduling_does_not_disturb_yolo_correlation_state(monkeypatch, fake_uploader):
    """Smart Motion's own media task must not call into save_yolo_events()
    or record_object_detection() at all -- it only ever reuses
    build_motion_event_clip()/upload_motion_event_media(), the same
    generic pair every event type already reuses independently."""
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "vehicle")

    yolo_calls = []
    monkeypatch.setattr(main, "save_yolo_events", lambda *a, **k: yolo_calls.append((a, k)) or [])
    monkeypatch.setattr(main.smart_motion, "record_object_detection", lambda *a, **k: yolo_calls.append((a, k)))

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(5, now, now))

    assert yolo_calls == []
    assert _by_event_type(analytics_events, "smart_motion") is not None


# --------------------------------------------------------- failure isolation


def test_smart_motion_clip_build_failure_does_not_prevent_smart_motion_event_creation(monkeypatch, fake_uploader):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def failing_build_motion_event_clip(event_id, camera_number, start, end):
        raise RuntimeError("simulated clip build failure")

    monkeypatch.setattr(main, "build_motion_event_clip", failing_build_motion_event_clip)

    now = datetime.now()
    # Must not raise.
    asyncio.run(_store_and_drain(6, now, now))

    smart_event = _by_event_type(analytics_events, "smart_motion")
    assert smart_event["triggered_by"] == "person"
    assert fake_uploader == [], "a failed clip build must never reach the uploader"


def test_smart_motion_scheduling_failure_does_not_prevent_smart_motion_event_creation(monkeypatch, fake_uploader, capsys):
    """Exercises the new scheduling-level guard directly: even if
    asyncio.create_task() itself fails for the Smart Motion media task
    specifically, append_analytics_event() has already run by that
    point -- the Smart Motion event is created and cloud-syncable
    regardless of what happens to its media."""
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    real_create_task = asyncio.create_task
    call_count = {"n": 0}

    def selective_create_task(coro, *args, **kwargs):
        call_count["n"] += 1
        # The base Motion event's own clip task is always scheduled
        # first (inside the motion_event_lock block); the Smart Motion
        # one is scheduled second, only once classify_motion() returns
        # truthy -- matching store_motion_event()'s real, fixed order.
        if call_count["n"] == 2:
            coro.close()
            raise RuntimeError("simulated scheduling failure")
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "create_task", selective_create_task)

    now = datetime.now()
    # Must not raise -- the Smart Motion analytics event must still exist.
    asyncio.run(_store_and_drain(7, now, now))

    smart_event = _by_event_type(analytics_events, "smart_motion")
    assert smart_event["triggered_by"] == "person"

    captured = capsys.readouterr()
    assert "could not schedule" in captured.out
    assert "RuntimeError" in captured.out
    assert "simulated scheduling failure" in captured.out


def test_smart_motion_media_upload_failure_is_fail_open_and_logged(monkeypatch, capsys):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    def raising_upload(**kwargs):
        raise RuntimeError("simulated network failure")

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = raising_upload
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)

    now = datetime.now()
    # Must not raise -- both the base Motion and Smart Motion events survive.
    asyncio.run(_store_and_drain(8, now, now))

    assert _by_event_type(analytics_events, "motion") is not None
    smart_event = _by_event_type(analytics_events, "smart_motion")
    assert smart_event["triggered_by"] == "person"

    captured = capsys.readouterr()
    assert f"Smart Motion event {smart_event['id']}: media upload failed" in captured.out
    assert "RuntimeError" in captured.out
    assert "simulated network failure" in captured.out


# --------------------------------------------------------- no classification -> unaffected


def test_no_classification_schedules_no_smart_motion_media_at_all(monkeypatch, fake_uploader):
    """classify_motion() returning falsy (the existing, already-tested
    default in every test_motion_event_media_wiring.py case) must
    continue to schedule only the base Motion event's own media --
    zero Smart Motion analytics events, zero extra clip-build calls."""
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, None)

    clip_calls = []

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        clip_calls.append(event_id)
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(9, now, now))

    assert [event["event_type"] for event in analytics_events] == ["motion"]
    assert len(clip_calls) == 1
    assert len(fake_uploader) == 1
