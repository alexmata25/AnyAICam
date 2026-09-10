"""Small atomic local outbox for idempotent event-media retry."""
import json
import os
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
    jobs.append(job)
    save(jobs)


def remove(event_id: str) -> None:
    save([item for item in load() if item.get("event_id") != event_id])
