"""Local-only event-media capture contract tests.

All transport hooks fail the test if called.  These tests use temporary
files/outbox state and do not contact an appliance, cloud endpoint, or S3.
"""

from datetime import datetime

import event_media_outbox as outbox
import event_media_uploader as uploader
import recording_uploader


def _paths(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    thumbnail = tmp_path / "thumbnail.jpg"
    clip.write_bytes(b"clip-bytes")
    thumbnail.write_bytes(b"thumbnail-bytes")
    monkeypatch.setattr(outbox, "OUTBOX_FILE", tmp_path / "event_media_outbox.json")
    monkeypatch.setattr(uploader, "_local_path_from_recording_url", lambda url: {"/recordings/clip.mp4": clip, "/recordings/thumbnail.jpg": thumbnail}.get(url))
    return clip, thumbnail


def _call(**overrides):
    values = {
        "event_id": "evt-deterministic-1", "camera_number": 3,
        "event_start": datetime(2026, 9, 10, 12, 0, 0),
        "event_end": datetime(2026, 9, 10, 12, 0, 5),
        "clip_url": "/recordings/clip.mp4", "thumbnail_url": "/recordings/thumbnail.jpg",
    }
    values.update(overrides)
    return uploader.upload_motion_event_media(
        **values,
    )


def test_local_capture_persists_media_without_any_external_transport(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_CAPTURE_ENABLED", True)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)
    for name in ("_refresh_camera_map", "_ensure_session", "_control_plane_post"):
        monkeypatch.setattr(recording_uploader, name, lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external transport called")))
    monkeypatch.setattr(uploader, "_ensure_detection_event_synced", lambda *args: (_ for _ in ()).throw(AssertionError("external transport called")))

    assert _call() is True
    assert outbox.load() == [{
        "event_id": "evt-deterministic-1", "camera_number": 3,
        "event_start": "2026-09-10T12:00:00", "event_end": "2026-09-10T12:00:05",
        "clip_url": "/recordings/clip.mp4", "thumbnail_url": "/recordings/thumbnail.jpg",
        "attempts": 0, "next_attempt_at": None,
    }]


def test_capture_and_upload_off_does_nothing(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_CAPTURE_ENABLED", False)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)
    assert _call() is False
    assert outbox.load() == []


def test_upload_enabled_keeps_existing_transport_path(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_CAPTURE_ENABLED", False)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_UPLOAD_ENABLED", True)
    monkeypatch.setattr(recording_uploader, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_uploader, "_camera_identity", lambda _: {"camera_id": "cam-3", "cloud_recording_mode": "motion"})
    monkeypatch.setattr(recording_uploader, "_ensure_session", lambda *_: None)

    assert _call() is False
    assert outbox.load()[0]["event_id"] == "evt-deterministic-1"


def test_local_outbox_survives_restart_and_deduplicates_without_secrets(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_CAPTURE_ENABLED", True)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)
    assert _call() is True
    assert _call() is True
    persisted = (tmp_path / "event_media_outbox.json").read_text(encoding="utf-8")
    assert len(outbox.load()) == 1
    assert "credential" not in persisted.lower()
    assert "secret" not in persisted.lower()
    # Reloading reads the atomic on-disk state, as a new process would.
    assert outbox.load()[0]["event_id"] == "evt-deterministic-1"


def test_local_capture_strips_url_query_and_fragment_before_persistence(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_CAPTURE_ENABLED", True)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)
    assert _call(clip_url="/recordings/clip.mp4?temporary=value#fragment") is True
    persisted = (tmp_path / "event_media_outbox.json").read_text(encoding="utf-8")
    assert "temporary=value" not in persisted
    assert outbox.load()[0]["clip_url"] == "/recordings/clip.mp4"


def test_upload_disabled_retry_entry_point_never_transports(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_CAPTURE_ENABLED", True)
    monkeypatch.setattr(uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)
    assert _call() is True
    monkeypatch.setattr(uploader, "upload_motion_event_media", lambda **_: (_ for _ in ()).throw(AssertionError("transport called")))
    assert uploader.retry_pending_event_media() == {"attempted": 0, "completed": 0, "pending": 1}
