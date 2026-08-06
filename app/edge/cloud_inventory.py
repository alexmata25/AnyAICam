"""Report safe edge camera inventory through the existing appliance API."""

from __future__ import annotations

import json
import os
import secrets
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen


class CloudInventoryReporter:
    def __init__(
        self,
        base_url: str,
        appliance_id: str,
        credential: str,
        opener=urlopen,
    ):
        self.base_url = base_url.rstrip("/")
        self.appliance_id = appliance_id
        self.credential = credential
        self.opener = opener

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("ANYAICAM_CLOUD_URL", "")
            or os.environ.get("ANYAICAM_APPLIANCE_API_URL", ""),
            os.environ.get("ANYAICAM_APPLIANCE_ID", ""),
            os.environ.get("ANYAICAM_APPLIANCE_CREDENTIAL", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.appliance_id and self.credential)

    def report(self, inventory: dict) -> dict:
        if not self.configured:
            return {
                "status": "skipped",
                "reason": "appliance registration is not configured",
            }
        cameras = [
            {
                "id": str(camera.get("id", ""))[:100],
                "name": str(camera.get("name") or "Camera")[:160],
                "online": camera.get("status") == "online",
                "recording": False,
                "analytics": False,
                "manufacturer": str(camera.get("manufacturer") or "Unknown")[:120],
                "model": str(camera.get("model") or "Unknown")[:120],
                "protocols": [
                    protocol
                    for protocol in ("onvif", "rtsp")
                    if camera.get(protocol)
                ],
            }
            for camera in inventory.get("cameras", [])
            if camera.get("id")
        ]
        request = Request(
            urljoin(f"{self.base_url}/", "api/appliance/cameras"),
            data=json.dumps({"cameras": cameras}).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.credential}",
                "Content-Type": "application/json",
                "X-Appliance-ID": self.appliance_id,
                "X-Request-Timestamp": str(int(time.time())),
                "X-Request-Nonce": secrets.token_urlsafe(18),
            },
        )
        with self.opener(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        return {"status": "reported", "camera_count": len(cameras), "cloud": result}
