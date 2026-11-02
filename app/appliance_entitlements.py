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

TRUST-1 (Linux 1.2.7.1, wt-1271-design/TRUST1-G2-DESIGN.md): the trust anchor
is the protected entitlement trust REGISTRY (TRUST_REGISTRY_FILE) and its
monotonic protected state (TRUST_STATE_FILE), both root-owned beside the flat
keyset and delivered only by the installer or a signed Software Update. Every
signer is resolved per purpose (capacity / features / analytics /
local_access) through the shared resolver below: ACTIVE keys verify new
grants; RETIRED keys verify only already stored grants (the feature snapshot
until its 24 h expiry, the analytics grant until its <= 168 h expiry, accepted
capacity until replaced); REVOKED keys -- and a bound key absent from a later
registry -- verify nothing; a lower registry generation, or a missing registry
once generation >= 1 was installed, is registry_rollback (fail closed). With no
registry and no generation >= 1 recorded anywhere, LEGACY mode treats the flat
keyset as active for every purpose (existing 1.2.7 installs, Windows 1.3.3).
No key in a snapshot, a cache, a sync response or an accepted grant is trusted.
Revocation reaches an appliance only with the installer or a Software Update
(Phase 1); an offline appliance keeps using a since-revoked signer's grants
until that update is installed (docs/entitlement-signing-keys.md).
"""
from __future__ import annotations

import json
import logging
import os
import platform_paths
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("anyaicam.appliance_entitlements")

SNAPSHOT_TYPE = "anyaicam.feature_entitlements"
SNAPSHOT_VERSION = 1
FEATURES = ("talk_down", "voice_call")
CLOCK_SKEW = timedelta(minutes=5)

STATE_FILE = Path(os.environ.get("ANYAICAM_FEATURE_ENTITLEMENTS_FILE", platform_paths.native_path("/opt/anyaicam/data/config/feature_entitlements.json", "config", "feature_entitlements.json")))


def _parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None  # a snapshot always carries UTC


# Installed by the installer, root:root 0644 in a root:root 0755 directory.
# Deliberately not configurable from the environment: vms.env is writable by
# the unprivileged anyaicam user.
# On Windows: %ProgramData%\AnyAiCam\update (platform_paths; SYSTEM/Administrators only, checked below).
from platform_paths import UPDATE_DIR as _UPDATE_DIR  # noqa: E402

TRUST_ANCHOR_FILE = _UPDATE_DIR / "entitlement_signing_keys.json"
# TRUST-1 (1.2.7.1): the protected registry and its monotonic state, root 0644 beside the flat keyset.
TRUST_REGISTRY_FILE = _UPDATE_DIR / "entitlement_trust_registry.json"
TRUST_STATE_FILE = _UPDATE_DIR / "entitlement_trust_state.json"
_MAX_TRUST_FILE_BYTES = 256 * 1024
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# ===== BEGIN SHARED ENTITLEMENT TRUST RESOLVER (TRUST-1, Linux 1.2.7.1) =====
# Byte-identical in app/appliance_entitlements.py, appliance-agent/system/capacity_accept.py and
# appliance-agent/anyaicam_agent/updater/release_checks.py (appliance-agent/tests/test_trust_registry_1271.py compares
# the three copies). Standard library only; no I/O. Design: wt-1271-design/TRUST1-G2-DESIGN.md.
#
# Registry (/etc/anyaicam-update/entitlement_trust_registry.json, root 0644, delivered ONLY by the installer / the
# signed Software Update release chain, never by cloud configuration sync and never signed by an entitlement key):
#   {"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": "production", "generation": N,
#    "issued_at": ISO-8601, "keys": {key_id: {"public_key": base64, "state": "active|retired|revoked",
#    "purposes": [capacity|features|analytics|local_access], "environment": ..., "changed_generation": n,
#    "appliance_ids": [optional allow-list]}}}
# Protected state (entitlement_trust_state.json, root 0644): {"schema": 1, "environment", "generation",
#   "registry_sha256", "key_bindings": {key_id: sha256(public key)}, "revoked": {key_id: sha256(public key)}}.
#   The generation never decreases, revoked only grows, a key_id is never rebound, the environment is pinned.
# Lifecycle: active = signs and validates; retired = validates already-accepted/cached grants only; revoked (or a
#   bound key_id absent from a later registry) = nothing. A key from another environment never authorizes capacity
#   and authorizes other purposes only for the appliances it lists.
# LEGACY mode (no registry installed): the flat keyset is active for every purpose at generation 0 -- only while the
#   protected state is absent or at generation 0 and no accepted grant records a registry generation >= 1. Otherwise
#   a missing registry is registry_rollback. No key embedded in an accepted grant ever confers trust.
import base64 as _trust_base64
import datetime as _trust_datetime
import hashlib as _trust_hashlib
import json as _trust_json
import re as _trust_re

TRUST_REGISTRY_TYPE = "anyaicam.entitlement_trust_registry"
TRUST_REGISTRY_VERSION = 1
TRUST_STATE_SCHEMA = 1
TRUST_PURPOSES = ("analytics", "capacity", "features", "local_access")
TRUST_KEY_STATES = ("active", "retired", "revoked")
TRUST_LEGACY_ENVIRONMENT = "production"
TRUST_MAX_KEYS = 256
# Every reason a trust resolution can fail with (diagnostics; vms_capacity.capacity_status reports them).
TRUST_FAILURE_REASONS = frozenset((
    "binding_mismatch", "environment_mismatch", "invalid_registry", "invalid_trust_anchor", "invalid_trust_state",
    "no_trust_anchor", "purpose_denied", "registry_rollback", "registry_unsafe", "retired_cannot_sign", "revoked",
    "scope_denied", "trust_state_unsafe", "trust_unavailable", "unknown_key"))
_TRUST_NAME = _trust_re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_TRUST_HEX = _trust_re.compile(r"^[0-9a-f]{64}$")
_TRUST_REGISTRY_FIELDS = frozenset(("type", "version", "environment", "generation", "issued_at", "keys"))
_TRUST_KEY_REQUIRED = frozenset(("public_key", "state", "purposes", "environment", "changed_generation"))
_TRUST_KEY_OPTIONAL = frozenset(("appliance_ids",))
_TRUST_STATE_FIELDS = frozenset(("schema", "environment", "generation", "registry_sha256", "key_bindings", "revoked"))
_TRUST_LIFECYCLE_RANK = {"active": 0, "retired": 1, "revoked": 2}


class TrustError(ValueError):
    """A fail-closed trust refusal; str(error) is the stable reason shown in diagnostics."""


def _trust_int(value, minimum):
    return type(value) is int and value >= minimum


def _trust_public_key_bytes(value, reason):
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        try:
            raw = _trust_base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise TrustError(reason)
    else:
        raise TrustError(reason)
    if len(raw) != 32:
        raise TrustError(reason)
    return raw


def trust_key_fingerprint(public_key):
    """SHA-256 hex of a raw 32-byte Ed25519 public key (given as bytes or base64 text)."""
    return _trust_hashlib.sha256(_trust_public_key_bytes(public_key, "invalid_registry")).hexdigest()


def canonical_trust_registry(document):
    """The one canonical byte form of a registry (sorted keys, indent 2, LF-terminated ASCII). Its SHA-256 is what
    release.env, the release builder, the installer and the protected trust state record."""
    return (_trust_json.dumps(document, sort_keys=True, indent=2) + "\n").encode("ascii")


def canonical_trust_state(state):
    return (_trust_json.dumps(state, sort_keys=True, indent=2) + "\n").encode("ascii")


def parse_trust_registry(document):
    """The normalized registry, or TrustError('invalid_registry'). Strict: unknown fields, duplicate key material,
    a changed_generation above the generation, or anything malformed rejects the whole registry."""
    bad = "invalid_registry"
    if not isinstance(document, dict) or set(document) != _TRUST_REGISTRY_FIELDS:
        raise TrustError(bad)
    if document["type"] != TRUST_REGISTRY_TYPE or type(document["version"]) is not int or \
            document["version"] != TRUST_REGISTRY_VERSION:
        raise TrustError(bad)
    environment, generation, issued_at = document["environment"], document["generation"], document["issued_at"]
    if not isinstance(environment, str) or not _TRUST_NAME.match(environment) or not _trust_int(generation, 1):
        raise TrustError(bad)
    try:
        issued = _trust_datetime.datetime.fromisoformat(issued_at) if isinstance(issued_at, str) else None
    except ValueError:
        issued = None
    if issued is None or issued.tzinfo is None:
        raise TrustError(bad)
    keys = document["keys"]
    if not isinstance(keys, dict) or not keys or len(keys) > TRUST_MAX_KEYS:
        raise TrustError(bad)
    entries, seen = {}, set()
    for key_id, entry in keys.items():
        if not isinstance(key_id, str) or not _TRUST_NAME.match(key_id) or not isinstance(entry, dict):
            raise TrustError(bad)
        fields = set(entry)
        if not _TRUST_KEY_REQUIRED <= fields or not fields <= (_TRUST_KEY_REQUIRED | _TRUST_KEY_OPTIONAL):
            raise TrustError(bad)
        raw = _trust_public_key_bytes(entry["public_key"], bad)
        fingerprint = _trust_hashlib.sha256(raw).hexdigest()
        if fingerprint in seen:
            raise TrustError(bad)
        seen.add(fingerprint)
        purposes = entry["purposes"]
        if entry["state"] not in TRUST_KEY_STATES or not isinstance(purposes, list) or not purposes or \
                len(set(purposes)) != len(purposes) or any(purpose not in TRUST_PURPOSES for purpose in purposes):
            raise TrustError(bad)
        key_environment, changed = entry["environment"], entry["changed_generation"]
        if not isinstance(key_environment, str) or not _TRUST_NAME.match(key_environment) or \
                not _trust_int(changed, 1) or changed > generation:
            raise TrustError(bad)
        appliance_ids = None
        if "appliance_ids" in entry:
            listed = entry["appliance_ids"]
            if not isinstance(listed, list) or not listed or len(listed) > 1024 or any(
                    not isinstance(value, str) or not value.strip() or len(value) > 256 for value in listed):
                raise TrustError(bad)
            appliance_ids = frozenset(listed)
        entries[key_id] = {"public_key": raw, "fingerprint": fingerprint, "state": entry["state"],
                           "purposes": frozenset(purposes), "environment": key_environment,
                           "appliance_ids": appliance_ids, "changed_generation": changed}
    return {"normalized": True, "legacy": False, "environment": environment, "generation": generation,
            "issued_at": issued_at, "keys": entries}


def legacy_trust_registry(flat_keys):
    """LEGACY mode: the flat keyset {key_id: public key (base64 or raw bytes)} as active keys for every purpose,
    environment production, generation 0."""
    entries = {}
    for key_id, value in (flat_keys or {}).items():
        if not isinstance(key_id, str) or not _TRUST_NAME.match(key_id):
            raise TrustError("invalid_trust_anchor")
        raw = _trust_public_key_bytes(value, "invalid_trust_anchor")
        entries[key_id] = {"public_key": raw, "fingerprint": _trust_hashlib.sha256(raw).hexdigest(),
                           "state": "active", "purposes": frozenset(TRUST_PURPOSES),
                           "environment": TRUST_LEGACY_ENVIRONMENT, "appliance_ids": None, "changed_generation": 0}
    return {"normalized": True, "legacy": True, "environment": TRUST_LEGACY_ENVIRONMENT, "generation": 0,
            "issued_at": None, "keys": entries}


def _trust_normalized(registry):
    if registry is None or (isinstance(registry, dict) and registry.get("normalized") is True):
        return registry
    return parse_trust_registry(registry)


def parse_trust_state(document):
    """The protected trust state, or TrustError('invalid_trust_state')."""
    bad = "invalid_trust_state"
    if not isinstance(document, dict) or set(document) != _TRUST_STATE_FIELDS:
        raise TrustError(bad)
    if type(document["schema"]) is not int or document["schema"] != TRUST_STATE_SCHEMA:
        raise TrustError(bad)
    environment, generation, digest = document["environment"], document["generation"], document["registry_sha256"]
    if not isinstance(environment, str) or not _TRUST_NAME.match(environment) or not _trust_int(generation, 0):
        raise TrustError(bad)
    if not isinstance(digest, str) or not (_TRUST_HEX.match(digest) or (digest == "" and generation == 0)):
        raise TrustError(bad)
    maps = {}
    for field in ("key_bindings", "revoked"):
        value = document[field]
        if not isinstance(value, dict) or len(value) > 4 * TRUST_MAX_KEYS or any(
                not isinstance(key_id, str) or not _TRUST_NAME.match(key_id) or
                not isinstance(fingerprint, str) or not _TRUST_HEX.match(fingerprint)
                for key_id, fingerprint in value.items()):
            raise TrustError(bad)
        maps[field] = dict(sorted(value.items()))
    return {"schema": TRUST_STATE_SCHEMA, "environment": environment, "generation": generation,
            "registry_sha256": digest, "key_bindings": maps["key_bindings"], "revoked": maps["revoked"]}


def next_trust_state(registry, registry_sha256, state):
    """The protected state after installing `registry` (whose canonical bytes hash to registry_sha256) over `state`
    (None on first install). Monotonic: a lower generation, or a different document at the same generation, is
    registry_rollback; a key_id bound to other key material is binding_mismatch; a revoked key never returns; every
    bound key_id absent from the registry becomes revoked. An identical replay returns the same state."""
    registry = _trust_normalized(registry)
    if registry is None or registry["legacy"]:
        raise TrustError("invalid_registry")
    if not isinstance(registry_sha256, str) or not _TRUST_HEX.match(registry_sha256):
        raise TrustError("invalid_registry")
    bindings, revoked = {}, {}
    if state is not None:
        prior = parse_trust_state(state)
        if prior["environment"] != registry["environment"]:
            raise TrustError("environment_mismatch")
        if registry["generation"] < prior["generation"]:
            raise TrustError("registry_rollback")
        if registry["generation"] == prior["generation"] and prior["registry_sha256"] != registry_sha256:
            raise TrustError("registry_rollback")
        bindings, revoked = dict(prior["key_bindings"]), dict(prior["revoked"])
    revoked_material = set(revoked.values())
    for key_id, entry in registry["keys"].items():
        bound = bindings.get(key_id)
        if bound is not None and bound != entry["fingerprint"]:
            raise TrustError("binding_mismatch")
        if entry["state"] != "revoked" and (key_id in revoked or entry["fingerprint"] in revoked_material):
            raise TrustError("registry_rollback")
        bindings[key_id] = entry["fingerprint"]
    for key_id, fingerprint in bindings.items():
        entry = registry["keys"].get(key_id)
        if entry is None or entry["state"] == "revoked":
            revoked[key_id] = fingerprint
    return {"schema": TRUST_STATE_SCHEMA, "environment": registry["environment"], "generation": registry["generation"],
            "registry_sha256": registry_sha256, "key_bindings": dict(sorted(bindings.items())),
            "revoked": dict(sorted(revoked.items()))}


def check_trust_registry_transition(old_registry, new_registry):
    """Tooling / installer / applier guard from the installed registry to a new one: never a lower generation (an
    identical replay is a no-op), never another environment, never a dropped key_id (explicit tombstones only),
    never a key_id rebound to other key material, never a backwards lifecycle move (revoked->*, retired->active)."""
    new = _trust_normalized(new_registry)
    if new is None or new["legacy"]:
        raise TrustError("invalid_registry")
    old = _trust_normalized(old_registry)
    if old is None or old["legacy"]:
        return
    if new["environment"] != old["environment"]:
        raise TrustError("environment_mismatch")
    if new["generation"] < old["generation"]:
        raise TrustError("registry_rollback")
    if new["generation"] == old["generation"]:
        if new != old:
            raise TrustError("registry_rollback")
        return
    old_material = {entry["fingerprint"]: key_id for key_id, entry in old["keys"].items()}
    for key_id in sorted(old["keys"]):
        before, after = old["keys"][key_id], new["keys"].get(key_id)
        if after is None:
            raise TrustError("dropped_binding")
        if after["fingerprint"] != before["fingerprint"]:
            raise TrustError("binding_mismatch")
        if _TRUST_LIFECYCLE_RANK[after["state"]] < _TRUST_LIFECYCLE_RANK[before["state"]]:
            raise TrustError("lifecycle_backwards")
    for key_id, entry in new["keys"].items():
        if key_id not in old["keys"] and entry["fingerprint"] in old_material:
            raise TrustError("binding_mismatch")


class TrustContext:
    """One view of the installed trust: a normalized (or raw) registry -- None when none is installed -- plus the
    protected state. resolve() is the only way to obtain a verification key."""

    def __init__(self, registry, state=None, *, failure=None, registry_sha256=None, floor=0):
        self.failure = failure
        self.registry = _trust_normalized(registry)
        self.state = parse_trust_state(state) if state is not None else None
        self.registry_sha256 = registry_sha256
        self.floor = floor if type(floor) is int and floor > 0 else 0

    @property
    def mode(self):
        if self.failure:
            return "failed"
        return "registry" if self.registry is not None and not self.registry["legacy"] else "legacy"

    @property
    def generation(self):
        return self.registry["generation"] if self.registry is not None else 0

    def check(self, accepted_generation=0):
        """The registry if it may be used at all, else TrustError."""
        if self.failure:
            raise TrustError(self.failure)
        registry, state = self.registry, self.state
        floor = max(self.floor, state["generation"] if state is not None else 0,
                    accepted_generation if type(accepted_generation) is int else 0)
        if registry is None or registry["legacy"]:
            if floor > 0:
                raise TrustError("registry_rollback")
            if registry is None or not registry["keys"]:
                raise TrustError("no_trust_anchor")
        if registry["generation"] < floor:
            raise TrustError("registry_rollback")
        if state is not None:
            if state["environment"] != registry["environment"]:
                raise TrustError("environment_mismatch")
            if (not registry["legacy"] and registry["generation"] == state["generation"] and
                    self.registry_sha256 is not None and self.registry_sha256 != state["registry_sha256"]):
                raise TrustError("registry_rollback")
        return registry

    def resolve(self, key_id, *, purpose, for_new_grant, appliance_id=None, accepted_generation=0):
        """The raw public key that may verify a document signed by key_id for `purpose`, else TrustError.
        for_new_grant=True: a new grant (active keys only); False: an already accepted/cached grant (retired too)."""
        registry = self.check(accepted_generation)
        if purpose not in TRUST_PURPOSES:
            raise TrustError("purpose_denied")
        state = self.state
        entry = registry["keys"].get(key_id) if isinstance(key_id, str) else None
        if entry is None:
            if state is not None and isinstance(key_id, str) and (
                    key_id in state["revoked"] or key_id in state["key_bindings"]):
                raise TrustError("revoked")
            raise TrustError("unknown_key")
        if state is not None:
            bound = state["key_bindings"].get(key_id)
            if bound is not None and bound != entry["fingerprint"]:
                raise TrustError("binding_mismatch")
            if key_id in state["revoked"] or entry["fingerprint"] in state["revoked"].values():
                raise TrustError("revoked")
        if entry["state"] == "revoked":
            raise TrustError("revoked")
        if for_new_grant and entry["state"] != "active":
            raise TrustError("retired_cannot_sign")
        if purpose not in entry["purposes"]:
            raise TrustError("purpose_denied")
        if entry["environment"] != registry["environment"]:
            if purpose == "capacity" or entry["appliance_ids"] is None or appliance_id not in entry["appliance_ids"]:
                raise TrustError("scope_denied")
        elif entry["appliance_ids"] is not None and appliance_id not in entry["appliance_ids"]:
            raise TrustError("scope_denied")
        return entry["public_key"]

    def active_keys(self, purpose, appliance_id=None):
        """{key_id: base64 public key} that may verify NEW documents for `purpose`; {} on any trust failure."""
        try:
            registry = self.check()
        except TrustError:
            return {}
        keys = {}
        for key_id in sorted(registry["keys"]):
            try:
                raw = self.resolve(key_id, purpose=purpose, for_new_grant=True, appliance_id=appliance_id)
            except TrustError:
                continue
            keys[key_id] = _trust_base64.b64encode(raw).decode("ascii")
        return keys
# ===== END SHARED ENTITLEMENT TRUST RESOLVER =====



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
    writable by anyone else -- or a less-privileged user could swap it. On
    Windows the same rule is: owned by and writable only by SYSTEM or
    Administrators, and not a reparse point (platform_paths). Other platforms
    (a development checkout) are not checked."""
    if os.name == "nt":
        import platform_paths
        for candidate in (path.parent, path):
            try:
                problem = platform_paths.windows_write_problem(candidate)
            except OSError:
                return f"{candidate} is missing"
            if problem:
                return f"{candidate} {problem}"
        return ""
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


def _flat_keyset() -> dict[str, str]:
    """The provisioned flat keyset, or {} (trust nothing) when it is missing,
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


class _UnsafeLicensingFile(OSError):
    """A licensing file that is a link, or has a second name. Every reader already fails closed on OSError."""


def _read_single_name(path: Path, limit: int) -> bytes:
    """Up to limit + 1 bytes, proved safe on the open handle, so a swap after any earlier path check can't be read."""
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise _UnsafeLicensingFile("is a link")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _UnsafeLicensingFile("not a single-name regular file")
        return handle.read(limit + 1)


def _write_single_name(path: Path, text: str) -> None:
    """A unique exclusive temporary, then an atomic replace: nothing is ever written through a planted name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode) if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_trust_file(path: Path) -> tuple[bytes | None, str]:
    """(bytes, "") / (None, "") when absent / (None, problem) when present but unsafe or unreadable."""
    if not os.path.lexists(path):
        return None, ""
    problem = _anchor_file_problem(path)
    if problem:
        return None, problem
    try:
        data = _read_single_name(path, _MAX_TRUST_FILE_BYTES)
    except OSError as error:
        return None, type(error).__name__
    if len(data) > _MAX_TRUST_FILE_BYTES:
        return None, "too large"
    return data, ""


def _installed_trust() -> tuple[str, dict | None, dict | None, str | None, str | None]:
    """(mode, registry, state, registry_sha256, failure): mode 'registry' (a registry is installed), 'legacy' (none
    is) or 'failed' (a trust file is present but unsafe/malformed -- fail closed with that reason)."""
    import hashlib
    state_raw, problem = _read_trust_file(TRUST_STATE_FILE)
    if problem:
        logger.warning("appliance_entitlements.trust_state_rejected problem=%s", problem)
        return "failed", None, None, None, "trust_state_unsafe"
    state = None
    if state_raw is not None:
        try:
            state = parse_trust_state(json.loads(state_raw))
        except (ValueError, TrustError):
            return "failed", None, None, None, "invalid_trust_state"
    registry_raw, problem = _read_trust_file(TRUST_REGISTRY_FILE)
    if problem:
        logger.warning("appliance_entitlements.registry_rejected problem=%s", problem)
        return "failed", None, state, None, "registry_unsafe"
    if registry_raw is None:
        return "legacy", None, state, None, None
    try:
        registry = parse_trust_registry(json.loads(registry_raw))
    except (ValueError, TrustError):
        return "failed", None, state, None, "invalid_registry"
    return "registry", registry, state, hashlib.sha256(registry_raw).hexdigest(), None


def _accepted_trust_generation() -> int:
    """Return the rollback floor; only a genuinely absent accepted record means legacy mode (floor zero).

    A present record that is linked, unreadable, malformed, or has an invalid generation must fail closed. Treating
    those states as zero would let a damaged/deleted registry fall back to the legacy flat keyset.
    """
    try:
        import vms_capacity
        accepted_file = vms_capacity.ACCEPTED_FILE
        if not os.path.lexists(accepted_file):
            return 0
        raw = _read_single_name(accepted_file, 512 * 1024)
        if len(raw) > 512 * 1024:
            return 1
        data = json.loads(raw)
        if not isinstance(data, dict):
            return 1
        # Schema-2 1.2.7 and legacy-mode 1.3.4 accepted records predate trust generations; they are valid floor zero.
        if "trust_generation" not in data:
            return 0
        generation = data.get("trust_generation")
        if type(generation) is int and generation >= 0:
            return generation
        return 1
    except Exception:
        # Presence was established above whenever possible; if the check/read itself fails, do not enter legacy mode.
        return 1


def _trusted_keys() -> dict[str, str]:
    """Keys that may verify NEW documents: LEGACY mode -- the flat keyset; registry mode -- the ACTIVE registry keys
    (any purpose). {} when trust is unavailable. Purpose-specific callers use trusted_keys_for()."""
    mode, registry, state, registry_sha256, failure = _installed_trust()
    if failure:
        return {}
    if mode == "legacy":
        if (state is not None and state["generation"] > 0) or _accepted_trust_generation() > 0:
            return {}  # a registry was installed once: its absence is a rollback, never the flat keyset
        return _flat_keyset()
    context = TrustContext(registry, state, registry_sha256=registry_sha256, floor=_accepted_trust_generation())
    keys = {}
    for purpose in TRUST_PURPOSES:
        keys.update(context.active_keys(purpose, appliance_id=_local_appliance_id()))
    return keys


def _trust_registry() -> TrustContext:
    """The CURRENT installed trust as a resolver (never raises; a failure resolves nothing, with its reason)."""
    try:
        mode, registry, state, registry_sha256, failure = _installed_trust()
        if failure:
            return TrustContext(None, None, failure=failure)
        floor = _accepted_trust_generation()
        if mode == "legacy":
            try:
                legacy = legacy_trust_registry(_trusted_keys())
            except TrustError as error:
                return TrustContext(None, None, failure=str(error))
            return TrustContext(legacy, state, floor=floor)
        return TrustContext(registry, state, registry_sha256=registry_sha256, floor=floor)
    except Exception:
        logger.exception("appliance_entitlements.trust_unavailable")
        return TrustContext(None, None, failure="trust_unavailable")


def _local_appliance_id() -> str | None:
    identity = _local_identity()
    return identity.get("appliance_id") if isinstance(identity, dict) else None


def trusted_keys_for(purpose: str, *, appliance_id: str | None = None) -> dict[str, str]:
    """{key_id: base64} ACTIVE for `purpose` in the current trust (new documents only); {} on any trust failure."""
    return _trust_registry().active_keys(purpose, appliance_id=appliance_id or _local_appliance_id())


def _verify_trusted(document, verifier, *, purpose: str, for_new_grant: bool, **kwargs) -> str | None:
    """Resolve the document's signer for `purpose` under the CURRENT trust, then verify with that one key. Returns
    None or the refusal reason. An unknown key id keeps the historical 'bad_signature' reason here (features and
    analytics); capacity reports the trust reason itself (vms_capacity.capacity_status)."""
    if not isinstance(document, dict):
        return "missing"
    signature = document.get("signature")
    key_id = signature.get("key_id") if isinstance(signature, dict) else None
    try:
        raw = _trust_registry().resolve(key_id, purpose=purpose, for_new_grant=for_new_grant,
                                        appliance_id=_local_appliance_id())
    except ValueError as error:  # TrustError: a fail-closed refusal with the resolver's reason
        reason = str(error)
        return "bad_signature" if reason == "unknown_key" else reason
    import base64
    return verifier(document, {key_id: base64.b64encode(raw).decode("ascii")}, **kwargs)


def _read_state() -> dict:
    try:
        data = json.loads(_read_single_name(STATE_FILE, _MAX_TRUST_FILE_BYTES))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> bool:
    try:
        _write_single_name(STATE_FILE, json.dumps(state))
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


def verify(snapshot, public_keys: dict[str, str], *, identity, now: datetime | None = None,
           check_expiry: bool = True, accepted: bool = False) -> str | None:
    """None when the snapshot is valid right now for this appliance's
    persisted identity, else the reason. The snapshot's appliance_id,
    cloud_id and customer_id must each be present and equal that identity;
    a missing or malformed persisted identity denies.

    check_expiry=False (camera capacity only, vms_capacity.py): everything is
    verified except expires_at, so the licensed camera limit keeps applying
    to an offline appliance (recording never stops because the cloud is
    unreachable). Paid features always check expiry.

    accepted=True (G2, already accepted capacity only, vms_capacity.py): no
    clock check at all -- neither "issued in the future" nor expiry -- so a
    clock rollback never invalidates the last-known-good capacity; issued_at
    and expires_at must still parse and expires_at must follow issued_at."""
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
    if issued is None or expires is None:
        return "bad_time"
    if accepted:
        if expires <= issued:
            return "bad_time"
    elif issued - CLOCK_SKEW > now:
        return "bad_time"
    elif check_expiry and expires <= now:
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
    reason = _verify_trusted(snapshot, verify, purpose="features", for_new_grant=True, identity=_local_identity())
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
        # A cached snapshot: a RETIRED signer still verifies it until its own expiry (24 h); REVOKED does not.
        reason = _verify_trusted(snapshot, verify, purpose="features", for_new_grant=False, identity=_local_identity())
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


# ---------------------------------------------------------------- premium analytics: signed 168 h grant (2026-10-19)
# Owner-approved design RED-ANALYTICS-OFFLINE-168H-DESIGN-20261019.md (with Green F1/F2). Premium analytics run only
# with a separately signed "anyaicam.analytics_entitlements" document naming this camera and feature (the customer's
# explicit ON is a separate, independent requirement). recording_uploader ANDs every premium flag with
# camera_analytic_grants() on every pipeline read. Talk Down / Voice Call (the 24 h snapshot above) are unaffected.
#
# Accepted only if: signed by a key in the provisioned trust anchor; exact type/version; bound to this appliance's
# activation identity; issued_at not in the future (beyond skew); expires_at > now; expires_at - issued_at <= 168 h
# (+ skew) -- so no cloud or client configuration can extend authority; camera_analytics well-formed.
#
# Persisted state (ANALYTICS_STATE_FILE): {"grant", "high_water", "mac"}; HMAC-SHA256 keyed from this appliance's own
# ANYAICAM_APP_SECRETS. Missing / corrupt / MAC mismatch / no secret -> no grant (fail closed) until the next sync.
# high_water = the latest time observed (persisted at most once a minute, and whenever a grant is stored); a clock
# earlier than high_water - 5 min is a rollback -> no grant until a fresh document verifies.
# Renewal (store_analytics_grant, called ONLY with a successful authenticated configuration response):
#   valid and issued_at >= stored -> replaces it;  valid but OLDER than stored (Green F1) -> CLEARS (fail closed);
#   invalid -> clears;  absent (an older/rolled-back cloud no longer vouching) -> clears.
# Offline (no successful sync) nothing changes: the grant keeps applying until its own expires_at (<= 168 h after the
# last successful refresh) -- the explicit, unavoidable offline revocation delay.
# Software-only limitation: a local administrator who controls the clock AND the state file AND the app secret can
# forge the local state; the HMAC defeats accidental and unprivileged edits only (see the design document).
import hashlib as _hashlib
import hmac as _hmac

PREMIUM_ANALYTICS = ("smart_motion", "people_counting", "lpr", "ppe", "facial_recognition")
ANALYTICS_TYPE = "anyaicam.analytics_entitlements"
ANALYTICS_VERSION = 1
ANALYTICS_MAX_WINDOW = timedelta(hours=168)
HIGH_WATER_PERSIST = timedelta(minutes=1)
ANALYTICS_STATE_FILE = STATE_FILE.with_name("analytics_entitlements.json")
_GRANT_CACHE_SECONDS = 10.0
_grant_cache: dict = {"at": None, "mtime": None, "trust": None, "grants": None, "expires": None}


def _trust_fingerprint() -> tuple:
    """Changes whenever a trust file does, so a revocation never waits for the grant cache."""
    found = []
    for path in (TRUST_ANCHOR_FILE, TRUST_REGISTRY_FILE, TRUST_STATE_FILE):
        try:
            info = os.lstat(path)
            # Link count is part of the identity: adding a hard link does not necessarily alter mtime, size, or inode,
            # but it makes the trust file unsafe. Invalidate a cached grant before it can outlive that change.
            found.append((info.st_mtime_ns, info.st_size, info.st_ino, info.st_nlink, info.st_mode))
        except OSError:
            found.append(None)
    return tuple(found)


def _state_key() -> bytes | None:
    secret = os.environ.get("ANYAICAM_APP_SECRETS", "").split(",")[0].strip()
    if not secret:
        return None
    return _hashlib.sha256(b"anyaicam.analytics-state.v1\x00" + secret.encode("utf-8")).digest()


def _state_mac(key: bytes, grant, high_water: str) -> str:
    message = json.dumps({"grant": grant, "high_water": high_water}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _hmac.new(key, message, _hashlib.sha256).hexdigest()


def _read_analytics_state() -> dict | None:
    """{"grant", "high_water"} when present, well-formed and authenticated; otherwise None."""
    key = _state_key()
    if key is None:
        return None
    try:
        data = json.loads(_read_single_name(ANALYTICS_STATE_FILE, _MAX_TRUST_FILE_BYTES))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("grant"), dict) or not isinstance(data.get("high_water"), str):
        return None
    if not _hmac.compare_digest(str(data.get("mac") or ""), _state_mac(key, data["grant"], data["high_water"])):
        logger.warning("appliance_entitlements.analytics_state_rejected reason=mac_mismatch")
        return None
    return {"grant": data["grant"], "high_water": data["high_water"]}


def _write_analytics_state(grant, high_water: datetime) -> bool:
    key = _state_key()
    if key is None:
        return False
    stamp = high_water.astimezone(timezone.utc).isoformat()
    try:
        _write_single_name(ANALYTICS_STATE_FILE,
                           json.dumps({"grant": grant, "high_water": stamp, "mac": _state_mac(key, grant, stamp)}))
        return True
    except OSError as error:
        logger.warning("appliance_entitlements.analytics_persist_failed error=%s", type(error).__name__)
        return False


def clear_analytics() -> None:
    _grant_cache.update({"at": None, "mtime": None, "trust": None, "grants": None, "expires": None})
    try:
        ANALYTICS_STATE_FILE.unlink()
    except OSError:
        pass


def verify_analytics(document, public_keys: dict[str, str], *, identity, now: datetime | None = None) -> str | None:
    """None when the analytics document is valid right now for this appliance, else the reason."""
    if not isinstance(document, dict):
        return "missing"
    if not public_keys:
        return "no_trust_anchor"
    bound = _bound_identity(identity)
    if bound is None:
        return "not_activated"
    body = {key: value for key, value in document.items() if key != "signature"}
    try:
        import appliance_identity
        appliance_identity.verify_signed_body(body, document.get("signature") or {}, public_keys=public_keys)
    except Exception:
        return "bad_signature"
    if body.get("type") != ANALYTICS_TYPE or body.get("version") != ANALYTICS_VERSION:
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
    if expires <= issued or expires - issued > ANALYTICS_MAX_WINDOW + CLOCK_SKEW:
        return "window_too_long"
    if expires <= now:
        return "expired"
    grants = body.get("camera_analytics")
    if not isinstance(grants, dict) or any(
            not isinstance(camera, str) or not camera or not isinstance(keys, list)
            or any(key not in PREMIUM_ANALYTICS for key in keys) for camera, keys in grants.items()):
        return "malformed_grants"
    return None


def store_analytics_grant(document, *, now: datetime | None = None) -> str:
    """Call ONLY with a successful, authenticated configuration response (edge_camera_sync). Returns the outcome."""
    now = now or datetime.now(timezone.utc)
    _grant_cache.update({"at": None, "mtime": None, "trust": None, "grants": None, "expires": None})
    if document is None:
        clear_analytics()
        logger.warning("appliance_entitlements.analytics_cleared reason=absent_from_sync")
        return "cleared:absent"
    reason = _verify_trusted(document, verify_analytics, purpose="analytics", for_new_grant=True,
                             identity=_local_identity(), now=now)
    if reason:
        clear_analytics()
        logger.warning("appliance_entitlements.analytics_cleared reason=%s", reason)
        return f"cleared:rejected:{reason}"
    stored = _read_analytics_state()
    issued = _parse_time(document.get("issued_at"))
    if stored is not None:
        stored_issued = _parse_time(stored["grant"].get("issued_at"))
        if stored_issued is not None and issued < stored_issued:
            clear_analytics()   # Green F1: never keep an older, possibly broader grant
            logger.warning("appliance_entitlements.analytics_cleared reason=older_from_sync")
            return "cleared:older_than_stored"
    if not _write_analytics_state(document, max(now, issued)):
        clear_analytics()
        return "cleared:not_persisted"
    return "stored"


def _grants_from_state(current: datetime) -> tuple[dict, datetime | None]:
    state = _read_analytics_state()
    if state is None:
        return {}, None
    high_water = _parse_time(state["high_water"])
    reason = _verify_trusted(state["grant"], verify_analytics, purpose="analytics", for_new_grant=False,
                             identity=_local_identity(), now=current)
    if reason:
        logger.info("appliance_entitlements.analytics_denied reason=%s", reason)
        return {}, None
    if high_water is None or current < high_water - CLOCK_SKEW:
        logger.warning("appliance_entitlements.analytics_denied reason=clock_rollback")
        return {}, None
    if current - high_water >= HIGH_WATER_PERSIST:
        _write_analytics_state(state["grant"], current)
    grants = {camera_id: frozenset(keys) for camera_id, keys in state["grant"]["camera_analytics"].items()}
    return grants, _parse_time(state["grant"]["expires_at"])


def camera_analytic_grants(now: datetime | None = None) -> dict[str, frozenset]:
    """{camera_id: frozenset(premium keys)} from a valid, unexpired, authenticated grant with a sane clock; else {}.
    Never raises. Re-verified at most every _GRANT_CACHE_SECONDS, and whenever the state or a trust file changes; expiry is
    re-checked on every call."""
    import time
    try:
        current = now or datetime.now(timezone.utc)
        cached = _grant_cache
        try:
            mtime = ANALYTICS_STATE_FILE.stat().st_mtime_ns
        except OSError:
            mtime = None
        trust = _trust_fingerprint()
        if (now is None and cached["grants"] is not None and cached["mtime"] == mtime and cached["trust"] == trust
                and time.monotonic() - cached["at"] < _GRANT_CACHE_SECONDS
                and cached["expires"] is not None and cached["expires"] > current):
            return cached["grants"]
        grants, expires = _grants_from_state(current)
        if now is None:
            try:
                mtime = ANALYTICS_STATE_FILE.stat().st_mtime_ns
            except OSError:
                mtime = None
            _grant_cache.update({"at": time.monotonic(), "mtime": mtime, "trust": trust, "grants": grants, "expires": expires})
        return grants
    except Exception:
        logger.exception("appliance_entitlements.analytics_check_failed")
        return {}


def analytics_grant_held(now: datetime | None = None) -> bool:
    """True when a valid analytics document is currently held (it may grant nothing)."""
    try:
        state = _read_analytics_state()
        if state is None:
            return False
        current = now or datetime.now(timezone.utc)
        high_water = _parse_time(state["high_water"])
        return (_verify_trusted(state["grant"], verify_analytics, purpose="analytics", for_new_grant=False,
                                identity=_local_identity(), now=current) is None
                and high_water is not None and current >= high_water - CLOCK_SKEW)
    except Exception:
        return False


def camera_analytic_allowed(camera_id: str | None, analytic_key: str) -> bool:
    """Whether this appliance holds a valid, unexpired entitlement for one premium analytic on one camera."""
    if not isinstance(camera_id, str) or analytic_key not in PREMIUM_ANALYTICS:
        return False
    return analytic_key in camera_analytic_grants().get(camera_id, frozenset())


def clear() -> None:
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
