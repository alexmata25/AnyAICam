"""Users & household (2026-10-01): a customer owner adds other people to their
own AnyAiCam account -- a spouse, a family member -- each with their own
sign-in and only the permissions the owner grants.

Built entirely on the existing tenant-scoped identity model; there is no
second authentication system:

- A household member IS a partner_users row with role customer_viewer and
  the owner's customer_id, plus an identity_grants row (exactly what the
  partner-side invite already creates), signed in through the normal
  customer login. account_status 'suspended'/'revoked' is already refused
  at sign-in by partner_db.authenticate_detailed().
- Camera permissions reuse customer_camera_permissions and the enforcement
  every route already performs: can_live (Live view), can_playback (Playback,
  recordings and Events), can_alerts (alerts and notifications), can_talk,
  can_settings (camera settings) and can_unlock (per door). Access mode is
  always 'selected', so a camera added later is never exposed until the
  owner adds it (camera_access.py's own rule).
- Account-level grants that have no per-camera home -- People / Face
  Recognition, Face Access management and Backup Mobile Access -- live in
  customer_user_permissions, one row per household member.
- Invitations reuse the invitations table with a single-use, expiring link.
  Only the token's SHA-256 is stored; the invited person chooses their own
  password, and accepting is one conditional UPDATE, so a link can never be
  used twice.

People and household members stay separate: enrolling a face never creates a
login, and inviting a household member never enrolls them in Face Access.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from appliance_protocol import RateLimiter
from partner_db import audit, connection

INVITE_TTL_DAYS = 7
MAX_PENDING_INVITATIONS = 20
MAX_SENDS_PER_INVITATION = 5
RESEND_MIN_SECONDS = 60
MIN_PASSWORD_LENGTH = 12
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Camera permissions -> their existing customer_camera_permissions column.
CAMERA_PERMISSIONS = {
    "live": "can_live",
    "playback": "can_playback",
    "alerts": "can_alerts",
    "talk": "can_talk",
    "settings": "can_settings",
}
ACCOUNT_PERMISSIONS = ("people", "face_access", "backup_access")
PERMISSION_LABELS = {
    "live": "Live view",
    "playback": "Playback, recordings and events",
    "alerts": "Alerts and notifications",
    "talk": "Talk through cameras",
    "settings": "Camera settings",
    "people": "People / Face Recognition",
    "face_access": "Manage Face Access",
    "backup_access": "Backup Mobile Access",
}
# What a new invitation grants unless the owner changes it: watch live and
# recorded video and get alerts. Everything that speaks, changes settings,
# reveals people or opens doors is off until the owner turns it on.
DEFAULT_PERMISSIONS = {"live": True, "playback": True, "alerts": True, "talk": False, "settings": False,
                       "people": False, "face_access": False, "backup_access": False}

logger = logging.getLogger(__name__)

_invite_limiter = RateLimiter(limit=20, window_seconds=3600)
_join_ip_limiter = RateLimiter(limit=20, window_seconds=900)


class HouseholdError(ValueError):
    """A request the owner (or invitee) can fix; the message is shown as-is."""


def _now() -> datetime:
    return datetime.now()


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ permissions

def customer_cameras(db, customer_id: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT id,name,door_access_enabled FROM cameras WHERE customer_id=? ORDER BY camera_number,name",
        (customer_id,),
    ).fetchall()]


def normalize_permissions(db, customer_id: str, payload: dict | None) -> dict:
    """Validates a permission set against THIS customer's own cameras.

    Anything naming another tenant's camera is dropped, never trusted; doors
    must be door-enabled cameras the person can also see (an unlock control
    lives on a camera they can open)."""
    payload = payload if isinstance(payload, dict) else {}
    cameras = customer_cameras(db, customer_id)
    own = {camera["id"] for camera in cameras}
    doors_available = {camera["id"] for camera in cameras if camera["door_access_enabled"]}
    if payload.get("all_cameras"):
        camera_ids = sorted(own)
    else:
        camera_ids = sorted({str(item) for item in payload.get("camera_ids", []) or []} & own)
    flags = {}
    for key in (*CAMERA_PERMISSIONS, *ACCOUNT_PERMISSIONS):
        flags[key] = bool(payload.get(key, DEFAULT_PERMISSIONS[key]))
    door_ids = sorted({str(item) for item in payload.get("door_ids", []) or []} & doors_available & set(camera_ids))
    return {"camera_ids": camera_ids, "door_ids": door_ids, **flags}


def apply_permissions(db, *, customer_id: str, user_id: str, permissions: dict, granted_by: str, now: str) -> None:
    """Replaces this household member's whole permission set in one place."""
    db.execute("UPDATE partner_users SET camera_access_mode='selected',authorization_version=COALESCE(authorization_version,1)+1 "
               "WHERE id=? AND customer_id=? AND role='customer_viewer'", (user_id, customer_id))
    db.execute("DELETE FROM customer_camera_permissions WHERE user_id=?", (user_id,))
    doors = set(permissions["door_ids"])
    for camera_id in permissions["camera_ids"]:
        db.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_download,can_share,"
            "can_alerts,can_settings,can_talk,can_unlock) VALUES(?,?,?,?,0,0,?,?,?,?)",
            (user_id, camera_id, int(permissions["live"]), int(permissions["playback"]), int(permissions["alerts"]),
             int(permissions["settings"]), int(permissions["talk"]), int(camera_id in doors)),
        )
    db.execute(
        "INSERT INTO customer_user_permissions(user_id,customer_id,can_people,can_face_access,can_backup_access,updated_at,updated_by) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET customer_id=excluded.customer_id,can_people=excluded.can_people,"
        "can_face_access=excluded.can_face_access,can_backup_access=excluded.can_backup_access,updated_at=excluded.updated_at,"
        "updated_by=excluded.updated_by",
        (user_id, customer_id, int(permissions["people"]), int(permissions["face_access"]), int(permissions["backup_access"]),
         now, granted_by),
    )


def read_permissions(db, *, customer_id: str, user_id: str) -> dict:
    rows = db.execute(
        "SELECT p.* FROM customer_camera_permissions p JOIN cameras c ON c.id=p.camera_id "
        "WHERE p.user_id=? AND c.customer_id=?", (user_id, customer_id),
    ).fetchall()
    account = db.execute("SELECT * FROM customer_user_permissions WHERE user_id=? AND customer_id=?",
                         (user_id, customer_id)).fetchone()
    first = dict(rows[0]) if rows else {}
    permissions = {key: bool(first.get(column)) for key, column in CAMERA_PERMISSIONS.items()}
    permissions.update({key: bool(account and account[f"can_{key}"]) for key in ACCOUNT_PERMISSIONS})
    permissions["camera_ids"] = sorted(row["camera_id"] for row in rows)
    permissions["door_ids"] = sorted(row["camera_id"] for row in rows if row["can_unlock"])
    return permissions


def _lookup():
    """Read-only lookups made while rendering ordinary pages (navigation,
    People gates). Uses the plain connection -- never partner_db.connection(),
    which initializes and caches the current database target -- so merely
    showing a page can never be the first thing to initialize a database.
    The app initializes its database at startup; a caller that finds no
    household schema falls back to the unchanged role behaviour."""
    from database_backend import connect
    return connect()


def account_permission(identity: dict | None, key: str) -> bool:
    """People / Face Access / Backup Mobile Access for this signed-in person.

    An owner always has them. A household member has exactly what the owner
    granted. A customer_viewer with no household record (created before Users
    & household existed, e.g. by a partner) keeps exactly the role behaviour
    it always had: People view, nothing more."""
    if not identity:
        return False
    role = identity.get("role")
    if role == "customer_owner":
        return True
    if role != "customer_viewer":
        return False
    if not identity.get("email") or not identity.get("customer_id"):
        # Not a signed-in household member (no account to look up): the
        # unchanged customer_viewer role behaviour, People view only.
        return key == "people"
    try:
        with _lookup() as db:
            user = db.execute("SELECT id FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?",
                              (identity.get("email", ""), identity.get("customer_id"))).fetchone()
            grant = (db.execute("SELECT * FROM customer_user_permissions WHERE user_id=?", (user["id"],)).fetchone()
                     if user else None)
    except Exception:  # no household schema here: unchanged role behaviour
        return key == "people"
    if grant is None:
        return key == "people"
    return bool(grant[f"can_{key}"])


def facial_permission_allowed(identity: dict | None, permission: str) -> bool:
    """The household layer on top of the role permission for facial.* routes:
    a household member sees People only with the People or Face Access grant,
    and manages Face Access only with the Face Access grant."""
    if not identity or identity.get("role") != "customer_viewer":
        return True
    if permission == "facial.manage":
        return account_permission(identity, "face_access")
    if permission == "facial.view":
        return account_permission(identity, "people") or account_permission(identity, "face_access")
    return True


def unlockable_door_ids(identity: dict) -> set[str] | None:
    """None = every door (owner). Otherwise the doors this person may unlock."""
    if identity.get("role") == "customer_owner":
        return None
    try:
        with _lookup() as db:
            return {row["camera_id"] for row in db.execute(
                "SELECT p.camera_id FROM customer_camera_permissions p JOIN partner_users u ON u.id=p.user_id "
                "JOIN cameras c ON c.id=p.camera_id WHERE lower(u.email)=lower(?) AND u.customer_id=? AND c.customer_id=? "
                "AND p.can_unlock=1", (identity.get("email", ""), identity.get("customer_id"), identity.get("customer_id")),
            ).fetchall()}
    except Exception:  # no schema to check: no doors (fail closed)
        return set()


def restrict_face_access_payload(before: dict, payload: dict, manageable: set[str] | None) -> dict:
    """What a Face Access save may change for this person (None = owner, all).

    A household member with the Face Access grant manages door grants only
    on doors they may unlock themselves: every other door keeps exactly the
    grant it already had (they can neither add nor remove it). The person's
    account-wide switches (Face Access on/off, start/expiration dates, unit,
    site) affect every door, so they are kept as-is unless this member may
    unlock every door on the account."""
    if manageable is None:
        return payload
    restricted = dict(payload)
    all_doors = {door["camera_id"] for door in before.get("doors", [])}
    requested = {str(item.get("camera_id")): item for item in payload.get("doors") or [] if isinstance(item, dict)}
    doors = []
    for door in before.get("doors", []):
        camera_id = door["camera_id"]
        source = requested.get(camera_id, {"camera_id": camera_id, "allowed": False}) if camera_id in manageable else door
        doors.append({"camera_id": camera_id, "allowed": bool(source.get("allowed")), "start": source.get("start"),
                      "end": source.get("end"), "days": source.get("days")})
    restricted["doors"] = doors
    if not all_doors <= manageable:
        for key in ("access_enabled", "access_starts_on", "access_expires_on", "unit", "site_id"):
            restricted[key] = before.get(key)
    return restricted


# ------------------------------------------------------------------ invitations

def _invitation_status(row: dict, now: str) -> str:
    status = row.get("status") or ""
    if status == "pending" and (row.get("expires_at") or "") < now:
        return "expired"
    return status


def public_base_url(request: Request | None) -> str:
    configured = os.environ.get("ANYAICAM_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    if request is not None:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        return f"{scheme}://{request.headers.get('host') or request.url.netloc}"
    return ""


def join_link(base: str, raw: str) -> str:
    return f"{base}/customer/join?token={raw}"


def _send_invitation_email(*, to: str, name: str, owner_name: str, link: str, expires_at: str) -> str:
    import email_layout
    from email_service import get_email_service
    when = datetime.fromisoformat(expires_at).strftime("%B %d, %Y").replace(" 0", " ")
    inviter = owner_name or "The account owner"
    greeting = f"Hi {name}," if name else "Hi,"
    subject = f"{inviter} invited you to their AnyAiCam account"
    text = (f"{greeting}\n\n{inviter} invited you to their AnyAiCam security account, with your own sign-in.\n\n"
            f"Create your password with this link (it works once and expires on {when}):\n{link}\n\n"
            "If you weren't expecting this, you can ignore this email.\n\n— The AnyAiCam Team")
    body = (
        '<h2 style="margin:0 0 10px;font-size:20px">You\'re invited to AnyAiCam</h2>'
        f"<p>{escape(greeting)}</p>"
        f"<p>{escape(inviter)} invited you to their AnyAiCam security account, with your own sign-in.</p>"
        f'<p style="margin:18px 0">{email_layout.button("Accept invitation", link)}</p>'
        f'<p style="color:#475467;font-size:13px">This link works once and expires on {escape(when)}. If the button doesn\'t work, '
        f'copy this address into your browser:<br><span style="word-break:break-all">{escape(link)}</span></p>'
        '<p style="color:#475467;font-size:13px">If you weren\'t expecting this, you can ignore this email.</p>'
    )
    try:
        result = get_email_service().send("invitation", to, subject, text,
                                          html=email_layout.wrap(body, preheader=f"{inviter} invited you to AnyAiCam."),
                                          metadata={"kind": "household", "expires_at": expires_at})
    except Exception:  # the invitation is saved either way; the owner can resend
        logger.exception("household.invitation_email_failed")
        return "failed"
    return str((result or {}).get("status") or "failed")


def create_invitation(db, *, owner: dict, name: str, email: str, permissions: dict, now: datetime) -> tuple[str, str]:
    """Returns (invitation_id, raw_token). Raises HouseholdError."""
    customer_id = owner["customer_id"]
    name = (name or "").strip()[:120]
    email = (email or "").strip().lower()
    if not name:
        raise HouseholdError("Enter the person's name.")
    if len(email) > 254 or not _EMAIL.match(email):
        raise HouseholdError("Enter a valid email address.")
    existing = db.execute("SELECT id,customer_id,role,account_status FROM partner_users WHERE lower(email)=?", (email,)).fetchone()
    if existing:
        same_household = existing["customer_id"] == customer_id and existing["role"] == "customer_viewer"
        if not (same_household and (existing["account_status"] or "active") in ("suspended", "revoked")):
            # Deliberately the same message for "already in your household",
            # "owns another account" and "is a partner": an owner learns
            # nothing about who else uses AnyAiCam.
            raise HouseholdError("This email can't be invited. Use a different email address.")
    pending = db.execute("SELECT COUNT(*) AS n FROM invitations WHERE customer_id=? AND role='customer_viewer' AND status='pending' "
                         "AND expires_at>=?", (customer_id, now.isoformat())).fetchone()["n"]
    if pending >= MAX_PENDING_INVITATIONS:
        raise HouseholdError("Too many invitations are waiting. Cancel one before sending another.")
    # One live invitation per person: a new one replaces any earlier link.
    db.execute("UPDATE invitations SET status='cancelled',cancelled_at=? WHERE customer_id=? AND lower(email)=? "
               "AND role='customer_viewer' AND status='pending'", (now.isoformat(), customer_id, email))
    raw = secrets.token_urlsafe(32)
    invitation_id = secrets.token_hex(8)
    expires_at = (now + timedelta(days=INVITE_TTL_DAYS)).isoformat()
    db.execute(
        "INSERT INTO invitations(id,email,role,customer_id,status,expires_at,created_at,created_by,name,token_hash,permissions_json,"
        "last_sent_at,send_count) VALUES(?,?,'customer_viewer',?,'pending',?,?,?,?,?,?,?,1)",
        (invitation_id, email, customer_id, expires_at, now.isoformat(), owner["email"], name, token_hash(raw),
         json.dumps(permissions), now.isoformat()),
    )
    return invitation_id, raw


def _own_invitation(db, customer_id: str, invitation_id: str) -> dict:
    row = db.execute("SELECT * FROM invitations WHERE id=? AND customer_id=? AND role='customer_viewer' AND token_hash IS NOT NULL",
                     (invitation_id, customer_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    return dict(row)


def resend_invitation(db, *, customer_id: str, invitation_id: str, now: datetime) -> tuple[dict, str]:
    invitation = _own_invitation(db, customer_id, invitation_id)
    if invitation["status"] not in ("pending",):
        raise HouseholdError("Only a waiting or expired invitation can be sent again.")
    if int(invitation.get("send_count") or 0) >= MAX_SENDS_PER_INVITATION:
        raise HouseholdError("This invitation has been sent the maximum number of times. Cancel it and invite again.")
    last = invitation.get("last_sent_at")
    if last and (now - datetime.fromisoformat(last)).total_seconds() < RESEND_MIN_SECONDS:
        raise HouseholdError("Please wait a minute before sending it again.")
    raw = secrets.token_urlsafe(32)  # the earlier link stops working
    expires_at = (now + timedelta(days=INVITE_TTL_DAYS)).isoformat()
    db.execute("UPDATE invitations SET token_hash=?,expires_at=?,last_sent_at=?,send_count=COALESCE(send_count,1)+1 WHERE id=?",
               (token_hash(raw), expires_at, now.isoformat(), invitation_id))
    invitation["expires_at"] = expires_at
    return invitation, raw


def cancel_invitation(db, *, customer_id: str, invitation_id: str, now: datetime) -> None:
    invitation = _own_invitation(db, customer_id, invitation_id)
    if invitation["status"] != "pending":
        raise HouseholdError("This invitation is no longer waiting.")
    db.execute("UPDATE invitations SET status='cancelled',cancelled_at=? WHERE id=? AND status='pending'", (now.isoformat(), invitation_id))


def invitation_for_token(db, raw: str, now: datetime) -> dict | None:
    """The live invitation this link belongs to, or None (expired, used,
    cancelled, unknown -- all deliberately indistinguishable)."""
    if not raw or len(raw) > 200:
        return None
    row = db.execute(
        "SELECT i.*,c.name AS customer_name,c.status AS customer_status FROM invitations i JOIN customers c ON c.id=i.customer_id "
        "WHERE i.token_hash=? AND i.role='customer_viewer' AND i.status='pending' AND i.expires_at>=?",
        (token_hash(raw), now.isoformat()),
    ).fetchone()
    if not row or (row["customer_status"] or "active") not in ("active", "trial", "pending_installation"):
        return None
    return dict(row)


def accept_invitation(db, *, raw: str, password: str, now: datetime) -> dict:
    """Creates (or re-admits) the household member and consumes the link.
    One transaction: if anything fails, the link still works."""
    from partner_db import password_hash
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HouseholdError(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters.")
    invitation = invitation_for_token(db, raw, now)
    if not invitation:
        raise HouseholdError("This invitation link is invalid, expired, or already used.")
    consumed = db.execute("UPDATE invitations SET status='accepted',accepted_at=? WHERE id=? AND status='pending' AND token_hash=?",
                          (now.isoformat(), invitation["id"], token_hash(raw))).rowcount
    if consumed != 1:
        raise HouseholdError("This invitation link is invalid, expired, or already used.")
    customer_id = invitation["customer_id"]
    email = invitation["email"]
    existing = db.execute("SELECT * FROM partner_users WHERE lower(email)=?", (email,)).fetchone()
    if existing and not (existing["customer_id"] == customer_id and existing["role"] == "customer_viewer"
                         and (existing["account_status"] or "active") in ("suspended", "revoked")):
        raise HouseholdError("This invitation link is invalid, expired, or already used.")
    owner = db.execute("SELECT partner_id FROM customers WHERE id=?", (customer_id,)).fetchone()
    if existing:
        user_id = existing["id"]
        db.execute("UPDATE partner_users SET name=?,password_hash=?,approved=1,account_status='active',must_change_password=0,"
                   "authorization_version=COALESCE(authorization_version,1)+1 WHERE id=?",
                   (invitation["name"] or existing["name"], password_hash(password), user_id))
    else:
        user_id = secrets.token_hex(16)
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,"
                   "account_status,must_change_password,camera_access_mode) VALUES(?,?,?,?,'customer_viewer',?,1,?,?,'active',0,'selected')",
                   (user_id, (owner and owner["partner_id"]) or "anyaicam-primary", email, invitation["name"],
                    password_hash(password), customer_id, now.isoformat()))
    from appliance_identity import create_grant
    db.execute("UPDATE identity_grants SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now.isoformat(), user_id))
    create_grant(db, user_id=user_id, role="customer_viewer", scope_type="customer", scope_id=customer_id,
                 granted_by=f"household_invitation:{invitation['id']}", now=now.isoformat())
    permissions = normalize_permissions(db, customer_id, json.loads(invitation.get("permissions_json") or "{}"))
    apply_permissions(db, customer_id=customer_id, user_id=user_id, permissions=permissions,
                      granted_by=invitation.get("created_by") or "", now=now.isoformat())
    db.execute("UPDATE invitations SET accepted_user_id=? WHERE id=?", (user_id, invitation["id"]))
    return {"user_id": user_id, "email": email, "customer_id": customer_id, "invitation_id": invitation["id"]}


# ------------------------------------------------------------------ members

def _own_member(db, customer_id: str, user_id: str) -> dict:
    row = db.execute("SELECT * FROM partner_users WHERE id=? AND customer_id=? AND role='customer_viewer'",
                     (user_id, customer_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    return dict(row)


def _cut_off_access(db, user_id: str, now: str) -> None:
    """Signs the person out everywhere, now: browser sessions, the appliance's
    cloud-delegated login, and any cached authorization."""
    db.execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, user_id))
    db.execute("UPDATE identity_grants SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, user_id))
    db.execute("UPDATE partner_users SET authorization_version=COALESCE(authorization_version,1)+1 WHERE id=?", (user_id,))


def set_member_state(db, *, customer_id: str, user_id: str, action: str, actor: str, now: datetime) -> dict:
    member = _own_member(db, customer_id, user_id)
    stamp = now.isoformat()
    if action == "disable":
        db.execute("UPDATE partner_users SET account_status='suspended' WHERE id=?", (user_id,))
        _cut_off_access(db, user_id, stamp)
    elif action == "enable":
        if (member["account_status"] or "active") != "suspended":
            raise HouseholdError("Only a disabled user can be turned back on.")
        from appliance_identity import create_grant
        db.execute("UPDATE partner_users SET account_status='active',authorization_version=COALESCE(authorization_version,1)+1 WHERE id=?", (user_id,))
        create_grant(db, user_id=user_id, role="customer_viewer", scope_type="customer", scope_id=customer_id,
                     granted_by=actor, now=stamp)
    elif action == "remove":
        db.execute("UPDATE partner_users SET account_status='revoked' WHERE id=?", (user_id,))
        db.execute("DELETE FROM customer_camera_permissions WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM customer_user_permissions WHERE user_id=?", (user_id,))
        _cut_off_access(db, user_id, stamp)
    else:
        raise HouseholdError("Unknown action.")
    return member


def household_overview(db, customer_id: str, now: datetime) -> dict:
    stamp = now.isoformat()
    users = []
    for row in db.execute("SELECT id,email,name,role,account_status,created_at FROM partner_users WHERE customer_id=? "
                          "AND role IN ('customer_owner','customer_viewer') AND COALESCE(account_status,'active')!='revoked' "
                          "ORDER BY CASE role WHEN 'customer_owner' THEN 0 ELSE 1 END,lower(COALESCE(name,email))", (customer_id,)).fetchall():
        item = dict(row)
        item["status"] = "disabled" if (item.pop("account_status") or "active") == "suspended" else "active"
        if item["role"] == "customer_viewer":
            item["permissions"] = read_permissions(db, customer_id=customer_id, user_id=item["id"])
        users.append(item)
    invitations = []
    for row in db.execute("SELECT id,email,name,status,expires_at,created_at,accepted_at,cancelled_at,last_sent_at,send_count "
                          "FROM invitations WHERE customer_id=? AND role='customer_viewer' AND token_hash IS NOT NULL "
                          "ORDER BY created_at DESC LIMIT 50", (customer_id,)).fetchall():
        item = dict(row)
        item["status"] = _invitation_status(item, stamp)
        if item["status"] in ("pending", "expired"):
            invitations.append(item)
    cameras = customer_cameras(db, customer_id)
    return {"users": users, "invitations": invitations,
            "cameras": [{"id": c["id"], "name": c["name"], "door": bool(c["door_access_enabled"])} for c in cameras],
            "permission_labels": PERMISSION_LABELS, "default_permissions": DEFAULT_PERMISSIONS}


# ------------------------------------------------------------------ routes

def _delivery_message(status: str, email: str, *, first: bool) -> str:
    if status == "sent":
        return f"Invitation sent to {email}." if first else f"Invitation sent again to {email}."
    if status == "preview":  # email preview mode (development/staging without SMTP): nothing left the server
        return (f"Invitation created for {email} (email preview mode: nothing was emailed)." if first
                else f"New invitation link created for {email} (email preview mode: nothing was emailed).")
    return (f"Invitation created for {email}, but the email could not be sent. Use Resend to try again." if first
            else "The email could not be sent. Please try again.")


def _owner(request: Request) -> dict:
    from partner_portal import partner_identity
    identity = partner_identity(request)
    if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
        raise HTTPException(status_code=403, detail="Only the account owner can manage users.")
    return identity


def _owner_name(db, identity: dict) -> str:
    row = db.execute("SELECT name FROM partner_users WHERE lower(email)=lower(?)", (identity["email"],)).fetchone()
    return (row and row["name"]) or ""


def register_household_routes(app: FastAPI, page_shell) -> None:
    @app.get("/api/customer/household")
    def household(request: Request) -> dict:
        identity = _owner(request)
        with connection() as db:
            return household_overview(db, identity["customer_id"], _now())

    @app.post("/api/customer/household/invitations")
    def invite(request: Request, payload: dict) -> dict:
        identity = _owner(request)
        if not _invite_limiter.allow(identity["customer_id"]):
            raise HTTPException(status_code=429, detail="Too many invitations. Please try again later.")
        now = _now()
        try:
            with connection() as db:
                permissions = normalize_permissions(db, identity["customer_id"], payload.get("permissions"))
                invitation_id, raw = create_invitation(db, owner=identity, name=str(payload.get("name", "")),
                                                       email=str(payload.get("email", "")), permissions=permissions, now=now)
                owner_name = _owner_name(db, identity)
        except HouseholdError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        email = str(payload.get("email", "")).strip().lower()
        status = _send_invitation_email(to=email, name=str(payload.get("name", "")).strip(), owner_name=owner_name,
                                        link=join_link(public_base_url(request), raw),
                                        expires_at=(now + timedelta(days=INVITE_TTL_DAYS)).isoformat())
        audit(identity, "household.invited", "invitation", invitation_id, {"email_status": status, "permissions": permissions})
        message = _delivery_message(status, email, first=True)
        return {"message": message, "invitation_id": invitation_id, "email_status": status}

    @app.post("/api/customer/household/invitations/{invitation_id}/resend")
    def resend(request: Request, invitation_id: str) -> dict:
        identity = _owner(request)
        now = _now()
        try:
            with connection() as db:
                invitation, raw = resend_invitation(db, customer_id=identity["customer_id"], invitation_id=invitation_id, now=now)
                owner_name = _owner_name(db, identity)
        except HouseholdError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        status = _send_invitation_email(to=invitation["email"], name=invitation.get("name") or "", owner_name=owner_name,
                                        link=join_link(public_base_url(request), raw), expires_at=invitation["expires_at"])
        audit(identity, "household.invitation_resent", "invitation", invitation_id, {"email_status": status})
        return {"message": _delivery_message(status, invitation["email"], first=False), "email_status": status}

    @app.post("/api/customer/household/invitations/{invitation_id}/cancel")
    def cancel(request: Request, invitation_id: str) -> dict:
        identity = _owner(request)
        try:
            with connection() as db:
                cancel_invitation(db, customer_id=identity["customer_id"], invitation_id=invitation_id, now=_now())
        except HouseholdError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        audit(identity, "household.invitation_cancelled", "invitation", invitation_id)
        return {"message": "Invitation cancelled. The link no longer works."}

    @app.get("/api/customer/household/users/{user_id}/permissions")
    def get_member_permissions(request: Request, user_id: str) -> dict:
        identity = _owner(request)
        with connection() as db:
            _own_member(db, identity["customer_id"], user_id)
            return read_permissions(db, customer_id=identity["customer_id"], user_id=user_id)

    @app.put("/api/customer/household/users/{user_id}/permissions")
    def put_member_permissions(request: Request, user_id: str, payload: dict) -> dict:
        identity = _owner(request)
        now = _now().isoformat()
        with connection() as db:
            member = _own_member(db, identity["customer_id"], user_id)
            permissions = normalize_permissions(db, identity["customer_id"], payload)
            apply_permissions(db, customer_id=identity["customer_id"], user_id=user_id, permissions=permissions,
                              granted_by=identity["email"], now=now)
        audit(identity, "household.permissions_changed", "partner_user", user_id, permissions)
        return {"message": f"Permissions saved for {member['name'] or member['email']}.", "permissions": permissions}

    for action, verb in (("disable", "disabled"), ("enable", "turned back on"), ("remove", "removed")):
        def _make(action=action, verb=verb):
            def handler(request: Request, user_id: str) -> dict:
                identity = _owner(request)
                try:
                    with connection() as db:
                        member = set_member_state(db, customer_id=identity["customer_id"], user_id=user_id, action=action,
                                                  actor=identity["email"], now=_now())
                except HouseholdError as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error
                audit(identity, f"household.user_{action}d" if action != "remove" else "household.user_removed",
                      "partner_user", user_id)
                return {"message": f"{member['name'] or member['email']} was {verb}."}
            return handler
        app.add_api_route(f"/api/customer/household/users/{{user_id}}/{action}", _make(), methods=["POST"])

    @app.post("/api/customer/household/join")
    def join(request: Request, payload: dict) -> dict:
        client_ip = request.client.host if request.client else "unknown"
        if not _join_ip_limiter.allow(client_ip):
            raise HTTPException(status_code=429, detail="Too many attempts. Please wait a few minutes and try again.")
        password, confirm = str(payload.get("password", "")), str(payload.get("confirm_password", payload.get("password", "")))
        if password != confirm:
            raise HTTPException(status_code=400, detail="The two passwords don't match.")
        try:
            with connection() as db:
                joined = accept_invitation(db, raw=str(payload.get("token", "")), password=password, now=_now())
        except HouseholdError as error:
            audit({"email": "unknown", "role": "anonymous"}, "household.join_failed", "invitation", "", {"reason": str(error)[:80]})
            raise HTTPException(status_code=400, detail=str(error)) from error
        audit({"email": joined["email"], "role": "customer_viewer"}, "household.joined", "invitation", joined["invitation_id"],
              {"user_id": joined["user_id"]})
        return {"message": "Your account is ready. Sign in with your email and new password.", "destination": "/customer-login.html"}

    @app.get("/customer/join", response_class=HTMLResponse)
    def join_page(request: Request, token: str = ""):
        with connection() as db:
            invitation = invitation_for_token(db, token, _now())
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        return HTMLResponse(_join_page_html(invitation, token), headers=headers)

    @app.get("/customer/household", response_class=HTMLResponse)
    def household_page(request: Request):
        from partner_portal import partner_identity
        identity = partner_identity(request)
        if not identity:
            return RedirectResponse("/customer-login.html", status_code=303)
        if identity.get("role") != "customer_owner":
            content = ('<header class="topbar"><div><p class="eyebrow">Account</p><h1>Users &amp; household</h1></div></header>'
                       '<section class="panel"><p>Only the account owner can add or change users on this account.</p></section>')
            return HTMLResponse(page_shell("Users & household", "household", content), status_code=403)
        return HTMLResponse(page_shell("Users & household", "household", _HOUSEHOLD_CONTENT, _HOUSEHOLD_SCRIPT))


def _join_page_html(invitation: dict | None, token: str) -> str:
    from cloud_features import CUSTOMER_AUTH_STYLE as _CUSTOMER_AUTH_STYLE
    if invitation:
        who = escape(invitation.get("name") or "")
        body = (f'<h2>Join {escape(invitation.get("customer_name") or "an AnyAiCam account")}</h2>'
                f'<p>Welcome{", " + who if who else ""}. Create a password for <strong>{escape(invitation["email"])}</strong>. '
                'You\'ll sign in with your own email and password.</p>'
                f'<form id="join-form"><input id="join-token" type="hidden" value="{escape(token, quote=True)}">'
                f'<label>New password<input id="join-password" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>'
                f'<label>Confirm password<input id="join-confirm" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>'
                f'<p style="margin:0;color:#4b5873;font-size:14px">At least {MIN_PASSWORD_LENGTH} characters.</p>'
                '<div id="message" class="message" role="status"></div><button class="submit">Create my account</button></form>')
    else:
        body = ('<h2>This invitation can\'t be used</h2><p>The link is invalid, has expired, or was already used. '
                'Ask the account owner to send you a new invitation.</p>')
    script = ("const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';"
              "let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='\"'&&v[v.length-1]==='\"'?v.slice(1,-1):v};"
              "const form=document.getElementById('join-form');if(form)form.addEventListener('submit',async e=>{e.preventDefault();"
              "const msg=document.getElementById('message'),btn=form.querySelector('button');btn.disabled=true;"
              "const r=await fetch('/api/customer/household/join',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},"
              "body:JSON.stringify({token:document.getElementById('join-token').value,password:document.getElementById('join-password').value,"
              "confirm_password:document.getElementById('join-confirm').value})}),b=await r.json().catch(()=>({}));"
              "msg.style.display='block';msg.style.background=r.ok?'#e7f7ee':'#ffe8ec';msg.style.color=r.ok?'#14532d':'#8b1730';"
              "msg.textContent=b.message||b.detail||'Something went wrong. Please try again.';"
              "if(r.ok){form.querySelectorAll('input').forEach(i=>i.disabled=true);setTimeout(()=>location.href=b.destination||'/customer-login.html',1500)}else btn.disabled=false});")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><title>Join AnyAiCam | ANY AI CAM</title>'
            f'<style>{_CUSTOMER_AUTH_STYLE}</style></head><body>'
            '<header class="head"><a class="brand" href="/customer-login.html"><img src="/static/brand-icon.png" alt="AnyAiCam">ANY AI CAM</a></header>'
            f'<main class="auth-wrap"><section class="card">{body}<a class="back-link" href="/customer-login.html">Go to sign in</a></section></main>'
            f'<script>{script}</script></body></html>')


_HOUSEHOLD_CONTENT = '''<header class="topbar"><div><p class="eyebrow">Account</p><h1>Users &amp; household</h1></div>
<button class="action-button" id="hh-add" type="button">Add user</button></header>
<p class="health-detail" style="margin-top:-6px">Give the people you live or work with their own sign-in. Each person only sees what you allow.</p>
<section class="panel"><h3 style="margin-top:0">Users</h3><div id="hh-users" class="health-detail">Loading…</div></section>
<section class="panel" style="margin-top:14px"><h3 style="margin-top:0">Waiting invitations</h3><div id="hh-invites" class="health-detail">Loading…</div></section>
<p id="hh-message" class="health-detail" role="status" aria-live="polite"></p>
<dialog id="hh-dialog" style="width:min(560px,calc(100vw - 32px));border:0;border-radius:14px;padding:0;background:#18213a;color:#eef2f6">
<style>#hh-form input:not([type=checkbox]){padding:10px 12px;border:1px solid #4a5675;border-radius:8px;background:#0f1628;color:#eef2f6;font:inherit}#hh-form label{color:#eef2f6}#hh-form legend{padding:0 6px;font-weight:700}</style><form id="hh-form" method="dialog" style="display:grid;gap:12px;padding:20px">
<h2 id="hh-title" style="margin:0">Add user</h2>
<div id="hh-identity" style="display:grid;gap:10px">
<label style="display:grid;gap:4px">Name<input id="hh-name" autocomplete="name" maxlength="120"></label>
<label style="display:grid;gap:4px">Email<input id="hh-email" type="email" autocomplete="email" maxlength="254"></label></div>
<fieldset style="border:1px solid #3a4560;border-radius:10px;padding:10px"><legend>Cameras</legend>
<label><input type="checkbox" id="hh-all"> All current cameras</label><div id="hh-cameras" style="display:grid;gap:4px;margin-top:6px"></div>
<p class="health-detail" style="margin:6px 0 0">Cameras you add later stay hidden until you add them here.</p></fieldset>
<fieldset style="border:1px solid #3a4560;border-radius:10px;padding:10px"><legend>What they can do</legend><div id="hh-perms" style="display:grid;gap:4px"></div></fieldset>
<fieldset id="hh-doors-box" style="border:1px solid #3a4560;border-radius:10px;padding:10px"><legend>Doors they can unlock</legend><div id="hh-doors" style="display:grid;gap:4px"></div></fieldset>
<p id="hh-form-message" class="health-detail" role="alert"></p>
<div style="display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap"><button class="ghost-button" type="button" id="hh-cancel">Cancel</button>
<button class="action-button" type="submit" id="hh-submit">Send invitation</button></div></form></dialog>'''

_HOUSEHOLD_SCRIPT = r'''<script>
(()=>{
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=v=>v?new Date(v).toLocaleString([], {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'}):'';
const PERMS=['live','playback','alerts','talk','settings','people','face_access','backup_access'];
let data=null,editing=null;
const say=(t,ok=true)=>{const m=$('hh-message');m.textContent=t;m.style.color=ok?'':'#ffb4c0'};
async function call(url,method='POST',body){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(b.detail||'Something went wrong.');return b}
function permSummary(p){if(!p)return '';const on=PERMS.filter(k=>p[k]).map(k=>data.permission_labels[k]);const cams=p.camera_ids.length;return `${cams} camera${cams===1?'':'s'}`+(on.length?' · '+on.join(', '):'')+(p.door_ids.length?` · unlock ${p.door_ids.length} door${p.door_ids.length===1?'':'s'}`:'')}
function render(){
 $('hh-users').innerHTML=data.users.map(u=>`<div class="health-row" style="align-items:flex-start;gap:12px;flex-wrap:wrap"><span><strong>${esc(u.name||u.email)}</strong>${u.role==='customer_owner'?' <span class="pill">Owner</span>':''}${u.status==='disabled'?' <span class="pill" style="background:#5c3140;color:#ffd0da">Disabled</span>':''}<br><span class="health-detail">${esc(u.email)}</span>${u.permissions?`<br><span class="health-detail">${esc(permSummary(u.permissions))}</span>`:''}</span>`+
 (u.role==='customer_viewer'?`<span style="display:flex;gap:8px;flex-wrap:wrap"><button class="ghost-button" data-edit="${esc(u.id)}">Permissions</button>`+(u.status==='disabled'?`<button class="ghost-button" data-act="enable" data-id="${esc(u.id)}">Turn on</button>`:`<button class="ghost-button" data-act="disable" data-id="${esc(u.id)}">Disable</button>`)+`<button class="ghost-button" data-act="remove" data-id="${esc(u.id)}">Remove</button></span>`:'')+`</div>`).join('')||'No users yet.';
 $('hh-invites').innerHTML=data.invitations.map(i=>`<div class="health-row" style="gap:12px;flex-wrap:wrap"><span><strong>${esc(i.name||i.email)}</strong> <span class="pill">${i.status==='expired'?'Expired':'Waiting'}</span><br><span class="health-detail">${esc(i.email)} · ${i.status==='expired'?'expired':'expires'} ${esc(when(i.expires_at))}</span></span><span style="display:flex;gap:8px"><button class="ghost-button" data-inv="resend" data-id="${esc(i.id)}">Resend</button><button class="ghost-button" data-inv="cancel" data-id="${esc(i.id)}">Cancel</button></span></div>`).join('')||'No invitations waiting.';
}
async function load(){try{data=await call('/api/customer/household','GET');render()}catch(e){$('hh-users').textContent=e.message}}
function fill(p){
 $('hh-cameras').innerHTML=data.cameras.map(c=>`<label><input type="checkbox" class="hh-cam" value="${esc(c.id)}" ${p.camera_ids.includes(c.id)?'checked':''}> ${esc(c.name)}</label>`).join('')||'<span class="health-detail">No cameras yet.</span>';
 $('hh-all').checked=data.cameras.length>0&&data.cameras.every(c=>p.camera_ids.includes(c.id));
 $('hh-perms').innerHTML=PERMS.map(k=>`<label><input type="checkbox" class="hh-perm" value="${k}" ${p[k]?'checked':''}> ${esc(data.permission_labels[k])}</label>`).join('');
 const doors=data.cameras.filter(c=>c.door);$('hh-doors-box').hidden=!doors.length;
 $('hh-doors').innerHTML=doors.map(c=>`<label><input type="checkbox" class="hh-door" value="${esc(c.id)}" ${p.door_ids.includes(c.id)?'checked':''}> ${esc(c.name)}</label>`).join('');
}
function collect(){const p={camera_ids:[...document.querySelectorAll('.hh-cam:checked')].map(x=>x.value),door_ids:[...document.querySelectorAll('.hh-door:checked')].map(x=>x.value)};PERMS.forEach(k=>p[k]=!!document.querySelector(`.hh-perm[value="${k}"]`)?.checked);return p}
function open(user){editing=user;$('hh-form-message').textContent='';$('hh-identity').style.display=user?'none':'grid';$('hh-title').textContent=user?`Permissions · ${user.name||user.email}`:'Add user';$('hh-submit').textContent=user?'Save permissions':'Send invitation';
 const p=user?user.permissions:{...data.default_permissions,camera_ids:data.cameras.map(c=>c.id),door_ids:[]};if(!user){$('hh-name').value='';$('hh-email').value=''}fill(p);$('hh-dialog').showModal()}
$('hh-add').onclick=()=>data&&open(null);$('hh-cancel').onclick=()=>$('hh-dialog').close();
$('hh-all').onchange=e=>document.querySelectorAll('.hh-cam').forEach(x=>x.checked=e.target.checked);
$('hh-form').onsubmit=async e=>{e.preventDefault();const p=collect();try{let r;if(editing){r=await call(`/api/customer/household/users/${encodeURIComponent(editing.id)}/permissions`,'PUT',p)}else{r=await call('/api/customer/household/invitations','POST',{name:$('hh-name').value,email:$('hh-email').value,permissions:p})}$('hh-dialog').close();say(r.message);load()}catch(err){$('hh-form-message').textContent=err.message}};
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b||!data)return;
 if(b.dataset.edit){open(data.users.find(u=>u.id===b.dataset.edit));return}
 if(b.dataset.act){const u=data.users.find(x=>x.id===b.dataset.id),name=u.name||u.email;if(b.dataset.act==='remove'&&!confirm(`Remove ${name}? They are signed out everywhere and lose all access.`))return;if(b.dataset.act==='disable'&&!confirm(`Disable ${name}? They are signed out everywhere until you turn them back on.`))return;
  try{const r=await call(`/api/customer/household/users/${encodeURIComponent(b.dataset.id)}/${b.dataset.act}`);say(r.message);load()}catch(err){say(err.message,false)}return}
 if(b.dataset.inv){try{const r=await call(`/api/customer/household/invitations/${encodeURIComponent(b.dataset.id)}/${b.dataset.inv}`);say(r.message);load()}catch(err){say(err.message,false)}}});
load();
})();
</script>'''
