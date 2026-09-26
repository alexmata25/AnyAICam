"""RDM-2 (device-side integration, Group 2A): constructs the single
UpdateStateMachine instance appliance-agent's service.py -- and, in a
later group (2C), commands.py -- shares.

restart_signal/health_check default to safe, clearly-labeled placeholders
here. Group 2A's own job is only the startup resume/sweep sequencing
(see service.py) -- on a freshly-provisioned device with no update ever
attempted, resume_if_pending() finds nothing to resume and never touches
either collaborator at all. The real restart_signal (Group 2B, wrapping
the exact stop_event.set() commands.py's existing restart_service handler
already uses) and the real health_check (Group 2F, hybrid local+network)
are injected by whichever caller has them once those groups land -- the
placeholders below exist only so a state machine can be constructed at
all before then, and are never a substitute for those groups' own
reviewed implementations.

source similarly defaults to a small, explicit, fail-closed placeholder
here, consistent with updater.source.get_configured_source() already
always returning None today. Group 2G replaces this with a real
UpdateSourceProvider (private-S3 + presigned GET); nothing before then
can ever actually download a package -- process_install_update() would
reach DOWNLOAD_FAILED via _UnconfiguredSource, never anything else.
"""

from typing import Callable, Optional

from .history import UpdateHistory
from .source import PackageDownloadError, SourceUnavailable, UpdateSourceProvider
from .state_machine import UpdateStateMachine
from .verify import PackageVerifier


def _unwired_restart_signal() -> None:
    """Placeholder until Group 2B wires the real restart mechanism.
    Intentionally does nothing -- see module docstring."""
    return None


class _UnconfiguredSource(UpdateSourceProvider):
    """Placeholder until Group 2G wires a real UpdateSourceProvider.
    Fails closed on both operations, exactly mirroring
    updater.source.get_configured_source()'s existing "always None"
    fail-closed stance for this phase."""

    def check_for_manifest(self, current_version: str, target: str, channel: str):
        raise SourceUnavailable("No update source is configured yet.")

    def download_package(self, manifest_dict: dict, destination_path) -> None:
        raise PackageDownloadError("No update source is configured yet.")


def build_update_state_machine(
    config,
    *,
    restart_signal: Optional[Callable[[], None]] = None,
    health_check: Optional[Callable[[], bool]] = None,
    source: Optional[UpdateSourceProvider] = None,
) -> UpdateStateMachine:
    """Constructs the one UpdateStateMachine instance for this agent
    process, from AgentConfig's already-established update paths (RDM-1
    Group 1) plus the RDM-2 update_target/update_channel fields.

    Every dependency is overridable by the caller (real collaborators in
    later groups; fakes in tests) -- nothing here hardcodes a
    collaborator a caller cannot replace. `history`/`verifier` are always
    the real classes (Groups 2/3, already reviewed) -- there is no
    "unconfigured" placeholder for those, since they are pure local
    filesystem/SQLite wrappers with no external dependency to be
    "unconfigured" about.
    """
    return UpdateStateMachine(
        history=UpdateHistory(config.update_history_file),
        verifier=PackageVerifier(config.trusted_public_key_file),
        source=source if source is not None else _UnconfiguredSource(),
        versions_dir=config.update_versions_dir,
        staging_dir=config.update_staging_dir,
        pointer_file=config.current_version_pointer_file,
        pending_validation_file=config.pending_validation_file,
        device_target=config.update_target,
        channel=config.update_channel,
        restart_signal=restart_signal if restart_signal is not None else _unwired_restart_signal,
        health_check=health_check,
    )
