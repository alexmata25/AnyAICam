"""event_media_uploader.register_shared_event_media(): the registration-
only path a correlated Smart Motion event uses to reference an already-
uploaded S3 object (the base Motion event's own clip/thumbnail) under
its own, independent detection_event_id -- no ffmpeg encode, no S3
PutObject, ever, from this function.

Mocks recording_uploader's and analytics_sync's own module-level
functions directly, matching test_event_media_uploader.py's own
established pattern for upload_motion_event_media() -- this file
exercises register_shared_event_media()'s own control flow only.
"""
import types
from datetime import datetime

import pytest

import analytics_sync
import event_media_uploader
import recording_uploader as recording_upload


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", True)


def _wire_happy_path(monkeypatch, identity=None, media_status="accepted"):
    identity = identity or {"camera_id": "cam-1", "site_id": "site-1", "cloud_recording_mode": "motion"}
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: identity)
    control_plane_calls = []
    monkeypatch.setattr(
        recording_upload,
        "_control_plane_post",
        lambda path, payload: (control_plane_calls.append((path, payload)), {"status": media_status, "media_id": "media-1"})[1],
    )
    monkeypatch.setattr(analytics_sync, "_load_local_events", lambda: [{"id": "smart-evt-1", "camera": 1}])
    monkeypatch.setattr(analytics_sync, "_build_payload", lambda event: {"id": event["id"]})
    monkeypatch.setattr(analytics_sync, "_control_plane_post", lambda path, payload: {"status": "accepted"})
    persisted = []
    monkeypatch.setattr(analytics_sync, "_persist_synced_id", lambda event_id: persisted.append(event_id))
    return {"control_plane_calls": control_plane_calls, "persisted": persisted}


def _call(**overrides):
    kwargs = {
        "event_id": "smart-evt-1",
        "camera_number": 1,
        "s3_key": "cust-1/site-1/appl-1/cam-1/2026/09/14/events/motion_base-evt-1.mp4",
        "thumbnail_s3_key": "cust-1/site-1/appl-1/cam-1/2026/09/14/events/motion_base-evt-1.jpg",
        "duration_seconds": 15.6,
        "size_bytes": 4153816,
        "started_at": "2026-09-14T01:57:17.041509",
        "ended_at": "2026-09-14T01:57:32.595207",
    }
    kwargs.update(overrides)
    return event_media_uploader.register_shared_event_media(**kwargs)


def test_disabled_flag_skips_without_touching_anything(monkeypatch):
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)

    def _should_not_be_called(camera_number):
        raise AssertionError("camera identity must never be looked up while the appliance-wide flag is off")

    monkeypatch.setattr(recording_upload, "_camera_identity", _should_not_be_called)

    assert _call() is False


def test_unknown_camera_returns_false(monkeypatch):
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: None)

    assert _call() is False


def test_happy_path_registers_the_reused_key_verbatim_and_never_touches_s3(monkeypatch):
    """No boto3/S3 client is ever constructed or referenced by this
    function at all -- confirmed by simply never patching/providing one
    and still succeeding, unlike upload_motion_event_media()'s own tests
    which all require a fake S3 client."""
    calls = _wire_happy_path(monkeypatch)

    result = _call()

    assert result is True
    media_calls = [c for c in calls["control_plane_calls"] if c[0].endswith("/media")]
    assert len(media_calls) == 1
    path, payload = media_calls[0]
    assert path == "/api/appliance/analytics/cam-1/events/smart-evt-1/media"
    # The exact s3_key/thumbnail_s3_key passed in are registered verbatim
    # -- never re-derived from this call's own event_id.
    assert payload["s3_key"] == "cust-1/site-1/appl-1/cam-1/2026/09/14/events/motion_base-evt-1.mp4"
    assert payload["thumbnail_s3_key"] == "cust-1/site-1/appl-1/cam-1/2026/09/14/events/motion_base-evt-1.jpg"
    assert payload["duration_seconds"] == 15.6
    assert payload["size_bytes"] == 4153816
    assert payload["started_at"] == "2026-09-14T01:57:17.041509"
    assert payload["ended_at"] == "2026-09-14T01:57:32.595207"


def test_never_calls_ensure_session_or_boto3(monkeypatch):
    """The whole point of this function: registering a second pointer to
    an already-uploaded object requires no S3 credentials at all."""
    calls = _wire_happy_path(monkeypatch)

    session_calls = []
    monkeypatch.setattr(recording_upload, "_ensure_session", lambda *a, **k: session_calls.append((a, k)) or None)

    assert _call() is True
    assert session_calls == []


def test_registration_deferred_when_detection_event_not_yet_synced(monkeypatch):
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: {"camera_id": "cam-1", "cloud_recording_mode": "motion"})
    monkeypatch.setattr(analytics_sync, "_load_local_events", lambda: [])  # the local event can't be found at all

    result = _call()

    assert result is False


def test_control_plane_failure_retries_then_returns_false(monkeypatch):
    calls = _wire_happy_path(monkeypatch)
    monkeypatch.setattr(recording_upload, "_control_plane_post", lambda path, payload: None)
    monkeypatch.setattr(event_media_uploader.time, "sleep", lambda seconds: None)

    result = _call()

    assert result is False


def test_duplicate_status_is_treated_as_success_and_idempotent(monkeypatch):
    """Matches upload_motion_event_media()'s own 'duplicate' handling --
    a retried/duplicate registration call must not error or loop."""
    _wire_happy_path(monkeypatch, media_status="duplicate")

    assert _call() is True
    assert _call() is True


def test_never_writes_to_the_durable_outbox(monkeypatch):
    """Deliberately does not introduce a second queue: there is no local
    artifact for this call to protect, so it must never touch
    event_media_outbox at all -- unlike upload_motion_event_media()'s
    own put()/remove() calls."""
    _wire_happy_path(monkeypatch)

    outbox_calls = []
    monkeypatch.setattr(event_media_uploader.event_media_outbox, "put", lambda job: outbox_calls.append(("put", job)))
    monkeypatch.setattr(event_media_uploader.event_media_outbox, "remove", lambda event_id: outbox_calls.append(("remove", event_id)))

    assert _call() is True
    assert outbox_calls == []
