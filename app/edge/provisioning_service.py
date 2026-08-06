"""Orchestration for discovery-to-provisioning and sanitized synchronization."""

from __future__ import annotations

from edge.camera_health import CameraHealthMonitor
from edge.camera_inventory import CameraInventoryStore
from edge.camera_provisioning import CameraConfigurationStore, CameraProbeService, normalize_name, private_camera_url
from edge.cloud_inventory import CloudInventoryReporter


class CameraProvisioningService:
    def __init__(
        self,
        inventory: CameraInventoryStore,
        configurations: CameraConfigurationStore,
        probe: CameraProbeService,
        health: CameraHealthMonitor,
        reporter_factory=CloudInventoryReporter.from_environment,
        max_camera_number: int = 256,
    ):
        self.inventory = inventory
        self.configurations = configurations
        self.probe = probe
        self.health = health
        self.reporter_factory = reporter_factory
        self.max_camera_number = max(1, int(max_camera_number))

    def discovered_camera(self, camera_id: str) -> dict:
        camera = next((item for item in self.inventory.load()["cameras"] if item.get("id") == camera_id), None)
        if not camera:
            raise KeyError(camera_id)
        return camera

    def validation_inputs(self, camera_id: str, payload: dict) -> dict:
        discovered = self.discovered_camera(camera_id)
        configured = self.configurations.get(camera_id) or {}
        streams = discovered.get("stream_urls") or []
        onvif_urls = discovered.get("onvif_xaddrs") or []
        raw_rtsp = str(payload.get("rtsp_url") or configured.get("rtsp_url") or (streams[0] if streams else ""))
        raw_onvif = str(payload.get("onvif_url") or configured.get("onvif_url") or (onvif_urls[0] if onvif_urls else ""))
        return {
            "rtsp_url": private_camera_url(raw_rtsp, {"rtsp"}) if raw_rtsp else "",
            "onvif_url": private_camera_url(raw_onvif, {"http", "https"}) if raw_onvif else "",
            "username": str(payload.get("username") if "username" in payload else configured.get("username") or ""),
            "password": str(payload.get("password") if "password" in payload else configured.get("password") or ""),
        }

    def validate(self, camera_id: str, payload: dict) -> dict:
        values = self.validation_inputs(camera_id, payload)
        if not values["rtsp_url"]:
            raise ValueError("A discovered or operator-supplied RTSP URL is required.")
        rtsp = self.probe.validate_rtsp(values["rtsp_url"], values["username"], values["password"])
        onvif = (
            self.probe.validate_onvif(values["onvif_url"], values["username"], values["password"])
            if values["onvif_url"] else {"valid": None, "status": "not_available"}
        )
        return {"rtsp": rtsp, "onvif": onvif, "ready": bool(rtsp.get("valid") and onvif.get("valid") is not False)}

    def provision(self, camera_id: str, payload: dict) -> dict:
        discovered = self.discovered_camera(camera_id)
        values = self.validation_inputs(camera_id, payload)
        validation = self.validate(camera_id, payload)
        if not validation["rtsp"].get("valid"):
            raise ValueError(validation["rtsp"].get("error") or "RTSP validation failed.")
        if values["onvif_url"] and not validation["onvif"].get("valid"):
            raise ValueError(validation["onvif"].get("error") or "ONVIF credential validation failed.")
        camera_number = int(payload.get("camera_number") or 0)
        if camera_number < 1 or camera_number > self.max_camera_number:
            raise ValueError(f"Camera number must be between 1 and {self.max_camera_number}.")
        duplicate = next(
            (item for item in self.configurations.load()["cameras"] if int(item.get("camera_number") or 0) == camera_number and item.get("id") != camera_id),
            None,
        )
        if duplicate:
            raise ValueError("Camera number is already assigned.")
        record = self.configurations.save_camera({
            "id": camera_id,
            "camera_number": camera_number,
            "name": normalize_name(payload.get("name") or discovered.get("name") or "Camera"),
            "rtsp_url": values["rtsp_url"],
            "onvif_url": values["onvif_url"],
            "username": values["username"],
            "password": values["password"],
            "enabled": bool(payload.get("enabled", True)),
            "manufacturer": validation["onvif"].get("manufacturer") or discovered.get("manufacturer") or "Unknown",
            "model": validation["onvif"].get("model") or discovered.get("model") or "Unknown",
            "stream": validation["rtsp"],
        })
        self.inventory.update(camera_id, {
            "name": record["name"],
            "camera_number": camera_number,
            "provisioning_status": "provisioned",
            "rtsp_valid": True,
            "onvif_valid": validation["onvif"].get("valid"),
            "fps": validation["rtsp"].get("fps", 0),
            "bitrate_bps": validation["rtsp"].get("bitrate_bps", 0),
        })
        try:
            cloud_report = self.reporter_factory().report(self.inventory.load())
        except (OSError, ValueError) as error:
            cloud_report = {"status": "failed", "error": str(error)[:240]}
        return {"camera": record, "validation": validation, "cloud_report": cloud_report, "credentials_returned": False}

    def rename(self, camera_id: str, name: str) -> dict:
        camera = self.configurations.rename(camera_id, name)
        self.inventory.update(camera_id, {"name": camera["name"]})
        return camera

    def thumbnail(self, camera_id: str, payload: dict) -> bytes:
        values = self.validation_inputs(camera_id, payload)
        if not values["rtsp_url"]:
            raise ValueError("A discovered or configured RTSP URL is required.")
        return self.probe.thumbnail(values["rtsp_url"], values["username"], values["password"])

    def refresh_health(self) -> dict:
        result = self.health.refresh()
        for item in result["cameras"]:
            try:
                self.inventory.update(item["id"], {
                    "status": "online" if item["rtsp"] == "online" else "offline",
                    "health_state": item["state"],
                    "recording": item["recording"] == "running",
                    "fps": item["fps"],
                    "bitrate_bps": item["bitrate_bps"],
                })
            except KeyError:
                continue
        return result

    def synchronize(self) -> dict:
        health = self.refresh_health()
        result = self.reporter_factory().report(self.inventory.load())
        return {"health": health, "cloud_report": result, "sanitized": True}
