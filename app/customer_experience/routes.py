"""FastAPI routes for the tenant-scoped customer experience."""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from customer_experience.pages import dashboard_page, table_page
from customer_experience.service import CustomerExperienceService


def register_customer_experience_routes(
    app: FastAPI,
    *,
    page_shell: Callable,
    current_user: Callable,
) -> CustomerExperienceService:
    service = CustomerExperienceService()

    def identity(request: Request) -> dict:
        user = current_user(request)
        try:
            service.tenant_id(user)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return user

    def call(method: str, user: dict):
        try:
            return getattr(service, method)(user)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.get("/api/customer/dashboard")
    def customer_dashboard_api(request: Request) -> dict:
        return call("dashboard", identity(request))

    @app.get("/customer-portal", response_class=HTMLResponse)
    def customer_dashboard(request: Request) -> str:
        user = identity(request)
        model = call("dashboard", user)
        return page_shell("Customer dashboard", "dashboard", dashboard_page(model, user.get("display_name") or user.get("name")))

    @app.get("/customer-admin/users", response_class=HTMLResponse)
    def customer_users(request: Request) -> str:
        rows = call("users", identity(request))
        content = table_page("Users", "Accounts in this customer organization.", ["Name", "Email", "Role", "Status"], [[row.get("name"), row.get("email"), row.get("customer_role"), row.get("account_status") or ("active" if row.get("approved") else "pending")] for row in rows])
        return page_shell("Customer users", "customer-users", content)

    @app.get("/customer-admin/sites", response_class=HTMLResponse)
    def customer_sites(request: Request) -> str:
        rows = call("sites", identity(request))
        content = table_page("Sites", "Locations owned by this customer tenant.", ["Site", "Address", "Type", "Cameras", "Appliances"], [[row.get("name"), row.get("address"), row.get("site_type"), row.get("camera_count"), row.get("appliance_count")] for row in rows])
        return page_shell("Customer sites", "customer-sites", content)

    @app.get("/customer-admin/cameras", response_class=HTMLResponse)
    def customer_cameras(request: Request) -> str:
        rows = call("cameras", identity(request))
        content = table_page("Cameras", "Cameras assigned to this tenant and its Edge appliances.", ["Camera", "Status", "Resolution", "Site", "Edge appliance"], [[row.get("name"), row.get("status"), row.get("resolution"), row.get("site_name"), row.get("appliance_cloud_id")] for row in rows], '<a class="action-button" href="/live">Open live view</a>')
        return page_shell("Customer cameras", "customer-cameras", content)

    @app.get("/customer-admin/permissions", response_class=HTMLResponse)
    def customer_permissions(request: Request) -> str:
        rows = call("permissions", identity(request))
        formatted = [[row.get("user_name") or row.get("email"), row.get("customer_role"), row.get("camera_name"), "Yes" if row.get("can_view_live") else "No", "Yes" if row.get("can_playback") else "No", "Yes" if row.get("can_manage") else "No"] for row in rows]
        action = '<a class="action-button" href="/tenant/camera-sharing">Assign camera access</a>'
        content = table_page("Permissions", "Explicit camera access granted within this tenant.", ["User", "Role", "Camera", "Live", "Playback", "Manage"], formatted, action)
        return page_shell("Customer permissions", "customer-permissions", content)

    return service
