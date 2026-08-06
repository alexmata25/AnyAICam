"""Authorization policy with domain and ownership checks before roles."""

from __future__ import annotations

import hmac
from dataclasses import dataclass


PLATFORM_ROLES = {"owner", "sales", "support", "installer", "billing", "operations"}
CUSTOMER_ROLES = {"customer_admin", "manager", "viewer", "guard"}

ROLE_ALIASES = {
    "administrator": ("platform", "owner"),
    "admin": ("platform", "owner"),
    "support_admin": ("platform", "support"),
    "partner_owner": ("platform", "owner"),
    "partner_admin": ("platform", "operations"),
    "partner_sales": ("platform", "sales"),
    "salesperson": ("platform", "sales"),
    "technician": ("platform", "installer"),
    "customer_owner": ("customer", "customer_admin"),
    "customer_viewer": ("customer", "viewer"),
    "operator": ("customer", "manager"),
    "read-only": ("customer", "viewer"),
    "view-only": ("customer", "viewer"),
    "live-only": ("customer", "guard"),
}

PLATFORM_PERMISSIONS = {
    "owner": {"*", "customer.camera_data.access"},
    "sales": {"tenant.create", "tenant.view", "crm.manage", "quote.manage"},
    "support": {"tenant.view", "support.manage", "camera.health.view"},
    "installer": {"tenant.view", "appliance.assign", "camera.configure", "camera.preview"},
    "billing": {"tenant.view", "subscription.manage", "license.manage"},
    "operations": {"tenant.view", "appliance.manage", "camera.health.view", "update.manage"},
}

CUSTOMER_PERMISSIONS = {
    "customer_admin": {
        "tenant.view", "tenant.manage", "user.invite", "user.manage",
        "camera.view", "camera.playback", "camera.download", "camera.configure",
        "camera.share", "event.view", "subscription.view", "appliance.view",
    },
    "manager": {
        "tenant.view", "camera.view", "camera.playback", "camera.configure",
        "event.view", "user.view", "appliance.view",
    },
    "viewer": {"tenant.view", "camera.view", "camera.playback", "event.view"},
    "guard": {"tenant.view", "camera.view", "event.view"},
}

CAMERA_PERMISSION_FLAGS = {
    "camera.view": "can_view_live",
    "camera.playback": "can_playback",
    "camera.download": "can_download",
    "camera.configure": "can_manage",
}


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str


def normalize_role(identity: dict) -> tuple[str, str]:
    raw_domain = str(identity.get("identity_domain") or "").strip().lower()
    raw_role = str(
        identity.get("platform_role")
        or identity.get("customer_role")
        or identity.get("tenant_role")
        or identity.get("role")
        or ""
    ).strip().lower()
    if raw_domain in {"platform", "customer"}:
        if raw_domain == "platform" and raw_role in PLATFORM_ROLES:
            return raw_domain, raw_role
        if raw_domain == "customer" and raw_role in CUSTOMER_ROLES:
            return raw_domain, raw_role
        legacy = ROLE_ALIASES.get(raw_role)
        if legacy and legacy[0] == raw_domain:
            return legacy
        return raw_domain, "invalid"
    return ROLE_ALIASES.get(raw_role, ("customer", "viewer"))


def identity_domain(identity: dict) -> str:
    return normalize_role(identity)[0]


def _same_tenant(identity_tenant: str | None, resource_tenant: str | None) -> bool:
    if not identity_tenant or not resource_tenant:
        return False
    return hmac.compare_digest(str(identity_tenant), str(resource_tenant))


def authorize(identity: dict, permission: str, resource_tenant_id: str | None = None) -> AuthorizationDecision:
    """Evaluate identity domain and tenant ownership before role permission."""
    if not identity or not identity.get("enabled", True):
        return AuthorizationDecision(False, "identity is not active")
    domain, role = normalize_role(identity)
    if domain == "customer":
        tenant_id = identity.get("tenant_id") or identity.get("customer_id")
        if not tenant_id:
            return AuthorizationDecision(False, "customer identity has no tenant")
        if resource_tenant_id and not _same_tenant(str(tenant_id), resource_tenant_id):
            return AuthorizationDecision(False, "resource belongs to another tenant")
        permissions = CUSTOMER_PERMISSIONS.get(role, set())
    else:
        permissions = PLATFORM_PERMISSIONS.get(role, set())
        if permission.startswith(("camera.view", "camera.playback", "camera.download")):
            if "customer.camera_data.access" not in permissions:
                return AuthorizationDecision(False, "platform role has no customer camera-data permission")
    allowed = "*" in permissions or permission in permissions
    return AuthorizationDecision(allowed, "allowed" if allowed else "role permission denied")


def authorize_camera(
    identity: dict,
    permission: str,
    camera_tenant_id: str,
    share: dict | None = None,
) -> AuthorizationDecision:
    base = authorize(identity, permission, camera_tenant_id)
    if not base.allowed:
        return base
    domain, role = normalize_role(identity)
    if domain == "platform" or role == "customer_admin":
        return base
    flag = CAMERA_PERMISSION_FLAGS.get(permission)
    if flag and not bool((share or {}).get(flag)):
        return AuthorizationDecision(False, "camera has not been shared with this user")
    return base
