"""Test helpers for D-AUTHZ-1 residual / D-AUTHZ-2b / D-AUTHZ-3 (Linux 1.2.7.1).

partner_session_cookie() mints a REAL Partner Portal session: a user_sessions
row bound to the account's CURRENT authorization_version (and, for an
administrator, an MFA marker), plus the signed cookie that names it -- the same
state establish_partner_session() writes after a real sign-in. Tests use it in
place of a bare partner_portal._token(...) with no session_id, which 1.2.7.1
refuses for administrators (a session-less token cannot be revoked, re-versioned
or MFA-bound).

Columns that do not exist yet (a pre-1.2.7.1 database) are simply not written,
so a test that uses this helper fails on b8 for the behaviour it checks, not in
the helper.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from partner_db import connection, password_hash

DEFAULT_PARTNER = "anyaicam-primary"


def _session_columns(db) -> set:
    try:
        return {row["name"] for row in db.execute("PRAGMA table_info(user_sessions)").fetchall()}
    except Exception:
        rows = db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() "
                          "AND table_name='user_sessions'").fetchall()
        return {row["column_name"] for row in rows}


def make_partner_user(email: str, role: str, *, password: str = "correct-horse-battery-1", partner_id: str | None = DEFAULT_PARTNER,
                      customer_id: str | None = None, user_id: str | None = None, global_admin: bool = False,
                      partner_admin_grant: bool = False, approved: int = 1, account_status: str = "active") -> str:
    """A partner_users row (plus its partner, and optionally a live global or
    partner-scoped administrator grant). Returns the user id."""
    from appliance_identity import create_grant
    now = "2026-10-25T00:00:00"
    uid = user_id or ("u-" + email.replace("@", "-at-").replace(".", "-"))
    with connection() as db:
        if partner_id:
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",
                       (partner_id, "Partner " + partner_id, "approved", "real", now))
        if customer_id:
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
                       (customer_id, partner_id or DEFAULT_PARTNER, "Customer " + customer_id, customer_id + "@customers.test",
                        "active", "real", now))
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (uid, partner_id if role not in ("customer_owner", "customer_viewer") else None, email.lower(), email.split("@")[0],
                    role, password_hash(password), approved, customer_id, now, account_status))
        if global_admin:
            create_grant(db, user_id=uid, role="administrator", scope_type="global", scope_id=None, granted_by="test", now=now)
        if partner_admin_grant:
            create_grant(db, user_id=uid, role="administrator", scope_type="partner", scope_id=partner_id, granted_by="test", now=now)
    return uid


def current_version(email: str) -> int | None:
    with connection() as db:
        row = db.execute("SELECT authorization_version FROM partner_users WHERE lower(email)=?", (email.lower(),)).fetchone()
    return int(row["authorization_version"]) if row else None


def partner_session_cookie(email: str, role: str, *, partner_id=None, customer_id=None, mfa_state: str | None = "default",
                           auth_source: str = "password", version: int | None | str = "current", ttl_minutes: int = 120,
                           user_id: str | None = None) -> str:
    """A real session for an existing (or, for edge delegated sessions, absent)
    partner_users account; returns the signed cookie value.

    mfa_state "default": 'verified' for an administrator, NULL otherwise.
    version "current": the account's current authorization_version."""
    import partner_portal
    email = email.lower()
    if mfa_state == "default":
        mfa_state = "verified" if role == "administrator" else None
    session_id = secrets.token_hex(16)
    now = datetime.now()
    with connection() as db:
        user = db.execute("SELECT id,partner_id,customer_id,authorization_version FROM partner_users WHERE lower(email)=?", (email,)).fetchone()
        uid = user_id or (user["id"] if user else None)
        if version == "current":
            version = int(user["authorization_version"]) if user else None
        if partner_id is None and user:
            partner_id = user["partner_id"]
        if customer_id is None and user:
            customer_id = user["customer_id"]
        columns = _session_columns(db)
        values = {
            "id": session_id, "user_id": uid, "email": email, "role": role, "device_name": "Web browser", "session_type": "cookie",
            "created_at": now.isoformat(), "last_seen_at": now.isoformat(), "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
            "authorization_version_at_login": version, "auth_source": auth_source, "mfa_state": mfa_state,
            "mfa_verified_at": now.isoformat() if mfa_state == "verified" else None,
            "mfa_method": "totp" if mfa_state == "verified" else None,
        }
        values = {key: value for key, value in values.items() if key in columns}
        db.execute(f"INSERT INTO user_sessions({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
    return partner_portal._token(email, role, partner_id, customer_id, session_id, max(1, ttl_minutes // 60 or 1))


def cookies_for(token: str) -> dict:
    import partner_portal
    return {partner_portal.SESSION_COOKIE: token}


def enroll_confirmed_mfa(user_id: str) -> str:
    """A confirmed TOTP factor for this account; returns the secret."""
    import platform_owner
    secret = platform_owner.generate_totp_secret()
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute("INSERT INTO platform_owner_mfa(user_id,secret_base32,confirmed_at,created_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(user_id) DO UPDATE SET secret_base32=excluded.secret_base32,confirmed_at=excluded.confirmed_at",
                   (user_id, secret, now, now))
    return secret


def totp_now(secret: str) -> str:
    import time
    import platform_owner
    return platform_owner._totp_code_at(secret, int(time.time() // 30))


def set_cloud_profile(monkeypatch, cloud: bool = True) -> None:
    """Cloud (or edge) runtime profile for every source 1.2.7.1 reads."""
    import main
    role = "cloud" if cloud else "edge"
    monkeypatch.setattr(main, "RUNTIME_ROLE", role)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", role)
    try:
        import partner_portal
        from dataclasses import replace
        monkeypatch.setattr(partner_portal, "settings", replace(partner_portal.settings, runtime_role=role))
    except Exception:
        pass
    try:
        import admin_auth_policy
        admin_auth_policy.reset_cache_for_tests()
    except Exception:
        pass


def table_snapshot(exclude=("audit_logs", "user_sessions", "schema_migrations", "pending_mfa_logins", "login_attempts", "account_lockouts")) -> dict:
    """Every row of every table (except the excluded bookkeeping ones), for
    'no side effect' assertions."""
    snapshot = {}
    with connection() as db:
        tables = [row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            if table in exclude or table.startswith("sqlite_"):
                continue
            snapshot[table] = sorted(repr(tuple(row)) for row in db.execute(f'SELECT * FROM "{table}"').fetchall())
    return snapshot


def audit_actions(since_id: int = 0) -> list[tuple]:
    with connection() as db:
        return [(row["id"], row["action"], row["details_json"], row["actor_email"])
                for row in db.execute("SELECT id,action,details_json,actor_email FROM audit_logs WHERE id>? ORDER BY id", (since_id,)).fetchall()]


def max_audit_id() -> int:
    with connection() as db:
        row = db.execute("SELECT COALESCE(MAX(id),0) AS n FROM audit_logs").fetchone()
    return int(row["n"])


_ISOLATED_FILES = (
    ("USERS_FILE", "users.json"), ("SESSIONS_FILE", "sessions.json"),
    ("SUBSCRIPTION_REQUESTS_FILE", "subscription_requests.json"),
    ("BILLING_SUPPORT_TICKETS_FILE", "billing_support_tickets.json"),
    ("BILLING_ACCOUNTS_FILE", "billing_accounts.json"), ("BILLING_INVOICES_FILE", "billing_invoices.json"),
    ("MOBILE_DEVICES_FILE", "mobile_devices.json"),
)


from contextlib import contextmanager  # noqa: E402


@contextmanager
def isolated_app(tmp_path, monkeypatch, *, cloud: bool, db_name: str = "authz.db"):
    """main.app on its own SQLite database and its own users/sessions/billing
    files, in the cloud or edge profile, with no appliance identity."""
    import main
    from database_backend import override_target
    from fastapi.testclient import TestClient
    from partner_db import initialize_database

    for attribute, filename in _ISOLATED_FILES:
        monkeypatch.setattr(main, attribute, tmp_path / filename)
    for variable in ("ANYAICAM_APPLIANCE_ID", "ANYAICAM_APPLIANCE_CLOUD_ID", "ANYAICAM_APPLIANCE_CREDENTIAL",
                     "ANYAICAM_ADMIN_PASSWORD", "ANYAICAM_ADMIN_EMAIL", "ANYAICAM_PLATFORM_OWNER_EMAIL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(main, "own_appliance_identity", lambda: None)
    set_cloud_profile(monkeypatch, cloud)
    with override_target(sqlite_path=str(tmp_path / db_name)):
        initialize_database()
        yield TestClient(main.app, base_url="https://portal.test", follow_redirects=False)


def partner_cookie_from(response) -> str | None:
    """The Partner Portal session cookie a response set, if any."""
    import partner_portal
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name.strip() == partner_portal.SESSION_COOKIE:
            value = rest.split(";", 1)[0].strip().strip('"')
            if value:
                return value
    return None


def legacy_cookie_from(response) -> str | None:
    import main
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name.strip() == main.SESSION_COOKIE_NAME:
            value = rest.split(";", 1)[0].strip().strip('"')
            if value:
                return value
    return None


def session_row(token: str) -> dict | None:
    """The user_sessions row a signed partner cookie names."""
    import json
    from base64 import urlsafe_b64decode
    encoded = token.rsplit(".", 1)[0]
    payload = json.loads(urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if not payload.get("session_id"):
        return None
    with connection() as db:
        row = db.execute("SELECT * FROM user_sessions WHERE id=?", (payload["session_id"],)).fetchone()
    return dict(row) if row else None


ADMIN_NAMESPACE_PREFIXES = ("/api/admin/", "/api/operations/", "/operations/", "/admin", "/api/platform-owner/",
                            "/analytics-entitlements", "/billing-operations", "/license-management", "/subscription-admin")
# Reached mid-login, before any session exists (their own checks are tested separately).
PUBLIC_MID_LOGIN = {"/api/platform-owner/mfa/verify", "/api/platform-owner/break-glass-recover"}
# The MFA enrollment surface itself (an enrollment-only session's only reach).
ENROLLMENT_SURFACE_PREFIXES = ("/api/platform-owner/mfa/", "/api/platform-owner/recovery-codes/", "/admin/mfa-setup")


def admin_route_matrix(*, include_enrollment: bool = True) -> list[tuple[str, str]]:
    """(method, concrete path) for every administrator route main.app serves."""
    import main
    from starlette.routing import Route
    matrix = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not isinstance(route, Route) or not path.startswith(ADMIN_NAMESPACE_PREFIXES):
            continue
        if path in PUBLIC_MID_LOGIN:
            continue
        if not include_enrollment and path.startswith(ENROLLMENT_SURFACE_PREFIXES):
            continue
        import re
        concrete = re.sub(r"\{[^}]+\}", "cust-a", path)
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            matrix.append((method, concrete))
    return matrix


def refused(response) -> bool:
    """401/403, or a redirect that is not into an administrator page."""
    if response.status_code in (401, 403):
        return True
    if response.status_code in (302, 303, 307):
        location = response.headers.get("location", "")
        return not location.startswith(("/admin", "/operations", "/api/admin"))
    return False
