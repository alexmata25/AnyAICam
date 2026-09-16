"""event_media_uploader.upload_motion_event_media(): per-camera cloud-
recording eligibility, keeping motion-event upload separate from
continuous Cloud 24/7 recording, and graceful behavior on cloud
failure/retry.

Mocks recording_uploader's and analytics_sync's own module-level
functions directly (the same modules upload_motion_event_media() itself
calls) rather than going through a real network/S3/database -- this file
exercises upload_motion_event_media()'s own control flow, not those
modules' internals (which have their own dedicated test files).
"""
import types
from datetime import datetime

import pytest

import analytics_sync
import event_media_uploader
import recording_uploader as recording_upload
import smart_motion


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    # Every test in this file starts from the appliance-wide flag ON --
    # tests that specifically need it off set that themselves.
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", True)


@pytest.fixture(autouse=True)
def _local_files(tmp_path, monkeypatch):
    """Bypasses the real /app/recordings/... filesystem prefix
    _local_path_from_recording_url() hardcodes -- real files still exist
    on disk (clip_path.stat().st_size is called for real), just under
    tmp_path instead."""
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-mp4-bytes")
    thumbnail_path = tmp_path / "thumb.jpg"
    thumbnail_path.write_bytes(b"fake-jpg-bytes")

    def fake_local_path(value):
        if value is None:
            return None
        if "thumb" in str(value):
            return thumbnail_path
        return clip_path

    monkeypatch.setattr(event_media_uploader, "_local_path_from_recording_url", fake_local_path)
    return {"clip_path": clip_path, "thumbnail_path": thumbnail_path}


class _FakeS3Client:
    def __init__(self):
        self.uploaded = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploaded.append({"path": path, "bucket": bucket, "key": key, "content_type": (ExtraArgs or {}).get("ContentType")})


@pytest.fixture()
def fake_s3(monkeypatch):
    client = _FakeS3Client()
    fake_boto3 = types.SimpleNamespace(client=lambda *a, **k: client)
    monkeypatch.setattr(recording_upload, "boto3", fake_boto3)
    return client


def _eligible_identity(camera_id="cam-1", cloud_recording_mode="motion"):
    return {"camera_id": camera_id, "site_id": "site-1", "cloud_recording_mode": cloud_recording_mode}


def _wire_happy_path(monkeypatch, identity, fake_s3, media_status="accepted"):
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: identity)
    session = {
        "credentials": {"access_key_id": "AKIA-FAKE", "secret_access_key": "fake-secret", "session_token": "fake-token"},
        "bucket": "fake-bucket",
        "key_prefix": "cust-1/site-1/appl-1/cam-1/",
        "expires_at": "2026-09-10T12:00:00+00:00",
    }
    ensure_session_calls = []
    monkeypatch.setattr(recording_upload, "_ensure_session", lambda cam, cam_id: (ensure_session_calls.append((cam, cam_id)), session)[1])
    control_plane_calls = []
    monkeypatch.setattr(recording_upload, "_control_plane_post", lambda path, payload: (control_plane_calls.append((path, payload)), {"status": media_status, "media_id": "media-1"})[1])
    monkeypatch.setattr(analytics_sync, "_load_local_events", lambda: [{"id": "evt-1", "camera": 1}])
    monkeypatch.setattr(analytics_sync, "_build_payload", lambda event: {"id": event["id"]})
    monkeypatch.setattr(analytics_sync, "_control_plane_post", lambda path, payload: {"status": "accepted"})
    persisted = []
    monkeypatch.setattr(analytics_sync, "_persist_synced_id", lambda event_id: persisted.append(event_id))
    return {"ensure_session_calls": ensure_session_calls, "control_plane_calls": control_plane_calls, "persisted": persisted}


def _call(event_id="evt-1", camera_number=1):
    return event_media_uploader.upload_motion_event_media(
        event_id=event_id,
        camera_number=camera_number,
        event_start=datetime(2026, 9, 10, 12, 0, 0),
        event_end=datetime(2026, 9, 10, 12, 0, 10),
        clip_url="/recordings/clips/motion/motion_evt-1.mp4",
        thumbnail_url="/recordings/media/motion/thumb.jpg",
    )


# ------------------------------------------------------------- eligibility


def test_ineligible_camera_does_not_upload(monkeypatch, fake_s3):
    """cloud_recording_mode unset (no entitlement yet) -- must never
    reach session/credentials/S3/control-plane."""
    identity = _eligible_identity(cloud_recording_mode=None)
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)

    result = _call()

    assert result is False
    assert fake_s3.uploaded == []
    assert calls["ensure_session_calls"] == []
    assert calls["control_plane_calls"] == []


def test_continuous_mode_camera_does_not_upload_event_media(monkeypatch, fake_s3):
    """'continuous' is the separate Cloud 24/7 product (served entirely
    by recording_uploader.py's own path) -- must NOT be treated as
    eligible for motion-event media upload."""
    identity = _eligible_identity(cloud_recording_mode="continuous")
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)

    result = _call()

    assert result is False
    assert fake_s3.uploaded == []
    assert calls["ensure_session_calls"] == []


def test_explicitly_disabled_camera_does_not_upload(monkeypatch, fake_s3):
    identity = _eligible_identity(cloud_recording_mode="disabled")
    _wire_happy_path(monkeypatch, identity, fake_s3)

    assert _call() is False
    assert fake_s3.uploaded == []


def test_unknown_camera_does_not_upload(monkeypatch, fake_s3):
    """Camera missing from the cached map entirely (e.g. not yet
    provisioned) -- existing behavior, unaffected by the new eligibility
    check, still fails closed."""
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: None)

    assert _call() is False
    assert fake_s3.uploaded == []


def test_eligible_camera_uploads_clip_and_thumbnail(monkeypatch, fake_s3, _local_files):
    identity = _eligible_identity(cloud_recording_mode="motion")
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)

    result = _call()

    assert result is True
    assert len(calls["ensure_session_calls"]) == 1
    uploaded_keys = {item["key"].rsplit(".", 1)[-1] for item in fake_s3.uploaded}
    assert uploaded_keys == {"mp4", "jpg"}
    assert all(item["bucket"] == "fake-bucket" for item in fake_s3.uploaded)
    # The final media-registration POST is what marks a real success.
    media_calls = [c for c in calls["control_plane_calls"] if c[0].endswith("/media")]
    assert len(media_calls) == 1


def test_appliance_wide_disable_overrides_camera_eligibility(monkeypatch, fake_s3):
    """Even a fully-eligible camera (cloud_recording_mode='motion') must
    not upload while the appliance-wide flag is off -- and the camera-
    eligibility check must never even be reached (the master switch
    short-circuits first)."""
    monkeypatch.setattr(event_media_uploader, "EVENT_MEDIA_UPLOAD_ENABLED", False)

    def _should_not_be_called(camera_number):
        raise AssertionError("camera identity must never be looked up while the appliance-wide flag is off")

    monkeypatch.setattr(recording_upload, "_camera_identity", _should_not_be_called)

    assert _call() is False
    assert fake_s3.uploaded == []


# ------------------------------------------------------- environmental-motion filtering


def _eligible_identity_with_smart_motion(camera_id="cam-1", smart_motion_enabled=True):
    return {"camera_id": camera_id, "site_id": "site-1", "cloud_recording_mode": "motion", "smart_motion_enabled": smart_motion_enabled}


def test_smart_motion_enabled_with_no_correlated_object_skips_upload_but_reports_success(monkeypatch, fake_s3):
    """A raw pixel-diff trigger (tree, shadow, rain) with Smart Motion
    enabled and no real object correlated within the window -- local
    capture already happened upstream (this function's own contract),
    only the cloud upload is skipped. Returns True (not a failure, a
    deliberate skip), matching this function's own "captured local"
    convention for the appliance-wide-disabled case."""
    identity = _eligible_identity_with_smart_motion(smart_motion_enabled=True)
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)
    monkeypatch.setattr(smart_motion, "classify_motion", lambda camera_number: None)

    result = _call()

    assert result is True
    assert fake_s3.uploaded == []
    assert calls["ensure_session_calls"] == []


def test_smart_motion_enabled_with_a_correlated_object_uploads_normally(monkeypatch, fake_s3, _local_files):
    identity = _eligible_identity_with_smart_motion(smart_motion_enabled=True)
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)
    monkeypatch.setattr(smart_motion, "classify_motion", lambda camera_number: "person")

    result = _call()

    assert result is True
    assert len(calls["ensure_session_calls"]) == 1
    assert {item["key"].rsplit(".", 1)[-1] for item in fake_s3.uploaded} == {"mp4", "jpg"}


def test_smart_motion_disabled_uploads_every_basic_motion_clip_unfiltered(monkeypatch, fake_s3, _local_files):
    """No Smart Motion signal available in the existing architecture for
    this camera -- falls back to current (unfiltered) behavior rather
    than inventing a new heuristic, exactly as designed."""
    identity = _eligible_identity_with_smart_motion(smart_motion_enabled=False)
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)

    def _should_not_be_called(camera_number):
        raise AssertionError("classify_motion must never be consulted when smart_motion_enabled is off")

    monkeypatch.setattr(smart_motion, "classify_motion", _should_not_be_called)

    result = _call()

    assert result is True
    assert len(calls["ensure_session_calls"]) == 1


def test_already_classified_ai_yolo_path_bypasses_the_filter_entirely(monkeypatch, fake_s3, _local_files):
    """save_yolo_events()'s own path: its qualifying_detections check
    already guarantees a real classified object triggered this event --
    must never be blocked by the separate smart_motion_enabled
    entitlement/correlation signal, even when Smart Motion itself finds
    nothing (e.g. the correlation window already moved on)."""
    identity = _eligible_identity_with_smart_motion(smart_motion_enabled=True)
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)
    monkeypatch.setattr(smart_motion, "classify_motion", lambda camera_number: None)

    result = event_media_uploader.upload_motion_event_media(
        event_id="evt-1",
        camera_number=1,
        event_start=datetime(2026, 9, 10, 12, 0, 0),
        event_end=datetime(2026, 9, 10, 12, 0, 10),
        clip_url="/recordings/clips/motion/motion_evt-1.mp4",
        thumbnail_url="/recordings/media/motion/thumb.jpg",
        already_classified=True,
    )

    assert result is True
    assert len(calls["ensure_session_calls"]) == 1


# ------------------------------------------------------------- cloud failure / retry


def test_session_unavailable_returns_false_without_raising(monkeypatch, fake_s3):
    """A cloud/credential-issuance failure for an otherwise-eligible
    camera must degrade to False, never raise -- the caller (main.py's
    build_and_upload_event_media()) already treats a raised exception as
    fail-open for local recording, but this module's own contract is to
    return False cleanly whenever possible."""
    identity = _eligible_identity()
    monkeypatch.setattr(recording_upload, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(recording_upload, "_camera_identity", lambda camera_number: identity)
    monkeypatch.setattr(recording_upload, "_ensure_session", lambda cam, cam_id: None)  # simulated cloud/credential failure

    result = _call()

    assert result is False
    assert fake_s3.uploaded == []


def test_control_plane_unreachable_during_registration_returns_false(monkeypatch, fake_s3):
    identity = _eligible_identity()
    calls = _wire_happy_path(monkeypatch, identity, fake_s3)
    monkeypatch.setattr(recording_upload, "_control_plane_post", lambda path, payload: None)  # simulated network failure
    monkeypatch.setattr(event_media_uploader.time, "sleep", lambda seconds: None)  # skip the real 11x5s retry backoff

    result = _call()

    assert result is False
    # The clip WAS uploaded to S3 before the registration call failed --
    # this module never retries the upload itself; the object is
    # durable in S3 regardless of whether the control plane could be
    # reached to catalog it.
    assert len(fake_s3.uploaded) == 2


def test_retry_after_prior_success_is_duplicate_safe(monkeypatch, fake_s3):
    """Simulates a retried call for an event whose media the cloud
    already registered (status == 'duplicate', matching
    analytics_event_media_available()'s own real dedup response) --
    must still report success, not error or loop."""
    identity = _eligible_identity()
    _wire_happy_path(monkeypatch, identity, fake_s3, media_status="duplicate")

    assert _call() is True
    assert _call() is True  # a second retry behaves identically


# ------------------------------------------------------------- shared_media_out (2026-09-14)
#
# A correlated Smart Motion event (main.py's store_motion_event()) awaits
# this exact call's own task and reuses its result via
# register_shared_event_media() instead of independently re-encoding and
# re-uploading the identical physical window. These tests cover the new,
# purely-additive shared_media_out parameter itself -- save_yolo_events()
# and every pre-existing test above never pass it, and are all
# unaffected (confirmed by every test above still passing unchanged).


def test_shared_media_out_is_populated_on_real_success(monkeypatch, fake_s3, _local_files):
    identity = _eligible_identity(cloud_recording_mode="motion")
    _wire_happy_path(monkeypatch, identity, fake_s3)

    shared_media_out = {}
    result = event_media_uploader.upload_motion_event_media(
        event_id="evt-1",
        camera_number=1,
        event_start=datetime(2026, 9, 10, 12, 0, 0),
        event_end=datetime(2026, 9, 10, 12, 0, 10),
        clip_url="/recordings/clips/motion/motion_evt-1.mp4",
        thumbnail_url="/recordings/media/motion/thumb.jpg",
        shared_media_out=shared_media_out,
    )

    assert result is True
    assert shared_media_out["s3_key"].endswith("motion_evt-1.mp4")
    assert shared_media_out["thumbnail_s3_key"].endswith("motion_evt-1.jpg")
    assert shared_media_out["duration_seconds"] is not None
    assert shared_media_out["size_bytes"] == len(b"fake-mp4-bytes")
    # compute_clip_window() pads with pre-/post-roll -- these are the
    # WINDOW's own start/end (matching the real payload sent to the
    # control plane), not the raw event_start/event_end passed in.
    from event_clips import compute_clip_window
    window = compute_clip_window(datetime(2026, 9, 10, 12, 0, 0), datetime(2026, 9, 10, 12, 0, 10))
    assert shared_media_out["started_at"] == window.start.isoformat()
    assert shared_media_out["ended_at"] == window.end.isoformat()


def test_shared_media_out_left_empty_on_ineligible_camera(monkeypatch, fake_s3):
    """Every failure/no-op path must leave shared_media_out untouched --
    a correlated Smart Motion event must see this as falsy (nothing to
    share) and skip cleanly, never attempting its own independent
    encode as a fallback."""
    identity = _eligible_identity(cloud_recording_mode=None)
    _wire_happy_path(monkeypatch, identity, fake_s3)

    shared_media_out = {}
    result = event_media_uploader.upload_motion_event_media(
        event_id="evt-1",
        camera_number=1,
        event_start=datetime(2026, 9, 10, 12, 0, 0),
        event_end=datetime(2026, 9, 10, 12, 0, 10),
        clip_url="/recordings/clips/motion/motion_evt-1.mp4",
        thumbnail_url="/recordings/media/motion/thumb.jpg",
        shared_media_out=shared_media_out,
    )

    assert result is False
    assert shared_media_out == {}


def test_shared_media_out_left_empty_when_registration_ultimately_fails(monkeypatch, fake_s3):
    identity = _eligible_identity()
    _wire_happy_path(monkeypatch, identity, fake_s3)
    monkeypatch.setattr(recording_upload, "_control_plane_post", lambda path, payload: None)
    monkeypatch.setattr(event_media_uploader.time, "sleep", lambda seconds: None)

    shared_media_out = {}
    result = event_media_uploader.upload_motion_event_media(
        event_id="evt-1",
        camera_number=1,
        event_start=datetime(2026, 9, 10, 12, 0, 0),
        event_end=datetime(2026, 9, 10, 12, 0, 10),
        clip_url="/recordings/clips/motion/motion_evt-1.mp4",
        thumbnail_url="/recordings/media/motion/thumb.jpg",
        shared_media_out=shared_media_out,
    )

    assert result is False
    assert shared_media_out == {}


def test_omitting_shared_media_out_is_completely_unaffected(monkeypatch, fake_s3, _local_files):
    """save_yolo_events()'s own real call site never passes this
    parameter -- confirms the default (None) path behaves exactly as
    before this change, with no AttributeError or other side effect."""
    identity = _eligible_identity(cloud_recording_mode="motion")
    _wire_happy_path(monkeypatch, identity, fake_s3)

    assert _call() is True
