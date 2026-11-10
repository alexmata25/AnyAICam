"""Small atomic local outbox for idempotent event-media retry."""
import json
import os
import platform_paths
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

# /var/lib/anyaicam itself is mounted read-only into the anyaicam-vms
# container by design (only the host-side anyaicam-agent.service, which
# owns identity/credentials, is meant to write there) -- confirmed live
# 2026-09-13 when this file's own default here caused every media
# upload to fail with "OSError: [Errno 30] Read-only file system".
# /opt/anyaicam/data/config is the correct home instead: a separate,
# already-mounted-read-write, already-persistent-across-repair-installs
# directory the installer itself documents as being for exactly this
# kind of protected config/state data (05-provision-users-dirs.sh),
# and -- unlike /app/recordings, the other writable option -- not
# served to anyone via main.py's unauthenticated `/recordings` static
# mount. Does not touch STATE_DIR itself: live_relay_uploader.py and
# recording_uploader.py both still correctly read credential.json/
# live_relay_commands.json from the real /var/lib/anyaicam, and must
# keep doing so unchanged.
OUTBOX_FILE = Path(os.environ.get("ANYAICAM_EVENT_MEDIA_OUTBOX_FILE", platform_paths.native_path("/opt/anyaicam/data/config/event_media_outbox.json", "config", "event_media_outbox.json")))
_LOCK = threading.RLock()


def load() -> list[dict]:
    with _LOCK:
        try:
            data = json.loads(OUTBOX_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []


def save(jobs: list[dict]) -> None:
    with _LOCK:
        OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(jobs, separators=(",", ":"))
        descriptor, name = tempfile.mkstemp(prefix=OUTBOX_FILE.name + '.', suffix='.tmp', dir=OUTBOX_FILE.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, OUTBOX_FILE)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def put(job: dict) -> None:
    # Clip encoders and retry workers call from different threads. Atomic
    # rename alone does not protect a read/modify/write from lost updates.
    with _LOCK:
        jobs = [item for item in load() if item.get("event_id") != job.get("event_id")]
        job = {**job, "attempts": int(job.get("attempts", 0)), "next_attempt_at": job.get("next_attempt_at")}
        jobs.append(job)
        save(jobs)


def remove(event_id: str) -> None:
    with _LOCK:
        save([item for item in load() if item.get("event_id") != event_id])


def replace(event_id: str, replacement: dict) -> None:
    with _LOCK:
        save([replacement if item.get("event_id") == event_id else item for item in load()])


def due(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    result = []
    for job in load():
        try:
            next_at = job.get("next_attempt_at")
            if not next_at or datetime.fromisoformat(str(next_at)).astimezone(timezone.utc) <= now:
                result.append(job)
        except (TypeError, ValueError):
            result.append(job)
    return result
