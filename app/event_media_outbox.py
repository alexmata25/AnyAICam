"""Small atomic local outbox for idempotent event-media retry."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

OUTBOX_FILE = Path(os.environ.get("ANYAICAM_EVENT_MEDIA_OUTBOX_FILE", "/var/lib/anyaicam/event_media_outbox.json"))


def load() -> list[dict]:
    try:
        data = json.loads(OUTBOX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save(jobs: list[dict]) -> None:
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTBOX_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(jobs, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, OUTBOX_FILE)


def put(job: dict) -> None:
    jobs = [item for item in load() if item.get("event_id") != job.get("event_id")]
    job = {**job, "attempts": int(job.get("attempts", 0)), "next_attempt_at": job.get("next_attempt_at")}
    jobs.append(job)
    save(jobs)


def remove(event_id: str) -> None:
    save([item for item in load() if item.get("event_id") != event_id])


def replace(event_id: str, replacement: dict) -> None:
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
