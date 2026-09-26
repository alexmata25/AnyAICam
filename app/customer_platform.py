
import json
from datetime import datetime
from html import escape
from typing import Callable

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from partner_portal import partner_identity
from partner_db import connection, row, rows

class CameraNameUpdate(BaseModel):
    name: str = Field(default="", max_length=60)


class AdminEntitlementChange(BaseModel):
    """One per-camera analytic on/off, by cameras.id (2026-09-25)."""
    camera_id: str = Field(min_length=1, max_length=64)
    analytic_key: str = Field(min_length=1, max_length=64)
    enabled: bool



def _portal_camera_analytics(user: dict) -> dict[int, dict]:
    """camera_number -> {"id": cameras.id, "analytics": [{key,label,enabled,
    description}]} from the REAL per-camera entitlements
    (camera_analytics_entitlements -- what the camera page, the Analytics
    workspace and the license-capped assignment route use). 2026-09-25:
    this page previously read customer_camera_features.json, a legacy
    store nothing that runs analytics reads, so it showed "Upgrade
    required" for analytics that were actually on."""
    from customer_analytics_panel import UPGRADE_CARD_CONTENT, analytics_row_state, camera_entitlement_rows
    customer_id = user.get("customer_id")
    if not customer_id:
        return {}
    out: dict[int, dict] = {}
    with connection() as db:
        for camera in db.execute(
            "SELECT id, camera_number FROM cameras WHERE customer_id=? AND camera_number IS NOT NULL ORDER BY camera_number",
            (customer_id,),
        ).fetchall():
            analytics = analytics_row_state(camera_entitlement_rows(db, camera["id"]))
            for item in analytics:
                item["description"] = (UPGRADE_CARD_CONTENT.get(item["key"]) or {}).get("description", "")
            out[int(camera["camera_number"])] = {"id": camera["id"], "analytics": analytics}
    return out


def _is_customer(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"customer_owner", "customer_viewer"}


def _portal_customer_user(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"}:
        return {}

    account = row(
        "SELECT id,email,name,role,customer_id FROM partner_users WHERE lower(email)=lower(?)",
        (identity.get("email", ""),),
    )
    if not account:
        return {}

    if account.get("customer_id") != identity.get("customer_id"):
        return {}

    user = dict(account)
    user["display_name"] = user.get("name") or user.get("email") or "Customer"
    return user


def _portal_customer_camera_ids(user: dict) -> list[int]:
    customer_id = user.get("customer_id")
    if not customer_id:
        return []

    camera_rows = rows(
        "SELECT camera_number FROM cameras "
        "WHERE customer_id=? AND camera_number IS NOT NULL "
        "ORDER BY camera_number",
        (customer_id,),
    )

    return [int(camera["camera_number"]) for camera in camera_rows]


def _portal_customer_camera_names(user: dict) -> dict[int, str]:
    """Camera-number -> display name, for the same fleet
    _portal_customer_camera_ids() scopes. Prefers the real cameras.name
    column (same field live_view_page.py's /customer-live already reads)
    over a generic "Camera N" fallback, so an installer-assigned label
    such as "Front Door" shows up here too, without changing what's
    stored -- read-only, display purposes only."""
    customer_id = user.get("customer_id")
    if not customer_id:
        return {}

    camera_rows = rows(
        "SELECT camera_number, name FROM cameras "
        "WHERE customer_id=? AND camera_number IS NOT NULL "
        "ORDER BY camera_number",
        (customer_id,),
    )

    names = {}
    for camera in camera_rows:
        number = int(camera["camera_number"])
        label = (camera.get("name") or "").strip()
        names[number] = label if label and label != f"Camera {number}" else f"Camera {number}"
    return names


def _portal_customer_raw_camera_names(user: dict) -> dict[int, str]:
    """Camera-number -> the literal stored cameras.name value (empty
    string when unset), for the rename control's own input field.
    Deliberately not reusing _portal_customer_camera_names()'s output --
    that function substitutes the "Camera N" fallback for display
    everywhere else, which would make an unsaved input look identical
    to a saved name of literally "Camera N" text."""
    customer_id = user.get("customer_id")
    if not customer_id:
        return {}

    camera_rows = rows(
        "SELECT camera_number, name FROM cameras "
        "WHERE customer_id=? AND camera_number IS NOT NULL "
        "ORDER BY camera_number",
        (customer_id,),
    )
    return {int(camera["camera_number"]): (camera.get("name") or "").strip() for camera in camera_rows}


def register_customer_platform_routes(
    app,
    *,
    page_shell: Callable,
    current_user: Callable,
    user_camera_ids: Callable,
    load_license_state: Callable,
    record_audit: Callable,
    is_master_admin: Callable,
):
    @app.get("/customer-portal", response_class=HTMLResponse)
    def customer_portal(request: Request):
        user = _portal_customer_user(request)
        if not _is_customer(user):
            raise HTTPException(status_code=403, detail="Customer account required.")

        cameras = _portal_customer_camera_ids(user)
        camera_names = _portal_customer_camera_names(user)
        real = _portal_camera_analytics(user)
        enabled_total = 0
        cameras_with_analytics = 0
        cards = []

        for camera_id in cameras:
            info = real.get(camera_id) or {"id": None, "analytics": []}
            enabled_labels = [item["label"] for item in info["analytics"] if item.get("enabled")]
            enabled_total += len(enabled_labels)
            cameras_with_analytics += 1 if enabled_labels else 0
            camera_label = camera_names.get(camera_id, f"Camera {camera_id}")
            summary = ("Analytics: " + ", ".join(enabled_labels)) if enabled_labels else "No analytics enabled on this camera"
            live_href = f"/customer/cameras/{escape(str(info['id']), quote=True)}/live" if info["id"] else "/customer-live"
            cards.append(
                f'''<article class="feature-card">
                  <div class="feature-icon">▣</div>
                  <h2>{escape(camera_label)}</h2>
                  <p>{escape(summary)}</p>
                  <a class="download" href="{live_href}">Open camera</a>
                </article>'''
            )

        content = f'''
        <header class="topbar">
          <div><p class="eyebrow">Customer VMS</p><h1>Welcome, {user.get("display_name","Customer")}</h1></div>
          <a class="action-button" href="/mobile-app">Install mobile app</a>
        </header>
        <section class="launch-summary">
          <article class="launch-stat"><span>Authorized cameras</span><strong>{len(cameras)}</strong></article>
          <article class="launch-stat"><span>Cameras with analytics</span><strong>{cameras_with_analytics}</strong></article>
          <article class="launch-stat"><span>Analytics enabled</span><strong>{enabled_total}</strong></article>
          <article class="launch-stat"><span>Portal status</span><strong>Active</strong></article>
        </section>
        <section class="panel" style="margin-top:18px">
          <div class="panel-head"><div><h2>Your cameras</h2><div class="health-detail">Only cameras assigned to your account appear here.</div></div></div>
          <div class="feature-grid">{"".join(cards) or '<div class="empty">No cameras are assigned to this customer account.</div>'}</div>
        </section>
        <section class="panel" style="margin-top:18px">
          <div class="panel-head"><div><h2>Customer controls</h2><div class="health-detail">Manage paid camera analytics and notification preferences separately for each camera.</div></div></div>
          <div class="settings-list">
            <a class="setting-link" href="/customer-app-settings"><div><strong>Camera analytics and alerts</strong><div class="health-detail">Enable paid analytics, email alerts, push alerts, schedules, and quiet hours.</div></div><span>Open →</span></a>
            <a class="setting-link" href="/playback"><div><strong>Playback</strong><div class="health-detail">Review authorized recordings and event footage.</div></div><span>Open →</span></a>
            <a class="setting-link" href="/subscription-portal"><div><strong>Subscription</strong><div class="health-detail">Review billing and plan access.</div></div><span>Open →</span></a>
            <a class="setting-link" href="/mobile-app"><div><strong>Install phone app</strong><div class="health-detail">Install ANY AI CAM on Android or iPhone.</div></div><span>Install →</span></a>
          </div>
        </section>
        '''
        return page_shell("Customer portal", "dashboard", content)

    @app.get("/customer-app-settings", response_class=HTMLResponse)
    def customer_app_settings(request: Request):
        user = _portal_customer_user(request)
        if not _is_customer(user):
            raise HTTPException(status_code=403, detail="Customer account required.")

        cameras = _portal_customer_camera_ids(user)
        camera_names = _portal_customer_camera_names(user)
        raw_camera_names = _portal_customer_raw_camera_names(user)
        camera_options = "".join(
            f'<option value="{camera}">{escape(camera_names.get(camera, f"Camera {camera}"))}</option>'
            for camera in cameras
        )
        no_cameras_notice = "" if cameras else (
            '<div class="health-detail" id="no-cameras-notice" style="margin-top:8px">'
            'No cameras are set up on your account yet. Once a camera is added, its analytics and alerts can be configured here.</div>'
        )
        content = f'''
        <style>#camera-select,#camera-name-input{{min-height:40px;padding:8px 11px;border:1px solid rgba(170,196,207,.3);border-radius:9px;background:#111827;color:#fff;font:inherit;font-size:15px}}</style>
        <header class="topbar">
          <div><p class="eyebrow">Settings</p><h1>Camera analytics and alerts</h1></div>
          <a class="ghost-button" href="/settings/notifications">Notification settings</a>
          <a class="ghost-button" href="/customer-portal">Back to customer portal</a>
        </header>
        <section class="panel">
          <div class="panel-head"><div><h2>Choose a camera</h2><div class="health-detail">Paid analytics are enabled individually per camera.</div></div></div>
          <label style="display:grid;gap:7px;max-width:360px">Camera<select id="camera-select">{camera_options}</select></label>{no_cameras_notice}
        </section>
        <section class="panel" style="margin-top:16px">
          <div class="panel-head"><div><h2>Camera name</h2><div class="health-detail">Give this camera a name your household or team will recognize, like "Front Door" or "Driveway Right." This changes the display name only -- the camera's stream, recording, and analytics are unaffected. Leave blank to use the default "Camera N" label.</div></div></div>
          <label style="display:grid;gap:7px;max-width:360px">Display name
            <input id="camera-name-input" type="text" maxlength="60" placeholder="Camera name">
          </label>
          <button class="action-button" id="save-camera-name" type="button" style="margin-top:12px">Save camera name</button>
        </section>
        <div class="notification-grid" style="margin-top:16px">
          <section class="panel">
            <div class="panel-head"><div><h2>Analytics</h2><div class="health-detail">Analytics run per camera from your plan -- the same status the camera's live page shows.</div></div></div>
            <div id="camera-analytics-list" class="health-list"><div class="health-detail">Loading…</div></div>
          </section>
          <section class="panel" id="alert-program">
            <div class="panel-head"><div><h2>Alerts</h2><div class="health-detail">Choose which events alert you, for which cameras, by email or text, and quiet hours.</div></div></div>
            <a class="action-button" href="/settings/notifications">Open notification settings</a>
            <div class="push-status" style="margin-top:16px">
              <strong>Phone alerts</strong>
              <p class="health-detail">Pair your phone with the AnyAiCam app to get push alerts, or pause alerts for a paired device.</p>
              <a class="ghost-button" href="/mobile-devices">Manage mobile devices</a>
            </div>
          </section>
        </div>
        '''
        scripts = '''
        <script>
        const isOwner=''' + json.dumps(str(user.get("role") or "").lower() == "customer_owner") + ''';
        const cameraSelect=document.getElementById('camera-select');
        const cameraNameInput=document.getElementById('camera-name-input');
        const rawCameraNames=''' + json.dumps(raw_camera_names) + ''';

        function populateCameraName(){
          const cameraId=cameraSelect.value;
          cameraNameInput.value=rawCameraNames[cameraId]||'';
          cameraNameInput.placeholder=`Camera ${cameraId}`;
        }

        // A new account can have no cameras yet: nothing camera-specific can
        // be loaded or saved until one exists.
        const noCamera=()=>{if(cameraSelect.value)return false;showToast('Add a camera first.');return true};

        document.getElementById('save-camera-name').onclick=async()=>{
          if(noCamera())return;
          const cameraId=cameraSelect.value;
          const name=cameraNameInput.value.trim();
          const response=await fetch(`/api/customer/cameras/${cameraId}/name`,{
            method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})
          });
          const data=await response.json();
          if(!response.ok){showToast(data.detail||'Could not save camera name.');return}
          rawCameraNames[cameraId]=data.name;
          cameraNameInput.value=data.name;
          const option=cameraSelect.querySelector(`option[value="${cameraId}"]`);
          if(option)option.textContent=data.display_name;
          showToast(data.message||'Camera name saved.');
        };

        function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
        const analyticsList=document.getElementById('camera-analytics-list');
        async function loadCameraSettings(){
          const cameraId=cameraSelect.value;
          if(!cameraId){analyticsList.innerHTML='<div class="health-detail">No camera yet.</div>';return}
          analyticsList.innerHTML='<div class="health-detail">Loading…</div>';
          const response=await fetch(`/api/customer/cameras/${cameraId}/app-settings`);
          const data=await response.json().catch(()=>({}));
          if(!response.ok||!Array.isArray(data.analytics)){
            analyticsList.innerHTML=`<div class="health-detail">${esc(data.detail||'Could not load analytics for this camera.')}</div>`;return;
          }
          analyticsList.innerHTML=data.analytics.map(item=>{
            const status=item.enabled
              ?'<span class="active-badge">Enabled on this camera</span>'
              :`<span class="pending-badge">Not enabled</span> <a class="download" href="/subscription-portal">View plans</a>${isOwner&&data.camera_uuid?` <button class="compact-button" type="button" data-add="${esc(item.key)}">Add to this camera</button>`:''}`;
            return `<div class="health-row"><span><span class="health-name">${esc(item.label)}</span><br><span class="health-detail">${esc(item.description)}</span></span><span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end">${status}</span></div>`;
          }).join('');
          analyticsList.querySelectorAll('[data-add]').forEach(button=>button.addEventListener('click',async()=>{
            button.disabled=true;
            const r=await fetch(`/api/customer/cameras/${encodeURIComponent(data.camera_uuid)}/analytics/${encodeURIComponent(button.dataset.add)}`,{method:'POST',credentials:'same-origin'});
            const body=await r.json().catch(()=>({}));
            if(!r.ok){showToast(typeof body.detail==='string'?body.detail:'This analytic could not be added.');button.disabled=false;return}
            showToast(body.message||'Analytic enabled for this camera.');
            loadCameraSettings();
          }));
        }

        cameraSelect.onchange=()=>{loadCameraSettings();populateCameraName()};
        loadCameraSettings();
        populateCameraName();
        </script>
        '''
        return page_shell("Camera analytics and alerts", "customer-app-settings", content, scripts)

    @app.get("/api/customer/cameras/{camera_id}/app-settings")
    def customer_camera_settings(camera_id: int, request: Request):
        user = _portal_customer_user(request)
        if not _is_customer(user):
            raise HTTPException(status_code=403, detail="Customer account required.")
        if camera_id not in _portal_customer_camera_ids(user):
            raise HTTPException(status_code=403, detail="Camera is not assigned to this account.")
        real = _portal_camera_analytics(user).get(camera_id) or {"id": None, "analytics": []}
        return {"camera_id": camera_id, "camera_uuid": real["id"], "analytics": real["analytics"]}

    @app.put("/api/customer/cameras/{camera_id}/name")
    def update_customer_camera_name(camera_id: int, payload: CameraNameUpdate, request: Request):
        """Renames one of this customer's own cameras. Reuses the same
        cameras.name column every other customer-facing page already
        reads (_portal_customer_camera_names(), live_view_page.py's
        Live/detail views, _render_customer_playback()) -- no new
        naming system, no schema change. The camera's internal id and
        camera_number are looked up, never written; only the display
        name column changes, so RTSP/ONVIF config, recording, and
        analytics are completely unaffected. An empty name clears the
        override and every reader already falls back to "Camera N"."""
        user = _portal_customer_user(request)
        if str(user.get("role") or "").lower() != "customer_owner":
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        if camera_id not in _portal_customer_camera_ids(user):
            raise HTTPException(status_code=403, detail="Camera is not assigned to this account.")

        name = payload.name.strip()
        with connection() as db:
            db.execute(
                "UPDATE cameras SET name=? WHERE customer_id=? AND camera_number=?",
                (name, user["customer_id"], camera_id),
            )
        record_audit(request, "update", f"customer-camera:{camera_id}", "Customer renamed a camera.")
        display_name = name or f"Camera {camera_id}"
        return {"status": "complete", "name": name, "display_name": display_name, "message": "Camera name saved."}

    # Admin analytics entitlements (2026-09-25): the same per-camera system
    # the customer portal, the camera page and the Analytics workspace use
    # (camera_analytics_entitlements via customer_analytics_panel), and the
    # same billing cap (analytics_subscriptions licenses). This page used to
    # write customer_camera_features.json, which nothing that runs analytics
    # reads -- one source of truth now.
    def _require_master_admin(request: Request) -> dict:
        user = current_user(request)
        if not is_master_admin(user):
            raise HTTPException(status_code=403, detail="Master administrator required.")
        return user

    @app.get("/analytics-entitlements", response_class=HTMLResponse)
    def analytics_entitlements_page(request: Request):
        _require_master_admin(request)
        content = """
        <header class="topbar"><div><p class="eyebrow">Paid feature control</p><h1>Analytics entitlements</h1></div></header>
        <section class="panel">
          <div class="panel-head"><div><h2>Per-camera analytics</h2><div class="health-detail">Turns an analytic on or off for one camera -- the same setting customers see on the camera page and in Settings. Capacity comes from the customer's purchased licenses for that site.</div></div></div>
          <label style="display:grid;gap:7px;max-width:420px">Customer<select id="ent-customer"><option value="">Choose a customer…</option></select></label>
          <div id="ent-cameras" class="health-list" style="margin-top:14px"></div>
          <div id="entitlement-message" class="health-detail" role="status" aria-live="polite" style="margin-top:12px"></div>
        </section>
        """
        scripts = """
        <script>
        const customerSelect=document.getElementById('ent-customer');
        const cameraList=document.getElementById('ent-cameras');
        const message=document.getElementById('entitlement-message');
        function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
        async function loadCustomers(){
          const r=await fetch('/api/admin/analytics-entitlements');const d=await r.json().catch(()=>({}));
          if(!r.ok){message.textContent=d.detail||'Could not load customers.';return}
          customerSelect.insertAdjacentHTML('beforeend',d.customers.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join(''));
        }
        async function loadCameras(){
          cameraList.innerHTML='';message.textContent='';
          if(!customerSelect.value)return;
          const r=await fetch(`/api/admin/analytics-entitlements?customer_id=${encodeURIComponent(customerSelect.value)}`);
          const d=await r.json().catch(()=>({}));
          if(!r.ok){message.textContent=d.detail||'Could not load cameras.';return}
          if(!d.cameras.length){cameraList.innerHTML='<div class="health-detail">This customer has no cameras yet.</div>';return}
          cameraList.innerHTML=d.cameras.map(camera=>`<div class="health-row" style="align-items:flex-start"><span><span class="health-name">${esc(camera.name)}</span><br><span class="health-detail">${esc(camera.site||'')}</span></span><span style="display:flex;flex-wrap:wrap;gap:12px;justify-content:flex-end">${camera.analytics.map(a=>`<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" data-camera="${esc(camera.id)}" data-key="${esc(a.key)}" ${a.enabled?'checked':''}> ${esc(a.label)}</label>`).join('')}</span></div>`).join('');
        }
        cameraList.addEventListener('change',async event=>{
          const box=event.target.closest('input[data-key]');if(!box)return;
          box.disabled=true;
          const r=await fetch('/api/admin/analytics-entitlements',{method:'PUT',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({camera_id:box.dataset.camera,analytic_key:box.dataset.key,enabled:box.checked})});
          const d=await r.json().catch(()=>({}));
          box.disabled=false;
          if(!r.ok){box.checked=!box.checked;message.textContent=typeof d.detail==='string'?d.detail:'Not saved.';return}
          message.textContent=d.message||'Saved.';
        });
        customerSelect.addEventListener('change',loadCameras);
        loadCustomers();
        </script>
        """
        return page_shell("Analytics entitlements", "analytics-entitlements", content, scripts)

    @app.get("/api/admin/analytics-entitlements")
    def admin_entitlements_state(request: Request, customer_id: str = ""):
        _require_master_admin(request)
        from customer_analytics_panel import analytics_row_state, camera_entitlement_rows
        with connection() as db:
            if not customer_id:
                return {"customers": [dict(item) for item in db.execute("SELECT id, name FROM customers ORDER BY name").fetchall()]}
            if not db.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                raise HTTPException(status_code=404, detail="Customer not found.")
            cameras = []
            for camera in db.execute(
                "SELECT c.id, c.name, c.camera_number, s.name AS site_name FROM cameras c LEFT JOIN sites s ON s.id=c.site_id "
                "WHERE c.customer_id=? ORDER BY s.name, c.camera_number", (customer_id,)
            ).fetchall():
                cameras.append({
                    "id": camera["id"],
                    "name": (camera["name"] or "").strip() or f"Camera {camera['camera_number']}",
                    "site": camera["site_name"],
                    "analytics": analytics_row_state(camera_entitlement_rows(db, camera["id"])),
                })
        return {"customer_id": customer_id, "cameras": cameras}

    @app.put("/api/admin/analytics-entitlements")
    def update_entitlements(payload: AdminEntitlementChange, request: Request):
        _require_master_admin(request)
        from customer_analytics_panel import ANALYTIC_LABELS, LicenseLimitExceeded, assign_entitlement, remove_entitlement
        if payload.analytic_key not in ANALYTIC_LABELS:
            raise HTTPException(status_code=400, detail="Unknown analytic.")
        now = datetime.now().isoformat()
        with connection() as db:
            camera = db.execute("SELECT id, customer_id FROM cameras WHERE id=?", (payload.camera_id,)).fetchone()
            if not camera:
                raise HTTPException(status_code=404, detail="Camera not found.")
            try:
                if payload.enabled:
                    assign_entitlement(db, payload.camera_id, payload.analytic_key, now=now)
                else:
                    remove_entitlement(db, payload.camera_id, payload.analytic_key, now=now)
            except LicenseLimitExceeded as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        label = ANALYTIC_LABELS[payload.analytic_key][0]
        record_audit(request, "update", f"camera-analytics:{payload.camera_id}:{payload.analytic_key}",
                     f"{'Enabled' if payload.enabled else 'Disabled'} {label} for camera {payload.camera_id} (customer {camera['customer_id']}).")
        return {"status": "complete", "camera_id": payload.camera_id, "analytic_key": payload.analytic_key, "enabled": payload.enabled,
                "message": f"{label} {'enabled' if payload.enabled else 'disabled'} for this camera."}
