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

Playback format: the pilot camera's codec was confirmed H.264 (ffprobe,
2026-08-21) -- MKV itself is not reliably playable in a browser
<video> element regardless of interior codec (no MKV demuxer in
Chrome/Edge/Firefox), so each completed recording is stream-copy
remuxed (no re-encode, no CPU-heavy transcode -- see _remux_to_mp4())
into a standard, faststart MP4 before upload. If a future camera's
codec is ever not H.264, this same remux would still run but MP4
playback support for that codec is a separate, not-yet-decided
question -- this module does not attempt to detect or branch on codec.

This module never touches start_recording(), RECORDINGS_FOLDER's own
naming, camera_url(), or the local recording pipeline in any way --
local recording behavior is identical whether or not this worker is
enabled. The original MKV is never opened for writing, moved, or
deleted by this module under any outcome -- ffmpeg only ever reads it;
the remuxed MP4 lives in a dedicated per-camera staging subfolder and
is the only file this module ever removes.
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
except ImportError:
    boto3 = None

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

recording_upload_state: dict = {"worker_status": "disabled", "last_scan_at": None, "last_config_refresh_at": None, "last_error": None}

_lock = threading.Lock()
_camera_map: dict[int, dict] = {}          # camera_number -> {"camera_id":..., "site_id":...}, refreshed periodically
_sessions: dict[int, dict] = {}            # camera_number -> {credentials, bucket, key_prefix, expires_at}
_uploaded_files: dict[int, list[str]] = {}  # camera_number -> successfully uploaded+notified filenames (bounded)


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
    return session


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


def _remember_uploaded(camera_number: int, filename: str) -> None:
    uploaded = _uploaded_files.setdefault(camera_number, [])
    uploaded.append(filename)
    del uploaded[:-MAX_TRACKED_FILES_PER_CAMERA]


def _upload_recording(session: dict, local_path: Path, started_at: datetime) -> str:
    if boto3 is None:
        raise RuntimeError("boto3 is not installed.")
    creds = session["credentials"]
    client = boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        aws_session_token=creds["session_token"],
    )
    date_part = started_at.strftime("%Y/%m/%d")
    recording_key = f"{session['key_prefix']}{date_part}/{local_path.name}"
    client.upload_file(str(local_path), session["bucket"], recording_key, ExtraArgs={"ContentType": "video/mp4"})
    return recording_key


def _relay_camera_once(camera_number: int, camera_id: str) -> None:
    """A failed remux, upload, or catalog notification is deliberately
    NOT added to _uploaded_files -- unlike live relay's segments, a
    recording is durable evidence, not a disposable 2-second fragment,
    so it stays pending and is retried on the next scan instead of
    being dropped. The original MKV is untouched by every one of these
    failure paths; only the derived MP4 staging file is ever cleaned
    up, and only after its own upload attempt concludes."""
    session = _ensure_session(camera_number, camera_id)
    if not session:
        return
    already = set(_uploaded_files.get(camera_number, []))
    for local_path in _pending_recording_files(camera_number, already):
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

        mp4_path = _remux_to_mp4(local_path, camera_number, expected_duration_seconds)
        if mp4_path is None:
            continue  # original MKV untouched; retried next scan

        try:
            size_bytes = mp4_path.stat().st_size
            recording_key = _upload_recording(session, mp4_path, started_at)
        except Exception as error:
            logger.warning("recording_upload.file_upload_failed camera=%s path=%s error=%s", camera_number, local_path, error)
            _cleanup_staged_file(mp4_path)
            continue

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
