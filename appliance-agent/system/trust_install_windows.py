#!/usr/bin/env python3
"""LIC-W1: installs the entitlement trust registry on Windows. Run by Setup only, as an administrator.

The Windows counterpart of apply_release.install_trust_registry() plus capacity_accept.migrate_trust_state(). Those
need POSIX ownership checks and descriptors, and apply_release.py does not ship on Windows, so the flow is ported and
the shared policy (capacity_accept: next_trust_state, check_trust_registry_transition, _validate_state; release_checks:
validate_entitlement_trust_registry, entitlement_keyset_projection) is called, never copied.

Only the release chain delivers a registry: Authenticode-signed setup, then the SHA-256 pinned at build, then the
registry. No entitlement key signs it, and nothing from cloud sync reaches this file. A PC that never runs a new setup
never gets a new registry: see docs/entitlement-signing-keys.md for what that leaves exposed (LIC-W9).

  trust_install_windows.py <registry.json> <sha256> <flat-keyset.json>   verify, then write (install-runtime.ps1)
  trust_install_windows.py --check <registry.json> <sha256>              decide only, write nothing (PrepareToInstall)

--check runs from Setup's temporary folder on the OLD runtime, before the services stop, so a refusal leaves the PC
working. It needs no third-party package and skips the ACL walk, which install-runtime.ps1 repairs first. Its result
only ever adds a refusal: the second, full run always decides again.

A first install has no earlier registry or state, so next_trust_state and check_trust_registry_transition cannot see a
dropped key: an accepted grant from a key the new registry omits would become unknown_key, 0 cameras. So the signer of
the accepted grant is checked first. Active or retired continues. An explicit tombstone with the same key material
continues, and cameras stop by design until a newer grant. Absent, or other key material, is refused with every trust
file unchanged. An explicit tombstone is an intended revocation; an absence is an accident. Only the capacity grant is
checked here: feature and analytics grants expire and are re-verified on every read, so a signer they lose fails closed
until the next sync. A trust file with a second hard link is refused, by both runs, before any file is read.

Exit: 0 done, 3 refused (the reason on stderr, every file unchanged by this step), 2 bad arguments, 1 anything else.
No path comes from a command line for the store: every store path is platform_paths' own (capacity_accept_windows).
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

import capacity_accept_windows as base

Reject = base.Reject
REFUSED_EXIT = 3
MAX_KEYSET_BYTES = base.MAX_TRUST_BYTES
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _policy():
    system_dir = str(Path(__file__).resolve().parent)
    if system_dir not in sys.path:
        sys.path.insert(0, system_dir)
    import capacity_accept
    return capacity_accept


def _release_checks():
    """The appliance's stdlib-only release checks, loaded by file so the agent package's own imports do not run."""
    source = Path(__file__).resolve().parents[1] / "anyaicam_agent" / "updater" / "release_checks.py"
    spec = importlib.util.spec_from_file_location("anyaicam_setup_release_checks", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refuse_hard_links(paths) -> None:
    """Linux refuses these too (apply_release). The ACL walk skips this in --check, so the check is made here."""
    for name in ("registry", "trust_state", "accepted", "trust"):
        try:
            links = os.lstat(paths[name]).st_nlink
        except FileNotFoundError:
            continue
        except OSError as error:
            raise Reject("unreadable_file") from error
        if links != 1:
            raise Reject("hard_linked_trust_file")


def _read(path: Path, limit: int):
    """The file's bytes, None when absent."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Reject("unreadable_file") from error
    if len(raw) > limit:
        raise Reject("file_too_large")
    return raw


def _json_object(raw):
    """The JSON object in raw, None for anything else."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _known_fingerprints(policy, accepted: dict, key_id: str, flat_raw) -> set:
    """What this PC already records as the signer's key material: the grant's own fields and the installed flat keyset."""
    known = set()
    signer = accepted.get("signer")
    if isinstance(signer, dict) and isinstance(signer.get("public_key_sha256"), str):
        known.add(signer["public_key_sha256"])
    try:
        known.add(hashlib.sha256(base64.b64decode(accepted.get("accepted_key"), validate=True)).hexdigest())
    except (TypeError, ValueError):
        pass
    flat = _json_object(flat_raw)
    if flat is not None:
        try:
            raw = policy._validate_keyset(flat).get(key_id)
        except policy.Reject:
            raw = None
        if raw is not None:
            known.add(hashlib.sha256(raw).hexdigest())
    return known


def _signer_problem(policy, accepted, registry: dict, prior, flat_raw, verifier) -> str:
    """Why this registry must not be installed over the accepted grant, '' when it may."""
    if accepted is None:
        return ""
    try:
        key_id, signature = policy._decode_signature(accepted.get("snapshot"))
    except policy.Reject:
        return ""  # no readable signer to compare: never rewritten below, and the VMS reports it
    entry = registry["keys"].get(key_id)
    if entry is None:
        return "" if prior is not None and key_id in prior["revoked"] else "signer_absent"
    known = _known_fingerprints(policy, accepted, key_id, flat_raw)
    if any(fingerprint != entry["fingerprint"] for fingerprint in known):
        return "signer_key_mismatch"
    if not known and verifier is not None:
        body = policy._canonical(policy._body(accepted["snapshot"]))
        if not verifier(entry["public_key"], body, signature):
            return "signer_key_mismatch"
    if entry["state"] == "revoked":
        return ""
    identity = accepted.get("identity")
    try:
        policy.TrustContext(registry).resolve(key_id, purpose="capacity", for_new_grant=False, appliance_id=(
            identity.get("appliance_id") if isinstance(identity, dict) else None))
    except policy.TrustError as error:
        return f"signer_{error}"
    return ""


def _decide(policy, paths, data: bytes, digest: str, new_registry: dict, verifier) -> tuple:
    """(outcome, next state) from what is on disk, or Reject. Writes nothing. Stricter than the Linux applier in one
    case: with the old registry unreadable or deleted, a bound key the new registry drops is refused, not tombstoned."""
    _refuse_hard_links(paths)
    state_raw = _read(paths["trust_state"], base.MAX_TRUST_BYTES)
    state = None
    if state_raw is not None:
        try:
            state = policy.parse_trust_state(json.loads(state_raw.decode("ascii")))
        except (UnicodeError, ValueError) as error:
            raise Reject("invalid_trust_state") from error
    registry_raw = _read(paths["registry"], base.MAX_REGISTRY_BYTES)
    if state is not None and state["generation"] == new_registry["generation"] and state["registry_sha256"] == digest:
        return ("unchanged" if registry_raw == data else "restored"), state
    old_registry = None
    if registry_raw is not None:
        try:
            old_registry = policy.parse_trust_registry(json.loads(registry_raw.decode("ascii")))
        except (UnicodeError, ValueError):
            old_registry = None  # an unreadable registry is replaced; the protected state still guards it
    accepted = _json_object(_read(paths["accepted"], base.MAX_STATE_BYTES))
    if accepted is not None:
        try:
            floor = policy._state_trust_generation(accepted)
        except policy.Reject:
            floor = 0
        if new_registry["generation"] < floor:
            raise Reject("registry_rollback")
    if state is not None:
        for key_id in state["key_bindings"]:
            if key_id not in new_registry["keys"] and key_id not in state["revoked"]:
                raise Reject("dropped_binding")
    problem = _signer_problem(policy, accepted, new_registry, state, _read(paths["trust"], MAX_KEYSET_BYTES), verifier)
    if problem:
        raise Reject(problem)
    try:
        policy.check_trust_registry_transition(old_registry, new_registry)
        return "installed", policy.next_trust_state(new_registry, digest, state)
    except policy.TrustError as error:
        raise Reject(str(error)) from error


def _migrate_accepted(policy, paths, new_registry: dict, digest: str, state: dict, verifier) -> str:
    """migrate_trust_state's second half: add signer and trust_generation to an accepted grant the installed registry
    trusts. The generation is what makes deleting both registry and state registry_rollback instead of LEGACY. An
    untrusted or corrupt grant is never rewritten: the VMS fails closed on it and a newer active-signed grant replaces it."""
    raw = _read(paths["accepted"], base.MAX_STATE_BYTES)
    if raw is None:
        return "no_accepted_state"
    accepted = _json_object(raw)
    if accepted is None:
        return "accepted_state_untrusted"
    trust = policy.TrustContext(new_registry, state, registry_sha256=digest)
    try:
        policy._validate_state(accepted, trust, verifier=verifier)
        key_id, _ = policy._decode_signature(accepted["snapshot"])
        key = policy._resolve_signer(trust, key_id, policy._safe_identity(accepted["identity"]), for_new_grant=False,
                                     accepted_generation=policy._state_trust_generation(accepted))
    except ValueError:  # policy.Reject and TrustError alike
        return "accepted_state_untrusted"
    updated = dict(accepted, signer={"key_id": key_id, "public_key_sha256": hashlib.sha256(key).hexdigest()},
                   trust_generation=max(policy._state_trust_generation(accepted), trust.generation))
    if updated == accepted:
        return "unchanged"
    encoded = (json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > base.MAX_STATE_BYTES:
        raise Reject("accepted_state_too_large")
    base._atomic_replace(paths["accepted"], encoded)
    return "migrated"


def _parse_incoming(policy, data: bytes, registry_sha256: str) -> tuple:
    if len(data) > base.MAX_REGISTRY_BYTES:
        raise Reject("file_too_large")
    digest = hashlib.sha256(data).hexdigest()
    if digest != registry_sha256:
        raise Reject("registry_hash_mismatch")
    try:
        return digest, policy.parse_trust_registry(json.loads(data.decode("ascii")))
    except (UnicodeError, ValueError) as error:
        raise Reject("invalid_registry") from error


def _check_from_paths(paths, data: bytes, registry_sha256: str) -> str:
    """The early decision: no ACL walk, no lock, no signature library, no writes."""
    policy = _policy()
    digest, new_registry = _parse_incoming(policy, data, registry_sha256)
    return _decide(policy, paths, data, digest, new_registry, None)[0]


def _install_from_paths(paths, data: bytes, registry_sha256: str, keyset: bytes, *, verifier=None,
                        acl_problem=None) -> tuple:
    """(registry outcome, accepted-grant outcome). Injectable policy seam for tests; never exposed by the command line.
    Order, as Linux: registry, then state, then the accepted grant; every step converges when a rerun repeats it."""
    policy = _policy()
    digest, _ = _parse_incoming(policy, data, registry_sha256)
    checks = _release_checks()
    try:
        new_registry = checks.validate_entitlement_trust_registry(data)
        projection = checks.entitlement_keyset_projection(data)
    except checks.ReleaseCheckError as error:
        raise Reject("invalid_registry") from error
    if projection != keyset:
        raise Reject("keyset_not_projection")
    acl_problem = acl_problem or base._windows_acl_problem
    root = paths["root"]
    if acl_problem(paths["accepted"].parent, root):
        raise Reject("unsafe_store_acl")
    for name in ("registry", "trust_state", "accepted", "trust"):
        if paths[name].exists() and acl_problem(paths[name], root):
            raise Reject("unsafe_store_acl")
    verify = verifier or base._verify_ed25519
    with base._windows_lock(paths["accepted"].parent / ".vms_capacity_accepted.lock"):
        outcome, state = _decide(policy, paths, data, digest, new_registry, verify)
        if outcome != "unchanged":
            base._atomic_replace(paths["registry"], data)
        if outcome == "installed":
            base._atomic_replace(paths["trust_state"], policy.canonical_trust_state(state))
        return outcome, _migrate_accepted(policy, paths, new_registry, digest, state, verify)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = argv[:1] == ["--check"]
    if os.name != "nt" or len(argv) != 3 or (not check_only and argv[0].startswith("--")):
        return 2
    registry_sha256 = argv[2 if check_only else 1]
    if not _SHA256.match(registry_sha256):
        return 2
    try:
        data = _read(Path(argv[1 if check_only else 0]), base.MAX_REGISTRY_BYTES)
        if data is None:
            raise Reject("missing_registry")
        if check_only:
            print(_check_from_paths(base._paths(), data, registry_sha256))
            return 0
        keyset = _read(Path(argv[2]), MAX_KEYSET_BYTES)
        if keyset is None:
            raise Reject("missing_keyset")
        print(" ".join(_install_from_paths(base._paths(), data, registry_sha256, keyset)))
        return 0
    except Reject as error:
        print(f"entitlement trust registry refused: {error}", file=sys.stderr)
        return REFUSED_EXIT
    except Exception as error:
        # Never print key or filesystem detail.
        print(f"entitlement trust registry not installed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
