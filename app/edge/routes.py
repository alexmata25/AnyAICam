"""HTTP integration for edge-local camera discovery."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from edge.camera_discovery import CameraDiscoveryService, DiscoveryOptions
from edge.camera_health import CameraHealthMonitor, CameraHealthStore
from edge.camera_inventory import CameraInventoryStore
from edge.camera_provisioning import CameraConfigurationStore, CameraProbeService
from edge.cloud_inventory import CloudInventoryReporter
from edge.provisioning_service import CameraProvisioningService


DEFAULT_CONFIG_ROOT = Path("/opt/anyaicam/data/config")


def register_edge_discovery_routes(
    app: FastAPI,
    *,
    current_user: Callable,
    has_permission: Callable,
    runtime_role: str,
    structured_log: Callable,
    page_shell: Callable | None = None,
    hls_folder: str | Path = "/app/static/hls",
    recordings_folder: str | Path = "/app/recordings",
    hls_freshness_seconds: int = 15,
    process_state: Callable[[int], dict] | None = None,
    camera_capacity: int = 256,
) -> CameraProvisioningService:
    config_root = Path(
        os.environ.get("ANYAICAM_CONFIG_ROOT", str(DEFAULT_CONFIG_ROOT))
    )
    inventory_store = CameraInventoryStore(config_root / "camera_inventory.json")
    configuration_store = CameraConfigurationStore(config_root / "provisioned_cameras.json")
    health_store = CameraHealthStore(config_root / "camera_health.json")
    discovery = CameraDiscoveryService()
    probe = CameraProbeService()
    health_monitor = CameraHealthMonitor(
        configuration_store,
        health_store,
        probe,
        hls_folder,
        recordings_folder,
        hls_freshness_seconds,
        process_state,
    )
    provisioning = CameraProvisioningService(
        inventory_store,
        configuration_store,
        probe,
        health_monitor,
        max_camera_number=camera_capacity,
    )

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

    @app.get("/api/edge/cameras/configuration")
    def edge_camera_configuration(request: Request) -> dict:
        require_edge_operator(request)
        return configuration_store.safe_view()

    @app.post("/api/edge/cameras/{camera_id}/validate")
    def validate_edge_camera(request: Request, camera_id: str, payload: dict) -> dict:
        require_edge_operator(request)
        try:
            return provisioning.validate(camera_id, payload)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Discovered camera not found.") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/edge/cameras/{camera_id}/provision")
    def provision_edge_camera(request: Request, camera_id: str, payload: dict) -> dict:
        user = require_edge_operator(request)
        try:
            result = provisioning.provision(camera_id, payload)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Discovered camera not found.") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        structured_log(
            "edge.camera_provisioned",
            camera_id=camera_id,
            camera_number=result["camera"]["camera_number"],
            actor=user.get("id", "unknown"),
        )
        return {"status": "provisioned", **result}

    @app.patch("/api/edge/cameras/{camera_id}/name")
    def rename_edge_camera(request: Request, camera_id: str, payload: dict) -> dict:
        require_edge_operator(request)
        try:
            return {"status": "updated", "camera": provisioning.rename(camera_id, str(payload.get("name") or ""))}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Provisioned camera not found.") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/edge/cameras/{camera_id}/thumbnail")
    def edge_camera_thumbnail(request: Request, camera_id: str, payload: dict):
        require_edge_operator(request)
        try:
            image = provisioning.thumbnail(camera_id, payload)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Discovered camera not found.") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/edge/cameras/health")
    def edge_camera_health(request: Request) -> dict:
        require_edge_operator(request)
        return health_store.load()

    @app.post("/api/edge/cameras/health/refresh")
    def refresh_edge_camera_health(request: Request) -> dict:
        require_edge_operator(request)
        return provisioning.refresh_health()

    @app.post("/api/edge/cameras/synchronize")
    def synchronize_edge_cameras(request: Request) -> dict:
        require_edge_operator(request)
        try:
            return provisioning.synchronize()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)[:240]) from error

    if page_shell:
        @app.get("/edge/camera-provisioning", response_class=HTMLResponse)
        def edge_camera_provisioning_page(request: Request):
            require_edge_operator(request)
            content = '''<header class="topbar"><div><p class="eyebrow">Edge Appliance</p><h1>Camera provisioning & health</h1></div><span class="pill">RTSP stays local</span></header><section class="panel"><div class="panel-head"><div><h2>Discovered cameras</h2><p class="health-detail">Validate credentials, preview, name, and provision cameras after discovery.</p></div><div class="library-toolbar"><button id="reload-cameras" class="filter">Reload</button><button id="sync-cameras" class="filter">Synchronize sanitized inventory</button></div></div><div id="provisioning-grid" class="account-grid"></div></section><section class="panel" style="margin-top:18px"><div class="panel-head"><div><h2>Provisioned camera health</h2><p class="health-detail">RTSP, HLS freshness, FPS, bitrate, and recording state.</p></div><button id="refresh-camera-health" class="filter">Run health check</button></div><div id="provisioning-health" class="account-grid"></div></section>'''
            scripts = '''<script>const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function jsonCall(url,options={}){const response=await fetch(url,options),data=await response.json();if(!response.ok)throw new Error(data.detail||'Request failed');return data}function cameraCard(c,index){const stream=(c.stream_urls||[])[0]||'',onvif=(c.onvif_xaddrs||[])[0]||'';return `<article class="feature-card"><h3>${esc(c.name||'Camera')}</h3><p>${esc(c.manufacturer||'Unknown')} · ${esc(c.model||'Unknown')} · ${esc(c.ip_address)}</p><img id="preview-${c.id}" alt="Camera thumbnail" style="width:100%;aspect-ratio:16/9;object-fit:cover" hidden><label>Friendly name<input id="name-${c.id}" value="${esc(c.name||'Camera')}"></label><label>Camera number<input id="number-${c.id}" type="number" min="1" max="256" value="${Number(c.camera_number)||index+1}"></label><label>RTSP URL<input id="rtsp-${c.id}" value="${esc(stream)}"></label><label>ONVIF URL<input id="onvif-${c.id}" value="${esc(onvif)}"></label><label>Username<input id="user-${c.id}" autocomplete="username"></label><label>Password<input id="password-${c.id}" type="password" autocomplete="current-password"></label><div class="library-toolbar"><button class="filter" onclick="validateCamera('${c.id}')">Validate</button><button class="filter" onclick="previewCamera('${c.id}')">Preview</button><button class="action-button" onclick="provisionCamera('${c.id}')">Provision</button></div><div id="result-${c.id}" class="health-detail"></div></article>`}function payload(id){return {name:document.getElementById('name-'+id).value,camera_number:Number(document.getElementById('number-'+id).value),rtsp_url:document.getElementById('rtsp-'+id).value,onvif_url:document.getElementById('onvif-'+id).value,username:document.getElementById('user-'+id).value,password:document.getElementById('password-'+id).value}}async function loadCameras(){const data=await jsonCall('/api/edge/cameras/inventory');document.getElementById('provisioning-grid').innerHTML=data.cameras.map(cameraCard).join('')||'<div class="empty">Run Edge camera discovery first.</div>'}async function validateCamera(id){try{const data=await jsonCall('/api/edge/cameras/'+id+'/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload(id))});document.getElementById('result-'+id).textContent=data.ready?`Validated · ${data.rtsp.fps||0} FPS · ${data.rtsp.bitrate_bps||0} bps`:(data.rtsp.error||data.onvif.error||'Validation failed')}catch(error){showToast(error.message)}}async function provisionCamera(id){try{await jsonCall('/api/edge/cameras/'+id+'/provision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload(id))});document.getElementById('password-'+id).value='';showToast('Camera provisioned');loadCameras();refreshHealth()}catch(error){showToast(error.message)}}async function previewCamera(id){try{const response=await fetch('/api/edge/cameras/'+id+'/thumbnail',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload(id))});if(!response.ok){const data=await response.json();throw new Error(data.detail||'Preview failed')}const image=document.getElementById('preview-'+id);image.src=URL.createObjectURL(await response.blob());image.hidden=false}catch(error){showToast(error.message)}}function renderHealth(items){document.getElementById('provisioning-health').innerHTML=items.map(c=>`<article class="feature-card"><h3>${esc(c.name)}</h3><span class="pill">${esc(c.state)}</span><p>RTSP ${esc(c.rtsp)} · HLS ${esc(c.hls)} · Recording ${esc(c.recording)}</p><p>${Number(c.fps)||0} FPS · ${Number(c.bitrate_bps)||0} bps</p><p>${esc(c.error||'No current error')}</p></article>`).join('')||'<div class="empty">No provisioned cameras.</div>'}async function refreshHealth(){try{const data=await jsonCall('/api/edge/cameras/health/refresh',{method:'POST'});renderHealth(data.cameras)}catch(error){showToast(error.message)}}document.getElementById('reload-cameras').onclick=loadCameras;document.getElementById('refresh-camera-health').onclick=refreshHealth;document.getElementById('sync-cameras').onclick=async()=>{try{const data=await jsonCall('/api/edge/cameras/synchronize',{method:'POST'});showToast(data.cloud_report.status==='reported'?'Inventory synchronized':'Inventory saved locally; cloud registration is not configured')}catch(error){showToast(error.message)}};loadCameras();jsonCall('/api/edge/cameras/health').then(data=>renderHealth(data.cameras)).catch(()=>{});</script>'''
            scripts = scripts.replace('max="256"', f'max="{camera_capacity}"')
            return page_shell("Camera provisioning", "camera-provisioning", content, scripts)

    return provisioning
