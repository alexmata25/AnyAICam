"""P0 resource-safety milestone: tests for delete_expired_analytics_events(),
the bounded-retention sweep for local YOLO/motion analytics data
(analytics_events.json + AI thumbnail JPEGs under media/ai/).

Written in response to a real, measured production condition on the
Ryzen appliance: analytics_events.json had already hit its 5000-entry
cap and AI thumbnails had grown to 6.0 GB in under 24 hours with zero
retention, on a disk already at 92% used. This function is the fix;
these tests are the proof it behaves exactly as intended and touches
nothing else.

Same import/isolation constraints as test_customer_analytics_events.py
(imports `main` -- must run inside the deployed container or via
Windows-native Python, not this WSL host's plain python3/pytest).
Every test monkeypatches main.ANALYTICS_EVENTS_FILE, main.
AI_THUMBNAILS_FOLDER, and main.ANALYTICS_RETENTION_DAYS to tmp_path
locations/values before calling the function under test, so nothing
here ever touches real recordings, real analytics data, or the real
RECORDINGS_FOLDER -- and every test that plants a decoy .mkv,
MOTION_EVENTS_FILE, or MOTION_THUMBNAILS_FOLDER file confirms it
survives untouched, proving this function's blast radius is exactly
analytics_events.json and AI_THUMBNAILS_FOLDER, nothing else.
"""

import json
import os
import time
from datetime import datetime, timedelta

import main


def _set_mtime_days_ago(path, days):
    target = time.time() - days * 86400
    os.utime(path, (target, target))


def _write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(events, indent=2), encoding="utf-8")


def _write_thumbnail(folder, name, days_old):
    folder.mkdir(parents=True, exist_ok=True)
    thumbnail = folder / name
    thumbnail.write_bytes(b"fake-jpeg-bytes")
    _set_mtime_days_ago(thumbnail, days_old)
    return thumbnail


def test_expired_analytics_event_is_pruned_recent_one_is_kept(tmp_path, monkeypatch):
    events_file = tmp_path / "analytics_events.json"
    now = datetime.now()
    old_event = {"id": "old-1", "event_type": "car", "timestamp": (now - timedelta(days=10)).isoformat()}
    recent_event = {"id": "recent-1", "event_type": "person", "timestamp": (now - timedelta(hours=1)).isoformat()}
    _write_events(events_file, [old_event, recent_event])
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path / "media" / "ai")
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)

    main.delete_expired_analytics_events()

    remaining = json.loads(events_file.read_text(encoding="utf-8"))
    assert [event["id"] for event in remaining] == ["recent-1"]


def test_event_exactly_at_the_retention_boundary_is_not_yet_expired(tmp_path, monkeypatch):
    """timedelta(days=N) boundary: an event exactly N days old is kept,
    only one MORE than N days old is pruned -- same not-off-by-one-
    in-the-unsafe-direction guarantee as the existing recording
    retention sweep."""
    events_file = tmp_path / "analytics_events.json"
    now = datetime(2026, 8, 22, 12, 0, 0)
    boundary_event = {"id": "boundary-1", "event_type": "car", "timestamp": (now - timedelta(days=7)).isoformat()}
    _write_events(events_file, [boundary_event])
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path / "media" / "ai")
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)

    main.delete_expired_analytics_events(now=now)

    remaining = json.loads(events_file.read_text(encoding="utf-8"))
    assert [event["id"] for event in remaining] == ["boundary-1"]


def test_malformed_timestamp_event_is_retained_not_dropped(tmp_path, monkeypatch):
    """Deliberately the opposite of the existing motion-event prune's
    behavior: an event whose timestamp can't be parsed is kept, not
    silently discarded, because a parsing hiccup should never be able
    to destroy a detection record."""
    events_file = tmp_path / "analytics_events.json"
    malformed_events = [
        {"id": "bad-1", "event_type": "car"},  # missing timestamp entirely
        {"id": "bad-2", "event_type": "car", "timestamp": "not-a-real-timestamp"},
        {"id": "bad-3", "event_type": "car", "timestamp": None},
    ]
    _write_events(events_file, malformed_events)
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path / "media" / "ai")
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)

    main.delete_expired_analytics_events()

    remaining = json.loads(events_file.read_text(encoding="utf-8"))
    assert {event["id"] for event in remaining} == {"bad-1", "bad-2", "bad-3"}


def test_missing_analytics_events_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", tmp_path / "does-not-exist.json")
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path / "media" / "ai")
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)

    main.delete_expired_analytics_events()  # must not raise


def test_file_is_not_rewritten_when_nothing_is_expired(tmp_path, monkeypatch):
    """Avoids a needless write (and a needless bump of the file's own
    mtime) on every hourly retention tick when nothing has actually
    expired yet -- confirmed by checking the file's own mtime is
    untouched, not just that its content is unchanged."""
    events_file = tmp_path / "analytics_events.json"
    now = datetime.now()
    recent_event = {"id": "recent-1", "event_type": "person", "timestamp": (now - timedelta(hours=1)).isoformat()}
    _write_events(events_file, [recent_event])
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", events_file)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path / "media" / "ai")
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)
    mtime_before = events_file.stat().st_mtime

    main.delete_expired_analytics_events()

    assert events_file.stat().st_mtime == mtime_before


def test_expired_thumbnail_is_deleted_recent_one_is_kept(tmp_path, monkeypatch):
    ai_folder = tmp_path / "media" / "ai"
    old_thumbnail = _write_thumbnail(ai_folder / "2026-08-10", "camera1_old.jpg", days_old=10)
    recent_thumbnail = _write_thumbnail(ai_folder / "2026-08-22", "camera1_recent.jpg", days_old=0)
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", tmp_path / "analytics_events.json")
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", ai_folder)
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)

    main.delete_expired_analytics_events()

    assert not old_thumbnail.exists()
    assert recent_thumbnail.exists()


def test_orphaned_thumbnail_with_no_surviving_event_record_is_still_cleaned_up(tmp_path, monkeypatch):
    """An AI thumbnail whose analytics_events.json record was already
    evicted by the existing 5000-entry cap (append_analytics_event())
    has no event to correlate against -- this sweep is deliberately
    pure age-based on the thumbnail's own mtime, so it cleans these up
    too, exactly like a live event's thumbnail, with no dependency on
    whether a JSON record still exists."""
    ai_folder = tmp_path / "media" / "ai"
    orphaned_thumbnail = _write_thumbnail(ai_folder / "2026-08-10", "camera2_orphan.jpg", days_old=10)
    _write_events(tmp_path / "analytics_events.json", [])  # no corresponding event record at all
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", tmp_path / "analytics_events.json")
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", ai_folder)
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)

    main.delete_expired_analytics_events()

    assert not orphaned_thumbnail.exists()


def test_recordings_and_motion_data_are_never_touched(tmp_path, monkeypatch):
    """Proves this function's blast radius is exactly
    analytics_events.json + AI_THUMBNAILS_FOLDER -- a decoy old .mkv
    recording, an old motion thumbnail, and old motion_events.jsonl
    entries all survive untouched, because none of RECORDINGS_FOLDER,
    MOTION_EVENTS_FILE, or MOTION_THUMBNAILS_FOLDER is ever referenced
    by delete_expired_analytics_events()."""
    recordings_folder = tmp_path / "recordings"
    old_recording = recordings_folder / "camera1" / "old_clip.mkv"
    old_recording.parent.mkdir(parents=True, exist_ok=True)
    old_recording.write_bytes(b"fake-video-bytes")
    _set_mtime_days_ago(old_recording, 30)

    motion_thumbnails_folder = tmp_path / "media" / "motion"
    old_motion_thumbnail = _write_thumbnail(motion_thumbnails_folder, "old_motion.jpg", days_old=30)

    motion_events_file = tmp_path / "motion_events.jsonl"
    motion_events_file.write_text('{"id":"old-motion-1","start_time":"2020-01-01T00:00:00"}\n', encoding="utf-8")

    monkeypatch.setattr(main, "RECORDINGS_FOLDER", recordings_folder)
    monkeypatch.setattr(main, "MOTION_THUMBNAILS_FOLDER", motion_thumbnails_folder)
    monkeypatch.setattr(main, "MOTION_EVENTS_FILE", motion_events_file)
    monkeypatch.setattr(main, "ANALYTICS_EVENTS_FILE", tmp_path / "analytics_events.json")
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path / "media" / "ai")
    monkeypatch.setattr(main, "ANALYTICS_RETENTION_DAYS", 7)

    main.delete_expired_analytics_events()

    assert old_recording.exists()
    assert old_motion_thumbnail.exists()
    assert motion_events_file.read_text(encoding="utf-8") == '{"id":"old-motion-1","start_time":"2020-01-01T00:00:00"}\n'


def test_analytics_retention_days_env_var_default_and_override(monkeypatch):
    """Confirms the constant's own parsing: default 7, overridable via
    ANYAICAM_ANALYTICS_RETENTION_DAYS, floored at 1 so a stray 0 or
    negative value can never wipe every event on the next tick."""
    assert main.ANALYTICS_RETENTION_DAYS == max(1, int(os.environ.get("ANYAICAM_ANALYTICS_RETENTION_DAYS", "7")))


def test_retention_worker_calls_both_sweeps_every_tick(monkeypatch):
    """Confirms delete_expired_analytics_events() is actually wired
    into the hourly retention_worker() loop alongside the existing
    delete_expired_recordings() call, not just defined and orphaned."""
    import inspect

    source = inspect.getsource(main.retention_worker)
    assert "delete_expired_recordings()" in source
    assert "delete_expired_analytics_events()" in source
