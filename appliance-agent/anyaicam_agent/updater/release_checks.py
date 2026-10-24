"""Software Update (2026-10-03): checks shared by the agent (before it stages
an owner-requested release), the root applier (which re-runs every one of
them itself before touching /opt/anyaicam) and the offline release publisher.

Pure and dependency-light: stdlib only, so the root applier can import it
from the root-owned agent install without pulling anything else in.

A release package is the existing signed installer tarball
(installer/build_release_installer.py). Its manifest (verify.py) is signed
offline; this module never sees a private key.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import re
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Union

PathLike = Union[str, Path]

_VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}$")
_BUILD_ID = re.compile(r"^[0-9a-f]{40}$")
_UPDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Free space a release needs beyond the package itself: the staged
# application tree, a second image layer set, and the rollback copy.
MIN_FREE_BYTES_BEYOND_PACKAGE = 4 * 1024 ** 3
PACKAGE_SPACE_MULTIPLIER = 3

# Archive limits: the real installer is a few thousand files, well under
# 2 GiB unpacked. Anything far larger is refused rather than unpacked.
MAX_ARCHIVE_MEMBERS = 50_000
MAX_UNPACKED_BYTES = 8 * 1024 ** 3

# What an installer release package must contain (paths relative to its
# single top-level directory).
REQUIRED_RELEASE_FILES = (
    "install.sh",
    "validate.sh",
    "rollback.sh",
    "release.env",
    "artifact-files.json",
    "runtime/anyaicam-vms.service",
    "payload/vms/Dockerfile",
    "payload/vms/docker-compose.yml",
    "payload/vms/requirements.txt",
    "payload/vms/app/main.py",
    # Served by the VMS from the bind-mounted tree: proves after activation
    # that the swapped application is the one answering requests.
    "payload/vms/app/static/release-identity.json",
)


# Paid-feature entitlement trust anchor (2026-10-05): the cloud's
# entitlement-signing PUBLIC keyset a release may carry, and where the root
# applier installs it (the path app/appliance_entitlements.py reads).
ENTITLEMENT_KEYSET_PATH = "payload/keys/entitlement-signing-public-keys.json"
ENTITLEMENT_KEYSET_ENV = "ENTITLEMENT_SIGNING_KEYS_SHA256"
_ENTITLEMENT_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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



class ReleaseCheckError(Exception):
    """A release failed a check. `code` is a short stable reason the UI and
    the update ledger can show; the message is for logs."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ identity

def parse_release_version(version: str) -> tuple:
    """'1.2.0' -> (1, 2, 0). Dotted integers only (2 to 4 parts)."""
    if not isinstance(version, str) or not _VERSION.match(version):
        raise ReleaseCheckError("bad_version", f"release version {version!r} is not a dotted product version like 1.2.0")
    return tuple(int(part) for part in version.split("."))


def is_newer(candidate: str, current: str) -> bool:
    """True iff candidate is strictly newer. An unknown/blank current version
    (an appliance from before release versions existed) accepts any valid
    candidate; an unparseable current version refuses (never guesses)."""
    candidate_tuple = parse_release_version(candidate)
    if not current:
        return True
    try:
        current_tuple = parse_release_version(current)
    except ReleaseCheckError:
        return False
    width = max(len(candidate_tuple), len(current_tuple))
    pad = lambda value: value + (0,) * (width - len(value))  # noqa: E731
    return pad(candidate_tuple) > pad(current_tuple)


def validate_build_id(build_id: str) -> str:
    if not isinstance(build_id, str) or not _BUILD_ID.match(build_id):
        raise ReleaseCheckError("bad_build_id", "build_id must be the full 40-character lowercase Git commit")
    return build_id


def validate_update_id(update_id: str) -> str:
    if not isinstance(update_id, str) or not _UPDATE_ID.match(update_id):
        raise ReleaseCheckError("bad_update_id", f"update_id {update_id!r} is not a safe identifier")
    return update_id


def release_label(version: str, build_id: str) -> str:
    """What the appliance reports in the cloud's single software_version
    field: '1.2.0+3f2a9c1b0d4e'. The full build_id stays on the device."""
    return f"{version}+{build_id[:12]}" if version and build_id else (version or "")


def split_release_label(label: str) -> tuple[str, str]:
    """'1.2.0+3f2a9c1b0d4e' -> ('1.2.0', '3f2a9c1b0d4e'); anything else ->
    ('', '') so a legacy label is never mistaken for a release version."""
    if not isinstance(label, str) or "+" not in label:
        return "", ""
    version, _, build = label.partition("+")
    try:
        parse_release_version(version)
    except ReleaseCheckError:
        return "", ""
    return version, build if re.fullmatch(r"[0-9a-f]{7,40}", build) else ""


# ------------------------------------------------------------------ device

def device_platform(os_release: PathLike = "/etc/os-release") -> str:
    """The os-release ID ('ubuntu'), lower-case; '' if unknown."""
    try:
        for line in Path(os_release).read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                return line[3:].strip().strip('"').lower()
    except OSError:
        pass
    return ""


def device_architecture() -> str:
    machine = _platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def check_manifest_for_device(manifest, *, target: str, platform: str, architecture: str, current_version: str) -> None:
    """Target, platform, architecture, version format and downgrade/replay.
    `manifest` is an already signature-verified Manifest."""
    if manifest.target != target:
        raise ReleaseCheckError("wrong_target", f"release is for {manifest.target!r}, this device is {target!r}")
    if not manifest.platform or manifest.platform != platform:
        raise ReleaseCheckError("wrong_platform", f"release is for platform {manifest.platform!r}, this device is {platform!r}")
    if not manifest.architecture or manifest.architecture != architecture:
        raise ReleaseCheckError("wrong_architecture",
                                f"release is for {manifest.architecture!r}, this device is {architecture!r}")
    validate_update_id(manifest.update_id)
    validate_build_id(manifest.build_id)
    if not is_newer(manifest.version, current_version):
        raise ReleaseCheckError("not_newer", f"release {manifest.version} is not newer than the installed {current_version or 'release'}")
    if manifest.migration_safety != "additive":
        raise ReleaseCheckError("unsafe_migrations",
                                "the release is not marked as having only additive database changes")


def required_free_bytes(package_size_bytes: int) -> int:
    return max(0, int(package_size_bytes)) * PACKAGE_SPACE_MULTIPLIER + MIN_FREE_BYTES_BEYOND_PACKAGE


def check_free_space(path: PathLike, required_bytes: int, *, disk_usage=shutil.disk_usage) -> None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = disk_usage(str(probe)).free
    if free < required_bytes:
        raise ReleaseCheckError(
            "insufficient_disk",
            f"{probe} has {free // 1024 ** 2} MiB free; this update needs {required_bytes // 1024 ** 2} MiB",
        )


# ------------------------------------------------------------------ package

def sha256_file(path: PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_problem(member: tarfile.TarInfo) -> Optional[str]:
    name = member.name
    if not name or "\x00" in name or "\\" in name:
        return "invalid name"
    path = PurePosixPath(name)
    if path.is_absolute() or name.startswith("/"):
        return "absolute path"
    if any(part == ".." for part in path.parts):
        return "path traversal"
    if member.issym() or member.islnk():
        return "link entry"
    if not (member.isfile() or member.isdir()):
        return "special file"
    return None


def validate_archive_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Every member must be a regular file or directory with a relative
    path that stays inside the destination: no '..', no absolute paths, no
    symlinks or hard links (which could point outside), no devices/FIFOs.
    Checked for the whole archive before anything is written."""
    members = archive.getmembers()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ReleaseCheckError("unsafe_archive", f"archive has {len(members)} entries (limit {MAX_ARCHIVE_MEMBERS})")
    total = 0
    for member in members:
        problem = _member_problem(member)
        if problem:
            raise ReleaseCheckError("unsafe_archive", f"archive entry {member.name!r} rejected: {problem}")
        total += max(0, member.size)
        if total > MAX_UNPACKED_BYTES:
            raise ReleaseCheckError("unsafe_archive", "archive unpacks to more than the allowed size")
    return members


def safe_extract(package_path: PathLike, destination: PathLike) -> Path:
    """Validates every member, then extracts into `destination` (which must
    not exist yet) with tarfile's 'data' filter as a second layer. Returns
    the release root, which is `destination` itself: the installer tarball
    (build_release_installer.write_deterministic_tar) stores its files at
    the archive root."""
    destination = Path(destination)
    if destination.exists():
        raise ReleaseCheckError("unsafe_archive", f"extraction destination {destination} already exists")
    try:
        with tarfile.open(package_path, mode="r:*") as archive:
            validate_archive_members(archive)
            destination.mkdir(parents=True)
            archive.extractall(destination, filter="data")
    except tarfile.TarError as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise ReleaseCheckError("unsafe_archive", f"package is not a readable archive: {error}") from error
    except ReleaseCheckError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    resolved = destination.resolve()
    for path in destination.rglob("*"):
        if path.is_symlink() or not str(path.resolve()).startswith(str(resolved)):
            shutil.rmtree(destination, ignore_errors=True)
            raise ReleaseCheckError("unsafe_archive", f"extracted path escapes the staging directory: {path}")
    return destination


def read_release_env(path: PathLike) -> dict:
    """KEY=value lines only; parsed, never sourced or executed."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key] = value.strip().strip('"')
    return values


def verify_release_tree(release_root: PathLike, manifest) -> dict:
    """The extracted package is the release the signed manifest names: the
    required files exist, release.env carries the same version and build,
    and every file listed in artifact-files.json has its recorded SHA-256."""
    root = Path(release_root)
    for relative in REQUIRED_RELEASE_FILES:
        if not (root / relative).is_file():
            raise ReleaseCheckError("bad_package", f"release package is missing {relative}")
    env = read_release_env(root / "release.env")
    if env.get("RELEASE_VERSION") != manifest.version:
        raise ReleaseCheckError("bad_package", "release.env RELEASE_VERSION does not match the signed manifest")
    if env.get("VMS_RELEASE_COMMIT") != manifest.build_id:
        raise ReleaseCheckError("bad_package", "release.env VMS_RELEASE_COMMIT does not match the signed build_id")
    try:
        entries = json.loads((root / "artifact-files.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReleaseCheckError("bad_package", f"artifact-files.json is unreadable: {error}") from error
    if not isinstance(entries, list) or not entries:
        raise ReleaseCheckError("bad_package", "artifact-files.json lists no files")
    listed = set()
    for entry in entries:
        relative = str(entry.get("path", "")) if isinstance(entry, dict) else ""
        parts = PurePosixPath(relative).parts
        if not relative or relative.startswith("/") or ".." in parts:
            raise ReleaseCheckError("bad_package", f"artifact-files.json lists an unsafe path {relative!r}")
        candidate = root / PurePosixPath(relative)
        if not candidate.is_file():
            raise ReleaseCheckError("bad_package", f"listed file {relative} is missing")
        if sha256_file(candidate) != entry.get("sha256"):
            raise ReleaseCheckError("bad_package", f"listed file {relative} does not match its recorded hash")
        listed.add(relative)
    for relative in REQUIRED_RELEASE_FILES:
        if relative != "artifact-files.json" and relative not in listed:
            raise ReleaseCheckError("bad_package", f"{relative} is not covered by artifact-files.json")
    return env


# ------------------------------------------------------------------ migrations

# Statements a previous application version may not survive: they remove or
# reshape something it still reads or writes. Launch updates allow only
# additive changes (new tables, new nullable/defaulted columns, new indexes).
_DESTRUCTIVE = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|VIEW|TRIGGER)\b"
    r"|\bALTER\s+TABLE\s+[\w\"'.]+\s+(RENAME|DROP|ALTER)\b"
    r"|\bALTER\s+COLUMN\b"
    r"|\bRENAME\s+(TO|COLUMN)\b"
    r"|\bTRUNCATE\b",
    re.IGNORECASE,
)
_NOT_NULL_WITHOUT_DEFAULT = re.compile(r"\bADD\s+COLUMN\s+\w+\s+\w+[^'\";]*?\bNOT\s+NULL\b(?![^'\";]*\bDEFAULT\b)", re.IGNORECASE)
_MIGRATION_SOURCES = ("db_migrations.py", "partner_db.py", "database_backend.py")


def _statements(app_dir: Path) -> list[str]:
    found = []
    for name in _MIGRATION_SOURCES:
        path = app_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (_DESTRUCTIVE, _NOT_NULL_WITHOUT_DEFAULT):
            for match in pattern.finditer(text):
                start = text.rfind("\n", 0, match.start()) + 1
                end = text.find("\n", match.end())
                found.append(f"{name}: {' '.join(text[start:end if end != -1 else None].split())}")
    return found


def find_unsafe_migrations(current_app_dir: PathLike, new_app_dir: PathLike) -> list[str]:
    """Destructive or old-version-incompatible schema statements that the new
    release introduces relative to the running one. Statements already
    present in the running release (historic migrations) are not new."""
    current = _statements(Path(current_app_dir))
    remaining = list(current)
    introduced = []
    for statement in _statements(Path(new_app_dir)):
        if statement in remaining:
            remaining.remove(statement)
        else:
            introduced.append(statement)
    return introduced


# ------------------------------------------------------------------ application tree

# Inside the live application tree (/opt/anyaicam), what is not release code.
CARRIED_OVER = ("mediamtx", "app/auto.key", "app/auto.crt")
LEGACY_DATA = ("recordings", "data", ".env")


def preservation_problems(current_tree: PathLike, release_payload: PathLike) -> list[str]:
    """Top-level entries of the live tree that are neither release code nor
    carried over: refusing the update is safer than guessing what they are.
    Legacy data directories must have been migrated out by the installer."""
    current = Path(current_tree)
    payload = Path(release_payload)
    if not current.is_dir():
        return []
    shipped = {entry.name for entry in payload.iterdir()}
    problems = []
    for entry in sorted(current.iterdir(), key=lambda item: item.name):
        name = entry.name
        if name in shipped or name in CARRIED_OVER or name == "__pycache__":
            continue
        if name in LEGACY_DATA:
            if entry.is_file() or any(entry.iterdir()):
                problems.append(f"{name} (legacy data still inside the application tree; run the installer repair first)")
            continue
        problems.append(f"{name} (unknown entry in the application tree)")
    return problems


def ensure_regular_file(path: PathLike) -> Path:
    """Refuses symlinks and non-regular files (staged inputs live in a
    directory the unprivileged agent can write)."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError as error:
        raise ReleaseCheckError("bad_staging", f"{path.name} is missing: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseCheckError("bad_staging", f"{path.name} is not a regular file")
    return path


def iter_files(root: PathLike) -> Iterable[Path]:
    return (path for path in Path(root).rglob("*") if path.is_file())


# ------------------------------------------------------------------ entitlement trust anchor

def canonical_entitlement_keyset(keys: dict) -> bytes:
    """The one canonical form of a keyset: {"keys": {...}} sorted, indented 2,
    LF-terminated. Its SHA-256 is the keyset digest operators verify, the
    builder records in release.env and the applier checks. Must stay
    byte-identical to installer/build_release_installer.py and
    app/entitlement_keyset.py (tests compare them)."""
    return (json.dumps({"keys": dict(sorted(keys.items()))}, sort_keys=True, indent=2) + "\n").encode("ascii")


def validate_entitlement_keyset(data: bytes) -> dict:
    """{key_id: base64 Ed25519 public key} from a packaged keyset, or
    ReleaseCheckError. Public keys only, in canonical form: anything that
    looks like a key container or private material, an unexpected field, a
    malformed id, or a value that is not 32 raw bytes is refused."""
    import base64
    upper = data.upper()
    if b"PRIVATE" in upper or b"BEGIN" in upper or b"OPENSSH" in upper:
        raise ReleaseCheckError("bad_keyset", "the entitlement keyset contains key-container or private-key material")
    try:
        document = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ReleaseCheckError("bad_keyset", f"the entitlement keyset is not JSON: {error}") from error
    if not isinstance(document, dict) or set(document) != {"keys"} or not isinstance(document["keys"], dict) or not document["keys"]:
        raise ReleaseCheckError("bad_keyset", 'the entitlement keyset must be exactly {"keys": {key_id: public key}} with a key')
    for key_id, value in document["keys"].items():
        if not _ENTITLEMENT_KEY_ID.match(key_id):
            raise ReleaseCheckError("bad_keyset", f"the entitlement keyset has a malformed key id {key_id!r}")
        try:
            raw = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
        except ValueError:
            raw = b""
        if len(raw) != 32:
            raise ReleaseCheckError("bad_keyset", f"entitlement key {key_id!r} is not a 32-byte Ed25519 public key")
    if canonical_entitlement_keyset(document["keys"]) != data:
        raise ReleaseCheckError("bad_keyset", "the entitlement keyset is not in canonical form")
    return dict(document["keys"])


def release_entitlement_keyset(release_root: PathLike, env: dict) -> Optional[bytes]:
    """The verified keyset bytes a release carries, None when it carries none.
    release.env (covered by the signed package hash) must name the keyset's
    SHA-256 exactly when the keyset is present."""
    path = Path(release_root) / ENTITLEMENT_KEYSET_PATH
    expected = str(env.get(ENTITLEMENT_KEYSET_ENV) or "")
    if not expected:
        if path.exists() or path.is_symlink():
            raise ReleaseCheckError("bad_keyset", "the release carries an entitlement keyset that release.env does not name")
        return None
    if not _SHA256.match(expected):
        raise ReleaseCheckError("bad_keyset", f"release.env {ENTITLEMENT_KEYSET_ENV} is not a lowercase SHA-256")
    if path.is_symlink() or not path.is_file():
        raise ReleaseCheckError("bad_keyset", "release.env names an entitlement keyset the release does not contain")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ReleaseCheckError("bad_keyset", "the entitlement keyset does not match release.env")
    validate_entitlement_keyset(data)
    return data


# ------------------------------------------------------------------ entitlement trust registry (TRUST-1, 1.2.7.1)
# The protected registry a release may carry (payload/keys/entitlement-trust-registry.json), named by SHA-256 in
# release.env -- so the release-signing key vouches for it through the signed manifest and the package hash. An
# entitlement key never signs it. The flat keyset the same release carries must be its projection (active +
# retired keys; revoked excluded) for 1.2.7 readers and rollback.
ENTITLEMENT_REGISTRY_PATH = "payload/keys/entitlement-trust-registry.json"
ENTITLEMENT_REGISTRY_ENV = "ENTITLEMENT_TRUST_REGISTRY_SHA256"
# Phase 1 (owner decision 9): no lab/test signer in any shipped registry before independent TRUST-1 verification.
_LAB_KEY_ID = re.compile(r"^(lab|test)[._-]", re.IGNORECASE)


def validate_entitlement_trust_registry(data: bytes) -> dict:
    """The normalized registry, or ReleaseCheckError('bad_trust_registry'): canonical form, public keys only, the
    shared resolver's structural rules, every key in the registry's own environment, no lab/test key id in a
    production registry, and at least one ACTIVE capacity key."""
    upper = data.upper()
    if b"PRIVATE" in upper or b"BEGIN" in upper or b"OPENSSH" in upper:
        raise ReleaseCheckError("bad_trust_registry", "the entitlement trust registry contains key-container or private-key material")
    try:
        document = json.loads(data.decode("ascii"))
        registry = parse_trust_registry(document)
    except (UnicodeDecodeError, ValueError) as error:
        raise ReleaseCheckError("bad_trust_registry", f"the entitlement trust registry is malformed: {error}") from error
    if canonical_trust_registry(document) != data:
        raise ReleaseCheckError("bad_trust_registry", "the entitlement trust registry is not in canonical form")
    for key_id, entry in registry["keys"].items():
        if entry["environment"] != registry["environment"]:
            raise ReleaseCheckError("bad_trust_registry", f"key {key_id!r} belongs to environment "
                                    f"{entry['environment']!r}, not {registry['environment']!r} (no lab keys ship)")
        if registry["environment"] == "production" and _LAB_KEY_ID.match(key_id):
            raise ReleaseCheckError("bad_trust_registry", f"key id {key_id!r} is a lab/test key id in a production registry")
    if not any(entry["state"] == "active" and "capacity" in entry["purposes"] for entry in registry["keys"].values()):
        raise ReleaseCheckError("bad_trust_registry", "the entitlement trust registry has no active capacity key")
    return registry


def entitlement_keyset_projection(data: bytes) -> bytes:
    """The canonical flat keyset 1.2.7 readers use: every active and retired registry key (revoked excluded)."""
    import base64
    registry = validate_entitlement_trust_registry(data)
    keys = {key_id: base64.b64encode(entry["public_key"]).decode("ascii")
            for key_id, entry in registry["keys"].items() if entry["state"] != "revoked"}
    return canonical_entitlement_keyset(keys)


def release_entitlement_trust_registry(release_root: PathLike, env: dict) -> Optional[bytes]:
    """The verified registry bytes a release carries, None when it carries none. release.env must name its SHA-256
    exactly when it is present, and the release must also carry its flat keyset projection."""
    path = Path(release_root) / ENTITLEMENT_REGISTRY_PATH
    expected = str(env.get(ENTITLEMENT_REGISTRY_ENV) or "")
    if not expected:
        if path.exists() or path.is_symlink():
            raise ReleaseCheckError("bad_trust_registry", "the release carries an entitlement trust registry that release.env does not name")
        return None
    if not _SHA256.match(expected):
        raise ReleaseCheckError("bad_trust_registry", f"release.env {ENTITLEMENT_REGISTRY_ENV} is not a lowercase SHA-256")
    if path.is_symlink() or not path.is_file():
        raise ReleaseCheckError("bad_trust_registry", "release.env names an entitlement trust registry the release does not contain")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ReleaseCheckError("bad_trust_registry", "the entitlement trust registry does not match release.env")
    projection = entitlement_keyset_projection(data)
    keyset = release_entitlement_keyset(release_root, env)
    if keyset != projection:
        raise ReleaseCheckError("bad_trust_registry", "the release's flat entitlement keyset is not the registry's "
                                "projection (active + retired keys)")
    return data
