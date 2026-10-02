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
    # Timestamp names like real segments: retention never touches a
    # camera's newest file (still being written), so a third, newest one
    # sits after the two under test.
    old_recording = camera_folder / "camera1_2026-01-01_00-00-00.mkv"
    current_recording = camera_folder / "camera1_2026-01-02_00-00-00.mkv"
    (camera_folder / "camera1_2026-01-03_00-00-00.mkv").write_bytes(b"live")
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

    # Retention walks only cameras registered in the database, so each
    # deletion also drops its Playback catalog row.
    from database_backend import override_target
    from partner_db import connection, initialize_database
    with override_target(sqlite_path=tmp_path / "retention.db"):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','2026-01-01')")
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('c1','p1','C','c1@example.com','active','2026-01-01')")
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s1','c1','Main','2026-01-01')")
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('a1','c1','s1','AIC-1','2026-01-01')")
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,created_at) VALUES('cam1','c1','s1','a1','Camera 1',1,'2026-01-01')")
        main.delete_expired_recordings()

    assert not old_recording.exists()
    assert current_recording.exists()
    assert not old_thumbnail.exists()
    assert current_thumbnail.exists()
    assert main.load_motion_events() == [current_event]
