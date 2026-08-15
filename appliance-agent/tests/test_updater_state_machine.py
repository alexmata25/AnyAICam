"""RDM-1: focused tests for anyaicam_agent.updater.state_machine --
UpdateStateMachine, the orchestration layer wiring verify.py/history.py/
source.py/installer.py together.

No network, no AWS, no real service restart -- restart_signal and
health_check are always injected fakes. All I/O is against a per-test
temporary directory. Where a test needs to simulate "the process
restarted", it constructs a FRESH UpdateStateMachine instance pointed at
the same on-disk paths/history file (never reusing the previous
instance's Python object state) -- this is what proves durability
through the actual files, not just in-memory continuity.
"""

import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from anyaicam_agent.updater import installer
from anyaicam_agent.updater.history import UpdateHistory
from anyaicam_agent.updater.models import PendingValidation, TERMINAL_STATES, UpdateState
from anyaicam_agent.updater.source import FakeUpdateSourceProvider, SourceUnavailable
from anyaicam_agent.updater.state_machine import DEFAULT_VALIDATION_TIMEOUT_SECONDS, UpdateStateMachine
from anyaicam_agent.updater.verify import PackageVerifier, canonical_manifest_bytes, sha256_of_file

DEVICE_TARGET = "anyaicam-appliance"


# -- shared test doubles / helpers ------------------------------------------

class FakeClock:
    """Injected as the state machine's `now` callable -- gives tests full
    control over elapsed time (deadline expiry) and stays consistent
    across multiple "restarted" UpdateStateMachine instances in one
    test, since it is passed by reference, not copied."""

    def __init__(self, start: float = 1_000_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingRestart:
    """Fake restart_signal -- never actually restarts anything, just
    counts calls and can be configured to raise."""

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def __call__(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated restart_signal failure")


class ConfigurableHealthCheck:
    """Fake health_check -- returns a configurable result, can be made to
    raise, and counts calls so tests can prove exactly when (or whether)
    it was invoked."""

    def __init__(self, result: bool = True, raise_error: bool = False):
        self.result = result
        self.raise_error = raise_error
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("simulated health_check crash")
        return self.result


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _pem_bytes(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _make_tar(path: Path, entries) -> Path:
    with tarfile.open(path, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


# -- base test case -----------------------------------------------------

class StateMachineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.versions_dir = self.root / "updates" / "versions"
        self.staging_dir = self.root / "updates" / "staging"
        self.pointer_file = self.root / "updates" / "current_version.txt"
        self.pending_file = self.root / "updates" / "pending_validation.json"
        self.history_path = self.root / "updates" / "update_history.db"
        self.trusted_key_path = self.root / "trusted_signing_key.pem"

        self.private_key, self.public_key = _generate_keypair()
        self.trusted_key_path.write_bytes(_pem_bytes(self.public_key))

        self.clock = FakeClock()
        self.restart = RecordingRestart()
        self.health = ConfigurableHealthCheck(result=True)

    def make_machine(self, **overrides) -> UpdateStateMachine:
        """Each call builds a machine against a FRESH UpdateHistory/
        PackageVerifier object bound to the same on-disk files, unless a
        collaborator is explicitly overridden -- simulating a real
        process restart's "new objects, same durable files" reality."""
        history = overrides.pop("history", None)
        if history is None:
            history = UpdateHistory(self.history_path)
        verifier = overrides.pop("verifier", None)
        if verifier is None:
            verifier = PackageVerifier(self.trusted_key_path)
        source = overrides.pop("source", None)
        if source is None:
            source = FakeUpdateSourceProvider()
        kwargs = dict(
            history=history, verifier=verifier, source=source,
            versions_dir=self.versions_dir, staging_dir=self.staging_dir,
            pointer_file=self.pointer_file, pending_validation_file=self.pending_file,
            device_target=DEVICE_TARGET, restart_signal=self.restart, health_check=self.health,
            now=self.clock,
        )
        kwargs.update(overrides)
        return UpdateStateMachine(**kwargs)

    def sign(self, manifest_dict: dict) -> bytes:
        return self.private_key.sign(canonical_manifest_bytes(manifest_dict), padding.PKCS1v15(), hashes.SHA256())

    def make_update(self, update_id="upd-1", version="1.1.0", target=DEVICE_TARGET, entries=None):
        """Builds a real tar package, a manifest whose sha256 matches it,
        and a valid signature. Returns (manifest_dict, signature,
        package_bytes)."""
        entries = entries if entries is not None else [("VERSION", version.encode())]
        tar_path = self.root / f"{update_id}-{version}-ref.tar"
        _make_tar(tar_path, entries)
        manifest_dict = {
            "update_id": update_id, "version": version, "sha256": sha256_of_file(tar_path),
            "target": target, "platform": "linux", "architecture": "x86_64", "channel": "stable",
            "issued_at": "2026-08-14T00:00:00Z", "package_size_bytes": tar_path.stat().st_size,
        }
        signature = self.sign(manifest_dict)
        return manifest_dict, signature, tar_path.read_bytes()

    def install_and_activate_directly(self, version: str, entries=None) -> None:
        """Bypasses the state machine entirely to set up "version X is
        already installed and active" on disk, using the real installer
        module -- used by tests that need pre-existing versions/pointer
        state (e.g. downgrade checks, rollback targets) without going
        through a full process_install_update() call first."""
        entries = entries if entries is not None else [("VERSION", version.encode())]
        tar_path = self.root / f"seed-{version}.tar"
        _make_tar(tar_path, entries)
        installer.install_candidate(tar_path, version, self.versions_dir, self.staging_dir)
        installer.activate(version, self.versions_dir, self.pointer_file)


# -- RDM-2 Group 2C: has_unresolved_activation() --------------------------

class HasUnresolvedActivationTests(StateMachineTestCase):
    def test_no_marker_file_is_not_unresolved(self):
        machine = self.make_machine()
        self.assertFalse(machine.has_unresolved_activation())

    def test_marker_file_present_is_unresolved(self):
        machine = self.make_machine()
        machine.pending_validation_file.parent.mkdir(parents=True, exist_ok=True)
        machine.pending_validation_file.write_text("{}", encoding="utf-8")
        self.assertTrue(machine.has_unresolved_activation())

    def test_a_real_stat_failure_other_than_missing_propagates(self):
        # A real OS-level condition (ENOTDIR), not a mock: pending_
        # validation_file is pointed at a path whose PARENT component is
        # itself a regular file, not a directory, so stat() raises
        # something other than FileNotFoundError.
        blocking_file = self.root / "blocking_file"
        blocking_file.write_text("not a directory", encoding="utf-8")
        machine = self.make_machine(pending_validation_file=blocking_file / "pending_validation.json")
        with self.assertRaises(OSError):
            machine.has_unresolved_activation()


# -- happy path -----------------------------------------------------------

class HappyPathTests(StateMachineTestCase):
    def test_install_reaches_restarting_and_signals_restart_exactly_once(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.RESTARTING)
        self.assertEqual(self.restart.calls, 1)
        self.assertEqual(installer.current_version(self.pointer_file), "1.1.0")

    def test_health_check_is_never_called_before_a_restart(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)
        self.assertEqual(self.health.calls, 0)

    def test_resume_after_restart_reaches_healthy(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)

        restarted_machine = self.make_machine()  # fresh instance == "the process restarted"
        result = restarted_machine.resume_if_pending()

        self.assertEqual(result.state, UpdateState.HEALTHY)
        self.assertEqual(self.health.calls, 1)
        self.assertFalse(self.pending_file.exists())
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))

    def test_full_transition_sequence_is_recorded_in_order(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)
        self.make_machine().resume_if_pending()

        states = [row["state"] for row in self.make_machine().history.transitions("upd-1")]
        self.assertEqual(
            states,
            [
                UpdateState.VALIDATING_MANIFEST.value, UpdateState.DOWNLOADING.value, UpdateState.DOWNLOADED.value,
                UpdateState.VERIFYING.value, UpdateState.VERIFIED.value, UpdateState.INSTALLING.value,
                UpdateState.INSTALLED.value, UpdateState.ACTIVATED.value, UpdateState.RESTARTING.value,
                UpdateState.HEALTH_CHECKING.value, UpdateState.HEALTHY.value,
            ],
        )

    def test_installed_version_contents_are_correct(self):
        manifest_dict, signature, package_bytes = self.make_update(entries=[("VERSION", b"1.1.0"), ("bin/app", b"binary")])
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)
        version_dir = self.versions_dir / "1.1.0"
        self.assertEqual((version_dir / "VERSION").read_bytes(), b"1.1.0")
        self.assertEqual((version_dir / "bin" / "app").read_bytes(), b"binary")

    def test_upgrade_from_an_existing_version_is_allowed(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.RESTARTING)
        self.assertEqual(result.from_version, "1.0.0")
        self.assertEqual(result.to_version, "1.1.0")

    def test_check_and_install_delegates_to_process_install_update(self):
        manifest_dict, signature, package_bytes = self.make_update()
        source = FakeUpdateSourceProvider(manifest=manifest_dict, signature=signature, package_bytes=package_bytes)
        machine = self.make_machine(source=source)
        result = machine.check_and_install()
        self.assertEqual(result.state, UpdateState.RESTARTING)
        self.assertEqual(source.check_calls, [("", DEVICE_TARGET, "stable")])

    def test_check_and_install_returns_none_when_nothing_available(self):
        machine = self.make_machine(source=FakeUpdateSourceProvider())
        self.assertIsNone(machine.check_and_install())

    def test_check_and_install_propagates_source_unavailable_without_touching_history(self):
        machine = self.make_machine(source=FakeUpdateSourceProvider(fail_check=True))
        with self.assertRaises(SourceUnavailable):
            machine.check_and_install()
        self.assertEqual(machine.history.in_progress_update_ids(), [])


# -- idempotency / replay --------------------------------------------------

class IdempotencyReplayTests(StateMachineTestCase):
    def test_replay_of_a_healthy_update_id_is_a_side_effect_free_noop(self):
        manifest_dict, signature, package_bytes = self.make_update()
        source = FakeUpdateSourceProvider(package_bytes=package_bytes)
        machine = self.make_machine(source=source)
        machine.process_install_update(manifest_dict, signature)
        self.make_machine().resume_if_pending()

        source.check_calls.clear()
        source.download_calls.clear()
        restart_calls_before = self.restart.calls

        replay_result = self.make_machine(source=source).process_install_update(manifest_dict, signature)

        self.assertEqual(replay_result.state, UpdateState.HEALTHY)
        self.assertEqual(source.download_calls, [])
        self.assertEqual(self.restart.calls, restart_calls_before)

    def test_replay_of_an_in_progress_update_id_resumes_with_incremented_attempt(self):
        manifest_dict, signature, package_bytes = self.make_update()
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())  # simulate a crash right after begin_attempt

        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)

        self.assertEqual(result.state, UpdateState.RESTARTING)
        row = self.make_machine().history.get("upd-1")
        self.assertEqual(row["attempt_count"], 2)

    def test_same_update_id_with_a_different_to_version_is_rejected(self):
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())  # on record for 1.1.0, still in progress

        manifest_dict, signature, package_bytes = self.make_update(version="2.0.0")  # different to_version
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)

        self.assertEqual(result.state, UpdateState.REJECTED)
        # begin_attempt()'s own ValueError path touches nothing -- the
        # original 1.1.0 attempt is untouched, still in progress.
        self.assertEqual(self.make_machine().history.get("upd-1")["to_version"], "1.1.0")


# -- rejection: signature / target / downgrade -----------------------------

class RejectionValidationTests(StateMachineTestCase):
    def test_invalid_signature_is_rejected_and_never_recorded_to_history(self):
        manifest_dict, _signature, package_bytes = self.make_update()
        tampered_signature = b"not-a-real-signature"
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, tampered_signature)

        self.assertEqual(result.state, UpdateState.REJECTED)
        self.assertIsNone(self.make_machine().history.get("upd-1"))

    def test_missing_trusted_key_fails_closed_and_is_never_recorded(self):
        manifest_dict, signature, package_bytes = self.make_update()
        missing_key_verifier = PackageVerifier(self.root / "does-not-exist.pem")
        machine = self.make_machine(
            verifier=missing_key_verifier, source=FakeUpdateSourceProvider(package_bytes=package_bytes),
        )
        result = machine.process_install_update(manifest_dict, signature)

        self.assertEqual(result.state, UpdateState.REJECTED)
        self.assertIsNone(self.make_machine().history.get("upd-1"))

    def test_target_mismatch_is_rejected_and_is_durably_recorded(self):
        manifest_dict, signature, package_bytes = self.make_update(target="some-other-device")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)

        self.assertEqual(result.state, UpdateState.REJECTED)
        self.assertIn("target mismatch", result.error)
        self.assertIsNotNone(self.make_machine().history.get("upd-1"))  # authenticated -> durably recorded

    def test_downgrade_is_rejected(self):
        self.install_and_activate_directly("2.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.0.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)

        self.assertEqual(result.state, UpdateState.REJECTED)
        self.assertIn("downgrade", result.error)

    def test_replaying_the_same_version_already_active_is_rejected(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.0.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.REJECTED)

    def test_unparsable_version_is_rejected_fail_closed(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="not-a-version")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.REJECTED)

    def test_first_ever_install_with_no_prior_version_is_allowed(self):
        # No install_and_activate_directly() -- pointer_file does not exist yet.
        manifest_dict, signature, package_bytes = self.make_update(version="0.0.1")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.RESTARTING)


# -- source / download failures --------------------------------------------

class DownloadFailureTests(StateMachineTestCase):
    def test_missing_package_at_source_is_download_failed(self):
        manifest_dict, signature, _package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(missing_package=True))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.DOWNLOAD_FAILED)
        self.assertEqual(self.restart.calls, 0)

    def test_source_transfer_failure_is_download_failed(self):
        manifest_dict, signature, _package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(fail_download=True))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.DOWNLOAD_FAILED)

    def test_interrupted_download_is_download_failed_and_leaves_no_package_file(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(
            source=FakeUpdateSourceProvider(package_bytes=package_bytes, interrupt_after_bytes=3),
        )
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.DOWNLOAD_FAILED)
        self.assertFalse((self.staging_dir / "upd-1.pkg").exists())

    def test_download_failed_is_terminal_and_blocks_replay(self):
        manifest_dict, signature, _package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(fail_download=True))
        machine.process_install_update(manifest_dict, signature)
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))


class ChecksumAndInstallFailureTests(StateMachineTestCase):
    def test_corrupted_download_fails_checksum_verification(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes, corrupt=True))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.VERIFY_FAILED)

    def test_verify_failed_cleans_up_the_downloaded_package_file(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes, corrupt=True))
        machine.process_install_update(manifest_dict, signature)
        self.assertFalse((self.staging_dir / "upd-1.pkg").exists())

    def test_install_extraction_failure_is_install_failed(self):
        garbage = b"this is not a valid tar archive at all"
        manifest_dict = {
            "update_id": "upd-1", "version": "1.1.0", "sha256": hashlib.sha256(garbage).hexdigest(),
            "target": DEVICE_TARGET, "platform": "linux", "architecture": "x86_64", "channel": "stable",
            "issued_at": "2026-08-14T00:00:00Z", "package_size_bytes": len(garbage),
        }
        signature = self.sign(manifest_dict)
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=garbage))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.INSTALL_FAILED)
        self.assertFalse((self.versions_dir / "1.1.0").exists())

    def test_already_installed_version_from_a_prior_crashed_attempt_is_idempotent(self):
        manifest_dict, signature, package_bytes = self.make_update()
        # Simulate: a previous attempt already extracted the version
        # (install_candidate() succeeded) before crashing -- WITHOUT
        # ever activating it.
        tar_path = self.root / "pre-extracted.tar"
        _make_tar(tar_path, [("VERSION", b"1.1.0")])
        installer.install_candidate(tar_path, "1.1.0", self.versions_dir, self.staging_dir)

        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.RESTARTING)  # not INSTALL_FAILED


class ActivationFailureTests(StateMachineTestCase):
    def test_activation_failure_when_version_directory_is_missing(self):
        # Exercises _activate_and_restart()'s documented VersionNotInstalled
        # contract directly -- the only way to reach this branch is a
        # version directory disappearing between INSTALLING succeeding
        # and ACTIVATING running (e.g. concurrent external interference),
        # which cannot be triggered through the public pipeline alone.
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "9.9.9", now=self.clock())
        machine = self.make_machine(history=history)

        ok, error = machine._activate_and_restart(
            "upd-1", "9.9.9", marker_from="", marker_to="9.9.9",
            attempt="install", success_state=UpdateState.ACTIVATED,
        )
        self.assertFalse(ok)
        self.assertIn("9.9.9", error)
        self.assertFalse(self.pending_file.exists())
        self.assertEqual(self.restart.calls, 0)


# -- crash/restart: death before the marker is ever written -----------------

class DeathBeforeMarkerTests(StateMachineTestCase):
    def test_death_at_validating_manifest_leaves_nothing_to_resume(self):
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())  # crash immediately after this
        machine = self.make_machine(history=history)
        self.assertIsNone(machine.resume_if_pending())

    def test_death_at_installed_leaves_nothing_to_resume(self):
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.DOWNLOADING, now=self.clock())
        history.record_transition("upd-1", UpdateState.DOWNLOADED, now=self.clock())
        history.record_transition("upd-1", UpdateState.VERIFYING, now=self.clock())
        history.record_transition("upd-1", UpdateState.VERIFIED, now=self.clock())
        history.record_transition("upd-1", UpdateState.INSTALLING, now=self.clock())
        history.record_transition("upd-1", UpdateState.INSTALLED, now=self.clock())  # crash right here, no marker yet
        machine = self.make_machine(history=history)

        self.assertIsNone(machine.resume_if_pending())
        self.assertEqual(installer.current_version(self.pointer_file), "")  # pointer never touched

    def test_sweep_reports_pre_activation_stuck_update_id_without_abandoning_it(self):
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.DOWNLOADING, now=self.clock())
        machine = self.make_machine(history=history)

        report = machine.sweep_orphaned_state()
        self.assertEqual(report["pre_activation_in_progress_update_ids"], ["upd-1"])
        self.assertFalse(self.make_machine().history.is_terminal("upd-1"))  # not abandoned, still resumable

    def test_sweep_removes_leftover_staging_debris(self):
        self.staging_dir.mkdir(parents=True)
        orphan = self.staging_dir / "1.1.0.deadbeef"
        orphan.mkdir()
        (orphan / "partial").write_bytes(b"debris")
        machine = self.make_machine()
        report = machine.sweep_orphaned_state()
        self.assertEqual(report["removed_staging_entries"], ["1.1.0.deadbeef"])
        self.assertFalse(orphan.exists())

    def test_sweep_excludes_awaiting_restart_confirmation_update_ids(self):
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.ACTIVATED, now=self.clock())
        machine = self.make_machine(history=history)
        report = machine.sweep_orphaned_state()
        self.assertEqual(report["pre_activation_in_progress_update_ids"], [])


# -- crash/restart: marker written but its own activate() never completed --

class DeathAfterMarkerBeforeActivateTests(StateMachineTestCase):
    def _write_marker(self, attempt: str, from_version: str, to_version: str) -> None:
        marker = PendingValidation(
            update_id="upd-1", from_version=from_version, to_version=to_version,
            attempt=attempt, deadline="2099-01-01T00:00:00+00:00",
        )
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text(json.dumps(marker.as_dict()), encoding="utf-8")

    def test_install_direction_never_completing_activate_is_activation_failed(self):
        # History's from/to describe the update's original direction
        # (empty -> 1.1.0); the marker mirrors the same direction here
        # since this is the FIRST ("install") activation attempt.
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.INSTALLED, now=self.clock())
        self._write_marker("install", from_version="", to_version="1.1.0")
        # Pointer file left untouched -- activate() never actually ran.

        machine = self.make_machine(history=history)
        result = machine.resume_if_pending()

        self.assertEqual(result.state, UpdateState.ACTIVATION_FAILED)
        self.assertFalse(self.pending_file.exists())
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))
        self.assertEqual(self.health.calls, 0)  # never even attempted a health check

    def test_rollback_direction_never_completing_activate_is_rollback_failed(self):
        # Original update was 1.0.0 -> 1.1.0; it activated, was found
        # unhealthy, and a rollback marker (reversed direction: FROM the
        # bad 1.1.0 back TO the good 1.0.0) was written -- but the
        # rollback's own activate() call never completed, so the pointer
        # still shows the bad version.
        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.UNHEALTHY, now=self.clock())
        self._write_marker("rollback", from_version="1.1.0", to_version="1.0.0")
        self.pointer_file.parent.mkdir(parents=True, exist_ok=True)
        self.pointer_file.write_text("1.1.0", encoding="utf-8")  # rollback's own flip never completed

        machine = self.make_machine(history=history)
        result = machine.resume_if_pending()

        self.assertEqual(result.state, UpdateState.ROLLBACK_FAILED)
        self.assertFalse(self.pending_file.exists())
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))
        self.assertEqual(self.health.calls, 0)


# -- crash/restart: activation completed, history rows incomplete ----------

class ActivationCompletedHistoryGapTests(StateMachineTestCase):
    def test_pointer_already_flipped_but_activated_row_never_written_still_resolves(self):
        # Simulates dying between activate() succeeding and the ACTIVATED/
        # RESTARTING history rows being written -- proving the decision
        # is made from the pointer file, not from history's state name
        # (see module docstring).
        tar_path = self.root / "seed.tar"
        _make_tar(tar_path, [("VERSION", b"1.1.0")])
        installer.install_candidate(tar_path, "1.1.0", self.versions_dir, self.staging_dir)
        installer.activate("1.1.0", self.versions_dir, self.pointer_file)  # pointer now shows 1.1.0

        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.INSTALLED, now=self.clock())  # last row is still INSTALLED

        marker = PendingValidation(update_id="upd-1", from_version="", to_version="1.1.0",
                                    attempt="install", deadline="2099-01-01T00:00:00+00:00")
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text(json.dumps(marker.as_dict()), encoding="utf-8")

        machine = self.make_machine(history=history)
        result = machine.resume_if_pending()

        self.assertEqual(result.state, UpdateState.HEALTHY)
        self.assertEqual(self.health.calls, 1)


# -- health-check outcomes and rollback triggering --------------------------

class HealthCheckOutcomeTests(StateMachineTestCase):
    def _install_and_reach_restarting(self, health_result=True):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)
        self.health.result = health_result

    def test_healthy_reaches_healthy_and_deletes_marker(self):
        self._install_and_reach_restarting(health_result=True)
        result = self.make_machine().resume_if_pending()
        self.assertEqual(result.state, UpdateState.HEALTHY)
        self.assertFalse(self.pending_file.exists())

    def test_unhealthy_triggers_rollback_flips_pointer_back_and_restarts_again(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)
        self.assertEqual(self.restart.calls, 1)

        self.health.result = False
        result = self.make_machine().resume_if_pending()

        self.assertEqual(installer.current_version(self.pointer_file), "1.0.0")  # pointer flipped back
        self.assertEqual(self.restart.calls, 2)  # a second restart was signaled for the rollback
        self.assertTrue(self.pending_file.exists())
        marker = PendingValidation.from_dict(json.loads(self.pending_file.read_text()))
        self.assertEqual(marker.attempt, "rollback")
        self.assertEqual(marker.to_version, "1.0.0")
        self.assertEqual(result.state, UpdateState.RESTARTING)

    def test_health_check_none_is_treated_as_unhealthy(self):
        # This is also the first-ever install on this device (no prior
        # version exists) -- so "unhealthy" here hits the "nowhere to
        # roll back to" guard and concludes ROLLBACK_FAILED directly,
        # exercising that specific safety guard.
        self._install_and_reach_restarting()
        result = self.make_machine(health_check=None).resume_if_pending()
        self.assertEqual(result.state, UpdateState.ROLLBACK_FAILED)
        self.assertIn("first-ever activation", result.error)
        self.assertEqual(installer.current_version(self.pointer_file), "1.1.0")  # never touched -- nowhere to go

    def test_health_check_raising_is_treated_as_unhealthy(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)

        crashing_health = ConfigurableHealthCheck(raise_error=True)
        result = self.make_machine(health_check=crashing_health).resume_if_pending()

        self.assertEqual(installer.current_version(self.pointer_file), "1.0.0")
        # The UNHEALTHY transition's own detail carries the health_check()
        # exception text -- checked via the append-only audit trail, not
        # the summary row's `error` column, since that column reflects
        # only the MOST RECENT transition's detail (ROLLING_BACK/
        # RESTARTING follow UNHEALTHY and were recorded with no detail
        # of their own, per history.py's own documented contract).
        transitions = self.make_machine().history.transitions("upd-1")
        unhealthy_rows = [row for row in transitions if row["state"] == UpdateState.UNHEALTHY.value]
        self.assertEqual(len(unhealthy_rows), 1)
        self.assertIn("health_check() raised", unhealthy_rows[0]["detail"])

    def test_marker_deadline_expiry_triggers_rollback_without_calling_health_check(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)

        self.clock.advance(DEFAULT_VALIDATION_TIMEOUT_SECONDS + 10)
        self.health.result = True  # would report healthy, but must never even be asked
        result = self.make_machine().resume_if_pending()

        self.assertEqual(self.health.calls, 0)
        self.assertEqual(installer.current_version(self.pointer_file), "1.0.0")  # rolled back anyway


# -- rollback-to-conclusion and loop prevention ------------------------

class RollbackToConclusionTests(StateMachineTestCase):
    def _install_upgrade_and_trigger_rollback(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)  # 1.1.0 activated, restart #1

        self.health.result = False
        self.make_machine().resume_if_pending()  # unhealthy -> rollback triggered, restart #2
        self.assertEqual(self.restart.calls, 2)
        self.assertEqual(installer.current_version(self.pointer_file), "1.0.0")

    def test_rollback_validation_success_reaches_rolled_back(self):
        self._install_upgrade_and_trigger_rollback()

        self.health.result = True  # the good, old version validates fine
        result = self.make_machine().resume_if_pending()

        self.assertEqual(result.state, UpdateState.ROLLED_BACK)
        self.assertFalse(self.pending_file.exists())
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))

    def test_rollback_validation_failure_reaches_rollback_failed_never_retries(self):
        self._install_upgrade_and_trigger_rollback()

        self.health.result = False  # even the "good" version now fails validation
        result = self.make_machine().resume_if_pending()

        self.assertEqual(result.state, UpdateState.ROLLBACK_FAILED)
        self.assertFalse(self.pending_file.exists())
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))
        # No infinite loop: exactly 2 restarts total (forward + one
        # rollback attempt), never a third.
        self.assertEqual(self.restart.calls, 2)

    def test_rollback_failed_is_quiescent_on_further_resume_calls(self):
        self._install_upgrade_and_trigger_rollback()
        self.health.result = False
        self.make_machine().resume_if_pending()  # concludes ROLLBACK_FAILED

        # A third startup, with nothing left to resume -- must be a
        # clean no-op, not another rollback attempt.
        third_result = self.make_machine().resume_if_pending()
        self.assertIsNone(third_result)
        self.assertEqual(self.restart.calls, 2)
        self.assertEqual(installer.current_version(self.pointer_file), "1.0.0")

    def test_replay_of_the_original_update_id_after_rollback_failed_is_rejected(self):
        self._install_upgrade_and_trigger_rollback()
        self.health.result = False
        self.make_machine().resume_if_pending()

        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")  # same update_id/version again
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        result = machine.process_install_update(manifest_dict, signature)
        self.assertEqual(result.state, UpdateState.ROLLBACK_FAILED)  # cached terminal result, not reprocessed
        self.assertEqual(self.restart.calls, 2)  # no new activation attempt

    def test_no_prior_version_to_roll_back_to_is_rollback_failed_without_a_restart_attempt(self):
        # First-ever install (no prior version at all) goes unhealthy --
        # explicitly exercises the "nowhere to roll back to" guard,
        # distinct from "the good version's directory is missing".
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)
        self.assertEqual(self.restart.calls, 1)

        self.health.result = False
        result = self.make_machine().resume_if_pending()

        self.assertEqual(result.state, UpdateState.ROLLBACK_FAILED)
        self.assertIn("first-ever activation", result.error)
        self.assertEqual(self.restart.calls, 1)  # no second restart was ever attempted
        self.assertEqual(installer.current_version(self.pointer_file), "1.1.0")  # left exactly where it was
        # Regression (bugfix): this branch returns without ever calling
        # _activate_and_restart(), so it must delete the marker itself --
        # every other way of reaching ROLLBACK_FAILED already does.
        self.assertFalse(self.pending_file.exists())

    def test_no_prior_version_rollback_failed_does_not_repeat_on_a_later_fresh_restart(self):
        # Regression (bugfix): before the fix, the marker was left behind
        # by the "no prior version" branch, so a LATER fresh-process
        # resume_if_pending() call would re-discover it, re-run
        # health_check(), and append duplicate UNHEALTHY/ROLLBACK_FAILED
        # transitions forever. With the marker correctly deleted, a later
        # restart must find nothing to resume at all.
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)

        self.health.result = False
        first_result = self.make_machine().resume_if_pending()
        self.assertEqual(first_result.state, UpdateState.ROLLBACK_FAILED)

        transitions_after_first = self.make_machine().history.transitions("upd-1")
        health_calls_after_first = self.health.calls
        restarts_after_first = self.restart.calls

        # A later, genuinely fresh restart -- must be a clean no-op.
        second_result = self.make_machine().resume_if_pending()

        self.assertIsNone(second_result)
        self.assertEqual(self.health.calls, health_calls_after_first)  # health_check() never re-invoked
        self.assertEqual(self.restart.calls, restarts_after_first)  # no new restart attempted
        self.assertEqual(
            self.make_machine().history.transitions("upd-1"), transitions_after_first,
        )  # no duplicate transitions appended
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))

    def test_good_version_directory_missing_at_rollback_time_is_rollback_failed(self):
        self.install_and_activate_directly("1.0.0")
        manifest_dict, signature, package_bytes = self.make_update(version="1.1.0")
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update(manifest_dict, signature)

        shutil.rmtree(self.versions_dir / "1.0.0")  # e.g. externally pruned in between
        self.health.result = False
        result = self.make_machine().resume_if_pending()

        self.assertEqual(result.state, UpdateState.ROLLBACK_FAILED)
        self.assertFalse(self.pending_file.exists())
        self.assertEqual(self.restart.calls, 1)  # rollback's own restart never happened
        # No further automatic attempt on a subsequent startup either.
        self.assertIsNone(self.make_machine().resume_if_pending())


# -- malformed / corrupt marker quarantine and reconciliation ---------------

class MarkerCorruptionTests(StateMachineTestCase):
    def test_invalid_json_marker_is_quarantined_not_deleted(self):
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text("{not valid json", encoding="utf-8")

        machine = self.make_machine()
        result = machine.resume_if_pending()

        self.assertIsNone(result)
        self.assertFalse(self.pending_file.exists())
        quarantined = list(self.pending_file.parent.glob(self.pending_file.name + ".corrupt.*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{not valid json")

    def test_marker_missing_required_field_is_quarantined(self):
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text(json.dumps({"update_id": "upd-1"}), encoding="utf-8")

        result = self.make_machine().resume_if_pending()

        self.assertIsNone(result)
        self.assertFalse(self.pending_file.exists())
        self.assertEqual(len(list(self.pending_file.parent.glob(self.pending_file.name + ".corrupt.*"))), 1)

    def test_corrupt_marker_with_no_in_progress_history_is_a_clean_noop(self):
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text("garbage", encoding="utf-8")
        history = UpdateHistory(self.history_path)  # empty -- nothing in progress at all
        machine = self.make_machine(history=history)

        self.assertIsNone(machine.resume_if_pending())
        self.assertEqual(self.health.calls, 0)

    def test_corrupt_marker_with_forward_post_activation_row_reconciles_via_health_check(self):
        # Pointer already shows the NEW version (activation completed);
        # history's last row is ACTIVATED; the marker is corrupt. Must
        # reconstruct and genuinely re-run health_check(), not assume an
        # outcome.
        tar_path = self.root / "seed.tar"
        _make_tar(tar_path, [("VERSION", b"1.1.0")])
        installer.install_candidate(tar_path, "1.1.0", self.versions_dir, self.staging_dir)
        installer.activate("1.1.0", self.versions_dir, self.pointer_file)

        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.ACTIVATED, now=self.clock())

        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text("not json", encoding="utf-8")

        self.health.result = True
        result = self.make_machine(history=history).resume_if_pending()

        self.assertEqual(result.state, UpdateState.HEALTHY)
        self.assertEqual(self.health.calls, 1)

    def test_corrupt_marker_with_rollback_direction_row_reconciles_via_health_check(self):
        # Pointer already shows the ORIGINAL (good) version -- a
        # rollback's own re-activation must have already completed --
        # while history's from/to still describe the ORIGINAL forward
        # direction (1.0.0 -> 1.1.0) and its last row is UNHEALTHY.
        self.install_and_activate_directly("1.0.0")  # pointer currently 1.0.0 -- the rollback's target

        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.UNHEALTHY, now=self.clock())

        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text("not json", encoding="utf-8")

        self.health.result = True  # the good, original version validates fine
        result = self.make_machine(history=history).resume_if_pending()

        self.assertEqual(result.state, UpdateState.ROLLED_BACK)
        self.assertEqual(self.health.calls, 1)

    def test_corrupt_marker_with_pointer_matching_neither_version_fails_closed(self):
        self.pointer_file.parent.mkdir(parents=True, exist_ok=True)
        self.pointer_file.write_text("9.9.9-unexpected", encoding="utf-8")  # matches neither recorded version

        history = UpdateHistory(self.history_path)
        history.begin_attempt("upd-1", "1.0.0", "1.1.0", now=self.clock())
        history.record_transition("upd-1", UpdateState.ACTIVATED, now=self.clock())

        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text("not json", encoding="utf-8")

        result = self.make_machine(history=history).resume_if_pending()

        self.assertEqual(result.state, UpdateState.ROLLBACK_FAILED)
        self.assertEqual(self.health.calls, 0)  # never even attempted -- genuinely inconsistent state
        self.assertTrue(self.make_machine().history.is_terminal("upd-1"))


if __name__ == "__main__":
    unittest.main()
