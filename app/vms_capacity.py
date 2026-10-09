"""Licensed camera capacity on the appliance.

Cloud configuration sync writes its signed camera-capacity snapshot only to
untrusted staging. A separate, root-owned systemd helper verifies the cloud
signature, fixed trust anchor, appliance/cloud/customer identity, expiry on
arrival, and monotonic issue-time high-water mark before atomically promoting
the snapshot to ACCEPTED_FILE. The VMS reads only that protected accepted
store. Its expiry is deliberately not enforced after acceptance so the last
authorized camera limit continues to apply during an offline period.

Eligible pre-1.2.5 installations can still use the existing root-owned
FALLBACK_FILE migration until a signed authorization is first accepted. That
migration preserves the existing offline policy and its documented
pre-migration provenance limitation. Unsigned cloud capacity is never used.
The old STATE_FILE is maintained by the root helper only as a compatibility
mirror for an intentional rollback to 1.2.4; this release never reads it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile

logger = logging.getLogger("anyaicam.vms_capacity")

STATE_FILE = Path("/opt/anyaicam/data/config/vms_capacity.json")
# Fixed input path: runtime environment from vms.env must not redirect staging.
SIGNED_FILE = Path("/opt/anyaicam/data/config/vms_capacity_signed.json")
# Root-owned beside the entitlement trust anchor; never environment-configurable.
ACCEPTED_FILE = Path("/etc/anyaicam-update/vms_capacity_accepted.json")
_DEFAULT_ACCEPTED_FILE = ACCEPTED_FILE
FALLBACK_FILE = Path("/etc/anyaicam-update/vms_capacity_fallback.json")
MAX_CAMERA_SLOTS = 10_000
_MAX_FILE_BYTES = 512 * 1024


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


def _record(capacity: dict, source: str) -> dict | None:
    quantity = _quantity(capacity.get("camera_slot_quantity"))
    if quantity is None:
        return None
    return {"camera_slot_quantity": quantity, "plan_camera_slots": capacity.get("plan_camera_slots"),
            "vms_license_capacity": capacity.get("vms_license_capacity"), "source": source}


def _verified_capacity(snapshot, *, check_expiry: bool) -> tuple[str | None, dict | None]:
    """(reason, capacity record): reason is None when signature and identity verify."""
    import appliance_entitlements
    reason = appliance_entitlements.verify(snapshot, appliance_entitlements._trusted_keys(),
                                           identity=appliance_entitlements._local_identity(),
                                           check_expiry=check_expiry)
    if reason:
        return reason, None
    capacity = snapshot.get("camera_capacity") if isinstance(snapshot, dict) else None
    return None, (_record(capacity, "signed") if isinstance(capacity, dict) else None)


def _accepted_signed() -> tuple[bool, dict | None]:
    """Return (accepted-store-present, usable record); unsafe presence fails closed."""
    import appliance_entitlements

    try:
        parent = os.lstat(ACCEPTED_FILE.parent)
        if (stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode) or
                (os.name == "posix" and ACCEPTED_FILE == _DEFAULT_ACCEPTED_FILE and
                 (parent.st_uid != 0 or parent.st_mode & 0o022))):
            return os.path.lexists(ACCEPTED_FILE), None
        try:
            before = os.lstat(ACCEPTED_FILE)
        except FileNotFoundError:
            return False, None
        if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                (os.name == "posix" and ACCEPTED_FILE == _DEFAULT_ACCEPTED_FILE and
                 (before.st_uid != 0 or before.st_mode & 0o022))):
            return True, None
        fd = os.open(ACCEPTED_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                    (os.name == "posix" and ACCEPTED_FILE == _DEFAULT_ACCEPTED_FILE and
                     (opened.st_uid != 0 or opened.st_mode & 0o022))):
                return True, None
            raw = handle.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            return True, None
        state = json.loads(raw.decode("utf-8"))
        if not isinstance(state, dict) or state.get("schema") != 1:
            return True, None
        accepted_identity = appliance_entitlements._bound_identity(state.get("identity"))
        local_identity = appliance_entitlements._local_identity()
        if accepted_identity is None or accepted_identity != local_identity:
            return True, None
        snapshot = state.get("snapshot")
        reason, record = _verified_capacity(snapshot, check_expiry=False)
        if reason or record is None:
            return True, None
        body = {key: value for key, value in snapshot.items() if key != "signature"}
        high_water = state.get("high_water")
        issued = appliance_entitlements._parse_time(body.get("issued_at"))
        normalized_issued = issued.astimezone(timezone.utc).isoformat() if issued else None
        if (not isinstance(high_water, dict) or normalized_issued is None or
                high_water.get("issued_at") != normalized_issued or
                high_water.get("digest") != hashlib.sha256(_canonical_json(body)).hexdigest()):
            return True, None
        return True, record
    except (OSError, ValueError, TypeError):
        return os.path.lexists(ACCEPTED_FILE), None


def _canonical_json(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fallback_capacity() -> dict | None:
    """The existing installer/Software Update migrated limit, if safe."""
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
    if identity is None or any(data.get(field) != identity[field]
                               for field in appliance_entitlements.BOUND_IDENTITY_FIELDS):
        logger.info("vms_capacity.fallback_ignored problem=other_identity")
        return None
    record = _record(data, "migrated")
    if record is None:
        logger.warning("vms_capacity.fallback_ignored problem=malformed")
    return record


def _stage_snapshot(path: Path, snapshot: dict) -> bool:
    """Stage untrusted cloud data using a unique no-follow temp and atomic replace."""
    raw = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > _MAX_FILE_BYTES:
        logger.warning("vms_capacity.stage_rejected reason=too_large")
        return False
    directory_fd = None
    temporary = f".{path.name}.{os.getpid()}.{os.urandom(12).hex()}.tmp"
    try:
        if os.name == "posix" and os.open in getattr(os, "supports_dir_fd", set()):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temporary = temp_path
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        return True
    except (OSError, TypeError, NotImplementedError) as error:
        logger.warning("vms_capacity.stage_failed error=%s", type(error).__name__)
        return False
    finally:
        try:
            if directory_fd is not None:
                os.unlink(temporary, dir_fd=directory_fd)
                os.close(directory_fd)
            elif isinstance(temporary, str) and os.path.isabs(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def apply_sync(breakdown, snapshot) -> str:
    """Stage signed data for the root service; unsigned capacity is ignored."""
    if snapshot is None:
        return "absent"
    if not isinstance(snapshot, dict):
        logger.warning("vms_capacity.stage_rejected reason=malformed_snapshot")
        return "stage_failed"
    return "staged" if _stage_snapshot(SIGNED_FILE, snapshot) else "stage_failed"


def load_capacity() -> dict | None:
    """The authorized capacity, or None (fail closed). Never raises."""
    try:
        store_exists, record = _accepted_signed()
        if record is not None:
            return record
        if store_exists:
            return None
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
    """Allowed provisioned numbers, or None for cloud runtime (no cameras)."""
    if _cloud_runtime():
        return None
    capacity = load_capacity()
    if capacity is None:
        return []
    return sorted(provisioned)[:int(capacity["camera_slot_quantity"])]


def camera_licensed(camera_number: int, provisioned: list[int]) -> bool:
    allowed = licensed_camera_numbers(provisioned)
    return allowed is None or camera_number in allowed
