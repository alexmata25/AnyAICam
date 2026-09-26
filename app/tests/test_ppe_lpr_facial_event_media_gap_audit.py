"""Characterization tests for a real gap found during the 2026-09-18
Hybrid event-media audit (docs/hybrid-event-media-dedup-audit.md).

save_yolo_events()'s per-scan primary-class dedup (test_ai_event_clip_
wiring.py) correctly gives only ONE class per scan its own clip/upload,
and register_shared_event_media() correctly lets a correlated Smart
Motion event reuse its base Motion event's already-uploaded clip
(test_shared_event_media_registration.py, test_smart_motion_event_
media_wiring.py) -- both real, live, and confirmed working against the
real five-camera fleet's own data (smart_motion: 6 independent uploads
vs 299 shared registrations in the real dataset).

What this file documents instead: a PPE, LPR ("plate"), or
facial_recognition event created in the SAME scan as the person/vehicle
detection that triggered it is wired to NEITHER of the above mechanisms.
It gets its own independent analytics-history record (correct, and
unchanged by anything here) but currently calls neither
upload_motion_event_media() nor register_shared_event_media() -- so it
never gets a cloud video clip of its own, shared or otherwise, even when
the very same scan's primary AI-classified event (e.g. the person
detection that triggered the PPE check) already has one uploading in the
background. Confirmed against real production data on staging
(customer d75bdbecdd4887de4d2b89a9fcea9092): 613/613 real 'ppe' events
in the sampled window have zero detection_event_media rows.

This is NOT the duplicate-storage risk the audit was originally asked
to check (there is no second S3 PutObject happening for these event
types today -- there is no PutObject at all). It is a complementary,
separately-scoped gap: these event types could safely reuse the primary
clip via the exact register_shared_event_media() mechanism already
proven correct for Smart Motion, at zero additional storage cost, but
that wiring does not exist yet. Fixing it requires widening
appliance_cloud.py's analytics_event_media_shared() authorization
(currently hardcoded to a smart_motion child / motion parent pairing
only) to accept these additional child/parent combinations -- a
multi-tenant authorization boundary change deliberately left for a
dedicated follow-up pass rather than done inside this audit. These
tests exist so that follow-up changes this exact, currently-correct
behavior on purpose, not by accident.

2026-09-26: that follow-up is done for PPE and Facial Recognition (and
People Counting) -- see event_media_sharing.py and
test_event_media_sharing.py / test_analytics_media_reuse_cloud.py; the
PPE test below is now the positive path. LPR ("plate") is unchanged.
"""

import sys
import threading
import time
import types
from datetime import datetime

import numpy as np
import pytest

import main


def _fake_result(class_name: str, *, base_conf: float = 0.9) -> dict:
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    return {
        "detections": [
            {
                "x": 10, "y": 10, "width": 40, "height": 40,
                "class_name": class_name, "confidence": base_conf,
            }
        ],
        "frame": frame,
    }


@pytest.fixture(autouse=True)
def _reset_module_state():
    main.ai_event_clip_windows.clear()
    previous_loop = main._ai_event_media_loop
    yield
    main.ai_event_clip_windows.clear()
    main._ai_event_media_loop = previous_loop


@pytest.fixture
def fake_media_pipeline(monkeypatch):
    """Records every call to BOTH real sharing/upload entry points, so a
    test can assert on exactly which ones fire for a given event type."""
    upload_calls = []
    register_calls = []

    def fake_upload_motion_event_media(**kwargs):
        upload_calls.append(kwargs)
        return True

    def fake_register_shared_event_media(**kwargs):
        register_calls.append(kwargs)
        return True

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = fake_upload_motion_event_media
    module.register_shared_event_media = fake_register_shared_event_media
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    return types.SimpleNamespace(upload_calls=upload_calls, register_calls=register_calls)


@pytest.fixture
def background_loop():
    loop = __import__("asyncio").new_event_loop()
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


def test_ppe_event_in_same_scan_shares_that_scans_clip(
    monkeypatch, tmp_path, fake_media_pipeline, background_loop
):
    """A person is detected (this scan's own primary_class_name, so a
    real clip build+upload IS scheduled for it) AND PPE fires on that
    same person crop in the same scan. The gap this audit found is closed
    (2026-09-26, event_media_sharing.py): the PPE event names the person
    event as its media parent and registers the SAME clip via
    register_shared_event_media() once it lands -- no second upload."""
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)
    _standard_mocks(monkeypatch, tmp_path)

    analytics_events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: analytics_events.append(event))

    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda camera_number: True)
    monkeypatch.setattr(
        main.recording_uploader, "_camera_identity",
        lambda camera_number: {"ppe_enabled": True, "smart_motion_enabled": False},
    )
    monkeypatch.setattr(
        main.ppe, "detect_ppe",
        lambda crop, camera_number=None: {
            "hard_hat_present": True, "safety_vest_present": True, "confidence": 0.87,
        },
    )
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda camera_number: False)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera_number: False)

    def worker():
        main.save_yolo_events(170, _fake_result("person"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    person_event = next(e for e in analytics_events if e["event_type"] == "person")
    ppe_event = next(e for e in analytics_events if e["event_type"] == "ppe")
    assert person_event["id"] != ppe_event["id"]

    # The person event's own real upload actually happens (proves this
    # scan genuinely had a clip to share, not merely "nothing fired").
    assert _wait_until(lambda: len(fake_media_pipeline.upload_calls) == 1)
    assert fake_media_pipeline.upload_calls[0]["event_id"] == person_event["id"]

    # ...and the PPE event reuses it, once, naming the person event.
    assert ppe_event["media_parent_event_id"] == person_event["id"]
    assert _wait_until(lambda: len(fake_media_pipeline.register_calls) == 1)
    assert fake_media_pipeline.register_calls == [
        {"event_id": ppe_event["id"], "camera_number": 170, "parent_local_event_id": person_event["id"]}
    ]
    assert len(fake_media_pipeline.upload_calls) == 1


def test_ppe_event_is_suppressed_when_merged_into_the_immediately_prior_window(
    monkeypatch, tmp_path, fake_media_pipeline
):
    """2026-09-22 fix, updated from this test's original characterization
    (a bare 'person' detection whose own scan merged into the immediately-
    prior window used to still get its own independent PPE analytics
    record every time -- confirmed live on real production data: Living
    Room alone produced 1,419 'ppe' events over 11 hours, ~1 every 28s,
    because nothing suppressed a repeat scan of the same person still
    sitting in frame). PPE now reuses the exact same is_duplicate signal
    that already gives the primary AI-classified event's own Hybrid clip
    build "one per real continuous event" semantics -- a merged/duplicate
    scan now creates NO new PPE event at all, not just no media call."""
    monkeypatch.setattr(main, "_ai_event_media_loop", None)
    _standard_mocks(monkeypatch, tmp_path)

    analytics_events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: analytics_events.append(event))

    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda camera_number: True)
    monkeypatch.setattr(
        main.recording_uploader, "_camera_identity",
        lambda camera_number: {"ppe_enabled": True, "smart_motion_enabled": False},
    )
    monkeypatch.setattr(
        main.ppe, "detect_ppe",
        lambda crop, camera_number=None: {
            "hard_hat_present": False, "safety_vest_present": False, "confidence": 0.6,
        },
    )
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda camera_number: False)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera_number: False)

    # Pre-seed this camera's clip window so this scan's own detection is
    # treated as a duplicate/merge of an already-covered window -- no new
    # clip build/upload is scheduled for it at all.
    from event_clips import compute_clip_window
    now = datetime.now()
    main.ai_event_clip_windows[171] = compute_clip_window(now, now)

    def worker():
        main.save_yolo_events(171, _fake_result("person"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert not any(e["event_type"] == "ppe" for e in analytics_events), (
        "a scan merged into the immediately-prior window must not create "
        "a second PPE event for the same continuous real-world presence"
    )
    assert fake_media_pipeline.upload_calls == []
    assert fake_media_pipeline.register_calls == []


def test_two_real_consecutive_scans_of_the_same_person_produce_one_ppe_event(
    monkeypatch, tmp_path, fake_media_pipeline
):
    """Direct reproduction of the real production symptom (1,419 'ppe'
    events on one camera over 11 hours): two genuine, independent
    save_yolo_events() calls a moment apart for the same camera -- no
    manual clip-window pre-seeding this time, just two real calls close
    enough in wall-clock time to be should_merge()-classified as the same
    continuous presence -- must produce exactly one PPE analytics event,
    not two."""
    monkeypatch.setattr(main, "_ai_event_media_loop", None)
    _standard_mocks(monkeypatch, tmp_path)

    analytics_events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: analytics_events.append(event))
    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda camera_number: True)
    monkeypatch.setattr(
        main.recording_uploader, "_camera_identity",
        lambda camera_number: {"ppe_enabled": True, "smart_motion_enabled": False},
    )
    monkeypatch.setattr(
        main.ppe, "detect_ppe",
        lambda crop, camera_number=None: {
            "hard_hat_present": False, "safety_vest_present": False, "confidence": 0.6,
        },
    )
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda camera_number: False)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera_number: False)

    main.save_yolo_events(173, _fake_result("person"))
    main.save_yolo_events(173, _fake_result("person"))  # a moment later -- same real presence

    ppe_events = [e for e in analytics_events if e["event_type"] == "ppe"]
    assert len(ppe_events) == 1, (
        f"expected exactly one PPE event for one continuous presence, got {len(ppe_events)}"
    )
