"""Event clips and AI snapshots follow RETENTION_DAYS (2026-10-01).

Live on the Ryzen: clips/motion/ and media/ai/ were never pruned -- 18 days
held 190 GB, more than the recordings themselves -- so a full disk would
have made the storage manager delete recordings to make room for them.
"""
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import event_media_outbox
import main


@pytest.fixture()
def root(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", root)
    monkeypatch.setattr(event_media_outbox, "OUTBOX_FILE", tmp_path / "outbox.json")
    return root


def _file(path: Path, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    mtime = (datetime.now() - timedelta(days=age_days)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def test_expired_clips_and_snapshots_go_recent_ones_and_exports_stay(root):
    old_clip = _file(root / "clips" / "motion" / "motion_old.mp4", 9)
    new_clip = _file(root / "clips" / "motion" / "motion_new.mp4", 1)
    export = _file(root / "clips" / "camera1_export.mp4", 30)  # a customer's own export
    old_snap = _file(root / "media" / "ai" / "2026-09-20" / "person_old.jpg", 9)
    new_snap = _file(root / "media" / "ai" / "2026-09-30" / "person_new.jpg", 1)
    recording = _file(root / "camera1" / "camera1_2026-09-30_00-00-00.mkv", 1)

    main.delete_expired_event_media(datetime.now() - timedelta(days=7))

    assert not old_clip.exists() and not old_snap.exists()
    assert not (root / "media" / "ai" / "2026-09-20").exists()  # emptied day folder removed
    assert new_clip.exists() and new_snap.exists() and export.exists() and recording.exists()


def test_a_pending_upload_of_a_deleted_clip_leaves_the_outbox(root):
    _file(root / "clips" / "motion" / "motion_gone.mp4", 9)
    _file(root / "clips" / "motion" / "motion_kept.mp4", 1)
    for event_id in ("gone", "kept"):
        event_media_outbox.put({"event_id": event_id, "camera_number": 1,
                                "clip_url": f"/recordings/clips/motion/motion_{event_id}.mp4"})
    event_media_outbox.put({"event_id": "card", "camera_number": 1, "kind": "shared", "parent_local_event_id": "gone"})

    main.delete_expired_event_media(datetime.now() - timedelta(days=7))

    assert sorted(job["event_id"] for job in event_media_outbox.load()) == ["card", "kept"]


def test_the_hourly_retention_pass_includes_event_media(root, monkeypatch):
    monkeypatch.setattr(main, "MOTION_EVENTS_FILE", root / "motion_events.jsonl")
    monkeypatch.setattr(main, "MOTION_THUMBNAILS_FOLDER", root / "media" / "motion")
    monkeypatch.setattr(main, "RETENTION_DAYS", 7)
    calls = []
    monkeypatch.setattr(main, "delete_expired_event_media", lambda cutoff: calls.append(cutoff))
    from database_backend import override_target
    from partner_db import initialize_database
    with override_target(sqlite_path=str(root.parent / "retention.db")):
        initialize_database()
        main.delete_expired_recordings()
    assert len(calls) == 1
    assert abs((datetime.now() - timedelta(days=7) - calls[0]).total_seconds()) < 60


def test_nothing_happens_without_the_folders(root):
    main.delete_expired_event_media(datetime.now())
    assert list(root.iterdir()) == []
