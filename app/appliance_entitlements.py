"""Paid-feature entitlements on the appliance (2026-10-05, Codex review of
51d1346: an edge runtime allowed Talk Down and AAC Voice Call without any
check, and an unknown runtime role did the same).

The appliance holds no billing tables. The cloud therefore sends, with every
configuration sync (edge_camera_sync.py, every ~60 s), a snapshot of only
what this appliance needs, signed with the cloud's Ed25519 identity key
(appliance_identity.sign_body):

    {"type": "anyaicam.feature_entitlements", "version": 1,
     "appliance_id", "cloud_id", "customer_id",
     "features": {"talk_down": bool, "voice_call": bool},
     "issued_at", "expires_at"}            # UTC ISO-8601

It is cached on disk so it keeps applying through a cloud outage, and
re-verified on every use. feature_allowed() is True only for a snapshot that
  * carries a valid signature from a trusted cloud key,
  * names exactly this appliance's persisted identity -- appliance_id,
    cloud_id and customer_id from the activation identity file, checked in
    verify() itself, so no caller can skip it (a caller that also names a
    customer must name that same one),
  * is not expired (and not issued in the future beyond clock skew),
  * grants that one feature.
Anything else -- missing, unreadable, tampered, another appliance's, expired,
unknown key -- denies that paid feature only. Recording, live view and the
rest of the VMS never consult this module.

Revocation: a cancelled/refunded/suspended plan or add-on reaches the
appliance on the next sync (about a minute while online); offline, the last
snapshot stops granting anything after its TTL (cloud setting
ANYAICAM_ENTITLEMENT_SNAPSHOT_TTL_HOURS, default 24).

Trusted keys (2026-10-05, staging finding on 1f66bcd: keys sent by config
sync could become trusted, so a forged snapshot signed with any key the sync
response supplied was accepted). The only trust anchor is the keyset a
release provisions -- the installer (installer/13-entitlement-signing-
keys.sh) or a signed Software Update (appliance-agent/system/
apply_release.py), each only when it matches the release's recorded digest:
TRUST_ANCHOR_FILE, root-owned under
/etc/anyaicam-update/ (the Software Update trust-anchor directory, outside
everything the anyaicam user owns), bind-mounted read-only into the VMS
container. Its path is fixed in code, not taken from the environment.
  * Keys in a configuration response, inside a snapshot, or in the local
    cache are never trusted; a sync can neither add nor replace a key.
  * No keyset, an unreadable/malformed one, or one not safely root-owned:
    every paid feature is denied (the VMS itself is unaffected).
  * A cached snapshot is re-verified against the keyset on every use, so one
    that does not verify grants nothing.
  * Rotation: the keyset holds several {key_id: public key}; a release
    adds the next cloud key before the cloud signs with it and drops the old
    one only after it is retired (docs/entitlement-signing-keys.md).
The cloud's private signing key never leaves the cloud.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("anyaicam.appliance_entitlements")

SNAPSHOT_TYPE = "anyaicam.feature_entitlements"
SNAPSHOT_VERSION = 1
FEATURES = ("talk_down", "voice_call")
CLOCK_SKEW = timedelta(minutes=5)

STATE_FILE = Path(os.environ.get("ANYAICAM_FEATURE_ENTITLEMENTS_FILE", "/opt/anyaicam/data/config/feature_entitlements.json"))


def _parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None  # a snapshot always carries UTC


# Installed by the installer, root:root 0644 in a root:root 0755 directory.
# Deliberately not configurable from the environment: vms.env is writable by
# the unprivileged anyaicam user.
TRUST_ANCHOR_FILE = Path("/etc/anyaicam-update/entitlement_signing_keys.json")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def ownership_problem(info) -> str:
    """Why an lstat() result is not a safe trust-anchor entry, or ""."""
    import stat
    if stat.S_ISLNK(info.st_mode):
        return "is a symbolic link"
    if info.st_uid != 0:
        return "is not owned by root"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "is writable by group or others"
    return ""


def _anchor_file_problem(path: Path) -> str:
    """The keyset and its directory must be root-owned, not symlinks, and not
    writable by anyone else -- or a less-privileged user could swap it. (POSIX
    only; a Windows development checkout has no such ownership model.)"""
    if os.name != "posix":
        return ""
    for candidate in (path.parent, path):
        try:
            problem = ownership_problem(os.lstat(candidate))
        except OSError:
            return f"{candidate} is missing"
        if problem:
            return f"{candidate} {problem}"
    return ""


def parse_trusted_keyset(raw: str) -> dict[str, str]:
    """{key_id: base64 Ed25519 public key} from the keyset document
    {"keys": {...}}. Raises ValueError for anything malformed -- one bad
    entry rejects the whole keyset rather than trusting part of it."""
    import base64
    document = json.loads(raw)
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, dict) or not keys:
        raise ValueError("the keyset holds no keys")
    trusted = {}
    for key_id, value in keys.items():
        if not isinstance(key_id, str) or not _KEY_ID_PATTERN.match(key_id):
            raise ValueError("a key id is malformed")
        if not isinstance(value, str):
            raise ValueError("a key is not a string")
        try:
            raw_key = base64.b64decode(value, validate=True)
        except ValueError as error:
            raise ValueError("a key is not base64") from error
        if len(raw_key) != 32:
            raise ValueError("a key is not a 32-byte Ed25519 public key")
        trusted[key_id] = value
    return trusted


def _trusted_keys() -> dict[str, str]:
    """The provisioned keyset, or {} (trust nothing) when it is missing,
    unsafe or malformed."""
    problem = _anchor_file_problem(TRUST_ANCHOR_FILE)
    if problem:
        logger.warning("appliance_entitlements.no_trust_anchor problem=%s", problem)
        return {}
    try:
        return parse_trusted_keyset(TRUST_ANCHOR_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("appliance_entitlements.no_trust_anchor problem=%s", error)
        return {}


def _read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> bool:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(STATE_FILE)
        return True
    except OSError as error:
        logger.warning("appliance_entitlements.persist_failed error=%s", type(error).__name__)
        return False


# The snapshot must name exactly the identity this appliance was activated
# with (appliance_activation's persisted identity file).
BOUND_IDENTITY_FIELDS = ("appliance_id", "cloud_id", "customer_id")


def _bound_identity(identity) -> dict[str, str] | None:
    """The appliance/cloud/customer ids from a persisted identity, or None
    when any is missing or not a non-empty string."""
    if not isinstance(identity, dict):
        return None
    bound = {field: identity.get(field) for field in BOUND_IDENTITY_FIELDS}
    if not all(isinstance(value, str) and value.strip() for value in bound.values()):
        return None
    return bound


def _local_identity() -> dict[str, str] | None:
    try:
        from appliance_activation import load_persisted_identity
        return _bound_identity(load_persisted_identity())
    except Exception:
        return None


def verify(snapshot, public_keys: dict[str, str], *, identity, now: datetime | None = None) -> str | None:
    """None when the snapshot is valid right now for this appliance's
    persisted identity, else the reason. The snapshot's appliance_id,
    cloud_id and customer_id must each be present and equal that identity;
    a missing or malformed persisted identity denies."""
    if not isinstance(snapshot, dict):
        return "missing"
    if not public_keys:
        return "no_trust_anchor"
    bound = _bound_identity(identity)
    if bound is None:
        return "not_activated"
    body = {key: value for key, value in snapshot.items() if key != "signature"}
    try:
        import appliance_identity
        appliance_identity.verify_signed_body(body, snapshot.get("signature") or {}, public_keys=public_keys)
    except Exception:
        return "bad_signature"
    if body.get("type") != SNAPSHOT_TYPE or body.get("version") != SNAPSHOT_VERSION:
        return "unsupported"
    for field, reason in (("appliance_id", "other_appliance"), ("cloud_id", "other_cloud_id"), ("customer_id", "other_customer")):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            return "malformed_identity"
        if value != bound[field]:
            return reason
    now = now or datetime.now(timezone.utc)
    issued, expires = _parse_time(body.get("issued_at")), _parse_time(body.get("expires_at"))
    if issued is None or expires is None or issued - CLOCK_SKEW > now:
        return "bad_time"
    if expires <= now:
        return "expired"
    if not isinstance(body.get("features"), dict):
        return "unsupported"
    return None


def store_snapshot(snapshot) -> str:
    """Called by each configuration sync. A valid snapshot replaces the cached
    one; an invalid one is not kept and clears the cache (fail closed -- a
    snapshot the cloud just sent that cannot be verified must not leave an
    older grant in force). A configuration with no snapshot at all (an older
    cloud) leaves the cache to expire on its own. Returns the outcome. Keys
    are never taken from the sync (see the module docstring)."""
    state = _read_state()
    state.pop("public_keys", None)  # keys an older build cached from sync are never trusted
    if snapshot is None:
        _write_state(state)
        return "absent"
    reason = verify(snapshot, _trusted_keys(), identity=_local_identity())
    if reason:
        logger.warning("appliance_entitlements.snapshot_rejected reason=%s", reason)
        state.pop("snapshot", None)
        _write_state(state)
        return f"rejected:{reason}"
    state["snapshot"] = snapshot
    _write_state(state)
    return "stored"


def feature_allowed(feature: str, customer_id: str | None = None) -> bool:
    """Whether this appliance may run a paid feature now. Never raises."""
    try:
        if feature not in FEATURES:
            return False
        state = _read_state()
        snapshot = state.get("snapshot")
        reason = verify(snapshot, _trusted_keys(), identity=_local_identity())
        if reason:
            logger.info("appliance_entitlements.denied feature=%s reason=%s", feature, reason)
            return False
        # verify() bound the snapshot to this appliance's own customer; a
        # caller naming a customer must name that same one.
        if customer_id is not None and str(customer_id) != snapshot["customer_id"]:
            return False
        return snapshot["features"].get(feature) is True
    except Exception:
        logger.exception("appliance_entitlements.check_failed feature=%s", feature)
        return False


def clear() -> None:
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
