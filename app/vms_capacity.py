"""Licensed camera capacity on the appliance (2026-10-01; signed 2026-10-09).

Claiming an appliance is open to any customer account; what the VMS may
actually run is not. The cloud sends this appliance's usable capacity --
the smaller of the customer's camera plan and their VMS software license
(customer_entitlements.usable_camera_capacity()) -- with every configuration
sync, and it is kept here so it keeps applying offline (Local mode).

The recording/live pipeline asks camera_licensed() through camera_url(), the
one place every camera worker resolves its camera:

- capacity known: only the first N provisioned camera numbers run
  (0 runs none);
- no authoritative capacity -- never claimed, claimed moments ago, or
  nothing below applies -- nothing runs (fail closed, 2026-10-02 Codex
  finding). The first configuration sync (seconds after activation) enables
  the licensed cameras; the supervisor keeps polling refused slots, so no
  restart is needed. Claiming and registering never depend on this.

Where the capacity comes from (2026-10-09, owner-approved Option A; before,
the unsigned vms_capacity.json -- writable by the anyaicam account -- was
trusted, so the limit could be raised offline), in this order:

 1. signed: camera_capacity inside the cloud's signed entitlement snapshot
    (feature_entitlements.appliance_snapshot), verified on every read
    against the root-owned trust anchor and this appliance's activation
    identity (appliance_entitlements.verify). Its expiry is NOT enforced:
    the last signed limit keeps applying offline, so recording never stops
    because the cloud is unreachable. A newer signed limit replaces it
    (never an older one).
 2. legacy cloud, transitional: a cloud deployed before this change signs
    no capacity. Only then -- the sync's signed snapshot verifies, is
    unexpired and carries no camera_capacity -- the unsigned value of a sync
    received IN THIS PROCESS RUN is used (held in memory, never read back
    from disk), and never once a signed capacity has been accepted. It ends
    by itself within the snapshot TTL (24 h) after the cloud ships, and is
    removed in the release after that. The signed cloud change must be
    deployed before this release is installed for customers.
 3. migrated: FALLBACK_FILE, written once, root:root 0644, by the installer
    or Software Update when an appliance is upgraded from a release that
    did not sign capacity (installer/apply_release migration). Used only
    while no signed capacity is present, only for the activation identity
    it was migrated for, and only when it is a safe root-owned file. Keeps
    an upgraded offline appliance recording.
 4. none: fail closed.

STATE_FILE (the old unsigned vms_capacity.json) is still written, from
accepted values only, so a rollback to 1.2.4 keeps the right limit; it is
never read for enforcement.

A cloud process runs no camera pipeline and is never restricted here.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("anyaicam.vms_capacity")

STATE_FILE = Path(os.environ.get("ANYAICAM_VMS_CAPACITY_FILE", "/opt/anyaicam/data/config/vms_capacity.json"))
SIGNED_FILE = Path(os.environ.get("ANYAICAM_VMS_CAPACITY_SIGNED_FILE", "/opt/anyaicam/data/config/vms_capacity_signed.json"))
# Root-owned, beside the trust anchor (read-only mount). Deliberately not
# configurable from the environment: vms.env is writable by the anyaicam user.
FALLBACK_FILE = Path("/etc/anyaicam-update/vms_capacity_fallback.json")

MAX_CAMERA_SLOTS = 10_000
_MAX_FILE_BYTES = 256 * 1024

# Source 2: the unsigned capacity of a sync from a legacy cloud, this process run only.
_legacy_cloud_capacity: dict | None = None


def _quantity(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_CAMERA_SLOTS else None


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(path)
        return True
    except OSError as error:
        logger.warning("vms_capacity.persist_failed file=%s error=%s", path.name, type(error).__name__)
        return False


def _record(capacity: dict, source: str) -> dict | None:
    quantity = _quantity(capacity.get("camera_slot_quantity"))
    if quantity is None:
        return None
    return {"camera_slot_quantity": quantity, "plan_camera_slots": capacity.get("plan_camera_slots"),
            "vms_license_capacity": capacity.get("vms_license_capacity"), "source": source}


def _legacy_copy(record: dict) -> None:
    """The unsigned file a rollback to 1.2.4 reads; never read here."""
    _write_json(STATE_FILE, {key: record[key] for key in ("camera_slot_quantity", "plan_camera_slots", "vms_license_capacity")})


# ------------------------------------------------------------------ 1. signed

def _verified_capacity(snapshot, *, check_expiry: bool) -> tuple[str | None, dict | None]:
    """(reason, capacity record): reason is None when the snapshot verifies;
    the record is None when it carries no usable camera_capacity."""
    import appliance_entitlements
    reason = appliance_entitlements.verify(snapshot, appliance_entitlements._trusted_keys(),
                                           identity=appliance_entitlements._local_identity(),
                                           check_expiry=check_expiry)
    if reason:
        return reason, None
    capacity = snapshot.get("camera_capacity")
    return None, (_record(capacity, "signed") if isinstance(capacity, dict) else None)


def _stored_signed() -> tuple[dict | None, dict | None]:
    """(snapshot, record) of the stored signed capacity when it still
    verifies (signature, trust anchor, identity; expiry not enforced)."""
    snapshot = _read_json(SIGNED_FILE)
    if snapshot is None:
        return None, None
    reason, record = _verified_capacity(snapshot, check_expiry=False)
    if reason or record is None:
        logger.info("vms_capacity.signed_ignored reason=%s", reason or "malformed_capacity")
        return None, None
    return snapshot, record


def _issued(snapshot: dict | None) -> datetime | None:
    import appliance_entitlements
    return appliance_entitlements._parse_time((snapshot or {}).get("issued_at"))


# ------------------------------------------------------------------ 3. migrated

def _fallback_capacity() -> dict | None:
    """The installer/Software Update migrated limit, or None when absent,
    unsafe, malformed or for another identity."""
    import appliance_entitlements
    if not os.path.lexists(FALLBACK_FILE):
        return None
    problem = appliance_entitlements._anchor_file_problem(FALLBACK_FILE)
    if problem:
        logger.warning("vms_capacity.fallback_ignored problem=%s", problem)
        return None
    data = _read_json(FALLBACK_FILE)
    if not data or data.get("schema") != 1 or data.get("source") != "migrated_unsigned_capacity":
        logger.warning("vms_capacity.fallback_ignored problem=malformed")
        return None
    identity = appliance_entitlements._local_identity()
    if identity is None or any(data.get(field) != identity[field] for field in appliance_entitlements.BOUND_IDENTITY_FIELDS):
        logger.info("vms_capacity.fallback_ignored problem=other_identity")
        return None
    record = _record(data, "migrated")
    if record is None:
        logger.warning("vms_capacity.fallback_ignored problem=malformed")
    return record


# ------------------------------------------------------------------ sync and reads

def apply_sync(breakdown, snapshot) -> str:
    """Called by each configuration sync with its unsigned
    usable_camera_capacity and its signed entitlement snapshot. Returns the
    outcome. An invalid or capacity-less snapshot never removes an accepted
    signed limit (a bad sync does not stop recording)."""
    global _legacy_cloud_capacity
    if snapshot is None:
        return "absent"
    reason, record = _verified_capacity(snapshot, check_expiry=True)
    stored, _ = _stored_signed()
    if reason is None and record is not None:
        stored_issued, new_issued = _issued(stored), _issued(snapshot)
        if stored_issued and new_issued and new_issued < stored_issued:
            return "older_than_stored"
        if not _write_json(SIGNED_FILE, snapshot):
            return "persist_failed"
        _legacy_cloud_capacity = None
        _legacy_copy(record)
        return "signed"
    if reason is None and "camera_capacity" not in snapshot:
        # A cloud deployed before capacity was signed (transitional, see the module docstring).
        if stored is not None:
            return "ignored_unsigned"
        legacy = _record(breakdown, "legacy_cloud") if isinstance(breakdown, dict) else None
        if legacy is None:
            return "ignored_unsigned"
        _legacy_cloud_capacity = legacy
        _legacy_copy(legacy)
        return "legacy_cloud"
    logger.warning("vms_capacity.sync_rejected reason=%s", reason or "malformed_capacity")
    return f"rejected:{reason or 'malformed_capacity'}"


def load_capacity() -> dict | None:
    """The capacity in force -- {"camera_slot_quantity", ..., "source"} -- or
    None (fail closed). Never raises."""
    try:
        _, record = _stored_signed()
        if record is not None:
            return record
        if _legacy_cloud_capacity is not None:
            return dict(_legacy_cloud_capacity)
        return _fallback_capacity()
    except Exception:
        logger.exception("vms_capacity.load_failed")
        return None


def _cloud_runtime() -> bool:
    return os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower() == "cloud"


def _activated() -> bool:
    try:
        import appliance_activation
        return appliance_activation.active_appliance_id() is not None
    except Exception:
        return False


def licensed_camera_numbers(provisioned: list[int]) -> list[int] | None:
    """The provisioned camera numbers this appliance may run, or None for
    "no limit applies" (the cloud only, which runs no cameras)."""
    if _cloud_runtime():
        return None
    capacity = load_capacity()
    if capacity is None:
        return []  # no authoritative capacity: fail closed
    return sorted(provisioned)[: int(capacity["camera_slot_quantity"])]


def camera_licensed(camera_number: int, provisioned: list[int]) -> bool:
    allowed = licensed_camera_numbers(provisioned)
    return allowed is None or camera_number in allowed
