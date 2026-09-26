"""Appliance side of PPE / Facial Recognition / People Counting media reuse
(2026-09-26): which clip a result is linked to (event_media_sharing.py),
how that link reaches the cloud (analytics_sync.py), and the wiring in
save_yolo_events() / the People Counting path (main.py)."""

import asyncio
import sys
import threading
import time
import types
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np
import pytest

import analytics_sync
import event_media_outbox
import event_media_sharing
import main
from event_clips import compute_clip_window

T0 = datetime(2026, 9, 26, 10, 0, 0)


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(event_media_outbox, "OUTBOX_FILE", tmp_path / "outbox.json")
    event_media_sharing.owners.reset()
    main.ai_event_clip_windows.clear()
    previous_loop = main._ai_event_media_loop
    yield
    event_media_sharing.owners.reset()
    main.ai_event_clip_windows.clear()
    main._ai_event_media_loop = previous_loop


@pytest.fixture
def pipeline(monkeypatch):
    """Stand-in for event_media_uploader recording both entry points."""
    calls = types.SimpleNamespace(uploads=[], shared=[])
    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = lambda **kwargs: calls.uploads.append(kwargs) or True
    module.register_shared_event_media = lambda **kwargs: calls.shared.append(kwargs) or True
    module.EVENT_MEDIA_CAPTURE_ENABLED = False
    module.EVENT_MEDIA_UPLOAD_ENABLED = True
    module.RETRY_SECONDS = 120
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    return calls


def _owner(camera, owner_id, at):
    window = compute_clip_window(at, at)
    event_media_sharing.owners.register(camera, owner_id, window.start, window.end)


# ------------------------------------------------ which clip covers a moment

def test_same_camera_same_moment_is_covered_by_its_clip_window_only():
    _owner(1, "owner", T0)
    owners = event_media_sharing.owners
    assert owners.covering(1, T0) == "owner"
    assert owners.covering(1, T0 - timedelta(seconds=5)) == "owner"   # pre-roll edge
    assert owners.covering(1, T0 + timedelta(seconds=5)) == "owner"   # post-roll edge
    assert owners.covering(1, T0 + timedelta(seconds=5, milliseconds=1)) is None
    assert owners.covering(1, T0 - timedelta(seconds=5, milliseconds=1)) is None


def test_never_across_cameras():
    _owner(1, "owner", T0)
    assert event_media_sharing.owners.covering(2, T0) is None


def test_ambiguous_nearby_clips_resolve_to_the_one_containing_the_moment():
    _owner(1, "first", T0)
    _owner(1, "second", T0 + timedelta(seconds=20))
    owners = event_media_sharing.owners
    assert owners.covering(1, T0 + timedelta(seconds=3)) == "first"
    assert owners.covering(1, T0 + timedelta(seconds=22)) == "second"
    assert owners.covering(1, T0 + timedelta(seconds=10)) is None  # between the two clips: neither recorded it
    # Overlapping windows (a Facial Recognition clip inside a YOLO clip): the newest, both truthful.
    _owner(1, "overlap", T0 + timedelta(seconds=24))
    assert owners.covering(1, T0 + timedelta(seconds=22)) == "overlap"


def test_a_failed_clip_covers_nothing():
    _owner(1, "owner", T0)
    event_media_sharing.owners.finish("owner", False)
    assert event_media_sharing.owners.covering(1, T0) is None


def test_old_owners_are_pruned_once_finished():
    owners = event_media_sharing.owners
    _owner(1, "old-done", T0)
    owners.finish("old-done", True)
    _owner(1, "old-pending", T0)
    _owner(1, "new", T0 + timedelta(seconds=event_media_sharing.KEEP_SECONDS + 30))
    assert owners.covering(1, T0) is None
    assert owners.attach("old-done", "child") == event_media_sharing.FAILED
    # Still uploading: its results are still delivered when it lands, then it is forgotten.
    assert owners.attach("old-pending", "child-2") == event_media_sharing.PENDING
    assert owners.finish("old-pending", True) == ["child-2"]
    assert owners.attach("old-pending", "child-3") == event_media_sharing.FAILED
    for index in range(40):
        _owner(2, f"burst-{index}", T0 + timedelta(seconds=index))
        owners.finish(f"burst-{index}", True)
    assert len(owners._by_id) <= event_media_sharing.MAX_OWNERS_PER_CAMERA + 1


# ------------------------------------------------ delivery once the owner's clip lands

def test_children_wait_for_the_owner_and_are_registered_when_it_lands(pipeline):
    _owner(1, "owner", T0)
    event_media_sharing.attach_child("owner", "ppe-1", 1)
    event_media_sharing.attach_child("owner", "face-1", 1)
    assert pipeline.shared == []  # nothing before the owner's media exists
    event_media_sharing.owner_finished("owner", 1, True)
    assert pipeline.shared == [
        {"event_id": "ppe-1", "camera_number": 1, "parent_local_event_id": "owner"},
        {"event_id": "face-1", "camera_number": 1, "parent_local_event_id": "owner"},
    ]


def test_a_child_after_the_owner_landed_registers_straight_away(pipeline):
    _owner(1, "owner", T0)
    event_media_sharing.owner_finished("owner", 1, True)
    event_media_sharing.attach_child("owner", "face-1", 1)
    assert _wait_until(lambda: pipeline.shared == [{"event_id": "face-1", "camera_number": 1, "parent_local_event_id": "owner"}])


def test_owner_upload_retrying_queues_children_behind_it(pipeline):
    _owner(1, "owner", T0)
    event_media_sharing.attach_child("owner", "ppe-1", 1)
    event_media_outbox.put({"event_id": "owner", "camera_number": 1, "clip_url": "/recordings/x.mp4"})  # the owner's retry job
    event_media_sharing.owner_finished("owner", 1, False)
    jobs = {job["event_id"]: job for job in event_media_outbox.load()}
    assert jobs["ppe-1"]["kind"] == "shared" and jobs["ppe-1"]["parent_local_event_id"] == "owner"
    assert jobs["ppe-1"]["next_attempt_at"]  # after the owner's own retry
    assert pipeline.shared == []


def test_no_matching_media_leaves_the_child_without_media(pipeline):
    _owner(1, "owner", T0)
    event_media_sharing.attach_child("owner", "ppe-1", 1)
    event_media_sharing.owner_finished("owner", 1, False)  # no retry job: the clip will never exist
    assert pipeline.shared == [] and event_media_outbox.load() == []
    event_media_sharing.attach_child("unknown-owner", "face-1", 1)
    time.sleep(0.2)
    assert pipeline.shared == [] and event_media_outbox.load() == []


# ------------------------------------------------ the link on the wire

def test_sync_payload_forwards_the_media_parent_under_the_existing_field():
    assert analytics_sync._build_payload({"id": "ppe-1", "event_type": "ppe", "timestamp": "t", "media_parent_event_id": "owner"})["parent_local_event_id"] == "owner"
    assert analytics_sync._build_payload({"id": "sm", "event_type": "smart_motion", "timestamp": "t", "motion_event_id": "m"})["parent_local_event_id"] == "m"
    assert analytics_sync._build_payload({"id": "x", "event_type": "person", "timestamp": "t"})["parent_local_event_id"] is None
    assert len(analytics_sync._build_payload({"id": "x", "event_type": "ppe", "timestamp": "t", "media_parent_event_id": "o"})) == 7


def test_link_never_points_an_event_at_itself():
    event = {"id": "a"}
    event_media_sharing.link(event, "a")
    event_media_sharing.link(event, None)
    assert "media_parent_event_id" not in event


# ------------------------------------------------ save_yolo_events(): PPE and Facial Recognition

def _person_scan():
    return {"detections": [{"x": 10, "y": 10, "width": 40, "height": 40, "class_name": "person", "confidence": 0.9}],
            "frame": np.zeros((120, 160, 3), dtype=np.uint8)}


@pytest.fixture
def background_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


@pytest.fixture
def scan(monkeypatch, tmp_path, pipeline, background_loop):
    events = []
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_analytics_event", events.append)
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera: {"mode": "off"})
    monkeypatch.setattr(main, "_backfill_ai_event_linked_recording", lambda *a, **k: None, raising=False)

    async def build(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", build)
    identity = {"ppe_enabled": False, "smart_motion_enabled": False, "cloud_recording_mode": "motion"}
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: identity)
    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda camera: identity["ppe_enabled"])
    monkeypatch.setattr(main.ppe, "detect_ppe", lambda crop, camera_number=None: {"hard_hat_present": False, "safety_vest_present": True, "confidence": 0.8})
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera: False)
    faces = {"on": False, "next": 0}
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda camera: faces["on"])

    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr("database_backend.connect", fake_connect)

    def record_facial_events(db, **kwargs):
        faces["next"] += 1
        return [{"id": f"fevt_{faces['next']}", "confidence": 0.9, "match_state": "recognized", "matched_person_id": "p",
                 "matched_person_name": "Ana", "matched_watchlist_id": None, "matched_watchlist_name": None,
                 "engine": "t", "engine_version": "1"}]

    monkeypatch.setattr(main.facial_events, "record_facial_events", record_facial_events)
    monkeypatch.setattr(main.aac_voice_call_events if hasattr(main, "aac_voice_call_events") else main, "is_entrance_camera", lambda *a, **k: False, raising=False)

    def run(camera=7, at=None):
        if at is not None:
            monkeypatch.setattr(main, "datetime", types.SimpleNamespace(now=lambda: at, fromisoformat=datetime.fromisoformat))
        done = threading.Event()
        threading.Thread(target=lambda: (main.save_yolo_events(camera, _person_scan()), done.set())).start()
        assert done.wait(5)
        return events

    return types.SimpleNamespace(run=run, events=events, identity=identity, faces=faces, calls=pipeline)


def _of(events, event_type):
    return [e for e in events if e["event_type"] == event_type]


def test_ppe_shows_its_own_scans_clip(scan):
    scan.identity["ppe_enabled"] = True
    events = scan.run()
    person, ppe_event = _of(events, "person")[0], _of(events, "ppe")[0]
    assert ppe_event["media_parent_event_id"] == person["id"]
    assert _wait_until(lambda: len(scan.calls.shared) == 1)
    assert [u["event_id"] for u in scan.calls.uploads] == [person["id"]]  # one clip, not two
    assert scan.calls.shared == [{"event_id": ppe_event["id"], "camera_number": 7, "parent_local_event_id": person["id"]}]


def test_facial_recognition_in_the_clip_building_scan_reuses_it(scan):
    scan.faces["on"] = True
    events = scan.run()
    person, face = _of(events, "person")[0], _of(events, "facial_recognition")[0]
    assert face["media_parent_event_id"] == person["id"]
    assert _wait_until(lambda: len(scan.calls.shared) == 1)
    assert [u["event_id"] for u in scan.calls.uploads] == [person["id"]]


def test_facial_recognition_later_in_the_same_clip_window_reuses_it(scan):
    scan.run(at=T0)                                  # person only: builds the clip
    scan.faces["on"] = True
    events = scan.run(at=T0 + timedelta(seconds=4))  # merged scan (no clip), still inside T0's window
    person, face = _of(events, "person")[0], _of(events, "facial_recognition")[0]
    assert face["media_parent_event_id"] == person["id"]
    assert _wait_until(lambda: len(scan.calls.shared) == 1)
    assert len(scan.calls.uploads) == 1


def test_facial_recognition_outside_any_clip_gets_a_clip_of_its_own(scan):
    scan.run(at=T0)
    scan.faces["on"] = True
    events = scan.run(at=T0 + timedelta(seconds=7))  # merged (no new person clip) but past T0's clip
    face = _of(events, "facial_recognition")[0]
    assert "media_parent_event_id" not in face
    assert _wait_until(lambda: len(scan.calls.uploads) == 2)
    own = scan.calls.uploads[1]
    assert own["event_id"] == face["id"] and own["event_start"] == T0 + timedelta(seconds=7)
    assert own["thumbnail_url"] and own["already_classified"] is True
    assert scan.calls.shared == []


def test_facial_recognition_builds_nothing_when_the_camera_cannot_keep_media(scan):
    scan.run(at=T0)
    scan.faces["on"] = True
    scan.identity["cloud_recording_mode"] = "continuous"  # not Hybrid: the upload would be refused
    events = scan.run(at=T0 + timedelta(seconds=7))  # merged scan, past T0's clip
    face = _of(events, "facial_recognition")[0]
    assert "media_parent_event_id" not in face
    time.sleep(0.3)
    assert [u["event_id"] for u in scan.calls.uploads] == [_of(events, "person")[0]["id"]]


def test_facial_recognition_on_another_camera_does_not_reuse_this_cameras_clip(scan):
    scan.run(camera=7, at=T0)
    scan.faces["on"] = True
    events = scan.run(camera=8, at=T0 + timedelta(seconds=1))
    face = [e for e in _of(events, "facial_recognition") if e["camera"] == 8][0]
    person8 = [e for e in _of(events, "person") if e["camera"] == 8][0]
    assert face["media_parent_event_id"] == person8["id"]  # camera 8's own clip, never camera 7's


# ------------------------------------------------ People Counting

def test_people_counting_reuses_a_covering_clip_or_owns_one(pipeline, background_loop, monkeypatch):
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: {"cloud_recording_mode": "motion"})
    _owner(3, "person-owner", T0)
    assert main._analytics_media_owner(3, "count-1", T0 + timedelta(seconds=2)) == "person-owner"
    # Nothing covers the crossing: it owns a clip of its own, and a second crossing in the same frame reuses it.
    moment = T0 + timedelta(seconds=40)
    assert main._analytics_media_owner(3, "count-2", moment) == "count-2"
    assert main._analytics_media_owner(3, "count-3", moment) == "count-2"
    # Not on another camera.
    assert main._analytics_media_owner(4, "count-4", T0) == "count-4"


def test_people_counting_owned_clip_is_built_uploaded_and_shared(pipeline, monkeypatch):
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: {"cloud_recording_mode": "motion"})

    async def build(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", build)

    async def crossing():  # people_counting_worker() runs on the event loop
        owner = main._analytics_media_owner(3, "count-1", T0)
        main._schedule_owned_analytics_clip("count-1", 3, T0, "/recordings/media/ai/x.jpg")
        second = main._analytics_media_owner(3, "count-2", T0)
        event_media_sharing.attach_child(second, "count-2", 3)
        await asyncio.sleep(0.3)
        return owner, second

    owner, second = asyncio.run(crossing())
    assert owner == second == "count-1"
    assert [u["event_id"] for u in pipeline.uploads] == ["count-1"]
    assert _wait_until(lambda: pipeline.shared == [{"event_id": "count-2", "camera_number": 3, "parent_local_event_id": "count-1"}])


def test_people_counting_without_media_capture_builds_nothing(pipeline, monkeypatch):
    sys.modules["event_media_uploader"].EVENT_MEDIA_UPLOAD_ENABLED = False
    assert main._analytics_media_owner(3, "count-1", T0) is None
