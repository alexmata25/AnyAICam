"""Transactional tenant onboarding and camera-sharing services."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timedelta

from database_backend import connect
from tenancy.policy import authorize, authorize_camera, normalize_role


def _text(value, label: str, minimum: int = 2, maximum: int = 160) -> str:
    cleaned = " ".join(str(value or "").split())
    if not minimum <= len(cleaned) <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum} characters.")
    return cleaned


def _email(value) -> str:
    cleaned = str(value or "").strip().lower()
    if len(cleaned) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned):
        raise ValueError("A valid primary administrator email is required.")
    return cleaned


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:48] or "customer"


class TenantOnboardingService:
    def __init__(self, connection_factory=connect, password_hasher=None):
        self.connection_factory = connection_factory
        self.password_hasher = password_hasher

    def _hash(self, value: str) -> str:
        if self.password_hasher is None:
            from partner_db import password_hash
            self.password_hasher = password_hash
        return self.password_hasher(value)

    def onboard(self, actor: dict, payload: dict) -> dict:
        decision = authorize(actor, "tenant.create")
        if not decision.allowed:
            raise PermissionError(decision.reason)
        domain, role = normalize_role(actor)
        if domain != "platform" or role not in {"owner", "sales"}:
            raise PermissionError("Only platform owners and sales users can create customers.")

        tenant_name = _text(payload.get("tenant_name"), "Company name", 2, 120)
        admin_name = _text(payload.get("admin_name"), "Administrator name", 2, 120)
        admin_email = _email(payload.get("admin_email"))
        site_name = _text(payload.get("site_name") or "Primary Site", "Site name", 2, 120)
        plan_code = _text(payload.get("plan_code") or "starter", "Plan", 2, 50).lower()
        if plan_code not in {"starter", "professional", "enterprise", "trial"}:
            raise ValueError("Unsupported subscription plan.")
        camera_limit = max(1, min(512, int(payload.get("camera_limit") or 4)))
        now = datetime.now().isoformat()
        tenant_id = str(uuid.uuid4())
        site_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        subscription_id = str(uuid.uuid4())
        license_id = str(uuid.uuid4())
        invitation_id = str(uuid.uuid4())
        appliance_id = str(payload.get("appliance_id") or uuid.uuid4())
        cloud_id = str(payload.get("cloud_id") or f"edge-{secrets.token_hex(6)}")[:120]
        temporary_password = secrets.token_urlsafe(16)
        base_slug = _slug(tenant_name)
        slug = f"{base_slug}-{tenant_id[:8]}"
        partner_id = str(actor.get("partner_id") or "anyaicam-primary")

        with self.connection_factory() as db:
            if db.execute("SELECT id FROM partner_users WHERE lower(email)=?", (admin_email,)).fetchone():
                raise ValueError("A user with this email already exists.")
            db.execute(
                "INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",
                (partner_id, "AnyAiCam", "approved", "real", now),
            )
            db.execute(
                "INSERT INTO tenants(id,slug,name,status,tenant_type,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                (tenant_id, slug, tenant_name, "active", "customer", now, actor.get("id") or actor.get("email")),
            )
            db.execute(
                """INSERT INTO customers(id,partner_id,name,company,email,status,source,created_at,created_by,tenant_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tenant_id, partner_id, tenant_name, tenant_name, admin_email, "active", "real", now, actor.get("id"), tenant_id),
            )
            db.execute(
                "INSERT INTO sites(id,customer_id,name,site_type,created_at,tenant_id) VALUES(?,?,?,?,?,?)",
                (site_id, tenant_id, site_name, "default", now, tenant_id),
            )
            db.execute(
                """INSERT INTO partner_users(
                    id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,
                    account_status,must_change_password,tenant_id,identity_domain,customer_role)
                   VALUES(?,?,?,?,?,?,1,?,?,'active',1,?,'customer','customer_admin')""",
                (user_id, partner_id, admin_email, admin_name, "customer_admin", self._hash(temporary_password), tenant_id, now, tenant_id),
            )
            db.execute(
                "INSERT INTO tenant_memberships(tenant_id,user_id,role,status,created_at,created_by) VALUES(?,?,?,?,?,?)",
                (tenant_id, user_id, "customer_admin", "active", now, actor.get("id")),
            )
            db.execute(
                """INSERT INTO tenant_subscriptions(
                    id,tenant_id,plan_code,status,camera_limit,starts_at,renews_at,created_at,created_by)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (subscription_id, tenant_id, plan_code, "trial" if plan_code == "trial" else "pending", camera_limit, now, (datetime.now() + timedelta(days=30)).isoformat(), now, actor.get("id")),
            )
            db.execute(
                "INSERT INTO tenant_licenses(id,tenant_id,subscription_id,status,camera_limit,created_at) VALUES(?,?,?,?,?,?)",
                (license_id, tenant_id, subscription_id, "pending", camera_limit, now),
            )
            existing_appliance = db.execute("SELECT id,tenant_id FROM appliances WHERE id=?", (appliance_id,)).fetchone()
            if existing_appliance:
                if existing_appliance["tenant_id"] and str(existing_appliance["tenant_id"]) != tenant_id:
                    raise ValueError("Appliance is already assigned to another tenant.")
                db.execute(
                    "UPDATE appliances SET customer_id=?,site_id=?,tenant_id=? WHERE id=?",
                    (tenant_id, site_id, tenant_id, appliance_id),
                )
            else:
                db.execute(
                    """INSERT INTO appliances(
                        id,customer_id,site_id,cloud_id,appliance_type,online_status,camera_capacity,
                        created_at,tenant_id,activation_status,state)
                       VALUES(?,?,?,?,?,'offline',?,?,?,'pending','offline')""",
                    (appliance_id, tenant_id, site_id, cloud_id, "AnyAiCam Edge", camera_limit, now, tenant_id),
                )
            expires_at = (datetime.now() + timedelta(days=7)).isoformat()
            db.execute(
                """INSERT INTO invitations(
                    id,email,role,customer_id,status,temporary_password_hash,email_preview,
                    expires_at,created_at,created_by,tenant_id)
                   VALUES(?,?,?,?,'pending',?,?,?,?,?,?)""",
                (invitation_id, admin_email, "customer_admin", tenant_id, self._hash(temporary_password), "Primary administrator invitation", expires_at, now, actor.get("id"), tenant_id),
            )

        return {
            "tenant": {"id": tenant_id, "name": tenant_name, "slug": slug},
            "primary_administrator": {"id": user_id, "email": admin_email, "role": "customer_admin"},
            "default_site": {"id": site_id, "name": site_name},
            "subscription": {"id": subscription_id, "plan_code": plan_code, "status": "trial" if plan_code == "trial" else "pending"},
            "license": {"id": license_id, "status": "pending", "camera_limit": camera_limit},
            "appliance": {"id": appliance_id, "cloud_id": cloud_id, "status": "pending"},
            "invitation": {"id": invitation_id, "expires_at": expires_at, "temporary_password": temporary_password},
        }

    def grant_camera_access(self, actor: dict, tenant_id: str, user_id: str, camera_id: str, permissions: dict) -> dict:
        decision = authorize(actor, "camera.share", tenant_id)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        now = datetime.now().isoformat()
        with self.connection_factory() as db:
            user = db.execute("SELECT id,tenant_id FROM partner_users WHERE id=?", (user_id,)).fetchone()
            camera = db.execute("SELECT id,tenant_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if not user or str(user["tenant_id"] or "") != tenant_id:
                raise ValueError("User does not belong to this tenant.")
            if not camera or str(camera["tenant_id"] or "") != tenant_id:
                raise ValueError("Camera does not belong to this tenant.")
            values = (
                int(bool(permissions.get("can_view_live", True))),
                int(bool(permissions.get("can_playback", False))),
                int(bool(permissions.get("can_download", False))),
                int(bool(permissions.get("can_manage", False))),
            )
            db.execute(
                """INSERT INTO camera_user_access(
                    tenant_id,user_id,camera_id,can_view_live,can_playback,can_download,can_manage,granted_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,user_id,camera_id) DO UPDATE SET
                    can_view_live=excluded.can_view_live,can_playback=excluded.can_playback,
                    can_download=excluded.can_download,can_manage=excluded.can_manage,
                    granted_by=excluded.granted_by,updated_at=excluded.updated_at""",
                (tenant_id, user_id, camera_id, *values, actor.get("id"), now, now),
            )
        return {"tenant_id": tenant_id, "user_id": user_id, "camera_id": camera_id, **dict(zip(("can_view_live", "can_playback", "can_download", "can_manage"), map(bool, values)))}

    def camera_access(self, identity: dict, camera_id: str, permission: str) -> bool:
        with self.connection_factory() as db:
            camera = db.execute("SELECT tenant_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if not camera or not camera["tenant_id"]:
                return False
            share = db.execute(
                "SELECT * FROM camera_user_access WHERE tenant_id=? AND user_id=? AND camera_id=?",
                (camera["tenant_id"], identity.get("id"), camera_id),
            ).fetchone()
        return authorize_camera(identity, permission, str(camera["tenant_id"]), dict(share) if share else None).allowed

