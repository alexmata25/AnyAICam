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

Backlog cutoff (first-activation safety): a camera that has been
recording locally for days before upload is ever enabled must not
have that entire history auto-uploaded the moment the flag flips.
_load_or_establish_cutoff() persists a single, appliance-wide moment
-- the first time this worker ever runs -- to CUTOFF_FILE in
STATE_DIR (the same host-mounted, restart-durable directory
credential.json already lives in). Only completed recordings whose
own started_at is at or after that moment are ever eligible for
upload; anything older is left on local disk untouched, forever --
see _pending_recording_files(). A missing cutoff file means "never
activated before" and is created fresh at the current moment, so the
very first scan after that already enforces it -- there is no window
where a missing file could be read as "no cutoff." A restart reads
the existing file back unchanged; the cutoff never moves forward on
its own, so a brief restart never re-exposes backlog that was already
excluded. A present-but-corrupt cutoff file fails safe by blocking
every file (an effectively-infinite cutoff) rather than guessing --
guessing a too-old cutoff risks repeating the exact backlog-drain this
mechanism exists to prevent, and guessing a too-new one risks silently
losing real recent recordings, so neither guess is acceptable.
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
# Backlog-cutoff persistence (see module docstring). Overridable purely for
# tests -- production always uses the default, same STATE_DIR credential.json
# already lives in.
CUTOFF_FILE = Path(os.environ.get("ANYAICAM_RECORDING_UPLOAD_CUTOFF_FILE", str(STATE_DIR / "recording_upload_cutoff.json")))
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


def _parse_max_files_per_scan() -> int | None:
    """Unset/empty (the default) means no cap -- exactly today's
    behavior, every eligible file gets processed in one scan. A set,
    valid positive integer caps how many of the already
    cutoff-filtered pending files _relay_camera_once() processes per
    camera per scan; anything left over stays on disk, still eligible,
    picked up on a later scan -- nothing is dropped, only deferred. An
    invalid value (non-numeric, zero, or negative) is a configuration
    mistake, not a request to remove the limit -- silently falling
    back to "no cap" here would be exactly backwards from what whoever
    set this clearly intended, so it fails safe to the tightest
    possible cap (1) instead, logged once so it's diagnosable."""
    raw = os.environ.get("ANYAICAM_RECORDING_UPLOAD_MAX_FILES_PER_SCAN", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("recording_upload.max_files_per_scan_invalid raw=%r failing_safe_to=1", raw)
        return 1
    if value < 1:
        logger.warning("recording_upload.max_files_per_scan_invalid raw=%r failing_safe_to=1", raw)
        return 1
    return value


MAX_FILES_PER_SCAN = _parse_max_files_per_scan()

recording_upload_state: dict = {"worker_status": "disabled", "last_scan_at": None, "last_config_refresh_at": None, "last_error": None}

_lock = threading.Lock()
_camera_map: dict[int, dict] = {}          # camera_number -> {"camera_id":..., "site_id":...}, refreshed periodically
_sessions: dict[int, dict] = {}            # camera_number -> {credentials, bucket, key_prefix, expires_at}
_uploaded_files: dict[int, list[str]] = {}  # camera_number -> successfully uploaded+notified filenames (bounded)
_unsupported_codec_files: dict[int, set[str]] = {}  # camera_number -> filenames already logged as unsupported/undetected this process lifetime -- avoids re-probing/re-logging the same permanently-bad file every scan
_transcode_semaphore = threading.Semaphore(TRANSCODE_MAX_CONCURRENCY)
_cutoff_lock = threading.Lock()
_cutoff_cache: datetime | None = None       # loaded/established once per process lifetime -- see _load_or_establish_cutoff()
_backlog_skip_logged: set[int] = set()      # camera_numbers already logged as having pre-cutoff backlog this process lifetime -- avoids re-logging the same stable count every scan


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


def _load_or_establish_cutoff() -> datetime:
    """The single, appliance-wide backlog cutoff -- see the module
    docstring. Naive local-time throughout, matching
    _recording_started_at()'s own naive local-time return value
    exactly (this file's one exception, expiration in _ensure_session(),
    is a completely separate AWS-credential concern using
    datetime.now(timezone.utc); recording timestamps never do).
    Cached after the first call each process lifetime -- the on-disk
    file is only ever read (or written, on first-ever activation)
    once per process, never polled every scan."""
    global _cutoff_cache
    with _cutoff_lock:
        if _cutoff_cache is not None:
            return _cutoff_cache
        try:
            raw = json.loads(CUTOFF_FILE.read_text())
            cutoff = datetime.fromisoformat(raw["cutoff"])
        except FileNotFoundError:
            cutoff = datetime.now()
            try:
                CUTOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
                CUTOFF_FILE.write_text(json.dumps({"cutoff": cutoff.isoformat()}))
                logger.info("recording_upload.cutoff_established cutoff=%s", cutoff.isoformat())
            except OSError as error:
                # Cutoff still enforced in-memory for the rest of this
                # process even though persistence failed -- a restart
                # before this write ever succeeds simply establishes a
                # fresh (later, never earlier) cutoff at that point, which
                # is still safe: it can only ever exclude more backlog,
                # never less.
                logger.warning("recording_upload.cutoff_write_failed cutoff=%s error=%s", cutoff.isoformat(), error)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            # Corrupt or unreadable state: guessing a cutoff either risks
            # repeating a backlog drain (guess too old) or silently losing
            # real recent recordings (guess too new). Neither is
            # acceptable, so upload is fully paused -- nothing is ever
            # "at or after" datetime.max -- until an operator repairs or
            # removes CUTOFF_FILE.
            logger.warning("recording_upload.cutoff_corrupt_failing_safe path=%s error=%s", CUTOFF_FILE, error)
            cutoff = datetime.max
        _cutoff_cache = cutoff
        return cutoff


def _pending_recording_files(camera_number: int, already_uploaded: set[str]) -> list[Path]:
    folder_resolved = _recording_folder(camera_number).resolve()
    cutoff = _load_or_establish_cutoff()
    pending = []
    skipped_backlog = 0
    for local_path in _completed_recording_files(camera_number):
        if local_path.name in already_uploaded:
            continue
        started_at = _recording_started_at(local_path, camera_number)
        if started_at is not None and started_at < cutoff:
            # Pre-cutoff backlog: never auto-uploaded, never touched,
            # never retried -- see the module docstring. A file with an
            # unparseable name (started_at is None) is NOT treated as
            # backlog here; it falls through to _relay_camera_once()'s
            # existing unparseable-filename handling unchanged.
            skipped_backlog += 1
            continue
        try:
            resolved = local_path.resolve()
        except OSError:
            continue
        if resolved.parent != folder_resolved:
            logger.warning("recording_upload.file_path_outside_recordings_folder camera=%s path=%s", camera_number, resolved)
            continue
        pending.append(local_path)
    if skipped_backlog and camera_number not in _backlog_skip_logged:
        logger.info("recording_upload.pre_cutoff_backlog_skipped camera=%s count=%s cutoff=%s", camera_number, skipped_backlog, cutoff.isoformat())
        _backlog_skip_logged.add(camera_number)
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
    ever cleaned up, and only after its own upload attempt concludes."""
    session = _ensure_session(camera_number, camera_id)
    if not session:
        return
    already = set(_uploaded_files.get(camera_number, []))
    pending = _pending_recording_files(camera_number, already)
    if MAX_FILES_PER_SCAN is not None:
        pending = pending[:MAX_FILES_PER_SCAN]
    for local_path in pending:
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

        mp4_path = _prepare_cloud_copy(local_path, camera_number, expected_duration_seconds)
        if mp4_path is None:
            continue  # unsupported codec, or remux/transcode failed -- original MKV untouched either way

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
    scan_number = 0
    while True:
        try:
            scan_number += 1
            # Diagnostic-only heartbeat: this codebase stays silent on an
            # uneventful successful scan by convention, which made a
            # genuinely stuck loop indistinguishable from a healthy-but-
            # boring one in the logs. This single unconditional line at
            # the top of every real iteration removes that ambiguity --
            # it changes nothing about timing, credentials, S3 behavior,
            # the cutoff, or the per-scan upload cap.
            logger.info("recording_upload.scan_tick_begin scan_number=%s at=%s", scan_number, datetime.now().isoformat())
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
