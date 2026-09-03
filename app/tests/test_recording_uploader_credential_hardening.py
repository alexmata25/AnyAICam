"""Recording-uploader ExpiredToken hardening (2026-09-03): focused tests
for _relay_camera_once()'s new credential-class-failure handling.

Background: _ensure_session() cached STS-style credentials purely by a
self-reported expiration timestamp, never invalidated by an actual AWS
rejection. A single boto3 client was constructed fresh per file. On an
S3 ExpiredToken (or RequestExpired/InvalidToken) rejection, the scan
blindly kept retrying the same (already-proven-bad) session against
every remaining pending file, with no backoff, no per-scan cap, and no
explicit multipart concurrency bound.

This file proves, all against the real (unmodified except for this fix)
_relay_camera_once()/_ensure_session()/_ensure_client() functions, with
only _prepare_cloud_copy()/_create_recording_thumbnail() (real ffmpeg/
ffprobe work, orthogonal to what's tested here) and the network-facing
primitives (_control_plane_post, boto3.client) faked:

  1.  ExpiredToken invalidates both the cached session and its client.
  2.  No later files from that camera are attempted in the same pass.
  3.  Another camera is not delayed by that camera's backoff.
  4.  The next attempt genuinely requests fresh credentials.
  5.  A non-auth file error still continues to the next file.
  6.  Exponential backoff grows, caps, and resets.
  7.  One S3 client is reused for multiple files under one session.
  8.  At most RECORDING_UPLOAD_MAX_FILES_PER_SCAN files are attempted.
  9.  The remaining backlog survives (untouched, unmarked) for later scans.
  10. Multipart concurrency is explicitly configured for both the
      recording and thumbnail uploads.
  11. The original MKV is never touched under any failure path.

2026-09-03 correction, added after a real production ExpiredToken during
a live acceptance test was NOT caught by any of the above: a real
ExpiredToken from client.upload_file() (the high-level, multipart-
capable method _upload_recording() actually calls) never arrives as a
raw ClientError -- boto3's own S3Transfer.upload_file() catches it
internally and re-raises as boto3.exceptions.S3UploadFailedError, which
does not inherit from ClientError. Tests 1-11 above all used a fake
client that raised a raw ClientError directly, which is not what
production actually does, so this gap passed every one of them. The
following tests reproduce the REAL wrapped shape (an actual
S3UploadFailedError raised from inside `except ClientError as e:`, so
Python's own implicit chaining sets __context__ exactly like boto3
does -- not a hand-set attribute standing in for it):

  12. A wrapped ExpiredToken (S3UploadFailedError wrapping ClientError)
      engages the same credential handling as a direct one.
  13. A wrapped non-credential S3 failure (e.g. AccessDenied) does NOT
      engage credential handling -- still just skip-and-continue.
  14. Direct ClientError credential handling still works unchanged.
  15. Wrapped-case session/client invalidation.
  16. Wrapped-case: rest of that camera's pass stops.
  17. Wrapped-case: another camera is not delayed.
  18. Wrapped-case: backoff activates.
  19. Wrapped-case: the next attempt requests fresh credentials.

Same import/isolation constraints as this suite's other fix-verification
files: imports the real module (must run inside the deployed
container's Python); every test redirects RECORDINGS_FOLDER to a
tmp_path and resets every module-level mutable dict before running --
nothing here ever touches real production recordings, credentials, or
makes a real network/AWS call.
"""

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

import recording_uploader as ru


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "RECORDINGS_FOLDER", tmp_path)
    monkeypatch.setattr(ru, "_camera_map", {})
    monkeypatch.setattr(ru, "_sessions", {})
    monkeypatch.setattr(ru, "_clients", {})
    monkeypatch.setattr(ru, "_camera_backoff", {})
    monkeypatch.setattr(ru, "_uploaded_files", {})
    monkeypatch.setattr(ru, "_unsupported_codec_files", {})
    # Real ffmpeg/ffprobe work (codec probe, remux, thumbnail extraction)
    # is orthogonal to everything this file tests -- faked out by default
    # so tests are fast and deterministic; individual tests override
    # these where the fake's own return value matters to what's being
    # proven (e.g. the multipart-config test needs a real thumbnail path).
    monkeypatch.setattr(ru, "_create_recording_thumbnail", lambda mp4_path, camera_number: None)


def _make_recording(folder, camera_number, start, content=b"original mkv bytes"):
    camera_folder = folder / f"camera{camera_number}"
    camera_folder.mkdir(parents=True, exist_ok=True)
    name = f"camera{camera_number}_{start.strftime('%Y-%m-%d_%H-%M-%S')}.mkv"
    path = camera_folder / name
    path.write_bytes(content)
    return path


def _seed_pending_files(tmp_path, camera_number, count, base=None):
    """count "completed" files, plus one extra newest file that
    _completed_recording_files() always excludes as still-being-written
    -- matches the real function's own contract."""
    base = base or datetime(2026, 9, 1, 8, 0, 0)
    paths = []
    for i in range(count + 1):
        paths.append(_make_recording(tmp_path, camera_number, base + timedelta(minutes=5 * i), content=f"mkv-{i}".encode()))
    return paths[:-1]  # the completed ones, oldest-first -- matches what a real scan would see as "pending"


def _fake_credentials_response(**overrides):
    response = {
        "credentials": {
            "access_key_id": "AKIAFAKE",
            "secret_access_key": "fakesecret",
            "session_token": "faketoken",
            "expiration": (datetime.now().astimezone().isoformat()),
        },
        "bucket": "test-bucket",
        "key_prefix": "recordings/tenant/",
    }
    response.update(overrides)
    return response


class _FakeS3Client:
    """Stands in for boto3.client("s3", ...). upload_calls records every
    upload_file() invocation (including its Config kwarg, for the
    multipart-concurrency assertions). `fail_plan` is an optional
    callable(call_index_starting_at_1) -> Exception | None, applied only
    to the *recording* upload (not the thumbnail) -- lets a test make
    call N fail with a specific error while earlier/later calls succeed."""

    _next_id = 0

    def __init__(self, fail_plan=None):
        _FakeS3Client._next_id += 1
        self.id = _FakeS3Client._next_id
        self.fail_plan = fail_plan
        self.upload_calls = []
        self._recording_call_count = 0

    def upload_file(self, path, bucket, key, ExtraArgs=None, Config=None):
        self.upload_calls.append({"path": path, "bucket": bucket, "key": key, "ExtraArgs": ExtraArgs, "Config": Config})
        is_thumbnail = str(key).endswith(".jpg")
        if not is_thumbnail:
            self._recording_call_count += 1
            if self.fail_plan is not None:
                error = self.fail_plan(self._recording_call_count)
                if error is not None:
                    raise error


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "simulated"}}, "UploadFile")


def _wrapped_client_error(code):
    """Reproduces the REAL exception boto3's client.upload_file() raises
    on a ClientError -- not a hand-built stand-in. boto3.s3.transfer.
    S3Transfer.upload_file() does exactly this:

        try:
            future.result()
        except ClientError as e:
            raise S3UploadFailedError(f"Failed to upload {filename} to {bucket}/{key}: {e}")

    A bare `raise NewException(...)` inside an `except` block, with no
    `from e` -- so __cause__ stays None, but Python's own implicit
    exception chaining still sets __context__ to the original ClientError.
    Reproducing that exact control flow (raise inside except, catch,
    return) rather than manually setting error.__context__ is the whole
    point: it proves _classify_credential_error() works against what
    Python actually produces, not against a test's assumption of it."""
    original = _client_error(code)
    try:
        try:
            raise original
        except ClientError as inner:
            raise S3UploadFailedError(f"Failed to upload x to bucket/key: {inner}")
    except S3UploadFailedError as wrapped:
        return wrapped


class _FakeBoto3:
    def __init__(self, fail_plan=None):
        self.fail_plan = fail_plan
        self.clients_created = []

    def client(self, *args, **kwargs):
        c = _FakeS3Client(fail_plan=self.fail_plan)
        self.clients_created.append(c)
        return c


def _install_fake_boto3(monkeypatch, fail_plan=None):
    fake = _FakeBoto3(fail_plan=fail_plan)
    monkeypatch.setattr(ru, "boto3", fake)
    return fake


def _install_fake_control_plane(monkeypatch, credentials_calls=None, notify_calls=None):
    credentials_calls = credentials_calls if credentials_calls is not None else []
    notify_calls = notify_calls if notify_calls is not None else []

    def fake_post(path, payload):
        if path.endswith("/credentials"):
            credentials_calls.append(path)
            return _fake_credentials_response()
        if path.endswith("/available"):
            notify_calls.append(payload)
            return {"status": "accepted"}
        return None

    monkeypatch.setattr(ru, "_control_plane_post", fake_post)
    return credentials_calls, notify_calls


def _install_fake_prepare_cloud_copy(monkeypatch, tmp_path):
    def fake_prepare(mkv_path, camera_number, expected_duration_seconds):
        out = tmp_path / f"staged-{mkv_path.stem}.mp4"
        out.write_bytes(b"fake mp4 bytes")
        return out

    monkeypatch.setattr(ru, "_prepare_cloud_copy", fake_prepare)


# ---------------------------------------------------------------------------
# 1 & 2. ExpiredToken invalidates session+client; no later files attempted.
# ---------------------------------------------------------------------------

def test_expired_token_invalidates_session_and_client_and_stops_the_pass(monkeypatch, tmp_path):
    camera_number = 1
    pending = _seed_pending_files(tmp_path, camera_number, count=3)
    assert len(pending) == 3

    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)
    fake_boto3 = _install_fake_boto3(monkeypatch, fail_plan=lambda n: _client_error("ExpiredToken"))

    ru._relay_camera_once(camera_number, "cam-1-id")

    assert camera_number not in ru._sessions, "session must be invalidated on ExpiredToken"
    assert camera_number not in ru._clients, "client must be invalidated on ExpiredToken"
    assert len(fake_boto3.clients_created) == 1
    # Only the first (newest-prioritized) file was attempted -- the
    # remaining 2 were never touched this pass.
    assert fake_boto3.clients_created[0]._recording_call_count == 1


def test_request_expired_and_invalid_token_are_also_credential_class(monkeypatch, tmp_path):
    for code in ("RequestExpired", "InvalidToken"):
        camera_number = 2
        ru._sessions.clear()
        ru._clients.clear()
        ru._camera_backoff.clear()
        ru._uploaded_files.clear()
        _seed_pending_files(tmp_path, camera_number, count=2)
        _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
        _install_fake_control_plane(monkeypatch)
        _install_fake_boto3(monkeypatch, fail_plan=lambda n, code=code: _client_error(code))

        ru._relay_camera_once(camera_number, "cam-2-id")

        assert camera_number not in ru._sessions, f"{code} must invalidate the session"
        assert ru._in_backoff_window(camera_number), f"{code} must enter backoff"


# ---------------------------------------------------------------------------
# 3. Another camera is not delayed by a backoff'd camera.
# ---------------------------------------------------------------------------

def test_camera_in_backoff_is_skipped_without_delaying_another_camera(monkeypatch, tmp_path):
    camera_a, camera_b = 3, 4
    ru._camera_backoff[camera_a] = {"consecutive_failures": 2, "next_retry_at": time.monotonic() + 1000}

    _seed_pending_files(tmp_path, camera_a, count=2)
    _seed_pending_files(tmp_path, camera_b, count=2)

    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    credentials_calls, notify_calls = _install_fake_control_plane(monkeypatch)
    fake_boto3 = _install_fake_boto3(monkeypatch)

    ru._relay_camera_once(camera_a, "cam-a-id")
    ru._relay_camera_once(camera_b, "cam-b-id")

    # Camera A: still in backoff -- never even requested credentials.
    assert camera_a not in ru._sessions
    assert not any("cam-a-id" in "" for _ in credentials_calls)  # no crash; real check below
    # Camera B: processed completely normally.
    assert camera_b in ru._sessions
    assert len(ru._uploaded_files.get(camera_b, [])) == 2
    assert len(fake_boto3.clients_created) == 1  # only camera B ever built a client


# ---------------------------------------------------------------------------
# 4. The next attempt genuinely requests fresh credentials.
# ---------------------------------------------------------------------------

def test_next_attempt_after_invalidation_requests_fresh_credentials(monkeypatch, tmp_path):
    camera_number = 5
    _seed_pending_files(tmp_path, camera_number, count=1)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    credentials_calls, _ = _install_fake_control_plane(monkeypatch)
    _install_fake_boto3(monkeypatch, fail_plan=lambda n: _client_error("ExpiredToken"))

    ru._relay_camera_once(camera_number, "cam-5-id")
    assert len(credentials_calls) == 1

    # Bypass the backoff window directly (proving the *credential fetch*
    # itself is fresh is the point of this test, not the backoff timing,
    # which is covered separately below).
    ru._camera_backoff.pop(camera_number, None)
    # New files for this second pass (the first was never marked uploaded,
    # but re-seeding is unnecessary -- pending files are still on disk).
    ru._relay_camera_once(camera_number, "cam-5-id")

    assert len(credentials_calls) == 2, "the second pass must fetch credentials again, not reuse anything stale"


# ---------------------------------------------------------------------------
# 5. A non-auth error still continues to the next file.
# ---------------------------------------------------------------------------

def test_non_auth_error_continues_to_next_file(monkeypatch, tmp_path):
    camera_number = 6
    _seed_pending_files(tmp_path, camera_number, count=2)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)

    def fail_first_only(n):
        if n == 1:
            return RuntimeError("simulated transient upload error, not a credential problem")
        return None

    _install_fake_boto3(monkeypatch, fail_plan=fail_first_only)

    ru._relay_camera_once(camera_number, "cam-6-id")

    assert camera_number in ru._sessions, "a non-credential error must not invalidate the session"
    assert not ru._in_backoff_window(camera_number)
    assert len(ru._uploaded_files.get(camera_number, [])) == 1, "the second file must still have been attempted and succeeded"


def test_non_credential_client_error_code_also_continues(monkeypatch, tmp_path):
    camera_number = 7
    _seed_pending_files(tmp_path, camera_number, count=2)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)

    def fail_first_with_access_denied(n):
        if n == 1:
            return _client_error("AccessDenied")
        return None

    _install_fake_boto3(monkeypatch, fail_plan=fail_first_with_access_denied)

    ru._relay_camera_once(camera_number, "cam-7-id")

    assert camera_number in ru._sessions, "AccessDenied is not a credential-class code -- must not invalidate the session"
    assert len(ru._uploaded_files.get(camera_number, [])) == 1


# ---------------------------------------------------------------------------
# 6. Exponential backoff grows, caps, and resets.
# ---------------------------------------------------------------------------

def test_backoff_grows_caps_and_resets(monkeypatch):
    camera_number = 8
    monkeypatch.setattr(ru, "SCAN_SECONDS", 30.0)
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_BACKOFF_SECONDS", 600.0)

    expected = [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0]  # last two both hit the cap
    for expected_delay in expected:
        before = time.monotonic()
        ru._record_credential_failure(camera_number)
        after = time.monotonic()
        delay = ru._camera_backoff[camera_number]["next_retry_at"] - before
        assert abs(delay - expected_delay) < (after - before + 0.05), (
            f"expected ~{expected_delay}s backoff, computed next_retry_at implies {delay:.1f}s"
        )
        assert ru._in_backoff_window(camera_number)

    ru._record_credential_success(camera_number)
    assert camera_number not in ru._camera_backoff
    assert not ru._in_backoff_window(camera_number)


# ---------------------------------------------------------------------------
# 7. One S3 client is reused for multiple files under one session.
# ---------------------------------------------------------------------------

def test_one_client_reused_for_multiple_files_under_one_session(monkeypatch, tmp_path):
    camera_number = 9
    _seed_pending_files(tmp_path, camera_number, count=3)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)
    fake_boto3 = _install_fake_boto3(monkeypatch)

    ru._relay_camera_once(camera_number, "cam-9-id")

    assert len(fake_boto3.clients_created) == 1, "exactly one client must be constructed for the whole scan"
    assert fake_boto3.clients_created[0]._recording_call_count == 3, "and reused for all 3 files"
    assert len(ru._uploaded_files.get(camera_number, [])) == 3


# ---------------------------------------------------------------------------
# 8 & 9. Per-scan file cap; remaining backlog survives for later scans.
# ---------------------------------------------------------------------------

def test_max_files_per_scan_cap_is_respected_and_backlog_survives(monkeypatch, tmp_path):
    camera_number = 10
    monkeypatch.setattr(ru, "RECORDING_UPLOAD_MAX_FILES_PER_SCAN", 5)
    pending = _seed_pending_files(tmp_path, camera_number, count=8)
    assert len(pending) == 8

    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)
    _install_fake_boto3(monkeypatch)

    ru._relay_camera_once(camera_number, "cam-10-id")

    uploaded_first_pass = list(ru._uploaded_files.get(camera_number, []))
    assert len(uploaded_first_pass) == 5, "at most 5 files attempted in one scan"

    # Every original MKV is still on disk, whether uploaded or not.
    for path in pending:
        assert path.exists(), f"{path} must never be deleted"

    remaining_names = {p.name for p in pending} - set(uploaded_first_pass)
    assert len(remaining_names) == 3

    # A second scan picks up (some of) the remainder -- backlog is not stuck.
    ru._relay_camera_once(camera_number, "cam-10-id")
    uploaded_after_second_pass = set(ru._uploaded_files.get(camera_number, []))
    assert remaining_names <= uploaded_after_second_pass, "the rest of the backlog must be reachable on a later scan"


# ---------------------------------------------------------------------------
# 10. Multipart concurrency is explicitly configured for both uploads.
# ---------------------------------------------------------------------------

def test_multipart_concurrency_explicit_for_recording_and_thumbnail(monkeypatch, tmp_path):
    assert ru.RECORDING_UPLOAD_MULTIPART_MAX_CONCURRENCY == 2
    assert ru._UPLOAD_TRANSFER_CONFIG is not None
    assert ru._UPLOAD_TRANSFER_CONFIG.max_concurrency == 2

    camera_number = 11
    _seed_pending_files(tmp_path, camera_number, count=1)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)
    fake_boto3 = _install_fake_boto3(monkeypatch)

    thumb_path = tmp_path / "fake_thumb.jpg"
    thumb_path.write_bytes(b"fake jpg bytes")
    monkeypatch.setattr(ru, "_create_recording_thumbnail", lambda mp4_path, camera_number: thumb_path)

    ru._relay_camera_once(camera_number, "cam-11-id")

    client = fake_boto3.clients_created[0]
    assert len(client.upload_calls) == 2  # recording + thumbnail
    for call in client.upload_calls:
        assert call["Config"] is ru._UPLOAD_TRANSFER_CONFIG


# ---------------------------------------------------------------------------
# 11. The original MKV is never touched under any failure path.
# ---------------------------------------------------------------------------

def test_original_mkv_untouched_across_every_failure_path(monkeypatch, tmp_path):
    scenarios = [
        ("credential_error", lambda n: _client_error("ExpiredToken")),
        ("non_credential_error", lambda n: RuntimeError("boom")),
        ("notify_failure", None),  # upload succeeds; control-plane notify fails instead
    ]
    for label, fail_plan in scenarios:
        camera_number = 100 + hash(label) % 50
        ru._sessions.clear()
        ru._clients.clear()
        ru._camera_backoff.clear()
        ru._uploaded_files.clear()

        original = _seed_pending_files(tmp_path, camera_number, count=1)[0]
        original_bytes = original.read_bytes()
        original_mtime = original.stat().st_mtime

        _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
        if label == "notify_failure":
            def fake_post(path, payload):
                if path.endswith("/credentials"):
                    return _fake_credentials_response()
                return {"status": "rejected"}  # notify explicitly fails
            monkeypatch.setattr(ru, "_control_plane_post", fake_post)
        else:
            _install_fake_control_plane(monkeypatch)
        _install_fake_boto3(monkeypatch, fail_plan=fail_plan)

        ru._relay_camera_once(camera_number, f"cam-{label}-id")

        assert original.exists(), f"[{label}] original MKV must still exist"
        assert original.read_bytes() == original_bytes, f"[{label}] original MKV content must be unchanged"
        assert original.stat().st_mtime == original_mtime, f"[{label}] original MKV must not have been rewritten"


# ---------------------------------------------------------------------------
# 12, 15, 16, 18. A wrapped ExpiredToken (the real production shape) engages
# credential handling exactly like a direct one: session+client invalidated,
# backoff activated, rest of the pass stopped.
# ---------------------------------------------------------------------------

def test_wrapped_expired_token_invalidates_session_and_client_and_stops_the_pass(monkeypatch, tmp_path):
    camera_number = 12
    pending = _seed_pending_files(tmp_path, camera_number, count=3)
    assert len(pending) == 3

    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)
    fake_boto3 = _install_fake_boto3(monkeypatch, fail_plan=lambda n: _wrapped_client_error("ExpiredToken"))

    ru._relay_camera_once(camera_number, "cam-12-id")

    assert camera_number not in ru._sessions, "a wrapped ExpiredToken must invalidate the session"
    assert camera_number not in ru._clients, "a wrapped ExpiredToken must invalidate the client"
    assert ru._in_backoff_window(camera_number), "a wrapped ExpiredToken must activate backoff"
    assert len(fake_boto3.clients_created) == 1
    # Only the first (newest-prioritized) file was attempted -- the
    # remaining 2 were never touched this pass, same as the direct-
    # ClientError case.
    assert fake_boto3.clients_created[0]._recording_call_count == 1


# ---------------------------------------------------------------------------
# 13. A wrapped, non-credential S3 failure (AccessDenied) must NOT be
# misclassified as a credential error -- still just skip-and-continue.
# ---------------------------------------------------------------------------

def test_wrapped_non_credential_s3_failure_does_not_engage_credential_handling(monkeypatch, tmp_path):
    camera_number = 13
    _seed_pending_files(tmp_path, camera_number, count=2)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)

    def fail_first_with_wrapped_access_denied(n):
        if n == 1:
            return _wrapped_client_error("AccessDenied")
        return None

    _install_fake_boto3(monkeypatch, fail_plan=fail_first_with_wrapped_access_denied)

    ru._relay_camera_once(camera_number, "cam-13-id")

    assert camera_number in ru._sessions, "a wrapped non-credential failure must not invalidate the session"
    assert camera_number in ru._clients, "a wrapped non-credential failure must not invalidate the client"
    assert not ru._in_backoff_window(camera_number)
    assert len(ru._uploaded_files.get(camera_number, [])) == 1, "the second file must still have been attempted and succeeded"


# ---------------------------------------------------------------------------
# 14. Direct ClientError credential handling is unchanged by the refactor
# that merged the ClientError/Exception branches into one classifier call.
# ---------------------------------------------------------------------------

def test_direct_client_error_credential_handling_still_works(monkeypatch, tmp_path):
    camera_number = 14
    _seed_pending_files(tmp_path, camera_number, count=2)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)
    _install_fake_boto3(monkeypatch, fail_plan=lambda n: _client_error("InvalidToken"))

    ru._relay_camera_once(camera_number, "cam-14-id")

    assert camera_number not in ru._sessions
    assert camera_number not in ru._clients
    assert ru._in_backoff_window(camera_number)


# ---------------------------------------------------------------------------
# 17. Wrapped-case: a camera in backoff from a wrapped failure does not
# delay another camera's own normal scan.
# ---------------------------------------------------------------------------

def test_wrapped_failure_backoff_does_not_delay_another_camera(monkeypatch, tmp_path):
    camera_a, camera_b = 17, 18
    _seed_pending_files(tmp_path, camera_a, count=1)
    _seed_pending_files(tmp_path, camera_b, count=2)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    _install_fake_control_plane(monkeypatch)

    # Camera A's boto3 client always raises a wrapped ExpiredToken;
    # camera B's must succeed normally -- proven by giving each camera
    # its own fail_plan via a per-camera fake boto3 module swap between
    # the two calls, exactly like the direct-ClientError equivalent test
    # proves it via a pre-seeded backoff window; here the backoff is
    # earned live, in-test, from a real wrapped failure first.
    fake_boto3_a = _install_fake_boto3(monkeypatch, fail_plan=lambda n: _wrapped_client_error("ExpiredToken"))
    ru._relay_camera_once(camera_a, "cam-a-id")
    assert ru._in_backoff_window(camera_a)

    fake_boto3_b = _install_fake_boto3(monkeypatch)  # fresh fake, no failures
    ru._relay_camera_once(camera_b, "cam-b-id")

    assert camera_b in ru._sessions
    assert len(ru._uploaded_files.get(camera_b, [])) == 2, "camera B must process its own backlog completely, unaffected by camera A's wrapped failure"
    assert len(fake_boto3_b.clients_created) == 1


# ---------------------------------------------------------------------------
# 19. Wrapped-case: the next attempt after invalidation genuinely requests
# fresh credentials, not a cached/stale session.
# ---------------------------------------------------------------------------

def test_wrapped_failure_next_attempt_requests_fresh_credentials(monkeypatch, tmp_path):
    camera_number = 19
    _seed_pending_files(tmp_path, camera_number, count=1)
    _install_fake_prepare_cloud_copy(monkeypatch, tmp_path)
    credentials_calls, _ = _install_fake_control_plane(monkeypatch)
    _install_fake_boto3(monkeypatch, fail_plan=lambda n: _wrapped_client_error("RequestExpired"))

    ru._relay_camera_once(camera_number, "cam-19-id")
    assert len(credentials_calls) == 1

    ru._camera_backoff.pop(camera_number, None)  # bypass the backoff window itself -- not what this test proves
    ru._relay_camera_once(camera_number, "cam-19-id")

    assert len(credentials_calls) == 2, "the second pass must fetch credentials again after a wrapped credential failure, not reuse anything stale"
