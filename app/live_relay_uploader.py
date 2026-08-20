"""Phase 3 (docs/AI_HANDOFF.md §8): appliance-side live-segment watcher/uploader.

Watches the local HLS playlist that start_live_stream() already produces --
unchanged, not touched by this module -- and, only for cameras with an
active relay flag, requests/renews a short-lived S3 upload credential from
the Phase 2 control-plane endpoint (POST /api/appliance/live/{camera_id}/session)
and PutObjects each newly-listed .ts segment directly to S3, then notifies
the Phase 2 segment-available endpoint
(POST /api/appliance/live/{camera_id}/segment-available) with tiny JSON
metadata -- never a media byte. FastAPI is never in the media data path;
see docs/AI_HANDOFF.md §8 "Control plane vs. media data plane".

Segment discovery deliberately reads the camera's own local .m3u8 playlist
rather than globbing filenames in HLS_FOLDER: start_live_stream() does not
pass -hls_segment_filename, so FFmpeg's default segment naming has no
guaranteed separator between a camera number and its segment index (e.g.
camera1's segments could collide with camera10's under a naive glob). The
playlist already lists the exact, unambiguous segment filenames for that
one camera -- it's the same file hls.js already reads for local playback --
so this reuses it read-only instead of inventing a second, riskier
discovery mechanism.

Appliance identity (appliance_id + long-lived bearer credential) is read
from the same credential.json file format the appliance-agent package
already writes on activation (anyaicam_agent.config.save_credential),
rooted at ANYAICAM_STATE_DIR (default /var/lib/anyaicam, matching that
package's default). This module does not import appliance-agent -- it only
reads the same file convention, so a single activation (anyaicam-setup) is
enough to authorize both the control-plane agent and this uploader. This
assumes ANYAICAM_STATE_DIR is reachable from wherever this process runs;
confirm that mount exists in whatever deployment enables this feature.

Per-camera relay-active state defaults OFF and is driven only by
set_relay_active() -- Phase 4 (start_live_relay/stop_live_relay command
wiring, not part of this phase) is the only intended caller in production.
Nothing in this module starts uploading on its own.

This module never touches start_live_stream(), camera_url(), or the local
recording pipeline -- local live view and recording behavior is identical
whether or not this worker is enabled.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import secrets
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

logger = logging.getLogger("anyaicam.live_relay_uploader")

_relay_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="live_relay",
)

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
LIVE_RELAY_ENABLED = os.environ.get("ANYAICAM_LIVE_RELAY_ENABLED", "false").strip().lower() == "true"
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
STATE_DIR = Path(os.environ.get("ANYAICAM_STATE_DIR", "/var/lib/anyaicam"))
CREDENTIAL_FILE = STATE_DIR / "credential.json"
RELAY_COMMANDS_FILE = STATE_DIR / "live_relay_commands.json"  # Phase 4: written by appliance-agent commands.py
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip()
SCAN_SECONDS = max(0.5, float(os.environ.get("ANYAICAM_LIVE_RELAY_SCAN_SECONDS", "1.0")))
SESSION_RENEW_MARGIN_SECONDS = max(30, int(os.environ.get("ANYAICAM_LIVE_RELAY_SESSION_RENEW_MARGIN_SECONDS", "120")))
MAX_TRACKED_SEGMENTS_PER_CAMERA = 50  # bounds our own memory; local .ts files already rotate fast under hls_flags=delete_segments

live_relay_state: dict = {"worker_status": "disabled", "last_scan_at": None, "last_error": None}

_lock = threading.Lock()
_active_cameras: dict[int, str] = {}           # camera_number -> camera_id, present only while relay is active
_sessions: dict[int, dict] = {}                # camera_number -> {credentials, bucket, key_prefix, expires_at}
_uploaded_segments: dict[int, list[str]] = {}  # camera_number -> recently uploaded segment filenames (bounded, ordered)
_next_sequence: dict[int, int] = {}            # camera_number -> next sequence number to report
_last_applied_relay_state: dict[int, tuple[str, bool]] = {}  # Phase 4: camera_number -> (camera_id, active) last actually applied via set_relay_active()


def set_relay_active(camera_number: int, camera_id: str, active: bool = True) -> None:
    """Phase 4's start_live_relay/stop_live_relay command handlers are the
    intended caller. Defaults to inactive for every camera -- nothing here
    starts uploading on its own."""
    with _lock:
        if active:
            _active_cameras[camera_number] = camera_id
        else:
            _active_cameras.pop(camera_number, None)
            _sessions.pop(camera_number, None)


def is_relay_active(camera_number: int) -> bool:
    with _lock:
        return camera_number in _active_cameras


def active_camera_numbers() -> list[int]:
    with _lock:
        return sorted(_active_cameras)


def _validate_camera_number(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 256 else None


def _validate_camera_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed if trimmed and len(trimmed) <= 128 else None


def _parse_relay_commands_file() -> dict[int, tuple[str, bool]]:
    try:
        raw = json.loads(RELAY_COMMANDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    desired: dict[int, tuple[str, bool]] = {}
    for camera_number_key, entry in raw.items():
        camera_number = _validate_camera_number(camera_number_key)
        if camera_number is None or not isinstance(entry, dict):
            logger.warning("live_relay.relay_command_entry_invalid key=%r", camera_number_key)
            continue
        camera_id = _validate_camera_id(entry.get("camera_id"))
        active = entry.get("active")
        if camera_id is None or not isinstance(active, bool):
            logger.warning("live_relay.relay_command_entry_invalid camera_number=%s", camera_number_key)
            continue
        desired[camera_number] = (camera_id, active)
    return desired


def _reconcile_relay_commands() -> None:
    desired = _parse_relay_commands_file()

    for camera_number in list(_last_applied_relay_state):
        if camera_number in desired:
            continue
        previous_camera_id, previous_active = _last_applied_relay_state.pop(camera_number)
        if previous_active:
            set_relay_active(camera_number, previous_camera_id, False)

    for camera_number, (camera_id, active) in desired.items():
        previous = _last_applied_relay_state.get(camera_number)
        if previous == (camera_id, active):
            continue
        if previous is not None and active and previous[0] != camera_id and previous[1]:
            set_relay_active(camera_number, previous[0], False)
        if previous is not None or active:
            set_relay_active(camera_number, camera_id, active)
        _last_applied_relay_state[camera_number] = (camera_id, active)


def _load_appliance_identity() -> tuple[str, str] | None:
    try:
        data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        # Malformed/corrupted credential.json (e.g. a JSON list or scalar) -- treat
        # as "not activated" rather than raising, since .get() below would only be
        # safe for a dict.
        return None
    appliance_id = str(data.get("appliance_id") or "").strip()
    credential = str(data.get("credential") or "").strip()
    if not appliance_id or not credential:
        return None
    return appliance_id, credential


def _control_plane_request(path: str, payload: dict) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AnyAiCam-LiveRelay/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }
    request = urllib.request.Request(
        CLOUD_URL + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("live_relay.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("live_relay.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _session_expires_soon(session: dict) -> bool:
    expires_at = session.get("expires_at") if isinstance(session, dict) else None
    if not isinstance(expires_at, str) or not expires_at.strip():
        # Missing, non-string, or blank -- cannot safely evaluate. Ambiguous
        # always means "expired": force a renewal rather than risk reusing a
        # credential we can't actually confirm is still valid.
        return True
    try:
        expiration = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return True
    if expiration.tzinfo is None or expiration.tzinfo.utcoffset(expiration) is None:
        # Timezone-naive value -- comparing it against an aware "now" would
        # either raise or silently assume the wrong offset. Treat as ambiguous.
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
    response = _control_plane_request(f"/api/appliance/live/{camera_id}/session", {})

    def _invalid(reason: str) -> None:
        # Log only camera number/id and a fixed reason code -- never the response
        # body, which may contain live AWS credentials.
        logger.warning(
            "live_relay.session_response_invalid camera_number=%s camera_id=%s reason=%s",
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


def _segment_name_pattern(camera_number: int) -> re.Pattern:
    # Requires the literal "camera{N}" prefix for *this* camera followed only by
    # digits/./_/- before a mandatory ".ts" suffix -- e.g. "camera1_000123.ts".
    # Anything else (a different extension, an embedded path, an unexpected
    # character) is rejected rather than uploaded. This is a second, independent
    # check on top of Path(...).name already stripping any directory component
    # below, not a replacement for it.
    return re.compile(rf"^camera{camera_number}[0-9_.\-]*\.ts$")


def _playlist_segment_names(hls_folder: Path, camera_number: int) -> list[str]:
    """Reads the exact segment filenames out of the camera's own local HLS
    playlist -- see module docstring for why this is used instead of a
    filename glob. Every entry is validated before being returned: only a
    bare filename (no directory component -- Path(...).name discards
    anything before the final separator, which also defeats "../" traversal)
    matching this camera's own "camera{N}...ts" naming is accepted; anything
    else is logged and dropped, never opened or uploaded."""
    manifest = hls_folder / f"camera{camera_number}.m3u8"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    pattern = _segment_name_pattern(camera_number)
    names = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = Path(stripped).name
        if pattern.fullmatch(name):
            names.append(name)
        else:
            logger.warning("live_relay.playlist_entry_rejected camera=%s entry=%r", camera_number, stripped)
    return names


def _pending_segments(hls_folder: Path, camera_number: int, already_uploaded: set[str]) -> list[Path]:
    hls_folder_resolved = hls_folder.resolve()
    pending = []
    for name in _playlist_segment_names(hls_folder, camera_number):
        if name in already_uploaded:
            continue
        local_path = hls_folder / name
        # Belt-and-suspenders: even though the regex above already forbids any
        # separator in `name`, confirm the resolved path still lands directly
        # inside hls_folder before it's ever handed to open()/upload_file().
        try:
            resolved = local_path.resolve()
        except OSError:
            continue
        if resolved.parent != hls_folder_resolved:
            logger.warning("live_relay.segment_path_outside_hls_folder camera=%s path=%s", camera_number, resolved)
            continue
        pending.append(local_path)
    return pending


def _remember_uploaded(camera_number: int, segment_name: str) -> None:
    uploaded = _uploaded_segments.setdefault(camera_number, [])
    uploaded.append(segment_name)
    del uploaded[:-MAX_TRACKED_SEGMENTS_PER_CAMERA]


def _upload_segment(session: dict, local_path: Path) -> str:
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
    segment_key = session["key_prefix"] + local_path.name
    client.upload_file(str(local_path), session["bucket"], segment_key, ExtraArgs={"ContentType": "video/mp2t"})
    return segment_key


def _relay_camera_once(hls_folder: Path, camera_number: int, camera_id: str) -> None:
    session = _ensure_session(camera_number, camera_id)
    if not session:
        return
    already = set(_uploaded_segments.get(camera_number, []))
    for local_path in _pending_segments(hls_folder, camera_number, already):
        if not local_path.exists():
            # Already rotated out by hls_flags=delete_segments before we got to it --
            # per docs/AI_HANDOFF.md §8 "Reconnect/failure behavior", a stale live
            # segment is dropped, not backfilled; the next one supersedes it.
            _remember_uploaded(camera_number, local_path.name)
            continue
        try:
            segment_key = _upload_segment(session, local_path)
        except Exception as error:
            logger.warning(
                "live_relay.segment_upload_failed camera=%s path=%s error=%s", camera_number, local_path, error
            )
            _remember_uploaded(camera_number, local_path.name)
            continue
        _remember_uploaded(camera_number, local_path.name)
        sequence = _next_sequence.get(camera_number, 0)
        _next_sequence[camera_number] = sequence + 1
        _control_plane_request(
            f"/api/appliance/live/{camera_id}/segment-available",
            {"segment_key": segment_key, "sequence": sequence},
        )


async def live_relay_worker(hls_folder: Path) -> None:
    if RUNTIME_ROLE not in {"edge", "combined"} or not LIVE_RELAY_ENABLED:
        live_relay_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    live_relay_state["worker_status"] = "running" if boto3 is not None else "dependency_missing"
    logger.info("live_relay.worker_started status=%s", live_relay_state["worker_status"])
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(_relay_executor, _reconcile_relay_commands)
            for camera_number, camera_id in list(_active_cameras.items()):
                await loop.run_in_executor(_relay_executor, _relay_camera_once, hls_folder, camera_number, camera_id)
            live_relay_state["last_scan_at"] = datetime.now().isoformat()
            live_relay_state["last_error"] = None
            await asyncio.sleep(SCAN_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            live_relay_state["last_error"] = str(error)
            logger.warning("live_relay.worker_iteration_failed error=%s", error)
            await asyncio.sleep(SCAN_SECONDS)
