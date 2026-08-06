"""Read models for the tenant-scoped customer dashboard and administration."""

from __future__ import annotations

from database_backend import connect
from tenancy.policy import authorize, normalize_role


class CustomerExperienceService:
    def __init__(self, connection_factory=connect):
        self.connection_factory = connection_factory

    @staticmethod
    def tenant_id(identity: dict) -> str:
        domain, _role = normalize_role(identity)
        tenant_id = str(identity.get("tenant_id") or identity.get("customer_id") or "")
        if domain != "customer" or not tenant_id:
            raise PermissionError("A tenant-scoped customer account is required.")
        return tenant_id

    def require(self, identity: dict, permission: str) -> str:
        tenant_id = self.tenant_id(identity)
        decision = authorize(identity, permission, tenant_id)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return tenant_id

    def dashboard(self, identity: dict) -> dict:
        tenant_id = self.require(identity, "tenant.view")
        with self.connection_factory() as db:
            tenant = db.execute(
                "SELECT id,name,status FROM tenants WHERE id=? AND tenant_type='customer'",
                (tenant_id,),
            ).fetchone()
            cameras = db.execute(
                """SELECT id,name,status,site_id,appliance_id,resolution
                   FROM cameras WHERE tenant_id=? ORDER BY name""",
                (tenant_id,),
            ).fetchall()
            appliances = db.execute(
                """SELECT id,cloud_id,state,online_status,last_check_in,cpu,memory,disk,
                          disk_capacity,recording_used,last_error
                   FROM appliances WHERE tenant_id=? ORDER BY cloud_id""",
                (tenant_id,),
            ).fetchall()
            events = db.execute(
                """SELECT id,event_type,event_timestamp,camera_id,payload_json
                   FROM ai_events WHERE tenant_id=? ORDER BY event_timestamp DESC LIMIT 8""",
                (tenant_id,),
            ).fetchall()
            alerts = db.execute(
                """SELECT id,title,severity,message,timestamp,camera_id,read_at
                   FROM notifications WHERE customer_id=? ORDER BY timestamp DESC LIMIT 8""",
                (tenant_id,),
            ).fetchall()
            storage = db.execute(
                """SELECT COUNT(*) AS asset_count,COALESCE(SUM(size_bytes),0) AS used_bytes
                   FROM recording_assets WHERE tenant_id=?""",
                (tenant_id,),
            ).fetchone()
            subscription = db.execute(
                """SELECT plan_code,status,camera_limit,renews_at
                   FROM tenant_subscriptions WHERE tenant_id=? ORDER BY created_at DESC LIMIT 1""",
                (tenant_id,),
            ).fetchone()
        camera_items = [dict(row) for row in cameras]
        live_states = {"online", "live", "streaming"}
        online = sum(str(item.get("status") or "").lower() in live_states for item in camera_items)
        return {
            "tenant": dict(tenant) if tenant else {"id": tenant_id, "name": "Customer", "status": "active"},
            "cameras": camera_items,
            "camera_counts": {"total": len(camera_items), "online": online, "offline": len(camera_items) - online},
            "appliances": [dict(row) for row in appliances],
            "events": [dict(row) for row in events],
            "alerts": [dict(row) for row in alerts],
            "storage": dict(storage) if storage else {"asset_count": 0, "used_bytes": 0},
            "subscription": dict(subscription) if subscription else None,
        }

    def users(self, identity: dict) -> list[dict]:
        tenant_id = self.require(identity, "user.manage")
        with self.connection_factory() as db:
            rows = db.execute(
                """SELECT id,name,email,customer_role,account_status,approved,created_at
                   FROM partner_users
                   WHERE tenant_id=? AND identity_domain='customer' ORDER BY name,email""",
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def sites(self, identity: dict) -> list[dict]:
        tenant_id = self.require(identity, "tenant.manage")
        with self.connection_factory() as db:
            rows = db.execute(
                """SELECT s.id,s.name,s.address,s.site_type,
                          COUNT(DISTINCT c.id) AS camera_count,
                          COUNT(DISTINCT a.id) AS appliance_count
                   FROM sites s
                   LEFT JOIN cameras c ON c.tenant_id=s.tenant_id AND c.site_id=s.id
                   LEFT JOIN appliances a ON a.tenant_id=s.tenant_id AND a.site_id=s.id
                   WHERE s.tenant_id=?
                   GROUP BY s.id,s.name,s.address,s.site_type ORDER BY s.name""",
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cameras(self, identity: dict) -> list[dict]:
        tenant_id = self.require(identity, "camera.configure")
        with self.connection_factory() as db:
            rows = db.execute(
                """SELECT c.id,c.name,c.status,c.resolution,s.name AS site_name,
                          a.cloud_id AS appliance_cloud_id
                   FROM cameras c
                   LEFT JOIN sites s ON s.id=c.site_id AND s.tenant_id=c.tenant_id
                   LEFT JOIN appliances a ON a.id=c.appliance_id AND a.tenant_id=c.tenant_id
                   WHERE c.tenant_id=? ORDER BY c.name""",
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def permissions(self, identity: dict) -> list[dict]:
        tenant_id = self.require(identity, "camera.share")
        with self.connection_factory() as db:
            rows = db.execute(
                """SELECT access.user_id,u.name AS user_name,u.email,u.customer_role,
                          access.camera_id,c.name AS camera_name,
                          access.can_view_live,access.can_playback,
                          access.can_download,access.can_manage
                   FROM camera_user_access access
                   JOIN partner_users u ON u.id=access.user_id AND u.tenant_id=access.tenant_id
                   JOIN cameras c ON c.id=access.camera_id AND c.tenant_id=access.tenant_id
                   WHERE access.tenant_id=? ORDER BY u.name,c.name""",
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]
