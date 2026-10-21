"""Requirement #11 (VMS 1.2.7): offline admin tool to close the owner's TEST customer accounts safely.

    python test_account_cleanup.py inventory [--approved approved.json] [--output inventory.json]
    python test_account_cleanup.py plan --approved approved.json --output plan.json
                                        [--allow-appliance-linked --appliance-linked-reason "..."]
    python test_account_cleanup.py apply --plan plan.json --confirm <token> --actor <who>
                                         (--backup-dir DIR | --backup-verified DUMP) [--max-age-minutes 30]

It works on the application database through the app's own DB layer (database_backend: SQLite or PostgreSQL via
ANYAICAM_DATABASE_BACKEND / ANYAICAM_PARTNER_DB / ANYAICAM_DATABASE_URL; --sqlite-path for an offline copy). It never
calls Stripe, AWS, S3 or an appliance. Procedure and owner gates: docs/test-account-cleanup.md. Safety design:
Red's RED-SAFETY-MAP (2026-10-16).

Classes (decided from live dependency rows, never from the approved list):
  P protected        owner/Founder/platform-owner/admin identities, staff roles, global grants: never touched.
  C appliance-linked any appliance, camera, claim, recording, detection event (or their media/peers): refused
                     unless --allow-appliance-linked with a reason; then tombstoned, appliance rows untouched.
  B tombstone        any other history (billing, Stripe, orders, notes, ...): every row kept; logins disabled,
                     sessions/grants revoked, the email released (rewritten to a non-routable @tombstone.invalid
                     address so it can never again match a purchase by email), customers.status='closed_test'.
  A hard delete      only login/identity/preference rows exist: deleted in FK-safe order.

Safety: inventory and plan open the database read-only. plan refuses the whole list on any problem (P, C without
the flag, class or email-hash mismatch, live Stripe evidence, active/paid billing rows not marked test, unknown or
already-closed ids). apply needs the exact confirm token of a fresh, untampered plan, re-derives every change from
live data and refuses if anything differs, makes and verifies a backup first (SQLite online backup +
integrity_check; PostgreSQL: a pg_dump taken after the plan, passed as --backup-verified), runs one transaction,
checks row-count deltas, foreign keys, integrity and that every other account is unchanged, writes audit_logs and
test_account_closures rows, and is idempotent (a re-run of an applied plan changes nothing).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import database_backend

TOOL = "test_account_cleanup"
FORMAT = 1
CLOSED_STATUS = "closed_test"
CLOSURES_TABLE = "test_account_closures"
TOMBSTONE_DOMAIN = "tombstone.invalid"  # RFC 2606 reserved: never deliverable, never a real customer's address
DISABLED_PASSWORD = "!closed-test-account"  # not a pbkdf2 string: partner_db.verify_password() always says no
DEFAULT_MAX_AGE_MINUTES = 30
APPLY_TIME = "<apply-time>"
ACTOR_ROLE = "maintenance:test_account_cleanup"

# Owner-protected identities (RED-SAFETY-MAP section 2, class P). Emails are matched by sha256 of the normalized address.
PROTECTED_EMAIL_SHA256 = {
    "42248ae6ff1fd48d8db75000896a4e9c877f34336f8ed7cca9432940a0aadc5f",  # amata@anyaicam.com (admin)
    "cb32b9bbad83bc3811ca7bb87aa1d3cdff0f2c9dcf0ad2ff38e2a5b950730820",  # alexmata25@gmail.com (Founder)
}
PROTECTED_CUSTOMER_PREFIXES = ("4efaf5153f",)  # Founder account: never archive
PROTECTED_GRANT_PREFIXES = ("ca357bbbd5388114",)  # sandbox admin grant
PROTECTED_EMAIL_ENV = ("ANYAICAM_PLATFORM_OWNER_EMAIL", "ANYAICAM_ADMIN_EMAIL")
FOUNDER_CUSTOMER_IDS_ENV = "ANYAICAM_FOUNDER_LPR_CUSTOMER_IDS"
STAFF_ROLES = {"administrator", "partner_owner", "salesperson", "technician"}

CUSTOMER_COLUMNS = ("customer_id", "resolved_customer_id")
USER_COLUMNS = ("user_id", "answered_by_user_id", "requested_by_user_id", "partner_user_id", "accepted_user_id",
                "invitee_user_id", "salesperson_user_id", "actor_user_id")
SCAN_EXCLUDED = {"customers", CLOSURES_TABLE, "audit_logs", "schema_migrations"}
C_TABLES = {"appliances", "cameras", "appliance_claims", "recordings", "detection_events", "detection_event_media",
            "appliance_wireguard_peers", "camera_provisioning_requests", "camera_scan_jobs", "facial_events",
            "live_view_sessions"}
BILLING_TABLES = ("customer_entitlements", "camera_plan_entitlements_v2", "addon_subscriptions", "analytics_subscriptions",
                  "subscription_payments", "hardware_orders", "hardware_checkouts", "hardware_returns", "storage_trials",
                  "deferred_storage_plans", "checkout_pending", "plan_upgrade_attempts", "camera_plan_change_attempts_v2",
                  "build_system_intents", "stripe_customer_bindings", "stripe_customer_mode_bindings",
                  "pending_customer_links", "pending_hardware_order_links", "pending_analytics_links",
                  "purchase_attachments", "commission_ledger")
# Rows with no status of their own that always need a test mark (a binding or an attached/resolved purchase).
ALWAYS_REVIEW = {"stripe_customer_bindings", "pending_customer_links", "pending_hardware_order_links",
                 "pending_analytics_links", "purchase_attachments", "commission_ledger"}
ENDED_STATUSES = {"cancelled", "canceled", "expired", "ended", "refunded", "failed", "void", "voided",
                  "incomplete_expired", "rejected", "superseded", "closed", "deleted", "inactive", "revoked",
                  "reversed", "abandoned"}
MONEY_COLUMNS = ("amount_paid_cents", "amount_cents", "amount_total_cents", "original_amount_cents")

# Class A: deleted in this order (children before parents). Selectors: customer:<col> = the customer id,
# users:<col> IN the customer's login users, emails:<col> = one of the account's own (normalized) emails on a row of
# no other customer, parent:<col>:<table> IN the ids of that table's rows selected for this account.
A_DELETE_ORDER = (
    ("notification_deliveries", ("parent:notification_id:notifications",)),
    ("notifications", ("customer:customer_id", "users:user_id")),
    ("mobile_push_outbox", ("parent:device_id:mobile_push_devices",)),
    ("mobile_push_devices", ("customer:customer_id", "users:user_id")),
    ("mobile_refresh_tokens", ("users:user_id", "parent:device_id:mobile_devices")),
    ("mobile_devices", ("customer:customer_id", "users:user_id")),
    ("customer_notification_channel_cameras", ("users:user_id",)),
    ("customer_notification_channels", ("customer:customer_id", "users:user_id")),
    ("notification_preferences", ("customer:customer_id", "users:user_id")),
    ("notification_event_keys", ("customer:customer_id",)),
    ("customer_site_permissions", ("users:user_id",)),
    ("customer_user_permissions", ("customer:customer_id", "users:user_id")),
    ("mfa_settings", ("users:user_id",)),
    ("partner_terms_acceptances", ("users:user_id",)),
    ("password_reset_tokens", ("users:user_id",)),
    ("pending_mfa_logins", ("users:user_id",)),
    ("user_sessions", ("users:user_id",)),
    ("identity_grants", ("users:user_id",)),
    ("local_customer_credentials", ("customer:customer_id", "users:user_id")),
    ("sms_test_sends", ("customer:customer_id", "users:user_id")),
    ("customer_registration_requests", ("customer:customer_id", "users:user_id", "emails:email")),
    ("direct_signups", ("customer:customer_id", "users:user_id", "emails:email")),
    ("invitations", ("customer:customer_id", "users:accepted_user_id", "users:invitee_user_id")),
    ("account_lockouts", ("emails:email",)),
    ("customer_setup_drafts", ("customer:customer_id",)),
    ("local_storage_policy", ("customer:customer_id",)),
    ("customer_cloud_policy", ("customer:customer_id",)),
    ("aaco_customer_settings", ("customer:customer_id",)),
    ("partner_users", ("customer:customer_id",)),
    ("sites", ("customer:customer_id",)),
    ("customers", ("customer:id",)),
)
A_ALLOWED = {f"{table}.{sel.split(':')[1]}" for table, sels in A_DELETE_ORDER if table != "customers"
             for sel in sels if sel.split(":")[0] in ("customer", "users")}

# Class B (and C with the flag): bulk updates that disable access. (table, selectors, extra condition, set)
B_BULK_UPDATES = (
    ("invitations", ("customer:customer_id", "users:accepted_user_id", "users:invitee_user_id"), "status='pending'",
     {"status": "cancelled", "cancelled_at": APPLY_TIME}),
    ("user_sessions", ("users:user_id",), "revoked_at IS NULL", {"revoked_at": APPLY_TIME}),
    ("identity_grants", ("users:user_id",), "revoked_at IS NULL", {"revoked_at": APPLY_TIME}),
    ("mobile_refresh_tokens", ("users:user_id",), "revoked_at IS NULL", {"revoked_at": APPLY_TIME}),
    ("mobile_devices", ("customer:customer_id", "users:user_id"), "revoked_at IS NULL", {"revoked_at": APPLY_TIME}),
    ("mobile_push_devices", ("customer:customer_id", "users:user_id"), "enabled<>0", {"enabled": 0}),
    ("local_customer_credentials", ("customer:customer_id", "users:user_id"), "revoked_at IS NULL",
     {"revoked_at": APPLY_TIME, "revoked_reason": CLOSED_STATUS}),
    ("password_reset_tokens", ("users:user_id",), "used_at IS NULL", {"used_at": APPLY_TIME}),
    ("pending_mfa_logins", ("users:user_id",), "used_at IS NULL", {"used_at": APPLY_TIME}),
    ("customer_notification_channels", ("customer:customer_id", "users:user_id"), "(email_enabled<>0 OR sms_enabled<>0)",
     {"email_enabled": 0, "sms_enabled": 0}),
)
# Rows that hold one of the account's emails and are rewritten one by one (the column is UNIQUE in places).
B_EMAIL_REWRITES = (
    ("customer_registration_requests", "r", ("customer:customer_id", "users:user_id", "emails:email"),
     {"verification_token_hash": None}),
    ("direct_signups", "s", ("customer:customer_id", "users:user_id", "emails:email"), {}),
)

SECRET_ID_TABLES = {"user_sessions", "pending_mfa_logins", "password_reset_tokens", "mobile_refresh_tokens",
                    "break_glass_tokens", "appliance_activation_tokens"}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX = re.compile(r"^[0-9a-f]{12,64}$")


class Refused(Exception):
    """A safety rule said no. Nothing was written."""

    def __init__(self, reason: str, details=None):
        super().__init__(reason)
        self.reason = reason
        self.details = details


# ------------------------------------------------------------------------------------------------------------ helpers
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_email(value) -> str:
    return str(value or "").strip().lower()


def email_sha256(value) -> str:
    return hashlib.sha256(normalize_email(value).encode()).hexdigest()


def mask_email(value) -> str | None:
    if not value:
        return value
    local, _, domain = normalize_email(value).partition("@")
    return f"{local[:2]}***@{domain} (sha256:{email_sha256(value)[:12]})"


def tombstone_email(kind: str, row_id) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(row_id))
    return f"closed-test+{kind}-{safe}@{TOMBSTONE_DOMAIN}"


def is_tombstone_email(value) -> bool:
    return normalize_email(value).endswith("@" + TOMBSTONE_DOMAIN)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "t", "yes"}


def _is_sqlite() -> bool:
    return database_backend.backend() == "sqlite"


def _q(db, sql: str, params=()) -> list[dict]:
    return [dict(item) for item in db.execute(sql, tuple(params)).fetchall()]


def _scalar(db, sql: str, params=()):
    found = _q(db, sql, params)
    return next(iter(found[0].values())) if found else None


def target_descriptor() -> dict:
    """Which database this run talks to (no secrets): a plan is valid only for the same target."""
    if _is_sqlite():
        return {"backend": "sqlite", "path": str(database_backend.sqlite_target_path().resolve())}
    parts = urlsplit(database_backend.postgres_target_url())
    return {"backend": "postgresql", "host": parts.hostname, "port": parts.port,
            "database": (parts.path or "").lstrip("/"), "user": parts.username}


@contextmanager
def read_only_connection():
    """inventory/plan: SQLite opened mode=ro + query_only (never creates the file); PostgreSQL READ ONLY transaction."""
    if _is_sqlite():
        path = database_backend.sqlite_target_path()
        if not path.is_file():
            raise Refused(f"database file not found: {path}")
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        try:
            yield db
        finally:
            db.close()
    else:
        with database_backend.connect() as db:
            db.execute("SET TRANSACTION READ ONLY")
            yield db


def load_schema(db) -> dict[str, list[str]]:
    if _is_sqlite():
        names = [r["name"] for r in _q(db, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: [c["name"] for c in _q(db, f"PRAGMA table_info({n})")] for n in names if _IDENT.match(n)}
    schema: dict[str, list[str]] = {}
    for r in _q(db, "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema() "
                    "ORDER BY table_name,ordinal_position"):
        if _IDENT.match(r["table_name"]) and _IDENT.match(r["column_name"]):
            schema.setdefault(r["table_name"], []).append(r["column_name"])
    return schema


# ------------------------------------------------------------------------------------------------------ the scan
def _row_key(table: str, item: dict) -> str:
    if item.get("id") not in (None, ""):
        return str(item["id"])
    if item.get("stripe_customer_id"):
        return str(item["stripe_customer_id"]) + (f"/livemode={item.get('livemode')}" if "livemode" in item else "")
    return _sha256_text(item)[:12]


def _billing_flags(table: str, item: dict, test_ids: set, live_ids: set) -> dict:
    sessions = [str(v) for k, v in item.items() if "session" in k and v]
    scid = str(item.get("stripe_customer_id") or "")
    if table == "stripe_customer_mode_bindings":
        live = _truthy(item.get("livemode"))
        test = not live
    else:
        live = any(s.startswith("cs_live_") for s in sessions) or bool(scid and scid in live_ids)
        test = any(s.startswith("cs_test_") for s in sessions) or bool(scid and scid in test_ids)
    status = str(item.get("status") or item.get("state") or "").strip().lower()
    money = False
    for column in MONEY_COLUMNS:
        try:
            money = money or int(item.get(column) or 0) > 0
        except (TypeError, ValueError):
            money = True
    is_open = (bool(status) and status not in ENDED_STATUSES) or (not status and table in ALWAYS_REVIEW)
    needs_test_mark = live or (not test and (is_open or money or table in ALWAYS_REVIEW))
    return {"row": f"{table}:{_row_key(table, item)}", "status": status or None, "test_marked": test,
            "live_evidence": live, "money": money, "needs_test_mark": needs_test_mark}


def _env_protected_emails() -> set[str]:
    return {normalize_email(os.environ.get(name, "")) for name in PROTECTED_EMAIL_ENV} - {""}


def _env_founder_ids() -> set[str]:
    return {item.strip() for item in os.environ.get(FOUNDER_CUSTOMER_IDS_ENV, "").split(",") if item.strip()}


def scan(db, schema: dict) -> dict:
    """Every fact the classification needs, read in one pass (a few GROUP BY queries per table)."""
    if "customers" not in schema:
        raise Refused("customers table not found")
    customers = _q(db, "SELECT * FROM customers ORDER BY created_at, id")
    ids = {c["id"] for c in customers}
    users = _q(db, "SELECT * FROM partner_users ORDER BY created_at, id") if "partner_users" in schema else []
    owner_of = {u["id"]: u["customer_id"] for u in users if u.get("customer_id") in ids}
    counts: dict[str, Counter] = {cid: Counter() for cid in ids}
    for table, cols in schema.items():
        if table in SCAN_EXCLUDED:
            continue
        for col in cols:
            if col in CUSTOMER_COLUMNS:
                for r in _q(db, f"SELECT {col} AS k, COUNT(*) AS n FROM {table} WHERE {col} IS NOT NULL GROUP BY {col}"):
                    if r["k"] in counts:
                        counts[r["k"]][f"{table}.{col}"] += int(r["n"])
            elif col in USER_COLUMNS and owner_of:
                for r in _q(db, f"SELECT {col} AS k, COUNT(*) AS n FROM {table} WHERE {col} IS NOT NULL GROUP BY {col}"):
                    if r["k"] in owner_of:
                        counts[owner_of[r["k"]]][f"{table}.{col}"] += int(r["n"])
    grants: dict[str, list] = {}
    if "identity_grants" in schema:
        for g in _q(db, "SELECT id,user_id,role,scope_type,revoked_at FROM identity_grants"):
            grants.setdefault(g["user_id"], []).append(g)
    test_ids: dict[str, set] = {}
    live_ids: dict[str, set] = {}
    if "stripe_customer_mode_bindings" in schema:
        for b in _q(db, "SELECT customer_id,livemode,stripe_customer_id FROM stripe_customer_mode_bindings"):
            (live_ids if _truthy(b["livemode"]) else test_ids).setdefault(b["customer_id"], set()).add(str(b["stripe_customer_id"]))
    billing: dict[str, list] = {cid: [] for cid in ids}
    for table in BILLING_TABLES:
        cols = schema.get(table, [])
        col = next((c for c in CUSTOMER_COLUMNS if c in cols), None)
        if not col:
            continue
        for item in _q(db, f"SELECT * FROM {table} WHERE {col} IS NOT NULL"):
            cid = item[col]
            if cid in billing:
                billing[cid].append({"table": table, **_billing_flags(table, item, test_ids.get(cid, set()), live_ids.get(cid, set()))})
    closures = {}
    if CLOSURES_TABLE in schema:
        closures = {r["customer_id"]: r for r in _q(db, f"SELECT * FROM {CLOSURES_TABLE}")}
    requests = []
    if "customer_registration_requests" in schema:
        requests = _q(db, "SELECT id,email,status,customer_id,user_id,requested_at FROM customer_registration_requests "
                          "ORDER BY requested_at,id")
    return {"customers": customers, "users": users, "counts": counts, "grants": grants, "billing": billing,
            "closures": closures, "requests": requests}


def _protected_reasons(customer: dict, users: list, grants: dict) -> list[str]:
    reasons = []
    env_emails = _env_protected_emails()
    emails = [customer.get("email")] + [u.get("email") for u in users]
    if any(email_sha256(e) in PROTECTED_EMAIL_SHA256 for e in emails if e):
        reasons.append("owner-protected email (owner admin or Founder)")
    if any(normalize_email(e) in env_emails for e in emails if e):
        reasons.append("platform owner / bootstrap admin email")
    if str(customer["id"]).startswith(PROTECTED_CUSTOMER_PREFIXES):
        reasons.append("Founder account: never archive")
    if customer["id"] in _env_founder_ids():
        reasons.append(f"listed in {FOUNDER_CUSTOMER_IDS_ENV}")
    for u in users:
        if str(u.get("role") or "") in STAFF_ROLES:
            reasons.append(f"login user {u['id']} has staff role {u['role']}")
        for g in grants.get(u["id"], []):
            if str(g["id"]).startswith(PROTECTED_GRANT_PREFIXES):
                reasons.append("sandbox admin grant")
            elif g.get("scope_type") == "global" and not g.get("revoked_at"):
                reasons.append(f"login user {u['id']} holds a global grant")
    return sorted(set(reasons))


def classify(customer: dict, facts: dict) -> dict:
    cid = customer["id"]
    users = [u for u in facts["users"] if u.get("customer_id") == cid]
    deps = {k: n for k, n in sorted(facts["counts"][cid].items()) if n}
    protected = _protected_reasons(customer, users, facts["grants"])
    c_hits = sorted(k for k in deps if k.split(".")[0] in C_TABLES)
    other = sorted(k for k in deps if k not in A_ALLOWED and k not in c_hits)
    if protected:
        cls, reasons = "P", protected
    elif c_hits:
        cls, reasons = "C", [f"appliance-linked: {k}={deps[k]}" for k in c_hits]
    elif other:
        cls, reasons = "B", [f"history kept: {k}={deps[k]}" for k in other]
    else:
        cls, reasons = "A", ["only login/identity/preference rows: may be deleted"]
    billing = facts["billing"][cid]
    digest = _sha256_text({"customer": customer, "users": users, "deps": deps, "billing": billing,
                           "grants": [facts["grants"].get(u["id"], []) for u in users]})
    return {"customer_id": cid, "class": cls, "reasons": reasons, "status": customer.get("status"),
            "source": customer.get("source"), "created_at": customer.get("created_at"),
            "closed": str(customer.get("status") or "").lower() == CLOSED_STATUS or cid in facts["closures"],
            "email_sha256": email_sha256(customer.get("email")), "_email": customer.get("email"),
            "login_users": [{"id": u["id"], "role": u.get("role"), "account_status": u.get("account_status"),
                             "email_sha256": email_sha256(u.get("email")), "_email": u.get("email")} for u in users],
            "dependencies": deps, "billing": billing,
            "billing_needing_test_mark": [b["row"] for b in billing if b["needs_test_mark"]],
            "state_digest": digest}


def classify_all(facts: dict) -> list[dict]:
    order = {"P": 0, "C": 1, "B": 2, "A": 3}
    result = [classify(c, facts) for c in facts["customers"]]
    result.sort(key=lambda r: (order[r["class"]], str(r.get("created_at") or ""), r["customer_id"]))
    return result


# ------------------------------------------------------------------------------------------------- inventory
def _approved_hashes(approved: dict | None) -> dict[str, str]:
    return {a["customer_id"]: a["email_sha256"] for a in (approved or {}).get("accounts", [])}


def _show(email, cid: str, approved: dict[str, str]) -> str | None:
    """An email is shown in full only when the owner-approved list names this account with this email's hash."""
    expected = approved.get(cid)
    if email and expected and email_sha256(email).startswith(expected):
        return normalize_email(email)
    return mask_email(email)


def build_inventory(approved: dict | None = None) -> dict:
    allowed = _approved_hashes(approved)
    with read_only_connection() as db:
        schema = load_schema(db)
        facts = scan(db, schema)
    rows = classify_all(facts)
    class_of = {r["customer_id"]: r["class"] for r in rows}
    customers = []
    for r in rows:
        item = {k: v for k, v in r.items() if not k.startswith("_")}
        item["email"] = _show(r["_email"], r["customer_id"], allowed)
        item["login_users"] = [{**{k: v for k, v in u.items() if not k.startswith("_")},
                                "email": _show(u["_email"], r["customer_id"], allowed)} for u in r["login_users"]]
        customers.append(item)
    users = []
    for u in facts["users"]:
        cid = u.get("customer_id")
        if cid in class_of:
            cls, reason = class_of[cid], f"follows customer {cid}"
        else:
            cls, reason = "P", "not a customer account (staff/partner or orphan user): never touched by this tool"
        users.append({"id": u["id"], "role": u.get("role"), "account_status": u.get("account_status"),
                      "approved": u.get("approved"), "customer_id": cid, "class": cls, "reason": reason,
                      "email": _show(u.get("email"), cid, allowed) if cid else mask_email(u.get("email")),
                      "email_sha256": email_sha256(u.get("email"))})
    known_emails = {normalize_email(c.get("email")) for c in facts["customers"]} | {normalize_email(u.get("email")) for u in facts["users"]}
    requests = [{"id": q["id"], "status": q.get("status"), "customer_id": q.get("customer_id"), "user_id": q.get("user_id"),
                 "requested_at": q.get("requested_at"), "email": mask_email(q.get("email")),
                 "email_sha256": email_sha256(q.get("email")),
                 "orphan_blocks_reregistration": normalize_email(q.get("email")) not in known_emails
                 and str(q.get("status") or "") in ("pending", "approved")} for q in facts["requests"]]
    return {"tool": TOOL, "format": FORMAT, "mode": "inventory", "read_only": True,
            "generated_at": _utcnow().isoformat(), "database": target_descriptor(),
            "schema": {"tables": len(schema), "closures_table": CLOSURES_TABLE in schema},
            "summary": {"customers": len(rows), "by_class": dict(sorted(Counter(class_of.values()).items())),
                        "login_users": len(users), "users_by_class": dict(sorted(Counter(u["class"] for u in users).items())),
                        "closed": sum(1 for r in rows if r["closed"])},
            "customers": customers, "users": users, "registration_requests": requests}


# ------------------------------------------------------------------------------------------------------ the plan
def load_approved(data) -> dict:
    """Validate the owner-approved list strictly; any problem refuses the whole list."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise Refused("approved list: expected a JSON object with \"format\": 1")
    unknown = set(data) - {"format", "approved_by", "approved_at", "note", "accounts"}
    if unknown:
        raise Refused(f"approved list: unknown fields {sorted(unknown)}")
    if not str(data.get("approved_by") or "").strip() or not str(data.get("approved_at") or "").strip():
        raise Refused("approved list: approved_by and approved_at are required")
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise Refused("approved list: accounts must be a non-empty list")
    seen, normalized = set(), []
    for index, entry in enumerate(accounts):
        if not isinstance(entry, dict):
            raise Refused(f"approved list: entry {index} is not an object")
        unknown = set(entry) - {"customer_id", "expected_class", "email_sha256", "test_billing_rows", "note"}
        if unknown:
            raise Refused(f"approved list: entry {index} has unknown fields {sorted(unknown)}")
        cid = str(entry.get("customer_id") or "").strip()
        expected = str(entry.get("expected_class") or "").strip().upper()
        digest = str(entry.get("email_sha256") or "").strip().lower()
        rows = entry.get("test_billing_rows") or []
        if not cid or expected not in ("A", "B", "C") or not _HEX.match(digest):
            raise Refused(f"approved list: entry {index} needs customer_id, expected_class A/B/C and email_sha256 "
                          "(12-64 lowercase hex of sha256(normalized email))")
        if not isinstance(rows, list) or not all(isinstance(r, str) and r for r in rows):
            raise Refused(f"approved list: entry {index} test_billing_rows must be a list of row keys")
        if cid in seen:
            raise Refused(f"approved list: customer {cid} is listed twice")
        seen.add(cid)
        normalized.append({"customer_id": cid, "expected_class": expected, "email_sha256": digest,
                           "test_billing_rows": sorted(set(rows))})
    return {"format": FORMAT, "approved_by": str(data["approved_by"]).strip(), "approved_at": str(data["approved_at"]).strip(),
            "accounts": normalized}


def _selector(db_schema: dict, table: str, sels, ctx: dict) -> tuple[str, list, str] | None:
    """(where-sql, params, human description) for one table and this account, or None when nothing applies."""
    cols = db_schema.get(table)
    if cols is None:
        return None
    parts, params, human = [], [], []
    for sel in sels:
        kind, column, *rest = sel.split(":")
        if kind == "parent":
            parent = rest[0]
            sub = _selector(db_schema, parent, dict(A_DELETE_ORDER).get(parent, ()), ctx) if column in cols else None
            if sub and "id" in db_schema.get(parent, []):
                parts.append(f"{column} IN (SELECT id FROM {parent} WHERE {sub[0]})")
                params += sub[1]
                human.append(f"{column} IN ({parent} WHERE {sub[2]})")
            continue
        if column not in cols:
            continue
        if kind == "customer":
            parts.append(f"{column}=?")
            params.append(ctx["customer_id"])
            human.append(f"{column}={ctx['customer_id']}")
        elif kind == "users" and ctx["user_ids"]:
            parts.append(f"{column} IN ({','.join('?' * len(ctx['user_ids']))})")
            params += ctx["user_ids"]
            human.append(f"{column} IN ({','.join(ctx['user_ids'])})")
        elif kind == "emails" and ctx["emails"]:
            clause = f"lower({column}) IN ({','.join('?' * len(ctx['emails']))})"
            if "customer_id" in cols:
                clause = f"({clause} AND (customer_id IS NULL OR customer_id=?))"
            parts.append(clause)
            params += ctx["emails"] + ([ctx["customer_id"]] if "customer_id" in cols else [])
            human.append(f"{column} IN (sha256:{','.join(email_sha256(e)[:12] for e in ctx['emails'])})")
    if not parts:
        return None
    return "(" + " OR ".join(parts) + ")", params, " OR ".join(human)


def _select_rows(db, schema, table, where, params) -> tuple[int, list]:
    count = int(_scalar(db, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params) or 0)
    ids = []
    if count and "id" in schema[table]:
        ids = sorted(str(r["id"]) for r in _q(db, f"SELECT id FROM {table} WHERE {where}", params))
    return count, ids


def _shown_ids(table: str, ids: list) -> list:
    """Some ids are bearer values (a session or pending-MFA id is the token itself): the plan shows only a hash."""
    if table in SECRET_ID_TABLES:
        return sorted("sha256:" + hashlib.sha256(i.encode()).hexdigest()[:12] for i in ids)
    return ids


def _steps_for(db, schema: dict, info: dict, customer: dict, users: list, method: str) -> list[dict]:
    """The exact row changes for one account. Each step carries the SQL it will run (private, '_'-prefixed) and a
    reviewable description; plan and apply derive them with this same function from live data."""
    cid = customer["id"]
    emails = sorted({normalize_email(customer.get("email"))} | {normalize_email(u.get("email")) for u in users} - {""})
    ctx = {"customer_id": cid, "user_ids": sorted(u["id"] for u in users), "emails": emails}
    steps = []

    def add(op, table, where, params, human, set_values=None, extra=None):
        full_where = where + (f" AND {extra}" if extra else "")
        count, ids = _select_rows(db, schema, table, full_where, params)
        if not count:
            return
        steps.append({"op": op, "table": table, "match": human + (f" AND {extra}" if extra else ""), "rows": count,
                      "ids": _shown_ids(table, ids), "set": set_values or {}, "_where": full_where, "_params": list(params)})

    if method == "delete":
        for table, sels in A_DELETE_ORDER:
            found = _selector(schema, table, sels, ctx)
            if found:
                add("delete", table, *found)
        return steps
    # tombstone: the account and each of its users first, then every other row holding one of its emails
    add("update", "customers", "id=?", [cid], f"id={cid}",
        {"email": tombstone_email("c", cid), "status": CLOSED_STATUS})
    user_cols = schema.get("partner_users", [])
    for u in sorted(users, key=lambda item: item["id"]):
        values = {"email": tombstone_email("u", u["id"]), "approved": 0, "password_hash": DISABLED_PASSWORD}
        if "account_status" in user_cols:
            values["account_status"] = "revoked"
        if "authorization_version" in user_cols:
            values["authorization_version"] = int(u.get("authorization_version") or 1) + 1
        add("update", "partner_users", "id=?", [u["id"]], f"id={u['id']}", values)
    for table, kind, sels, extra_values in B_EMAIL_REWRITES:
        found = _selector(schema, table, sels, ctx)
        if not found:
            continue
        for row_id in _select_rows(db, schema, table, found[0], found[1])[1]:
            values = {"email": tombstone_email(kind, row_id),
                      **{k: v for k, v in extra_values.items() if k in schema[table]}}
            add("update", table, "id=?", [row_id], f"id={row_id}", values)
    for table, sels, extra, values in B_BULK_UPDATES:
        found = _selector(schema, table, sels, ctx)
        if found and all(k in schema[table] for k in values):
            add("update", table, *found, values, extra)
    found = _selector(schema, "account_lockouts", ("emails:email",), ctx)
    if found:
        add("delete", "account_lockouts", *found)
    return steps


def _public_step(step: dict) -> dict:
    return {k: v for k, v in step.items() if not k.startswith("_")}


def derive_entries(db, schema: dict, approved: dict, *, allow_appliance_linked: bool, appliance_reason: str) -> list[dict]:
    """Check every approved account against live data and derive its exact changes. Raises Refused listing every
    problem; nothing is partial."""
    problems = []
    if CLOSURES_TABLE not in schema:
        problems.append(f"schema has no {CLOSURES_TABLE} table: deploy the release with migration "
                        "20261020_test_account_closures first")
    if allow_appliance_linked and len(appliance_reason.strip()) < 10:
        problems.append("--allow-appliance-linked needs --appliance-linked-reason (at least 10 characters)")
    facts = scan(db, schema)
    by_id = {c["id"]: c for c in facts["customers"]}
    entries = []
    for want in approved["accounts"]:
        cid = want["customer_id"]
        customer = by_id.get(cid)
        if customer is None:
            problems.append(f"{cid}: no such customer")
            continue
        live = classify(customer, facts)
        if live["class"] == "P":
            problems.append(f"{cid}: PROTECTED ({'; '.join(live['reasons'])}): never cleaned up")
            continue
        if live["closed"]:
            problems.append(f"{cid}: already closed")
            continue
        if live["class"] != want["expected_class"]:
            problems.append(f"{cid}: live class {live['class']} differs from approved class {want['expected_class']} "
                            f"({'; '.join(live['reasons'])})")
            continue
        if not live["email_sha256"].startswith(want["email_sha256"]):
            problems.append(f"{cid}: email hash does not match the approved list")
            continue
        if live["class"] == "C" and not allow_appliance_linked:
            problems.append(f"{cid}: appliance-linked (class C) needs --allow-appliance-linked and a reason")
            continue
        attested = set(want["test_billing_rows"])
        rows = {b["row"]: b for b in live["billing"]}
        for key in sorted(attested - set(rows)):
            problems.append(f"{cid}: attested billing row {key} does not exist")
        for b in live["billing"]:
            if b["live_evidence"]:
                problems.append(f"{cid}: LIVE Stripe evidence on {b['row']}: never cleaned up by this tool")
            elif b["needs_test_mark"] and b["row"] not in attested:
                problems.append(f"{cid}: billing row {b['row']} (status {b['status']}) is not marked test; the owner must "
                                "list it in test_billing_rows after checking it in Stripe Test Mode")
        users = [u for u in facts["users"] if u.get("customer_id") == cid]
        method = "delete" if live["class"] == "A" else "tombstone"
        entries.append({"customer_id": cid, "class": live["class"], "method": method,
                        "email_sha256": live["email_sha256"], "state_digest": live["state_digest"],
                        "previous_status": customer.get("status"),
                        "attested_test_billing_rows": sorted(attested),
                        "steps": _steps_for(db, schema, live, customer, users, method)})
    if problems:
        raise Refused("the approved list was refused; nothing was planned", problems)
    return entries


def _plan_hash(plan: dict) -> str:
    return _sha256_text({k: v for k, v in plan.items() if k not in ("plan_sha256", "confirm_token")})


def confirm_token_for(plan_sha256: str) -> str:
    return "APPLY-" + plan_sha256[:16]


def build_plan(approved_data, *, allow_appliance_linked: bool = False, appliance_reason: str = "",
               now: datetime | None = None) -> dict:
    approved = load_approved(approved_data)
    created = now or _utcnow()
    with read_only_connection() as db:
        schema = load_schema(db)
        entries = derive_entries(db, schema, approved, allow_appliance_linked=allow_appliance_linked,
                                 appliance_reason=appliance_reason)
    plan = {"tool": TOOL, "format": FORMAT, "mode": "plan", "dry_run": True,
            "created_at": created.isoformat(), "database": target_descriptor(),
            "approved": approved, "approved_sha256": _sha256_text(approved),
            "allow_appliance_linked": bool(allow_appliance_linked), "appliance_linked_reason": appliance_reason.strip(),
            "entries": [{**e, "steps": [_public_step(s) for s in e["steps"]]} for e in entries],
            "summary": {"accounts": len(entries), "delete": sum(e["method"] == "delete" for e in entries),
                        "tombstone": sum(e["method"] == "tombstone" for e in entries),
                        "row_changes": sum(s["rows"] for e in entries for s in e["steps"])},
            "also_written_on_apply": "per account: 1 test_account_closures row + 1 audit_logs row; plus 1 summary audit_logs row"}
    plan["plan_sha256"] = _plan_hash(plan)
    plan["confirm_token"] = confirm_token_for(plan["plan_sha256"])
    return plan


# ------------------------------------------------------------------------------------------------------- apply
def _write_connection():
    @contextmanager
    def opened():
        if _is_sqlite() and not database_backend.sqlite_target_path().is_file():
            raise Refused(f"database file not found: {database_backend.sqlite_target_path()}")
        with database_backend.connect() as db:
            if _is_sqlite():
                db.execute("BEGIN IMMEDIATE")  # writers wait; readers (the backup) still read
            else:
                db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")  # a concurrent change aborts the run
            yield db
    return opened()


def _sqlite_backup(backup_dir: Path, plan_sha: str, expected_digests: dict) -> dict:
    """Online backup of the live SQLite file, then integrity_check and a full re-scan of the copy: it must hold
    exactly the pre-change state of every account."""
    source = database_backend.sqlite_target_path().resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest = (backup_dir / f"{source.stem}.pre-test-account-cleanup-{stamp}-{plan_sha[:12]}.db").resolve()
    if dest == source or dest.exists():
        raise Refused(f"backup target is not a new file: {dest}")
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30.0)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(dest.as_uri() + "?mode=ro", uri=True)
    check.row_factory = sqlite3.Row
    try:
        integrity = [r[0] for r in check.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise Refused(f"backup integrity_check failed: {integrity[:5]}")
        copied = {r["customer_id"]: r["state_digest"] for r in classify_all(scan(check, load_schema(check)))}
    finally:
        check.close()
    if copied != expected_digests:
        raise Refused("backup does not match the live database state")
    return {"kind": "sqlite-online-backup", "path": str(dest), "sha256": _sha256_file(dest),
            "size_bytes": dest.stat().st_size, "integrity_check": "ok", "accounts_verified": len(copied)}


def _pg_backup(path: Path, plan_created: datetime) -> dict:
    """PostgreSQL: the operator takes `pg_dump -Fc` AFTER the plan and verifies it with `pg_restore --list`
    (docs/test-account-cleanup.md); this tool only accepts such a file and records its hash."""
    if not path.is_file() or path.stat().st_size == 0:
        raise Refused(f"--backup-verified file missing or empty: {path}")
    taken = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    if taken < plan_created:
        raise Refused("--backup-verified dump is older than the plan: take a new pg_dump after planning")
    return {"kind": "pg_dump-operator-verified", "path": str(path.resolve()), "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size, "taken_at": taken.replace(microsecond=0).isoformat()}


def _fk_violations(db) -> Counter:
    return Counter((r["table"], r["parent"], r["fkid"], r["rowid"]) for r in _q(db, "PRAGMA foreign_key_check"))


def _table_counts(db, schema) -> dict:
    return {t: int(_scalar(db, f"SELECT COUNT(*) AS n FROM {t}") or 0) for t in schema}


def _others_digest(facts: dict, targets: set) -> dict:
    """Digest of every account and login user NOT in the plan: must be identical before and after."""
    accounts = {r["customer_id"]: r["state_digest"] for r in classify_all(facts) if r["customer_id"] not in targets}
    users = _sha256_text([u for u in facts["users"] if u.get("customer_id") not in targets])
    return {"accounts": accounts, "users": users}


def execute_step(db, step: dict, now_iso: str) -> None:
    if step["op"] == "delete":
        cursor = db.execute(f"DELETE FROM {step['table']} WHERE {step['_where']}", tuple(step["_params"]))
    else:
        columns = list(step["set"])
        values = [now_iso if v == APPLY_TIME else v for v in step["set"].values()]
        assignments = ",".join(f"{c}=?" for c in columns)
        cursor = db.execute(f"UPDATE {step['table']} SET {assignments} WHERE {step['_where']}",
                            tuple(values) + tuple(step["_params"]))
    if cursor.rowcount != step["rows"]:
        raise RuntimeError(f"{step['op']} {step['table']} changed {cursor.rowcount} rows, planned {step['rows']}")


def _load_plan(plan: dict, token: str, max_age_minutes: int, now: datetime) -> datetime:
    if not isinstance(plan, dict) or plan.get("tool") != TOOL or plan.get("mode") != "plan" or plan.get("format") != FORMAT:
        raise Refused("not a test_account_cleanup plan")
    if _plan_hash(plan) != plan.get("plan_sha256"):
        raise Refused("plan was modified after it was produced (plan_sha256 mismatch)")
    if token != confirm_token_for(plan["plan_sha256"]):
        raise Refused("confirm token does not match this plan")
    try:
        created = datetime.fromisoformat(plan["created_at"])
    except (KeyError, TypeError, ValueError):
        raise Refused("plan has no valid created_at")
    if created > now + timedelta(minutes=5):
        raise Refused("plan created_at is in the future")
    if now - created > timedelta(minutes=max_age_minutes):
        raise Refused(f"plan is stale: older than {max_age_minutes} minutes; produce a new plan")
    if plan.get("database") != target_descriptor():
        raise Refused("plan was produced for a different database")
    return created


def apply_plan(plan: dict, *, confirm: str, actor: str, backup_dir: Path | None = None,
               backup_verified: Path | None = None, max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
               now: datetime | None = None) -> dict:
    now = now or _utcnow()
    if not str(actor or "").strip():
        raise Refused("--actor is required (who runs this, recorded in audit_logs)")
    if _is_sqlite() and backup_dir is None:
        raise Refused("SQLite: --backup-dir is required; a verified online backup is made before any write")
    if not _is_sqlite() and backup_verified is None:
        raise Refused("PostgreSQL: refusing without --backup-verified <pg_dump file taken after the plan and checked "
                      "with pg_restore --list> (see docs/test-account-cleanup.md)")
    created = _load_plan(plan, confirm, max_age_minutes, now)
    plan_sha = plan["plan_sha256"]
    targets = {e["customer_id"] for e in plan["entries"]}
    now_iso = now.isoformat()
    with _write_connection() as db:
        schema = load_schema(db)
        if CLOSURES_TABLE not in schema:
            raise Refused(f"schema has no {CLOSURES_TABLE} table")
        done = {r["customer_id"]: r for r in _q(db, f"SELECT customer_id,plan_sha256 FROM {CLOSURES_TABLE}")}
        mine = {cid for cid in targets if cid in done and done[cid]["plan_sha256"] == plan_sha}
        if mine == targets:
            return {"status": "already_applied", "plan_sha256": plan_sha, "accounts": sorted(targets), "changes": 0}
        if mine or any(cid in done for cid in targets):
            raise Refused("some accounts of this plan are already closed by another run; produce a new plan")
        # Re-derive from live data under the write lock: any difference from the reviewed plan refuses.
        entries = derive_entries(db, schema, plan["approved"], allow_appliance_linked=plan["allow_appliance_linked"],
                                 appliance_reason=plan.get("appliance_linked_reason") or "")
        fresh = [{**e, "steps": [_public_step(s) for s in e["steps"]]} for e in entries]
        if _canonical(fresh) != _canonical(plan["entries"]):
            raise Refused("stale plan: the live data changed since the plan was made; produce a new plan")
        facts_before = scan(db, schema)
        all_before = {r["customer_id"]: r["state_digest"] for r in classify_all(facts_before)}
        backup = (_sqlite_backup(backup_dir, plan_sha, all_before) if _is_sqlite()
                  else _pg_backup(backup_verified, created))
        counts_before = _table_counts(db, schema)
        fk_before = _fk_violations(db) if _is_sqlite() else Counter()
        others_before = _others_digest(facts_before, targets)

        expected_delta = Counter()
        for entry in entries:
            for step in entry["steps"]:
                execute_step(db, step, now_iso)
                if step["op"] == "delete":
                    expected_delta[step["table"]] -= step["rows"]
            changes = dict(sum((Counter({f"{s['table']}:{s['op']}": s["rows"]}) for s in entry["steps"]), Counter()))
            details = {"tool": TOOL, "plan_sha256": plan_sha, "class": entry["class"], "method": entry["method"],
                       "email_sha256": entry["email_sha256"], "previous_status": entry["previous_status"],
                       "changes": changes, "backup_sha256": backup["sha256"],
                       "attested_test_billing_rows": entry["attested_test_billing_rows"],
                       "approved_by": plan["approved"]["approved_by"]}
            if entry["class"] == "C":
                details["appliance_linked_reason"] = plan["appliance_linked_reason"]
            db.execute(f"INSERT INTO {CLOSURES_TABLE}(customer_id,cleanup_class,method,email_sha256,plan_sha256,closed_at,"
                       "closed_by,backup_sha256,details_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (entry["customer_id"], entry["class"], entry["method"], entry["email_sha256"], plan_sha, now_iso,
                        actor, backup["sha256"], _canonical(details)))
            db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (actor, ACTOR_ROLE, f"{TOOL}.{entry['method']}", "customer",
                                                 entry["customer_id"], _canonical(details), now_iso))
        db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (actor, ACTOR_ROLE, f"{TOOL}.apply", "test_account_cleanup", plan_sha,
                                             _canonical({"plan_sha256": plan_sha, "accounts": sorted(targets),
                                                         "backup": backup, "approved_by": plan["approved"]["approved_by"]}),
                                             now_iso))
        expected_delta[CLOSURES_TABLE] += len(entries)
        expected_delta["audit_logs"] += len(entries) + 1

        # ---- post-checks, still inside the transaction: any failure rolls everything back
        counts_after = _table_counts(db, schema)
        delta = {t: counts_after[t] - counts_before[t] for t in schema if counts_after[t] != counts_before[t]}
        if delta != {t: n for t, n in expected_delta.items() if n}:
            raise RuntimeError(f"row-count check failed: changed {delta}, expected {dict(expected_delta)}")
        checks = {"row_count_deltas": delta}
        if _is_sqlite():
            new_fk = _fk_violations(db) - fk_before
            if new_fk:
                raise RuntimeError(f"foreign_key_check: new violations {sorted(new_fk)[:5]}")
            quick = [r["quick_check"] for r in _q(db, "PRAGMA quick_check")]
            if quick != ["ok"]:
                raise RuntimeError(f"quick_check failed: {quick[:5]}")
            checks.update(foreign_key_check="no new violations", quick_check="ok")
        facts_after = scan(db, schema)
        if _others_digest(facts_after, targets) != others_before:
            raise RuntimeError("an account outside the plan changed")
        checks["other_accounts_unchanged"] = len(others_before["accounts"])
        _verify_outcome(db, schema, facts_after, entries, facts_before)
        checks["outcome"] = "deleted accounts gone; tombstoned emails released and logins disabled"
    return {"status": "applied", "plan_sha256": plan_sha, "applied_at": now_iso, "actor": actor, "backup": backup,
            "accounts": [{"customer_id": e["customer_id"], "class": e["class"], "method": e["method"],
                          "rows_changed": sum(s["rows"] for s in e["steps"])} for e in entries],
            "checks": checks}


def _verify_outcome(db, schema, facts_after, entries, facts_before) -> None:
    remaining = {c["id"]: c for c in facts_after["customers"]}
    before = {c["id"]: c for c in facts_before["customers"]}
    for entry in entries:
        cid = entry["customer_id"]
        old_emails = {normalize_email(before[cid].get("email"))} | {
            normalize_email(u.get("email")) for u in facts_before["users"] if u.get("customer_id") == cid}
        old_emails.discard("")
        if entry["method"] == "delete":
            if cid in remaining:
                raise RuntimeError(f"{cid}: customer row remains after delete")
            for table, cols in schema.items():
                if table in SCAN_EXCLUDED:
                    continue
                for col in (c for c in cols if c in CUSTOMER_COLUMNS):
                    if _scalar(db, f"SELECT COUNT(*) AS n FROM {table} WHERE {col}=?", (cid,)):
                        raise RuntimeError(f"{cid}: rows remain in {table}.{col} after delete")
        else:
            row = remaining.get(cid) or {}
            if row.get("status") != CLOSED_STATUS or not is_tombstone_email(row.get("email")):
                raise RuntimeError(f"{cid}: not tombstoned")
            for u in (u for u in facts_after["users"] if u.get("customer_id") == cid):
                if not is_tombstone_email(u.get("email")) or str(u.get("account_status") or "revoked") != "revoked":
                    raise RuntimeError(f"{cid}: login user {u['id']} still usable")
        for email in old_emails:
            for table in ("customers", "partner_users"):
                if table in schema and _scalar(db, f"SELECT COUNT(*) AS n FROM {table} WHERE lower(email)=?", (email,)):
                    raise RuntimeError(f"{cid}: an old email is still held in {table}")


# --------------------------------------------------------------------------------------------------------- CLI
def _write_json(data: dict, output: Path | None) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"
    if output is None:
        sys.stdout.write(text)
    else:
        if output.exists():
            raise Refused(f"refusing to overwrite {output}")
        output.write_text(text, encoding="utf-8", newline="\n")
        print(json.dumps({"status": "written", "file": str(output), **(
            {"plan_sha256": data["plan_sha256"], "confirm_token": data["confirm_token"], "summary": data["summary"]}
            if data.get("mode") == "plan" else {"summary": data.get("summary")})}, sort_keys=True))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="test_account_cleanup.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite-path", type=Path, help="SQLite file to use instead of ANYAICAM_PARTNER_DB (e.g. a copy)")
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory", help="read-only: classify every account (P/A/B/C) as JSON")
    inv.add_argument("--approved", type=Path, help="owner-approved list: its accounts' emails are shown unmasked")
    inv.add_argument("--output", type=Path)
    pl = sub.add_parser("plan", help="read-only: dry-run plan of the exact row changes for the approved list")
    pl.add_argument("--approved", type=Path, required=True)
    pl.add_argument("--output", type=Path, required=True)
    pl.add_argument("--allow-appliance-linked", action="store_true", help="also tombstone approved class C accounts")
    pl.add_argument("--appliance-linked-reason", default="")
    ap = sub.add_parser("apply", help="apply a fresh plan (backup first, one transaction)")
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--confirm", required=True, help="the plan's confirm_token")
    ap.add_argument("--actor", required=True, help="who runs this (recorded in audit_logs)")
    ap.add_argument("--backup-dir", type=Path, help="SQLite: directory for the verified online backup")
    ap.add_argument("--backup-verified", type=Path, help="PostgreSQL: pg_dump -Fc file taken after the plan")
    ap.add_argument("--max-age-minutes", type=int, default=DEFAULT_MAX_AGE_MINUTES)
    args = parser.parse_args(argv)

    @contextmanager
    def target():
        if args.sqlite_path is not None:
            with database_backend.override_target(sqlite_path=args.sqlite_path):
                yield
        else:
            yield

    try:
        with target():
            if args.command == "inventory":
                approved = load_approved(json.loads(args.approved.read_text(encoding="utf-8"))) if args.approved else None
                _write_json(build_inventory(approved), args.output)
            elif args.command == "plan":
                plan = build_plan(json.loads(args.approved.read_text(encoding="utf-8")),
                                  allow_appliance_linked=args.allow_appliance_linked,
                                  appliance_reason=args.appliance_linked_reason)
                _write_json(plan, args.output)
            else:
                result = apply_plan(json.loads(args.plan.read_text(encoding="utf-8")), confirm=args.confirm,
                                    actor=args.actor, backup_dir=args.backup_dir, backup_verified=args.backup_verified,
                                    max_age_minutes=args.max_age_minutes)
                print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    except Refused as refusal:
        print(json.dumps({"status": "refused", "reason": refusal.reason, "details": refusal.details}, indent=2))
        return 2
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "error", "reason": f"{type(error).__name__}: {error}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
