"""RDM-2 (device-side integration, Group 2C): focused tests for
anyaicam_agent.commands.execute()'s install_update handling -- payload
parsing, the startup/update interlock, the UpdateResult -> (status,
result, error) mapping, idempotent replay, and proof that every OTHER
command is completely unaffected by the new interlock parameters.

Everything here goes through the REAL commands.execute() entry point and
a REAL UpdateStateMachine built the same way service.py builds one
(anyaicam_agent.updater.factory.build_update_state_machine()) -- not a
mocked state machine. Only the update SOURCE is a test double
(FakeUpdateSourceProvider, already established in updater/source.py),
since a real source does not exist yet (RDM-2 Group 2G). No network, no
AWS, no real process exit -- restart_signal is always a fake that only
counts calls.
"""

import base64
import json
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

from anyaicam_agent.commands import execute
from anyaicam_agent.config import AgentConfig
from anyaicam_agent.updater import installer
from anyaicam_agent.updater.factory import build_update_state_machine
from anyaicam_agent.updater.models import PendingValidation, UpdateResult, UpdateState
from anyaicam_agent.updater.source import FakeUpdateSourceProvider
from anyaicam_agent.updater.verify import canonical_manifest_bytes, sha256_of_file

DEVICE_TARGET = "anyaicam-appliance"


class RecordingRestart:
    """Fake restart_signal -- never actually restarts anything, just
    counts calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _pem_bytes(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


class CommandsInstallUpdateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = AgentConfig(state_dir=str(self.root), config_dir=str(self.root), log_dir=str(self.root))

        self.private_key, self.public_key = _generate_keypair()
        self.config.trusted_public_key_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.trusted_public_key_file.write_bytes(_pem_bytes(self.public_key))

        self.restart = RecordingRestart()

    def sign(self, manifest_dict: dict) -> bytes:
        return self.private_key.sign(canonical_manifest_bytes(manifest_dict), padding.PKCS1v15(), hashes.SHA256())

    def make_update(self, update_id="upd-1", version="1.1.0", target=DEVICE_TARGET):
        """Builds a real tar package, a manifest whose sha256 matches it,
        and a valid signature. Returns (manifest_dict, signature_bytes,
        package_bytes)."""
        tar_path = self.root / f"{update_id}-{version}-ref.tar"
        with tarfile.open(tar_path, mode="w") as tar:
            import io
            data = version.encode()
            info = tarfile.TarInfo(name="VERSION")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        manifest_dict = {
            "update_id": update_id, "version": version, "sha256": sha256_of_file(tar_path),
            "target": target, "platform": "linux", "architecture": "x86_64", "channel": "stable",
            "issued_at": "2026-08-14T00:00:00Z", "package_size_bytes": tar_path.stat().st_size,
        }
        signature = self.sign(manifest_dict)
        return manifest_dict, signature, tar_path.read_bytes()

    def payload_for(self, manifest_dict: dict, signature: bytes) -> dict:
        return {"manifest": manifest_dict, "signature": base64.b64encode(signature).decode("ascii")}

    def make_machine(self, source=None, restart_signal=None):
        return build_update_state_machine(
            self.config,
            source=source if source is not None else FakeUpdateSourceProvider(),
            restart_signal=restart_signal if restart_signal is not None else self.restart,
        )

    def write_marker(self, machine, update_id="upd-x", from_version="1.0.0", to_version="1.1.0"):
        marker = PendingValidation(
            update_id=update_id, from_version=from_version, to_version=to_version,
            attempt="install", deadline="2099-01-01T00:00:00+00:00",
        )
        machine.pending_validation_file.parent.mkdir(parents=True, exist_ok=True)
        machine.pending_validation_file.write_text(json.dumps(marker.as_dict()), encoding="utf-8")


# -- owner-requested staging (Software Update, 2026-10-03) ---------------------
#
# install_update no longer runs the legacy pointer-flip pipeline: it hands
# the owner-confirmed release to OwnerUpdate.stage() (tested in
# test_owner_update_staging.py) and reports its outcome. These tests pin the
# command-channel contract around it.

class RecordingOwnerUpdate:
    def __init__(self, state=UpdateState.ACTIVATION_REQUESTED, error=""):
        self.state = state
        self.error = error
        self.calls = []

    def stage(self, payload):
        self.calls.append(payload)
        return UpdateResult(update_id=str((payload or {}).get("update_id") or ""), from_version="1.1.0",
                            to_version="1.2.0", state=self.state, error=self.error)


OWNER_PAYLOAD = {"update_id": "rel-1-2-0", "version": "1.2.0", "sha256": "a" * 64, "confirmed": True,
                 "requested_by": "owner@example.test"}


class OwnerStagingTests(CommandsInstallUpdateTestCase):
    def test_install_update_stages_the_owner_release_and_reports_it_as_provisional(self):
        machine = self.make_machine()
        owner = RecordingOwnerUpdate()

        status, result, error = execute("install_update", OWNER_PAYLOAD, self.config, state_machine=machine,
                                        owner_update=owner)

        self.assertEqual((status, error), ("completed", ""))
        self.assertEqual(result["state"], "activation_requested")
        self.assertFalse(result["health_confirmed"])
        self.assertEqual(owner.calls, [OWNER_PAYLOAD])
        # The legacy pointer never moves and nothing restarts from here.
        self.assertEqual(installer.current_version(self.config.current_version_pointer_file), "")
        self.assertEqual(self.restart.calls, 0)

    def test_a_rejected_release_is_reported_failed_with_its_reason(self):
        owner = RecordingOwnerUpdate(state=UpdateState.REJECTED, error="bad_signature: no")

        status, result, error = execute("install_update", OWNER_PAYLOAD, self.config, state_machine=self.make_machine(),
                                        owner_update=owner)

        self.assertEqual(status, "failed")
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(error, "bad_signature: no")

    def test_without_the_owner_updater_install_update_is_refused(self):
        status, _result, error = execute("install_update", OWNER_PAYLOAD, self.config, state_machine=self.make_machine())

        self.assertEqual(status, "failed")
        self.assertIn("not configured", error)

    def test_the_legacy_pointer_flip_pipeline_is_unreachable_from_commands(self):
        manifest_dict, signature, package_bytes = self.make_update()
        machine = self.make_machine(source=FakeUpdateSourceProvider(package_bytes=package_bytes))
        machine.process_install_update = lambda *a, **k: self.fail("process_install_update must not run")
        machine.check_and_install = lambda *a, **k: self.fail("check_and_install must not run")

        execute("install_update", self.payload_for(manifest_dict, signature), self.config, state_machine=machine,
                owner_update=RecordingOwnerUpdate(state=UpdateState.REJECTED, error="bad_request: x"))

        self.assertEqual(installer.current_version(self.config.current_version_pointer_file), "")


# -- the startup/update interlock --------------------------------------------

class InterlockTests(CommandsInstallUpdateTestCase):
    def test_blocked_after_failed_startup_resume(self):
        owner = RecordingOwnerUpdate()

        status, result, error = execute(
            "install_update", OWNER_PAYLOAD, self.config, state_machine=self.make_machine(), update_resume_failed=True,
            owner_update=owner,
        )

        self.assertEqual(status, "failed")
        self.assertIn("resume", error.lower())
        self.assertEqual(owner.calls, [])

    def test_blocked_while_marker_exists_before_process_exit(self):
        machine = self.make_machine()
        self.write_marker(machine)
        owner = RecordingOwnerUpdate()

        status, result, error = execute("install_update", OWNER_PAYLOAD, self.config, state_machine=machine,
                                        owner_update=owner)

        self.assertEqual(status, "failed")
        self.assertIn("awaiting restart", error.lower())
        self.assertEqual(owner.calls, [])

    def test_blocked_when_marker_stat_fails(self):
        machine = self.make_machine()
        blocking_file = self.root / "blocking_file"
        blocking_file.write_text("not a directory", encoding="utf-8")
        machine.pending_validation_file = blocking_file / "pending_validation.json"
        owner = RecordingOwnerUpdate()

        status, result, error = execute("install_update", OWNER_PAYLOAD, self.config, state_machine=machine,
                                        owner_update=owner)

        self.assertEqual(status, "failed")
        self.assertIn("storage error", error.lower())
        self.assertEqual(owner.calls, [])

    def test_unblocked_after_a_clean_fresh_process_resume(self):
        machine = self.make_machine()
        self.assertIsNone(machine.resume_if_pending())  # nothing pending -- the "fresh process" case
        owner = RecordingOwnerUpdate()

        status, result, error = execute(
            "install_update", OWNER_PAYLOAD, self.config, state_machine=machine, update_resume_failed=False,
            owner_update=owner,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(result["state"], "activation_requested")
        self.assertEqual(len(owner.calls), 1)


# -- non-update commands are unaffected ---------------------------------------

class NonUpdateCommandsUnaffectedTests(CommandsInstallUpdateTestCase):
    """Every one of these is called with an ACTIVE interlock condition
    (update_resume_failed=True AND an unresolved marker present) to prove
    the interlock parameters are inert for anything but install_update."""

    def _interlocked_machine(self):
        machine = self.make_machine()
        self.write_marker(machine)
        return machine

    def test_restart_service_unaffected(self):
        from threading import Event
        machine = self._interlocked_machine()
        baseline = execute("restart_service", {}, self.config, Event())
        event = Event()
        interlocked = execute(
            "restart_service", {}, self.config, event, state_machine=machine, update_resume_failed=True,
        )
        self.assertEqual(baseline[0], interlocked[0])
        self.assertEqual(baseline[2], interlocked[2])
        self.assertTrue(event.is_set())

    def test_refresh_cameras_unaffected(self):
        machine = self._interlocked_machine()
        baseline = execute("refresh_cameras", {}, self.config)
        interlocked = execute(
            "refresh_cameras", {}, self.config, state_machine=machine, update_resume_failed=True,
        )
        self.assertEqual(baseline, interlocked)

    def test_run_diagnostics_unaffected(self):
        machine = self._interlocked_machine()
        baseline_status, baseline_result, baseline_error = execute("run_diagnostics", {}, self.config)
        status, result, error = execute(
            "run_diagnostics", {}, self.config, state_machine=machine, update_resume_failed=True,
        )
        self.assertEqual(status, baseline_status)
        self.assertEqual(error, baseline_error)
        self.assertEqual(result["hostname"], baseline_result["hostname"])

    def test_relay_commands_unaffected(self):
        machine = self._interlocked_machine()
        baseline = execute("start_live_relay", {"camera_number": 1, "camera_id": "cam-1"}, self.config)
        interlocked = execute(
            "start_live_relay", {"camera_number": 1, "camera_id": "cam-1"}, self.config,
            state_machine=machine, update_resume_failed=True,
        )
        self.assertEqual(baseline, interlocked)

        baseline_stop = execute("stop_live_relay", {"camera_number": 1, "camera_id": "cam-1"}, self.config)
        interlocked_stop = execute(
            "stop_live_relay", {"camera_number": 1, "camera_id": "cam-1"}, self.config,
            state_machine=machine, update_resume_failed=True,
        )
        self.assertEqual(baseline_stop, interlocked_stop)


if __name__ == "__main__":
    unittest.main()
