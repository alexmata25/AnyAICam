"""Face Access -- Backup Mobile Access (2026-10-01).

When facial recognition doesn't let an authorized person in, they open
AnyAiCam on their phone -> People -> their profile -> Unlock door, choose
the door and enter their own Backup Access PIN. Everything below is enforced
on the server; the phone only ever sends a door id and a PIN.

Two factors, both required:
  1. an authenticated AnyAiCam session allowed to use Backup Mobile Access
     (the account owner, or a household member the owner granted it to --
     household_users.py), and
  2. the PIN of the enrolled person (facial_people) being let in. A PIN
     belongs to one person -- there is no door or household PIN -- and is
     stored only as a salted PBKDF2 hash (partner_db.password_hash); it is
     never shown again after it is set.

Then the same rules automatic Face Access uses: the person is active, Face
Access is on, today is within their start/expiration dates, and they hold an
enabled grant for this door whose days and hours include now.

Wrong PINs are counted per person: five in a row lock Backup Mobile Access
for that person for 15 minutes; there is also a per-signed-in-user rate
limit. Each unlock is a new single-use command (128-bit id, 30-second
lifetime) recorded in backup_unlock_commands on the cloud and checked again
on the appliance, which refuses a command id it has already seen or one
issued outside its clock window -- so a captured or repeated command cannot
open the door again. It travels over the appliance's existing authenticated
control channel (appliance_control.request), the same path the portal's
Unlock button already uses.

Fail closed, and honest: no relay configured -> "Access authorized - no door
hardware configured. The door was not unlocked."; appliance offline or no
answer -> "was not unlocked". Success means the access-control output
accepted the unlock command -- never that the door was seen to open (no
supported door reports its position; see door_feedback.py).

Every attempt is audited in door_access_events with trigger_type
'backup_mobile': denied PIN, locked out, outside schedule, authorized and
the relay result (activated / simulated / not_sent / failed). The account
owner is notified of every successful authorization.

Life safety is never AnyAiCam's job: mechanical keys, free egress and any
commercial strike/maglock fire release stay independent of this software and
of the internet. Backup Mobile Access is an additional way in, never the
only one.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request

from appliance_protocol import RateLimiter
from partner_db import audit, connection

logger = logging.getLogger("anyaicam.backup_access")

PIN_MIN_LENGTH, PIN_MAX_LENGTH = 6, 10
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
COMMAND_TTL_SECONDS = 30
APPLIANCE_CLOCK_WINDOW_SECONDS = 120
TRIGGER = "backup_mobile"
NO_HARDWARE_MESSAGE = "Access authorized — no door hardware is configured for {door}. The door was not unlocked."
_COMMAND_ID = re.compile(r"^[0-9a-f]{32}$")

_actor_limiter = RateLimiter(limit=20, window_seconds=900)


class BackupAccessError(Exception):
    def __init__(self, status_code: int, detail: str, *, audit_result: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.audit_result = audit_result


# ------------------------------------------------------------------ PIN

def validate_pin(pin) -> str:
    pin = str(pin or "").strip()
    if not pin.isdigit() or not (PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH):
        raise BackupAccessError(400, f"The PIN must be {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} digits.")
    digits = [int(c) for c in pin]
    steps = {b - a for a, b in zip(digits, digits[1:])}
    if len(set(digits)) == 1 or steps in ({1}, {-1}):
        raise BackupAccessError(400, "That PIN is too easy to guess. Avoid repeated or consecutive digits.")
    return pin


def pin_record(db, *, customer_id: str, person_id: str) -> dict | None:
    row = db.execute("SELECT * FROM facial_person_backup_pins WHERE person_id=? AND customer_id=?",
                     (person_id, customer_id)).fetchone()
    return dict(row) if row else None


def set_pin(db, *, customer_id: str, person_id: str, pin: str, actor: str, now: datetime) -> None:
    from partner_db import password_hash
    pin = validate_pin(pin)
    db.execute(
        "INSERT INTO facial_person_backup_pins(person_id,customer_id,pin_hash,set_at,set_by,failed_attempts,locked_until) "
        "VALUES(?,?,?,?,?,0,NULL) ON CONFLICT(person_id) DO UPDATE SET customer_id=excluded.customer_id,"
        "pin_hash=excluded.pin_hash,set_at=excluded.set_at,set_by=excluded.set_by,failed_attempts=0,locked_until=NULL",
        (person_id, customer_id, password_hash(pin), now.isoformat(), actor),
    )


def reset_pin(db, *, customer_id: str, person_id: str) -> None:
    db.execute("DELETE FROM facial_person_backup_pins WHERE person_id=? AND customer_id=?", (person_id, customer_id))


def check_pin(db, *, customer_id: str, person_id: str, pin: str, now: datetime) -> BackupAccessError | None:
    """None when the PIN is right and not locked out, else the error to raise.

    Returned, not raised: the wrong-PIN count must be committed, and
    connection() rolls the whole transaction back the moment an exception
    leaves it -- raising here would silently undo the count and the lockout
    would never engage. Wrong PINs count toward the lockout; a right one
    resets the count."""
    from partner_db import verify_password
    record = pin_record(db, customer_id=customer_id, person_id=person_id)
    if not record:
        return BackupAccessError(409, "No Backup Access PIN is set for this person yet.", audit_result="no_pin")
    if record.get("locked_until") and record["locked_until"] > now.isoformat():
        return BackupAccessError(423, "Too many wrong PINs. Backup access for this person is paused for a few minutes.",
                                 audit_result="locked_out")
    if verify_password(str(pin or ""), record["pin_hash"]):
        db.execute("UPDATE facial_person_backup_pins SET failed_attempts=0,locked_until=NULL WHERE person_id=?", (person_id,))
        return None
    failures = int(record.get("failed_attempts") or 0) + 1
    locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat() if failures >= MAX_FAILED_ATTEMPTS else None
    db.execute("UPDATE facial_person_backup_pins SET failed_attempts=?,locked_until=?,last_failed_at=? WHERE person_id=?",
               (0 if locked_until else failures, locked_until, now.isoformat(), person_id))
    if locked_until:
        return BackupAccessError(423, "Too many wrong PINs. Backup access for this person is paused for a few minutes.",
                                 audit_result="locked_out")
    return BackupAccessError(403, "That PIN is not correct.", audit_result="denied_pin")


# ------------------------------------------------------------------ door rules

def door_open_for(db, *, customer_id: str, person_id: str, camera_id: str, now: datetime) -> tuple[bool, str]:
    """Whether this person's Face Access lets them through this door now --
    the same checks automatic Face Access applies."""
    import facial_events
    import relay_control
    if not facial_events.person_access_open(db, customer_id=customer_id, person_id=person_id, today=now.date().isoformat()):
        return False, "Face Access is off for this person, or today is outside their access dates."
    grant = db.execute(
        "SELECT * FROM facial_rules WHERE customer_id=? AND person_id=? AND camera_id=? AND trigger_type='specific_person' AND enabled=1",
        (customer_id, person_id, camera_id),
    ).fetchone()
    if not grant:
        return False, "This person is not allowed through this door."
    weekday = relay_control.WEEKDAYS[now.weekday()]
    days = grant["days_of_week"] if "days_of_week" in grant.keys() else None
    if days and weekday not in days.split(","):
        return False, "This person's access to this door doesn't include today."
    if not relay_control._within_schedule(now.strftime("%H:%M"), grant["schedule_start"], grant["schedule_end"]):
        return False, "This person's access to this door doesn't include this time of day."
    return True, ""


def doors_for(db, *, customer_id: str, person_id: str, now: datetime) -> list[dict]:
    doors = []
    for camera in db.execute("SELECT id,name,door_relay_channel FROM cameras WHERE customer_id=? AND door_access_enabled=1 ORDER BY name",
                             (customer_id,)).fetchall():
        granted = db.execute("SELECT 1 FROM facial_rules WHERE customer_id=? AND person_id=? AND camera_id=? AND "
                             "trigger_type='specific_person' AND enabled=1", (customer_id, person_id, camera["id"])).fetchone()
        if not granted:
            continue
        open_now, reason = door_open_for(db, customer_id=customer_id, person_id=person_id, camera_id=camera["id"], now=now)
        doors.append({"camera_id": camera["id"], "name": camera["name"], "open_now": open_now, "reason": reason,
                      "hardware": camera["door_relay_channel"] is not None})
    return doors


# ------------------------------------------------------------------ commands

def issue_command(db, *, customer_id: str, person_id: str, camera_id: str, actor: str, now: datetime) -> dict:
    command = {"command_id": secrets.token_hex(16), "issued_at": time.time()}
    command["expires_at"] = command["issued_at"] + COMMAND_TTL_SECONDS
    db.execute("INSERT INTO backup_unlock_commands(id,customer_id,person_id,camera_id,actor_email,issued_at,expires_at,status,created_at) "
               "VALUES(?,?,?,?,?,?,?,'issued',?)",
               (command["command_id"], customer_id, person_id, camera_id, actor, command["issued_at"], command["expires_at"], now.isoformat()))
    return command


def accept_command_on_appliance(db, message: dict, *, now: float | None = None) -> str | None:
    """Appliance side: None when this backup command may run, else why not.
    Records the command id first, so the same command can never run twice."""
    now = time.time() if now is None else now
    command_id = str(message.get("command_id") or "")
    if not _COMMAND_ID.match(command_id):
        return "bad_command"
    try:
        issued_at = float(message.get("issued_at"))
    except (TypeError, ValueError):
        return "bad_command"
    if abs(now - issued_at) > APPLIANCE_CLOCK_WINDOW_SECONDS:
        return "expired_command"
    try:
        db.execute("INSERT INTO backup_unlock_commands(id,customer_id,person_id,camera_id,actor_email,issued_at,expires_at,status,created_at) "
                   "VALUES(?,?,?,?,?,?,?,'received',?)",
                   (command_id, message.get("customer_id") or "", message.get("person_id") or "", message.get("camera_id") or "",
                    message.get("actor") or "", issued_at, issued_at + COMMAND_TTL_SECONDS, datetime.now().isoformat()))
    except Exception:
        return "replayed_command"
    return None


# ------------------------------------------------------------------ the unlock

def can_use_backup(identity: dict | None) -> bool:
    import household_users
    return bool(identity) and household_users.account_permission(identity, "backup_access")


def _audit_attempt(*, identity: dict, customer_id: str, camera: dict | None, camera_id: str, person: dict,
                   authorization_result: str, relay_result: str, success: bool, error: str | None, now: datetime) -> None:
    import door_access
    with connection() as db:
        user = db.execute("SELECT id FROM partner_users WHERE lower(email)=lower(?)", (identity.get("email", ""),)).fetchone()
        door_access.record_door_access_event(
            db, customer_id=customer_id, camera_id=camera_id, door_name=(camera or {}).get("name") or "",
            relay_channel=(camera or {}).get("door_relay_channel"), trigger_type=TRIGGER,
            actor_user_id=user["id"] if user else None, actor_email=identity.get("email"),
            matched_person_id=person["id"], matched_person_name=person.get("display_name"),
            authorization_result=authorization_result, relay_result=relay_result, success=success, error=error, now=now,
        )


def _notify_owner(*, customer_id: str, person_name: str, door_name: str, actor: str, unlocked: bool, now: datetime) -> None:
    """In-app notification (and phone push) to the account owner(s)."""
    try:
        import notification_engine
        title = "Backup Mobile Access used"
        message = (f"{person_name} used Backup Mobile Access at {door_name} ({actor}). "
                   + ("The unlock command was accepted." if unlocked else "The door was not unlocked."))
        with connection() as db:
            for owner in db.execute("SELECT id FROM partner_users WHERE customer_id=? AND role='customer_owner' AND "
                                    "COALESCE(account_status,'active')='active'", (customer_id,)).fetchall():
                notification_id = secrets.token_hex(16)
                db.execute("INSERT INTO notifications(id,user_id,customer_id,event_type,severity,title,message,timestamp,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?)",
                           (notification_id, owner["id"], customer_id, "backup_access", "warning", title, message,
                            now.isoformat(), now.isoformat()))
                notification_engine._enqueue_mobile_push(db, notification_id)
    except Exception as error:
        logger.warning("backup_access.notify_failed customer_id=%s error=%s", customer_id, type(error).__name__)


def unlock(identity: dict, *, person_id: str, camera_id: str, pin: str, now: datetime | None = None) -> dict:
    import door_access
    now = now or datetime.now()
    customer_id = identity.get("customer_id")
    if identity.get("role") not in ("customer_owner", "customer_viewer") or not customer_id:
        raise BackupAccessError(403, "Customer account required.")
    if not can_use_backup(identity):
        raise BackupAccessError(403, "You don't have Backup Mobile Access on this account.")
    if not _actor_limiter.allow(f"{customer_id}:{identity.get('email', '').lower()}"):
        raise BackupAccessError(429, "Too many unlock attempts. Please wait a few minutes.")
    with connection() as db:
        person = db.execute("SELECT id,display_name FROM facial_people WHERE id=? AND customer_id=?", (person_id, customer_id)).fetchone()
        if not person:
            raise BackupAccessError(404, "Person not found.")
        person = dict(person)
        camera = db.execute("SELECT * FROM cameras WHERE id=? AND customer_id=? AND door_access_enabled=1", (camera_id, customer_id)).fetchone()
        if not camera:
            raise BackupAccessError(404, "Door not found.")
        camera = dict(camera)
    attempt = dict(identity=identity, customer_id=customer_id, camera=camera, camera_id=camera_id, person=person, now=now)
    with connection() as db:
        refusal = check_pin(db, customer_id=customer_id, person_id=person_id, pin=pin, now=now)
    if refusal is not None:  # committed above; audited in its own transaction
        _audit_attempt(**attempt, authorization_result=refusal.audit_result or "denied", relay_result="skipped",
                       success=False, error=refusal.detail)
        raise refusal
    with connection() as db:
        open_now, reason = door_open_for(db, customer_id=customer_id, person_id=person_id, camera_id=camera_id, now=now)
    if not open_now:
        _audit_attempt(**attempt, authorization_result="denied_schedule", relay_result="skipped", success=False, error=reason)
        raise BackupAccessError(403, reason)

    door = camera["name"]
    if camera.get("door_relay_channel") is None:
        _audit_attempt(**attempt, authorization_result="authorized", relay_result="no_hardware", success=False,
                       error="No relay channel is configured for this door.")
        _notify_owner(customer_id=customer_id, person_name=person["display_name"], door_name=door,
                      actor=identity.get("email", ""), unlocked=False, now=now)
        return {"authorized": True, "unlocked": False, "status": "no_hardware", "message": NO_HARDWARE_MESSAGE.format(door=door)}

    with connection() as db:
        command = issue_command(db, customer_id=customer_id, person_id=person_id, camera_id=camera_id,
                                actor=identity.get("email", ""), now=now)
    extra = {"trigger": TRIGGER, "command_id": command["command_id"], "issued_at": command["issued_at"],
             "person_id": person_id, "customer_id": customer_id}

    def _finish(status: str) -> None:
        with connection() as db:
            db.execute("UPDATE backup_unlock_commands SET status=? WHERE id=?", (status, command["command_id"]))

    try:
        result = door_access.dispatch_manual_unlock(camera, actor=identity.get("email", ""), pulse_ms=camera.get("door_relay_pulse_ms"),
                                                    extra=extra)
    except door_access.DoorNotReachable as error:
        _finish("not_sent")
        _audit_attempt(**attempt, authorization_result="authorized", relay_result="not_sent", success=False, error=str(error))
        _notify_owner(customer_id=customer_id, person_name=person["display_name"], door_name=door,
                      actor=identity.get("email", ""), unlocked=False, now=now)
        raise BackupAccessError(503, f"Access authorized, but {door} was not unlocked: {error}.")
    except Exception as error:
        _finish("failed")
        _audit_attempt(**attempt, authorization_result="authorized", relay_result="failed", success=False, error=type(error).__name__)
        raise BackupAccessError(502, f"Access authorized, but {door} could not be reached. The door was not unlocked.")

    if result.simulated:
        _finish("simulated")
        _audit_attempt(**attempt, authorization_result="authorized", relay_result="simulated", success=False,
                       error="No door relay hardware is connected.")
        _notify_owner(customer_id=customer_id, person_name=person["display_name"], door_name=door,
                      actor=identity.get("email", ""), unlocked=False, now=now)
        return {"authorized": True, "unlocked": False, "status": "no_hardware", "message": NO_HARDWARE_MESSAGE.format(door=door)}
    if not result.activated:
        _finish("refused")
        _audit_attempt(**attempt, authorization_result="authorized", relay_result=door_access.audit_relay_result(result),
                       success=False, error=result.suppressed_reason)
        message = ("This door was just unlocked -- please wait a moment before trying again."
                   if result.suppressed_reason == "cooldown" else f"Access authorized, but {door} did not accept the unlock command.")
        raise BackupAccessError(409, message)
    _finish("accepted")
    _audit_attempt(**attempt, authorization_result="authorized", relay_result="activated", success=True, error=None)
    _notify_owner(customer_id=customer_id, person_name=person["display_name"], door_name=door,
                  actor=identity.get("email", ""), unlocked=True, now=now)
    return {"authorized": True, "unlocked": True, "status": "accepted",
            "message": f"Unlock command accepted by {door}'s access-control output."}


# ------------------------------------------------------------------ routes

def _identity(request: Request) -> dict:
    from partner_portal import partner_identity
    identity = partner_identity(request)
    if not identity or identity.get("role") not in ("customer_owner", "customer_viewer") or not identity.get("customer_id"):
        raise HTTPException(status_code=403, detail="Customer account required.")
    return identity


def _manager(identity: dict) -> bool:
    import facial_recognition_ui
    return facial_recognition_ui.facial_allowed(identity, "facial.manage")


def register_backup_access_routes(app: FastAPI) -> None:
    @app.get("/api/aac/people/{person_id}/backup-access")
    def backup_status(request: Request, person_id: str) -> dict:
        identity = _identity(request)
        manager, user = _manager(identity), can_use_backup(identity)
        if not (manager or user):
            raise HTTPException(status_code=403, detail="You do not have permission for this action.")
        now = datetime.now()
        with connection() as db:
            if not db.execute("SELECT 1 FROM facial_people WHERE id=? AND customer_id=?", (person_id, identity["customer_id"])).fetchone():
                raise HTTPException(status_code=404, detail="Person not found.")
            record = pin_record(db, customer_id=identity["customer_id"], person_id=person_id)
            doors = doors_for(db, customer_id=identity["customer_id"], person_id=person_id, now=now)
        locked = bool(record and record.get("locked_until") and record["locked_until"] > now.isoformat())
        return {"pin_set": bool(record), "pin_set_at": (record or {}).get("set_at"), "locked": locked,
                "can_manage_pin": manager, "can_unlock": user, "doors": doors}

    @app.put("/api/aac/people/{person_id}/backup-pin")
    def put_pin(request: Request, person_id: str, payload: dict) -> dict:
        identity = _identity(request)
        if not _manager(identity):
            raise HTTPException(status_code=403, detail="You do not have permission for this action.")
        now = datetime.now()
        try:
            with connection() as db:
                if not db.execute("SELECT 1 FROM facial_people WHERE id=? AND customer_id=?", (person_id, identity["customer_id"])).fetchone():
                    raise HTTPException(status_code=404, detail="Person not found.")
                existed = pin_record(db, customer_id=identity["customer_id"], person_id=person_id) is not None
                set_pin(db, customer_id=identity["customer_id"], person_id=person_id, pin=payload.get("pin"),
                        actor=identity.get("email", ""), now=now)
        except BackupAccessError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        audit(identity, "backup_access.pin_changed" if existed else "backup_access.pin_set", "facial_people", person_id)
        return {"message": "Backup Access PIN changed." if existed else "Backup Access PIN set."}

    @app.delete("/api/aac/people/{person_id}/backup-pin")
    def delete_pin(request: Request, person_id: str) -> dict:
        identity = _identity(request)
        if not _manager(identity):
            raise HTTPException(status_code=403, detail="You do not have permission for this action.")
        with connection() as db:
            if not db.execute("SELECT 1 FROM facial_people WHERE id=? AND customer_id=?", (person_id, identity["customer_id"])).fetchone():
                raise HTTPException(status_code=404, detail="Person not found.")
            reset_pin(db, customer_id=identity["customer_id"], person_id=person_id)
        audit(identity, "backup_access.pin_reset", "facial_people", person_id)
        return {"message": "Backup Access PIN removed. Set a new one before it can be used."}

    @app.post("/api/aac/people/{person_id}/backup-unlock")
    def backup_unlock(request: Request, person_id: str, payload: dict) -> dict:
        identity = _identity(request)
        try:
            outcome = unlock(identity, person_id=person_id, camera_id=str(payload.get("camera_id") or ""), pin=str(payload.get("pin") or ""))
        except BackupAccessError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        audit(identity, "backup_access.unlock", "facial_people", person_id, {"camera_id": payload.get("camera_id"), "status": outcome["status"]})
        return outcome
