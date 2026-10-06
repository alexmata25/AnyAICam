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

Trusted keys: ANYAICAM_CLOUD_SIGNING_PUBLIC_KEYS (JSON {key_id: base64 key})
pins them for the deployment; without it, the keys the cloud sends over the
authenticated configuration channel are cached next to the snapshot (so a
rotated key is picked up at the next sync).
"""
from __future__ import annotations

import json
import logging
import os
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


def _pinned_keys() -> dict[str, str] | None:
    raw = os.environ.get("ANYAICAM_CLOUD_SIGNING_PUBLIC_KEYS", "").strip()
    if not raw:
        return None
    try:
        keys = json.loads(raw)
    except ValueError:
        logger.error("appliance_entitlements.pinned_keys_unreadable")
        return {}  # a broken pin trusts nothing rather than everything
    return {str(k): str(v) for k, v in keys.items()} if isinstance(keys, dict) else {}


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


def _trusted_keys(state: dict) -> dict[str, str]:
    pinned = _pinned_keys()
    if pinned is not None:
        return pinned
    keys = state.get("public_keys")
    return {str(k): str(v) for k, v in keys.items()} if isinstance(keys, dict) else {}


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


def store_snapshot(snapshot, public_keys=None) -> str:
    """Called by each configuration sync. A valid snapshot replaces the cached
    one; an invalid one is not kept and clears the cache (fail closed -- a
    snapshot the cloud just sent that cannot be verified must not leave an
    older grant in force). A configuration with no snapshot at all (an older
    cloud) leaves the cache to expire on its own. Returns the outcome."""
    state = _read_state()
    if isinstance(public_keys, dict) and public_keys:
        state["public_keys"] = {str(k): str(v) for k, v in public_keys.items()}
    if snapshot is None:
        _write_state(state)
        return "absent"
    reason = verify(snapshot, _trusted_keys(state), identity=_local_identity())
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
        reason = verify(snapshot, _trusted_keys(state), identity=_local_identity())
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
