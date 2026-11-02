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

TRUST-1 + G2 (Linux 1.2.7.1, wt-1271-design/TRUST1-G2-DESIGN.md):
  * The accepted grant's signer (snapshot signature key_id) is resolved on
    EVERY read against the CURRENT protected trust registry for purpose
    "capacity" (appliance_entitlements._trust_registry): active or retired
    verify; revoked, absent-after-binding, unknown, other-environment, rebound
    or a rolled-back registry give no capacity. The stored accepted_key and
    signer fields are cross-checks only and never confer trust.
  * Accepted mode: the accepted grant is verified without any clock check
    (appliance_entitlements.verify(accepted=True)), so a clock rollback or a
    cloud outage never removes the last-known-good capacity. A clock behind
    the trusted floor (max(high-water issue time, accepted_at)) is reported by
    capacity_status() as clock_behind_trusted_time, informational only; the
    root acceptor keeps refusing new grants that look future-dated until the
    clock is corrected (NTP), then accepts newer grants normally.
  * Every fail-closed outcome has an explicit reason (capacity_status()) and is
    logged once per change. Recordings, event media and credentials are never
    touched; the accepted file is kept as the anti-replay floor.
"""
from __future__ import annotations

import hashlib
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile

logger = logging.getLogger("anyaicam.vms_capacity")

from platform_paths import CONFIG_DIR as _CONFIG_DIR, UPDATE_DIR as _UPDATE_DIR  # noqa: E402

STATE_FILE = _CONFIG_DIR / "vms_capacity.json"
# Fixed input path: runtime environment from vms.env must not redirect staging.
SIGNED_FILE = _CONFIG_DIR / "vms_capacity_signed.json"
_DEFAULT_SIGNED_FILE = SIGNED_FILE  # tests redirect SIGNED_FILE; only the real path triggers acceptance
# Root-owned (Windows: SYSTEM/Administrators-only) beside the entitlement trust anchor; never environment-configurable.
ACCEPTED_FILE = _UPDATE_DIR / "vms_capacity_accepted.json"
_DEFAULT_ACCEPTED_FILE = ACCEPTED_FILE
FALLBACK_FILE = _UPDATE_DIR / "vms_capacity_fallback.json"
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
    # Gateway local operation uses the independent licence component of the
    # SAME verified grant, never unsigned sync or a guessed billing allowance.
    # Full VMS and legacy grants without this component retain the old limit.
    from gateway_edition import current_edition
    if source == "signed" and current_edition() in {"local_gateway", "cloud_gateway"} and "vms_license_capacity" in capacity:
        local_quantity = _quantity(capacity["vms_license_capacity"])
        if local_quantity is None:
            return None
        quantity = local_quantity
    return {"camera_slot_quantity": quantity, "plan_camera_slots": capacity.get("plan_camera_slots"),
            "vms_license_capacity": capacity.get("vms_license_capacity"), "source": source}


_TRUST_CRITICAL = frozenset(("binding_mismatch", "environment_mismatch", "invalid_registry", "invalid_trust_state",
                             "registry_rollback", "registry_unsafe", "revoked", "scope_denied", "trust_state_unsafe",
                             "unknown_key", "purpose_denied", "retired_cannot_sign"))


def _accepted_signed_detail(now: datetime | None = None) -> tuple[bool, dict | None, str | None, dict]:
    """(accepted-store-present, usable record, refusal reason, info). Unsafe presence fails closed with a reason.
    info: signer_key_id, trust_generation, trust_mode, trusted_floor (datetime) when known."""
    import appliance_entitlements

    info: dict = {}
    try:
        if os.name == "nt" and ACCEPTED_FILE == _DEFAULT_ACCEPTED_FILE:
            import platform_paths
            if platform_paths.windows_write_problem(ACCEPTED_FILE.parent):
                present = os.path.lexists(ACCEPTED_FILE)
                return present, None, ("accepted_store_unsafe" if present else None), info
            if os.path.lexists(ACCEPTED_FILE) and platform_paths.windows_write_problem(ACCEPTED_FILE):
                return True, None, "accepted_store_unsafe", info
        parent = os.lstat(ACCEPTED_FILE.parent)
        if (stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode) or
                (os.name == "posix" and ACCEPTED_FILE == _DEFAULT_ACCEPTED_FILE and
                 (parent.st_uid != 0 or parent.st_mode & 0o022))):
            present = os.path.lexists(ACCEPTED_FILE)
            return present, None, ("accepted_store_unsafe" if present else None), info
        try:
            before = os.lstat(ACCEPTED_FILE)
        except FileNotFoundError:
            return False, None, None, info
        if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                (os.name == "posix" and ACCEPTED_FILE == _DEFAULT_ACCEPTED_FILE and
                 (before.st_uid != 0 or before.st_mode & 0o022))):
            return True, None, "accepted_store_unsafe", info
        fd = os.open(ACCEPTED_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                    (os.name == "posix" and ACCEPTED_FILE == _DEFAULT_ACCEPTED_FILE and
                     (opened.st_uid != 0 or opened.st_mode & 0o022))):
                return True, None, "accepted_store_unsafe", info
            raw = handle.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            return True, None, "accepted_state_malformed", info
        state = json.loads(raw.decode("utf-8"))
        if not isinstance(state, dict) or state.get("schema") not in {1, 2}:
            return True, None, "accepted_state_malformed", info
        accepted_identity = appliance_entitlements._bound_identity(state.get("identity"))
        local_identity = appliance_entitlements._local_identity()
        if accepted_identity is None or accepted_identity != local_identity:
            return True, None, "identity_mismatch", info
        snapshot = state.get("snapshot")
        signature = snapshot.get("signature") if isinstance(snapshot, dict) else None
        key_id = signature.get("key_id") if isinstance(signature, dict) else None
        trust_generation = state.get("trust_generation", 0)
        if not isinstance(key_id, str) or type(trust_generation) is not int or trust_generation < 0:
            return True, None, "accepted_state_malformed", info
        info.update(signer_key_id=key_id, trust_generation=trust_generation)
        signer = state.get("signer")
        if signer is not None and (not isinstance(signer, dict) or signer.get("key_id") != key_id):
            return True, None, "binding_mismatch", info
        trust = appliance_entitlements._trust_registry()
        info["trust_mode"] = trust.mode
        try:
            key = trust.resolve(key_id, purpose="capacity", for_new_grant=False,
                                appliance_id=local_identity["appliance_id"], accepted_generation=trust_generation)
        except ValueError as error:  # TrustError (fail closed with the resolver's reason)
            return True, None, str(error), info
        if state.get("schema") == 2:
            try:
                accepted_key = base64.b64decode(state.get("accepted_key"), validate=True)
            except (TypeError, ValueError, base64.binascii.Error):
                return True, None, "accepted_state_malformed", info
            if accepted_key != key:
                return True, None, "binding_mismatch", info
        if signer is not None and signer.get("public_key_sha256") != hashlib.sha256(key).hexdigest():
            return True, None, "binding_mismatch", info
        reason = appliance_entitlements.verify(snapshot, {key_id: base64.b64encode(key).decode("ascii")},
                                               identity=local_identity, accepted=True)
        if reason:
            return True, None, reason, info
        capacity = snapshot.get("camera_capacity")
        record = _record(capacity, "signed") if isinstance(capacity, dict) else None
        if record is None:
            return True, None, "malformed_capacity", info
        body = {key: value for key, value in snapshot.items() if key != "signature"}
        high_water = state.get("high_water")
        issued = appliance_entitlements._parse_time(body.get("issued_at"))
        normalized_issued = issued.astimezone(timezone.utc).isoformat() if issued else None
        if (not isinstance(high_water, dict) or normalized_issued is None or
                high_water.get("issued_at") != normalized_issued or
                high_water.get("digest") != hashlib.sha256(_canonical_json(body)).hexdigest()):
            return True, None, "invalid_high_water", info
        transition = body.get("identity_transition")
        if transition is not None and (not isinstance(transition, dict) or
                                       state.get("transition_id") != transition.get("transition_id")):
            return True, None, "invalid_identity_transition", info
        floor = issued
        accepted_at = appliance_entitlements._parse_time(state.get("accepted_at"))
        if accepted_at is not None and accepted_at > floor:
            floor = accepted_at
        info["trusted_floor"] = floor
        return True, record, None, info
    except (OSError, ValueError, TypeError):
        present = os.path.lexists(ACCEPTED_FILE)
        return present, None, ("accepted_state_malformed" if present else None), info


def _accepted_signed(now: datetime | None = None) -> tuple[bool, dict | None]:
    """Return (accepted-store-present, usable record); unsafe presence fails closed."""
    present, record, _reason, _info = _accepted_signed_detail(now)
    return present, record


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
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    # After a successful os.replace(), the temporary name is
                    # gone already. Treat that as normal cleanup while still
                    # closing the directory descriptor below.
                    pass
                finally:
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
    if not _stage_snapshot(SIGNED_FILE, snapshot):
        return "stage_failed"
    if os.name == "nt" and SIGNED_FILE == _DEFAULT_SIGNED_FILE:
        return _accept_on_windows()
    return "staged"


def _accept_on_windows() -> str:
    """Windows step 4 (Codex 07e934a, ported): there is no separate root service to promote the staged snapshot --
    the VMS service already runs as LocalSystem -- so the acceptance helper runs here, with its own fixed paths, the
    trust-folder ACL checks and the release keyset. It never raises; any refusal keeps the last accepted capacity."""
    import sys
    system_dir = str(Path(__file__).resolve().parents[1] / "agent" / "system")
    if system_dir not in sys.path:
        sys.path.insert(0, system_dir)
    try:
        import capacity_accept_windows
        return capacity_accept_windows.accept_pending()
    except Exception as error:
        logger.warning("vms_capacity.accept_rejected error=%s", type(error).__name__)
        return "accept_rejected"


_last_status: dict = {"key": None}


def _reset_status_for_tests() -> None:
    _last_status["key"] = None


def _evaluate(now: datetime | None = None) -> dict:
    present, record, reason, info = _accepted_signed_detail(now)
    source = "signed"
    if record is None and not present:
        record, source = _fallback_capacity(), "migrated"
        if record is None:
            reason = "no_accepted_capacity"
    current = (now or datetime.now(timezone.utc))
    floor = info.get("trusted_floor")
    import appliance_entitlements
    status = {
        "state": "licensed" if record is not None else "fail_closed",
        "reason": None if record is not None else (reason or "unknown"),
        "source": source if record is not None else None,
        "camera_slot_quantity": record["camera_slot_quantity"] if record is not None else None,
        "signer_key_id": info.get("signer_key_id"),
        "trust_generation": info.get("trust_generation"),
        "trust_mode": info.get("trust_mode"),
        "clock_behind_trusted_time": bool(floor is not None and current < floor - appliance_entitlements.CLOCK_SKEW),
    }
    _note_status(status)
    return {"status": status, "record": record}


def _note_status(status: dict) -> None:
    """Log once per change (never per read/frame): a fail-closed reason at CRITICAL for trust failures, WARNING
    otherwise; recovery at WARNING."""
    key = (status["state"], status["reason"], status["source"])
    if _last_status["key"] == key:
        return
    previous, _last_status["key"] = _last_status["key"], key
    if status["state"] == "fail_closed":
        level = logging.CRITICAL if status["reason"] in _TRUST_CRITICAL else logging.WARNING
        logger.log(level, "vms_capacity.fail_closed reason=%s signer=%s trust_generation=%s trust_mode=%s -- no "
                   "camera is licensed until this is resolved; recordings are kept", status["reason"],
                   status["signer_key_id"], status["trust_generation"], status["trust_mode"])
    elif previous is not None and previous[0] == "fail_closed":
        logger.warning("vms_capacity.recovered source=%s cameras=%s", status["source"], status["camera_slot_quantity"])
    else:
        logger.info("vms_capacity.licensed source=%s cameras=%s", status["source"], status["camera_slot_quantity"])


def capacity_status(now: datetime | None = None) -> dict:
    """The authorized capacity and, when none, WHY (TRUST-1/G2 diagnostics): state 'licensed' | 'fail_closed' |
    'cloud_runtime', reason (registry_rollback, revoked, unknown_key, binding_mismatch, scope_denied,
    environment_mismatch, no_trust_anchor, identity_mismatch, no_accepted_capacity, ...), signer, trust generation and
    clock_behind_trusted_time (informational). Never raises."""
    if _cloud_runtime():
        return {"state": "cloud_runtime", "reason": None, "source": None, "camera_slot_quantity": None,
                "signer_key_id": None, "trust_generation": None, "trust_mode": None,
                "clock_behind_trusted_time": False}
    try:
        return _evaluate(now)["status"]
    except Exception:
        logger.exception("vms_capacity.status_failed")
        return {"state": "fail_closed", "reason": "status_failed", "source": None, "camera_slot_quantity": None,
                "signer_key_id": None, "trust_generation": None, "trust_mode": None,
                "clock_behind_trusted_time": False}


def load_capacity(now: datetime | None = None) -> dict | None:
    """The authorized capacity, or None (fail closed, with its reason in capacity_status()). Never raises."""
    try:
        return _evaluate(now)["record"]
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


def licensed_camera_numbers(provisioned: list[int], now: datetime | None = None) -> list[int] | None:
    """Allowed provisioned numbers, or None for cloud runtime (no cameras)."""
    if _cloud_runtime():
        return None
    capacity = load_capacity(now)
    if capacity is None:
        return []
    return sorted(provisioned)[:int(capacity["camera_slot_quantity"])]


def camera_licensed(camera_number: int, provisioned: list[int], now: datetime | None = None) -> bool:
    allowed = licensed_camera_numbers(provisioned, now)
    return allowed is None or camera_number in allowed
