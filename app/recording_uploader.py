"""R3 (recording-pipeline roadmap, distinct from the earlier Live Phase
1-8 numbering): appliance-side recording uploader.

Watches each local camera's completed (rotated-out) recording files in
RECORDINGS_FOLDER/camera{N}/*.mkv -- the same local recording pipeline
start_recording() already produces, unchanged, not touched by this
module -- and, only when RUNTIME_ROLE is edge/combined AND
ANYAICAM_RECORDING_UPLOAD_ENABLED is true, requests short-lived S3
upload credentials from R1's own
POST /api/appliance/recordings/{camera_id}/credentials, PutObjects each
completed file, then notifies R2's
POST /api/appliance/recordings/{camera_id}/available with tiny JSON
metadata -- never any other cloud call, and never a media byte through
FastAPI. Mirrors live_relay_uploader.py's security model file-for-file:
same credential.json identity, same control-plane bearer+nonce+
timestamp auth, same fail-closed session validation, same path-
containment double-check before any file is touched. Authorization/
control-plane-request logic is deliberately duplicated from
live_relay_uploader.py rather than imported -- the same explicit scope
decision that module's own docstring already made relative to
live_playlist.py, kept consistent here.

Recording upload is deliberately NOT gated by a per-camera "active"
flag the way live relay is: local recording itself is already
continuous/ambient (policy-driven by the appliance's own
configuration, independent of any customer currently watching), so
this worker uploads whatever completed files exist for every one of
this appliance's cameras that the existing, unchanged
GET /api/appliance/configuration endpoint reports -- once the outer
RUNTIME_ROLE/flag gate is satisfied. That endpoint already returns
each camera's id/site_id/camera_number; this module reuses it as the
camera_number -> camera_id/site_id mapping rather than inventing a
second channel for the same information.

Failure handling deliberately differs from live relay's own "drop,
never retry" segment policy: a live segment is 2 seconds of an
ongoing stream and losing one is inconsequential, but a recording is
the durable evidence a VMS exists to keep, so a failed upload is left
OFF this camera's "already handled" list and is retried on the next
scan rather than dropped -- see _relay_camera_once()'s docstring for
the exact mechanism.

Playback format: MKV itself is not reliably playable in a browser
<video> element regardless of interior codec (no MKV demuxer in
Chrome/Edge/Firefox), so every completed recording gets a browser-
compatible cloud copy prepared before upload -- automatically, per
file, based on its own actual codec (_detect_video_codec()), never a
fixed assumption and never something a customer or installer has to
configure:

  - H.264 (confirmed on the pilot camera, 2026-08-21): stream-copy
    remux into a standard, faststart MP4 -- no re-encode, negligible
    CPU (_remux_to_mp4()).
  - H.265/HEVC: a real re-encode to H.264 is required -- browser HEVC
    support is inconsistent enough that a stream-copy remux alone
    would not be reliably playable. A fast x264 preset bounds the
    CPU cost per job, and a module-level semaphore
    (TRANSCODE_MAX_CONCURRENCY, default 1) bounds how many transcodes
    ever run at once on this appliance, regardless of how many
    cameras have pending files -- see _transcode_hevc_to_h264().
  - Anything else, or a codec ffprobe can't even determine: fails
    safe. No cloud copy is prepared, no catalog notification is ever
    sent for that file (so no broken Playback entry can exist), the
    reason is logged once, and the original recording is left exactly
    as it was -- see _prepare_cloud_copy().

This module never touches start_recording(), RECORDINGS_FOLDER's own
naming, camera_url(), or the local recording pipeline in any way --
local recording behavior is identical whether or not this worker is
enabled. The original MKV is never opened for writing, moved, or
deleted by this module under any outcome -- ffmpeg only ever reads it;
the derived MP4 (remuxed or transcoded) lives in a dedicated
per-camera staging subfolder and is the only file this module ever
removes.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None
    TransferConfig = None
    ClientError = None

logger = logging.getLogger("anyaicam.recording_uploader")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
RECORDING_UPLOAD_ENABLED = os.environ.get("ANYAICAM_RECORDING_UPLOAD_ENABLED", "false").strip().lower() == "true"
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
STATE_DIR = Path(os.environ.get("ANYAICAM_STATE_DIR", "/var/lib/anyaicam"))
CREDENTIAL_FILE = STATE_DIR / "credential.json"
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip()
# RECORDINGS_FOLDER is intentionally hardcoded, matching main.py's own
# RECORDINGS_FOLDER constant exactly -- this must always agree with where
# start_recording() actually writes, not be independently configurable.
RECORDINGS_FOLDER = Path("/app/recordings")
SCAN_SECONDS = max(5.0, float(os.environ.get("ANYAICAM_RECORDING_UPLOAD_SCAN_SECONDS", "30.0")))
CONFIG_REFRESH_SECONDS = max(60.0, float(os.environ.get("ANYAICAM_RECORDING_UPLOAD_CONFIG_REFRESH_SECONDS", "300.0")))
SESSION_RENEW_MARGIN_SECONDS = max(30, int(os.environ.get("ANYAICAM_RECORDING_UPLOAD_SESSION_RENEW_MARGIN_SECONDS", "120")))
MAX_TRACKED_FILES_PER_CAMERA = 200  # generous vs. live relay's 50: a recording that fails upload stays untracked (retried), so this only bounds successes
REMUX_TIMEOUT_SECONDS = max(30, int(os.environ.get("ANYAICAM_RECORDING_REMUX_TIMEOUT_SECONDS", "120")))
DURATION_VERIFY_TOLERANCE_SECONDS = 10.0  # ffprobe's reported duration vs. (ended_at - started_at); relative tolerance below scales this up for longer clips
CODEC_DETECT_TIMEOUT_SECONDS = 30
# H.265/HEVC needs a real re-encode -- these bound its CPU footprint on the
# appliance. TRANSCODE_MAX_CONCURRENCY is the queued/limited-concurrency
# mechanism: at most this many transcodes run at once, across every camera
# on this appliance, regardless of how many files are pending; everything
# beyond that blocks (queues) on _transcode_semaphore.acquire() rather than
# starting. TRANSCODE_THREADS additionally caps ffmpeg's own internal
# thread count per job, so even a single transcode doesn't claim every core.
TRANSCODE_PRESET = os.environ.get("ANYAICAM_RECORDING_TRANSCODE_PRESET", "veryfast").strip()
TRANSCODE_CRF = max(0, int(os.environ.get("ANYAICAM_RECORDING_TRANSCODE_CRF", "23")))
TRANSCODE_THREADS = max(1, int(os.environ.get("ANYAICAM_RECORDING_TRANSCODE_THREADS", "2")))
TRANSCODE_TIMEOUT_SECONDS = max(60, int(os.environ.get("ANYAICAM_RECORDING_TRANSCODE_TIMEOUT_SECONDS", "600")))
TRANSCODE_MAX_CONCURRENCY = max(1, int(os.environ.get("ANYAICAM_RECORDING_TRANSCODE_MAX_CONCURRENCY", "1")))

# ExpiredToken/RequestExpired/InvalidToken: AWS's own vocabulary for "this
# credential set itself is no good any more" -- as opposed to a one-off
# network blip, a single corrupt file, or an S3 permission/bucket problem,
# none of which mean every other pending file will also fail the exact
# same way. Retrying the identical (bad) session against every remaining
# file in a scan wastes calls and floods logs for no benefit -- see
# _relay_camera_once()'s own handling below.
#
# 2026-09-03 correction: a real ExpiredToken from client.upload_file()
# (the high-level, multipart-capable method _upload_recording() actually
# calls) never arrives as a raw ClientError. boto3's own S3Transfer.
# upload_file() catches it internally and re-raises as
# boto3.exceptions.S3UploadFailedError(f"Failed to upload {filename} to
# {bucket}/{key}: {e}") -- a bare `raise NewError(...)` inside the
# `except ClientError as e:` block, which does NOT inherit from
# ClientError (MRO: S3UploadFailedError -> Boto3Error -> Exception) but
# DOES get e attached as __context__ via Python's own implicit exception
# chaining (no `from e` was used, so __cause__ stays None -- __context__
# is what's actually set). _classify_credential_error() below is what
# actually recovers the real code from either shape; CREDENTIAL_ERROR_CODES
# itself stays just the set of codes to recognize once found.
CREDENTIAL_ERROR_CODES = frozenset({"ExpiredToken", "RequestExpired", "InvalidToken"})
RECORDING_UPLOAD_MAX_BACKOFF_SECONDS = max(1.0, float(os.environ.get("ANYAICAM_RECORDING_UPLOAD_MAX_BACKOFF_SECONDS", "600")))
# Bounds how many files one camera's scan will actually attempt to
# codec-detect/remux-or-transcode/upload in a single pass -- a large
# backlog (uploader off for days, or credentials broken for a while)
# drains across several scans instead of one scan occupying its
# asyncio.to_thread() worker for an unbounded amount of time. Nothing is
# ever dropped: files beyond this cap simply remain pending and are
# picked up on a later scan, exactly like any other not-yet-uploaded file.
RECORDING_UPLOAD_MAX_FILES_PER_SCAN = max(1, int(os.environ.get("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "5")))
# boto3's own default (10) is never explicitly set anywhere in this file
# otherwise -- start conservatively; can be raised later once real
# Samsung/network behavior under load has been measured.
RECORDING_UPLOAD_MULTIPART_MAX_CONCURRENCY = max(1, int(os.environ.get("ANYAICAM_RECORDING_UPLOAD_MULTIPART_MAX_CONCURRENCY", "2")))

recording_upload_state: dict = {"worker_status": "disabled", "last_scan_at": None, "last_config_refresh_at": None, "last_error": None}

_lock = threading.Lock()
_camera_map: dict[int, dict] = {}          # camera_number -> {"camera_id":..., "site_id":...}, refreshed periodically
_sessions: dict[int, dict] = {}            # camera_number -> {credentials, bucket, key_prefix, expires_at}
_clients: dict[int, object] = {}           # camera_number -> cached boto3 S3 client -- always rebuilt together with _sessions' own entry, never reused across a credential refresh
_camera_backoff: dict[int, dict] = {}      # camera_number -> {"consecutive_failures": int, "next_retry_at": float (time.monotonic())} -- credential-failure backoff only, see _record_credential_failure()
_uploaded_files: dict[int, list[str]] = {}  # camera_number -> successfully uploaded+notified filenames (bounded)
_unsupported_codec_files: dict[int, set[str]] = {}  # camera_number -> filenames already logged as unsupported/undetected this process lifetime -- avoids re-probing/re-logging the same permanently-bad file every scan
_transcode_semaphore = threading.Semaphore(TRANSCODE_MAX_CONCURRENCY)
_UPLOAD_TRANSFER_CONFIG = TransferConfig(max_concurrency=RECORDING_UPLOAD_MULTIPART_MAX_CONCURRENCY) if TransferConfig is not None else None


def _load_appliance_identity() -> tuple[str, str] | None:
    try:
        data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    appliance_id = str(data.get("appliance_id") or "").strip()
    credential = str(data.get("credential") or "").strip()
    if not appliance_id or not credential:
        return None
    return appliance_id, credential


def _control_plane_headers(appliance_id: str, credential: str) -> dict:
    return {
        "User-Agent": "AnyAiCam-RecordingUpload/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }


def _control_plane_post(path: str, payload: dict) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    headers = {"Content-Type": "application/json", **_control_plane_headers(appliance_id, credential)}
    request = urllib.request.Request(
        CLOUD_URL + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("recording_upload.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("recording_upload.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _control_plane_get(path: str) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    request = urllib.request.Request(CLOUD_URL + path, headers=_control_plane_headers(appliance_id, credential), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("recording_upload.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("recording_upload.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _refresh_camera_map() -> None:
    """Polls the existing, unchanged GET /api/appliance/configuration for
    this appliance's own camera_number -> camera_id/site_id mapping.
    Never writes anything; a failed/unreachable poll just leaves the
    previous mapping in place, so a transient network blip never stops
    already-known cameras from continuing to upload."""
    response = _control_plane_get("/api/appliance/configuration")
    if not isinstance(response, dict):
        return
    cameras = response.get("cameras")
    if not isinstance(cameras, list):
        return
    mapping: dict[int, dict] = {}
    for item in cameras:
        if not isinstance(item, dict):
            continue
        camera_id = item.get("id")
        camera_number = item.get("camera_number")
        site_id = item.get("site_id")
        if not isinstance(camera_id, str) or not camera_id.strip():
            continue
        if isinstance(camera_number, bool) or not isinstance(camera_number, int):
            continue
        if not isinstance(site_id, str) or not site_id.strip():
            continue
        mapping[camera_number] = {"camera_id": camera_id, "site_id": site_id}
    with _lock:
        _camera_map.clear()
        _camera_map.update(mapping)
    recording_upload_state["last_config_refresh_at"] = datetime.now().isoformat()


def _known_camera_numbers() -> list[int]:
    with _lock:
        return sorted(_camera_map)


def _camera_identity(camera_number: int) -> dict | None:
    with _lock:
        return _camera_map.get(camera_number)


def _session_expires_soon(session: dict) -> bool:
    expires_at = session.get("expires_at") if isinstance(session, dict) else None
    if not isinstance(expires_at, str) or not expires_at.strip():
        return True
    try:
        expiration = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return True
    if expiration.tzinfo is None or expiration.tzinfo.utcoffset(expiration) is None:
        return True
    try:
        remaining = (expiration - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, OverflowError):
        return True
    return remaining <= SESSION_RENEW_MARGIN_SECONDS


def _ensure_session(camera_number: int, camera_id: str) -> dict | None:
    session = _sessions.get(camera_number)
    if session and not _session_expires_soon(session):
        return session
    response = _control_plane_post(f"/api/appliance/recordings/{camera_id}/credentials", {})

    def _invalid(reason: str) -> None:
        # Log only camera number/id and a fixed reason code -- never the
        # response body, which may contain live AWS credentials.
        logger.warning(
            "recording_upload.session_response_invalid camera_number=%s camera_id=%s reason=%s",
            camera_number, camera_id, reason,
        )

    if not isinstance(response, dict):
        _invalid("not_a_dict")
        return None
    credentials = response.get("credentials")
    if not isinstance(credentials, dict):
        _invalid("credentials_not_a_dict")
        return None
    for key in ("access_key_id", "secret_access_key", "session_token"):
        if not isinstance(credentials.get(key), str) or not credentials[key].strip():
            _invalid(f"missing_{key}")
            return None
    expiration = credentials.get("expiration")
    if not isinstance(expiration, str) or not expiration.strip():
        _invalid("missing_expiration")
        return None
    for key in ("bucket", "key_prefix"):
        if not isinstance(response.get(key), str) or not response[key].strip():
            _invalid(f"missing_{key}")
            return None
    session = {
        "credentials": credentials,
        "bucket": response["bucket"],
        "key_prefix": response["key_prefix"],
        "expires_at": expiration,
    }
    _sessions[camera_number] = session
    # A freshly (re-)issued session invalidates whatever S3 client was
    # built from the previous one -- that client has the old credentials
    # baked in at construction time. _ensure_client() rebuilds on demand.
    _clients.pop(camera_number, None)
    return session


def _ensure_client(camera_number: int, session: dict):
    """One boto3 S3 client per camera, reused for every file in a scan --
    rather than the previous per-file construction. Tied 1:1 to the
    currently cached session: _ensure_session() clears this entry
    whenever it issues a new session, and _invalidate_session() below
    clears both together on a credential-class failure, so this can
    never silently outlive the credentials it was built from."""
    client = _clients.get(camera_number)
    if client is not None:
        return client
    if boto3 is None:
        return None
    creds = session["credentials"]
    client = boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        aws_session_token=creds["session_token"],
    )
    _clients[camera_number] = client
    return client


def _invalidate_session(camera_number: int) -> None:
    """Called on a credential-class S3 failure (ExpiredToken/
    RequestExpired/InvalidToken) -- discards both the cached session and
    its client together, so the next attempt for this camera is
    guaranteed to request genuinely fresh credentials rather than
    trusting the same (apparently wrong) self-reported expiration again."""
    _sessions.pop(camera_number, None)
    _clients.pop(camera_number, None)


def _credential_error_code(error: object) -> str | None:
    """The AWS error code if `error` itself is a ClientError, else None.
    Never raises -- a malformed/missing response dict just yields None,
    same as "not a credential error", rather than blowing up the caller's
    own error-handling path."""
    if isinstance(error, ClientError):
        return (getattr(error, "response", None) or {}).get("Error", {}).get("Code")
    return None


def _classify_credential_error(error: BaseException) -> str | None:
    """Returns the AWS error code if `error` is, or wraps, a credential-
    class ClientError (one of CREDENTIAL_ERROR_CODES) -- else None,
    including when it wraps some OTHER, non-credential ClientError (e.g.
    AccessDenied, NoSuchBucket) or isn't ClientError-shaped at all (a
    corrupt file, a network blip). The membership check against
    CREDENTIAL_ERROR_CODES lives here, not in the caller, so "is this a
    credential error" has exactly one answer regardless of which of the
    two real shapes it arrived in.

    Checks `error` itself first (the direct-ClientError shape a
    low-level call like put_object() would raise), then one level into
    __cause__ and __context__ (the wrapped shape client.upload_file()
    actually raises in production -- see CREDENTIAL_ERROR_CODES's own
    comment above for exactly why). boto3 never nests more than one
    level deep here, so this deliberately does not walk further."""
    for candidate in (error, getattr(error, "__cause__", None), getattr(error, "__context__", None)):
        code = _credential_error_code(candidate)
        if code in CREDENTIAL_ERROR_CODES:
            return code
    return None


def _in_backoff_window(camera_number: int) -> bool:
    entry = _camera_backoff.get(camera_number)
    if not entry:
        return False
    return time.monotonic() < entry.get("next_retry_at", 0.0)


def _record_credential_failure(camera_number: int) -> None:
    """Bounded exponential backoff, credential failures only -- starts
    at the normal scan interval, doubles each consecutive failure, caps
    at RECORDING_UPLOAD_MAX_BACKOFF_SECONDS. This is state, not a sleep:
    recording_upload_worker()'s own per-camera loop stays a plain,
    non-blocking sequential scan -- _in_backoff_window() lets a camera
    still inside its window be skipped in the time it takes to check one
    dict, so a credential problem on one camera never delays any other
    camera's own normal scan."""
    entry = _camera_backoff.setdefault(camera_number, {"consecutive_failures": 0, "next_retry_at": 0.0})
    entry["consecutive_failures"] += 1
    delay = min(
        SCAN_SECONDS * (2 ** (entry["consecutive_failures"] - 1)),
        RECORDING_UPLOAD_MAX_BACKOFF_SECONDS,
    )
    entry["next_retry_at"] = time.monotonic() + delay
    logger.warning(
        "recording_upload.credential_backoff camera=%s consecutive_failures=%s next_retry_in_seconds=%.0f",
        camera_number, entry["consecutive_failures"], delay,
    )


def _record_credential_success(camera_number: int) -> None:
    _camera_backoff.pop(camera_number, None)


def _recording_folder(camera_number: int) -> Path:
    return RECORDINGS_FOLDER / f"camera{camera_number}"


def _staging_folder(camera_number: int) -> Path:
    return _recording_folder(camera_number) / "_upload_staging"


def _cleanup_staged_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _verify_output_duration(mp4_path: Path, expected_duration_seconds: float, camera_number: int) -> bool:
    """Advisory when ffprobe itself can't be run at all (missing binary,
    or times out) -- proceeds without verifying rather than blocking
    every upload on an optional check. A real mismatch once ffprobe
    DOES successfully report a duration is treated as a genuine remux
    failure, not advisory -- this is the "preserve duration accurately"
    guarantee actually being checked, not just assumed."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(mp4_path)],
            capture_output=True, timeout=30, text=True, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode != 0 or not result.stdout.strip():
        return True
    try:
        actual_duration = float(result.stdout.strip())
    except ValueError:
        return True
    tolerance = max(DURATION_VERIFY_TOLERANCE_SECONDS, expected_duration_seconds * 0.1)
    if abs(actual_duration - expected_duration_seconds) > tolerance:
        logger.warning(
            "recording_upload.remux_duration_mismatch camera=%s expected=%.1f actual=%.1f",
            camera_number, expected_duration_seconds, actual_duration,
        )
        return False
    return True


def _remux_to_mp4(mkv_path: Path, camera_number: int, expected_duration_seconds: float) -> Path | None:
    """Stream-copy remux (no re-encode -- the pilot camera's own codec
    is confirmed H.264, 2026-08-21) from the original MKV into a
    browser-playable MP4 with the moov atom moved to the front
    (+faststart), so a presigned S3 GET URL is seekable via ordinary
    HTTP Range requests with no server-side change needed. Every
    stream present is copied (-map 0 -c copy) rather than hardcoding
    which tracks exist, matching start_recording()'s own optional-audio
    convention (0:a:0?).

    The original MKV is never opened for writing, never moved, never
    deleted -- ffmpeg only ever reads it here. The MP4 is written to a
    dedicated per-camera staging subfolder; the caller is responsible
    for removing it once its own upload attempt concludes.

    Returns None (caller skips this file, retried next scan) on any
    failure: ffmpeg missing, non-zero exit, empty/missing output, or a
    duration ffprobe reports as implausibly different from the
    source's actual (ended_at - started_at) -- never uploads a file
    that might be truncated or corrupt."""
    staging_folder = _staging_folder(camera_number)
    try:
        staging_folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    output_path = staging_folder / (mkv_path.stem + ".mp4")

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(mkv_path), "-map", "0", "-c", "copy", "-movflags", "+faststart", str(output_path)],
            capture_output=True, timeout=REMUX_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("recording_upload.remux_failed camera=%s path=%s error=%s", camera_number, mkv_path, error)
        _cleanup_staged_file(output_path)
        return None

    if result.returncode != 0:
        logger.warning("recording_upload.remux_nonzero_exit camera=%s path=%s code=%s", camera_number, mkv_path, result.returncode)
        _cleanup_staged_file(output_path)
        return None

    try:
        output_size = output_path.stat().st_size
    except OSError:
        return None
    if output_size <= 0:
        logger.warning("recording_upload.remux_empty_output camera=%s path=%s", camera_number, mkv_path)
        _cleanup_staged_file(output_path)
        return None

    if not _verify_output_duration(output_path, expected_duration_seconds, camera_number):
        _cleanup_staged_file(output_path)
        return None

    return output_path


def _recording_filename_pattern(camera_number: int) -> re.Pattern:
    # Requires the literal "camera{N}_" prefix for *this* camera followed by
    # start_recording()'s own strftime shape, ".mkv" only -- anything else
    # (a different extension, an embedded path, an unexpected character) is
    # rejected rather than opened. A second, independent check on top of
    # Path(...).name already stripping any directory component below.
    return re.compile(rf"^camera{camera_number}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}\.mkv$")


def _recording_started_at(local_path: Path, camera_number: int) -> datetime | None:
    prefix = f"camera{camera_number}_"
    try:
        return datetime.strptime(local_path.stem.removeprefix(prefix), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def _completed_recording_files(camera_number: int) -> list[Path]:
    """Every *closed* recording file for this camera. The most recently
    created file is presumed still being actively written by ffmpeg and
    is deliberately excluded -- start_recording()'s own segment rotation
    only begins a new file once the previous one is fully closed, so
    every file except the newest is guaranteed complete. Every filename
    is validated against this camera's own naming pattern before being
    returned; a resolved-path containment check happens again in
    _pending_recording_files() below, mirroring live_relay_uploader.py's
    own belt-and-suspenders structure exactly."""
    folder = _recording_folder(camera_number)
    pattern = _recording_filename_pattern(camera_number)
    try:
        candidates = [item for item in folder.iterdir() if item.is_file() and pattern.fullmatch(item.name)]
    except OSError:
        return []
    candidates.sort(key=lambda item: item.name)
    return candidates[:-1] if len(candidates) > 1 else []


def _pending_recording_files(camera_number: int, already_uploaded: set[str]) -> list[Path]:
    folder_resolved = _recording_folder(camera_number).resolve()
    pending = []
    for local_path in _completed_recording_files(camera_number):
        if local_path.name in already_uploaded:
            continue
        try:
            resolved = local_path.resolve()
        except OSError:
            continue
        if resolved.parent != folder_resolved:
            logger.warning("recording_upload.file_path_outside_recordings_folder camera=%s path=%s", camera_number, resolved)
            continue
        pending.append(local_path)
    return pending


def _detect_video_codec(mkv_path: Path) -> str | None:
    """ffprobe's first video stream's codec_name, lowercased. None means
    "couldn't determine" -- callers must treat that identically to a
    genuinely unsupported codec, never assume h264 by default."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(mkv_path)],
            capture_output=True, timeout=CODEC_DETECT_TIMEOUT_SECONDS, text=True, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("recording_upload.codec_detect_failed path=%s error=%s", mkv_path, error)
        return None
    if result.returncode != 0:
        return None
    codec = result.stdout.strip().lower()
    return codec or None


def _transcode_hevc_to_h264(mkv_path: Path, camera_number: int, expected_duration_seconds: float) -> Path | None:
    """Real re-encode via ffmpeg's libx264 encoder -- H.265/HEVC has no
    free (stream-copy) path to a codec browsers reliably decode, unlike
    H.264. TRANSCODE_PRESET is chosen for speed over compression
    efficiency (this is a one-time background job, not a live stream),
    and TRANSCODE_THREADS caps ffmpeg's own thread use per job so a
    single transcode doesn't claim every core on the appliance.

    _transcode_semaphore is the queued/limited-concurrency mechanism:
    acquire() blocks (queues) here, with no timeout, until fewer than
    TRANSCODE_MAX_CONCURRENCY transcodes are already running anywhere
    on this appliance -- safe to block indefinitely since this always
    runs inside asyncio.to_thread(), never on the event loop itself.
    Audio is stream-copied unchanged (already AAC, already
    MP4-compatible) -- only the video stream is actually re-encoded."""
    staging_folder = _staging_folder(camera_number)
    try:
        staging_folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    output_path = staging_folder / (mkv_path.stem + ".mp4")

    _transcode_semaphore.acquire()
    try:
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(mkv_path),
                 "-map", "0:v:0", "-map", "0:a:0?",
                 "-c:v", "libx264", "-preset", TRANSCODE_PRESET, "-crf", str(TRANSCODE_CRF),
                 "-threads", str(TRANSCODE_THREADS),
                 "-c:a", "copy", "-movflags", "+faststart", str(output_path)],
                capture_output=True, timeout=TRANSCODE_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.warning("recording_upload.transcode_failed camera=%s path=%s error=%s", camera_number, mkv_path, error)
            _cleanup_staged_file(output_path)
            return None
    finally:
        _transcode_semaphore.release()

    if result.returncode != 0:
        logger.warning("recording_upload.transcode_nonzero_exit camera=%s path=%s code=%s", camera_number, mkv_path, result.returncode)
        _cleanup_staged_file(output_path)
        return None

    try:
        output_size = output_path.stat().st_size
    except OSError:
        return None
    if output_size <= 0:
        logger.warning("recording_upload.transcode_empty_output camera=%s path=%s", camera_number, mkv_path)
        _cleanup_staged_file(output_path)
        return None

    if not _verify_output_duration(output_path, expected_duration_seconds, camera_number):
        _cleanup_staged_file(output_path)
        return None

    return output_path


def _prepare_cloud_copy(mkv_path: Path, camera_number: int, expected_duration_seconds: float) -> Path | None:
    """Codec-aware dispatch -- the customer/installer never chooses a
    conversion method, this decides automatically per file:

      - h264 -> _remux_to_mp4() (stream copy, negligible CPU)
      - hevc/h265 -> _transcode_hevc_to_h264() (real re-encode,
        concurrency-limited)
      - anything else, or undetectable -> fails safe: no cloud copy is
        prepared, so _relay_camera_once() never notifies R2's catalog
        endpoint for this file and no broken Playback entry can ever
        exist for it. Logged once (not every scan) via
        _unsupported_codec_files -- the original recording is left
        exactly as it was in every case."""
    already_known_unsupported = _unsupported_codec_files.get(camera_number, set())
    if mkv_path.name in already_known_unsupported:
        return None

    codec = _detect_video_codec(mkv_path)
    if codec == "h264":
        return _remux_to_mp4(mkv_path, camera_number, expected_duration_seconds)
    if codec in {"hevc", "h265"}:
        return _transcode_hevc_to_h264(mkv_path, camera_number, expected_duration_seconds)

    logger.warning(
        "recording_upload.codec_unsupported camera=%s path=%s codec=%s",
        camera_number, mkv_path, codec or "undetected",
    )
    _unsupported_codec_files.setdefault(camera_number, set()).add(mkv_path.name)
    return None


def _remember_uploaded(camera_number: int, filename: str) -> None:
    uploaded = _uploaded_files.setdefault(camera_number, [])
    uploaded.append(filename)
    del uploaded[:-MAX_TRACKED_FILES_PER_CAMERA]


def _create_recording_thumbnail(mp4_path: Path, camera_number: int) -> Path | None:
    """Extract a small JPEG preview from the already-prepared cloud MP4.

    Thumbnail failure never blocks the recording upload. The JPEG is a
    transient staging artifact and is removed by the caller.
    """
    thumbnail_path = mp4_path.with_suffix(".jpg")
    _cleanup_staged_file(thumbnail_path)

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "5",
                "-i", str(mp4_path),
                "-frames:v", "1",
                "-vf", "scale=320:-2",
                "-q:v", "3",
                str(thumbnail_path),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning(
            "recording_upload.thumbnail_failed camera=%s path=%s error=%s",
            camera_number, mp4_path, error,
        )
        _cleanup_staged_file(thumbnail_path)
        return None

    try:
        valid = result.returncode == 0 and thumbnail_path.stat().st_size > 0
    except OSError:
        valid = False

    if not valid:
        logger.warning(
            "recording_upload.thumbnail_nonzero_or_empty camera=%s path=%s code=%s",
            camera_number, mp4_path, result.returncode,
        )
        _cleanup_staged_file(thumbnail_path)
        return None

    return thumbnail_path


def _upload_recording(
    client,
    session: dict,
    local_path: Path,
    started_at: datetime,
    camera_number: int,
) -> str:
    # client is caller-provided (one per session, reused across every file
    # in a scan -- see _ensure_client()) rather than constructed here per
    # file. Caller guarantees it is not None before calling this.
    date_part = started_at.strftime("%Y/%m/%d")
    recording_key = f"{session['key_prefix']}{date_part}/{local_path.name}"

    client.upload_file(
        str(local_path),
        session["bucket"],
        recording_key,
        ExtraArgs={"ContentType": "video/mp4"},
        Config=_UPLOAD_TRANSFER_CONFIG,
    )

    thumbnail_path = _create_recording_thumbnail(local_path, camera_number)
    if thumbnail_path is not None:
        thumbnail_key = recording_key.rsplit(".", 1)[0] + ".jpg"
        try:
            client.upload_file(
                str(thumbnail_path),
                session["bucket"],
                thumbnail_key,
                ExtraArgs={"ContentType": "image/jpeg"},
                Config=_UPLOAD_TRANSFER_CONFIG,
            )
            logger.info(
                "recording_upload.thumbnail_uploaded camera=%s key=%s",
                camera_number, thumbnail_key,
            )
        except Exception as error:
            logger.warning(
                "recording_upload.thumbnail_upload_failed camera=%s error=%s",
                camera_number, error,
            )
        finally:
            _cleanup_staged_file(thumbnail_path)

    return recording_key


def _relay_camera_once(camera_number: int, camera_id: str) -> None:
    """A failed codec-prep (remux or transcode -- see
    _prepare_cloud_copy()), upload, or catalog notification is
    deliberately NOT added to _uploaded_files -- unlike live relay's
    segments, a recording is durable evidence, not a disposable
    2-second fragment, so it stays pending and is retried on the next
    scan instead of being dropped (an unsupported/undetectable codec
    is the one exception -- see _prepare_cloud_copy()'s own per-process
    de-dup, which never retries or re-logs it, but still never touches
    or deletes the original either). The original MKV is untouched by
    every one of these paths; only the derived MP4 staging file is
    ever cleaned up, and only after its own upload attempt concludes.

    A credential-class S3 failure (ExpiredToken/RequestExpired/
    InvalidToken -- see CREDENTIAL_ERROR_CODES) is handled differently
    from every other failure here: the cached session AND its S3 client
    are both invalidated, this camera enters bounded-exponential backoff
    (_record_credential_failure()), and the REST of this camera's
    pending backlog is left untouched for this pass -- retrying every
    remaining file against the same (already-proven-bad) credentials
    would only waste calls and flood logs. This is recognized whether
    the failure arrives as a direct ClientError or -- the shape
    client.upload_file() actually raises in production -- wrapped in
    boto3.exceptions.S3UploadFailedError; see _classify_credential_error()
    for exactly how the wrapped case is unwrapped via __cause__/
    __context__. Any other exception (a single corrupt file, a transient
    network error, a bucket/permission problem, or an S3UploadFailedError
    that turns out to wrap some other, non-credential ClientError) keeps
    the existing behavior exactly: log, skip just that one file, continue
    to the next."""
    if _in_backoff_window(camera_number):
        return

    session = _ensure_session(camera_number, camera_id)
    if not session:
        return
    client = _ensure_client(camera_number, session)
    if client is None:
        return

    already = set(_uploaded_files.get(camera_number, []))
    pending = _pending_recording_files(camera_number, already)

    # Prioritize the newest completed recording so fresh Playback
    # footage is not trapped behind historical upload backlog.
    # Remaining recordings continue oldest-first so backlog still drains.
    if len(pending) > 1:
        pending = [pending[-1], *pending[:-1]]

    attempted = 0
    for local_path in pending:
        if attempted >= RECORDING_UPLOAD_MAX_FILES_PER_SCAN:
            break  # remainder stays pending -- picked up on a later scan, never dropped

        if not local_path.exists():
            continue
        started_at = _recording_started_at(local_path, camera_number)
        if started_at is None:
            logger.warning("recording_upload.unparseable_filename camera=%s path=%s", camera_number, local_path)
            continue
        try:
            stat = local_path.stat()
        except OSError:
            continue
        ended_at = datetime.fromtimestamp(stat.st_mtime)
        expected_duration_seconds = max(0.0, (ended_at - started_at).total_seconds())

        # Counts toward the per-scan cap here -- this is where real,
        # potentially expensive work (codec probe, remux/transcode,
        # upload) actually begins, regardless of how it concludes.
        attempted += 1

        mp4_path = _prepare_cloud_copy(local_path, camera_number, expected_duration_seconds)
        if mp4_path is None:
            continue  # unsupported codec, or remux/transcode failed -- original MKV untouched either way

        try:
            size_bytes = mp4_path.stat().st_size
            recording_key = _upload_recording(client, session, mp4_path, started_at, camera_number)
        except Exception as error:
            # Single handler for both real shapes a credential-class
            # failure can arrive in (direct ClientError, or wrapped in
            # S3UploadFailedError by client.upload_file() -- see
            # _classify_credential_error()'s own docstring) so "is this
            # a credential error" is answered identically either way,
            # instead of only being checked in a ClientError-specific
            # branch a wrapped failure would never reach.
            code = _classify_credential_error(error)
            if code is not None:
                logger.warning("recording_upload.credential_error camera=%s code=%s", camera_number, code)
                _invalidate_session(camera_number)
                _record_credential_failure(camera_number)
                _cleanup_staged_file(mp4_path)
                return  # stop this camera's backlog for this pass; other cameras are unaffected
            logger.warning("recording_upload.file_upload_failed camera=%s path=%s error=%s", camera_number, local_path, error)
            _cleanup_staged_file(mp4_path)
            continue

        # A real upload against these credentials just succeeded --
        # proof the session is genuinely good, not just recently issued.
        _record_credential_success(camera_number)

        response = _control_plane_post(
            f"/api/appliance/recordings/{camera_id}/available",
            {
                "s3_key": recording_key,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_seconds": max(0, int(expected_duration_seconds)),
                "size_bytes": size_bytes,
            },
        )
        _cleanup_staged_file(mp4_path)  # transient artifact -- the S3 object is already durable at this point regardless of the notify outcome below

        if not isinstance(response, dict) or response.get("status") not in {"accepted", "duplicate"}:
            logger.warning("recording_upload.notify_failed camera=%s path=%s", camera_number, local_path)
            continue
        _remember_uploaded(camera_number, local_path.name)


async def recording_upload_worker() -> None:
    if RUNTIME_ROLE not in {"edge", "combined"} or not RECORDING_UPLOAD_ENABLED:
        recording_upload_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    recording_upload_state["worker_status"] = "running" if boto3 is not None else "dependency_missing"
    logger.info("recording_upload.worker_started status=%s", recording_upload_state["worker_status"])
    last_config_refresh = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_config_refresh >= CONFIG_REFRESH_SECONDS:
                await asyncio.to_thread(_refresh_camera_map)
                last_config_refresh = now
            for camera_number in _known_camera_numbers():
                identity = _camera_identity(camera_number)
                if not identity:
                    continue
                await asyncio.to_thread(_relay_camera_once, camera_number, identity["camera_id"])
            recording_upload_state["last_scan_at"] = datetime.now().isoformat()
            recording_upload_state["last_error"] = None
            await asyncio.sleep(SCAN_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            recording_upload_state["last_error"] = str(error)
            logger.warning("recording_upload.worker_iteration_failed error=%s", error)
            await asyncio.sleep(SCAN_SECONDS)
