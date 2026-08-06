"""HTTP integration for edge-local camera discovery."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request

from edge.camera_discovery import CameraDiscoveryService, DiscoveryOptions
from edge.camera_inventory import CameraInventoryStore
from edge.cloud_inventory import CloudInventoryReporter


DEFAULT_CONFIG_ROOT = Path("/opt/anyaicam/data/config")


def register_edge_discovery_routes(
    app: FastAPI,
    *,
    current_user: Callable,
    has_permission: Callable,
    runtime_role: str,
    structured_log: Callable,
) -> None:
    config_root = Path(
        os.environ.get("ANYAICAM_CONFIG_ROOT", str(DEFAULT_CONFIG_ROOT))
    )
    inventory_store = CameraInventoryStore(config_root / "camera_inventory.json")
    discovery = CameraDiscoveryService()

    def require_edge_operator(request: Request) -> dict:
        if runtime_role not in {"edge", "combined"}:
            raise HTTPException(
                status_code=409,
                detail="Camera discovery runs only on the Edge Appliance.",
            )
        user = current_user(request)
        if not has_permission(user, "manage_settings"):
            raise HTTPException(
                status_code=403,
                detail="Settings permission is required.",
            )
        return user

    @app.get("/api/edge/cameras/inventory")
    def edge_camera_inventory(request: Request) -> dict:
        require_edge_operator(request)
        return inventory_store.load()

    @app.post("/api/edge/cameras/discover")
    def discover_edge_cameras(request: Request, payload: dict) -> dict:
        user = require_edge_operator(request)
        if payload.get("authorized") is not True:
            raise HTTPException(
                status_code=400,
                detail="Authorization to scan this private network must be confirmed.",
            )
        network = str(payload.get("network") or "").strip()
        raw_ports = payload.get("ports") or []
        if not isinstance(raw_ports, list) or len(raw_ports) > 16:
            raise HTTPException(status_code=400, detail="Invalid discovery port list.")
        try:
            ports = tuple(int(port) for port in raw_ports) or DiscoveryOptions(network).ports
            options = DiscoveryOptions(
                network=network,
                ports=ports,
                connect_timeout=max(
                    0.05,
                    min(2.0, float(payload.get("connect_timeout", 0.35))),
                ),
                onvif_timeout=max(
                    0.25,
                    min(5.0, float(payload.get("onvif_timeout", 1.5))),
                ),
            )
            discovered = discovery.scan(options)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        inventory = inventory_store.reconcile(str(options.network), discovered)
        try:
            cloud_report = CloudInventoryReporter.from_environment().report(inventory)
        except (OSError, ValueError) as error:
            cloud_report = {"status": "failed", "error": str(error)[:240]}
        structured_log(
            "edge.camera_discovery.completed",
            network=network,
            discovered=len(discovered),
            inventory_total=len(inventory["cameras"]),
            cloud_report_status=cloud_report["status"],
            actor=user.get("id", "unknown"),
        )
        return {
            "status": "complete",
            "discovered": len(discovered),
            "inventory": inventory,
            "cloud_report": cloud_report,
            "credentials_stored": False,
        }

    @app.post("/api/edge/cameras/inventory/report")
    def report_edge_camera_inventory(request: Request) -> dict:
        require_edge_operator(request)
        inventory = inventory_store.load()
        try:
            return CloudInventoryReporter.from_environment().report(inventory)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)[:240]) from error
