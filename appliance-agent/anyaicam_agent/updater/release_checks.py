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
