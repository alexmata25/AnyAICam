"""Notifications settings (Email + SMS channels) -- v1.

One row per portal user (SQL table customer_notification_channels --
deliberately NOT named notification_preferences; db_migrations.py
already defines a different, pre-existing table under that exact name,
the admin-managed per-event-type/camera/site rule set notification_
engine.py's real fanout path reads -- see partner_db.py's own comment
next to this table's CREATE statement for the full disambiguation),
scoped to their own customer_id: which event types they want to hear
about, whether that
applies to all of their cameras or a specific selected set, quiet
hours, and immediate-vs-summary delivery. camera_access.py's already-
established authorized_camera_ids() is the single source of truth for
which cameras a customer_viewer may even select -- this module never
re-derives or duplicates that decision, only enforces it: save_
preferences() rejects (does not silently drop) any requested camera_id
outside the caller-supplied authorized set, so a restricted user can
never subscribe to alerts for a camera they cannot access, no matter
what a client sends.

Pure, DB/FastAPI-free decision logic (fully unit-testable) plus
DB-touching wrappers that take an explicit db connection, matching
camera_access.py's established dependency-light pattern in this
codebase.
"""
from __future__ import annotations

import json
import re

EVENT_TYPES: dict[str, str] = {
    "smart_motion": "Smart Motion",
    "person": "Person detected",
    "vehicle": "Vehicle detected",
    "lpr": "License plate recognition",
    "ppe": "PPE violation",
    "people_counting": "People counting",
    "camera_offline": "Camera offline",
    "appliance_offline": "Appliance offline",
    "storage_problem": "Recording/storage problem",
    "system_health": "System/health warning",
}

CAMERA_SCOPES = {"all", "selected"}
DELIVERY_MODES = {"immediate", "summary"}

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# E.164-ish: a leading + and 8-15 digits -- matches sms_service.py's own
# PHONE_PATTERN exactly (deliberately loose; real validation belongs to
# the provider, this only catches obviously malformed input).
_PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")

DEFAULT_PREFERENCES: dict = {
    "email_address": "", "email_enabled": False, "email_verified_at": None,
    "phone_number": "", "sms_enabled": False, "phone_verified_at": None,
    "event_types": [], "camera_scope": "all", "camera_ids": [],
    "quiet_hours_enabled": False, "quiet_start": "22:00", "quiet_end": "07:00",
    "delivery_mode": "immediate",
}


class NotAuthorizedCameraError(ValueError):
    """Raised by save_preferences() when camera_ids includes a camera
    outside the caller-supplied authorized set -- callers turn this
    into an HTTP 403, never a silent drop."""


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_PATTERN.match((value or "").strip()))


def is_valid_phone(value: str) -> bool:
    return bool(_PHONE_PATTERN.match((value or "").strip()))


def is_valid_time(value: str) -> bool:
    return bool(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", (value or "").strip()))


def validate_event_types(values: list[str]) -> list[str]:
    """Fail-closed filter, not an error -- an unknown/removed event type
    in a stale request is simply dropped, matching customer_platform.py's
    own established allowed_events filtering convention for camera
    alerts. Order-preserving, de-duplicated."""
    seen: set[str] = set()
    result = []
    for value in values or []:
        if value in EVENT_TYPES and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def resolve_effective_camera_ids(*, camera_scope: str, camera_ids: list[str], authorized_camera_ids: set[str]) -> list[str]:
    """The cameras a saved preference actually currently applies to,
    re-derived from live authorization every time this is called --
    never a frozen snapshot. camera_scope='all' always means "all of
    this identity's *currently* authorized cameras" (a camera granted
    or revoked after saving takes effect immediately, with no re-save
    needed); camera_scope='selected' means exactly the stored
    camera_ids, but only the ones still within the live authorized set
    -- a camera_viewer's access revoked after saving silently stops
    applying rather than continuing to alert on a camera they can no
    longer see."""
    if camera_scope == "all":
        return sorted(authorized_camera_ids)
    return sorted(set(camera_ids) & authorized_camera_ids)


def _prepare_preferences(
    *,
    email_address: str,
    email_enabled: bool,
    phone_number: str,
    sms_enabled: bool,
    event_types: list[str],
    camera_scope: str,
    camera_ids: list[str],
    authorized_camera_ids: set[str],
    quiet_hours_enabled: bool,
    quiet_start: str,
    quiet_end: str,
    delivery_mode: str,
    previous: dict | None,
) -> dict:
    """Pure validation/normalization step save_preferences() wraps with
    the actual DB write. Raises ValueError for a structurally invalid
    request (bad email/phone format, bad scope/mode/time, or -- the one
    that matters for the permission requirement -- a selected camera_id
    outside authorized_camera_ids) rather than silently coercing it, so
    a caller always knows a rejection happened instead of guessing from
    what got saved."""
    email_address = (email_address or "").strip()
    if email_enabled and not is_valid_email(email_address):
        raise ValueError("A valid email address is required to enable email notifications.")
    phone_number = (phone_number or "").strip()
    if sms_enabled and not is_valid_phone(phone_number):
        raise ValueError("A valid phone number (e.g. +15551234567) is required to enable SMS notifications.")
    if camera_scope not in CAMERA_SCOPES:
        raise ValueError(f"Unknown camera_scope: {camera_scope!r}")
    if delivery_mode not in DELIVERY_MODES:
        raise ValueError(f"Unknown delivery_mode: {delivery_mode!r}")
    if quiet_hours_enabled and not (is_valid_time(quiet_start) and is_valid_time(quiet_end)):
        raise ValueError("Quiet hours start/end must be HH:MM.")
    if camera_scope == "selected":
        requested = set(camera_ids or [])
        unauthorized = requested - authorized_camera_ids
        if unauthorized:
            raise NotAuthorizedCameraError(
                f"Not authorized for camera(s): {', '.join(sorted(unauthorized))}"
            )

    previous = previous or {}
    email_changed = email_address != (previous.get("email_address") or "")
    phone_changed = phone_number != (previous.get("phone_number") or "")

    return {
        "email_address": email_address,
        "email_enabled": bool(email_enabled),
        # Changing the address resets verification -- the previously
        # verified address is not the one now on file.
        "email_verified_at": None if email_changed else previous.get("email_verified_at"),
        "phone_number": phone_number,
        "sms_enabled": bool(sms_enabled),
        "phone_verified_at": None if phone_changed else previous.get("phone_verified_at"),
        "event_types": validate_event_types(event_types),
        "camera_scope": camera_scope,
        "camera_ids": sorted(set(camera_ids or []) & authorized_camera_ids) if camera_scope == "selected" else [],
        "quiet_hours_enabled": bool(quiet_hours_enabled),
        "quiet_start": quiet_start if quiet_hours_enabled else previous.get("quiet_start", "22:00"),
        "quiet_end": quiet_end if quiet_hours_enabled else previous.get("quiet_end", "07:00"),
        "delivery_mode": delivery_mode,
    }


def get_preferences(db, *, user_id: str) -> dict:
    row = db.execute(
        "SELECT email_address,email_enabled,email_verified_at,phone_number,sms_enabled,phone_verified_at,"
        "event_types_json,camera_scope,quiet_hours_enabled,quiet_start,quiet_end,delivery_mode "
        "FROM customer_notification_channels WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return dict(DEFAULT_PREFERENCES)
    camera_ids = [
        item["camera_id"] for item in db.execute(
            "SELECT camera_id FROM customer_notification_channel_cameras WHERE user_id=? ORDER BY camera_id", (user_id,)
        ).fetchall()
    ]
    return {
        "email_address": row["email_address"] or "",
        "email_enabled": bool(row["email_enabled"]),
        "email_verified_at": row["email_verified_at"],
        "phone_number": row["phone_number"] or "",
        "sms_enabled": bool(row["sms_enabled"]),
        "phone_verified_at": row["phone_verified_at"],
        "event_types": json.loads(row["event_types_json"] or "[]"),
        "camera_scope": row["camera_scope"] or "all",
        "camera_ids": camera_ids,
        "quiet_hours_enabled": bool(row["quiet_hours_enabled"]),
        "quiet_start": row["quiet_start"] or "22:00",
        "quiet_end": row["quiet_end"] or "07:00",
        "delivery_mode": row["delivery_mode"] or "immediate",
    }


def save_preferences(
    db,
    *,
    user_id: str,
    customer_id: str,
    authorized_camera_ids: set[str],
    now: str,
    **fields,
) -> dict:
    """DB-touching wrapper: validates via _prepare_preferences() (which
    raises NotAuthorizedCameraError/ValueError on any invalid or
    unauthorized input -- callers must let that propagate to an HTTP
    4xx, never catch-and-ignore it) against this user's *current*
    saved row (so email/phone-changed verification-reset logic sees the
    real previous state), then replaces the row and the selected-
    cameras junction rows outright -- the same replace-not-append
    convention camera_access.set_camera_access() already established in
    this codebase."""
    previous = get_preferences(db, user_id=user_id)
    prepared = _prepare_preferences(authorized_camera_ids=authorized_camera_ids, previous=previous, **fields)

    db.execute(
        "INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,email_verified_at,"
        "phone_number,sms_enabled,phone_verified_at,event_types_json,camera_scope,quiet_hours_enabled,"
        "quiet_start,quiet_end,delivery_mode,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET customer_id=excluded.customer_id,email_address=excluded.email_address,"
        "email_enabled=excluded.email_enabled,email_verified_at=excluded.email_verified_at,"
        "phone_number=excluded.phone_number,sms_enabled=excluded.sms_enabled,"
        "phone_verified_at=excluded.phone_verified_at,event_types_json=excluded.event_types_json,"
        "camera_scope=excluded.camera_scope,quiet_hours_enabled=excluded.quiet_hours_enabled,"
        "quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,delivery_mode=excluded.delivery_mode,"
        "updated_at=excluded.updated_at",
        (
            user_id, customer_id, prepared["email_address"], int(prepared["email_enabled"]), prepared["email_verified_at"],
            prepared["phone_number"], int(prepared["sms_enabled"]), prepared["phone_verified_at"],
            json.dumps(prepared["event_types"]), prepared["camera_scope"], int(prepared["quiet_hours_enabled"]),
            prepared["quiet_start"], prepared["quiet_end"], prepared["delivery_mode"], now,
        ),
    )
    db.execute("DELETE FROM customer_notification_channel_cameras WHERE user_id=?", (user_id,))
    for camera_id in prepared["camera_ids"]:
        db.execute(
            "INSERT INTO customer_notification_channel_cameras(user_id,camera_id) VALUES(?,?)", (user_id, camera_id)
        )
    return get_preferences(db, user_id=user_id)


def mark_email_verified(db, *, user_id: str, now: str) -> None:
    db.execute("UPDATE customer_notification_channels SET email_verified_at=? WHERE user_id=?", (now, user_id))


def mark_phone_verified(db, *, user_id: str, now: str) -> None:
    db.execute("UPDATE customer_notification_channels SET phone_verified_at=? WHERE user_id=?", (now, user_id))
