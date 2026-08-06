"""Persistent health monitoring for provisioned Edge cameras."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from edge.camera_provisioning import CameraConfigurationStore, CameraProbeService
from edge_streaming import playlist_snapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CameraHealthStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("cameras"), list):
                return value
        except (OSError, json.JSONDecodeError):
            pass
        return {"updated_at": None, "cameras": []}

    def save(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)


class CameraHealthMonitor:
    def __init__(
        self,
        configurations: CameraConfigurationStore,
        store: CameraHealthStore,
        probe: CameraProbeService,
        hls_folder: str | Path,
        recordings_folder: str | Path,
        freshness_seconds: int,
        process_state: Callable[[int], dict] | None = None,
    ):
        self.configurations = configurations
        self.store = store
        self.probe = probe
        self.hls_folder = Path(hls_folder)
        self.recordings_folder = Path(recordings_folder)
        self.freshness_seconds = freshness_seconds
        self.process_state = process_state or (lambda _number: {})
        self._lock = threading.Lock()

    def refresh(self) -> dict:
        with self._lock:
            cameras = [self.inspect(camera) for camera in self.configurations.load()["cameras"]]
            result = {"updated_at": utc_now(), "cameras": cameras}
            self.store.save(result)
            return result

    def inspect(self, camera: dict) -> dict:
        number = int(camera.get("camera_number") or 0)
        rtsp = self.probe.validate_rtsp(camera["rtsp_url"], camera.get("username", ""), camera.get("password", ""))
        hls = playlist_snapshot(self.hls_folder, number, self.freshness_seconds) if number > 0 else {"exists": False, "fresh": False}
        workers = self.process_state(number) if number > 0 else {}
        latest_recording = self._latest_recording(number)
        recording_running = str(workers.get("recording") or "").lower() == "running"
        if not rtsp.get("valid"):
            state = "offline"
        elif hls.get("fresh") and recording_running:
            state = "healthy"
        elif hls.get("exists") and not hls.get("fresh"):
            state = "stale"
        else:
            state = "degraded"
        return {
            "id": camera["id"],
            "camera_number": number,
            "name": camera.get("name") or "Camera",
            "state": state,
            "rtsp": "online" if rtsp.get("valid") else "offline",
            "hls": "fresh" if hls.get("fresh") else "stale" if hls.get("exists") else "missing",
            "fps": float(rtsp.get("fps") or 0),
            "bitrate_bps": int(rtsp.get("bitrate_bps") or 0),
            "codec": rtsp.get("codec"),
            "recording": "running" if recording_running else "stopped",
            "latest_recording": latest_recording,
            "manifest_age_seconds": hls.get("manifest_age_seconds"),
            "segment_age_seconds": hls.get("segment_age_seconds"),
            "checked_at": utc_now(),
            "error": rtsp.get("error") if not rtsp.get("valid") else workers.get("recording_error"),
        }

    def _latest_recording(self, camera_number: int) -> dict | None:
        folder = self.recordings_folder / f"camera{camera_number}"
        try:
            files = [item for item in folder.glob("*.mkv") if item.is_file()]
            latest = max(files, key=lambda item: item.stat().st_mtime, default=None)
            if not latest:
                return None
            modified = latest.stat().st_mtime
            return {
                "file": latest.name,
                "modified_at": datetime.fromtimestamp(modified, timezone.utc).isoformat(),
                "size_bytes": latest.stat().st_size,
            }
        except OSError:
            return None
