"""RDM-1 (Remote Device Management, revised design approved 2026-08-14):
local version install/activate/prune primitives -- the on-device
filesystem side of the update state machine (Group 6, not yet built).

Version-directory + pointer-file model (NOT symlinks -- this project's
own os.symlink() call raises WinError 1314 on Windows without elevated
privileges, discovered earlier in this project's history, so a plain
text pointer file is used everywhere instead of a symlink): each
installed version lives in its own directory, `{versions_dir}/{version}/`;
a single plain-text pointer file names which one is currently active.
Rollback is therefore nothing more than activating a version that is
already installed -- see activate()'s docstring -- never "undo the
changes a version made". There is deliberately no separate rollback()
function in this module: rollback IS activate(), called with the
previous version.

Three responsibilities, three core functions, plus two housekeeping
ones:
  * install_candidate() -- extracts a downloaded, already-verified
    package archive into a fresh version directory. Everything before
    its final atomic rename is cleanup-and-abort on failure; nothing
    durable has changed yet.
  * activate() -- THE activation boundary (revised design Sec 1): the
    one atomic pointer-file write that changes "which version is
    running". Every failure from this point onward is rollback-eligible,
    per the revised design's state machine.
  * current_version() -- reads the pointer file.
  * prune_old_versions() / cleanup_orphaned_candidates() -- disk-space
    and crash-debris housekeeping; neither touches the pointer file or
    any installed version's own contents.

No network, no AWS, no service restart -- this module only ever
extracts an already-downloaded/verified package archive (checksum and
signature verification is updater/verify.py's job, already done before
anything here is called) and writes one small pointer file.
"""

import shutil
import tarfile
import uuid
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


class InstallError(Exception):
    """install_candidate() failed to produce a valid version directory --
    wraps any tarfile/OS-level failure during extraction or the final
    rename. The failed version is guaranteed to NOT be present at
    versions_dir/version when this is raised; any partial extraction is
    cleaned up before this is raised, not left behind in staging_dir."""


class VersionAlreadyInstalled(Exception):
    """install_candidate() was asked to install a version that already
    has a complete versions_dir/version directory. A version directory,
    once installed, is immutable -- re-installing the same version is a
    caller bug, not something this module silently overwrites."""


class VersionNotInstalled(Exception):
    """activate() was asked to activate a version with no corresponding
    versions_dir/version directory -- install_candidate() must complete
    for a version before activate() can point to it."""


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or not version:
        raise ValueError("version must be a non-empty string.")
    if "/" in version or "\\" in version or version in (".", ".."):
        raise ValueError("version must not contain path separators or be '.' or '..'.")
    return version


def install_candidate(package_path: PathLike, version: str, versions_dir: PathLike, staging_dir: PathLike) -> Path:
    """Extracts the tar archive at package_path into a fresh candidate
    directory under staging_dir, then atomically renames the fully
    extracted directory into versions_dir/version.

    versions_dir/version either does not exist, or exists complete --
    there is no observable partial state there in between, the same
    write-then-rename discipline as updater/source.py's
    download_package(). The candidate directory's name includes a random
    suffix (uuid4) so concurrent/retried attempts for the same version
    never collide with each other while extracting.

    Uses tarfile's extractall(..., filter="data") (Python 3.12+ security
    filter) to reject absolute paths, path-traversal ("..") entries, and
    symlink/device-file escapes within the archive -- the same
    defense-in-depth discipline already used for other untrusted-input-
    derived filesystem operations in this codebase (e.g.
    live_relay_uploader.py's/live_cdn_signing.py's path containment).

    Raises:
        ValueError: version is empty or contains a path separator.
        VersionAlreadyInstalled: versions_dir/version already exists.
        InstallError: extraction or the final rename failed for any
            reason -- the partially-extracted candidate directory is
            removed before this is raised, so nothing accumulates in
            staging_dir from a failed attempt.

    Returns the final versions_dir/version Path on success.
    """
    version = _validate_version(version)
    versions_dir = Path(versions_dir)
    staging_dir = Path(staging_dir)
    target_dir = versions_dir / version

    if target_dir.exists():
        raise VersionAlreadyInstalled(f"version {version!r} is already installed at {target_dir}.")

    versions_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir = staging_dir / f"{version}.{uuid.uuid4().hex}"

    try:
        candidate_dir.mkdir()
        with tarfile.open(package_path, mode="r:*") as archive:
            archive.extractall(candidate_dir, filter="data")
    except Exception as error:
        shutil.rmtree(candidate_dir, ignore_errors=True)
        raise InstallError(f"failed to extract package for version {version!r}: {error}") from error

    try:
        candidate_dir.rename(target_dir)
    except OSError as error:
        shutil.rmtree(candidate_dir, ignore_errors=True)
        raise InstallError(f"failed to finalize extracted candidate for version {version!r}: {error}") from error

    return target_dir


def current_version(pointer_file: PathLike) -> str:
    """Returns the version name currently recorded in pointer_file, or
    "" if the file does not exist yet (no version has ever been
    activated). A plain read -- the pointer file is only ever mutated by
    activate() via write-then-rename, so a read here always sees either
    the previous or the newly-activated value in full, never a partial
    write."""
    pointer_file = Path(pointer_file)
    if not pointer_file.exists():
        return ""
    return pointer_file.read_text(encoding="utf-8").strip()


def activate(version: str, versions_dir: PathLike, pointer_file: PathLike) -> str:
    """THE activation boundary (revised design Sec 1): the single atomic
    write that changes "which version is running" durable state.
    Everything upstream of this call (download, verification,
    extraction) is cleanup-and-abort on failure, since nothing durable
    has changed; everything from this call onward is rollback-eligible.

    Rollback is this same function, called again with the PREVIOUS
    version (which activate() itself hands back to the caller) -- there
    is no separate rollback() function, because "roll back" and
    "activate a different already-installed version" are the same
    operation.

    Requires versions_dir/version to already exist (install_candidate()
    must have completed for it) -- raises VersionNotInstalled otherwise.
    This function itself never creates or modifies the version
    directory's contents, only the pointer file.

    Writes pointer_file via the same atomic write-then-rename discipline
    used throughout this codebase (e.g. anyaicam_agent/config.py's
    save_credential()): a temporary sibling is written in full, then
    renamed into place. The rename is the one moment "which version is
    active" changes; there is no in-between state where the pointer file
    is half-written or missing.

    Returns the PREVIOUS current version (the pointer's content before
    this call), or "" if there was no prior pointer (first-ever
    activation) -- callers need this value to know what to roll back TO
    if this activation is later deemed unhealthy.
    """
    version = _validate_version(version)
    versions_dir = Path(versions_dir)
    pointer_file = Path(pointer_file)
    target_dir = versions_dir / version

    if not target_dir.is_dir():
        raise VersionNotInstalled(
            f"version {version!r} has no installed directory at {target_dir}; call install_candidate() first."
        )

    previous_version = current_version(pointer_file)

    pointer_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer_file.with_suffix(pointer_file.suffix + ".tmp")
    temporary.write_text(version, encoding="utf-8")
    temporary.replace(pointer_file)

    return previous_version


def prune_old_versions(versions_dir: PathLike, pointer_file: PathLike, keep: int = 2) -> list:
    """Removes old, inactive version directories under versions_dir,
    keeping the currently active version (per pointer_file) plus the
    `keep` most-recently-installed OTHER version directories. The
    currently active version is never removed, even if it happens to be
    the oldest present -- an active version being pruned out from under
    itself would break rollback entirely.

    Ordering is by each version directory's own filesystem modification
    time (set when install_candidate() performed its final rename) --
    deliberately not by parsing/sorting version strings as semver, so
    this function takes no dependency on any particular version-string
    format.

    Returns the list of version names actually removed (possibly empty).
    Never raises for an empty/missing versions_dir.
    """
    versions_dir = Path(versions_dir)
    if not versions_dir.is_dir():
        return []

    active = current_version(pointer_file)
    candidates = [entry for entry in versions_dir.iterdir() if entry.is_dir() and entry.name != active]
    candidates.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)

    removed = []
    for stale in candidates[keep:]:
        shutil.rmtree(stale, ignore_errors=True)
        removed.append(stale.name)
    return removed


def cleanup_orphaned_candidates(staging_dir: PathLike) -> list:
    """Removes every leftover entry found directly under staging_dir.

    A successful install_candidate() call always renames its candidate
    directory OUT of staging_dir into versions_dir -- so by construction,
    anything still present directly under staging_dir is debris from an
    install_candidate() attempt that was interrupted (crash/restart)
    before that final rename. This function performs only the raw
    removal; deciding WHETHER a given in-progress update should be
    treated as orphaned (e.g. by cross-referencing
    updater.history.UpdateHistory.in_progress_update_ids() against what
    is actually on disk) is the future state machine's job (Group 6),
    called before this sweep runs.

    Returns the list of entry names removed. Never raises for an
    empty/missing staging_dir.
    """
    staging_dir = Path(staging_dir)
    if not staging_dir.is_dir():
        return []

    removed = []
    for entry in staging_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed.append(entry.name)
    return removed
