"""Persistent, credential-free camera inventory for the edge appliance."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


INVENTORY_VERSION = 1
SENSITIVE_KEY_PARTS = ("username", "password", "credential", "secret", "token")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _without_credentials(value):
    if isinstance(value, dict):
        return {
            key: _without_credentials(item)
            for key, item in value.items()
            if not any(part in key.lower() for part in SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_without_credentials(item) for item in value]
    return value


class CameraInventoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("cameras"), list):
                return value
        except (OSError, json.JSONDecodeError):
            pass
        return {
            "version": INVENTORY_VERSION,
            "updated_at": None,
            "last_scan": None,
            "cameras": [],
        }

    def reconcile(self, network: str, discovered: list[dict]) -> dict:
        """Upsert this scan and retain offline devices for operator review."""
        with self._lock:
            inventory = self.load()
            now = utc_now()
            existing = {
                str(camera.get("id")): camera
                for camera in inventory.get("cameras", [])
                if camera.get("id")
            }
            seen: set[str] = set()
            for item in discovered:
                safe = _without_credentials(item)
                camera_id = str(safe["id"])
                seen.add(camera_id)
                previous = existing.get(camera_id, {})
                if previous.get("provisioning_status") == "provisioned":
                    safe["discovered_name"] = safe.get("name")
                    for key in (
                        "name", "provisioning_status", "camera_number",
                        "rtsp_valid", "onvif_valid", "health_state",
                        "recording", "fps", "bitrate_bps",
                    ):
                        if key in previous:
                            safe[key] = previous[key]
                safe["first_seen"] = previous.get("first_seen") or now
                safe["last_seen"] = now
                safe["last_scan"] = now
                safe["discovery_network"] = network
                safe["status"] = "online"
                existing[camera_id] = safe

            for camera_id, camera in existing.items():
                if camera.get("discovery_network") == network and camera_id not in seen:
                    camera["status"] = "offline"
                    camera["last_scan"] = now

            cameras = sorted(
                existing.values(),
                key=lambda camera: (
                    str(camera.get("ip_address", "")),
                    str(camera.get("id", "")),
                ),
            )
            inventory = {
                "version": INVENTORY_VERSION,
                "updated_at": now,
                "last_scan": {
                    "network": network,
                    "completed_at": now,
                    "discovered": len(discovered),
                },
                "cameras": cameras,
            }
            self._save(inventory)
            return inventory

    def update(self, camera_id: str, values: dict) -> dict:
        """Update only public inventory fields; credentials are always discarded."""
        allowed = {
            "name", "status", "provisioning_status", "camera_number",
            "rtsp_valid", "onvif_valid", "health_state", "recording",
            "fps", "bitrate_bps",
        }
        with self._lock:
            inventory = self.load()
            camera = next(
                (item for item in inventory["cameras"] if item.get("id") == camera_id),
                None,
            )
            if camera is None:
                raise KeyError(camera_id)
            camera.update(
                _without_credentials({key: value for key, value in values.items() if key in allowed})
            )
            camera["updated_at"] = utc_now()
            inventory["updated_at"] = camera["updated_at"]
            self._save(inventory)
            return dict(camera)

    def _save(self, inventory: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(_without_credentials(inventory), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)
