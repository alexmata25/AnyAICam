"""Real short-clip + thumbnail cloud delivery for a single qualifying
motion/AI detection event (store_motion_event()/save_yolo_events()'s own
per-event build-then-upload background task) -- distinct from, and
unrelated to, recording_uploader.py's own periodic bulk recording sync
and analytics_sync.py's own periodic bulk analytics-event sync.

This module's own cloud calls (an STS-credentialed S3 PutObject for the
clip and thumbnail, then two control-plane POSTs to register the
detection event and its media in EC2) are gated by their own explicit
flag, ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED (EVENT_MEDIA_UPLOAD_ENABLED
below) -- deliberately NOT by ANYAICAM_RECORDING_UPLOAD_ENABLED or
ANYAICAM_ANALYTICS_SYNC_ENABLED, whose own meaning is unchanged by this
module and stays scoped to gating recording_uploader.py's and
analytics_sync.py's own periodic background workers only, exactly as
before. Local event creation, local clip creation (build_motion_event_
clip()), and local thumbnail creation all happen upstream of this
module's own entry point and are never affected by this flag either
way -- only the outbound network calls below are."""

import logging
import os
import subprocess
import time
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import recording_uploader as recording_upload
from event_clips import compute_clip_window
import event_media_outbox

logger = logging.getLogger("anyaicam.event_media_uploader")

APP_ROOT = Path("/app")
RECORDINGS_ROOT = APP_ROOT / "recordings"

# Default false: matches the same safe-by-default convention every other
# appliance -> cloud call in this codebase already uses (recording_
# uploader.RECORDING_UPLOAD_ENABLED, analytics_sync.ANALYTICS_SYNC_
# ENABLED, and analytics_sync.py's own ANALYTICS_SYNC_NOTIFY_ENABLED --
# all default "false"). Before this flag existed, this module's calls
# ran unconditionally; a fresh or already-deployed appliance that never
# sets ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED now gets the same off-by-
# default posture as every sibling cloud-call flag, not a silent
# continuation of the previous always-on behavior.
EVENT_MEDIA_UPLOAD_ENABLED = os.environ.get("ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED", "false").strip().lower() == "true"
RETRY_SECONDS = max(30, int(os.environ.get("ANYAICAM_EVENT_MEDIA_RETRY_SECONDS", "120")))
RETRY_MAX_SECONDS = max(RETRY_SECONDS, int(os.environ.get("ANYAICAM_EVENT_MEDIA_RETRY_MAX_SECONDS", "3600")))
RETRY_MAX_JOBS = max(1, int(os.environ.get("ANYAICAM_EVENT_MEDIA_RETRY_MAX_JOBS", "10")))
event_media_retry_state = {"worker_status": "not_started", "last_summary": None, "last_error": None}


def _local_path_from_recording_url(value: str | None) -> Path | None:
    if not value:
        return None

    value = str(value).split("#", 1)[0].split("?", 1)[0]

    if not value.startswith("/recordings/"):
        return None

    # URLs are not file capabilities.  Resolve and contain the resulting
    # filesystem path before opening it so a URL such as
    # /recordings/../../etc/shadow can never be turned into an upload.
    try:
        root = RECORDINGS_ROOT.resolve(strict=True)
        path = (APP_ROOT / value.lstrip("/")).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _duration_seconds(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _ensure_detection_event_synced(
    event_id: str,
    camera_id: str,
) -> bool:
    """Ensure this exact local analytics event exists in EC2 before media registration."""
    import analytics_sync

    events = analytics_sync._load_local_events()
    event = next(
        (
            item
            for item in events
            if str(item.get("id") or "").strip() == event_id
        ),
        None,
    )

    if event is None:
        logger.warning(
            "event_media.analytics_event_missing event_id=%s",
            event_id,
        )
        return False

    payload = analytics_sync._build_payload(event)

    for attempt in range(1, 13):
        response = analytics_sync._control_plane_post(
            f"/api/appliance/analytics/{camera_id}/events",
            payload,
        )

        if (
            isinstance(response, dict)
            and response.get("status") in {"accepted", "duplicate"}
        ):
            analytics_sync._persist_synced_id(event_id)

            logger.info(
                "event_media.analytics_synced "
                "event_id=%s camera_id=%s status=%s",
                event_id,
                camera_id,
                response.get("status"),
            )
            return True

        if attempt < 12:
            time.sleep(5)

    logger.warning(
        "event_media.analytics_sync_failed "
        "event_id=%s camera_id=%s",
        event_id,
        camera_id,
    )
    return False


def upload_motion_event_media(
    *,
    event_id: str,
    camera_number: int,
    event_start: datetime,
    event_end: datetime,
    clip_url: str,
    thumbnail_url: str | None,
) -> bool:
    # Checked first, before any local file/network work below: local
    # event/clip/thumbnail creation (all upstream of this function) is
    # never affected either way -- only the STS credential request, the
    # S3 clip/thumbnail upload, and the two control-plane POSTs
    # (_ensure_detection_event_synced()'s event registration and this
    # function's own /events/{id}/media registration) are skipped.
    if not EVENT_MEDIA_UPLOAD_ENABLED:
        logger.info(
            "event_media.upload_disabled event_id=%s camera=%s",
            event_id,
            camera_number,
        )
        return False

    clip_path = _local_path_from_recording_url(clip_url)

    if clip_path is None:
        logger.warning(
            "event_media.clip_missing event_id=%s camera=%s clip=%s",
            event_id,
            camera_number,
            clip_url,
        )
        return False

    thumbnail_path = _local_path_from_recording_url(thumbnail_url)

    recording_upload._refresh_camera_map()
    identity = recording_upload._camera_identity(camera_number)

    if not identity:
        logger.warning(
            "event_media.camera_unknown event_id=%s camera=%s",
            event_id,
            camera_number,
        )
        return False

    # Motion event clips are a separate entitlement from continuous cloud
    # recording.  Do not infer event-media permission from a camera that is
    # merely licensed for continuous archive upload.
    if identity.get("cloud_recording_mode") != "motion":
        logger.info(
            "event_media.camera_ineligible event_id=%s camera=%s cloud_recording_mode=%s",
            event_id,
            camera_number,
            identity.get("cloud_recording_mode"),
        )
        return False

    camera_id = identity["camera_id"]
    # Persist the original local references before the first cloud attempt.
    # Repeating the deterministic S3 puts is safe, and lets a later worker
    # resume after a crash or a failed upload/registration attempt.
    event_media_outbox.put({"event_id": event_id, "camera_number": camera_number,
                            "event_start": event_start.isoformat(), "event_end": event_end.isoformat(),
                            "clip_url": clip_url, "thumbnail_url": thumbnail_url})
    session = recording_upload._ensure_session(camera_number, camera_id)

    if not session:
        logger.warning(
            "event_media.session_unavailable event_id=%s camera=%s",
            event_id,
            camera_number,
        )
        return False

    if recording_upload.boto3 is None:
        logger.warning(
            "event_media.boto3_missing event_id=%s",
            event_id,
        )
        return False

    creds = session["credentials"]

    client = recording_upload.boto3.client(
        "s3",
        region_name=recording_upload.AWS_REGION,
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        aws_session_token=creds["session_token"],
    )

    date_part = event_start.strftime("%Y/%m/%d")
    base_key = (
        f"{session['key_prefix']}"
        f"{date_part}/events/motion_{event_id}"
    )

    clip_key = base_key + ".mp4"

    client.upload_file(
        str(clip_path),
        session["bucket"],
        clip_key,
        ExtraArgs={"ContentType": "video/mp4"},
    )

    thumbnail_key = None

    if thumbnail_path is not None:
        thumbnail_key = base_key + ".jpg"

        try:
            client.upload_file(
                str(thumbnail_path),
                session["bucket"],
                thumbnail_key,
                ExtraArgs={"ContentType": "image/jpeg"},
            )
        except Exception as error:
            logger.warning(
                "event_media.thumbnail_upload_failed "
                "event_id=%s camera=%s error=%s",
                event_id,
                camera_number,
                error,
            )
            thumbnail_key = None

    window = compute_clip_window(event_start, event_end)

    actual_duration = _duration_seconds(clip_path)
    duration_seconds = (
        actual_duration
        if actual_duration is not None
        else (window.end - window.start).total_seconds()
    )

    size_bytes = clip_path.stat().st_size

    payload = {
        "s3_key": clip_key,
        "thumbnail_s3_key": thumbnail_key,
        "started_at": window.start.isoformat(),
        "ended_at": window.end.isoformat(),
        "duration_seconds": duration_seconds,
        "size_bytes": size_bytes,
    }

    # Media registration depends on detection_events existing first.
    # Sync this exact event into EC2 before attempting media registration.
    if not _ensure_detection_event_synced(event_id, camera_id):
        logger.warning(
            "event_media.registration_deferred "
            "event_id=%s camera=%s",
            event_id,
            camera_number,
        )
        return False

    # detection_events now exists, so media registration can proceed.
    for attempt in range(1, 13):
        response = recording_upload._control_plane_post(
            f"/api/appliance/analytics/"
            f"{camera_id}/events/{event_id}/media",
            payload,
        )

        if (
            isinstance(response, dict)
            and response.get("status") in {"accepted", "duplicate"}
        ):
            logger.info(
                "event_media.registered "
                "event_id=%s camera=%s clip_key=%s thumbnail_key=%s",
                event_id,
                camera_number,
                clip_key,
                thumbnail_key,
            )
            event_media_outbox.remove(event_id)
            return True

        if attempt < 12:
            time.sleep(5)

    logger.warning(
        "event_media.registration_failed "
        "event_id=%s camera=%s clip_key=%s",
        event_id,
        camera_number,
        clip_key,
    )

    return False


def retry_pending_event_media(max_jobs: int = RETRY_MAX_JOBS) -> dict:
    """Retry durable jobs on the next local worker tick; never contacts a
    service unless the normal event-media feature gate is enabled."""
    attempted = completed = 0
    for job in event_media_outbox.due()[:max(1, max_jobs)]:
        attempted += 1
        try:
            succeeded = upload_motion_event_media(
                event_id=str(job["event_id"]), camera_number=int(job["camera_number"]),
                event_start=datetime.fromisoformat(str(job["event_start"])),
                event_end=datetime.fromisoformat(str(job["event_end"])),
                clip_url=str(job["clip_url"]), thumbnail_url=job.get("thumbnail_url"),
            )
            if succeeded:
                completed += 1
            else:
                attempts = int(job.get("attempts", 0)) + 1
                delay = min(RETRY_MAX_SECONDS, RETRY_SECONDS * (2 ** min(attempts, 8)))
                event_media_outbox.replace(str(job["event_id"]), {**job, "attempts": attempts, "last_error": "retry_failed", "next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()})
        except (KeyError, TypeError, ValueError):
            # Keep malformed records inspectable, but defer them so one bad
            # item cannot cause a tight loop or block later jobs.
            attempts = int(job.get("attempts", 0)) + 1
            delay = min(RETRY_MAX_SECONDS, RETRY_SECONDS * (2 ** min(attempts, 8)))
            event_media_outbox.replace(str(job.get("event_id")), {**job, "attempts": attempts, "last_error": "invalid_job", "next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()})
    return {"attempted": attempted, "completed": completed, "pending": len(event_media_outbox.load())}


async def event_media_retry_worker() -> None:
    if not EVENT_MEDIA_UPLOAD_ENABLED:
        event_media_retry_state["worker_status"] = "disabled"
        while True: await asyncio.sleep(3600)
    event_media_retry_state["worker_status"] = "running"
    while True:
        try:
            event_media_retry_state["last_summary"] = await asyncio.to_thread(retry_pending_event_media)
            event_media_retry_state["last_error"] = None
        except asyncio.CancelledError: raise
        except Exception as error:
            event_media_retry_state["last_error"] = type(error).__name__
            logger.warning("event_media.retry_tick_failed error=%s", type(error).__name__)
        await asyncio.sleep(RETRY_SECONDS)
