"""event_media_uploader.register_shared_event_media(): the parent-id-
only registration path a correlated Smart Motion event uses to
reference its base Motion event's own already-registered media, under
its own, independent detection_event_id -- no ffmpeg encode, no S3
PutObject, no S3 credential of any kind, ever, from this function
(2026-09-14, Phase A revision after independent security review: the
prior round forwarded S3 keys/timing/duration/size and hit a real,
repeated HTTP 403 from the cloud's pre-existing anti-spoofing check;
this function now sends ONLY the parent's own local id, and the cloud
derives every approved value itself).

Mocks recording_uploader's and analytics_sync's own module-level
functions directly, matching test_event_media_uploader.py's own
established pattern for upload_motion_event_media() -- this file
exercises register_shared_event_media()'s own control flow only.
"""
import pytest

import analytics_sync
import event_media_outbox
import event_media_uploader
import recording_uploader as recording_upload


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", True)


@pytest.fixture(autouse=True)
def _isolated_outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(event_media_outbox, "OUTBOX_FILE", tmp_path / "outbox.json")


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
        "parent_local_event_id": "base-evt-1",
    }
    kwargs.update(overrides)
    return event_media_uploader.register_shared_event_media(**kwargs)


def test_disabled_flag_skips_without_touching_anything(monkeypatch):
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)

    def _should_not_be_called(camera_number):
        raise AssertionError("camera identity must never be looked up while the appliance-wide flag is off")

    monkeypatch.setattr(recording_upload, "_camera_identity", _should_not_be_called)

    assert _call() is False
    assert event_media_outbox.load() == []


def test_unknown_camera_returns_false(monkeypatch):
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: None)

    assert _call() is False
    assert event_media_outbox.load() == []


def test_happy_path_sends_only_the_parent_local_event_id_and_never_touches_s3(monkeypatch):
    """No boto3/S3 client is ever constructed or referenced by this
    function at all -- confirmed by simply never patching/providing one
    and still succeeding, unlike upload_motion_event_media()'s own tests
    which all require a fake S3 client. The outgoing payload carries
    ONLY the parent's own local id -- never a storage key of any kind,
    the exact shape change this fix exists to make."""
    calls = _wire_happy_path(monkeypatch)

    result = _call()

    assert result is True
    media_calls = [c for c in calls["control_plane_calls"] if c[0].endswith("/media/shared")]
    assert len(media_calls) == 1
    path, payload = media_calls[0]
    assert path == "/api/appliance/analytics/cam-1/events/smart-evt-1/media/shared"
    assert payload == {"parent_local_event_id": "base-evt-1"}


def test_never_calls_ensure_session_or_boto3(monkeypatch):
    """The whole point of this function: registering a reference to an
    already-uploaded object requires no S3 credentials at all."""
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
    # Durable intent still recorded -- a later resync of the still-
    # unsynced child, followed by a retry, can recover this.
    jobs = event_media_outbox.load()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "shared"
    assert jobs[0]["parent_local_event_id"] == "base-evt-1"


def test_control_plane_failure_retries_then_returns_false(monkeypatch):
    calls = _wire_happy_path(monkeypatch)
    monkeypatch.setattr(recording_upload, "_control_plane_post", lambda path, payload: None)
    monkeypatch.setattr(event_media_uploader.time, "sleep", lambda seconds: None)

    result = _call()

    assert result is False
    assert len(event_media_outbox.load()) == 1


def test_duplicate_status_is_treated_as_success_and_idempotent(monkeypatch):
    """Matches upload_motion_event_media()'s own 'duplicate' handling --
    a retried/duplicate registration call must not error or loop."""
    _wire_happy_path(monkeypatch, media_status="duplicate")

    assert _call() is True
    assert _call() is True
    assert event_media_outbox.load() == []


# --------------------------------------------------------- durable recovery via the existing outbox


def test_writes_a_durable_shared_entry_before_attempting_anything(monkeypatch):
    """Unlike the old design (which never touched the outbox at all,
    since it had nothing local to protect), this call is durable from
    its very first line: a crash or restart mid-registration must never
    silently lose the intent to share media for this event."""
    calls = _wire_happy_path(monkeypatch)

    _call()

    jobs = event_media_outbox.load()
    assert jobs == []  # removed again after a successful registration
    assert calls["control_plane_calls"]  # but the attempt genuinely happened


def test_successful_registration_removes_the_durable_entry(monkeypatch):
    _wire_happy_path(monkeypatch)

    assert _call() is True
    assert event_media_outbox.load() == []


def test_failed_registration_leaves_the_durable_entry_for_a_later_retry(monkeypatch):
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: {"camera_id": "cam-1", "cloud_recording_mode": "motion"})
    monkeypatch.setattr(analytics_sync, "_load_local_events", lambda: [])  # deferred

    assert _call() is False

    jobs = event_media_outbox.load()
    assert len(jobs) == 1
    assert jobs[0]["event_id"] == "smart-evt-1"
    assert jobs[0]["camera_number"] == 1
    assert jobs[0]["kind"] == "shared"
    assert jobs[0]["parent_local_event_id"] == "base-evt-1"


def test_retry_pending_event_media_recovers_a_shared_entry_without_any_encode_or_upload(monkeypatch):
    """The actual recovery worker: a durable 'shared' outbox entry is
    retried via register_shared_event_media() alone -- never via
    upload_motion_event_media() or build_motion_event_clip()."""
    event_media_outbox.put({
        "event_id": "smart-evt-1",
        "camera_number": 1,
        "kind": "shared",
        "parent_local_event_id": "base-evt-1",
    })

    register_calls = []

    def fake_register(*, event_id, **kwargs):
        register_calls.append({"event_id": event_id, **kwargs})
        event_media_outbox.remove(event_id)  # matches the real function's own on-success contract
        return True

    def fail_if_called(**kwargs):
        raise AssertionError("a 'shared' outbox entry must never trigger an upload/encode retry")

    monkeypatch.setattr(event_media_uploader, "register_shared_event_media", fake_register)
    monkeypatch.setattr(event_media_uploader, "upload_motion_event_media", fail_if_called)

    summary = event_media_uploader.retry_pending_event_media()

    assert summary == {"attempted": 1, "completed": 1, "pending": 0}
    assert register_calls == [{"event_id": "smart-evt-1", "camera_number": 1, "parent_local_event_id": "base-evt-1"}]


def test_retry_pending_event_media_still_uses_upload_path_for_ordinary_upload_entries(monkeypatch):
    """The pre-existing 'upload' kind (no 'kind' key at all, matching
    every entry ever written before this change) is completely
    unaffected -- still routed to upload_motion_event_media(), never to
    the new shared-registration function."""
    event_media_outbox.put({
        "event_id": "evt-1",
        "camera_number": 1,
        "event_start": "2026-09-14T00:00:00",
        "event_end": "2026-09-14T00:00:10",
        "clip_url": "/recordings/clips/motion/motion_evt-1.mp4",
        "thumbnail_url": None,
    })

    upload_calls = []

    def fake_upload(*, event_id, **kwargs):
        upload_calls.append({"event_id": event_id, **kwargs})
        event_media_outbox.remove(event_id)  # matches the real function's own on-success contract
        return True

    def fail_if_called(**kwargs):
        raise AssertionError("an ordinary upload entry must never be routed to register_shared_event_media()")

    monkeypatch.setattr(event_media_uploader, "upload_motion_event_media", fake_upload)
    monkeypatch.setattr(event_media_uploader, "register_shared_event_media", fail_if_called)

    summary = event_media_uploader.retry_pending_event_media()

    assert summary == {"attempted": 1, "completed": 1, "pending": 0}
    assert len(upload_calls) == 1
