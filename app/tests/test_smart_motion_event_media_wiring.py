"""Smart Motion's own event-media wiring (2026-09-14, Phase A revision
after independent security review).

Round 1 (commit 3d54556) gave a correlated Smart Motion event its own
independent event-media by independently re-running build_motion_event_
clip() + upload_motion_event_media() a second time, keyed by its own
event id. That worked, but real production validation surfaced a real
performance cost: a correlated Smart Motion event's own window is
*always* identical to its base Motion event's (camera_number, start_
time, end_time) by construction, so the second encode was pure,
measured waste -- confirmed live: both independently-built clips were
byte-for-byte identical.

Round 2 (commit 818f07f) had the Smart Motion task await the base
Motion event's own media task and forward its resulting s3_key/
thumbnail_s3_key/timing/duration/size to a new registration-only
function. Real production validation of THAT design hit a real,
repeated HTTP 403: the cloud's pre-existing anti-spoofing check
requires a submitted key to be derived from the REGISTERING event's
own id, which a shared key by definition never is. An independent
security review (Codex) confirmed the root cause and required a
narrower fix.

Round 3 (this file, Phase A): the Smart Motion media task still awaits
clip_task (the base Motion event's own already-scheduled media task) --
now purely as a "did the base event's own media actually succeed"
signal -- and calls register_shared_event_media() with ONLY the base
Motion event's own LOCAL id (`parent_local_event_id`) -- never any S3
key, timing, duration, or size. The cloud independently re-resolves
that id (established once, immutably, at analytics-ingestion time),
verifies the full ownership chain, and derives the approved clip/
thumbnail/metadata itself from the parent's own already-registered
media (see appliance_cloud.py's analytics_event_media_shared()).

Ownership stays fully independent: base Motion and Smart Motion keep
their own analytics events, their own event ids, and their own
detection_event_media rows -- which now intentionally reference the
SAME s3_key/thumbnail_s3_key (via the new source_media_id provenance
column) rather than two independently-uploaded copies of identical
bytes.
"""

import asyncio
import sys
import types
from datetime import datetime
from unittest import mock

import pytest

import main


@pytest.fixture
def fake_media_pipeline(monkeypatch):
    """event_media_uploader.py exists only on the physical appliance --
    injects a fake module under that exact import name so both real call
    sites resolve to real, call-recording stand-ins: `upload_motion_
    event_media` (the base Motion event's own path, unchanged) and
    `register_shared_event_media` (the parent-id-only path a correlated
    Smart Motion event now uses instead of a second encode/upload). The
    fake upload mirrors the real function's own contract: shared_media_
    out is populated only on a successful "upload" -- main.py only ever
    checks this dict's truthiness (as the "did the base event succeed"
    signal), it never reads any specific key out of it anymore."""
    upload_calls = []
    register_calls = []

    def fake_upload_motion_event_media(*, shared_media_out=None, **kwargs):
        upload_calls.append(kwargs)
        if shared_media_out is not None:
            shared_media_out.update({"ok": True})
        return True

    def fake_register_shared_event_media(**kwargs):
        register_calls.append(kwargs)
        return True

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = fake_upload_motion_event_media
    module.register_shared_event_media = fake_register_shared_event_media
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    return types.SimpleNamespace(upload_calls=upload_calls, register_calls=register_calls)


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


# --------------------------------------------------------- shared encode/upload, exactly once


def test_exactly_one_build_motion_event_clip_call_for_a_motion_plus_smart_motion_pair(monkeypatch, fake_media_pipeline):
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

    # Exactly one physical encode for the pair -- never a second one for
    # Smart Motion's own id.
    assert clip_calls == [motion_event["id"]]
    assert smart_event["id"] not in clip_calls


def test_exactly_one_physical_upload_for_the_shared_artifact(monkeypatch, fake_media_pipeline):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(2, now, now))

    smart_event = _by_event_type(analytics_events, "smart_motion")

    # Exactly one real S3 upload (the base Motion event's own) -- Smart
    # Motion's own media reaches the pipeline via exactly one
    # registration-only call, never a second upload.
    assert len(fake_media_pipeline.upload_calls) == 1
    assert len(fake_media_pipeline.register_calls) == 1
    assert fake_media_pipeline.register_calls[0]["event_id"] == smart_event["id"]


# --------------------------------------------------------- independent ownership, parent-id-only


def test_motion_and_smart_motion_retain_distinct_event_ids_and_media_ownership(monkeypatch, fake_media_pipeline):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(3, now, now))

    motion_event = _by_event_type(analytics_events, "motion")
    smart_event = _by_event_type(analytics_events, "smart_motion")

    assert motion_event["id"] != smart_event["id"]
    assert smart_event["motion_event_id"] == motion_event["id"]

    # The base Motion event owns the real upload call under its own id;
    # Smart Motion owns a separate registration call under its own,
    # different id -- two independent ownership records, never merged.
    assert fake_media_pipeline.upload_calls[0]["event_id"] == motion_event["id"]
    assert fake_media_pipeline.register_calls[0]["event_id"] == smart_event["id"]


def test_shared_registration_sends_only_the_parent_local_event_id_never_a_storage_key(monkeypatch, fake_media_pipeline):
    """The requirement this whole redesign exists to satisfy: the
    appliance sends ONLY the base Motion event's own LOCAL id -- never
    an S3 key, bucket, timing, duration, or size. The cloud derives
    every approved value itself from the verified parent's own media.
    A regression that starts forwarding any storage field again (the
    exact shape that produced the real HTTP 403 in production) would
    be caught here by the exact-keys assertion."""
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "vehicle")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(4, now, now))

    motion_event = _by_event_type(analytics_events, "motion")

    register_call = fake_media_pipeline.register_calls[0]
    assert set(register_call.keys()) == {"event_id", "camera_number", "parent_local_event_id"}
    assert register_call["parent_local_event_id"] == motion_event["id"]


def test_correct_camera_passed_into_the_shared_registration(monkeypatch, fake_media_pipeline):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "vehicle")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    start = datetime(2026, 9, 14, 9, 30, 0)
    end = datetime(2026, 9, 14, 9, 30, 12)
    asyncio.run(_store_and_drain(5, start, end))

    register_call = fake_media_pipeline.register_calls[0]
    assert register_call["camera_number"] == 5


def test_no_duplicate_smart_motion_media_scheduling_for_a_single_event(monkeypatch, fake_media_pipeline):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(6, now, now))

    smart_event = _by_event_type(analytics_events, "smart_motion")

    smart_registrations = [c for c in fake_media_pipeline.register_calls if c["event_id"] == smart_event["id"]]
    assert len(smart_registrations) == 1


def test_smart_motion_media_scheduling_does_not_disturb_yolo_correlation_state(monkeypatch, fake_media_pipeline):
    """Smart Motion's own media task must not call into save_yolo_events()
    or record_object_detection() at all."""
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "vehicle")

    yolo_calls = []
    monkeypatch.setattr(main, "save_yolo_events", lambda *a, **k: yolo_calls.append((a, k)) or [])
    monkeypatch.setattr(main.smart_motion, "record_object_detection", lambda *a, **k: yolo_calls.append((a, k)))

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(7, now, now))

    assert yolo_calls == []
    assert _by_event_type(analytics_events, "smart_motion") is not None


# --------------------------------------------------------- safe failure behavior: no fallback encode


def test_base_motion_clip_build_failure_preserves_both_events_and_never_triggers_a_smart_motion_encode(monkeypatch, fake_media_pipeline):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    clip_calls = []

    async def failing_build_motion_event_clip(event_id, camera_number, start, end):
        clip_calls.append(event_id)
        raise RuntimeError("simulated clip build failure")

    monkeypatch.setattr(main, "build_motion_event_clip", failing_build_motion_event_clip)

    now = datetime.now()
    # Must not raise.
    asyncio.run(_store_and_drain(8, now, now))

    motion_event = _by_event_type(analytics_events, "motion")
    smart_event = _by_event_type(analytics_events, "smart_motion")
    assert smart_event["triggered_by"] == "person"

    # build_motion_event_clip() was attempted exactly once -- for the
    # base Motion event -- never a second time as a Smart Motion
    # fallback encode.
    assert clip_calls == [motion_event["id"]]
    assert fake_media_pipeline.upload_calls == []
    assert fake_media_pipeline.register_calls == [], (
        "a failed base Motion clip build must never reach the shared-"
        "media registration path for Smart Motion"
    )


def test_base_motion_upload_failure_preserves_both_events_and_never_triggers_a_smart_motion_encode(monkeypatch, capsys):
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, "person")

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    register_calls = []

    def raising_upload(*, shared_media_out=None, **kwargs):
        # Real upload_motion_event_media() never populates shared_media_out
        # on a failure path -- this fake mirrors that exactly.
        raise RuntimeError("simulated network failure")

    def fake_register_shared_event_media(**kwargs):
        register_calls.append(kwargs)
        return True

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = raising_upload
    module.register_shared_event_media = fake_register_shared_event_media
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)

    now = datetime.now()
    # Must not raise -- both the base Motion and Smart Motion events survive.
    asyncio.run(_store_and_drain(9, now, now))

    assert _by_event_type(analytics_events, "motion") is not None
    smart_event = _by_event_type(analytics_events, "smart_motion")
    assert smart_event["triggered_by"] == "person"

    # No independent Smart Motion re-encode/registration was attempted.
    assert register_calls == []

    captured = capsys.readouterr()
    assert "media upload failed" in captured.out
    assert "RuntimeError" in captured.out
    assert "simulated network failure" in captured.out
    assert f"Smart Motion event {smart_event['id']}: no shared media available" in captured.out


def test_smart_motion_scheduling_failure_does_not_prevent_smart_motion_event_creation(monkeypatch, fake_media_pipeline, capsys):
    """Exercises the scheduling-level guard directly: even if
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
    asyncio.run(_store_and_drain(10, now, now))

    smart_event = _by_event_type(analytics_events, "smart_motion")
    assert smart_event["triggered_by"] == "person"

    captured = capsys.readouterr()
    assert "could not schedule" in captured.out
    assert "RuntimeError" in captured.out
    assert "simulated scheduling failure" in captured.out


# --------------------------------------------------------- no classification -> unaffected


def test_no_classification_retains_existing_base_motion_behavior(monkeypatch, fake_media_pipeline):
    """classify_motion() returning falsy (the existing, already-tested
    default in every test_motion_event_media_wiring.py case) must
    continue to schedule only the base Motion event's own encode and
    upload -- zero Smart Motion analytics events, zero registration
    calls, base Motion's own pipeline completely untouched by any of
    this change."""
    analytics_events = _standard_motion_mocks(monkeypatch)
    _classify_as(monkeypatch, None)

    clip_calls = []

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        clip_calls.append(event_id)
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    now = datetime.now()
    asyncio.run(_store_and_drain(11, now, now))

    assert [event["event_type"] for event in analytics_events] == ["motion"]
    assert len(clip_calls) == 1
    assert len(fake_media_pipeline.upload_calls) == 1
    assert fake_media_pipeline.register_calls == []
