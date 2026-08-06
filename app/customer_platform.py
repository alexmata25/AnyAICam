
import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

RECORDINGS_FOLDER = Path("/app/recordings")
FEATURES_FILE = RECORDINGS_FOLDER / "customer_camera_features.json"
ALERTS_FILE = RECORDINGS_FOLDER / "customer_camera_alerts.json"

ANALYTICS_CATALOG = {
    "smart_motion": {"label": "Smart Motion", "description": "People, vehicle, and animal event filtering."},
    "people_counting": {"label": "People Counting", "description": "Occupancy and foot-traffic reporting."},
    "lpr": {"label": "License Plate Recognition", "description": "Plate capture and searchable vehicle events."},
    "ppe_detection": {"label": "PPE Detection", "description": "Hard-hat, vest, and safety-equipment monitoring."},
}


class CameraFeatureUpdate(BaseModel):
    analytics_enabled: dict[str, bool] = Field(default_factory=dict)


class CameraAlertUpdate(BaseModel):
    enabled: bool = True
    event_types: list[str] = Field(default_factory=lambda: ["motion", "person"])
    email_enabled: bool = True
    push_enabled: bool = True
    recipient_email: str = ""
    quiet_hours_enabled: bool = False
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"


class EntitlementUpdate(BaseModel):
    user_id: str
    camera_id: int = Field(ge=1, le=256)
    entitlements: list[str] = Field(default_factory=list)


def _load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return default


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def _features() -> dict:
    value = _load(FEATURES_FILE, {})
    return value if isinstance(value, dict) else {}


def _alerts() -> dict:
    value = _load(ALERTS_FILE, {})
    return value if isinstance(value, dict) else {}


def _camera_key(user_id: str, camera_id: int) -> str:
    return f"{user_id}:{camera_id}"


def _is_customer(user: dict) -> bool:
    from tenancy.policy import identity_domain
    return identity_domain(user) == "customer" and bool(user.get("tenant_id") or user.get("customer_id"))


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
    @app.get("/customer-app", response_class=HTMLResponse)
    def customer_portal(request: Request):
        user = current_user(request)
        if not _is_customer(user):
            raise HTTPException(status_code=403, detail="Customer account required.")

        cameras = user_camera_ids(user)
        feature_state = _features()
        enabled_total = 0
        entitled_total = 0
        cards = []

        for camera_id in cameras:
            state = feature_state.get(_camera_key(user["id"], camera_id), {})
            entitlements = set(state.get("entitlements") or [])
            enabled = {
                key for key, value in (state.get("analytics_enabled") or {}).items()
                if value and key in entitlements
            }
            enabled_total += len(enabled)
            entitled_total += len(entitlements)
            cards.append(
                f'''<article class="feature-card">
                  <div class="feature-icon">▣</div>
                  <h2>Camera {camera_id}</h2>
                  <p>{len(enabled)} analytics enabled · {len(entitlements)} paid entitlement(s)</p>
                  <a class="download" href="/camera/{camera_id}">Open camera</a>
                </article>'''
            )

        content = f'''
        <header class="topbar">
          <div><p class="eyebrow">Customer VMS</p><h1>Welcome, {user.get("display_name","Customer")}</h1></div>
          <a class="action-button" href="/mobile-app">Install mobile app</a>
        </header>
        <section class="launch-summary">
          <article class="launch-stat"><span>Authorized cameras</span><strong>{len(cameras)}</strong></article>
          <article class="launch-stat"><span>Paid analytics</span><strong>{entitled_total}</strong></article>
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
        user = current_user(request)
        if not _is_customer(user):
            raise HTTPException(status_code=403, detail="Customer account required.")

        cameras = user_camera_ids(user)
        camera_options = "".join(
            f'<option value="{camera}">Camera {camera}</option>' for camera in cameras
        )
        analytic_options = "".join(
            f'''<label class="feature-toggle" data-feature="{key}">
              <span><strong>{item["label"]}</strong><small>{item["description"]}</small></span>
              <input type="checkbox" id="feature-{key}">
              <em id="entitlement-{key}">Checking plan…</em>
            </label>'''
            for key, item in ANALYTICS_CATALOG.items()
        )
        content = f'''
        <header class="topbar">
          <div><p class="eyebrow">Mobile app settings</p><h1>Camera analytics and alerts</h1></div>
          <a class="ghost-button" href="/customer-portal">Back to customer portal</a>
        </header>
        <section class="panel">
          <div class="panel-head"><div><h2>Choose a camera</h2><div class="health-detail">Paid analytics are enabled individually per camera.</div></div></div>
          <label style="display:grid;gap:7px;max-width:360px">Camera<select id="camera-select">{camera_options}</select></label>
        </section>
        <div class="notification-grid" style="margin-top:16px">
          <section class="panel">
            <div class="panel-head"><div><h2>Paid analytics</h2><div class="health-detail">Locked features require an active entitlement assigned by ANY AI CAM.</div></div></div>
            <div class="customer-feature-grid">{analytic_options}</div>
            <button class="action-button" id="save-features" type="button">Save camera analytics</button>
          </section>
          <section class="panel">
            <div class="panel-head"><div><h2>Notifications</h2><div class="health-detail">Program alerts for this camera.</div></div></div>
            <form class="notification-form" id="alert-form">
              <label><span><input id="alerts-enabled" type="checkbox" checked> Enable alerts for this camera</span></label>
              <label>Recipient email<input id="recipient-email" type="email" value="{user.get("email","")}"></label>
              <label><span><input id="email-enabled" type="checkbox" checked> Email notifications</span></label>
              <label><span><input id="push-enabled" type="checkbox" checked> Mobile push notifications</span></label>
              <div class="event-check-grid">
                <label><input class="event-type" value="motion" type="checkbox" checked> Motion</label>
                <label><input class="event-type" value="person" type="checkbox" checked> Person</label>
                <label><input class="event-type" value="vehicle" type="checkbox"> Vehicle</label>
                <label><input class="event-type" value="offline" type="checkbox"> Camera offline</label>
              </div>
              <label><span><input id="quiet-enabled" type="checkbox"> Quiet hours</span></label>
              <label>Quiet hours start<input id="quiet-start" type="time" value="22:00"></label>
              <label>Quiet hours end<input id="quiet-end" type="time" value="07:00"></label>
              <button class="action-button" type="submit">Save alert program</button>
            </form>
            <div class="push-status" style="margin-top:14px">
              <strong>Push enrollment</strong>
              <p class="health-detail">Install the mobile app, allow browser notifications, then enroll this device.</p>
              <button class="compact-button" id="enable-push" type="button">Enable push on this device</button>
            </div>
          </section>
        </div>
        '''
        scripts = '''
        <script>
        const catalog=['smart_motion','people_counting','lpr','ppe_detection'];
        const cameraSelect=document.getElementById('camera-select');

        async function loadCameraSettings(){
          const cameraId=cameraSelect.value;
          const response=await fetch(`/api/customer/cameras/${cameraId}/app-settings`);
          const data=await response.json();
          const entitlements=new Set(data.features.entitlements||[]);
          const enabled=data.features.analytics_enabled||{};
          catalog.forEach(name=>{
            const input=document.getElementById(`feature-${name}`);
            const badge=document.getElementById(`entitlement-${name}`);
            const paid=entitlements.has(name);
            input.disabled=!paid;
            input.checked=Boolean(paid&&enabled[name]);
            badge.textContent=paid?'Included in plan':'Upgrade required';
            badge.className=paid?'active-badge':'pending-badge';
          });
          const a=data.alerts||{};
          document.getElementById('alerts-enabled').checked=a.enabled!==false;
          document.getElementById('recipient-email').value=a.recipient_email||'';
          document.getElementById('email-enabled').checked=a.email_enabled!==false;
          document.getElementById('push-enabled').checked=a.push_enabled!==false;
          document.getElementById('quiet-enabled').checked=Boolean(a.quiet_hours_enabled);
          document.getElementById('quiet-start').value=a.quiet_start||'22:00';
          document.getElementById('quiet-end').value=a.quiet_end||'07:00';
          document.querySelectorAll('.event-type').forEach(box=>{
            box.checked=(a.event_types||['motion','person']).includes(box.value);
          });
        }

        document.getElementById('save-features').onclick=async()=>{
          const analytics_enabled={};
          catalog.forEach(name=>analytics_enabled[name]=document.getElementById(`feature-${name}`).checked);
          const response=await fetch(`/api/customer/cameras/${cameraSelect.value}/features`,{
            method:'PUT',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({analytics_enabled})
          });
          const data=await response.json();
          showToast(data.message||'Analytics updated.');
          loadCameraSettings();
        };

        document.getElementById('alert-form').onsubmit=async event=>{
          event.preventDefault();
          const payload={
            enabled:document.getElementById('alerts-enabled').checked,
            recipient_email:document.getElementById('recipient-email').value,
            email_enabled:document.getElementById('email-enabled').checked,
            push_enabled:document.getElementById('push-enabled').checked,
            quiet_hours_enabled:document.getElementById('quiet-enabled').checked,
            quiet_start:document.getElementById('quiet-start').value,
            quiet_end:document.getElementById('quiet-end').value,
            event_types:[...document.querySelectorAll('.event-type:checked')].map(x=>x.value)
          };
          const response=await fetch(`/api/customer/cameras/${cameraSelect.value}/alerts`,{
            method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
          });
          const data=await response.json();
          showToast(data.message||'Alert program saved.');
        };

        document.getElementById('enable-push').onclick=async()=>{
          if(!('Notification' in window)){return showToast('Push notifications are not supported on this browser.');}
          const permission=await Notification.requestPermission();
          if(permission!=='granted')return showToast('Notification permission was not granted.');
          const response=await fetch('/api/mobile/push/enroll',{
            method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
              device_name:navigator.userAgent.includes('iPhone')?'iPhone':'Android or desktop browser',
              platform:navigator.platform||'web',
              endpoint:'browser-permission',
              keys:{}
            })
          });
          const data=await response.json();
          showToast(data.message||'This device is enrolled.');
        };

        cameraSelect.onchange=loadCameraSettings;
        loadCameraSettings();
        </script>
        '''
        return page_shell("Camera analytics and alerts", "customer-app-settings", content, scripts)

    @app.get("/api/customer/cameras/{camera_id}/app-settings")
    def customer_camera_settings(camera_id: int, request: Request):
        user = current_user(request)
        if not _is_customer(user):
            raise HTTPException(status_code=403, detail="Customer account required.")
        if camera_id not in user_camera_ids(user):
            raise HTTPException(status_code=403, detail="Camera is not assigned to this account.")
        key = _camera_key(user["id"], camera_id)
        return {
            "camera_id": camera_id,
            "features": _features().get(key, {"entitlements": [], "analytics_enabled": {}}),
            "alerts": _alerts().get(key, {
                "enabled": True,
                "event_types": ["motion", "person"],
                "email_enabled": True,
                "push_enabled": True,
                "recipient_email": user.get("email", ""),
                "quiet_hours_enabled": False,
                "quiet_start": "22:00",
                "quiet_end": "07:00",
            }),
        }

    @app.put("/api/customer/cameras/{camera_id}/features")
    def update_customer_camera_features(camera_id: int, payload: CameraFeatureUpdate, request: Request):
        user = current_user(request)
        from tenancy.policy import normalize_role
        if normalize_role(user) != ("customer", "customer_admin"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        if camera_id not in user_camera_ids(user):
            raise HTTPException(status_code=403, detail="Camera is not assigned to this account.")

        all_features = _features()
        key = _camera_key(user["id"], camera_id)
        state = all_features.get(key, {"entitlements": [], "analytics_enabled": {}})
        entitlements = set(state.get("entitlements") or [])
        state["analytics_enabled"] = {
            feature: bool(enabled and feature in entitlements)
            for feature, enabled in payload.analytics_enabled.items()
            if feature in ANALYTICS_CATALOG
        }
        state["updated_at"] = datetime.now().isoformat()
        all_features[key] = state
        _save(FEATURES_FILE, all_features)
        record_audit(request, "update", f"customer-camera:{camera_id}", "Customer updated per-camera paid analytics.")
        return {"status": "complete", "features": state, "message": "Camera analytics saved. Locked features were not enabled."}

    @app.put("/api/customer/cameras/{camera_id}/alerts")
    def update_customer_camera_alerts(camera_id: int, payload: CameraAlertUpdate, request: Request):
        user = current_user(request)
        from tenancy.policy import normalize_role
        if normalize_role(user) != ("customer", "customer_admin"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        if camera_id not in user_camera_ids(user):
            raise HTTPException(status_code=403, detail="Camera is not assigned to this account.")

        allowed_events = {"motion", "person", "vehicle", "offline"}
        state = payload.model_dump()
        state["event_types"] = [event for event in state["event_types"] if event in allowed_events]
        state["updated_at"] = datetime.now().isoformat()
        alerts = _alerts()
        alerts[_camera_key(user["id"], camera_id)] = state
        _save(ALERTS_FILE, alerts)
        record_audit(request, "update", f"customer-camera:{camera_id}", "Customer updated per-camera notification program.")
        return {"status": "complete", "alerts": state, "message": "Camera alert program saved."}

    @app.get("/analytics-entitlements", response_class=HTMLResponse)
    def analytics_entitlements_page(request: Request):
        user = current_user(request)
        if not is_master_admin(user):
            raise HTTPException(status_code=403, detail="Master administrator required.")
        content = '''
        <header class="topbar"><div><p class="eyebrow">Paid feature control</p><h1>Analytics entitlements</h1></div></header>
        <section class="panel">
          <div class="panel-head"><div><h2>Assign paid analytics per customer and camera</h2><div class="health-detail">Customers can only enable features that are granted here or by a future Stripe entitlement webhook.</div></div></div>
          <form class="notification-form" id="entitlement-form">
            <label>Customer user ID<input id="ent-user" required placeholder="User ID from Business users"></label>
            <label>Camera number<input id="ent-camera" type="number" min="1" value="1" required></label>
            <div class="event-check-grid">
              <label><input class="ent-feature" value="smart_motion" type="checkbox"> Smart Motion</label>
              <label><input class="ent-feature" value="people_counting" type="checkbox"> People Counting</label>
              <label><input class="ent-feature" value="lpr" type="checkbox"> LPR</label>
              <label><input class="ent-feature" value="ppe_detection" type="checkbox"> PPE Detection</label>
            </div>
            <button class="action-button" type="submit">Save paid entitlements</button>
          </form>
          <div id="entitlement-message" class="health-detail" style="margin-top:12px"></div>
        </section>
        '''
        scripts = '''
        <script>
        document.getElementById('entitlement-form').onsubmit=async event=>{
          event.preventDefault();
          const payload={
            user_id:document.getElementById('ent-user').value.trim(),
            camera_id:Number(document.getElementById('ent-camera').value),
            entitlements:[...document.querySelectorAll('.ent-feature:checked')].map(x=>x.value)
          };
          const response=await fetch('/api/admin/analytics-entitlements',{
            method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
          });
          const data=await response.json();
          document.getElementById('entitlement-message').textContent=data.message||data.detail||'Saved.';
          showToast(data.message||'Entitlements saved.');
        };
        </script>
        '''
        return page_shell("Analytics entitlements", "analytics-entitlements", content, scripts)

    @app.put("/api/admin/analytics-entitlements")
    def update_entitlements(payload: EntitlementUpdate, request: Request):
        user = current_user(request)
        if not is_master_admin(user):
            raise HTTPException(status_code=403, detail="Master administrator required.")
        allowed = [item for item in payload.entitlements if item in ANALYTICS_CATALOG]
        features = _features()
        key = _camera_key(payload.user_id, payload.camera_id)
        prior = features.get(key, {})
        enabled = prior.get("analytics_enabled") or {}
        features[key] = {
            "entitlements": allowed,
            "analytics_enabled": {name: bool(enabled.get(name) and name in allowed) for name in ANALYTICS_CATALOG},
            "updated_at": datetime.now().isoformat(),
            "updated_by": user.get("id"),
        }
        _save(FEATURES_FILE, features)
        record_audit(request, "update", f"analytics-entitlements:{key}", f"Assigned paid analytics: {', '.join(allowed) or 'none'}.")
        return {"status": "complete", "features": features[key], "message": "Paid analytics entitlements saved."}
