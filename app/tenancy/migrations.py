"""Additive multi-tenant migration with legacy customer backfill."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from database_backend import backend, connect
from tenancy.policy import normalize_role


MIGRATION_VERSION = "20260806_phase6_multitenant_foundation"
PLATFORM_TENANT_ID = "00000000-0000-4000-8000-000000000001"
TENANT_TABLES = (
    "customers", "partner_users", "sites", "cameras", "appliances", "plans",
    "analytics_subscriptions", "invitations", "appliance_events", "user_sessions",
)


def _tables(db) -> set[str]:
    if backend() == "sqlite":
        return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return {
        row["table_name"]
        for row in db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()").fetchall()
    }


def _columns(db, table: str) -> set[str]:
    if backend() == "sqlite":
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    return {
        row["column_name"]
        for row in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?",
            (table,),
        ).fetchall()
    }


def _slug(value: str, suffix: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "customer"
    return f"{normalized[:45]}-{suffix[:8]}"


def _create_schema(db) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS tenants(
            id TEXT PRIMARY KEY,slug TEXT UNIQUE NOT NULL,name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',tenant_type TEXT NOT NULL DEFAULT 'customer',
            created_at TEXT NOT NULL,created_by TEXT)""",
        """CREATE TABLE IF NOT EXISTS tenant_memberships(
            tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL,created_by TEXT,
            PRIMARY KEY(tenant_id,user_id),FOREIGN KEY(tenant_id) REFERENCES tenants(id),
            FOREIGN KEY(user_id) REFERENCES partner_users(id))""",
        """CREATE TABLE IF NOT EXISTS tenant_subscriptions(
            id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,plan_code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',camera_limit INTEGER NOT NULL DEFAULT 0,
            starts_at TEXT,renews_at TEXT,created_at TEXT NOT NULL,created_by TEXT,
            FOREIGN KEY(tenant_id) REFERENCES tenants(id))""",
        """CREATE TABLE IF NOT EXISTS tenant_licenses(
            id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,subscription_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',camera_limit INTEGER NOT NULL DEFAULT 0,
            license_key_hash TEXT,activated_at TEXT,expires_at TEXT,created_at TEXT NOT NULL,
            FOREIGN KEY(tenant_id) REFERENCES tenants(id),
            FOREIGN KEY(subscription_id) REFERENCES tenant_subscriptions(id))""",
        """CREATE TABLE IF NOT EXISTS recording_assets(
            id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,site_id TEXT,camera_id TEXT,
            storage_key TEXT NOT NULL,started_at TEXT,ended_at TEXT,size_bytes INTEGER,
            status TEXT NOT NULL DEFAULT 'available',created_at TEXT NOT NULL,
            UNIQUE(tenant_id,storage_key),FOREIGN KEY(tenant_id) REFERENCES tenants(id))""",
        """CREATE TABLE IF NOT EXISTS ai_events(
            id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,site_id TEXT,camera_id TEXT,
            appliance_id TEXT,event_type TEXT NOT NULL,event_timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
            FOREIGN KEY(tenant_id) REFERENCES tenants(id))""",
        """CREATE TABLE IF NOT EXISTS camera_user_access(
            tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,camera_id TEXT NOT NULL,
            can_view_live INTEGER NOT NULL DEFAULT 1,can_playback INTEGER NOT NULL DEFAULT 0,
            can_download INTEGER NOT NULL DEFAULT 0,can_manage INTEGER NOT NULL DEFAULT 0,
            granted_by TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,user_id,camera_id),FOREIGN KEY(tenant_id) REFERENCES tenants(id),
            FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(camera_id) REFERENCES cameras(id))""",
    )
    for statement in statements:
        db.execute(statement)


def _add_columns(db) -> None:
    tables = _tables(db)
    for table in TENANT_TABLES:
        if table in tables and "tenant_id" not in _columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT")
    if "partner_users" in tables:
        columns = _columns(db, "partner_users")
        for name, definition in (
            ("identity_domain", "TEXT"),
            ("platform_role", "TEXT"),
            ("customer_role", "TEXT"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE partner_users ADD COLUMN {name} {definition}")


def _create_indexes(db) -> None:
    tables = _tables(db)
    for table in TENANT_TABLES:
        if table in tables and "tenant_id" in _columns(db, table):
            db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant_id ON {table}(tenant_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_camera_user_access_user ON camera_user_access(tenant_id,user_id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_memberships_user ON tenant_memberships(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_recording_assets_camera ON recording_assets(tenant_id,camera_id,started_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ai_events_camera ON ai_events(tenant_id,camera_id,event_timestamp)")


def _backfill(db) -> None:
    now = datetime.now().isoformat()
    db.execute(
        "INSERT OR IGNORE INTO tenants(id,slug,name,status,tenant_type,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
        (PLATFORM_TENANT_ID, "anyaicam-platform", "AnyAiCam Platform", "active", "platform", now, "migration"),
    )
    tables = _tables(db)
    customer_tenants: dict[str, str] = {}
    if "customers" in tables:
        for row in db.execute("SELECT id,name,tenant_id,created_at FROM customers").fetchall():
            customer_id = str(row["id"])
            tenant_id = str(row["tenant_id"] or uuid.uuid4())
            customer_tenants[customer_id] = tenant_id
            db.execute(
                "INSERT OR IGNORE INTO tenants(id,slug,name,status,tenant_type,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                (tenant_id, _slug(str(row["name"] or "Customer"), tenant_id), str(row["name"] or "Customer"), "active", "customer", row["created_at"] or now, "migration"),
            )
            db.execute("UPDATE customers SET tenant_id=? WHERE id=?", (tenant_id, customer_id))
            for table in ("sites", "cameras", "appliances", "plans", "analytics_subscriptions"):
                if table in tables and "tenant_id" in _columns(db, table):
                    db.execute(f"UPDATE {table} SET tenant_id=? WHERE customer_id=? AND (tenant_id IS NULL OR tenant_id='')", (tenant_id, customer_id))

    if "partner_users" in tables:
        users = db.execute("SELECT id,role,customer_id,tenant_id FROM partner_users").fetchall()
        for user in users:
            domain, role = normalize_role(dict(user))
            tenant_id = customer_tenants.get(str(user["customer_id"]), None) if user["customer_id"] else None
            db.execute(
                "UPDATE partner_users SET tenant_id=?,identity_domain=?,platform_role=?,customer_role=? WHERE id=?",
                (tenant_id, domain, role if domain == "platform" else None, role if domain == "customer" else None, user["id"]),
            )
            if tenant_id:
                db.execute(
                    "INSERT OR IGNORE INTO tenant_memberships(tenant_id,user_id,role,status,created_at,created_by) VALUES(?,?,?,?,?,?)",
                    (tenant_id, user["id"], role, "active", now, "migration"),
                )

    if "appliance_events" in tables and "tenant_id" in _columns(db, "appliance_events") and "appliances" in tables:
        for event in db.execute("SELECT appliance_id,event_id FROM appliance_events WHERE tenant_id IS NULL OR tenant_id='' ").fetchall():
            appliance = db.execute("SELECT tenant_id FROM appliances WHERE id=?", (event["appliance_id"],)).fetchone()
            if appliance and appliance["tenant_id"]:
                db.execute("UPDATE appliance_events SET tenant_id=? WHERE appliance_id=? AND event_id=?", (appliance["tenant_id"], event["appliance_id"], event["event_id"]))

    if "invitations" in tables and "tenant_id" in _columns(db, "invitations"):
        for customer_id, tenant_id in customer_tenants.items():
            db.execute(
                "UPDATE invitations SET tenant_id=? WHERE customer_id=? AND (tenant_id IS NULL OR tenant_id='')",
                (tenant_id, customer_id),
            )

    if "user_sessions" in tables and "tenant_id" in _columns(db, "user_sessions") and "partner_users" in tables:
        db.execute(
            """UPDATE user_sessions SET tenant_id=(
                   SELECT partner_users.tenant_id FROM partner_users WHERE partner_users.id=user_sessions.user_id)
               WHERE (tenant_id IS NULL OR tenant_id='') AND user_id IS NOT NULL"""
        )

    if "customer_camera_permissions" in tables and "cameras" in tables:
        query = """SELECT p.user_id,p.camera_id,p.can_live,p.can_playback,p.can_download,p.can_settings,c.tenant_id
                   FROM customer_camera_permissions p JOIN cameras c ON c.id=p.camera_id"""
        for grant in db.execute(query).fetchall():
            if not grant["tenant_id"]:
                continue
            db.execute(
                """INSERT OR IGNORE INTO camera_user_access(
                    tenant_id,user_id,camera_id,can_view_live,can_playback,can_download,can_manage,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (grant["tenant_id"], grant["user_id"], grant["camera_id"], grant["can_live"], grant["can_playback"], grant["can_download"], grant["can_settings"], now, now),
            )


def apply_tenant_migration() -> None:
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)")
        _create_schema(db)
        _add_columns(db)
        _backfill(db)
        _create_indexes(db)
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (MIGRATION_VERSION, datetime.now().isoformat()),
        )
