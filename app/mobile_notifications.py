
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

RECORDINGS_FOLDER = Path("/app/recordings")
ENROLLMENTS_FILE = RECORDINGS_FOLDER / "mobile_push_enrollments.json"


class PushEnrollment(BaseModel):
    device_name: str = "Mobile device"
    platform: str = "web"
    endpoint: str
    keys: dict = Field(default_factory=dict)


def _load() -> list[dict]:
    try:
        if ENROLLMENTS_FILE.exists():
            data = json.loads(ENROLLMENTS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save(items: list[dict]) -> None:
    ENROLLMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ENROLLMENTS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(items[-2000:], indent=2), encoding="utf-8")
    temporary.replace(ENROLLMENTS_FILE)


def register_mobile_notification_routes(app, *, current_user: Callable, record_audit: Callable):
    @app.post("/api/mobile/push/enroll")
    def enroll_mobile_push(payload: PushEnrollment, request: Request):
        user = current_user(request)
        if user.get("id") in {None, "", "anonymous"}:
            raise HTTPException(status_code=401, detail="Sign in before enabling push.")
        enrollments = _load()
        existing = next(
            (
                item for item in enrollments
                if item.get("user_id") == user.get("id")
                and item.get("endpoint") == payload.endpoint
                and item.get("device_name") == payload.device_name
            ),
            None,
        )
        now = datetime.now().isoformat()
        if existing:
            existing.update({"platform": payload.platform, "keys": payload.keys, "enabled": True, "updated_at": now})
            enrollment = existing
        else:
            enrollment = {
                "id": uuid.uuid4().hex,
                "user_id": user.get("id"),
                "device_name": payload.device_name,
                "platform": payload.platform,
                "endpoint": payload.endpoint,
                "keys": payload.keys,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            }
            enrollments.append(enrollment)
        _save(enrollments)
        record_audit(request, "create", f"mobile-device:{enrollment['id']}", "Mobile push device enrolled.")
        return {"status": "complete", "enrollment": enrollment, "message": "This device is enrolled for ANY AI CAM push alerts."}

    @app.get("/api/mobile/push/enrollments")
    def mobile_push_enrollments(request: Request):
        user = current_user(request)
        if user.get("id") in {None, "", "anonymous"}:
            raise HTTPException(status_code=401, detail="Sign in required.")
        return {"enrollments": [item for item in _load() if item.get("user_id") == user.get("id")]}
