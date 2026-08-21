"""Session-cache revalidation: tests for _upload_currently_authorized()
and its enforcement in _ensure_session(). Built in response to a real
issue found during the pilot's bounded cloud-upload test: EC2's
ANYAICAM_RECORDING_UPLOAD_ENABLED=false only blocks *new* credential
issuance -- an already-cached, still-valid STS session (up to
RECORDING_SESSION_DURATION_SECONDS = 900s) kept working and the
appliance uploaded four more files straight to S3 after the flag was
disabled, each one later failing only at the /available catalog-notify
step (a 404), leaving orphaned, uncataloged S3 objects.

Fast/pure tests only -- no ffmpeg, no AWS, no network. The one
end-to-end test (no_upload_occurs_after_disable) exercises the real,
unmocked _relay_camera_once() with only the network boundary
(_control_plane_get/_control_plane_post) and the codec-prep/upload
steps mocked, matching this project's own established pattern for
these tests.
"""

from datetime import datetime, timedelta, timezone

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(ru, "CUTOFF_FILE", tmp_path / "state" / "recording_upload_cutoff.json")
    monkeypatch.setattr(ru, "MAX_FILES_PER_SCAN", None)
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._uploaded_files.clear()
    ru._sessions.clear()
    yield
    ru._cutoff_cache = None
    ru._backlog_skip_logged.clear()
    ru._uploaded_files.clear()
    ru._sessions.clear()


def _valid_cached_session(camera_number: int):
    session = {
        "credentials": {"access_key_id": "a", "secret_access_key": "b", "session_token": "c"},
        "bucket": "test-bucket",
        "key_prefix": "recordings/cust/site/appl/cam/",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    ru._sessions[camera_number] = session
    return session


def _make_eligible_recording(tmp_path, camera_number, started_at):
    folder = ru._recording_folder(camera_number)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{started_at.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = folder / name
    path.write_bytes(b"fake-recording")
    return path


def _make_still_open_placeholder(camera_number):
    folder = ru._recording_folder(camera_number)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"camera{camera_number}_2099-01-01_00-00-00.mkv").write_bytes(b"placeholder")


# --------------------------------------------------- _upload_currently_authorized


def test_authorized_true_when_status_returns_enabled(monkeypatch):
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: {"enabled": True})
    assert ru._upload_currently_authorized() is True


def test_authorized_false_when_status_returns_disabled(monkeypatch):
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: {"enabled": False})
    assert ru._upload_currently_authorized() is False


def test_authorized_false_fails_closed_when_control_plane_unreachable(monkeypatch):
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: None)
    assert ru._upload_currently_authorized() is False


def test_authorized_false_fails_closed_on_malformed_response(monkeypatch):
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: {"enabled": "yes"})  # not a real bool
    assert ru._upload_currently_authorized() is False


def test_status_check_uses_the_dedicated_status_path(monkeypatch):
    called = []
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: called.append(path) or {"enabled": True})
    ru._upload_currently_authorized()
    assert called == ["/api/appliance/recordings/status"]


# ------------------------------------------------------------- _ensure_session


def test_cached_session_rejected_and_evicted_when_status_disabled(monkeypatch):
    _valid_cached_session(1)
    monkeypatch.setattr(ru, "_upload_currently_authorized", lambda: False)
    post_calls = []
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: post_calls.append(path) or None)

    result = ru._ensure_session(1, "camera-1-id")

    assert result is None
    assert 1 not in ru._sessions  # evicted, not just ignored
    assert post_calls == []  # never even attempted a fresh credential fetch


def test_cached_session_rejected_and_evicted_when_status_check_fails(monkeypatch):
    _valid_cached_session(2)
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: None)  # network failure

    result = ru._ensure_session(2, "camera-2-id")

    assert result is None
    assert 2 not in ru._sessions


def test_enabled_status_allows_cached_session_reuse_without_a_new_fetch(monkeypatch):
    session = _valid_cached_session(3)
    monkeypatch.setattr(ru, "_upload_currently_authorized", lambda: True)
    post_calls = []
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: post_calls.append(path) or None)

    result = ru._ensure_session(3, "camera-3-id")

    assert result == session  # exact same cached object reused
    assert post_calls == []  # no wasted STS call -- caching still works when authorized


def test_fresh_session_path_also_revalidates_and_succeeds_when_authorized(monkeypatch):
    monkeypatch.setattr(ru, "_upload_currently_authorized", lambda: True)
    fresh_response = {
        "status": "accepted",
        "bucket": "test-bucket",
        "key_prefix": "recordings/cust/site/appl/cam/",
        "credentials": {
            "access_key_id": "x", "secret_access_key": "y", "session_token": "z",
            "expiration": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        },
    }
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: fresh_response)

    result = ru._ensure_session(4, "camera-4-id")

    assert result is not None
    assert result["bucket"] == "test-bucket"
    assert ru._sessions[4] == result


def test_fresh_session_path_never_reached_when_not_authorized(monkeypatch):
    monkeypatch.setattr(ru, "_upload_currently_authorized", lambda: False)
    post_calls = []
    monkeypatch.setattr(ru, "_control_plane_post", lambda path, payload: post_calls.append(path) or {"status": "should never be used"})

    result = ru._ensure_session(5, "camera-5-id")

    assert result is None
    assert post_calls == []


# ------------------------------------------------------- end-to-end: the real fix


def test_no_upload_occurs_after_disable_even_with_a_valid_cached_session(tmp_path, monkeypatch):
    """The exact scenario from the real incident: a still-valid (not
    expiring) cached session exists, but the control plane now reports
    upload disabled. No upload attempt of any kind should reach the
    codec-prep/S3 steps."""
    _valid_cached_session(1)
    monkeypatch.setattr(ru, "_control_plane_get", lambda path: {"enabled": False})
    ru._cutoff_cache = datetime.now() - timedelta(hours=1)
    _make_eligible_recording(tmp_path, 1, datetime.now() - timedelta(minutes=10))
    _make_still_open_placeholder(1)

    prepare_calls = []
    monkeypatch.setattr(ru, "_prepare_cloud_copy", lambda *a, **k: prepare_calls.append(1) or None)
    upload_calls = []
    monkeypatch.setattr(ru, "_upload_recording", lambda *a, **k: upload_calls.append(1) or "should-never-be-called")

    ru._relay_camera_once(1, "camera-1-id")

    assert prepare_calls == []  # never even reached codec prep
    assert upload_calls == []  # never reached the upload step
    assert 1 not in ru._sessions  # the stale-but-valid session was evicted
    assert ru._uploaded_files.get(1, []) == []
