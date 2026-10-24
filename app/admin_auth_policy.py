"""admin_auth_policy: the cloud/edge profile decision and sanitized
authentication monitoring for administrator access (Linux 1.2.7.1, D-AUTHZ-2b).

Cloud profile
-------------
is_cloud_profile() is True when ANY configured source says cloud/combined
(main.RUNTIME_ROLE, cloud_config.settings.runtime_role, the partner portal's
settings, feature_entitlements.runtime_role()) OR when this database carries
the role lock (platform_security_state key 'role_lock' = 'cloud') written on the
first cloud run. A cloud database later started with an edge configuration
therefore keeps the cloud rules (administrator MFA, no legacy admin sign-in) and
logs security.runtime_role_mismatch -- a mis-set env var can never silently
drop the internet-facing portal back to the appliance's password-only rules.
The lock is never written by an edge configuration.

Monitoring
----------
auth_audit() writes an audit_logs row and a structured log line with no
secrets: the e-mail is masked (first letters + a short hash, so one account's
events still correlate), and detail keys that look like a credential
(password, code, token, secret, recovery, otp, hash, cookie) are dropped.
failed_mfa_attempts_24h() is the owner-visible per-account count (M-2); five
failed MFA codes for one account within fifteen minutes log
security.mfa_failure_burst and audit mfa_failure_burst.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger("anyaicam.security")

CLOUD_ROLES = frozenset({"cloud", "combined"})
ENROLLMENT_SESSION_MINUTES = 15
MFA_FAILURE_BURST_THRESHOLD = 5
MFA_FAILURE_BURST_WINDOW_MINUTES = 15
# Legacy users.json roles that reach the Administrator Portal (manage_settings /
# manage_users in main.ROLE_PERMISSIONS).
ADMIN_CAPABLE_LEGACY_ROLES = frozenset({"administrator", "admin", "support_admin", "installer"})
_ADMIN_PERMISSIONS = frozenset({"manage_settings", "manage_users"})
_SECRET_MARKERS = ("password", "passwd", "code", "token", "secret", "recovery", "otp", "totp", "hash", "cookie", "credential")

_lock = threading.Lock()
_locked_targets: set = set()
_negative_cache: dict = {}
_NEGATIVE_TTL_SECONDS = 5.0
_mismatch_logged: set = set()


def reset_cache_for_tests() -> None:
    with _lock:
        _locked_targets.clear()
        _negative_cache.clear()
        _mismatch_logged.clear()


# ------------------------------------------------------------------ profile


def configured_roles() -> list[str]:
    roles: list[str] = []
    main_module = sys.modules.get("main")
    if main_module is not None:
        roles.append(str(getattr(main_module, "RUNTIME_ROLE", "") or ""))
    for module_name in ("cloud_config", "partner_portal"):
        module = sys.modules.get(module_name)
        settings = getattr(module, "settings", None) if module is not None else None
        if settings is not None:
            roles.append(str(getattr(settings, "runtime_role", "") or ""))
    try:
        import feature_entitlements
        roles.append(feature_entitlements.runtime_role())
    except Exception:
        pass
    return [role.strip().lower() for role in roles if role]


def configured_cloud() -> bool:
    return any(role in CLOUD_ROLES for role in configured_roles())


def _target() -> str:
    try:
        from database_backend import target_key
        return str(target_key())
    except Exception:
        return "default"


def _db_role_lock() -> str | None:
    target = _target()
    with _lock:
        if target in _locked_targets:
            return "cloud"
        checked = _negative_cache.get(target)
        if checked is not None and time.monotonic() - checked < _NEGATIVE_TTL_SECONDS:
            return None
    value = None
    try:
        from database_backend import connect
        with connect() as db:
            row = db.execute("SELECT value FROM platform_security_state WHERE key='role_lock'").fetchone()
            value = row["value"] if row else None
    except Exception:
        value = None
    with _lock:
        if value == "cloud":
            _locked_targets.add(target)
            _negative_cache.pop(target, None)
            return "cloud"
        _negative_cache[target] = time.monotonic()
    return None


def is_cloud_profile() -> bool:
    if configured_cloud():
        return True
    if _db_role_lock() == "cloud":
        target = _target()
        if target not in _mismatch_logged:
            _mismatch_logged.add(target)
            logger.warning("security.runtime_role_mismatch configured=%s role_lock=cloud -- keeping the cloud profile",
                           ",".join(sorted(set(configured_roles()))) or "unset")
        return True
    return False


ROLE_LOCK_DDL = ("CREATE TABLE IF NOT EXISTS platform_security_state("
                 "key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL)")


def record_role_lock(db=None) -> bool:
    """Writes the cloud role lock when (and only when) this process is
    configured as cloud/combined. Idempotent; never removes a lock."""
    if not configured_cloud():
        return False

    def _write(handle) -> None:
        handle.execute(ROLE_LOCK_DDL)
        if not handle.execute("SELECT 1 FROM platform_security_state WHERE key='role_lock'").fetchone():
            handle.execute("INSERT INTO platform_security_state(key,value,updated_at) VALUES('role_lock','cloud',?)",
                           (datetime.now().isoformat(),))

    if db is not None:
        _write(db)
    else:
        from database_backend import connect
        with connect() as handle:
            _write(handle)
    with _lock:
        _locked_targets.add(_target())
        _negative_cache.pop(_target(), None)
    return True


# ------------------------------------------------------------------ legacy accounts


def legacy_user_is_admin_capable(user: dict | None) -> bool:
    if not user:
        return False
    role = str(user.get("role") or "").strip().lower()
    if role in ADMIN_CAPABLE_LEGACY_ROLES or user.get("super_admin"):
        return True
    main_module = sys.modules.get("main")
    permissions = getattr(main_module, "ROLE_PERMISSIONS", {}).get(role, set()) if main_module is not None else set()
    return bool(set(permissions) & _ADMIN_PERMISSIONS)


# ------------------------------------------------------------------ sanitized monitoring


def mask_email(email: str | None) -> str:
    email = str(email or "").strip().lower()
    if not email:
        return ""
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:10]
    local, _, domain = email.partition("@")
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    return f"{local[:1]}***@{domain[:1]}***{('.' + tld) if tld else ''}#{digest}"


def _clean_details(details: dict | None) -> dict:
    clean: dict = {}
    for key, value in (details or {}).items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
            clean[key] = value
        elif isinstance(value, str) and len(value) <= 80:
            clean[key] = mask_email(value) if "@" in value else value
    return clean


def auth_audit(action: str, *, actor_email: str | None = None, actor_role: str | None = None, reason: str | None = None,
               entity_type: str = "session", entity_id: str = "", details: dict | None = None) -> None:
    """One sanitized authentication event: audit_logs row + structured log.
    Never raises (monitoring must not change an authorization outcome)."""
    payload = _clean_details(details)
    if reason:
        payload = {"reason": str(reason)[:80], **{k: v for k, v in payload.items() if k != "reason"}}
    masked = mask_email(actor_email)
    try:
        from partner_db import connection
        with connection() as db:
            db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                       (masked, actor_role, action, entity_type, str(entity_id or ""), json.dumps(payload), datetime.now().isoformat()))
    except Exception as error:  # pragma: no cover - monitoring only
        logger.error("auth_audit.write_failed action=%s error=%s", action, type(error).__name__)
    logger.info("auth_audit action=%s actor=%s role=%s reason=%s", action, masked or "-", actor_role or "-", payload.get("reason", "-"))


def record_mfa_failure(*, user_id: str, email: str | None, reason: str = "incorrect_code") -> int:
    """Audits one failed MFA attempt and raises the burst alert; returns the
    failures for this account within the burst window."""
    auth_audit("mfa_login_failed", actor_email=email, actor_role="administrator", reason=reason, entity_id=user_id)
    since = (datetime.now() - timedelta(minutes=MFA_FAILURE_BURST_WINDOW_MINUTES)).isoformat()
    count = 0
    try:
        from partner_db import connection
        with connection() as db:
            count = int(db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='mfa_login_failed' AND entity_id=? AND created_at>?",
                                   (str(user_id), since)).fetchone()["n"])
    except Exception:
        count = 0
    if count >= MFA_FAILURE_BURST_THRESHOLD:
        logger.warning("security.mfa_failure_burst actor=%s failures=%s window_minutes=%s", mask_email(email), count,
                       MFA_FAILURE_BURST_WINDOW_MINUTES)
        auth_audit("mfa_failure_burst", actor_email=email, actor_role="administrator", reason="threshold", entity_id=user_id,
                   details={"count": count})
    return count


def failed_mfa_attempts_24h(user_id: str | None = None):
    """Failed MFA codes in the last 24 hours: an int for one account, or
    {user_id: count} for every account (owner-visible, M-2)."""
    since = (datetime.now() - timedelta(hours=24)).isoformat()
    from partner_db import connection
    with connection() as db:
        if user_id is not None:
            return int(db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='mfa_login_failed' AND entity_id=? AND created_at>?",
                                  (str(user_id), since)).fetchone()["n"])
        rows = db.execute("SELECT entity_id, COUNT(*) AS n FROM audit_logs WHERE action='mfa_login_failed' AND created_at>? GROUP BY entity_id",
                          (since,)).fetchall()
    return {row["entity_id"]: int(row["n"]) for row in rows}
