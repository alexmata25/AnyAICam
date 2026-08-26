"""Notifications settings page (v1) -- Email + SMS channels, reachable
at /settings/notifications for a customer_owner/customer_viewer
identity. Registered on `app` before the generic /settings/{settings_slug}
catch-all in main.py so this specific path is never shadowed by it --
see admin_partner_bridge.py's own commit history this session for why
route registration order matters in this codebase (Starlette matches
first-registered-wins, not most-specific-wins).

Scoped to customer identities only (customer_owner/customer_viewer):
notification preferences are inherently "alert *me*, personally, about
*my* cameras" -- a concept that maps cleanly onto a customer_id-scoped
identity but not onto partner-side staff (administrator/partner_owner/
salesperson/technician), who have no single customer_id or personal
camera fleet to scope alerts to. A legacy Admin Portal-only visitor (no
partner_identity() at all) sees the same honest "sign in to the Partner
Portal" explanation /operations/rdm already established, including
benefiting automatically from an existing admin_partner_bridge link if
one exists for them.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from camera_access import authorized_camera_ids
from notification_preferences import (
    EVENT_TYPES,
    NotAuthorizedCameraError,
    get_preferences,
    mark_email_verified,
    mark_phone_verified,
    save_preferences,
)
from partner_db import connection
from partner_portal import partner_identity

CUSTOMER_ROLES = {"customer_owner", "customer_viewer"}


def _camera_context(db, identity: dict) -> tuple[str, set[str]]:
    """(user_id, authorized_camera_ids) for this identity -- customer_owner
    gets every camera on the account; customer_viewer only the ones
    explicitly granted, via the exact same is_camera_authorized()/
    authorized_camera_ids() decision Live/Playback/Investigate already
    use in this codebase. Raises HTTPException(403) for a customer_viewer
    with no matching partner_users row (mirrors _customer_playback_
    cameras()'s own established handling of that case)."""
    user = db.execute(
        "SELECT id,camera_access_mode FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?",
        (identity.get("email", ""), identity.get("customer_id")),
    ).fetchone()
    if identity.get("role") == "customer_owner":
        user_id = user["id"] if user else ""
        access_mode = "all"
    else:
        if not user:
            raise HTTPException(status_code=403, detail="Customer account not found.")
        user_id = user["id"]
        access_mode = user["camera_access_mode"] or "selected"
    cameras = authorized_camera_ids(
        db, user_id=user_id, customer_id=identity["customer_id"], role=identity["role"], access_mode=access_mode,
    )
    return user_id, cameras


def _resolve_customer_identity(request: Request) -> dict | None:
    identity = partner_identity(request)
    if identity and identity.get("role") in CUSTOMER_ROLES:
        return identity
    return None


def register_notification_settings_routes(app: FastAPI, shell: Callable) -> None:
    @app.get("/settings/notifications", response_class=HTMLResponse)
    def notifications_settings_page(request: Request) -> str:
        identity = _resolve_customer_identity(request)
        if not identity:
            content = (
                '<header class="topbar"><div><p class="eyebrow">Settings</p><h1>Notifications</h1></div>'
                '<a class="ghost-button" href="/settings">All settings</a></header>'
                '<section class="panel"><div class="panel-head"><h2>Customer Portal sign-in required</h2></div>'
                '<div class="empty">Notification preferences belong to one customer account. Sign in to the '
                'Customer Portal to manage them.</div>'
                '<a class="action-button" href="/customer-login.html" style="margin-top:12px;display:inline-block">'
                "Sign in to Customer Portal</a></section>"
            )
            return shell("Notifications", "settings", content)

        with connection() as db:
            _user_id, cameras = _camera_context(db, identity)
            camera_rows = db.execute(
                "SELECT id,name,camera_number FROM cameras WHERE customer_id=? ORDER BY camera_number",
                (identity["customer_id"],),
            ).fetchall()
        camera_options = "".join(
            f'<label class="notification-camera-option"><input type="checkbox" class="notif-camera" '
            f'value="{escape(camera["id"], quote=True)}"> '
            f'{escape(camera["name"] or "Camera " + str(camera["camera_number"]))}</label>'
            for camera in camera_rows if camera["id"] in cameras
        )
        event_type_options = "".join(
            f'<label class="notification-event-option"><input type="checkbox" class="notif-event" '
            f'value="{escape(key, quote=True)}"> {escape(label)}</label>'
            for key, label in EVENT_TYPES.items()
        )

        content = f'''<header class="topbar"><div><p class="eyebrow">Settings</p><h1>Notifications</h1></div>
        <a class="ghost-button" href="/settings">All settings</a></header>
        <section class="panel">
          <div class="panel-head"><h2>Email</h2></div>
          <label><input type="checkbox" id="notif-email-enabled"> Enable email notifications</label>
          <label>Notification email address<input id="notif-email-address" type="email" placeholder="you@example.com"></label>
          <div class="health-detail" id="notif-email-verified-status">Unverified</div>
          <button class="ghost-button" type="button" id="notif-test-email">Send test email</button>
          <div class="health-detail" id="notif-test-email-result"></div>
        </section>
        <section class="panel" style="margin-top:14px">
          <div class="panel-head"><h2>SMS / text message</h2></div>
          <label><input type="checkbox" id="notif-sms-enabled"> Enable SMS notifications</label>
          <label>Notification mobile number<input id="notif-phone-number" placeholder="+15551234567"></label>
          <div class="health-detail" id="notif-phone-verified-status">Unverified</div>
          <button class="ghost-button" type="button" id="notif-test-sms">Send test SMS</button>
          <div class="health-detail" id="notif-test-sms-result"></div>
        </section>
        <section class="panel" style="margin-top:14px">
          <div class="panel-head"><h2>Event types</h2></div>
          <div class="notification-event-grid">{event_type_options}</div>
        </section>
        <section class="panel" style="margin-top:14px">
          <div class="panel-head"><h2>Cameras</h2></div>
          <label><input type="radio" name="notif-camera-scope" id="notif-scope-all" value="all" checked> All cameras</label>
          <label><input type="radio" name="notif-camera-scope" id="notif-scope-selected" value="selected"> Selected cameras</label>
          <div class="notification-camera-grid" id="notif-camera-picker" hidden>{camera_options}</div>
        </section>
        <section class="panel" style="margin-top:14px">
          <div class="panel-head"><h2>Quiet hours</h2></div>
          <label><input type="checkbox" id="notif-quiet-enabled"> Enable quiet hours</label>
          <label>From<input id="notif-quiet-start" type="time" value="22:00"></label>
          <label>To<input id="notif-quiet-end" type="time" value="07:00"></label>
        </section>
        <section class="panel" style="margin-top:14px">
          <div class="panel-head"><h2>Delivery</h2></div>
          <label><input type="radio" name="notif-delivery-mode" id="notif-mode-immediate" value="immediate" checked> Immediate</label>
          <label><input type="radio" name="notif-delivery-mode" id="notif-mode-summary" value="summary"> Daily summary</label>
        </section>
        <div class="notification-actions" style="margin-top:14px"><button class="action-button" id="notif-save">Save notification settings</button></div>'''

        scripts = '''<script>
        const scopeAll=document.getElementById('notif-scope-all'),scopeSelected=document.getElementById('notif-scope-selected'),picker=document.getElementById('notif-camera-picker');
        function syncScope(){picker.hidden=!scopeSelected.checked}
        scopeAll.addEventListener('change',syncScope);scopeSelected.addEventListener('change',syncScope);
        async function loadPreferences(){
          const response=await fetch('/api/customer/notifications/preferences'),data=await response.json();
          document.getElementById('notif-email-enabled').checked=data.email_enabled;
          document.getElementById('notif-email-address').value=data.email_address;
          document.getElementById('notif-email-verified-status').textContent=data.email_verified_at?'Verified':'Unverified';
          document.getElementById('notif-sms-enabled').checked=data.sms_enabled;
          document.getElementById('notif-phone-number').value=data.phone_number;
          document.getElementById('notif-phone-verified-status').textContent=data.phone_verified_at?'Verified':'Unverified';
          document.querySelectorAll('.notif-event').forEach(box=>box.checked=data.event_types.includes(box.value));
          (data.camera_scope==='selected'?scopeSelected:scopeAll).checked=true;syncScope();
          document.querySelectorAll('.notif-camera').forEach(box=>box.checked=data.camera_ids.includes(box.value));
          document.getElementById('notif-quiet-enabled').checked=data.quiet_hours_enabled;
          document.getElementById('notif-quiet-start').value=data.quiet_start;
          document.getElementById('notif-quiet-end').value=data.quiet_end;
          (data.delivery_mode==='summary'?document.getElementById('notif-mode-summary'):document.getElementById('notif-mode-immediate')).checked=true;
          if(!data.email_provider_available)document.getElementById('notif-test-email').title='Email delivery is not configured yet.';
          if(!data.sms_provider_available)document.getElementById('notif-test-sms').title='SMS delivery is not configured yet.';
        }
        function payload(){
          return{
            email_enabled:document.getElementById('notif-email-enabled').checked,
            email_address:document.getElementById('notif-email-address').value,
            sms_enabled:document.getElementById('notif-sms-enabled').checked,
            phone_number:document.getElementById('notif-phone-number').value,
            event_types:[...document.querySelectorAll('.notif-event:checked')].map(box=>box.value),
            camera_scope:scopeSelected.checked?'selected':'all',
            camera_ids:[...document.querySelectorAll('.notif-camera:checked')].map(box=>box.value),
            quiet_hours_enabled:document.getElementById('notif-quiet-enabled').checked,
            quiet_start:document.getElementById('notif-quiet-start').value,
            quiet_end:document.getElementById('notif-quiet-end').value,
            delivery_mode:document.getElementById('notif-mode-summary').checked?'summary':'immediate',
          }
        }
        document.getElementById('notif-save').addEventListener('click',async()=>{
          const response=await fetch('/api/customer/notifications/preferences',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())}),r=await response.json();
          showToast(response.ok?'Notification settings saved.':(r.detail||'Could not save notification settings.'));
          if(response.ok)loadPreferences();
        });
        document.getElementById('notif-test-email').addEventListener('click',async()=>{
          const response=await fetch('/api/customer/notifications/test-email',{method:'POST'}),r=await response.json();
          document.getElementById('notif-test-email-result').textContent=r.message;if(response.ok)loadPreferences();
        });
        document.getElementById('notif-test-sms').addEventListener('click',async()=>{
          const response=await fetch('/api/customer/notifications/test-sms',{method:'POST'}),r=await response.json();
          document.getElementById('notif-test-sms-result').textContent=r.message;if(response.ok)loadPreferences();
        });
        loadPreferences();
        </script>'''
        return shell("Notifications", "settings", content, scripts)

    @app.get("/api/customer/notifications/preferences")
    def get_notification_preferences(request: Request) -> dict:
        identity = _resolve_customer_identity(request)
        if not identity:
            raise HTTPException(status_code=403, detail="Customer Portal sign-in required.")
        from cloud_config import settings as cloud_settings
        with connection() as db:
            user_id, _cameras = _camera_context(db, identity)
            preferences = get_preferences(db, user_id=user_id)
        preferences["email_provider_available"] = cloud_settings.email_backend == "smtp"
        preferences["sms_provider_available"] = cloud_settings.sms_backend == "twilio" and bool(
            cloud_settings.twilio_account_sid and cloud_settings.twilio_auth_token and cloud_settings.twilio_from_number
        )
        return preferences

    @app.put("/api/customer/notifications/preferences")
    def put_notification_preferences(request: Request, payload: dict) -> dict:
        identity = _resolve_customer_identity(request)
        if not identity:
            raise HTTPException(status_code=403, detail="Customer Portal sign-in required.")
        with connection() as db:
            user_id, cameras = _camera_context(db, identity)
            if not user_id:
                raise HTTPException(status_code=403, detail="Customer account not found.")
            try:
                saved = save_preferences(
                    db,
                    user_id=user_id,
                    customer_id=identity["customer_id"],
                    authorized_camera_ids=cameras,
                    now=datetime.now().isoformat(),
                    email_address=payload.get("email_address", ""),
                    email_enabled=bool(payload.get("email_enabled")),
                    phone_number=payload.get("phone_number", ""),
                    sms_enabled=bool(payload.get("sms_enabled")),
                    event_types=payload.get("event_types") or [],
                    camera_scope=payload.get("camera_scope", "all"),
                    camera_ids=payload.get("camera_ids") or [],
                    quiet_hours_enabled=bool(payload.get("quiet_hours_enabled")),
                    quiet_start=payload.get("quiet_start", "22:00"),
                    quiet_end=payload.get("quiet_end", "07:00"),
                    delivery_mode=payload.get("delivery_mode", "immediate"),
                )
            except NotAuthorizedCameraError as error:
                raise HTTPException(status_code=403, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        return {"status": "complete", "preferences": saved, "message": "Notification settings saved."}

    @app.post("/api/customer/notifications/test-email")
    def send_test_email(request: Request) -> dict:
        identity = _resolve_customer_identity(request)
        if not identity:
            raise HTTPException(status_code=403, detail="Customer Portal sign-in required.")
        from email_service import get_email_service
        with connection() as db:
            user_id, _cameras = _camera_context(db, identity)
            preferences = get_preferences(db, user_id=user_id)
            if not preferences["email_address"]:
                raise HTTPException(status_code=400, detail="Save a notification email address first.")
            result = get_email_service().send(
                "notification_test", preferences["email_address"], "AnyAiCam test notification",
                "This is a test notification from your AnyAiCam account. If you received this, email notifications are working.",
            )
            status = result.get("status", "preview")
            if status == "sent":
                mark_email_verified(db, user_id=user_id, now=datetime.now().isoformat())
        message = {
            "sent": "Test email sent.",
            "preview": "No email provider is configured yet -- this test was written to the local preview log, not delivered.",
        }.get(status, "Test email could not be delivered.")
        return {"status": status, "message": message}

    @app.post("/api/customer/notifications/test-sms")
    def send_test_sms(request: Request) -> dict:
        identity = _resolve_customer_identity(request)
        if not identity:
            raise HTTPException(status_code=403, detail="Customer Portal sign-in required.")
        from sms_service import get_sms_service
        with connection() as db:
            user_id, _cameras = _camera_context(db, identity)
            preferences = get_preferences(db, user_id=user_id)
            if not preferences["phone_number"]:
                raise HTTPException(status_code=400, detail="Save a notification mobile number first.")
            result = get_sms_service().send(
                "notification_test", preferences["phone_number"],
                "AnyAiCam test notification: SMS alerts are working for your account.",
            )
            status = result.get("status", "unavailable")
            if status == "sent":
                mark_phone_verified(db, user_id=user_id, now=datetime.now().isoformat())
        message = {
            "sent": "Test SMS sent.",
            "preview": "No SMS provider is configured yet -- this test was written to the local preview log, not delivered.",
            "unavailable": "SMS delivery is not configured yet.",
            "failed": result.get("detail") or "Test SMS could not be delivered.",
        }.get(status, "Test SMS could not be delivered.")
        return {"status": status, "message": message}
