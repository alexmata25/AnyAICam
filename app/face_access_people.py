"""Face Access settings of one enrolled person (2026-10-01).

For apartment buildings and businesses: who the person is (unit/apartment,
site/building), whether their Face Access is on and between which dates, and
which doors they may open on which days and hours.

A door is a camera the customer set up as a door (cameras.door_access_enabled,
with its relay channel and unlock time). A door grant is the existing
facial_rules row the recognition engine already evaluates
(trigger_type 'specific_person', one per person and door), marked
origin='cloud' so facial_embedding_sync mirrors it to the appliance. Door
grants are written dry_run=0: whether a door may move is decided by the door
itself (access_control.py keeps every new door in dry run until its owner
turns that off on site).

Turning Face Access off, or an expired/not-yet-started date range, stops
every automatic unlock for that person (facial_events.person_access_open);
recognition and door history are kept.
"""
from __future__ import annotations

import re
import secrets
from datetime import date

import relay_control

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_MIN_CONFIDENCE = 0.85
DEFAULT_COOLDOWN_SECONDS = 10


class AccessSettingsError(ValueError):
    pass


def _date_or_none(value, label: str) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as error:
        raise AccessSettingsError(f"{label} must be a date (YYYY-MM-DD).") from error


def _time_or_none(value, label: str) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else value
    if not value:
        return None
    if not _HHMM.match(str(value)):
        raise AccessSettingsError(f"{label} must be a time (HH:MM).")
    return str(value)


def doors_for_customer(db, customer_id: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT c.id,c.name,c.site_id,c.door_relay_channel,c.door_relay_pulse_ms,s.name AS site_name FROM cameras c "
        "LEFT JOIN sites s ON s.id=c.site_id WHERE c.customer_id=? AND c.door_access_enabled=1 ORDER BY s.name,c.name",
        (customer_id,),
    ).fetchall()]


def get_access(db, *, customer_id: str, person_id: str) -> dict | None:
    person = db.execute("SELECT * FROM facial_people WHERE id=? AND customer_id=?", (person_id, customer_id)).fetchone()
    if not person:
        return None
    grants = {row["camera_id"]: dict(row) for row in db.execute(
        "SELECT * FROM facial_rules WHERE customer_id=? AND person_id=? AND trigger_type='specific_person'",
        (customer_id, person_id),
    ).fetchall()}
    sites = [dict(row) for row in db.execute("SELECT id,name FROM sites WHERE customer_id=? ORDER BY name", (customer_id,)).fetchall()]
    doors = []
    for door in doors_for_customer(db, customer_id):
        grant = grants.get(door["id"])
        doors.append({
            "camera_id": door["id"], "name": door["name"], "site": door["site_name"],
            "allowed": bool(grant and grant["enabled"]),
            "days": (grant["days_of_week"] or "").split(",") if grant and grant["days_of_week"] else [],
            "start": grant["schedule_start"] if grant else None, "end": grant["schedule_end"] if grant else None,
        })
    return {
        "person_id": person_id, "display_name": person["display_name"], "status": person["status"],
        "unit": person["unit"], "site_id": person["site_id"], "sites": sites,
        "access_enabled": bool(person["access_enabled"]),
        "access_starts_on": person["access_starts_on"], "access_expires_on": person["access_expires_on"],
        "doors": doors,
    }


def save_access(db, *, customer_id: str, person_id: str, payload: dict, actor: str, now: str) -> dict:
    """Validates everything first, then writes the person's fields and
    replaces their door grants in one transaction. Returns
    {"settings": get_access(...), "access_reduced": bool} -- access_reduced
    tells the caller to push an immediate re-sync to the appliance."""
    before = get_access(db, customer_id=customer_id, person_id=person_id)
    if before is None:
        raise LookupError("Person not found.")
    unit = str(payload.get("unit") or "").strip()[:40] or None
    site_id = str(payload.get("site_id") or "").strip() or None
    if site_id and site_id not in {site["id"] for site in before["sites"]}:
        raise AccessSettingsError("Choose one of this account's sites.")
    enabled = bool(payload.get("access_enabled", True))
    starts = _date_or_none(payload.get("access_starts_on"), "Start date")
    expires = _date_or_none(payload.get("access_expires_on"), "Expiration date")
    if starts and expires and expires < starts:
        raise AccessSettingsError("The expiration date is before the start date.")
    doors = {door["id"]: door for door in doors_for_customer(db, customer_id)}
    grants = []
    for item in payload.get("doors") or []:
        if not isinstance(item, dict) or not item.get("allowed"):
            continue
        camera_id = str(item.get("camera_id") or "")
        door = doors.get(camera_id)
        if not door:
            raise AccessSettingsError("A selected door is not a door on this account.")
        if door["door_relay_channel"] is None:
            raise AccessSettingsError(f"{door['name']} has no relay channel set up yet.")
        start, end = _time_or_none(item.get("start"), "Start time"), _time_or_none(item.get("end"), "End time")
        if bool(start) != bool(end):
            raise AccessSettingsError("Give both a start and an end time, or neither (any time).")
        try:
            days = relay_control.normalize_days(item.get("days"))
        except ValueError as error:
            raise AccessSettingsError(str(error)) from error
        grants.append((door, start, end, days))
    db.execute(
        "UPDATE facial_people SET unit=?,site_id=?,access_enabled=?,access_starts_on=?,access_expires_on=?,updated_at=? "
        "WHERE id=? AND customer_id=?",
        (unit, site_id, 1 if enabled else 0, starts, expires, now, person_id, customer_id),
    )
    db.execute("DELETE FROM facial_rules WHERE customer_id=? AND person_id=? AND trigger_type='specific_person'",
               (customer_id, person_id))
    for door, start, end, days in grants:
        db.execute(
            "INSERT INTO facial_rules(id,customer_id,site_id,camera_id,name,trigger_type,person_id,min_confidence,relay_channel,"
            "pulse_ms,cooldown_seconds,dry_run,enabled,schedule_start,schedule_end,days_of_week,origin,created_at,updated_at,created_by) "
            "VALUES(?,?,?,?,?,'specific_person',?,?,?,?,?,0,1,?,?,?,'cloud',?,?,?)",
            ("rule_" + secrets.token_hex(10), customer_id, door["site_id"], door["id"],
             f"{before['display_name']} at {door['name']}"[:120], person_id, DEFAULT_MIN_CONFIDENCE,
             door["door_relay_channel"], door["door_relay_pulse_ms"] or 3000, DEFAULT_COOLDOWN_SECONDS,
             start, end, days, now, now, actor),
        )
    after = get_access(db, customer_id=customer_id, person_id=person_id)
    reduced = (
        (before["access_enabled"] and not enabled)
        or (expires or "") != (before["access_expires_on"] or "")
        or (starts or "") != (before["access_starts_on"] or "")
        or any(d["allowed"] and not next((n for n in after["doors"] if n["camera_id"] == d["camera_id"]), {}).get("allowed")
               for d in before["doors"])
        or any((d["days"], d["start"], d["end"]) != (n["days"], n["start"], n["end"])
               for d in before["doors"] for n in after["doors"] if d["camera_id"] == n["camera_id"] and d["allowed"] and n["allowed"])
    )
    return {"settings": after, "access_reduced": bool(reduced)}
