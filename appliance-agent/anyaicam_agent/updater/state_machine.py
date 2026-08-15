"""RDM-1 (Remote Device Management, revised design approved 2026-08-14):
the update orchestration layer -- wires updater/verify.py,
updater/history.py, updater/source.py, and updater/installer.py together
into the actual install/activate/validate/rollback state machine.

THE ACTIVATION BOUNDARY (revised design Sec 1) is installer.activate()'s
atomic pointer-file write. Every failure before it is cleanup-and-abort
(nothing durable changed, so there is nothing to roll back FROM); every
failure from it onward is rollback-eligible. This module's entire
crash-safety design follows from one rule, applied consistently:

    A pending_validation.json marker is always written to disk BEFORE
    installer.activate() is called for ANY pointer flip (the first
    "install" activation, or the later "rollback" re-activation) -- never
    after. If the process dies between the marker write and activate()
    completing, resume_if_pending() compares the marker's own claimed
    to_version against the ACTUAL current-version pointer file (never a
    separately-tracked "did it complete" boolean, which could itself
    desync from reality across a crash) to tell, unambiguously, whether
    that specific pointer flip actually happened.

Combined with "every terminal conclusion deletes the marker" (see
_conclude_success()/_conclude_rollback_failed()/_abandon_pending_marker())
and "is_terminal() permanently blocks begin_attempt() for a concluded
update_id" (updater/history.py, Group 3), this closes every crash timing
without needing special-case logic per timing window:

  * Died before the marker was ever written (anywhere from
    VALIDATING_MANIFEST through INSTALLED): nothing durable changed.
    resume_if_pending() finds no marker and no history row awaiting
    restart confirmation -- there is nothing to resume. The staged
    package/candidate files this may have left behind are swept by
    sweep_orphaned_state() (via installer.cleanup_orphaned_candidates());
    the history row itself is left non-terminal, naturally eligible to
    resume via begin_attempt()'s own resume-on-redelivery behaviour if
    the same update_id is ever delivered again.
  * Died after the marker was written but before/during activate(): the
    marker exists, but current_version() still shows the OLD version --
    _abandon_pending_marker() concludes ACTIVATION_FAILED (install
    direction) or ROLLBACK_FAILED (rollback direction), deletes the
    marker, and stops. No rollback is attempted (nothing to roll back
    FROM).
  * Died after activate() completed but before the ACTIVATED/RESTARTING
    history rows were written, or before restart_signal() was ever
    called, or between restart_signal() and the process actually
    restarting: the marker survives all of this untouched, and
    current_version() already shows the NEW version -- resume_if_pending()
    (whenever the process is next started, by ANY means: this module's
    own restart_signal callback, an external supervisor noticing the
    process is gone, or a manual restart) finds the marker, sees the
    pointer already matches marker.to_version, and proceeds straight to
    health-check validation. The ACTIVATED/RESTARTING rows being
    missing/incomplete in history never matters, because this decision
    is made from the pointer file, not from history's state name.
  * The marker itself goes missing or is corrupted after activation: it
    is quarantined (renamed aside, never deleted outright, for
    forensics) and durable history is cross-checked instead --
    _find_awaiting_restart_confirmation_row() looks for any update_id
    whose last recorded state is one this module only ever reaches AFTER
    a pointer flip has already durably completed. If found,
    _reconcile_orphaned_post_activation_row() compares the ACTUAL
    pointer against that row's own from_version/to_version (which always
    describe the update's ORIGINAL direction, never flipped by a
    rollback) to work out which direction was in flight, reconstructs an
    equivalent marker in memory, and re-runs the SAME health_check()
    validation path used when a marker is present -- it never assumes an
    outcome just because the paper trail was damaged.

HOW ROLLBACK_FAILED AVOIDS INFINITE RESTART LOOPS: a rollback attempt's
OWN validation failure (marker.attempt == "rollback" reaching
_validate_after_restart() and failing) NEVER calls _begin_rollback()
again -- it goes straight to _conclude_rollback_failed(), a genuinely
TERMINAL state (in models.TERMINAL_STATES). The same is true if the
rollback's own activate() call itself fails (the "good" version's
directory has gone missing, e.g. pruned), or if there is no prior version
to roll back to at all (the failed update was the very first version
ever activated on this device). Reaching ROLLBACK_FAILED always
deletes the marker and (via history.is_terminal()) permanently blocks any
future begin_attempt() for that update_id. There is structurally no path
in this module that ever triggers a SECOND automatic pointer flip once a
rollback's own re-activation has been attempted once -- rollback is tried
exactly once per update_id, ever.

No network, no AWS, no REAL service restart: restart_signal is an
injected callable (a no-op fake in tests; wired to the real
commands.py/restart_service primitive only in a future group). Likewise
health_check is an injected optional callable -- this module builds no
real (network-touching) implementation of it, only the extension point,
mirroring updater/source.py's get_configured_source()/updater/
verify.py's fail-closed-when-absent style: health_check=None is treated
as an immediate "unhealthy" result, never as "assume healthy".
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Union

from . import installer
from .history import ABANDONED, AlreadyTerminal, UnknownUpdateId, UpdateHistory
from .models import POST_ACTIVATION_STATES, Manifest, PendingValidation, UpdateResult, UpdateState
from .source import PackageDownloadError, UpdateSourceProvider
from .verify import ManifestSignatureInvalid, PackageChecksumMismatch, PackageVerifier, TrustedKeyUnavailable

PathLike = Union[str, Path]

# How long a device is given, after a restart, to have resume_if_pending()
# actually run and confirm health before a pending marker is treated as
# timed out (same failure handling as an explicit health_check()==False).
# Tunable per-instance via the constructor; this is only the default.
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 300.0

# States this module only ever reaches AFTER a pointer flip has already
# durably completed (installer.activate() returned successfully) --
# POST_ACTIVATION_STATES (models.py, the "install" direction) plus
# ROLLING_BACK, this module's own equivalent marker for the "rollback"
# direction (not part of models.POST_ACTIVATION_STATES, since that set is
# owned by Group 1/models.py and out of scope to modify here). Used only
# by the marker-lost recovery path to decide whether a history row needs
# reconciling at all.
_AWAITING_RESTART_CONFIRMATION_STATES = POST_ACTIVATION_STATES | {UpdateState.ROLLING_BACK}


def _parse_version(version: str) -> tuple:
    """Parses a dotted-integer version string ("1.2.0" -> (1, 2, 0)) for
    ordering comparisons only. Raises ValueError for anything else --
    this device does not guess at ordering for a format it does not
    understand."""
    if not isinstance(version, str) or not version:
        raise ValueError("version must be a non-empty string.")
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as error:
        raise ValueError(f"version {version!r} is not a dotted-integer version string.") from error


class UpdateStateMachine:
    """Orchestrates one device's update lifecycle end-to-end. Holds no
    network/AWS clients of its own -- only the already-constructed
    collaborators (history, verifier, source) and plain filesystem
    paths, exactly mirroring the dependency-injection style already
    established across updater/verify.py and updater/source.py.
    """

    def __init__(
        self,
        *,
        history: UpdateHistory,
        verifier: PackageVerifier,
        source: UpdateSourceProvider,
        versions_dir: PathLike,
        staging_dir: PathLike,
        pointer_file: PathLike,
        pending_validation_file: PathLike,
        device_target: str,
        restart_signal: Callable[[], None],
        channel: str = "stable",
        health_check: Optional[Callable[[], bool]] = None,
        validation_timeout_seconds: float = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        self.history = history
        self.verifier = verifier
        self.source = source
        self.versions_dir = Path(versions_dir)
        self.staging_dir = Path(staging_dir)
        self.pointer_file = Path(pointer_file)
        self.pending_validation_file = Path(pending_validation_file)
        self.device_target = device_target
        self.channel = channel
        self.restart_signal = restart_signal
        self.health_check = health_check
        self.validation_timeout_seconds = validation_timeout_seconds
        self._now = now

    # -- public entry points ----------------------------------------------

    def check_and_install(self, current_version: Optional[str] = None):
        """Queries self.source for the latest available manifest for this
        device's target/channel, and if one is available, feeds it
        straight into process_install_update(). current_version defaults
        to this device's own installer.current_version() when not
        supplied.

        Returns None if the source reports nothing available (a normal,
        expected outcome, not an error). Propagates SourceUnavailable
        uncaught if the source could not be reached/queried at all --
        this happens before any update_id is known, so (like a manifest-
        signature failure) nothing is durably recorded in history for
        it; the caller decides how to report/log a source-level outage.
        """
        version_to_check = current_version if current_version is not None else self._current_version()
        available = self.source.check_for_manifest(version_to_check, self.device_target, self.channel)
        if available is None:
            return None
        manifest_dict, signature = available
        return self.process_install_update(manifest_dict, signature)

    def process_install_update(self, manifest_dict: dict, signature: bytes) -> UpdateResult:
        """Processes exactly one signed update manifest+signature pair --
        this is what a future commands.py wiring calls directly with an
        install_update command's own payload (Group 7, not this group).

        See the module docstring for the full crash-safety design. In
        outline: authenticate the manifest (no durable record for this
        step -- see below) -> begin durable tracking -> target/downgrade
        checks -> download -> checksum verify -> install (extract) ->
        THE activation boundary -> signal restart. Health-check
        validation of a freshly activated version never happens inside
        this method -- it can only meaningfully run after an actual
        restart, in resume_if_pending().

        Idempotency/replay: if manifest_dict's own (as yet unauthenticated)
        update_id already has a TERMINAL outcome on record, this returns
        that cached result immediately without touching the verifier,
        source, or installer at all -- a duplicate delivery of an
        already-concluded update_id is a free, side-effect-free no-op.
        Using the unauthenticated update_id purely as a dedup lookup key
        (never as an authorization decision) is safe: at worst, a forged
        manifest claiming a known-terminal update_id is simply ignored,
        which is the safe default, not a vulnerability.

        History records ONLY authenticated update attempts: a manifest
        that fails signature verification (or is missing/malformed) gets
        an in-memory REJECTED result with no durable row at all -- an
        update_id/version this device cannot trust is not a real
        "attempt" worth persisting.
        """
        started_at = self._now()
        raw_update_id = manifest_dict.get("update_id") if isinstance(manifest_dict, dict) else None

        if isinstance(raw_update_id, str) and raw_update_id and self.history.is_terminal(raw_update_id):
            return self._result_from_history(raw_update_id)

        try:
            manifest = self.verifier.verify_manifest(manifest_dict, signature)
        except (TrustedKeyUnavailable, ManifestSignatureInvalid, ValueError) as error:
            return UpdateResult(
                update_id=raw_update_id or "",
                from_version=self._current_version(),
                to_version="",
                state=UpdateState.REJECTED,
                error=f"manifest verification failed: {error}",
                duration_seconds=self._now() - started_at,
            )

        # manifest.update_id / manifest.version are authenticated from
        # here on -- durable tracking begins now.
        from_version = self._current_version()
        to_version = manifest.version

        try:
            self.history.begin_attempt(manifest.update_id, from_version, to_version, now=started_at)
        except AlreadyTerminal:
            return self._result_from_history(manifest.update_id)
        except ValueError as error:
            # update_id reused for a different from/to pairing than what
            # is already on record for a still-in-progress attempt --
            # reject outright; begin_attempt() touches nothing durable
            # on this path (see history.py), so there is nothing to
            # record here either.
            return UpdateResult(
                update_id=manifest.update_id, from_version=from_version, to_version=to_version,
                state=UpdateState.REJECTED, error=str(error), duration_seconds=self._now() - started_at,
            )

        return self._run_pipeline(manifest, from_version, to_version)

    def resume_if_pending(self) -> Optional[UpdateResult]:
        """Called once at agent startup, BEFORE any command processing
        (per the revised design's restart-resume protocol) -- the
        expected call order at startup is resume_if_pending() first,
        then sweep_orphaned_state(). See the module docstring for the
        full crash-timing analysis this implements.

        Returns None if there is nothing to resume (the common case).
        Returns the concluding UpdateResult (HEALTHY, ROLLED_BACK,
        ROLLBACK_FAILED, ACTIVATION_FAILED, or a RESTARTING/ROLLING_BACK
        in-progress result if a rollback was just triggered and is
        awaiting its OWN next restart) otherwise.
        """
        marker, _was_corrupt = self._read_pending_validation_marker()

        if marker is not None:
            actual = self._current_version()
            if actual != marker.to_version:
                return self._abandon_pending_marker(marker, "activation never completed before restart/crash")
            return self._validate_after_restart(marker)

        orphaned_row = self._find_awaiting_restart_confirmation_row()
        if orphaned_row is None:
            return None
        return self._reconcile_orphaned_post_activation_row(orphaned_row)

    def sweep_orphaned_state(self) -> dict:
        """Startup housekeeping for everything BEFORE the activation
        boundary -- distinct from resume_if_pending(), which owns
        everything from the activation boundary onward.

        Always removes any leftover entries directly under staging_dir
        (installer.cleanup_orphaned_candidates() -- safe by construction:
        a successful download/install always moves its files OUT of
        staging_dir, so anything still there is debris from an
        interrupted attempt; this also incidentally sweeps any
        downloaded-but-not-yet-installed package file left by a crash
        between DOWNLOADED and INSTALLED, since this module stores those
        directly under staging_dir too).

        Does NOT mark any pre-activation in-progress update_id abandoned:
        this method has no access to the original manifest/signature
        bytes needed to resume processing on its own, and deciding a
        staleness policy for how long a stuck pre-activation attempt
        should be tolerated is a policy decision for a future caller, not
        this sweep. Such an update_id simply remains non-terminal,
        naturally eligible for begin_attempt()'s own resume-on-
        redelivery behaviour if the same update_id is ever delivered
        again.

        Returns {"removed_staging_entries": [...],
        "pre_activation_in_progress_update_ids": [...]}.
        """
        removed = installer.cleanup_orphaned_candidates(self.staging_dir)
        pre_activation = []
        for update_id in self.history.in_progress_update_ids():
            row = self.history.get(update_id)
            if row is not None and UpdateState(row["state"]) not in _AWAITING_RESTART_CONFIRMATION_STATES:
                pre_activation.append(update_id)
        return {"removed_staging_entries": removed, "pre_activation_in_progress_update_ids": pre_activation}

    def has_unresolved_activation(self) -> bool:
        """RDM-2 (device-side integration, Group 2C): True iff a
        pending_validation marker currently exists on disk -- i.e. this
        device has activated (or attempted to roll back to) a version
        whose health has not yet been confirmed by a subsequent restart's
        resume_if_pending() call. While this is True, commands.py's
        install_update interlock blocks any NEW install_update: letting a
        second pointer flip stack on top of unresolved post-activation
        state would break every crash-timing guarantee resume_if_pending()
        relies on (see the module docstring) -- it assumes at most one
        marker is ever pending at a time.

        Uses an explicit stat() call rather than Path.exists():
        FileNotFoundError unambiguously means "no marker is pending", but
        ANY OTHER OSError (permission denied, an I/O error, a parent
        directory replaced by a file, etc.) means this device cannot
        currently determine whether a marker exists at all -- and an
        interlock whose own existence-check is unreliable must fail
        closed (propagate the error so the caller blocks new updates)
        rather than silently behave as if nothing were pending.
        """
        try:
            self.pending_validation_file.stat()
            return True
        except FileNotFoundError:
            return False

    # -- the main pipeline (pre-activation) --------------------------------

    def _run_pipeline(self, manifest: Manifest, from_version: str, to_version: str) -> UpdateResult:
        if not self.verifier.target_matches(manifest, self.device_target):
            self.history.record_transition(
                manifest.update_id, UpdateState.REJECTED,
                f"target mismatch: manifest target {manifest.target!r} != device target {self.device_target!r}",
                now=self._now(),
            )
            return self._result_from_history(manifest.update_id)

        if not self._is_upgrade(from_version, to_version):
            self.history.record_transition(
                manifest.update_id, UpdateState.REJECTED,
                f"downgrade or replay rejected: {to_version!r} is not newer than currently active {from_version!r}",
                now=self._now(),
            )
            return self._result_from_history(manifest.update_id)

        package_path = self._candidate_package_path(manifest.update_id)

        self.history.record_transition(manifest.update_id, UpdateState.DOWNLOADING, now=self._now())
        try:
            self.source.download_package(manifest.as_dict(), package_path)
        except PackageDownloadError as error:  # PackageNotFound is a subclass; caught here too
            self.history.record_transition(manifest.update_id, UpdateState.DOWNLOAD_FAILED, str(error), now=self._now())
            return self._result_from_history(manifest.update_id)
        self.history.record_transition(manifest.update_id, UpdateState.DOWNLOADED, now=self._now())

        self.history.record_transition(manifest.update_id, UpdateState.VERIFYING, now=self._now())
        try:
            self.verifier.verify_package(manifest, package_path)
        except PackageChecksumMismatch as error:
            self._cleanup_package_file(package_path)
            self.history.record_transition(manifest.update_id, UpdateState.VERIFY_FAILED, str(error), now=self._now())
            return self._result_from_history(manifest.update_id)
        self.history.record_transition(manifest.update_id, UpdateState.VERIFIED, now=self._now())

        self.history.record_transition(manifest.update_id, UpdateState.INSTALLING, now=self._now())
        try:
            installer.install_candidate(package_path, manifest.version, self.versions_dir, self.staging_dir)
        except installer.VersionAlreadyInstalled:
            pass  # idempotent resume: a prior (crashed) attempt already extracted this version.
        except installer.InstallError as error:
            self._cleanup_package_file(package_path)
            self.history.record_transition(manifest.update_id, UpdateState.INSTALL_FAILED, str(error), now=self._now())
            return self._result_from_history(manifest.update_id)
        self._cleanup_package_file(package_path)
        self.history.record_transition(manifest.update_id, UpdateState.INSTALLED, now=self._now())

        # THE activation boundary. _activate_and_restart() writes the
        # pending_validation marker BEFORE calling installer.activate()
        # -- see the module docstring for why this ordering is what
        # makes everything from here on crash-safe.
        ok, error = self._activate_and_restart(
            manifest.update_id, manifest.version, marker_from=from_version, marker_to=to_version,
            attempt="install", success_state=UpdateState.ACTIVATED,
        )
        if not ok:
            self.history.record_transition(manifest.update_id, UpdateState.ACTIVATION_FAILED, error, now=self._now())

        return self._result_from_history(manifest.update_id)

    # -- post-activation: validation and rollback --------------------------

    def _validate_after_restart(self, marker: PendingValidation) -> UpdateResult:
        """Runs (or times out) health-check validation for a pointer
        flip that is already durably confirmed to have completed
        (current_version() == marker.to_version, checked by the caller).
        Shared by the marker-present path (resume_if_pending()) and the
        marker-lost reconstruction path
        (_reconcile_orphaned_post_activation_row()) -- both ultimately
        boil down to "here is a marker (real or reconstructed), evaluate
        it", which is exactly what this method does.
        """
        if self._is_marker_expired(marker):
            healthy, detail = False, f"validation deadline {marker.deadline} passed before health_check ran"
        else:
            self.history.record_transition(marker.update_id, UpdateState.HEALTH_CHECKING, now=self._now())
            try:
                healthy = bool(self.health_check()) if self.health_check is not None else False
                detail = "" if healthy else "health_check() reported unhealthy"
            except Exception as error:  # noqa: BLE001 -- any health_check failure means "not healthy", never propagate
                healthy, detail = False, f"health_check() raised: {error}"

        if healthy:
            return self._conclude_success(marker)
        if marker.attempt == "rollback":
            # See module docstring: a rollback's OWN validation failing
            # is always terminal, never triggers a second rollback.
            return self._conclude_rollback_failed(marker, detail)
        return self._begin_rollback(
            marker.update_id, bad_version=marker.to_version, good_version=marker.from_version, reason=detail,
        )

    def _begin_rollback(self, update_id: str, bad_version: str, good_version: str, reason: str) -> UpdateResult:
        """Triggers rollback: re-activates good_version (the previous,
        presumably-working version) and signals a restart so this device
        can validate it too -- rollback is not a different mechanism,
        only a different direction for the same activate()+marker+
        restart sequence. Only ever reached for an update_id already
        past its FIRST activation boundary.
        """
        self.history.record_transition(update_id, UpdateState.UNHEALTHY, reason, now=self._now())

        if good_version == "":
            # This was the very first version ever activated on this
            # device -- there is no prior version to roll back TO at
            # all. installer.activate("") would itself raise ValueError
            # (an empty version is never valid), which is not the same
            # failure as "the version directory is missing" and must be
            # checked separately, before ever calling
            # _activate_and_restart(). Genuinely terminal, same as the
            # good version's directory having gone missing: never
            # attempt a further automatic pointer flip from here.
            self.history.record_transition(
                update_id, UpdateState.ROLLBACK_FAILED,
                "no prior version to roll back to -- this was the first-ever activation on this device",
                now=self._now(),
            )
            self._delete_marker()  # bugfix: this branch returns without ever
            # calling _activate_and_restart() (there is nowhere to activate),
            # so nothing else on this path would otherwise delete the
            # now-stale marker -- every OTHER way of reaching ROLLBACK_FAILED
            # already does (via _conclude_rollback_failed() or
            # _activate_and_restart()'s own VersionNotInstalled handler).
            # Leaving it in place would make every subsequent restart
            # re-discover the same stale marker and needlessly re-run
            # health_check() + re-append duplicate UNHEALTHY/ROLLBACK_FAILED
            # transitions forever, contradicting this module's own stated
            # invariant that reaching ROLLBACK_FAILED always deletes the
            # marker.
            return self._result_from_history(update_id)

        ok, error = self._activate_and_restart(
            update_id, good_version, marker_from=bad_version, marker_to=good_version,
            attempt="rollback", success_state=UpdateState.ROLLING_BACK,
        )
        if not ok:
            # good_version's own directory is missing (e.g. pruned) --
            # nowhere left to roll back TO. Genuinely terminal: never
            # attempt a further automatic pointer flip from here.
            self.history.record_transition(update_id, UpdateState.ROLLBACK_FAILED, error, now=self._now())

        return self._result_from_history(update_id)

    def _activate_and_restart(
        self, update_id: str, activate_version: str, *, marker_from: str, marker_to: str,
        attempt: str, success_state: UpdateState,
    ):
        """Writes the pending_validation marker, THEN calls
        installer.activate(activate_version, ...), THEN (only if that
        succeeds) records success_state, records RESTARTING, and invokes
        restart_signal(). This exact ordering -- marker before activate()
        -- is the crash-safety mechanism described in the module
        docstring.

        A restart_signal() failure is recorded as RESTART_FAILED but does
        NOT unwind anything already durably written: the marker survives
        regardless, and resume_if_pending() will reconcile correctly
        whenever the process is next restarted, by any means (RESTART_
        FAILED is deliberately absent from models.TERMINAL_STATES for
        exactly this reason).

        Returns (True, "") if activate() succeeded (whether or not
        restart_signal() itself later failed), or (False, error_message)
        if activate() itself raised VersionNotInstalled -- in which case
        the marker just written is removed again, since there is nothing
        to validate.
        """
        deadline_epoch = self._now() + self.validation_timeout_seconds
        marker = PendingValidation(
            update_id=update_id, from_version=marker_from, to_version=marker_to,
            attempt=attempt, deadline=self._epoch_to_iso(deadline_epoch),
        )
        self._write_marker(marker)

        try:
            installer.activate(activate_version, self.versions_dir, self.pointer_file)
        except installer.VersionNotInstalled as error:
            self._delete_marker()
            return False, str(error)

        self.history.record_transition(update_id, success_state, now=self._now())
        self.history.record_transition(update_id, UpdateState.RESTARTING, now=self._now())
        try:
            self.restart_signal()
        except Exception as error:  # noqa: BLE001 -- never let a bad restart callback corrupt durable state
            self.history.record_transition(update_id, UpdateState.RESTART_FAILED, str(error), now=self._now())
        return True, ""

    def _conclude_success(self, marker: PendingValidation) -> UpdateResult:
        final_state = UpdateState.HEALTHY if marker.attempt == "install" else UpdateState.ROLLED_BACK
        self.history.record_transition(marker.update_id, final_state, now=self._now())
        self._delete_marker()
        return self._result_from_history(marker.update_id)

    def _conclude_rollback_failed(self, marker: PendingValidation, detail: str) -> UpdateResult:
        self.history.record_transition(marker.update_id, UpdateState.ROLLBACK_FAILED, detail, now=self._now())
        self._delete_marker()  # terminal -- never retried automatically again
        return self._result_from_history(marker.update_id)

    def _abandon_pending_marker(self, marker: PendingValidation, reason: str) -> UpdateResult:
        """The marker existed, but ITS OWN activate() call never actually
        completed (the pointer still shows the version we came FROM, not
        marker.to_version) -- nothing durable changed for "which version
        is running", so this is a cleanup-only conclusion, never a
        rollback (there is nothing to roll back FROM)."""
        target_state = UpdateState.ACTIVATION_FAILED if marker.attempt == "install" else UpdateState.ROLLBACK_FAILED
        try:
            self.history.record_transition(marker.update_id, target_state, reason, now=self._now())
        except UnknownUpdateId:
            pass  # defensive only -- begin_attempt() always precedes any marker write in normal operation
        self._delete_marker()
        return self._result_from_history(marker.update_id)

    def _reconcile_orphaned_post_activation_row(self, row: dict) -> UpdateResult:
        """Called when the pending_validation marker is missing/corrupt
        (already quarantined by the time this runs) but durable history
        shows an update_id already past its FIRST activation boundary
        with no terminal conclusion yet. Reconstructs an equivalent
        marker purely from durable artifacts -- history's own
        from_version/to_version (which always describe the update's
        ORIGINAL direction, never flipped by a rollback) plus the ACTUAL
        current_version() pointer, which tells us which direction was
        actually in flight -- and re-runs the SAME health_check()
        validation path a present marker would have used. It never
        assumes an outcome just because the paper trail was damaged.
        """
        update_id = row["update_id"]
        original_from, original_to = row["from_version"], row["to_version"]
        actual = self._current_version()
        future_deadline = self._epoch_to_iso(self._now() + self.validation_timeout_seconds)

        if actual == original_to:
            synthetic = PendingValidation(
                update_id=update_id, from_version=original_from, to_version=original_to,
                attempt="install", deadline=future_deadline,
            )
            return self._validate_after_restart(synthetic)

        if actual == original_from:
            synthetic = PendingValidation(
                update_id=update_id, from_version=original_to, to_version=original_from,
                attempt="rollback", deadline=future_deadline,
            )
            return self._validate_after_restart(synthetic)

        # The pointer matches NEITHER version this update_id is on record
        # for -- an unexplained/inconsistent state. Fail closed: never
        # attempt a further automatic pointer flip; require operator/
        # cloud attention.
        self.history.record_transition(
            update_id, UpdateState.ROLLBACK_FAILED,
            f"pointer file content {actual!r} matches neither recorded version "
            f"({original_from!r} or {original_to!r}); marker missing or corrupt -- "
            "automatic recovery stopped.",
            now=self._now(),
        )
        return self._result_from_history(update_id)

    def _find_awaiting_restart_confirmation_row(self) -> Optional[dict]:
        for update_id in self.history.in_progress_update_ids():
            row = self.history.get(update_id)
            if row is not None and UpdateState(row["state"]) in _AWAITING_RESTART_CONFIRMATION_STATES:
                return row
        return None

    # -- pending_validation marker: read/write/delete/quarantine ----------

    def _write_marker(self, marker: PendingValidation) -> None:
        """Atomic write-then-rename -- the same discipline used
        throughout this codebase (e.g. anyaicam_agent/config.py's
        save_credential(), updater/installer.py's activate())."""
        self.pending_validation_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.pending_validation_file.with_suffix(self.pending_validation_file.suffix + ".tmp")
        temporary.write_text(json.dumps(marker.as_dict()), encoding="utf-8")
        temporary.replace(self.pending_validation_file)

    def _delete_marker(self) -> None:
        self.pending_validation_file.unlink(missing_ok=True)

    def _read_pending_validation_marker(self):
        """Returns (marker, was_corrupt). marker is None if no file
        exists, OR the file was corrupt -- in which case was_corrupt is
        True and the file has already been quarantined (renamed aside,
        never deleted outright, so it remains available for forensics)
        by the time this returns."""
        if not self.pending_validation_file.exists():
            return None, False
        try:
            raw = json.loads(self.pending_validation_file.read_text(encoding="utf-8"))
            marker = PendingValidation.from_dict(raw)
        except (OSError, ValueError) as error:
            self._quarantine_marker_file(str(error))
            return None, True
        return marker, False

    def _quarantine_marker_file(self, reason: str = "") -> None:
        quarantine_path = self.pending_validation_file.with_name(
            self.pending_validation_file.name + f".corrupt.{int(self._now())}"
        )
        try:
            self.pending_validation_file.replace(quarantine_path)
        except OSError:
            pass  # best-effort -- resume_if_pending() still treats this as "no marker" either way

    # -- small helpers ------------------------------------------------------

    def _current_version(self) -> str:
        return installer.current_version(self.pointer_file)

    def _is_upgrade(self, from_version: str, to_version: str) -> bool:
        """True iff to_version is strictly newer than from_version (the
        downgrade/replay protection check). An empty from_version (no
        version has ever been activated on this device yet) always
        counts as an upgrade. A version string that does not parse as a
        dotted-integer tuple is treated as NOT an upgrade -- this device
        refuses rather than guesses at an ordering it cannot verify."""
        if from_version == "":
            return True
        try:
            return _parse_version(to_version) > _parse_version(from_version)
        except ValueError:
            return False

    def _candidate_package_path(self, update_id: str) -> Path:
        return self.staging_dir / f"{update_id}.pkg"

    def _cleanup_package_file(self, package_path: Path) -> None:
        package_path.unlink(missing_ok=True)

    def _epoch_to_iso(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    def _iso_to_epoch(self, iso: str) -> float:
        return datetime.fromisoformat(iso).timestamp()

    def _is_marker_expired(self, marker: PendingValidation) -> bool:
        try:
            deadline_epoch = self._iso_to_epoch(marker.deadline)
        except ValueError:
            return True  # unparsable deadline -- fail closed, treat as expired
        return self._now() >= deadline_epoch

    def _result_from_history(self, update_id: str) -> UpdateResult:
        row = self.history.get(update_id)
        if row is None:
            # Defensive only -- every caller of this helper has just
            # written or already confirmed a row exists for update_id.
            return UpdateResult(update_id=update_id, from_version="", to_version="", state=UpdateState.REJECTED)
        try:
            state = UpdateState(row["state"])
        except ValueError:
            # row["state"] == history.ABANDONED -- a bookkeeping
            # conclusion reached by a future recovery sweep, not a real
            # UpdateState. Reported as REJECTED: from a caller's
            # perspective, an abandoned update_id will not be
            # (re)processed -- the same externally visible outcome as an
            # outright rejection.
            state = UpdateState.REJECTED
        return UpdateResult(
            update_id=update_id, from_version=row["from_version"], to_version=row["to_version"],
            state=state, error=row["error"],
            duration_seconds=max(0.0, row["updated_at"] - row["created_at"]),
        )
