#!/usr/bin/python3
"""AnyAiCam Software Update -- the root applier (2026-10-03).

Started only by the privileged watcher's fixed 'apply_release' action, in its
own transient systemd unit. It takes NO arguments and reads NOTHING from the
marker: it looks for the single release the agent staged under
/var/lib/anyaicam/updates/staged/<update_id>/ and re-verifies all of it
before touching the running VMS.

Runs with the system /usr/bin/python3 and imports only root-owned files from
its own directory (/opt/anyaicam-agent/privileged/): never the agent's venv.

Untrusted input: everything under /var/lib/anyaicam (the agent's state) and
/etc/anyaicam (owned by the agent user) is treated as attacker-controlled.
Staged files are opened O_NOFOLLOW relative to an O_NOFOLLOW directory
handle and fstat-checked (regular file, one link, owned by the agent user,
not group/world-writable) before a byte is read. Root's own state -- work
area, results, audit log, and the installed-release record used for the
downgrade check -- lives in root-owned /var/lib/anyaicam-update/. Every file
root writes into an agent-owned directory (vms.env, the release marker) is
created under a random name with O_EXCL|O_NOFOLLOW and renamed into place,
so a planted symlink can never redirect a root write. Nothing from a staged
file is ever used as a command, a path or a shell fragment: the update_id
is a strict identifier that must equal the signed manifest's, the build_id
is 40 hex digits, and the version is dotted integers.

Sequence (each failure is reported; nothing claims success early):

  lock (one update at a time, across processes)
  copy the staged files into root's 0700 work area (checks above)
  verify: manifest signature (system openssl, root-owned pinned key),
          package SHA-256 + size, target, platform, CPU architecture,
          newer than the installed release, release.env version/build,
          artifact-files.json hashes, safe archive members, free disk,
          additive-only migrations, nothing unexpected in the live tree
          and the entitlement PUBLIC keyset the release may carry (named in
          release.env, canonical, public-only)
  stage the new application as /opt/anyaicam.next (live tree untouched)
  build the new image, back up the database, tag the current image --
          all while the current release keeps running
  install that keyset root:root 0644 at the fixed trust-anchor path the
          VMS reads (atomic, never through a symlink); a failure stops the
          update here with nothing changed
  ---- downtime ----
  stop the VMS; rename /opt/anyaicam -> /opt/anyaicam.previous and
  /opt/anyaicam.next -> /opt/anyaicam (directory renames, never a copy into
  the bind-mounted live tree); point the image and vms.env at the release;
  start the VMS
  validate: /version reports the release's version AND full build_id,
          /static/release-identity.json (served from the bind-mounted tree)
          names the same build, /health answers, /ready self-test passes
  success: only now record the release (root record, then the marker)
  failure: stop, rename the failed tree aside, rename .previous back,
          restore the image tag and vms.env identity, start, and validate
          that the previous build is the one answering. If that fails too:
          hard failure with recovery information, never success.

Persistent data never lives in the swapped tree: recordings, database (incl.
face templates), HLS, data-config, credentials, identity, certificates and
entitlements are under /var/lib/anyaicam and /etc/anyaicam and are never
touched. The installer-provisioned mediamtx/ and any legacy TLS files are
carried into the new tree; any other unexpected entry stops the update.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import stat as statmod
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # installed next to this script as a root-owned copy
    import anyaicam_release_checks as release_checks  # type: ignore
except ImportError:  # running from the source tree (tests)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from anyaicam_agent.updater import release_checks  # type: ignore

DEVICE_TARGET = "anyaicam-appliance"
VMS_SERVICE = "anyaicam-vms.service"
VMS_IMAGE = "anyaicam-vms"
VMS_CONTAINER = "anyaicam-vms"
IDENTITY_KEYS = ("ANYAICAM_VERSION", "ANYAICAM_BUILD_ID", "ANYAICAM_VMS_COMMIT")
RELEASE_IDENTITY_STATIC = "static/release-identity.json"   # inside payload/vms/app (release_checks.REQUIRED)
_STAGED_FILES = ("manifest.json", "manifest.sig", "package.tar.gz", "request.json")
_MAX_SMALL_FILE = 1024 * 1024
_PRINTABLE = re.compile(r"[^\x20-\x7e]")

# Online, integrity-checked SQLite backup inside the running VMS: the same
# method installer/06-deploy-vms.sh backup_vms_database() uses.
_DB_BACKUP = (
    "import sqlite3,sys\n"
    "name=sys.argv[1]\n"
    "src=sqlite3.connect('file:/app/recordings/partner_portal.db?mode=ro',uri=True)\n"
    "out=sqlite3.connect('/app/recordings/'+name)\n"
    "src.backup(out);out.close();src.close()\n"
    "chk=sqlite3.connect('file:/app/recordings/'+name+'?mode=ro',uri=True)\n"
    "ok=chk.execute('PRAGMA quick_check').fetchone()[0]\n"
    "chk.close()\n"
    "sys.exit(0 if ok=='ok' else 1)\n"
)


@dataclass
class Paths:
    staged: Path = Path("/var/lib/anyaicam/updates/staged")        # agent-owned: untrusted
    root_state: Path = Path("/var/lib/anyaicam-update")             # root-owned 0755
    live: Path = Path("/opt/anyaicam")
    trusted_key: Path = Path("/etc/anyaicam-update/trusted_signing_key.pem")
    # Paid-feature trust anchor (2026-10-05): fixed, the path the VMS reads
    # (app/appliance_entitlements.py TRUST_ANCHOR_FILE).
    entitlement_keys: Path = Path("/etc/anyaicam-update/entitlement_signing_keys.json")
    vms_env: Path = Path("/etc/anyaicam/vms.env")                   # in an agent-owned directory
    release_marker: Path = Path("/etc/anyaicam/vms_release.json")   # in an agent-owned directory
    recordings: Path = Path("/var/lib/anyaicam/vms/recordings")     # where the database backups are
    lock: Path = Path("/run/anyaicam-software-update.lock")
    os_release: Path = Path("/etc/os-release")
    vms_url: str = "http://127.0.0.1:8000"

    @property
    def work(self) -> Path:
        return self.root_state / "work"

    @property
    def results(self) -> Path:
        return self.root_state / "results"

    @property
    def audit_log(self) -> Path:
        return self.root_state / "audit.log"

    @property
    def installed_record(self) -> Path:
        return self.root_state / "installed_release.json"

    @property
    def rollback_dir(self) -> Path:
        """Rollback points for `sudo anyaicam-rollback` (installer/rollback.sh);
        the installer writes its own here too."""
        return self.root_state / "rollback"

    @property
    def next(self) -> Path:
        return self.live.with_name(self.live.name + ".next")

    @property
    def previous(self) -> Path:
        return self.live.with_name(self.live.name + ".previous")

    @property
    def failed(self) -> Path:
        return self.live.with_name(self.live.name + ".failed")


class UpdateBusy(Exception):
    pass


class Failure(Exception):
    def __init__(self, state: str, code: str, message: str):
        super().__init__(message)
        self.state = state
        self.code = code


def _posix() -> bool:
    return hasattr(os, "geteuid")


def _agent_uid() -> Optional[int]:
    try:
        import pwd
        return pwd.getpwnam("anyaicam").pw_uid
    except (ImportError, KeyError):
        return None


# ---------------------------------------------------------------- file safety

def ownership_problem(info, *, expected_uid: Optional[int], posix: Optional[bool] = None) -> str:
    """'' when a stat result is a single-link regular file or a directory,
    owned by expected_uid and not writable by group or others; otherwise
    what is wrong. expected_uid=None skips only the owner comparison (hosts
    without POSIX ownership, i.e. tests on Windows)."""
    if not (statmod.S_ISREG(info.st_mode) or statmod.S_ISDIR(info.st_mode)):
        return "is not a regular file or directory"
    if statmod.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) != 1:
        return "has more than one hard link"
    if expected_uid is not None and info.st_uid != expected_uid:
        return f"is owned by uid {info.st_uid}, expected {expected_uid}"
    if (_posix() if posix is None else posix) and info.st_mode & 0o022:
        return "is writable by group or others"
    return ""


def safe_replace(directory: Path, name: str, data: bytes, *, mode: int = 0o644, owner: Optional[tuple] = None) -> None:
    """Writes `name` inside `directory` without following any symlink a
    less-privileged user could have planted: a fresh random temp file
    (O_CREAT|O_EXCL|O_NOFOLLOW), then an atomic rename, which replaces a
    planted symlink itself rather than writing through it."""
    temporary = directory / f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            if _posix():
                os.fchmod(handle.fileno(), mode)
                if owner is not None:
                    os.fchown(handle.fileno(), owner[0], owner[1])
        os.replace(temporary, directory / name)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class TrustAnchorError(Exception):
    """A trust anchor could not be installed safely. `code` is
    'trust_anchor_unsafe' (the destination is not safe to write) or
    'trust_anchor_write_failed' (the write itself failed)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _make_root_dir(directory: Path, posix: bool) -> None:
    os.mkdir(directory, 0o755)  # fails rather than following anything planted there
    if posix:
        os.chown(directory, 0, 0, follow_symlinks=False)
        os.chmod(directory, 0o755, follow_symlinks=False)


def install_trust_anchor(target: Path, data: bytes, *, posix: Optional[bool] = None, lstat=os.lstat,
                         write=None, make_dir=None) -> None:
    """Installs `data` as a root-owned trust anchor at `target`: the one
    routine for both the Software Update applier and installer step 13
    (installer/13-entitlement-signing-keys.sh), with the same rules the VMS
    (app/appliance_entitlements.py) and validate.sh enforce.

    Refused, before anything is written (TrustAnchorError
    'trust_anchor_unsafe'):
      * the directory is a symlink, not a directory, not owned by root, or
        writable by group or others;
      * an existing destination is a symlink, not a regular file, hard-linked,
        not owned by root, or writable by group or others.
    A missing directory is created root:root 0755. The keyset is written
    root:root 0644 through safe_replace(): a random O_CREAT|O_EXCL|O_NOFOLLOW
    temp file in that root-owned directory, then an atomic rename -- never
    through a symlink. Any failure leaves the previous anchor (or none) in
    place, so paid features fail closed. posix/lstat/write/make_dir exist for
    tests on hosts without POSIX ownership."""
    posix = _posix() if posix is None else posix
    root_uid = 0 if posix else None
    directory = target.parent

    def unsafe(message: str) -> TrustAnchorError:
        return TrustAnchorError("trust_anchor_unsafe", message)

    try:
        info = lstat(directory)
    except FileNotFoundError:
        try:
            (make_dir or _make_root_dir)(directory, posix)
            info = lstat(directory)
        except OSError as error:
            raise unsafe(f"{directory} could not be created safely: {error}") from error
    except OSError as error:
        raise unsafe(f"{directory} cannot be inspected: {error}") from error
    if statmod.S_ISLNK(info.st_mode):
        raise unsafe(f"{directory} is a symbolic link")
    if not statmod.S_ISDIR(info.st_mode):
        raise unsafe(f"{directory} is not a directory")
    problem = ownership_problem(info, expected_uid=root_uid, posix=posix)
    if problem:
        raise unsafe(f"{directory} {problem}")
    try:
        existing = lstat(target)
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise unsafe(f"{target} cannot be inspected: {error}") from error
    if existing is not None:
        if statmod.S_ISLNK(existing.st_mode):
            raise unsafe(f"{target} is a symbolic link")
        if not statmod.S_ISREG(existing.st_mode):
            raise unsafe(f"{target} is not a regular file")
        problem = ownership_problem(existing, expected_uid=root_uid, posix=posix)
        if problem:
            raise unsafe(f"{target} {problem}")
    try:
        (write or safe_replace)(directory, target.name, data, mode=0o644, owner=(0, 0) if posix else None)
    except OSError as error:
        raise TrustAnchorError("trust_anchor_write_failed", f"writing {target} failed: {error}") from error


def read_untrusted(path: Path, *, max_bytes: int, expected_uid: Optional[int], dir_fd: Optional[int] = None) -> bytes:
    """Reads a file a less-privileged user controls: never through a
    symlink, never a hard link to someone else's file, never a device."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        if dir_fd is not None:
            descriptor = os.open(path.name, flags, dir_fd=dir_fd)
        else:
            if path.is_symlink():
                raise OSError("is a symbolic link")
            descriptor = os.open(path, flags)
    except OSError as error:
        raise Failure("rejected", "bad_staging", f"{path.name} cannot be opened safely: {error}") from error
    with os.fdopen(descriptor, "rb") as handle:
        problem = ownership_problem(os.fstat(handle.fileno()), expected_uid=expected_uid)
        if problem:
            raise Failure("rejected", "bad_staging", f"{path.name} {problem}")
        if statmod.S_ISDIR(os.fstat(handle.fileno()).st_mode):
            raise Failure("rejected", "bad_staging", f"{path.name} is a directory")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise Failure("rejected", "bad_staging", f"{path.name} is unexpectedly large")
    return data


def ensure_root_dir(path: Path, mode: int) -> Path:
    """Creates (or checks) a directory only root may write."""
    path.mkdir(parents=True, exist_ok=True)
    info = os.lstat(path)
    if statmod.S_ISLNK(info.st_mode) or not statmod.S_ISDIR(info.st_mode):
        raise Failure("rejected", "unsafe_state_dir", f"{path} is not a real directory")
    if _posix():
        if info.st_uid != 0:
            os.chown(path, 0, 0, follow_symlinks=False)
        os.chmod(path, mode)
    return path


def trusted_file_problem(path: Path) -> str:
    """The trusted key and its directory must be root-owned and not writable
    by anyone else, or a less-privileged user could swap the key."""
    for candidate in (path.parent, path):
        try:
            info = os.lstat(candidate)
        except OSError as error:
            return f"{candidate} is missing ({error})"
        if statmod.S_ISLNK(info.st_mode):
            return f"{candidate} is a symbolic link"
        problem = ownership_problem(info, expected_uid=0 if _posix() else None)
        if problem:
            return f"{candidate} {problem}"
    return ""


class _Lock:
    """Exclusive, process-wide: flock where available (released by the
    kernel if this process dies), else an O_EXCL lock file."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None
        self.flock = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # non-POSIX test hosts
            try:
                self.handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as error:
                raise UpdateBusy("another software update is already running") from error
            return self
        self.handle = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self.handle)
            self.handle = None
            raise UpdateBusy("another software update is already running") from error
        self.flock = True
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            os.close(self.handle)
            if not self.flock:
                self.path.unlink(missing_ok=True)
            self.handle = None
        return False


def _default_run(argv: list, timeout: int) -> tuple:
    """Runs one fixed argument vector: never a shell string."""
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, str(error)
    return done.returncode, done.stdout.decode("utf-8", "replace")[-4000:]


def _default_http_get(url: str, timeout: float) -> tuple:
    request = urllib.request.Request(url, headers={"X-Forwarded-Proto": "https"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(65536).decode("utf-8", "replace") if error.fp else ""
    except (urllib.error.URLError, OSError, ValueError) as error:
        return 0, str(error)


def openssl_verify(key: Path, data: bytes, signature: bytes, run=_default_run) -> bool:
    """RSA-PKCS1v15 + SHA-256, the scheme updater/verify.py uses, checked with
    the system openssl so the root side imports no third-party code."""
    with tempfile.TemporaryDirectory() as scratch:
        data_path = Path(scratch) / "manifest.canonical"
        sig_path = Path(scratch) / "manifest.sig.bin"
        data_path.write_bytes(data)
        sig_path.write_bytes(signature)
        code, _ = run(["openssl", "dgst", "-sha256", "-verify", str(key), "-signature", str(sig_path), str(data_path)], 60)
    return code == 0


def canonical_manifest_bytes(manifest_dict: dict) -> bytes:
    return json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class _Manifest:
    """The fields release_checks needs, read from the verified manifest."""
    update_id: str
    version: str
    sha256: str
    target: str
    platform: str
    architecture: str
    package_size_bytes: int
    build_id: str
    migration_safety: str

    @classmethod
    def from_dict(cls, data: dict) -> "_Manifest":
        try:
            return cls(update_id=str(data["update_id"]), version=str(data["version"]), sha256=str(data["sha256"]).lower(),
                       target=str(data["target"]), platform=str(data["platform"]), architecture=str(data["architecture"]),
                       package_size_bytes=int(data["package_size_bytes"]), build_id=str(data.get("build_id") or ""),
                       migration_safety=str(data.get("migration_safety") or ""))
        except (KeyError, TypeError, ValueError) as error:
            raise Failure("rejected", "bad_manifest", f"manifest is malformed: {error}") from error


@dataclass
class Applier:
    paths: Paths = field(default_factory=Paths)
    run: Callable[[list, int], tuple] = _default_run
    http_get: Callable[[str, float], tuple] = _default_http_get
    verify_signature: Optional[Callable[[Path, bytes, bytes], bool]] = None
    platform: Optional[str] = None
    architecture: Optional[str] = None
    disk_usage: Callable = shutil.disk_usage
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    validation_timeout_seconds: float = 300.0
    validation_interval_seconds: float = 3.0
    build_timeout_seconds: int = 3600
    agent_uid: Optional[int] = field(default_factory=lambda: _agent_uid() if _posix() else None)

    # ------------------------------------------------------------------ output
    def _prepare_root_state(self) -> None:
        ensure_root_dir(self.paths.root_state, 0o755)
        ensure_root_dir(self.paths.results, 0o755)   # the agent reads results; only root writes them
        ensure_root_dir(self.paths.work, 0o700)

    def _write_result(self, update_id: str, payload: dict) -> None:
        safe_replace(self.paths.results, f"{update_id}.json", json.dumps(payload, sort_keys=True).encode("utf-8"))
        _journal(payload)

    def _audit(self, entry: dict) -> None:
        """Append-only JSON lines in root's own directory (never the
        agent-owned /var/log/anyaicam). No secrets: identities, versions,
        states and error summaries only."""
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.paths.audit_log, flags, 0o640)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ entry
    def apply_staged(self) -> Optional[dict]:
        """Processes the single staged release, if any. Returns the final
        result written (None when nothing was staged or another update holds
        the lock)."""
        try:
            with _Lock(self.paths.lock):
                self._prepare_root_state()
                update_id = self._pick_staged()
                if update_id is None:
                    return None
                return self._apply(update_id)
        except UpdateBusy:
            return None

    def _pick_staged(self) -> Optional[str]:
        staged = self.paths.staged
        if staged.is_symlink() or not staged.is_dir():
            return None
        candidates = []
        for entry in sorted(staged.iterdir(), key=lambda item: item.name):
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                release_checks.validate_update_id(entry.name)
            except release_checks.ReleaseCheckError:
                continue
            candidates.append(entry.name)
        if len(candidates) > 1:
            for update_id in candidates:
                self._write_result(update_id, {"update_id": update_id, "state": "rejected", "final": True,
                                               "error": "update_in_progress: more than one release was staged"})
                shutil.rmtree(staged / update_id, ignore_errors=True)
            return None
        return candidates[0] if candidates else None

    def _apply(self, update_id: str) -> dict:
        started = self.now()
        self.rollback_image, self.database_backup, self.previous_env, self.previous_marker = "", "", None, None
        self.entitlement_keyset = None
        previous = self._installed_release()
        record = {"update_id": update_id, "from_version": previous.get("version", ""),
                  "from_build_id": previous.get("build_id", ""), "to_version": "", "to_build_id": "",
                  "started_at": datetime.now(timezone.utc).isoformat(), "requested_by": ""}

        def progress(state: str, **extra) -> None:
            record.update(extra)
            self._write_result(update_id, dict(record, state=state, final=False))

        def finish(state: str, error: str = "", **extra) -> dict:
            record.update(extra)
            result = dict(record, state=state, final=True, error=error[:1000],
                          finished_at=datetime.now(timezone.utc).isoformat(),
                          duration_seconds=round(self.now() - started, 1))
            self._write_result(update_id, result)
            self._audit({k: result.get(k) for k in ("update_id", "requested_by", "from_version", "from_build_id",
                                                    "to_version", "to_build_id", "state", "error", "recovery",
                                                    "started_at", "finished_at")})
            return result

        progress("preflight")
        downtime_started = False
        try:
            manifest, release_root, request = self._verify(update_id)
            record.update(to_version=manifest.version, to_build_id=manifest.build_id,
                          requested_by=request["requested_by"])
            progress("preflight")
            self._stage_next(release_root)
            self._prepare_before_downtime(manifest, previous)
            record["entitlement_keyset"] = self._install_entitlement_keyset()
            progress("installing")
            downtime_started = True
            self._activate(manifest)
            progress("validating")
            problem = self._validate(manifest.version, manifest.build_id)
            if problem:
                raise Failure("rolling_back", "health_check_failed", problem)
            self._record_release(manifest, release_root, update_id)
            self._cleanup_after_success(update_id)
            # A manual way back to the release just replaced (2026-10-04). The
            # update itself already succeeded: a problem here is reported in
            # the result, never turned into a failed update.
            try:
                rollback_point = self._write_rollback_point(manifest, previous, update_id)
            except Exception as error:  # noqa: BLE001
                rollback_point = f"unavailable: {type(error).__name__}: {error}"[:300]
            return finish("healthy", rollback_point=rollback_point)
        except Failure as failure:
            if not downtime_started:
                self._cleanup_without_change(update_id)
                return finish("rejected" if failure.state == "rejected" else "install_failed", f"{failure.code}: {failure}")
            progress("rolling_back", error=f"{failure.code}: {failure}")
            return self._rollback(previous, f"{failure.code}: {failure}", finish)
        except Exception as error:  # noqa: BLE001 -- never leave a half-applied release unreported
            if not downtime_started:
                self._cleanup_without_change(update_id)
                return finish("install_failed", f"unexpected_error: {type(error).__name__}: {error}")
            progress("rolling_back", error=f"unexpected_error: {type(error).__name__}")
            return self._rollback(previous, f"unexpected_error: {type(error).__name__}: {error}", finish)

    # ------------------------------------------------------------------ verify
    def _installed_release(self) -> dict:
        """The running release, for the downgrade check and rollback
        validation. Root's own record wins: the release marker sits in a
        directory the agent user owns and could be forged to a lower version
        to allow a downgrade. The marker is used only before the first
        Software Update has recorded a release."""
        def parse(path: Path, expected_uid) -> dict:
            try:
                data = json.loads(read_untrusted(path, max_bytes=_MAX_SMALL_FILE, expected_uid=expected_uid))
            except (Failure, ValueError):
                return {}
            if not isinstance(data, dict):
                return {}
            version = str(data.get("release_version") or data.get("installer_version") or "")
            build = str(data.get("vms_release_commit") or "")
            try:
                release_checks.parse_release_version(version)
            except release_checks.ReleaseCheckError:
                version = ""
            return {"version": version, "build_id": build if re.fullmatch(r"[0-9a-f]{40}", build) else ""}
        record = parse(self.paths.installed_record, 0 if _posix() else None)
        if record.get("version"):
            return record
        return parse(self.paths.release_marker, None)

    def _work_dir(self, update_id: str) -> Path:
        return self.paths.work / update_id

    def _copy_staged(self, update_id: str) -> Path:
        """Copies the staged files into root's 0700 work area through file
        descriptors: the staged directory is opened O_NOFOLLOW|O_DIRECTORY
        and each file O_NOFOLLOW relative to it, so the agent cannot swap in
        a symlink between the check and the read. Each file must be owned by
        the agent user, single-link, not group/world-writable."""
        source = self.paths.staged / update_id
        work = self._work_dir(update_id)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        if _posix():
            os.chmod(work, 0o700)
        dir_fd = None
        if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
            try:
                dir_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError as error:
                raise Failure("rejected", "bad_staging", f"the staged release folder cannot be opened safely: {error}") from error
            problem = ownership_problem(os.fstat(dir_fd), expected_uid=self.agent_uid)
            if problem:
                os.close(dir_fd)
                raise Failure("rejected", "bad_staging", f"the staged release folder {problem}")
        elif source.is_symlink():
            raise Failure("rejected", "bad_staging", "the staged release folder is a symbolic link")
        try:
            for name in _STAGED_FILES:
                limit = release_checks.MAX_UNPACKED_BYTES if name == "package.tar.gz" else _MAX_SMALL_FILE
                data = read_untrusted(source / name, max_bytes=limit, expected_uid=self.agent_uid, dir_fd=dir_fd)
                descriptor = os.open(work / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                with os.fdopen(descriptor, "wb") as writer:
                    writer.write(data)
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
        shutil.rmtree(source, ignore_errors=True)  # consumed: the agent's copy is never read again
        return work

    def _verify(self, update_id: str):
        work = self._copy_staged(update_id)
        try:
            manifest_dict = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
            signature = base64.b64decode((work / "manifest.sig").read_bytes(), validate=True)
            request = json.loads((work / "request.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise Failure("rejected", "bad_staging", f"staged files are unreadable: {error}") from error
        if not isinstance(manifest_dict, dict) or not isinstance(request, dict):
            raise Failure("rejected", "bad_staging", "staged manifest/request must be JSON objects")
        problem = trusted_file_problem(self.paths.trusted_key)
        if problem:
            raise Failure("rejected", "untrusted_key", f"the release-signing key is not safely provisioned: {problem}")
        verify = self.verify_signature or (lambda key, data, sig: openssl_verify(key, data, sig, self.run))
        if not verify(self.paths.trusted_key, canonical_manifest_bytes(manifest_dict), signature):
            raise Failure("rejected", "bad_signature", "the manifest signature does not verify against the trusted key")
        manifest = _Manifest.from_dict(manifest_dict)
        if manifest.update_id != update_id:
            raise Failure("rejected", "bad_staging", "the staged directory does not match the signed update_id")
        # request.json is agent-written: used only as a sanitized audit label.
        request = {"requested_by": _PRINTABLE.sub("", str(request.get("requested_by") or ""))[:200]}
        try:
            platform = self.platform if self.platform is not None else release_checks.device_platform(self.paths.os_release)
            architecture = self.architecture if self.architecture is not None else release_checks.device_architecture()
            release_checks.check_manifest_for_device(manifest, target=DEVICE_TARGET, platform=platform,
                                                     architecture=architecture,
                                                     current_version=self._installed_release().get("version", ""))
            package = work / "package.tar.gz"
            if package.stat().st_size != manifest.package_size_bytes:
                raise release_checks.ReleaseCheckError("bad_hash", "package size does not match the signed manifest")
            if release_checks.sha256_file(package) != manifest.sha256:
                raise release_checks.ReleaseCheckError("bad_hash", "package SHA-256 does not match the signed manifest")
            needed = release_checks.required_free_bytes(manifest.package_size_bytes)
            release_checks.check_free_space(self.paths.work, needed, disk_usage=self.disk_usage)
            release_checks.check_free_space(self.paths.live.parent, needed, disk_usage=self.disk_usage)
            release_root = release_checks.safe_extract(package, work / "release")
            env = release_checks.verify_release_tree(release_root, manifest)
            # The entitlement PUBLIC keyset, if the release carries one: bound
            # to this signature through the package hash, release.env and
            # artifact-files.json, and validated here before anything changes.
            self.entitlement_keyset = release_checks.release_entitlement_keyset(release_root, env)
            payload = release_root / "payload" / "vms"
            identity = json.loads((payload / "app" / RELEASE_IDENTITY_STATIC).read_text(encoding="utf-8"))
            if identity.get("build_id") != manifest.build_id or identity.get("version") != manifest.version:
                raise release_checks.ReleaseCheckError("bad_package", "the release identity file does not match the manifest")
            unsafe = release_checks.find_unsafe_migrations(self.paths.live / "app", payload / "app")
            if unsafe:
                raise release_checks.ReleaseCheckError(
                    "unsafe_migrations", "the release changes the database in a way the previous version cannot use: "
                    + "; ".join(unsafe[:5]))
            problems = release_checks.preservation_problems(self.paths.live, payload)
            if problems:
                raise release_checks.ReleaseCheckError("unexpected_live_files",
                                                       "the application folder holds data this update would not carry over: "
                                                       + "; ".join(problems[:10]))
        except release_checks.ReleaseCheckError as error:
            raise Failure("rejected", error.code, str(error)) from error
        except (OSError, ValueError, AttributeError) as error:
            raise Failure("rejected", "bad_package", f"the release package is unreadable: {error}") from error
        return manifest, release_root, request

    # ------------------------------------------------------------------ stage (no downtime)
    def _stage_next(self, release_root: Path) -> None:
        nxt = self.paths.next
        shutil.rmtree(nxt, ignore_errors=True)
        shutil.copytree(release_root / "payload" / "vms", nxt, symlinks=False)
        for relative in release_checks.CARRIED_OVER:
            source = self.paths.live / relative
            if source.is_symlink():
                raise Failure("install_failed", "unexpected_live_files", f"{source} is a symbolic link")
            if source.is_dir():
                shutil.copytree(source, nxt / relative, symlinks=False, dirs_exist_ok=True)
            elif source.is_file():
                (nxt / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, nxt / relative)

    def _docker(self, *args, timeout: int = 300) -> tuple:
        return self.run(["docker", *args], timeout)

    def _prepare_before_downtime(self, manifest: _Manifest, previous: dict) -> None:
        tag = f"{VMS_IMAGE}:release-{manifest.build_id[:12]}"
        code, output = self._docker("build", "-t", tag, str(self.paths.next), timeout=self.build_timeout_seconds)
        if code != 0:
            raise Failure("install_failed", "image_build_failed", f"building the new image failed: {output[-500:]}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"partner_portal-pre-{manifest.build_id[:12]}-{stamp}.db"
        code, output = self._docker("exec", VMS_CONTAINER, "python", "-c", _DB_BACKUP, backup_name, timeout=600)
        if code != 0:
            raise Failure("install_failed", "database_backup_failed", f"the database backup failed: {output[-300:]}")
        self.database_backup = backup_name
        rollback_tag = f"{VMS_IMAGE}:rollback-{(previous.get('build_id') or 'previous')[:12]}"
        code, output = self._docker("tag", f"{VMS_IMAGE}:latest", rollback_tag)
        if code != 0:
            raise Failure("install_failed", "image_tag_failed", f"keeping the current image failed: {output[-300:]}")
        self.rollback_image = rollback_tag
        self.previous_env = self._read_identity_env()
        self.previous_marker = self._read_marker_for_rollback()

    def _install_entitlement_keyset(self) -> str:
        """Installs the verified keyset root:root 0644 at the fixed trust-anchor
        path (temp file + atomic rename, never through a symlink), before any
        downtime: a failure here stops the update with the running release
        and the existing keyset untouched, so paid features stay as they were
        (or denied, never granted). Kept if a later step rolls back: it is the
        signed release's own public keyset. 'unchanged' when the release
        carries none."""
        if self.entitlement_keyset is None:
            return "unchanged"
        try:
            install_trust_anchor(self.paths.entitlement_keys, self.entitlement_keyset)
        except TrustAnchorError as error:
            raise Failure("install_failed", error.code, f"installing the entitlement keyset failed: {error}") from error
        return "updated"

    # ------------------------------------------------------------------ activate (downtime)
    def _systemctl(self, action: str) -> tuple:
        return self.run(["systemctl", action, VMS_SERVICE], 600)

    def _activate(self, manifest: _Manifest) -> None:
        code, output = self._systemctl("stop")
        if code != 0:
            raise Failure("rolling_back", "stop_failed", f"stopping the VMS failed: {output[-300:]}")
        shutil.rmtree(self.paths.previous, ignore_errors=True)
        os.rename(self.paths.live, self.paths.previous)
        os.rename(self.paths.next, self.paths.live)
        code, output = self._docker("tag", f"{VMS_IMAGE}:release-{manifest.build_id[:12]}", f"{VMS_IMAGE}:latest")
        if code != 0:
            raise Failure("rolling_back", "image_tag_failed", f"activating the new image failed: {output[-300:]}")
        self._write_identity_env({"ANYAICAM_VERSION": manifest.version, "ANYAICAM_BUILD_ID": manifest.build_id,
                                  "ANYAICAM_VMS_COMMIT": manifest.build_id})
        code, output = self._systemctl("start")
        if code != 0:
            raise Failure("rolling_back", "start_failed", f"starting the new release failed: {output[-300:]}")

    # ------------------------------------------------------------------ validate
    def _validate(self, version: Optional[str], build_id: str, *, require_tree_identity: bool = True) -> str:
        """'' when the expected release is the one answering and healthy,
        else the last problem seen. Container state alone never counts:
          /version  -- the identity the restarted VMS process reports
                       (version is skipped when None: releases from before
                       product versions report the code's default)
          /static/release-identity.json -- read from the bind-mounted tree,
                       so it proves the swapped code is the code served
          /health and the /ready self-test."""
        deadline = self.now() + self.validation_timeout_seconds
        problem = "the VMS never answered"
        while True:
            status, body = self.http_get(f"{self.paths.vms_url}/version", 5.0)
            try:
                reported = json.loads(body) if status == 200 else {}
            except ValueError:
                reported = {}
            if status != 200:
                problem = f"/version did not answer (status {status})"
            elif reported.get("build_id") != build_id or (version is not None and reported.get("version") != version):
                problem = (f"/version reports {reported.get('version')!r} build {str(reported.get('build_id'))[:12]!r}, "
                           f"expected {version!r} build {build_id[:12]!r}")
            else:
                tree_problem = ""
                if require_tree_identity:
                    tree_status, tree_body = self.http_get(f"{self.paths.vms_url}/{RELEASE_IDENTITY_STATIC}", 5.0)
                    try:
                        tree = json.loads(tree_body) if tree_status == 200 else {}
                    except ValueError:
                        tree = {}
                    if tree.get("build_id") != build_id:
                        tree_problem = f"the served application tree is build {str(tree.get('build_id'))[:12]!r}"
                health_status, _ = self.http_get(f"{self.paths.vms_url}/health", 5.0)
                ready_status, ready_body = self.http_get(f"{self.paths.vms_url}/ready", 5.0)
                if tree_problem:
                    problem = tree_problem
                elif health_status != 200:
                    problem = f"/health answered {health_status}"
                elif not re.search(r'"self_test"\s*:\s*\{\s*"ok"\s*:\s*true', ready_body or ""):
                    problem = f"/ready self-test did not pass (status {ready_status})"
                else:
                    return ""
            if self.now() >= deadline:
                return problem
            self.sleep(self.validation_interval_seconds)

    # ------------------------------------------------------------------ rollback
    def _rollback(self, previous: dict, reason: str, finish) -> dict:
        steps = []
        try:
            self._systemctl("stop")
            if self.paths.previous.is_dir():
                if self.paths.live.exists():
                    shutil.rmtree(self.paths.failed, ignore_errors=True)
                    os.rename(self.paths.live, self.paths.failed)
                os.rename(self.paths.previous, self.paths.live)
                steps.append("previous application restored to /opt/anyaicam")
            elif not self.paths.live.exists():
                raise RuntimeError("neither the previous nor the current application folder exists")
            if self.rollback_image:
                code, output = self._docker("tag", self.rollback_image, f"{VMS_IMAGE}:latest")
                if code != 0:
                    raise RuntimeError(f"restoring the previous image failed: {output[-300:]}")
                steps.append(f"image {self.rollback_image} restored as latest")
            if self.previous_env is not None:
                self._write_identity_env(self.previous_env)
                steps.append("vms.env release identity restored")
            code, output = self._systemctl("start")
            if code != 0:
                raise RuntimeError(f"starting the previous release failed: {output[-300:]}")
            env = self.previous_env or {}
            expected_build = env.get("ANYAICAM_BUILD_ID") or previous.get("build_id") or ""
            if expected_build:
                # Older trees ship no release-identity file: /version's build
                # (from the restored vms.env, read by the restarted process)
                # plus /health and /ready are what the previous release offers.
                tree_known = (self.paths.live / "app" / RELEASE_IDENTITY_STATIC).is_file()
                problem = self._validate(env.get("ANYAICAM_VERSION") or None, expected_build,
                                         require_tree_identity=tree_known)
            else:
                status, _ = self.http_get(f"{self.paths.vms_url}/health", 5.0)
                problem = "" if status == 200 else f"/health answered {status}"
            if problem:
                raise RuntimeError(f"the previous release did not validate: {problem}")
            steps.append("previous release validated")
        except Exception as error:  # noqa: BLE001 -- a rollback failure is reported, never hidden
            recovery = ("Rollback did not complete. Previous application: "
                        f"{self.paths.previous if self.paths.previous.exists() else self.paths.live}; failed release: "
                        f"{self.paths.failed}; previous image: {self.rollback_image or 'unknown'}; database backup: "
                        f"{self.database_backup or 'none'} (in /var/lib/anyaicam/vms/recordings). `sudo anyaicam-rollback` "
                        "restores the newest rollback point; it refuses one that does not belong to the running release.")
            return finish("rollback_failed", f"{reason}; rollback failed: {error}", recovery=recovery,
                          rollback_steps=steps)
        return finish("rolled_back", reason, rollback_steps=steps)

    # ------------------------------------------------------------------ success / cleanup
    def _record_release(self, manifest: _Manifest, release_root: Path, update_id: str) -> None:
        """Only after validation: the release marker never names a release
        that has not been proven to be the one running."""
        env = release_checks.read_release_env(release_root / "release.env")
        marker = {
            "vms_release_commit": manifest.build_id,
            "release_version": manifest.version,
            "release_archive_sha256": env.get("VMS_RELEASE_SHA256", ""),
            "installer_source_commit": env.get("INSTALLER_SOURCE_COMMIT", ""),
            "mediamtx_included": env.get("MEDIAMTX_INCLUDED", "unknown"),
            "mediamtx_sha256": env.get("MEDIAMTX_SHA256", ""),
            "installer_version": manifest.version,
            "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "installed_by": "software_update",
            "update_id": update_id,
        }
        data = (json.dumps(marker, indent=2) + "\n").encode("utf-8")
        # Root's own record first (the downgrade check trusts only it), then
        # the shared marker the agent and the installer read.
        safe_replace(self.paths.root_state, self.paths.installed_record.name, data)
        safe_replace(self.paths.release_marker.parent, self.paths.release_marker.name, data)

    # ------------------------------------------------------------------ manual rollback point
    _ARCHIVE_EXCLUDES = ("recordings", "data/config", ".env", "mediamtx", "app/static/hls", "app/recordings",
                         "app/auto.key", "app/auto.crt")
    ROLLBACK_POINTS_KEPT = 3

    def _read_marker_for_rollback(self) -> Optional[dict]:
        """The installed-release record as it is before the update: root's
        own copy when there is one, else the shared marker."""
        for path, uid in ((self.paths.installed_record, 0 if _posix() else None), (self.paths.release_marker, None)):
            try:
                data = json.loads(read_untrusted(path, max_bytes=_MAX_SMALL_FILE, expected_uid=uid))
            except (Failure, ValueError):
                continue
            if isinstance(data, dict):
                return data
        return None

    def _archive_previous(self, archive: Path) -> None:
        """The replaced application folder as rollback.sh expects it:
        <live folder name>/app/..., without state, secrets or live segments."""
        source = self.paths.previous
        top = self.paths.live.name
        temporary = archive.with_name(f".{archive.name}.{secrets.token_hex(6)}.tmp")

        def keep(member: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
            relative = member.name.split("/", 1)[1] if "/" in member.name else ""
            parts = relative.split("/")
            if "__pycache__" in parts:
                return None
            if any(relative == item or relative.startswith(item + "/") for item in self._ARCHIVE_EXCLUDES):
                return None
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            return member

        try:
            with tarfile.open(temporary, "w:gz") as tar:
                tar.add(str(source), arcname=top, recursive=True, filter=keep)
            if _posix():
                os.chmod(temporary, 0o640)
            os.replace(temporary, archive)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _write_rollback_point(self, manifest: _Manifest, previous: dict, update_id: str) -> str:
        env = self.previous_env or {}
        build = previous.get("build_id") or env.get("ANYAICAM_BUILD_ID") or ""
        if not re.fullmatch(r"[0-9a-f]{7,40}", build):
            raise RuntimeError("the replaced release has no known build")
        if not self.rollback_image or not self.paths.previous.is_dir():
            raise RuntimeError("the replaced image or application folder is missing")
        version = previous.get("version") or env.get("ANYAICAM_VERSION") or ""
        try:
            release_checks.parse_release_version(version)
        except release_checks.ReleaseCheckError:
            version = ""
        directory = ensure_root_dir(self.paths.rollback_dir, 0o750)
        short = build[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = directory / f"vms-code-{short}-{stamp}.tar.gz"
        self._archive_previous(archive)
        marker = dict(self.previous_marker or {})
        if marker.get("vms_release_commit") != build:
            marker = {"vms_release_commit": build, "release_version": version, "installer_version": version}
        marker_name = f"release-marker-{short}-{stamp}.json"
        safe_replace(directory, marker_name, (json.dumps(marker, indent=2) + "\n").encode("utf-8"), mode=0o640)
        values = {
            "ROLLBACK_COMMIT": build,
            "ROLLBACK_VERSION": version,
            "ROLLBACK_MARKER": str(directory / marker_name),
            "ROLLBACK_IMAGE": self.rollback_image,
            "ROLLBACK_CODE_ARCHIVE": str(archive),
            "ROLLBACK_DATABASE_BACKUP": str(self.paths.recordings / self.database_backup) if self.database_backup else "none",
            "ROLLBACK_CREATED_AT": stamp,
            "UPGRADE_TO_COMMIT": manifest.build_id,
            "UPDATE_ID": update_id,
            "CREATED_BY": "software_update",
        }
        text = "".join(f"{key}={value}\n" for key, value in values.items()).encode("utf-8")
        name = f"rollback-{short}-{stamp}.env"
        safe_replace(directory, name, text, mode=0o640)
        safe_replace(directory, "latest.env", text, mode=0o640)
        self._prune_rollback_points(directory)
        return str(directory / name)

    def _prune_rollback_points(self, directory: Path) -> None:
        """Keeps the newest ROLLBACK_POINTS_KEPT points this applier wrote;
        never touches the installer's."""
        mine = []
        for path in directory.glob("rollback-*.env"):
            try:
                values = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
            except OSError:
                continue
            if values.get("CREATED_BY") == "software_update":
                mine.append((values.get("ROLLBACK_CREATED_AT", ""), path, values))
        mine.sort(key=lambda item: item[0], reverse=True)
        for _, path, values in mine[self.ROLLBACK_POINTS_KEPT:]:
            for key in ("ROLLBACK_CODE_ARCHIVE", "ROLLBACK_MARKER"):
                target = Path(values.get(key, ""))
                if target.parent == directory and target.is_file():
                    target.unlink()
            path.unlink()

    def _cleanup_after_success(self, update_id: str) -> None:
        shutil.rmtree(self._work_dir(update_id), ignore_errors=True)
        shutil.rmtree(self.paths.failed, ignore_errors=True)

    def _cleanup_without_change(self, update_id: str) -> None:
        shutil.rmtree(self.paths.next, ignore_errors=True)
        shutil.rmtree(self._work_dir(update_id), ignore_errors=True)
        shutil.rmtree(self.paths.staged / update_id, ignore_errors=True)

    # ------------------------------------------------------------------ vms.env identity
    def _read_identity_env(self) -> dict:
        values = {}
        try:
            text = read_untrusted(self.paths.vms_env, max_bytes=_MAX_SMALL_FILE, expected_uid=None).decode("utf-8", "replace")
        except Failure:
            return values
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key in IDENTITY_KEYS:
                values[key] = value
        return values

    def _write_identity_env(self, values: dict) -> None:
        """Rewrites only the release identity keys; every other line
        (customer configuration and secrets) is kept, as are the file's
        owner and mode. Symlink-safe temp file + atomic rename."""
        path = self.paths.vms_env
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                lines = handle.read(_MAX_SMALL_FILE).decode("utf-8", "replace").splitlines()
        except OSError:
            lines, info = [], None
        seen = set()
        output = []
        for line in lines:
            key, sep, _ = line.partition("=")
            if sep and key in IDENTITY_KEYS:
                if key in values:
                    output.append(f"{key}={values[key]}")
                    seen.add(key)
                continue
            output.append(line)
        output.extend(f"{key}={values[key]}" for key in IDENTITY_KEYS if key in values and key not in seen)
        safe_replace(path.parent, path.name, ("\n".join(output) + "\n").encode("utf-8"),
                     mode=(info.st_mode & 0o7777) if info is not None else 0o640,
                     owner=(info.st_uid, info.st_gid) if info is not None and _posix() else None)


def _journal(payload: dict) -> None:
    """One line per state change on stderr -- the anyaicam-software-update
    unit's journal (2026-10-03). Identifiers, versions, states and error
    summaries only, like the root audit log."""
    try:
        parts = [f"update_id={payload.get('update_id')}", f"state={payload.get('state')}"]
        if payload.get("to_version") or payload.get("to_build_id"):
            parts.append(f"{payload.get('from_version') or '?'}+{str(payload.get('from_build_id') or '')[:12]}"
                         f" -> {payload.get('to_version') or '?'}+{str(payload.get('to_build_id') or '')[:12]}")
        if payload.get("final"):
            parts.append(f"final duration={payload.get('duration_seconds')}s")
        if payload.get("error"):
            parts.append(f"error={str(payload.get('error'))[:300]}")
        print("apply_release: " + " ".join(parts), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 -- logging never changes the outcome
        pass


def main() -> int:
    if not _posix() or os.geteuid() != 0:
        print("apply_release must run as root (started by the privileged watcher).", file=sys.stderr)
        return 2
    os.umask(0o022)
    result = Applier().apply_staged()
    if result is not None:
        print(json.dumps({"update_id": result.get("update_id"), "state": result.get("state")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
