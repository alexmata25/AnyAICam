"""Security arm modes: Arm Stay / Arm Away / Disarm (2026-09-28).

Like a traditional alarm panel, per customer site:
- DISARMED: no security responses and no intrusion alarms. Recording
  continues exactly as the customer's recording settings say.
- STAY: only the cameras the customer picked for Stay (normally the
  exterior/perimeter) are armed; indoor cameras stay quiet/private.
- AWAY: the cameras picked for Away (normally all of them) are armed.

"Armed" gates the security responses only -- security_line intrusion
alarms, siren/talkdown automations. It never changes recording, normal
analytics or normal alerts.

Cloud/edge split: the cloud owns the state and the settings (the customer
changes them in the portal; every change is audited) and pushes them to
the site's appliance in the configuration payload. The appliance decides
per detection from its locally stored copy, so an internet outage never
silently disarms or arms the site -- it keeps the last state it was given.
"""
from __future__ import annotations

import json
from datetime import datetime

MODES = ("disarmed", "stay", "away")
MODE_LABELS = {"disarmed": "Disarmed", "stay": "Armed Stay", "away": "Armed Away"}
DEFAULT_SETTINGS = {
    "stay_camera_ids": [],
    "away_camera_ids": None,       # None = every camera at the site
    "alarm_cooldown_seconds": 60,
    "siren_on_alarm": False,       # automatic siren when an intrusion alarm fires (Away)
    "talkdown_on_alarm": False,    # automatic spoken warning through the camera speaker
    "talkdown_message": "Warning. You are on private property and are being recorded. The owner has been notified.",
    "notify_sms": True,            # SMS for intrusion alarms when the customer has SMS enabled
}


def normalize_mode(value) -> str:
    mode = str(value or "").strip().lower().replace("arm_", "").replace("armed_", "")
    if mode not in MODES:
        raise ValueError(f"unknown security mode {value!r}")
    return mode


def merged_settings(stored: dict | None) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    for key, value in (stored or {}).items():
        if key in DEFAULT_SETTINGS:
            settings[key] = value
    return settings


def camera_is_armed(mode: str, camera_id: str, settings: dict | None) -> bool:
    """Does this camera take part in security responses in this mode?"""
    settings = merged_settings(settings)
    if mode == "stay":
        return camera_id in (settings.get("stay_camera_ids") or [])
    if mode == "away":
        away = settings.get("away_camera_ids")
        return True if away is None else camera_id in away
    return False


# ---------------------------------------------------------------- storage (cloud authoritative, edge mirror)

def ensure_schema(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS security_arm_state("
        "customer_id TEXT NOT NULL, site_id TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'disarmed', "
        "changed_by TEXT, changed_at TEXT, PRIMARY KEY(customer_id, site_id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS security_settings("
        "customer_id TEXT NOT NULL, site_id TEXT NOT NULL, settings_json TEXT NOT NULL DEFAULT '{}', "
        "updated_by TEXT, updated_at TEXT, PRIMARY KEY(customer_id, site_id))"
    )


def get_state(db, customer_id: str, site_id: str) -> dict:
    ensure_schema(db)
    row = db.execute("SELECT mode, changed_by, changed_at FROM security_arm_state WHERE customer_id=? AND site_id=?",
                     (customer_id, site_id)).fetchone()
    settings_row = db.execute("SELECT settings_json FROM security_settings WHERE customer_id=? AND site_id=?",
                              (customer_id, site_id)).fetchone()
    try:
        stored = json.loads(settings_row["settings_json"]) if settings_row else {}
    except (TypeError, ValueError):
        stored = {}
    return {
        "customer_id": customer_id, "site_id": site_id,
        "mode": row["mode"] if row and row["mode"] in MODES else "disarmed",
        "changed_by": row["changed_by"] if row else None,
        "changed_at": row["changed_at"] if row else None,
        "settings": merged_settings(stored),
    }


def set_mode(db, customer_id: str, site_id: str, mode: str, *, actor: str, now: datetime | None = None) -> dict:
    mode = normalize_mode(mode)
    ensure_schema(db)
    stamp = (now or datetime.now()).isoformat()
    db.execute(
        "INSERT INTO security_arm_state(customer_id,site_id,mode,changed_by,changed_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(customer_id,site_id) DO UPDATE SET mode=excluded.mode,changed_by=excluded.changed_by,changed_at=excluded.changed_at",
        (customer_id, site_id, mode, actor, stamp),
    )
    return get_state(db, customer_id, site_id)


def save_settings(db, customer_id: str, site_id: str, settings: dict, *, actor: str, valid_camera_ids: set,
                  now: datetime | None = None) -> dict:
    """Validated, tenant-scoped settings: camera lists may only name this
    site's own cameras; numbers are clamped; unknown keys are dropped."""
    merged = merged_settings(settings)
    merged["stay_camera_ids"] = [c for c in (merged.get("stay_camera_ids") or []) if c in valid_camera_ids]
    away = merged.get("away_camera_ids")
    merged["away_camera_ids"] = None if away is None else [c for c in away if c in valid_camera_ids]
    merged["alarm_cooldown_seconds"] = max(10, min(3600, int(merged.get("alarm_cooldown_seconds") or 60)))
    merged["talkdown_message"] = str(merged.get("talkdown_message") or "")[:300]
    for key in ("siren_on_alarm", "talkdown_on_alarm", "notify_sms"):
        merged[key] = bool(merged.get(key))
    ensure_schema(db)
    db.execute(
        "INSERT INTO security_settings(customer_id,site_id,settings_json,updated_by,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(customer_id,site_id) DO UPDATE SET settings_json=excluded.settings_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (customer_id, site_id, json.dumps(merged), actor, (now or datetime.now()).isoformat()),
    )
    return get_state(db, customer_id, site_id)


def store_synced_state(db, customer_id: str, site_id: str, mode: str, settings: dict, changed_at: str | None) -> None:
    """Edge side: mirror what the cloud sent (never invents a state)."""
    ensure_schema(db)
    db.execute(
        "INSERT INTO security_arm_state(customer_id,site_id,mode,changed_by,changed_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(customer_id,site_id) DO UPDATE SET mode=excluded.mode,changed_by=excluded.changed_by,changed_at=excluded.changed_at",
        (customer_id, site_id, normalize_mode(mode), "cloud-sync", changed_at),
    )
    db.execute(
        "INSERT INTO security_settings(customer_id,site_id,settings_json,updated_by,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(customer_id,site_id) DO UPDATE SET settings_json=excluded.settings_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (customer_id, site_id, json.dumps(merged_settings(settings)), "cloud-sync", changed_at),
    )


def camera_armed_now(db, camera_id: str) -> tuple[bool, dict | None]:
    """(armed?, state) for a camera, from the local copy."""
    camera = db.execute("SELECT customer_id, site_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not camera:
        return False, None
    state = get_state(db, camera["customer_id"], camera["site_id"])
    return camera_is_armed(state["mode"], camera_id, state["settings"]), state
