"""RDM-1 (Remote Device Management, revised design approved 2026-08-14):
pure data models for the update state machine. No I/O, no imports beyond
stdlib -- matches the dependency-light module style already established in
this codebase (app/camera_mapping.py, app/live_cdn_signing.py).
"""

from dataclasses import dataclass
from enum import Enum


class UpdateState(str, Enum):
    VALIDATING_MANIFEST = "validating_manifest"
    REJECTED = "rejected"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    DOWNLOAD_FAILED = "download_failed"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    VERIFY_FAILED = "verify_failed"
    INSTALLING = "installing"
    INSTALLED = "installed"
    INSTALL_FAILED = "install_failed"
    ACTIVATING = "activating"
    ACTIVATED = "activated"
    ACTIVATION_FAILED = "activation_failed"
    RESTARTING = "restarting"
    HEALTH_CHECKING = "health_checking"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    RESTART_FAILED = "restart_failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


# Reached, attempted, or concluded without further automatic action --
# safe to treat as "this update_id is done" for idempotency purposes.
TERMINAL_STATES = {
    UpdateState.REJECTED,
    UpdateState.DOWNLOAD_FAILED,
    UpdateState.VERIFY_FAILED,
    UpdateState.INSTALL_FAILED,
    UpdateState.ACTIVATION_FAILED,
    UpdateState.HEALTHY,
    UpdateState.ROLLED_BACK,
    UpdateState.ROLLBACK_FAILED,
}

# States reached only after the activation boundary (the durable
# current-version pointer flip) has succeeded -- see the revised design's
# Sec 1. Only failures reachable from these states trigger rollback;
# everything upstream of activation is cleanup-and-abort instead.
POST_ACTIVATION_STATES = {
    UpdateState.ACTIVATED,
    UpdateState.RESTARTING,
    UpdateState.HEALTH_CHECKING,
    UpdateState.UNHEALTHY,
    UpdateState.RESTART_FAILED,
}


def _require(data: dict, key: str):
    if key not in data:
        raise ValueError(f"manifest is missing required field: {key}")
    return data[key]


@dataclass(frozen=True)
class Manifest:
    """The signed update manifest (revised design Sec 5). Binds update_id,
    version, sha256, target/platform/architecture, and channel together --
    all fields the signature authenticates. Constructed only via
    from_dict() AFTER signature verification has already succeeded
    elsewhere (see updater/verify.py) -- this class itself does no
    cryptographic checking, only structural validation."""

    update_id: str
    version: str
    sha256: str
    target: str
    platform: str
    architecture: str
    channel: str
    issued_at: str
    package_size_bytes: int

    def as_dict(self) -> dict:
        return {
            "update_id": self.update_id,
            "version": self.version,
            "sha256": self.sha256,
            "target": self.target,
            "platform": self.platform,
            "architecture": self.architecture,
            "channel": self.channel,
            "issued_at": self.issued_at,
            "package_size_bytes": self.package_size_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        if not isinstance(data, dict):
            raise ValueError("manifest must be a dict.")
        try:
            return cls(
                update_id=str(_require(data, "update_id")),
                version=str(_require(data, "version")),
                sha256=str(_require(data, "sha256")),
                target=str(_require(data, "target")),
                platform=str(_require(data, "platform")),
                architecture=str(_require(data, "architecture")),
                channel=str(_require(data, "channel")),
                issued_at=str(_require(data, "issued_at")),
                package_size_bytes=int(_require(data, "package_size_bytes")),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"malformed manifest: {error}") from error


@dataclass
class UpdateResult:
    """Returned by every UpdateStateMachine entry point -- the exact shape
    reported cloud-side via the existing generic command-result channel
    (POST /api/appliance/commands/{id})."""

    update_id: str
    from_version: str
    to_version: str
    state: UpdateState
    error: str = ""
    rollback_from: str | None = None
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "update_id": self.update_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "state": self.state.value,
            "error": self.error,
            "rollback_from": self.rollback_from,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class PendingValidation:
    """The durable restart-resume marker (revised design Sec 2). Its mere
    presence on disk is what tells a newly started agent process it needs
    to validate a just-activated version rather than starting normally."""

    update_id: str
    from_version: str
    to_version: str
    attempt: str  # "install" | "rollback"
    deadline: str  # ISO 8601, UTC

    def as_dict(self) -> dict:
        return {
            "update_id": self.update_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "attempt": self.attempt,
            "deadline": self.deadline,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingValidation":
        if not isinstance(data, dict):
            raise ValueError("pending validation marker must be a dict.")
        try:
            return cls(
                update_id=str(_require(data, "update_id")),
                from_version=str(_require(data, "from_version")),
                to_version=str(_require(data, "to_version")),
                attempt=str(_require(data, "attempt")),
                deadline=str(_require(data, "deadline")),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"malformed pending validation marker: {error}") from error
