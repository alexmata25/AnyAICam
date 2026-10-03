"""Software Update (2026-10-03): the agent's part of an owner-requested
release installation.

The account owner confirms an install in Settings -> System; the cloud queues
an install_update command naming one exact release (update_id, version,
sha256). This module, running as the unprivileged agent:

  1. refuses while any other update is in progress (one at a time);
  2. asks the cloud for the currently published release and requires it to
     be exactly the one the owner confirmed;
  3. verifies the manifest signature against the pinned public key, and the
     target / platform / CPU architecture / newer-version / migration rules;
  4. checks free disk space, downloads the package, verifies its SHA-256;
  5. stages manifest + signature + package under updates/staged/<update_id>/
     and asks the root watcher for the single fixed 'apply_release' action.

It never extracts, never touches /opt/anyaicam, never moves the legacy
current_version.txt pointer, and never restarts anything. The root applier
re-verifies everything it stages (the agent's directory is not trusted).
"""

from __future__ import annotations

import base64
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import release_checks
from .models import UpdateResult, UpdateState
from .source import PackageDownloadError, SourceUnavailable
from .verify import ManifestSignatureInvalid, PackageChecksumMismatch, TrustedKeyUnavailable

ACTION_TYPE = "apply_release"


class UpdateBusy(Exception):
    """Another update is already in progress on this appliance."""


def installed_release(marker_file) -> dict:
    """{'version', 'build_id'} of the release root last stamped as running
    (installer/09-identity.sh stamp_release, or the root applier after a
    validated activation). Older markers have no release_version: their
    installer_version stands in. {} when there is no marker."""
    try:
        data = json.loads(Path(marker_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "version": str(data.get("release_version") or data.get("installer_version") or ""),
        "build_id": str(data.get("vms_release_commit") or ""),
    }


def installed_release_label(config) -> str:
    release = installed_release(config.vms_release_marker_file)
    return release_checks.release_label(release.get("version", ""), release.get("build_id", "")) or config.software_version


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _parse_request(payload) -> dict:
    if not isinstance(payload, dict):
        raise release_checks.ReleaseCheckError("bad_request", "install_update payload must be a JSON object")
    update_id = release_checks.validate_update_id(str(payload.get("update_id") or ""))
    version = str(payload.get("version") or "")
    release_checks.parse_release_version(version)
    sha256 = str(payload.get("sha256") or "").lower()
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise release_checks.ReleaseCheckError("bad_request", "install_update payload needs the release sha256")
    if payload.get("confirmed") is not True:
        raise release_checks.ReleaseCheckError("bad_request", "the owner did not confirm this installation")
    requested_by = str(payload.get("requested_by") or "")[:200]
    return {"update_id": update_id, "version": version, "sha256": sha256, "requested_by": requested_by}


class OwnerUpdate:
    def __init__(self, config, *, history, verifier, source, queue_privileged_action: Callable[[str, dict], None],
                 now: Callable[[], float] = time.time, platform: Optional[str] = None,
                 architecture: Optional[str] = None, disk_usage=shutil.disk_usage):
        self.config = config
        self.history = history
        self.verifier = verifier
        self.source = source
        self.queue_privileged_action = queue_privileged_action
        self._now = now
        self.platform = platform if platform is not None else release_checks.device_platform()
        self.architecture = architecture if architecture is not None else release_checks.device_architecture()
        self.disk_usage = disk_usage

    # -------------------------------------------------------------- helpers
    def busy(self) -> bool:
        staged = self.config.update_staged_dir
        return bool(self.history.in_progress_update_ids()) or (staged.is_dir() and any(staged.iterdir()))

    def _result(self, update_id, from_version, to_version, state, error="", started=None) -> UpdateResult:
        return UpdateResult(update_id=update_id, from_version=from_version, to_version=to_version, state=state,
                            error=error, duration_seconds=(self._now() - started) if started else 0.0)

    def _fail(self, update_id, state, error, from_version, to_version, started, staged_dir=None) -> UpdateResult:
        if staged_dir is not None:
            shutil.rmtree(staged_dir, ignore_errors=True)
        if self.history.get(update_id) is not None:
            self.history.record_transition(update_id, state, error, now=self._now())
        return self._result(update_id, from_version, to_version, state, error, started)

    # -------------------------------------------------------------- main entry
    def stage(self, payload) -> UpdateResult:
        started = self._now()
        current = installed_release(self.config.vms_release_marker_file)
        from_version = current.get("version", "")
        try:
            request = _parse_request(payload)
        except release_checks.ReleaseCheckError as error:
            return self._result(str((payload or {}).get("update_id") or "") if isinstance(payload, dict) else "",
                                from_version, "", UpdateState.REJECTED, f"{error.code}: {error}", started)
        update_id = request["update_id"]
        if self.history.is_terminal(update_id):
            row = self.history.get(update_id) or {}
            return self._result(update_id, row.get("from_version", ""), row.get("to_version", ""),
                                UpdateState(row.get("state", UpdateState.REJECTED.value)), row.get("error") or "", started)
        if self.busy():
            return self._result(update_id, from_version, request["version"], UpdateState.REJECTED,
                                "update_in_progress: another update is already in progress on this appliance", started)

        # The release must be exactly the one the owner confirmed.
        try:
            offered = self.source.check_for_manifest(from_version, self.config.update_target, self.config.update_channel)
        except SourceUnavailable as error:
            return self._result(update_id, from_version, request["version"], UpdateState.REJECTED,
                                f"source_unavailable: {error}", started)
        if offered is None:
            return self._result(update_id, from_version, request["version"], UpdateState.REJECTED,
                                "release_unavailable: the requested release is no longer published", started)
        manifest_dict, signature = offered
        try:
            manifest = self.verifier.verify_manifest(manifest_dict, signature)
        except (TrustedKeyUnavailable, ManifestSignatureInvalid, ValueError) as error:
            return self._result(update_id, from_version, request["version"], UpdateState.REJECTED,
                                f"bad_signature: {error}", started)
        if (manifest.update_id, manifest.version, manifest.sha256.lower()) != (update_id, request["version"], request["sha256"]):
            return self._result(update_id, from_version, request["version"], UpdateState.REJECTED,
                                "release_changed: the published release is not the one that was confirmed", started)
        try:
            release_checks.check_manifest_for_device(
                manifest, target=self.config.update_target, platform=self.platform,
                architecture=self.architecture, current_version=from_version)
            release_checks.check_free_space(self.config.updates_dir, release_checks.required_free_bytes(manifest.package_size_bytes),
                                            disk_usage=self.disk_usage)
        except release_checks.ReleaseCheckError as error:
            return self._result(update_id, from_version, manifest.version, UpdateState.REJECTED,
                                f"{error.code}: {error}", started)

        # Durable from here on: the attempt is authenticated and allowed.
        self.history.begin_attempt(update_id, from_version, manifest.version, now=started)
        staged_dir = self.config.update_staged_dir / update_id
        shutil.rmtree(staged_dir, ignore_errors=True)
        staged_dir.mkdir(parents=True)
        package_path = staged_dir / "package.tar.gz"
        self.history.record_transition(update_id, UpdateState.DOWNLOADING, now=self._now())
        try:
            self.source.download_package(manifest.as_dict(), package_path)
        except PackageDownloadError as error:
            return self._fail(update_id, UpdateState.DOWNLOAD_FAILED, f"download_failed: {error}",
                              from_version, manifest.version, started, staged_dir)
        self.history.record_transition(update_id, UpdateState.VERIFYING, now=self._now())
        try:
            self.verifier.verify_package(manifest, package_path)
            if package_path.stat().st_size != manifest.package_size_bytes:
                raise PackageChecksumMismatch("package size does not match the signed manifest")
        except PackageChecksumMismatch as error:
            return self._fail(update_id, UpdateState.VERIFY_FAILED, f"bad_hash: {error}",
                              from_version, manifest.version, started, staged_dir)
        self.history.record_transition(update_id, UpdateState.VERIFIED, now=self._now())

        # Stage exactly what was verified: the signed manifest bytes as
        # received, the signature, the package, and who asked.
        _atomic_write(staged_dir / "manifest.json", json.dumps(manifest_dict, sort_keys=True).encode("utf-8"))
        _atomic_write(staged_dir / "manifest.sig", base64.b64encode(signature))
        _atomic_write(staged_dir / "request.json", json.dumps({
            "update_id": update_id, "requested_by": request["requested_by"],
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8"))
        self.history.record_transition(update_id, UpdateState.STAGED, now=self._now())
        try:
            self.queue_privileged_action(ACTION_TYPE, {"update_id": update_id})
        except OSError as error:
            return self._fail(update_id, UpdateState.INSTALL_FAILED, f"activation_request_failed: {error}",
                              from_version, manifest.version, started, staged_dir)
        self.history.record_transition(update_id, UpdateState.ACTIVATION_REQUESTED, now=self._now())
        return self._result(update_id, from_version, manifest.version, UpdateState.ACTIVATION_REQUESTED, "", started)
