#!/usr/bin/env python3
"""Accept a cloud-signed camera-capacity snapshot into root-owned state.

This is a narrowly scoped privileged helper.  It has no caller-supplied
paths, imports only Python's standard library, performs no network access,
and accepts data only from the fixed VMS staging path after independently
checking the release-provisioned Ed25519 keyset and the appliance identity.
The VMS may write the staging file; it cannot write the accepted store.

The accepted snapshot and its monotonic issue-time/digest high-water mark
are replaced as one fsync'd file.  An already accepted capacity remains
usable offline after its snapshot expires; newly received snapshots must be
unexpired.  On first migration only, an expired but otherwise valid signed
snapshot may be bootstrapped to preserve the capacity already in use.  This
cannot prove that a snapshot was not replayed before the protected store was
introduced; see docs/signed-camera-capacity-acceptance.md.
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
ACCEPTED_FILE = "vms_capacity_accepted.json"
IDENTITY_ANCHOR_FILE = "capacity_appliance_identity.json"
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


def _validate_snapshot(snapshot, public_keys: dict[str, bytes], identity: dict, *, now=None,
                       check_expiry=True, verifier=_openssl_verify) -> tuple[dict, str, str]:
    body = _body(snapshot)
    if body.get("type") != SNAPSHOT_TYPE or type(body.get("version")) is not int or body["version"] != SNAPSHOT_VERSION:
        raise Reject("unsupported_snapshot")
    bound = _safe_identity(identity)
    for field in BOUND_FIELDS:
        value = body.get(field)
        if not isinstance(value, str) or not value.strip() or value != bound[field]:
            raise Reject("identity_mismatch")
    key_id, signature = _decode_signature(snapshot)
    key = public_keys.get(key_id)
    if key is None or not verifier(key, _canonical(body), signature):
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
    if issued - CLOCK_SKEW > now:
        raise Reject("issued_in_future")
    if check_expiry and expires <= now:
        raise Reject("expired")
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    return body, issued.isoformat(), digest


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


def _state_record(snapshot, identity, issued_at, digest, accepted_at, accepted_key, transition_id=None) -> dict:
    return {"schema": 2, "identity": identity, "snapshot": snapshot,
            "accepted_key": base64.b64encode(accepted_key).decode("ascii"),
            "transition_id": transition_id,
            "high_water": {"issued_at": issued_at, "digest": digest},
            "accepted_at": accepted_at, "source": "cloud"}


def _validate_state(state, public_keys, *, now=None, verifier=_openssl_verify) -> tuple[dict, dict]:
    if not isinstance(state, dict) or state.get("schema") not in {1, 2}:
        raise Reject("invalid_accepted_state")
    identity = _safe_identity(state.get("identity"))
    state_keys = public_keys
    if state.get("schema") == 2:
        snapshot_key_id, _ = _decode_signature(state.get("snapshot"))
        encoded_key = state.get("accepted_key")
        try:
            accepted_key = base64.b64decode(encoded_key, validate=True)
        except (TypeError, ValueError, base64.binascii.Error):
            raise Reject("invalid_accepted_key")
        if len(accepted_key) != 32:
            raise Reject("invalid_accepted_key")
        state_keys = {snapshot_key_id: accepted_key}
    body, issued_at, digest = _validate_snapshot(state.get("snapshot"), state_keys, identity,
                                                 now=now, check_expiry=False, verifier=verifier)
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
    return identity, {"issued_at": issued_at, "digest": digest}


def _advance(snapshot, public_keys, identity, current_state, *, now=None, verifier=_openssl_verify,
             allow_expired_first=False, anchor=None):
    """Pure policy step, also exercised by tests without filesystem privileges."""
    body, issued_at, digest = _validate_snapshot(snapshot, public_keys, identity, now=now,
                                                 check_expiry=(current_state is not None or not allow_expired_first),
                                                 verifier=verifier)
    transition_id, transition_source_id = _snapshot_transition(body, identity)
    if current_state is not None:
        current_identity, high_water = _validate_state(current_state, public_keys, now=now, verifier=verifier)
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
            if transition.get("kind") == "initial_claim" and transition_source_id != anchor.get("appliance_id"):
                raise Reject("identity_anchor_mismatch")
            if transition.get("kind") == "authorized":
                raise Reject("accepted_identity_changed")
        if anchor is not None and anchor.get("appliance_id") != bound["appliance_id"]:
            if (not isinstance(transition, dict) or transition.get("kind") != "initial_claim" or
                    transition_source_id != anchor.get("appliance_id")):
                raise Reject("identity_anchor_mismatch")
        elif transition_id is not None and body.get("identity_transition", {}).get("kind") != "initial_claim":
            raise Reject("unexpected_identity_transition")
    key_id, _ = _decode_signature(snapshot)
    accepted_key = public_keys.get(key_id)
    if accepted_key is None:
        raise Reject("bad_signature")
    record = _state_record(snapshot, _safe_identity(identity), issued_at, digest,
                           (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).isoformat(),
                           accepted_key, transition_id)
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


def _bootstrap_identity(identity_dir_fd: int, trust_dir_fd: int, *, identity_name="appliance_identity.json") -> str:
    """Create the initial local pin, or wait for signed cloud identity acceptance."""
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
        if existing is not None:
            if existing["appliance_id"] != appliance_id:
                # A local identity changes only after claim/re-enrollment.
                # Never rewrite the root pin from this service-writable file;
                # the acceptance watcher must first verify a signed transition.
                try:
                    accepted_raw = _read_at(trust_dir_fd, ACCEPTED_FILE, MAX_STATE_BYTES,
                                            root_owned=True, absent_ok=True)
                    if accepted_raw is not None:
                        keyset = _validate_keyset(_json(_read_at(
                            trust_dir_fd, TRUST_FILE, MAX_TRUST_BYTES, root_owned=True)))
                        accepted_state = _json(accepted_raw)
                        accepted_identity, _ = _validate_state(accepted_state, keyset)
                        if (accepted_identity["appliance_id"] == appliance_id and
                                _transition_source_appliance_id(accepted_state) == existing["appliance_id"]):
                            _write_identity_anchor(trust_dir_fd, appliance_id)
                            return "reconciled_signed_transition"
                except (Reject, OSError):
                    pass
                return "awaiting_signed_transition"
            return "unchanged"
        _write_identity_anchor(trust_dir_fd, appliance_id)
        return "bootstrapped"
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
                   allow_expired_first=False) -> str:
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
            compat_file != COMPAT_FILE) and verifier is None:
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
        keyset = _validate_keyset(_json(_read_at(root_dir_fd, trust_file, MAX_TRUST_BYTES, root_owned=True)))
        identity_file = Path(identity_path)
        identity_dir_fd = _open_dir(str(identity_file.parent))
        snapshot = _json(raw)
        identity = _json(_read_at(identity_dir_fd, identity_file.name, MAX_IDENTITY_BYTES))
        pinned_identity = _identity_anchor(root_dir_fd)
        current_raw = _read_at(root_dir_fd, accepted_file, MAX_STATE_BYTES, root_owned=True, absent_ok=True)
        current_state = _json(current_raw) if current_raw is not None else None
        if current_state is not None:
            _validate_state(current_state, keyset, now=now, verifier=verifier or
                            (lambda key, message, sig: _openssl_verify(key, message, sig, openssl=openssl)))
        lock_fd = _acquire_lock(root_dir_fd)
        # Re-read after obtaining the lock so two concurrent root invocations
        # cannot validate against the same old high-water mark.
        current_raw = _read_at(root_dir_fd, accepted_file, MAX_STATE_BYTES, root_owned=True, absent_ok=True)
        current_state = _json(current_raw) if current_raw is not None else None
        pinned_identity = _identity_anchor(root_dir_fd)
        if current_state is not None and current_state.get("identity", {}).get("appliance_id") != pinned_identity["appliance_id"]:
            _validate_state(current_state, keyset, now=now, verifier=verifier or
                            (lambda key, message, sig: _openssl_verify(key, message, sig, openssl=openssl)))
            if _transition_source_appliance_id(current_state) != pinned_identity["appliance_id"]:
                raise Reject("identity_anchor_mismatch")
            _write_identity_anchor(root_dir_fd, current_state["identity"]["appliance_id"])
            pinned_identity = _identity_anchor(root_dir_fd)
        policy_verifier = verifier or (lambda key, message, sig: _openssl_verify(key, message, sig, openssl=openssl))
        next_state, outcome = _advance(snapshot, keyset, identity, current_state,
                                       now=now, verifier=policy_verifier,
                                       allow_expired_first=allow_expired_first,
                                       anchor=pinned_identity)
        if outcome == "unchanged":
            return outcome
        encoded = (json.dumps(next_state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_STATE_BYTES:
            raise Reject("accepted_state_too_large")
        _write_atomic_at(root_dir_fd, accepted_file, encoded)
        if pinned_identity["appliance_id"] != next_state["identity"]["appliance_id"]:
            # The accepted signed state is the transaction's source of truth.
            # If this secondary pin write is interrupted, a later invocation
            # can repair it only from the root-validated transition above.
            _write_identity_anchor(root_dir_fd, next_state["identity"]["appliance_id"])
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


def main() -> int:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("capacity acceptance helper must run as root", file=sys.stderr)
        return 1
    if len(sys.argv) == 2 and sys.argv[1] == "--bootstrap-identity":
        trust_dir = None
        try:
            trust_dir = _open_dir(TRUST_DIR, root_controlled=True)
            info = os.fstat(trust_dir)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise Reject("unsafe_trust_directory")
            missing = None
            for identity_path in (IDENTITY_FILE, INSTALLER_IDENTITY_FILE):
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
    if len(sys.argv) == 2 and sys.argv[1] == "--accept-migrated-snapshot":
        try:
            print(accept_pending(allow_expired_first=True))
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
