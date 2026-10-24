#!/usr/bin/env python3
"""Accept a cloud-signed camera-capacity snapshot into root-owned state.

This is a narrowly scoped privileged helper.  It has no caller-supplied
paths, imports only Python's standard library, performs no network access,
and accepts data only from the fixed VMS staging path after independently
checking the release-provisioned Ed25519 keyset and the appliance identity.
The VMS may write the staging file; it cannot write the accepted store.

The accepted snapshot and its monotonic issue-time/digest high-water mark
are replaced as one fsync'd file. An already accepted capacity remains
usable offline after its snapshot expires; newly received snapshots must be
unexpired. A fresh install keeps the installer UUID only as root-controlled
claim correlation and pins the cloud-assigned identity after signed
acceptance. On first migration only, an expired but otherwise valid signed
snapshot may be accepted to preserve existing capacity; its pre-migration
provenance cannot be proven retroactively.

TRUST-1 + G2 (Linux 1.2.7.1, wt-1271-design/TRUST1-G2-DESIGN.md):
  * Every signer is resolved against the protected trust registry
    (entitlement_trust_registry.json + the monotonic entitlement_trust_state.json,
    installer/Software Update delivered) through the shared resolver below:
    active keys sign NEW grants, retired keys only validate already accepted
    state, revoked or absent keys give nothing. accepted_key is kept only as a
    1.2.7-compatible cross-check; it never confers trust. Accepted state
    (schema 2, "option A") also records signer {key_id, public_key_sha256} and
    trust_generation.
  * Accepted mode: already accepted state is re-validated (signature,
    identity, trust, high-water) WITHOUT the future/expiry check, so a clock
    rollback never invalidates the last-known-good capacity. New grants stay
    strict (issued_at at most five minutes ahead, unexpired, newer than the
    high-water mark), so a far-future grant cannot poison the high-water mark.
  * _state_floor (structural: identity, high-water digest, transition) is the
    anti-replay floor; a newer grant from an ACTIVE signer may replace a
    revoked-signer state, an older one never can.
  * --migrate-trust-state (installer step 16 / the applier, under the lock)
    ratchets the protected state and adds signer/trust_generation to an
    existing 1.2.7 accepted state.
"""
from __future__ import annotations

import base64
import datetime as dt
try:
    import fcntl
except ImportError:  # importable for policy tests on Windows; production is Ubuntu/POSIX.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import subprocess
import sys
import tempfile


STAGING_FILE = "/var/lib/anyaicam/vms/data-config/vms_capacity_signed.json"
IDENTITY_FILE = "/var/lib/anyaicam/vms/recordings/appliance_identity.json"
INSTALLER_IDENTITY_FILE = "/etc/anyaicam/appliance_identity.json"
TRUST_DIR = "/etc/anyaicam-update"
TRUST_FILE = "entitlement_signing_keys.json"
TRUST_REGISTRY_FILE = "entitlement_trust_registry.json"
TRUST_STATE_FILE = "entitlement_trust_state.json"
MAX_REGISTRY_BYTES = 256 * 1024
ACCEPTED_FILE = "vms_capacity_accepted.json"
IDENTITY_ANCHOR_FILE = "capacity_appliance_identity.json"
PENDING_CLAIM_FILE = "capacity_pending_claim.json"
LOCK_FILE = ".vms_capacity_accepted.lock"
COMPAT_DIR = "/var/lib/anyaicam/vms/data-config"
COMPAT_FILE = "vms_capacity.json"
MAX_INPUT_BYTES = 256 * 1024
MAX_IDENTITY_BYTES = 64 * 1024
MAX_TRUST_BYTES = 64 * 1024
MAX_STATE_BYTES = 512 * 1024
MAX_CAMERA_SLOTS = 10_000
BOUND_FIELDS = ("appliance_id", "cloud_id", "customer_id")
SNAPSHOT_TYPE = "anyaicam.feature_entitlements"
SNAPSHOT_VERSION = 1
CLOCK_SKEW = dt.timedelta(minutes=5)
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class Reject(ValueError):
    """A safe refusal; the previous accepted authorization is retained."""


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



def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _body(snapshot) -> dict:
    if not isinstance(snapshot, dict):
        raise Reject("malformed_snapshot")
    return {key: value for key, value in snapshot.items() if key != "signature"}


def _parse_time(value):
    try:
        result = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise Reject("malformed_time")
    if result.tzinfo is None or result.utcoffset() is None:
        raise Reject("malformed_time")
    return result.astimezone(dt.timezone.utc)


def _validate_keyset(document) -> dict[str, bytes]:
    if not isinstance(document, dict) or not isinstance(document.get("keys"), dict) or not document["keys"]:
        raise Reject("invalid_trust_anchor")
    keys = {}
    for key_id, encoded in document["keys"].items():
        if not isinstance(key_id, str) or not key_id or len(key_id) > 128:
            raise Reject("invalid_trust_anchor")
        if not all(char.isalnum() or char in "._-" for char in key_id):
            raise Reject("invalid_trust_anchor")
        if not isinstance(encoded, str):
            raise Reject("invalid_trust_anchor")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise Reject("invalid_trust_anchor")
        if len(raw) != 32:
            raise Reject("invalid_trust_anchor")
        keys[key_id] = raw
    return keys


def _decode_signature(snapshot) -> tuple[str, bytes]:
    signature = snapshot.get("signature") if isinstance(snapshot, dict) else None
    if not isinstance(signature, dict) or signature.get("alg") != "Ed25519":
        raise Reject("bad_signature")
    key_id, encoded = signature.get("key_id"), signature.get("value")
    if not isinstance(key_id, str) or not isinstance(encoded, str) or len(encoded) > 256:
        raise Reject("bad_signature")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error):
        raise Reject("bad_signature")
    if len(raw) != 64:
        raise Reject("bad_signature")
    return key_id, raw


def _openssl_verify(public_key: bytes, message: bytes, signature: bytes, *, openssl="/usr/bin/openssl") -> bool:
    """Verify raw Ed25519 bytes using the Ubuntu system OpenSSL, no imports."""
    pem_body = base64.encodebytes(ED25519_SPKI_PREFIX + public_key).decode("ascii")
    pem = f"-----BEGIN PUBLIC KEY-----\n{pem_body}-----END PUBLIC KEY-----\n".encode("ascii")
    with tempfile.TemporaryDirectory(prefix="anyaicam-capacity-") as temp:
        directory = Path(temp)
        key_path, message_path, signature_path = (directory / name for name in ("key.pem", "body", "signature"))
        for path, data in ((key_path, pem), (message_path, message), (signature_path, signature)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        result = subprocess.run(
            [openssl, "pkeyutl", "-verify", "-pubin", "-inkey", str(key_path), "-rawin",
             "-in", str(message_path), "-sigfile", str(signature_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, check=False, timeout=5,
        )
        return result.returncode == 0


def _safe_identity(identity) -> dict:
    if not isinstance(identity, dict):
        raise Reject("missing_identity")
    bound = {field: identity.get(field) for field in BOUND_FIELDS}
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value)
           for value in bound.values()):
        raise Reject("malformed_identity")
    return bound


def _as_trust(trust) -> TrustContext:
    """A TrustContext, or a flat {key_id: public key} keyset treated as LEGACY trust (generation 0). Duck-typed:
    the VMS copy of the shared resolver (app/appliance_entitlements.py) is accepted too (tests, Windows)."""
    if isinstance(trust, TrustContext) or (callable(getattr(trust, "resolve", None)) and
                                           hasattr(trust, "generation") and hasattr(trust, "mode")):
        return trust
    if isinstance(trust, dict):
        try:
            return TrustContext(legacy_trust_registry(trust), None)
        except TrustError as error:
            raise Reject(str(error))
    raise Reject("invalid_trust_anchor")


def _resolve_signer(trust, key_id, identity: dict, *, for_new_grant: bool, accepted_generation=0) -> bytes:
    """The capacity verification key for key_id under the CURRENT trust, or Reject(<trust reason>)."""
    try:
        return _as_trust(trust).resolve(key_id, purpose="capacity", for_new_grant=for_new_grant,
                                        appliance_id=identity.get("appliance_id"),
                                        accepted_generation=accepted_generation)
    except ValueError as error:  # TrustError (either copy of the shared resolver): a fail-closed refusal
        raise Reject(str(error))


def _validate_snapshot_detail(snapshot, public_keys, identity: dict, *, now=None, check_expiry=True,
                              check_issued_at=True, for_new_grant=True, accepted_generation=0,
                              verifier=_openssl_verify):
    """(body, issued_at, digest, key_id, key). New grants: strict (active signer, issued_at not in the future,
    unexpired). Accepted mode (check_issued_at/check_expiry False, for_new_grant False): retired signers verify,
    and the clock is not consulted at all."""
    body = _body(snapshot)
    if body.get("type") != SNAPSHOT_TYPE or type(body.get("version")) is not int or body["version"] != SNAPSHOT_VERSION:
        raise Reject("unsupported_snapshot")
    bound = _safe_identity(identity)
    for field in BOUND_FIELDS:
        value = body.get(field)
        if not isinstance(value, str) or not value.strip() or value != bound[field]:
            raise Reject("identity_mismatch")
    key_id, signature = _decode_signature(snapshot)
    key = _resolve_signer(public_keys, key_id, bound, for_new_grant=for_new_grant,
                          accepted_generation=accepted_generation)
    if not verifier(key, _canonical(body), signature):
        raise Reject("bad_signature")
    capacity = body.get("camera_capacity")
    if not isinstance(capacity, dict):
        raise Reject("missing_capacity")
    quantity = capacity.get("camera_slot_quantity")
    if type(quantity) is not int or not 0 <= quantity <= MAX_CAMERA_SLOTS:
        raise Reject("malformed_capacity")
    for field in ("plan_camera_slots", "vms_license_capacity"):
        value = capacity.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_CAMERA_SLOTS):
            raise Reject("malformed_capacity")
    issued = _parse_time(body.get("issued_at"))
    expires = _parse_time(body.get("expires_at"))
    if expires <= issued:
        raise Reject("malformed_time")
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if check_issued_at and issued - CLOCK_SKEW > now:
        raise Reject("issued_in_future")
    if check_expiry and expires <= now:
        raise Reject("expired")
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    return body, issued.isoformat(), digest, key_id, key


def _validate_snapshot(snapshot, public_keys, identity: dict, *, now=None,
                       check_expiry=True, check_issued_at=True, verifier=_openssl_verify) -> tuple[dict, str, str]:
    """Strict validation of a NEW snapshot (an ACTIVE signer in the current trust). Returns (body, issued_at, digest)."""
    body, issued_at, digest, _, _ = _validate_snapshot_detail(snapshot, public_keys, identity, now=now,
                                                              check_expiry=check_expiry,
                                                              check_issued_at=check_issued_at,
                                                              for_new_grant=True, verifier=verifier)
    return body, issued_at, digest


def _transition_id(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or not all(
            char.isalnum() or char in "._:-" for char in value):
        raise Reject("invalid_identity_transition")
    return value


def _snapshot_transition(body, identity) -> tuple[str | None, str | None]:
    """Validate the signed cloud transition contract; return (id, source ID).

    Initial claim/activation transitions bridge the installer-generated ID to
    the cloud-assigned appliance ID. Later identity changes require an
    explicit cloud-signed transition from the currently accepted identity.
    """
    transition = body.get("identity_transition")
    if transition is None:
        return None, None
    if not isinstance(transition, dict) or transition.get("schema") != 1:
        raise Reject("invalid_identity_transition")
    transition_id = _transition_id(transition.get("transition_id"))
    target = _safe_identity(transition.get("to_identity"))
    if target != _safe_identity(identity):
        raise Reject("identity_transition_target_mismatch")
    kind = transition.get("kind")
    if kind == "initial_claim":
        source_id = transition.get("from_appliance_id")
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 256:
            raise Reject("invalid_identity_transition")
        return transition_id, source_id
    if kind == "authorized":
        _safe_identity(transition.get("from_identity"))
        if transition.get("reason") not in {"reenrollment", "ownership_transfer"}:
            raise Reject("invalid_identity_transition")
        authorized_at = _parse_time(transition.get("authorized_at"))
        if authorized_at is None:
            raise Reject("invalid_identity_transition")
        return transition_id, transition["from_identity"]["appliance_id"]
    raise Reject("invalid_identity_transition")


def _state_record(snapshot, identity, issued_at, digest, accepted_at, accepted_key, transition_id=None, *,
                  key_id=None, trust_generation=0) -> dict:
    """Schema 2 (1.2.7 readers parse it). accepted_key is a 1.2.7-compatible copy of the signer's key, a cross-check
    only; signer and trust_generation (option A, additive) name the signer the CURRENT registry must resolve."""
    record = {"schema": 2, "identity": identity, "snapshot": snapshot,
              "accepted_key": base64.b64encode(accepted_key).decode("ascii"),
              "transition_id": transition_id,
              "high_water": {"issued_at": issued_at, "digest": digest},
              "accepted_at": accepted_at, "source": "cloud"}
    if key_id is not None:
        record["signer"] = {"key_id": key_id, "public_key_sha256": hashlib.sha256(accepted_key).hexdigest()}
        record["trust_generation"] = trust_generation
    return record


def _state_trust_generation(state) -> int:
    generation = state.get("trust_generation", 0) if isinstance(state, dict) else 0
    if type(generation) is not int or generation < 0:
        raise Reject("invalid_accepted_state")
    return generation


def _state_floor(state) -> tuple[dict, dict]:
    """Structural checks of accepted state only -- identity, signed body vs high-water digest and issue time,
    transition id, signer/trust_generation shape. No signature, trust or clock check: this is the anti-replay and
    identity floor a NEW grant must beat (TRUST1-G2 design 2g)."""
    if not isinstance(state, dict) or state.get("schema") not in {1, 2}:
        raise Reject("invalid_accepted_state")
    identity = _safe_identity(state.get("identity"))
    snapshot = state.get("snapshot")
    body = _body(snapshot)
    if body.get("type") != SNAPSHOT_TYPE or type(body.get("version")) is not int or body["version"] != SNAPSHOT_VERSION:
        raise Reject("unsupported_snapshot")
    for field in BOUND_FIELDS:
        if body.get(field) != identity[field]:
            raise Reject("identity_mismatch")
    key_id, _ = _decode_signature(snapshot)
    issued = _parse_time(body.get("issued_at"))
    if _parse_time(body.get("expires_at")) <= issued:
        raise Reject("malformed_time")
    issued_at, digest = issued.isoformat(), hashlib.sha256(_canonical(body)).hexdigest()
    transition_id, _ = _snapshot_transition(body, identity)
    if state.get("schema") == 2:
        stored_transition_id = state.get("transition_id")
        if stored_transition_id is not None:
            _transition_id(stored_transition_id)
        if transition_id is not None and stored_transition_id != transition_id:
            raise Reject("invalid_identity_transition_state")
    high_water = state.get("high_water")
    if (not isinstance(high_water, dict) or high_water.get("issued_at") != issued_at or
            high_water.get("digest") != digest):
        raise Reject("invalid_high_water")
    signer = state.get("signer")
    if signer is not None and (not isinstance(signer, dict) or set(signer) != {"key_id", "public_key_sha256"} or
                               signer.get("key_id") != key_id or
                               not isinstance(signer.get("public_key_sha256"), str) or
                               len(signer["public_key_sha256"]) != 64):
        raise Reject("binding_mismatch")
    _state_trust_generation(state)
    return identity, {"issued_at": issued_at, "digest": digest}


def _validate_state(state, public_keys, *, now=None, verifier=_openssl_verify) -> tuple[dict, dict]:
    """Accepted mode (G2) under the CURRENT trust (TRUST-1): the floor, then the signature with the key the current
    registry resolves for the snapshot's key_id (retired allowed, revoked/absent/unknown refused, never the stored
    accepted_key), without any future or expiry check. `now` is accepted for API compatibility and ignored."""
    identity, high_water = _state_floor(state)
    _, _, _, _, key = _validate_snapshot_detail(state["snapshot"], public_keys, identity, now=now,
                                                check_expiry=False, check_issued_at=False, for_new_grant=False,
                                                accepted_generation=_state_trust_generation(state),
                                                verifier=verifier)
    if state.get("schema") == 2:
        try:
            accepted_key = base64.b64decode(state.get("accepted_key"), validate=True)
        except (TypeError, ValueError, base64.binascii.Error):
            raise Reject("invalid_accepted_key")
        if accepted_key != key:
            raise Reject("binding_mismatch")
    signer = state.get("signer")
    if signer is not None and signer["public_key_sha256"] != hashlib.sha256(key).hexdigest():
        raise Reject("binding_mismatch")
    return identity, high_water


def _advance(snapshot, public_keys, identity, current_state, *, now=None, verifier=_openssl_verify,
             allow_expired_first=False, anchor=None):
    """Pure policy step, also exercised by tests without filesystem privileges.

    The NEW snapshot is validated strictly (active signer, not future-dated, unexpired). The current state is used
    only through its structural floor (anti-replay, identity), so a newer active-signed grant can replace a state
    whose signer was revoked, while an older or equal one never can. A registry generation recorded by the current
    state is a floor for the trust used to accept the next one."""
    trust = _as_trust(public_keys)
    floor_generation = _state_trust_generation(current_state) if current_state is not None else 0
    body, issued_at, digest, key_id, accepted_key = _validate_snapshot_detail(
        snapshot, trust, identity, now=now, check_expiry=(current_state is not None or not allow_expired_first),
        for_new_grant=True, accepted_generation=floor_generation, verifier=verifier)
    transition_id, transition_source_id = _snapshot_transition(body, identity)
    if current_state is not None:
        current_identity, high_water = _state_floor(current_state)
        if _safe_identity(identity) != current_identity:
            transition = body.get("identity_transition")
            if (not isinstance(transition, dict) or transition.get("kind") != "authorized" or
                    _safe_identity(transition.get("from_identity")) != current_identity or
                    transition_id == current_state.get("transition_id")):
                raise Reject("accepted_identity_changed")
        elif transition_id is not None and current_state.get("transition_id") != transition_id:
            raise Reject("unexpected_identity_transition")
        elif transition_id is None and current_state.get("transition_id") is not None:
            # The cloud may retire a transition from later snapshots after it
            # has been accepted; the accepted root state retains its proof.
            transition_id = current_state.get("transition_id")
        current_time = _parse_time(high_water["issued_at"])
        new_time = _parse_time(issued_at)
        if new_time < current_time:
            raise Reject("replay")
        if new_time == current_time:
            if digest == high_water["digest"]:
                return current_state, "unchanged"
            raise Reject("same_timestamp_conflict")
    else:
        bound = _safe_identity(identity)
        transition = body.get("identity_transition")
        if anchor is not None and transition is not None:
            # A 1.2.4 claim may already have persisted the cloud-assigned ID
            # before 1.2.5 installs the root acceptance helper. In that case
            # the signed initial-claim bridge is already reconciled if its
            # authenticated target is exactly the protected pin. It does not
            # authorize a new identity; it only lets the first capacity
            # snapshot establish its high-water mark.
            already_reconciled = (
                transition.get("kind") == "initial_claim" and
                anchor.get("appliance_id") == bound["appliance_id"]
            )
            if (transition.get("kind") == "initial_claim" and
                    transition_source_id != anchor.get("appliance_id") and not already_reconciled):
                raise Reject("identity_anchor_mismatch")
            if transition.get("kind") == "authorized":
                raise Reject("accepted_identity_changed")
        if anchor is not None and anchor.get("appliance_id") != bound["appliance_id"]:
            if (not isinstance(transition, dict) or transition.get("kind") != "initial_claim" or
                    transition_source_id != anchor.get("appliance_id")):
                raise Reject("identity_anchor_mismatch")
        elif transition_id is not None and body.get("identity_transition", {}).get("kind") != "initial_claim":
            raise Reject("unexpected_identity_transition")
    record = _state_record(snapshot, _safe_identity(identity), issued_at, digest,
                           (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).isoformat(),
                           accepted_key, transition_id, key_id=key_id, trust_generation=trust.generation)
    return record, "accepted"


def _open_dir(path: str, *, root_controlled=False) -> int:
    """Open a fixed absolute directory by descriptor, never following links."""
    if not path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise Reject("unsafe_fixed_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open("/", flags)
    try:
        for part in PurePosixPath(path).parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise Reject("unsafe_directory")
        if root_controlled and (info.st_uid != 0 or info.st_mode & 0o022):
            raise Reject("unsafe_trust_directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_at(dir_fd: int, name: str, max_bytes: int, *, root_owned=False, absent_ok=False) -> bytes | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        if absent_ok:
            return None
        raise Reject("missing_file")
    except OSError:
        raise Reject("unsafe_file")
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
            raise Reject("unsafe_file")
        if root_owned and info.st_uid != 0:
            raise Reject("untrusted_root_file")
        chunks, total = [], 0
        while True:
            chunk = stream.read(min(16 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise Reject("file_too_large")
            chunks.append(chunk)
        return b"".join(chunks)


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Reject("malformed_json")


def _write_atomic_at(dir_fd: int, name: str, data: bytes, *, mode=0o600) -> None:
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temporary, flags, mode, dir_fd=dir_fd)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _identity_anchor(dir_fd: int) -> dict:
    raw = _read_at(dir_fd, IDENTITY_ANCHOR_FILE, 4096, root_owned=True)
    record = _json(raw)
    appliance_id = record.get("appliance_id") if isinstance(record, dict) and record.get("schema") == 1 else None
    if not isinstance(appliance_id, str) or not appliance_id.strip() or len(appliance_id) > 256:
        raise Reject("invalid_identity_anchor")
    return {"appliance_id": appliance_id}


def _write_identity_anchor(dir_fd: int, appliance_id: str) -> None:
    if not isinstance(appliance_id, str) or not appliance_id.strip() or len(appliance_id) > 256:
        raise Reject("invalid_identity_anchor")
    data = json.dumps({"schema": 1, "appliance_id": appliance_id}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8") + b"\n"
    _write_atomic_at(dir_fd, IDENTITY_ANCHOR_FILE, data, mode=0o600)


def _pending_claim_identity(dir_fd: int) -> dict | None:
    """Read the root-controlled claim correlation value, if one was staged.

    This is deliberately not an identity anchor and never authorizes capacity.
    It exists only to ensure a cloud-signed initial-claim transition is for the
    installer identity created during this installation. It is removed after
    the first accepted cloud identity is pinned.
    """
    raw = _read_at(dir_fd, PENDING_CLAIM_FILE, 4096, root_owned=True, absent_ok=True)
    if raw is None:
        return None
    record = _json(raw)
    appliance_id = record.get("provisional_appliance_id") if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get("schema") != 1 or
            record.get("purpose") != "initial_claim_correlation" or
            not isinstance(appliance_id, str) or not appliance_id.strip() or len(appliance_id) > 256):
        raise Reject("invalid_pending_claim")
    return {"appliance_id": appliance_id}


def _write_pending_claim_identity(dir_fd: int, appliance_id: str) -> None:
    if not isinstance(appliance_id, str) or not appliance_id.strip() or len(appliance_id) > 256:
        raise Reject("invalid_pending_claim")
    data = json.dumps({"schema": 1, "purpose": "initial_claim_correlation",
                       "provisional_appliance_id": appliance_id}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8") + b"\n"
    _write_atomic_at(dir_fd, PENDING_CLAIM_FILE, data, mode=0o600)


def _remove_pending_claim_identity(dir_fd: int) -> None:
    try:
        os.unlink(PENDING_CLAIM_FILE, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except FileNotFoundError:
        pass


def _validate_first_acceptance_source(body, identity, *, anchor, pending_claim,
                                      allow_legacy_migration: bool) -> None:
    """Bind first acceptance to either the pending claim or explicit legacy migration.

    A fresh install must present the signed initial_claim bridge from the
    root-protected provisional claim correlation ID. The legacy migration
    exception is available only to the fixed root installer/update entrypoint
    and relies on the pre-1.2.5 signed snapshot plus persisted activation ID.
    A pending claim identifies a clean-install population and disables that
    legacy exception.
    """
    transition = body.get("identity_transition")
    _, transition_source = _snapshot_transition(body, identity)
    if pending_claim is not None and transition is None:
        raise Reject("initial_claim_transition_required")
    if anchor is not None:
        if transition is not None and transition.get("kind") == "initial_claim":
            already_reconciled = anchor.get("appliance_id") == _safe_identity(identity)["appliance_id"]
            if transition_source != anchor.get("appliance_id") and not already_reconciled:
                raise Reject("identity_anchor_mismatch")
        return
    if transition is not None:
        if transition.get("kind") != "initial_claim":
            raise Reject("unexpected_identity_transition")
        if pending_claim is None:
            raise Reject("missing_pending_claim")
        if transition.get("from_appliance_id") != pending_claim.get("appliance_id"):
            raise Reject("pending_claim_identity_mismatch")
        return
    if not allow_legacy_migration:
        raise Reject("initial_claim_transition_required")
    # A migrated 1.2.4 authorization may predate the signed transition field.
    # Its historical provenance cannot be retroactively established; it is
    # still signature- and persisted-identity-validated before becoming root state.
    _safe_identity(identity)


def _transition_source_appliance_id(state) -> str | None:
    if not isinstance(state, dict) or not isinstance(state.get("snapshot"), dict):
        return None
    body = _body(state["snapshot"])
    transition = body.get("identity_transition")
    if not isinstance(transition, dict):
        return None
    if transition.get("kind") == "initial_claim":
        return transition.get("from_appliance_id")
    if transition.get("kind") == "authorized" and isinstance(transition.get("from_identity"), dict):
        return transition["from_identity"].get("appliance_id")
    return None


def _check_identity_anchor(identity, anchor) -> None:
    if not isinstance(identity, dict) or not isinstance(anchor, dict):
        raise Reject("identity_anchor_mismatch")
    if identity.get("appliance_id") != anchor.get("appliance_id"):
        raise Reject("identity_anchor_mismatch")


def _load_trust(dir_fd: int, *, trust_file=TRUST_FILE, registry_file=TRUST_REGISTRY_FILE,
                state_file=TRUST_STATE_FILE) -> TrustContext:
    """The installed trust, read without following links from root-owned, not group/other-writable files:
    the registry + protected state when a registry is installed; otherwise LEGACY (the flat keyset at generation 0,
    refused by the resolver whenever the protected state or the accepted grant records a generation >= 1)."""
    state = None
    state_raw = _read_at(dir_fd, state_file, MAX_TRUST_BYTES, root_owned=True, absent_ok=True)
    try:
        if state_raw is not None:
            state = parse_trust_state(_json(state_raw))
        registry_raw = _read_at(dir_fd, registry_file, MAX_REGISTRY_BYTES, root_owned=True, absent_ok=True)
        if registry_raw is not None:
            registry = parse_trust_registry(_json(registry_raw))
            return TrustContext(registry, state, registry_sha256=hashlib.sha256(registry_raw).hexdigest())
        flat_raw = _read_at(dir_fd, trust_file, MAX_TRUST_BYTES, root_owned=True, absent_ok=True)
        if flat_raw is None:
            return TrustContext(None, state)
        return TrustContext(legacy_trust_registry(_validate_keyset(_json(flat_raw))), state)
    except TrustError as error:
        raise Reject(str(error))


def _ratchet_trust_state(dir_fd: int, trust: TrustContext, *, state_file=TRUST_STATE_FILE) -> str:
    """Under the acceptance lock: advance the protected state to the installed registry (monotonic; refuses a
    rollback). Registry first, then state is the installer's write order; this repairs an interrupted second write."""
    if trust.mode != "registry":
        return "legacy"
    try:
        state = next_trust_state(trust.registry, trust.registry_sha256, trust.state)
    except TrustError as error:
        raise Reject(str(error))
    if trust.state == state:
        return "unchanged"
    _write_atomic_at(dir_fd, state_file, canonical_trust_state(state), mode=0o644)
    trust.state = parse_trust_state(state)
    return "ratcheted"


def _bootstrap_identity(identity_dir_fd: int, trust_dir_fd: int, *, identity_name="appliance_identity.json",
                        pin_existing_identity=False) -> str:
    """Stage claim correlation or recover an already accepted cloud identity.

    Never promote the installer's temporary UUID to the authoritative anchor.
    """
    identity = _json(_read_at(identity_dir_fd, identity_name, MAX_IDENTITY_BYTES))
    appliance_id = identity.get("appliance_id") if isinstance(identity, dict) else None
    if not isinstance(appliance_id, str) or not appliance_id.strip() or len(appliance_id) > 256:
        raise Reject("invalid_identity")
    lock_fd = _acquire_lock(trust_dir_fd)
    try:
        try:
            existing = _identity_anchor(trust_dir_fd)
        except Reject as error:
            if str(error) != "missing_file":
                raise
            existing = None
        accepted_raw = _read_at(trust_dir_fd, ACCEPTED_FILE, MAX_STATE_BYTES,
                                root_owned=True, absent_ok=True)
        if accepted_raw is not None:
            trust = _load_trust(trust_dir_fd)
            accepted_state = _json(accepted_raw)
            try:
                # Accepted mode (G2): a clock behind the accepted grant is
                # not a reason to refuse it. TRUST-1: never pin from a
                # signer the current registry does not trust.
                accepted_identity, _ = _validate_state(accepted_state, trust)
            except Reject as error:
                if str(error) not in TRUST_FAILURE_REASONS:
                    raise
                print(f"capacity identity bootstrap: accepted state not trusted ({error}); nothing pinned",
                      file=sys.stderr)
                return "awaiting_signed_transition"
            accepted_id = accepted_identity["appliance_id"]
            try:
                local_identity = _safe_identity(identity)
            except Reject:
                local_identity = None
            if existing is None:
                _write_identity_anchor(trust_dir_fd, accepted_id)
            elif existing["appliance_id"] != accepted_id:
                if _transition_source_appliance_id(accepted_state) != existing["appliance_id"]:
                    raise Reject("identity_anchor_mismatch")
                _write_identity_anchor(trust_dir_fd, accepted_id)
            if local_identity != accepted_identity:
                # A local activation change is not itself authorization. Keep
                # the last signed anchor and wait for a cloud-signed transition.
                return "awaiting_signed_transition"
            _remove_pending_claim_identity(trust_dir_fd)
            return "reconciled_accepted_identity" if existing is None or existing["appliance_id"] != accepted_id else "unchanged"
        if existing is not None:
            if existing["appliance_id"] != appliance_id:
                # A local identity changes only after claim/re-enrollment.
                # Never rewrite the root pin from this service-writable file;
                # the acceptance watcher must first verify a signed transition.
                return "awaiting_signed_transition"
            return "unchanged"
        pending = _pending_claim_identity(trust_dir_fd)
        if pending is not None:
            if pending["appliance_id"] != appliance_id:
                # A later mutable/local identity cannot replace the original
                # claim source. The cloud-signed first snapshot must bridge it.
                return "awaiting_signed_transition"
            return "awaiting_signed_transition"
        if pin_existing_identity:
            # An upgrade from 1.2.4 has a cloud-assigned activation identity
            # but no capacity acceptance state. Pin that existing identity
            # under root so a transition-less legacy snapshot can establish
            # its first high-water mark after upgrade. This mode is enabled
            # only by the installer migration entrypoint. Fresh installs keep
            # using pending-claim correlation and require initial_claim.
            _safe_identity(identity)
            _write_identity_anchor(trust_dir_fd, appliance_id)
            return "bootstrapped"
        _write_pending_claim_identity(trust_dir_fd, appliance_id)
        return "awaiting_signed_transition"
    finally:
        os.close(lock_fd)


def _acquire_lock(dir_fd: int):
    if fcntl is None:
        raise Reject("unsupported_platform")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(LOCK_FILE, flags, 0o600, dir_fd=dir_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise Reject("unsafe_lock")
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _save_compatibility_copy(snapshot: dict, *, compat_dir=COMPAT_DIR, compat_file=COMPAT_FILE) -> None:
    """Keep the old file current for an intentional 1.2.4 rollback only."""
    data = snapshot["camera_capacity"]
    compatibility = {key: data.get(key) for key in
                     ("camera_slot_quantity", "plan_camera_slots", "vms_license_capacity")}
    directory_fd = _open_dir(compat_dir)
    try:
        _write_atomic_at(directory_fd, compat_file,
                         (json.dumps(compatibility, sort_keys=True) + "\n").encode("utf-8"), mode=0o644)
    finally:
        os.close(directory_fd)


def accept_pending(*, staging_path=STAGING_FILE, identity_path=IDENTITY_FILE,
                   trust_dir=TRUST_DIR, trust_file=TRUST_FILE, accepted_file=ACCEPTED_FILE,
                   compat_dir=COMPAT_DIR, compat_file=COMPAT_FILE,
                   openssl="/usr/bin/openssl", now=None, verifier=None,
                   allow_expired_first=False, registry_file=TRUST_REGISTRY_FILE,
                   trust_state_file=TRUST_STATE_FILE) -> str:
    """Accept one pending snapshot. Production calls this with fixed defaults.

    Injectable paths/verifier exist only for isolated tests. No CLI option can
    select them, and `main()` rejects every argument.
    """
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise Reject("unsupported_platform")
    if os.geteuid() != 0:
        raise Reject("must_run_as_root")
    if (staging_path != STAGING_FILE or identity_path != IDENTITY_FILE or trust_dir != TRUST_DIR or
            trust_file != TRUST_FILE or accepted_file != ACCEPTED_FILE or compat_dir != COMPAT_DIR or
            compat_file != COMPAT_FILE or registry_file != TRUST_REGISTRY_FILE or
            trust_state_file != TRUST_STATE_FILE) and verifier is None:
        raise Reject("test_path_override_denied")
    staging = Path(staging_path)
    staging_dir_fd = _open_dir(str(staging.parent))
    raw = _read_at(staging_dir_fd, staging.name, MAX_INPUT_BYTES, absent_ok=True)
    if raw is None:
        os.close(staging_dir_fd)
        return "no_pending_snapshot"
    root_dir_fd = _open_dir(trust_dir, root_controlled=True)
    identity_dir_fd = None
    lock_fd = None
    try:
        trust_info = os.fstat(root_dir_fd)
        if trust_info.st_uid != 0 or trust_info.st_mode & 0o022:
            raise Reject("unsafe_trust_directory")
        identity_file = Path(identity_path)
        identity_dir_fd = _open_dir(str(identity_file.parent))
        snapshot = _json(raw)
        identity = _json(_read_at(identity_dir_fd, identity_file.name, MAX_IDENTITY_BYTES))
        current_raw = _read_at(root_dir_fd, accepted_file, MAX_STATE_BYTES, root_owned=True, absent_ok=True)
        current_state = _json(current_raw) if current_raw is not None else None
        if current_state is not None:
            # Structural corruption only (G2/TRUST-1): a clock rollback or a
            # revoked signer must not block a newer, valid, active-signed grant.
            _state_floor(current_state)
        lock_fd = _acquire_lock(root_dir_fd)
        # The trust (registry + protected state) is read and ratcheted under
        # the lock; a rollback or a tampered registry refuses here.
        trust = _load_trust(root_dir_fd, trust_file=trust_file, registry_file=registry_file,
                            state_file=trust_state_file)
        _ratchet_trust_state(root_dir_fd, trust, state_file=trust_state_file)
        # Re-read after obtaining the lock so two concurrent root invocations
        # cannot validate against the same old high-water mark.
        current_raw = _read_at(root_dir_fd, accepted_file, MAX_STATE_BYTES, root_owned=True, absent_ok=True)
        current_state = _json(current_raw) if current_raw is not None else None
        if current_state is not None:
            _state_floor(current_state)
        try:
            pinned_identity = _identity_anchor(root_dir_fd)
        except Reject as error:
            if str(error) != "missing_file":
                raise
            pinned_identity = None
        pending_claim = _pending_claim_identity(root_dir_fd)
        # The expired/no-transition exception exists for genuine legacy
        # 1.2.4 upgrades. A pending claim is the root-protected marker for a
        # clean install awaiting its first authorized initial_claim.
        allow_legacy_first = allow_expired_first and pending_claim is None
        if current_state is not None and pinned_identity is None:
            current_identity, _ = _validate_state(current_state, trust, now=now,
                                                  verifier=verifier or (lambda key, message, sig: _openssl_verify(key, message, sig, openssl=openssl)))
            _write_identity_anchor(root_dir_fd, current_identity["appliance_id"])
            _remove_pending_claim_identity(root_dir_fd)
            pinned_identity = _identity_anchor(root_dir_fd)
        if current_state is not None and current_state.get("identity", {}).get("appliance_id") != pinned_identity["appliance_id"]:
            _validate_state(current_state, trust, now=now, verifier=verifier or
                            (lambda key, message, sig: _openssl_verify(key, message, sig, openssl=openssl)))
            if _transition_source_appliance_id(current_state) != pinned_identity["appliance_id"]:
                raise Reject("identity_anchor_mismatch")
            _write_identity_anchor(root_dir_fd, current_state["identity"]["appliance_id"])
            pinned_identity = _identity_anchor(root_dir_fd)
        policy_verifier = verifier or (lambda key, message, sig: _openssl_verify(key, message, sig, openssl=openssl))
        if current_state is None:
            first_body, _, _ = _validate_snapshot(snapshot, trust, identity, now=now,
                                                  check_expiry=not allow_legacy_first,
                                                  verifier=policy_verifier)
            _validate_first_acceptance_source(first_body, identity, anchor=pinned_identity,
                                              pending_claim=pending_claim,
                                              allow_legacy_migration=allow_legacy_first)
        next_state, outcome = _advance(snapshot, trust, identity, current_state,
                                       now=now, verifier=policy_verifier,
                                       allow_expired_first=allow_legacy_first,
                                       anchor=pinned_identity)
        if outcome == "unchanged":
            return outcome
        encoded = (json.dumps(next_state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_STATE_BYTES:
            raise Reject("accepted_state_too_large")
        _write_atomic_at(root_dir_fd, accepted_file, encoded)
        if pinned_identity is None or pinned_identity["appliance_id"] != next_state["identity"]["appliance_id"]:
            # The accepted signed state is the transaction's source of truth.
            # If this secondary pin write is interrupted, a later invocation
            # can repair it only from the root-validated transition above.
            _write_identity_anchor(root_dir_fd, next_state["identity"]["appliance_id"])
        _remove_pending_claim_identity(root_dir_fd)
        try:
            _save_compatibility_copy(snapshot, compat_dir=compat_dir, compat_file=compat_file)
        except (OSError, Reject):
            # Compatibility data is not enforcement state. The protected,
            # atomically accepted authorization remains authoritative.
            pass
        return outcome
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if staging_dir_fd is not None:
            os.close(staging_dir_fd)
        if identity_dir_fd is not None:
            os.close(identity_dir_fd)
        os.close(root_dir_fd)


def migrate_trust_state(*, trust_dir=TRUST_DIR, accepted_file=ACCEPTED_FILE, verifier=None) -> tuple[str, str]:
    """--migrate-trust-state (installer step 16 and the root applier, after the registry is installed): under the
    acceptance lock, ratchet the protected trust state to the installed registry and add signer/trust_generation
    (option A) to an existing 1.2.7 accepted state that the CURRENT registry trusts. Returns (outcome, detail):
    'unchanged' (also: legacy mode, no accepted state), 'migrated', or 'accepted_state_untrusted' with the reason --
    an untrusted or corrupt accepted state is never rewritten (the VMS fails closed on it with that diagnostic and a
    newer active-signed grant replaces it). A registry rollback or an unsafe file raises Reject."""
    dir_fd = _open_dir(trust_dir, root_controlled=True)
    lock_fd = None
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise Reject("unsafe_trust_directory")
        lock_fd = _acquire_lock(dir_fd)
        trust = _load_trust(dir_fd)
        if _ratchet_trust_state(dir_fd, trust) == "legacy":
            return "unchanged", ""
        raw = _read_at(dir_fd, accepted_file, MAX_STATE_BYTES, root_owned=True, absent_ok=True)
        if raw is None:
            return "unchanged", ""
        try:
            state = _json(raw)
            _validate_state(state, trust, verifier=verifier or _openssl_verify)
            key_id, _ = _decode_signature(state["snapshot"])
            key = _resolve_signer(trust, key_id, _safe_identity(state["identity"]), for_new_grant=False,
                                  accepted_generation=_state_trust_generation(state))
        except Reject as error:
            return "accepted_state_untrusted", str(error)
        updated = dict(state, signer={"key_id": key_id, "public_key_sha256": hashlib.sha256(key).hexdigest()},
                       trust_generation=max(_state_trust_generation(state), trust.generation))
        if updated == state:
            return "unchanged", ""
        encoded = (json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_STATE_BYTES:
            raise Reject("accepted_state_too_large")
        _write_atomic_at(dir_fd, accepted_file, encoded)
        return "migrated", ""
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(dir_fd)


def main() -> int:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("capacity acceptance helper must run as root", file=sys.stderr)
        return 1
    if (len(sys.argv) == 2 and sys.argv[1] == "--bootstrap-identity" or
            len(sys.argv) == 3 and sys.argv[1:] == ["--bootstrap-identity", "--legacy-upgrade"]):
        legacy_upgrade = len(sys.argv) == 3
        trust_dir = None
        try:
            trust_dir = _open_dir(TRUST_DIR, root_controlled=True)
            info = os.fstat(trust_dir)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise Reject("unsafe_trust_directory")
            missing = None
            accepted_raw = _read_at(trust_dir, ACCEPTED_FILE, MAX_STATE_BYTES,
                                    root_owned=True, absent_ok=True)
            # With accepted state, compare against the mutable VMS activation
            # identity. Before first acceptance, stage the installer UUID as
            # a provisional claim-correlation value only.
            identity_paths = ((IDENTITY_FILE, INSTALLER_IDENTITY_FILE) if accepted_raw is not None
                              else (INSTALLER_IDENTITY_FILE, IDENTITY_FILE))
            if legacy_upgrade and accepted_raw is None:
                installer_dir = activation_dir = None
                try:
                    installer_dir = _open_dir(str(Path(INSTALLER_IDENTITY_FILE).parent))
                    activation_dir = _open_dir(str(Path(IDENTITY_FILE).parent))
                    installer_record = _json(_read_at(installer_dir, Path(INSTALLER_IDENTITY_FILE).name,
                                                      MAX_IDENTITY_BYTES))
                    activation_record = _json(_read_at(activation_dir, Path(IDENTITY_FILE).name,
                                                       MAX_IDENTITY_BYTES))
                    installer_id = installer_record.get("appliance_id") if isinstance(installer_record, dict) else None
                    activation = _safe_identity(activation_record)
                    if (isinstance(installer_id, str) and installer_id.strip() and
                            installer_id != activation["appliance_id"]):
                        result = _bootstrap_identity(activation_dir, trust_dir,
                                                     identity_name=Path(IDENTITY_FILE).name,
                                                     pin_existing_identity=True)
                        print(result)
                        return 0
                except FileNotFoundError:
                    pass
                except Reject as error:
                    if str(error) != "missing_file":
                        raise
                finally:
                    if activation_dir is not None:
                        os.close(activation_dir)
                    if installer_dir is not None:
                        os.close(installer_dir)
            for identity_path in identity_paths:
                identity_dir = None
                try:
                    identity_dir = _open_dir(str(Path(identity_path).parent))
                    try:
                        result = _bootstrap_identity(identity_dir, trust_dir,
                                                     identity_name=Path(identity_path).name)
                    except Reject as error:
                        if str(error) == "missing_file":
                            missing = error
                            continue
                        raise
                    print(result)
                    return 0
                except FileNotFoundError as error:
                    missing = error
                    continue
                finally:
                    if identity_dir is not None:
                        os.close(identity_dir)
            raise Reject("missing_identity") from missing
        except (Reject, OSError) as error:
            print(f"capacity identity bootstrap rejected: {error}", file=sys.stderr)
            return 1
        finally:
            if trust_dir is not None:
                os.close(trust_dir)
    if len(sys.argv) == 2 and sys.argv[1] == "--migrate-trust-state":
        try:
            outcome, detail = migrate_trust_state(trust_dir=TRUST_DIR, accepted_file=ACCEPTED_FILE)
        except (Reject, OSError) as error:
            print(f"capacity trust-state migration refused: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        print(outcome)
        if detail:
            print(f"capacity trust-state migration: the accepted capacity is not trusted ({detail}); it is kept "
                  "unchanged and the VMS refuses it until a newer grant from an active signer is accepted.",
                  file=sys.stderr)
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--accept-migrated-snapshot":
        try:
            print(accept_pending(staging_path=STAGING_FILE, identity_path=IDENTITY_FILE,
                                 trust_dir=TRUST_DIR, trust_file=TRUST_FILE,
                                 accepted_file=ACCEPTED_FILE, compat_dir=COMPAT_DIR,
                                 compat_file=COMPAT_FILE, allow_expired_first=True))
            return 0
        except (Reject, OSError, subprocess.SubprocessError, ValueError) as error:
            print(f"capacity migration snapshot not accepted: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
    if len(sys.argv) != 1:
        print("capacity acceptance helper takes no arguments", file=sys.stderr)
        return 2
    try:
        print(accept_pending())
        return 0
    except (Reject, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"capacity snapshot not accepted: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
