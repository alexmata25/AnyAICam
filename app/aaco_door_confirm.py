"""One-use confirmation for an AACO door unlock (2026-10-02, Codex review).

An AACO "unlock the front door" used to pulse the relay as soon as its
gates passed, and nothing stopped a replayed or double-submitted request
from pulsing it again. Now, mirroring AAC Voice Call's two-step unlock
(aac_voice_call_door.py):

- the command resolves and authorizes the door without any physical
  action and issues a random token; only its SHA-256 is stored, bound to
  this customer, this signed-in person and this exact door, for
  TTL_SECONDS;
- POST /api/aaco/door-unlock/confirm consumes it atomically (one UPDATE
  whose rowcount decides) and only then re-runs AACO settings, camera
  scope and the door authorization before the relay -- a second
  submission of the same token, an expired one, or one belonging to
  someone else does nothing.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from partner_db import connection

TTL_SECONDS = 60
DDL = ("CREATE TABLE IF NOT EXISTS aaco_door_unlock_confirmations(token_hash TEXT PRIMARY KEY,customer_id TEXT NOT NULL,"
       "user_email TEXT NOT NULL,door_camera_id TEXT NOT NULL,door_name TEXT,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,"
       "consumed_at TEXT)")


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(identity: dict, *, door_camera_id: str, door_name: str, now: datetime) -> str:
    raw = secrets.token_urlsafe(32)
    with connection() as db:
        db.execute(DDL)
        db.execute("INSERT INTO aaco_door_unlock_confirmations(token_hash,customer_id,user_email,door_camera_id,door_name,created_at,expires_at) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (_hash(raw), identity["customer_id"], str(identity.get("email") or "").lower(), door_camera_id, door_name,
                    now.isoformat(), (now + timedelta(seconds=TTL_SECONDS)).isoformat()))
    return raw


def consume(identity: dict, raw: str, *, now: datetime) -> dict | None:
    """The door this person confirmed, or None (unknown, expired, used, or
    not theirs) -- indistinguishable to the caller."""
    if not isinstance(raw, str) or not raw or len(raw) > 200:
        return None
    token_hash = _hash(raw)
    with connection() as db:
        db.execute(DDL)
        claimed = db.execute(
            "UPDATE aaco_door_unlock_confirmations SET consumed_at=? WHERE token_hash=? AND customer_id=? AND user_email=? "
            "AND consumed_at IS NULL AND expires_at>=?",
            (now.isoformat(), token_hash, identity["customer_id"], str(identity.get("email") or "").lower(), now.isoformat()),
        ).rowcount
        if claimed != 1:
            return None
        found = db.execute("SELECT door_camera_id,door_name FROM aaco_door_unlock_confirmations WHERE token_hash=?",
                           (token_hash,)).fetchone()
    return dict(found) if found else None
