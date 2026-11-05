#!/usr/bin/env python3
"""Windows counterpart to capacity_accept.py (Windows step 4: signed capacity acceptance).

Ported from Codex 07e934a onto Red's Windows trust layout (claude/red-127-windows-trust-paths):
- every path is fixed by platform_paths (the %ProgramData% known folder, never the environment):
  staging   %ProgramData%\\AnyAiCam\\config\\vms_capacity_signed.json   (written by the VMS cloud sync)
  identity  %ProgramData%\\AnyAiCam\\recordings\\appliance_identity.json
  keyset    %ProgramData%\\AnyAiCam\\update\\entitlement_signing_keys.json (installed by install-runtime.ps1)
  accepted  %ProgramData%\\AnyAiCam\\update\\vms_capacity_accepted.json
  registry  %ProgramData%\\AnyAiCam\\update\\entitlement_trust_registry.json (TRUST-1; installed by the Windows
            runtime installer once Blue's packaging ships it -- until then LEGACY mode: the flat keyset at generation 0)
  state     %ProgramData%\\AnyAiCam\\update\\entitlement_trust_state.json   (monotonic; ratcheted here under the lock)
- the ACL rule is platform_paths.windows_write_problem() -- the same check appliance_entitlements and
  vms_capacity already use -- applied to each input/store file and every directory up to %ProgramData%\\AnyAiCam;
- the signed-format, identity-transition, expiry, replay and high-water policy is the Linux helper's own
  (capacity_accept._validate_keyset / _state_floor / _advance and the shared trust resolver, TrustContext); filesystem
  work is never delegated to it. TRUST-1 + G2 parity (1.2.7.1): the signer is resolved against the current registry
  (active / retired / revoked, registry rollback refused), the current accepted state is checked structurally only
  (a clock rollback never blocks it), new grants stay strict.

No command-line or environment path overrides. The injectable seams are for isolated policy tests only.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

MAX_INPUT_BYTES = 256 * 1024
MAX_TRUST_BYTES = 64 * 1024
MAX_STATE_BYTES = 512 * 1024
MAX_REGISTRY_BYTES = 256 * 1024


class Reject(ValueError):
    """Safe refusal; previously accepted authorization remains authoritative."""


def _app_dir() -> Path:
    # <install>\agent\system\capacity_accept_windows.py -> <install>\app (platform_paths lives there)
    return Path(__file__).resolve().parents[2] / "app"


def _platform_paths():
    app_dir = str(_app_dir())
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import platform_paths
    return platform_paths


def _paths() -> dict:
    if os.name != "nt":
        raise Reject("unsupported_platform")
    platform_paths = _platform_paths()
    root = platform_paths.UPDATE_DIR.parent
    return {
        "root": root,
        "staging": platform_paths.CONFIG_DIR / "vms_capacity_signed.json",
        "identity": root / "recordings" / "appliance_identity.json",
        "trust": platform_paths.UPDATE_DIR / "entitlement_signing_keys.json",
        "accepted": platform_paths.UPDATE_DIR / "vms_capacity_accepted.json",
        "registry": platform_paths.UPDATE_DIR / "entitlement_trust_registry.json",
        "trust_state": platform_paths.UPDATE_DIR / "entitlement_trust_state.json",
    }


def _windows_acl_problem(path: Path, root: Path, *, include_self: bool = True) -> str:
    """'' when `path` (if include_self) and every directory from its parent up to `root` pass
    platform_paths.windows_write_problem(); otherwise why not. Fails closed on any error."""
    try:
        platform_paths = _platform_paths()
        path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
        if path != root and root not in path.parents:
            return f"{path} is outside {root}"
        chain = [path] if include_self else []
        current = path
        while current != root:
            current = current.parent
            chain.append(current)
        for entry in chain:
            problem = platform_paths.windows_write_problem(entry)
            if problem:
                return f"{entry} {problem}"
        return ""
    except Exception as error:  # unreadable ACL, missing entry, API failure: never trust it
        return f"{path} could not be checked ({type(error).__name__})"


def _refuse_second_name(stream) -> None:
    """Checked on the open handle: a path check before opening could be raced."""
    if os.fstat(stream.fileno()).st_nlink != 1:
        raise Reject("hard_linked_trust_file")


def _json_file(path: Path, limit: int) -> dict:
    try:
        with path.open("rb") as stream:
            _refuse_second_name(stream)
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise Reject("file_too_large")
        value = json.loads(raw.decode("utf-8"))
    except Reject:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise Reject("unreadable_file") from error
    if not isinstance(value, dict):
        raise Reject("malformed_file")
    return value


def _verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        return True
    except Exception:
        return False


def _atomic_replace(path: Path, payload: bytes) -> None:
    """Write a unique same-directory file, then atomically replace the accepted state."""
    temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _windows_lock(path: Path):
    """Single-byte Windows file lock serialising acceptances; a no-op off Windows (tests)."""
    if os.name != "nt":
        return nullcontext()
    import msvcrt

    class Lock:
        def __enter__(self):
            self.stream = path.open("a+b")
            try:
                _refuse_second_name(self.stream)
            except Reject:
                self.stream.close()
                raise
            self.stream.seek(0)  # A byte past the end can be locked; reading a held one raises instead of waiting.
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
            return self

        def __exit__(self, *_exc):
            try:
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.stream.close()

    return Lock()


def accept_pending() -> str:
    """Verify and atomically promote the staged snapshot, using only the compiled-in platform paths."""
    return _accept_from_paths(_paths())


def _accept_from_paths(paths, *, keyset=None, registry=None, trust_state=None, verifier=None, acl_problem=None,
                       now=None) -> str:
    """Injectable policy seam for tests; never exposed through a command line or the environment. keyset= (flat,
    LEGACY), registry= and trust_state= (documents) replace the corresponding files when given."""
    if not paths["staging"].exists():
        return "no_pending_snapshot"
    acl_problem = acl_problem or _windows_acl_problem
    root = paths["root"]
    registry_path, state_path = paths.get("registry"), paths.get("trust_state")
    registry_installed = registry is not None or (registry_path is not None and registry_path.exists())
    inputs = ["staging", "identity"]
    if not registry_installed and keyset is None:
        inputs.append("trust")
    for name in inputs:
        if acl_problem(paths[name], root):
            raise Reject("unsafe_input_acl")
    for path in (registry_path, state_path):
        if path is not None and path.exists() and acl_problem(path, root):
            raise Reject("unsafe_input_acl")
    if acl_problem(paths["accepted"].parent, root):
        raise Reject("unsafe_store_acl")
    if paths["accepted"].exists() and acl_problem(paths["accepted"], root):
        raise Reject("unsafe_store_acl")

    snapshot = _json_file(paths["staging"], MAX_INPUT_BYTES)
    identity = _json_file(paths["identity"], MAX_INPUT_BYTES)

    system_dir = str(Path(__file__).resolve().parent)
    if system_dir not in sys.path:
        sys.path.insert(0, system_dir)
    import capacity_accept as policy

    verify = verifier or _verify_ed25519
    with _windows_lock(paths["accepted"].parent / ".vms_capacity_accepted.lock"):
        try:
            trust = _trust_context(policy, paths, keyset=keyset, registry=registry, trust_state=trust_state)
            if trust.mode == "registry":  # ratchet the protected state under the lock (never backwards)
                next_state = policy.next_trust_state(trust.registry, trust.registry_sha256, trust.state)
                if next_state != trust.state:
                    _atomic_replace(state_path, policy.canonical_trust_state(next_state))
                    trust.state = policy.parse_trust_state(next_state)
        except policy.TrustError as error:
            raise Reject(str(error)) from error
        except policy.Reject as error:
            raise Reject(str(error)) from error
        current = None
        if paths["accepted"].exists():  # read under the lock: monotonic updates are serialised
            current = _json_file(paths["accepted"], MAX_STATE_BYTES)
            try:
                policy._state_floor(current)  # structural only: a clock rollback or revoked signer never blocks
            except policy.Reject as error:
                raise Reject(str(error)) from error
        try:
            next_state, outcome = policy._advance(snapshot, trust, identity, current, now=now, verifier=verify)
        except policy.Reject as error:
            raise Reject(str(error)) from error
        if outcome == "unchanged":
            return outcome
        encoded = (json.dumps(next_state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_STATE_BYTES:
            raise Reject("accepted_state_too_large")
        _atomic_replace(paths["accepted"], encoded)
        return outcome


def _trust_context(policy, paths, *, keyset=None, registry=None, trust_state=None):
    """The installed trust (TRUST-1): registry + protected state when a registry is installed, else LEGACY (the flat
    keyset at generation 0; refused by the resolver once the state or the accepted grant records generation >= 1)."""
    state_path, registry_path = paths.get("trust_state"), paths.get("registry")
    if trust_state is None and state_path is not None and state_path.exists():
        trust_state = _json_file(state_path, MAX_TRUST_BYTES)
    if registry is None and registry_path is not None and registry_path.exists():
        with registry_path.open("rb") as stream:
            _refuse_second_name(stream)
            data = stream.read(MAX_REGISTRY_BYTES + 1)
        if len(data) > MAX_REGISTRY_BYTES:
            raise Reject("file_too_large")
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise Reject("invalid_registry") from error
        return policy.TrustContext(document, trust_state, registry_sha256=hashlib.sha256(data).hexdigest())
    if registry is not None:
        return policy.TrustContext(registry, trust_state,
                                   registry_sha256=hashlib.sha256(policy.canonical_trust_registry(registry)).hexdigest())
    key_doc = keyset
    if key_doc is None and paths["trust"].exists():
        key_doc = _json_file(paths["trust"], MAX_TRUST_BYTES)
    if key_doc is None:
        return policy.TrustContext(None, trust_state)
    return policy.TrustContext(policy.legacy_trust_registry(policy._validate_keyset(key_doc)), trust_state)


def main() -> int:
    if os.name != "nt" or sys.argv[1:]:
        return 2
    try:
        print(accept_pending())
        return 0
    except Exception as error:
        # Never print snapshot, key, identity or filesystem detail.
        print(f"capacity snapshot not accepted: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
