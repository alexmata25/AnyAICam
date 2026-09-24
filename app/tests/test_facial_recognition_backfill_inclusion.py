"""Regression coverage for a real gap found by careful line-by-line
review of every `linked_recording=` call site in save_yolo_events()
(2026-09-22): the facial_recognition branch resolves the same
too-early `linked_recording` snapshot as every other AI-classification
event type (see test_ai_event_linked_recording_backfill.py's own
docstring for the full root cause), but its events are appended
directly via append_analytics_event() rather than added to
saved_events -- so the original backfill fix (which only collected ids
from saved_events) silently never scheduled a backfill for any
facial_recognition event at all. Fixed by tracking a separate
facial_event_ids list and including it in backfill_event_ids alongside
saved_events' own ids.
"""
import asyncio
import sys
import threading
import time
import types
from contextlib import contextmanager

import numpy as np
import pytest

import main


def _fake_result(*class_names: str) -> dict:
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    detections = [
        {"x": 10, "y": 10, "width": 40, "height": 40, "class_name": name, "confidence": 0.9}
        for name in class_names
    ]
    return {"detections": detections, "frame": frame}


@pytest.fixture(autouse=True)
def _reset_module_state():
    main.ai_event_clip_windows.clear()
    previous_loop = main._ai_event_media_loop
    yield
    main.ai_event_clip_windows.clear()
    main._ai_event_media_loop = previous_loop


@pytest.fixture
def background_loop():
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


@pytest.fixture
def facial_recognition_fires_one_match(monkeypatch):
    """Camera has facial recognition enabled and one already-completed
    match this scan -- the exact shape record_facial_events() returns
    for a real recognized (or unknown-logged) face. The real DB
    connection record_facial_events() would normally need is bypassed
    entirely: this fixture replaces record_facial_events() itself with
    a canned result, so the surrounding `with aac_connect() as aac_db`
    just needs a harmless no-op connection."""
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda camera_number: True)

    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr("database_backend.connect", fake_connect)

    monkeypatch.setattr(
        main.facial_events, "record_facial_events",
        lambda db, *, camera_number, appliance_id, person_crop_bgr, now, relay_provider=None: [
            {
                "id": "aac-evt-1",
                "confidence": 0.95,
                "match_state": "recognized",
                "matched_person_id": "person-1",
                "matched_person_name": "Test Person",
                "matched_watchlist_id": None,
                "matched_watchlist_name": None,
                "engine": "test-engine",
                "engine_version": "1.0",
            }
        ],
    )
    monkeypatch.setattr(main.ppe, "is_camera_enabled", lambda camera_number: False)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera_number: False)


def test_facial_recognition_event_id_is_included_in_the_backfill(
    monkeypatch, tmp_path, facial_recognition_fires_one_match, background_loop
):
    monkeypatch.setattr(main, "_ai_event_media_loop", background_loop)
    monkeypatch.setattr(main, "_local_recording_settings", lambda camera_number: {"mode": "event"})
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    monkeypatch.setattr(main, "persist_event_recording", lambda *a, **k: asyncio.sleep(0))

    async def fake_build_motion_event_clip(event_id, camera_number, start, end):
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    monkeypatch.setattr(main, "build_motion_event_clip", fake_build_motion_event_clip)

    recorded = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: recorded.append(event))

    backfill_calls = []

    async def fake_backfill(camera_number, event_ids, event_time):
        backfill_calls.append((camera_number, event_ids, event_time))

    monkeypatch.setattr(main, "_backfill_ai_event_linked_recording", fake_backfill)

    main.save_yolo_events(190, _fake_result("person"))

    assert any(event["id"] == "aac-evt-1" for event in recorded), \
        "the facial_recognition event must still be appended via append_analytics_event()"
    assert _wait_until(lambda: len(backfill_calls) == 1), \
        "a backfill must be scheduled at all for a camera with a facial_recognition match"
    camera_number, event_ids, _event_time = backfill_calls[0]
    assert camera_number == 190
    assert "aac-evt-1" in event_ids, (
        "the facial_recognition event's id must be included in the backfill batch -- "
        f"got {event_ids!r}"
    )
