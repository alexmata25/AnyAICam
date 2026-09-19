"""store_motion_event() real clip + cloud upload wiring (2026-09-02).

Reconciles the accepted, live-tested EC2/Samsung production behavior:
store_motion_event() no longer relies solely on the old
linked_recording_for() Media-Fragment stopgap -- it also (a) mirrors
the motion event into ANALYTICS_EVENTS_FILE via append_analytics_event()
(required for event_media_uploader._ensure_detection_event_synced() to
find this exact event by id before it will register cloud media), and
(b) schedules build_motion_event_clip() + event_media_uploader.
upload_motion_event_media() together, via asyncio.create_task() (the
existing pattern -- store_motion_event() already runs on the main
loop, so no cross-thread scheduling is needed here, unlike
save_yolo_events()'s own asyncio.to_thread() worker-thread path).

Traced verbatim from the real, live Samsung appliance's own
store_motion_event() (not invented) -- see the reconciliation commit
this file was added in for the exact grep/sed trace.
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
    injects a fake module under that exact import name so
    store_motion_event()'s own `from event_media_uploader import
    upload_motion_event_media` resolves to a real, call-recording
    stand-in."""
    calls = []

    def fake_upload_motion_event_media(**kwargs):
        calls.append(kwargs)
        return True

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = fake_upload_motion_event_media
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    return calls


def _standard_motion_mocks(monkeypatch):
    monkeypatch.setattr(main, "get_alert_rule", lambda camera_number: mock.Mock(enabled=False, event_types=[]))
    monkeypatch.setattr(main, "append_motion_event", lambda line: None)
    # Default no-op -- avoids real file I/O against ANALYTICS_EVENTS_FILE
    # in tests that don't specifically need to inspect this call; tests
    # that do (see below) override it afterward with their own capturing
    # version.
    monkeypatch.setattr(main, "append_analytics_event", lambda event: None)

    async def fake_create_motion_thumbnail(*args, **kwargs):
        return "/recordings/media/motion/fake.jpg"

    monkeypatch.setattr(main, "create_motion_thumbnail", fake_create_motion_thumbnail)


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
        await asyncio.gather(*pending)


# --------------------------------------------------------- scheduling


def test_store_motion_event_schedules_exactly_one_clip_build_task(monkeypatch, fake_uploader):
    _standard_motion_mocks(monkeypatch)

    clip_calls = []

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        clip_calls.append((event_id, camera_number, start, end))
        return None

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(3, now, now))

    assert len(clip_calls) == 1, "store_motion_event() must schedule exactly one clip-build task via asyncio.create_task()"


def test_correct_event_id_camera_and_timestamps_are_passed(monkeypatch, fake_uploader):
    _standard_motion_mocks(monkeypatch)

    captured = {}

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        captured["event_id"] = event_id
        captured["camera_number"] = camera_number
        captured["start"] = start
        captured["end"] = end
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    analytics_events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: analytics_events.append(event))

    start = datetime(2026, 9, 2, 15, 0, 0)
    end = datetime(2026, 9, 2, 15, 0, 12)
    asyncio.run(_store_and_drain(4, start, end))

    assert captured["camera_number"] == 4
    assert captured["start"] == start
    assert captured["end"] == end
    # The clip's own event_id must be the SAME id mirrored into
    # analytics_events.json -- upload_motion_event_media() looks the
    # event up there by exact id match.
    assert len(analytics_events) == 1
    assert captured["event_id"] == analytics_events[0]["id"]
    assert analytics_events[0]["event_type"] == "motion"
    assert analytics_events[0]["camera"] == 4
    assert fake_uploader[0]["event_id"] == captured["event_id"]
    assert fake_uploader[0]["camera_number"] == 4
    assert fake_uploader[0]["event_start"] == start
    assert fake_uploader[0]["event_end"] == end


def test_event_ids_are_unchanged_full_uuid_hex_not_truncated(monkeypatch, fake_uploader):
    """event_media_uploader finds the event by exact id match in
    ANALYTICS_EVENTS_FILE -- the id convention itself (uuid4().hex,
    32 chars, matching MotionEventModel.id/motion_events.jsonl/the
    clip filename) must be completely unchanged by this reconciliation."""
    _standard_motion_mocks(monkeypatch)

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return None

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    analytics_events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: analytics_events.append(event))

    now = datetime.now()
    asyncio.run(_store_and_drain(5, now, now))

    assert len(analytics_events[0]["id"]) == 32
    int(analytics_events[0]["id"], 16)  # must be valid hex


# --------------------------------------------------------- upload wiring


def test_successful_clip_triggers_the_real_upload_function(monkeypatch, fake_uploader):
    _standard_motion_mocks(monkeypatch)

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(6, now, now))

    assert len(fake_uploader) == 1
    assert fake_uploader[0]["clip_url"].startswith("/recordings/clips/motion/motion_")
    assert fake_uploader[0]["thumbnail_url"] == "/recordings/media/motion/fake.jpg"


def test_failed_clip_extraction_does_not_call_the_uploader_or_break_persistence(monkeypatch, fake_uploader):
    _standard_motion_mocks(monkeypatch)

    append_motion_event_calls = []
    monkeypatch.setattr(main, "append_motion_event", lambda line: append_motion_event_calls.append(line))

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return None  # e.g. no covering recording yet

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    # Must not raise.
    asyncio.run(_store_and_drain(7, now, now))

    assert fake_uploader == [], "no clip means no upload attempt"
    assert len(append_motion_event_calls) == 1, "motion-event persistence itself must be completely unaffected"


def test_uploader_failure_is_fail_open_and_logged(monkeypatch, capsys):
    _standard_motion_mocks(monkeypatch)

    append_motion_event_calls = []
    monkeypatch.setattr(main, "append_motion_event", lambda line: append_motion_event_calls.append(line))

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    def raising_upload(**kwargs):
        raise RuntimeError("simulated network failure")

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = raising_upload
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)

    now = datetime.now()
    # Must not raise -- motion detection/persistence survives.
    asyncio.run(_store_and_drain(8, now, now))

    assert len(append_motion_event_calls) == 1
    captured = capsys.readouterr()
    assert "media upload failed" in captured.out
    assert "RuntimeError" in captured.out
    assert "simulated network failure" in captured.out


# --------------------------------------------------------- linked_recording / unrelated behavior


def test_linked_recording_is_now_the_optimistic_clip_path_matching_production(monkeypatch, fake_uploader):
    """Production (EC2/Samsung) replaced the old linked_recording_for()
    call with the same optimistic future-clip-path convention
    save_yolo_events()'s own event_clip field already uses -- proven
    by diffing the real production main.py, not assumed."""
    _standard_motion_mocks(monkeypatch)

    linked_recording_for_calls = []
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: linked_recording_for_calls.append(a) or "should-not-be-used")

    async def fake_build_motion_event_clip(*a):
        return None

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    persisted_lines = []
    monkeypatch.setattr(main, "append_motion_event", lambda line: persisted_lines.append(line))

    now = datetime.now()
    asyncio.run(_store_and_drain(9, now, now))

    assert linked_recording_for_calls == [], "linked_recording_for() must no longer be called by store_motion_event()"
    assert len(persisted_lines) == 1
    assert "/recordings/clips/motion/motion_" in persisted_lines[0]
    assert ".mp4" in persisted_lines[0]


def test_motion_alert_logic_is_unaffected(monkeypatch, fake_uploader):
    """The existing in-app alert path (get_alert_rule/alert_rule_is_active/
    append_in_app_alert) must fire exactly as before, independent of
    clip building/upload."""
    rule = mock.Mock(enabled=True, event_types=["motion"], delivery_methods=["in_app"])
    monkeypatch.setattr(main, "get_alert_rule", lambda camera_number: rule)
    monkeypatch.setattr(main, "alert_rule_is_active", lambda rule, start_time: True)
    monkeypatch.setattr(main, "append_motion_event", lambda line: None)
    monkeypatch.setattr(main, "append_analytics_event", lambda event: None)

    async def fake_create_motion_thumbnail(*args, **kwargs):
        return "/recordings/media/motion/fake.jpg"

    monkeypatch.setattr(main, "create_motion_thumbnail", fake_create_motion_thumbnail)

    async def fake_build_motion_event_clip(*a):
        return None

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    alert_calls = []

    # append_in_app_alert() is invoked via asyncio.to_thread(...) in
    # store_motion_event() -- a plain sync callable, not awaited directly.
    monkeypatch.setattr(main, "append_in_app_alert", lambda alert: alert_calls.append(alert))

    now = datetime.now()
    asyncio.run(_store_and_drain(10, now, now))

    assert len(alert_calls) == 1
    assert alert_calls[0]["event_type"] == "motion"
    assert alert_calls[0]["camera"] == 10
