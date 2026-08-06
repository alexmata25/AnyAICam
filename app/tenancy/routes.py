"""FastAPI routes for tenant onboarding and camera sharing."""

from __future__ import annotations

from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from database_backend import connect
from tenancy.policy import CUSTOMER_PERMISSIONS, PLATFORM_PERMISSIONS, authorize, normalize_role
from tenancy.service import TenantOnboardingService


def register_tenant_routes(
    app: FastAPI,
    *,
    page_shell: Callable,
    current_user: Callable,
    record_audit: Callable,
) -> TenantOnboardingService:
    service = TenantOnboardingService()

    def require(request: Request, permission: str, tenant_id: str | None = None) -> dict:
        identity = current_user(request)
        decision = authorize(identity, permission, tenant_id)
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)
        return identity

    @app.get("/api/identity/roles")
    def identity_roles(request: Request) -> dict:
        require(request, "tenant.view")
        return {
            "platform": {role: sorted(values) for role, values in PLATFORM_PERMISSIONS.items()},
            "customer": {role: sorted(values) for role, values in CUSTOMER_PERMISSIONS.items()},
        }

    @app.get("/account/change-password", response_class=HTMLResponse)
    def change_temporary_password_page(request: Request) -> str:
        identity = current_user(request)
        if identity.get("identity_source") != "database" or not identity.get("must_change_password"):
            raise HTTPException(status_code=403, detail="A temporary-password session is required.")
        content = '''<header class="topbar"><div><p class="eyebrow">Account security</p><h1>Create your permanent password</h1></div></header><section class="panel" style="max-width:560px;margin:auto"><form id="tenant-password-change" class="rule-form"><label>New password<input name="password" type="password" minlength="12" autocomplete="new-password" required></label><button class="action-button" type="submit">Activate account</button></form></section>'''
        scripts = '''<script>document.getElementById('tenant-password-change').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,response=await fetch('/api/account/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(form)))}),result=await response.json();if(!response.ok)return showToast(result.detail||'Password could not be changed.');window.location.assign(result.destination)});</script>'''
        return page_shell("Create permanent password", "settings", content, scripts)

    @app.post("/api/account/change-password")
    def change_temporary_password(request: Request, payload: dict) -> dict:
        identity = current_user(request)
        if identity.get("identity_source") != "database" or not identity.get("must_change_password"):
            raise HTTPException(status_code=403, detail="A temporary-password session is required.")
        password = str(payload.get("password") or "")
        if len(password) < 12:
            raise HTTPException(status_code=400, detail="Use at least 12 characters.")
        from partner_db import password_hash
        with connect() as db:
            db.execute("UPDATE partner_users SET password_hash=?,must_change_password=0 WHERE id=?", (password_hash(password), identity["id"]))
            db.execute("UPDATE invitations SET status='accepted' WHERE lower(email)=? AND status='pending'", (str(identity.get("email") or "").lower(),))
        record_audit(request, "password.changed", f"user:{identity['id']}", "Temporary password replaced.")
        domain, _role = normalize_role(identity)
        return {"status": "complete", "destination": "/customer-portal" if domain == "customer" else "/admin-portal"}

    @app.get("/api/tenants")
    def list_tenants(request: Request) -> dict:
        identity = require(request, "tenant.view")
        domain, _role = normalize_role(identity)
        with connect() as db:
            if domain == "platform":
                rows = db.execute("SELECT id,slug,name,status,tenant_type,created_at FROM tenants WHERE tenant_type='customer' ORDER BY name").fetchall()
            else:
                tenant_id = str(identity.get("tenant_id") or identity.get("customer_id") or "")
                rows = db.execute("SELECT id,slug,name,status,tenant_type,created_at FROM tenants WHERE id=?", (tenant_id,)).fetchall()
        return {"tenants": [dict(row) for row in rows]}

    @app.get("/api/tenants/{tenant_id}")
    def tenant_summary(tenant_id: str, request: Request) -> dict:
        require(request, "tenant.view", tenant_id)
        with connect() as db:
            tenant = db.execute("SELECT id,slug,name,status,tenant_type,created_at FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found.")
            counts = {}
            for label, table in (("users", "partner_users"), ("sites", "sites"), ("cameras", "cameras"), ("appliances", "appliances")):
                counts[label] = db.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id=?", (tenant_id,)).fetchone()["count"]
            subscription = db.execute("SELECT id,plan_code,status,camera_limit FROM tenant_subscriptions WHERE tenant_id=? ORDER BY created_at DESC", (tenant_id,)).fetchone()
        return {"tenant": dict(tenant), "counts": counts, "subscription": dict(subscription) if subscription else None}

    @app.post("/api/tenants/onboard")
    def onboard_tenant(request: Request, payload: dict) -> dict:
        identity = current_user(request)
        try:
            result = service.onboard(identity, payload)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        invitation = result["invitation"]
        login_url = f"{str(request.base_url).rstrip('/')}/login"
        invitation_text = (
            f"Welcome to AnyAiCam.\n\n"
            f"Your organization: {result['tenant']['name']}\n"
            f"Login: {invitation['email']}\n"
            f"Temporary password: {invitation['temporary_password']}\n"
            f"Sign in: {login_url}\n\n"
            "You will be required to create a permanent password after signing in."
        )
        delivery_status = "failed"
        try:
            from email_service import get_email_service
            delivery = get_email_service().send(
                "invitation",
                invitation["email"],
                "Welcome to AnyAiCam",
                invitation_text,
                metadata={"tenant_id": result["tenant"]["id"], "invitation_id": invitation["id"]},
            )
            delivery_status = str(delivery.get("status") or "processed")
        except (OSError, RuntimeError, ValueError):
            delivery_status = "failed"
        with connect() as db:
            db.execute(
                "UPDATE invitations SET email_preview=? WHERE id=? AND tenant_id=?",
                (invitation_text, invitation["id"], result["tenant"]["id"]),
            )
        invitation["delivery_status"] = delivery_status
        record_audit(request, "tenant.created", f"tenant:{result['tenant']['id']}", "New customer tenant created.")
        return {"status": "complete", **result}

    @app.put("/api/tenants/{tenant_id}/users/{user_id}/cameras/{camera_id}")
    def share_camera(tenant_id: str, user_id: str, camera_id: str, request: Request, payload: dict) -> dict:
        identity = current_user(request)
        try:
            grant = service.grant_camera_access(identity, tenant_id, user_id, camera_id, payload)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        record_audit(request, "camera.share_updated", f"camera:{camera_id}", f"Camera access updated for user {user_id}.")
        return {"status": "complete", "grant": grant}

    @app.get("/api/tenants/{tenant_id}/users/{user_id}/cameras")
    def camera_sharing_state(tenant_id: str, user_id: str, request: Request) -> dict:
        require(request, "camera.share", tenant_id)
        with connect() as db:
            user = db.execute("SELECT id,name,customer_role FROM partner_users WHERE id=? AND tenant_id=?", (user_id, tenant_id)).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="Tenant user not found.")
            rows = db.execute(
                """SELECT c.id,c.name,a.can_view_live,a.can_playback,a.can_download,a.can_manage
                   FROM cameras c LEFT JOIN camera_user_access a
                     ON a.tenant_id=c.tenant_id AND a.camera_id=c.id AND a.user_id=?
                   WHERE c.tenant_id=? ORDER BY c.name""",
                (user_id, tenant_id),
            ).fetchall()
        return {"user": dict(user), "cameras": [dict(row) for row in rows]}

    @app.get("/admin/customers/new", response_class=HTMLResponse)
    def new_customer_wizard(request: Request) -> str:
        identity = require(request, "tenant.create")
        domain, role = normalize_role(identity)
        if domain != "platform" or role not in {"owner", "sales"}:
            raise HTTPException(status_code=403, detail="Platform owner or sales access is required.")
        from customer_experience.pages import onboarding_wizard_page
        with connect() as db:
            appliances = [dict(row) for row in db.execute(
                """SELECT id,cloud_id FROM appliances
                   WHERE tenant_id IS NULL OR tenant_id='' ORDER BY cloud_id"""
            ).fetchall()]
        content, scripts = onboarding_wizard_page(appliances)
        return page_shell("New Customer", "new-customer", content, scripts)

    @app.get("/tenant/camera-sharing", response_class=HTMLResponse)
    def camera_sharing_page(request: Request) -> str:
        identity = current_user(request)
        tenant_id = str(identity.get("tenant_id") or identity.get("customer_id") or "")
        decision = authorize(identity, "camera.share", tenant_id)
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)
        with connect() as db:
            users = db.execute(
                "SELECT id,name,email,customer_role FROM partner_users WHERE tenant_id=? AND identity_domain='customer' ORDER BY name",
                (tenant_id,),
            ).fetchall()
            cameras = db.execute("SELECT id,name FROM cameras WHERE tenant_id=? ORDER BY name", (tenant_id,)).fetchall()
        user_options = "".join(
            f'<option value="{escape(str(row["id"]), quote=True)}">{escape(str(row["name"] or row["email"] or "User"))} — {escape(str(row["customer_role"] or "viewer"))}</option>'
            for row in users if str(row["id"]) != str(identity.get("id"))
        )
        camera_options = "".join(
            f'<option value="{escape(str(row["id"]), quote=True)}">{escape(str(row["name"] or row["id"]))}</option>'
            for row in cameras
        )
        content = f'''<header class="topbar"><div><p class="eyebrow">Customer administration</p><h1>Camera sharing</h1></div><span class="pill">Tenant isolated</span></header><section class="panel"><div class="panel-head"><div><h2>Assign a camera</h2><p class="health-detail">Only users and cameras in your organization are available.</p></div></div><form id="tenant-camera-share" class="rule-form"><label>User<select name="user_id" required>{user_options}</select></label><label>Camera<select name="camera_id" required>{camera_options}</select></label><label><input name="can_view_live" type="checkbox" checked> Live view</label><label><input name="can_playback" type="checkbox"> Playback</label><label><input name="can_download" type="checkbox"> Download</label><label><input name="can_manage" type="checkbox"> Camera settings</label><button class="action-button" type="submit">Save camera access</button></form><div id="camera-share-result" class="health-detail" style="margin-top:12px"></div></section>'''
        scripts = f'''<script>document.getElementById('tenant-camera-share').addEventListener('submit',async event=>{{event.preventDefault();const form=event.currentTarget,data=new FormData(form),user=encodeURIComponent(data.get('user_id')),camera=encodeURIComponent(data.get('camera_id')),payload={{can_view_live:data.has('can_view_live'),can_playback:data.has('can_playback'),can_download:data.has('can_download'),can_manage:data.has('can_manage')}};const response=await fetch(`/api/tenants/{tenant_id}/users/${{user}}/cameras/${{camera}}`,{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}}),result=await response.json();if(!response.ok)return showToast(result.detail||'Camera access could not be saved.');document.getElementById('camera-share-result').textContent='Camera access saved.';showToast('Camera access saved.')}});</script>'''
        return page_shell("Camera sharing", "tenant-camera-sharing", content, scripts)

    return service
