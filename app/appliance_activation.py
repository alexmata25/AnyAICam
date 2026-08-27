"""Durable local appliance-activation identity: where THIS box remembers,
across restarts, that it is a specific activated appliance -- closing
gap #1 from the appliance identity contract (see appliance_identity.py
and the reviewed design doc). own_appliance_identity() (main.py) reads
what this module persists; nothing else should read or write the file
directly.

Storage: reuses this codebase's existing local-JSON-file convention
(RECORDINGS_FOLDER / "*.json", the same pattern users.json,
partner_customers.json, and live_manifest.json already use) rather than
inventing a new store -- one file, one purpose, atomic writes.

Persisted, never logged, never returned by any read-side function that
isn't explicitly this module's own loader: appliance_id, cloud_id, the
permanent activation credential, customer_id, site_id, partner_id,
activated_at, and activation_version. This file is the one place on an
activated appliance where that permanent credential lives at rest --
see _atomic_write() for the permission hardening applied to it.

Where activation is actually triggered from today: appliance_cloud.py's
POST /api/appliance/activate calls persist_activation() as its last
step, on the theory that in this monolith (cloud and appliance code
colocated, same as provisioning_service.MockProvisioningBackend's own
documented simplification) a box's own local process handling its own
activation call *is* that box activating itself. A real, separate
appliance agent calling an external cloud's /api/appliance/activate
over HTTPS would call persist_activation() itself, locally, from its
own response handler -- same function, different call site.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

ACTIVATION_IDENTITY_FILE = Path(os.getenv("ANYAICAM_APPLIANCE_IDENTITY_FILE", "/app/recordings/appliance_identity.json"))

_REQUIRED_FIELDS = {"appliance_id", "cloud_id", "credential", "customer_id", "site_id", "partner_id", "activated_at", "activation_version"}


class ActivationConflict(ValueError):
    """Raised by persist_activation() when this appliance is already
    durably activated as a DIFFERENT cloud_id and allow_overwrite wasn't
    explicitly set -- a second, different activation must never
    silently replace the first. Call reset_persisted_identity() first
    (an explicit, auditable re-provisioning action) to clear the way."""


def _atomic_write(path: Path, data: dict) -> None:
    """Write-temp-then-replace (Path.replace() is atomic on both POSIX
    and Windows) so a power loss mid-write can never leave a half-
    written identity file -- the file is either the old complete
    version or the new complete version, never a truncated mix of both.
    Restrictive permissions (owner read/write only) are applied to the
    temp file *before* the rename, so the credential is never briefly
    world-readable at any point -- best-effort on platforms without
    POSIX permission bits (e.g. Windows dev machines), never fatal
    there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def load_persisted_identity() -> dict | None:
    """Fails closed on anything but a complete, parseable file: missing
    file, unreadable file, invalid JSON, or JSON missing a required
    field all return None -- the same "not activated" outcome
    own_appliance_identity() already treats as safe/inert, never a
    crash and never a partially-trusted identity."""
    try:
        raw = ACTIVATION_IDENTITY_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not _REQUIRED_FIELDS.issubset(data.keys()):
        return None
    return data


def persist_activation(*, appliance_id: str, cloud_id: str, credential: str, customer_id: str, site_id: str, partner_id: str | None, allow_overwrite: bool = False) -> dict:
    """Idempotent for the SAME cloud_id (a legitimate credential refresh
    -- re-running activation with a freshly issued token updates the
    file and bumps activation_version); raises ActivationConflict for a
    DIFFERENT cloud_id unless allow_overwrite is explicitly set. Callers
    must run this check BEFORE consuming an activation token or minting
    a new credential server-side -- see appliance_cloud.py's
    activate_appliance(), which checks first specifically so a rejected
    activation here never leaves the token consumed or a credential
    issued with nothing local to show for it."""
    existing = load_persisted_identity()
    if existing and existing["cloud_id"] != cloud_id and not allow_overwrite:
        raise ActivationConflict(
            f"This appliance is already activated as {existing['cloud_id']!r}. "
            f"Refusing to silently switch to {cloud_id!r} -- reset the local activation identity first."
        )
    version = existing["activation_version"] + 1 if existing and existing["cloud_id"] == cloud_id else 1
    data = {
        "appliance_id": appliance_id, "cloud_id": cloud_id, "credential": credential,
        "customer_id": customer_id, "site_id": site_id, "partner_id": partner_id,
        "activated_at": datetime.now().isoformat(), "activation_version": version,
    }
    _atomic_write(ACTIVATION_IDENTITY_FILE, data)
    return data


def reset_persisted_identity() -> None:
    """Explicit re-provisioning path -- the only sanctioned way to let a
    subsequent activation switch this appliance to a different
    cloud_id. Deleting a file that doesn't exist is a no-op, not an
    error, so this is itself safe to call more than once."""
    try:
        ACTIVATION_IDENTITY_FILE.unlink()
    except FileNotFoundError:
        pass
