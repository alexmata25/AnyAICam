"""Regression coverage for the local recording-retention worker."""

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta

import main


def test_retention_worker_does_not_block_health_or_the_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_retention_scan():
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(main, "delete_expired_recordings", slow_retention_scan)

    async def exercise_worker():
        before = time.monotonic()
        worker = asyncio.create_task(main.retention_worker())
        # This prevents an old synchronous implementation from hanging the
        # suite forever; it would still fail the latency assertion below.
        fallback_release = threading.Timer(1.0, release.set)
        fallback_release.start()
        try:
            deadline = time.monotonic() + 0.5
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert started.is_set(), "retention scan did not start"
            assert not release.is_set(), "event loop resumed only after retention completed"

            response = main.health_endpoint()
            await asyncio.sleep(0)
            elapsed = time.monotonic() - before

            assert response.status_code == 200
            health = json.loads(response.body)
            assert health["status"] == "ok"
            assert health["service"] == "AnyAiCam VMS"
            assert health["version"] == main.APP_VERSION
            assert elapsed < 0.25, "retention work blocked the FastAPI event loop"
        finally:
            release.set()
            fallback_release.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    asyncio.run(exercise_worker())


def test_delete_expired_recordings_preserves_retention_policy(monkeypatch, tmp_path):
    recordings = tmp_path / "recordings"
    thumbnails = recordings / "media" / "motion"
    camera_folder = recordings / "camera1"
    thumbnails.mkdir(parents=True)
    camera_folder.mkdir(parents=True)

    motion_events = recordings / "motion_events.jsonl"
    old_recording = camera_folder / "old.mkv"
    current_recording = camera_folder / "current.mkv"
    old_thumbnail = thumbnails / "old.jpg"
    current_thumbnail = thumbnails / "current.jpg"
    for path in (old_recording, current_recording, old_thumbnail, current_thumbnail):
        path.write_bytes(b"test")

    now = datetime.now()
    old_time = now - timedelta(days=8)
    current_time = now - timedelta(days=6)
    for path in (old_recording, old_thumbnail):
        os.utime(path, (old_time.timestamp(), old_time.timestamp()))
    for path in (current_recording, current_thumbnail):
        os.utime(path, (current_time.timestamp(), current_time.timestamp()))

    current_event = {"id": "current", "start_time": current_time.isoformat()}
    old_event = {"id": "old", "start_time": old_time.isoformat()}
    malformed_event = {"id": "malformed", "start_time": "not-a-date"}
    motion_events.write_text(
        "\n".join(json.dumps(item) for item in (old_event, current_event, malformed_event)) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(main, "RETENTION_DAYS", 7)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings)
    monkeypatch.setattr(main, "MOTION_THUMBNAILS_FOLDER", thumbnails)
    monkeypatch.setattr(main, "MOTION_EVENTS_FILE", motion_events)

    main.delete_expired_recordings()

    assert not old_recording.exists()
    assert current_recording.exists()
    assert not old_thumbnail.exists()
    assert current_thumbnail.exists()
    assert main.load_motion_events() == [current_event]
