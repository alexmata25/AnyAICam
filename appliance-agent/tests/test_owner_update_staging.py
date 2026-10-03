"""Software Update (2026-10-03): the agent's side -- staging an owner-requested
release (updater/owner_update.py), relaying the root applier's outcome
(updater/apply_results.py), and the periodic check staying check-only."""
import base64
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anyaicam_agent.config import AgentConfig
from anyaicam_agent.service import ApplianceAgent
from anyaicam_agent.updater import installer
from anyaicam_agent.updater.apply_results import ACTIVATION_PICKUP_TIMEOUT_SECONDS, ResultRelay
from anyaicam_agent.updater.history import UpdateHistory
from anyaicam_agent.updater.models import TERMINAL_STATES, UpdateState
from anyaicam_agent.updater.owner_update import ACTION_TYPE, OwnerUpdate, installed_release_label
from anyaicam_agent.updater.source import PackageDownloadError
from anyaicam_agent.updater.verify import PackageVerifier

from software_update_helpers import BUILD_A, BUILD_B, generate_keypair, manifest_for, release_files, sign, write_tarball

POSIX = hasattr(os, "geteuid")
# Result files the test writes are root-owned only when it runs as root.
NOT_ROOT_POSIX = POSIX and os.geteuid() != 0


class FakeSource:
    def __init__(self, manifest, signature, package_bytes, fail_download=False):
        self.offer = (manifest, signature)
        self.package_bytes = package_bytes
        self.fail_download = fail_download
        self.downloads = 0

    def check_for_manifest(self, current_version, target, channel):
        return self.offer

    def download_package(self, manifest_dict, destination_path):
        self.downloads += 1
        if self.fail_download:
            raise PackageDownloadError("network down")
        Path(destination_path).write_bytes(self.package_bytes)


class StagingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        state = self.root / "var-lib" / "anyaicam"
        conf = self.root / "etc" / "anyaicam"
        state.mkdir(parents=True)
        conf.mkdir(parents=True)
        self.config = AgentConfig(state_dir=str(state), config_dir=str(conf), log_dir=str(self.root))
        self.private_key, public_pem = generate_keypair()
        self.config.trusted_public_key_file.parent.mkdir(parents=True)
        self.config.trusted_public_key_file.write_bytes(public_pem)
        self.marker = self.root / "vms_release.json"
        self.marker.write_text(json.dumps({"release_version": "1.1.0", "vms_release_commit": BUILD_A}))
        os.environ["ANYAICAM_VMS_RELEASE_MARKER"] = str(self.marker)
        self.addCleanup(os.environ.pop, "ANYAICAM_VMS_RELEASE_MARKER", None)
        self.package = write_tarball(self.root / "pkg.tar.gz", release_files("1.2.0", BUILD_B))
        self.manifest = manifest_for(self.package, version="1.2.0", build_id=BUILD_B)
        self.signature = sign(self.private_key, self.manifest)
        self.history = UpdateHistory(self.config.update_history_file)
        self.queued = []

    def owner_update(self, source=None, *, architecture="x86_64", platform="ubuntu", free=10 ** 13):
        source = source or FakeSource(self.manifest, self.signature, self.package.read_bytes())
        self.source = source
        return OwnerUpdate(self.config, history=self.history, verifier=PackageVerifier(self.config.trusted_public_key_file),
                           source=source, queue_privileged_action=lambda kind, payload: self.queued.append((kind, payload)),
                           platform=platform, architecture=architecture,
                           disk_usage=lambda _p: SimpleNamespace(free=free))

    def request(self, **overrides):
        payload = {"update_id": self.manifest["update_id"], "version": "1.2.0", "sha256": self.manifest["sha256"],
                   "confirmed": True, "requested_by": "owner@example.test"}
        payload.update(overrides)
        return payload


class StageTests(StagingTestCase):
    def test_a_confirmed_release_is_verified_staged_and_handed_to_root(self):
        result = self.owner_update().stage(self.request())

        self.assertEqual(result.state, UpdateState.ACTIVATION_REQUESTED, result.error)
        staged = self.config.update_staged_dir / self.manifest["update_id"]
        self.assertEqual(sorted(p.name for p in staged.iterdir()), ["manifest.json", "manifest.sig", "package.tar.gz", "request.json"])
        self.assertEqual(json.loads((staged / "manifest.json").read_text()), self.manifest)
        self.assertEqual(base64.b64decode((staged / "manifest.sig").read_bytes()), self.signature)
        self.assertEqual(self.queued, [(ACTION_TYPE, {"update_id": self.manifest["update_id"]})])
        # The agent never activates anything itself.
        self.assertEqual(installer.current_version(self.config.current_version_pointer_file), "")
        self.assertFalse(self.config.update_versions_dir.exists())
        self.assertEqual(self.history.get(self.manifest["update_id"])["state"], "activation_requested")

    def _refused(self, result, code):
        self.assertEqual(result.state.value in ("rejected", "download_failed", "verify_failed"), True, result.error)
        self.assertTrue(result.error.startswith(code), result.error)
        self.assertEqual(self.queued, [])
        self.assertFalse(any(self.config.update_staged_dir.glob("*/package.tar.gz")) if self.config.update_staged_dir.exists() else False)

    def test_an_unconfirmed_request_is_refused(self):
        self._refused(self.owner_update().stage(self.request(confirmed=False)), "bad_request")

    def test_the_published_release_must_be_the_one_the_owner_confirmed(self):
        self._refused(self.owner_update().stage(self.request(sha256="0" * 64)), "release_changed")

    def test_a_bad_signature_is_refused(self):
        bad = bytes([self.signature[0] ^ 1]) + self.signature[1:]
        self._refused(self.owner_update(FakeSource(self.manifest, bad, self.package.read_bytes())).stage(self.request()),
                      "bad_signature")

    def test_a_package_that_does_not_match_the_signed_hash_is_refused(self):
        self._refused(self.owner_update(FakeSource(self.manifest, self.signature, b"x" * self.manifest["package_size_bytes"]))
                      .stage(self.request()), "bad_hash")

    def test_the_wrong_cpu_architecture_or_platform_is_refused(self):
        self._refused(self.owner_update(architecture="aarch64").stage(self.request()), "wrong_architecture")
        self._refused(self.owner_update(platform="debian").stage(self.request()), "wrong_platform")

    def test_insufficient_disk_is_refused_before_downloading(self):
        update = self.owner_update(free=1024)
        self._refused(update.stage(self.request()), "insufficient_disk")
        self.assertEqual(self.source.downloads, 0)

    def test_a_downgrade_or_replay_is_refused(self):
        self.marker.write_text(json.dumps({"release_version": "1.2.0", "vms_release_commit": BUILD_B}))
        self._refused(self.owner_update().stage(self.request()), "not_newer")

    def test_a_second_request_while_one_is_in_progress_is_refused(self):
        self.assertEqual(self.owner_update().stage(self.request()).state, UpdateState.ACTIVATION_REQUESTED)
        other = self.owner_update().stage(self.request(update_id="1.2.0-other"))
        self.assertEqual(other.state, UpdateState.REJECTED)
        self.assertTrue(other.error.startswith("update_in_progress"))
        self.assertEqual(len(self.queued), 1)

    def test_a_failed_download_leaves_nothing_staged_and_nothing_requested(self):
        result = self.owner_update(FakeSource(self.manifest, self.signature, b"", fail_download=True)).stage(self.request())
        self.assertEqual(result.state, UpdateState.DOWNLOAD_FAILED)
        self.assertEqual(self.queued, [])
        self.assertFalse((self.config.update_staged_dir / self.manifest["update_id"]).exists())

    def test_the_heartbeat_reports_the_installed_release(self):
        self.assertEqual(installed_release_label(self.config), "1.1.0+" + BUILD_A[:12])


class RelayTests(StagingTestCase):
    def setUp(self):
        super().setUp()
        self.reports = []
        self.assertEqual(self.owner_update().stage(self.request()).state, UpdateState.ACTIVATION_REQUESTED)
        self.update_id = self.manifest["update_id"]

    def write_result(self, state, **extra):
        self.config.update_results_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(update_id=self.update_id, state=state, from_version="1.1.0", to_version="1.2.0", **extra)
        (self.config.update_results_dir / f"{self.update_id}.json").write_text(json.dumps(payload))

    def relay(self, now=None):
        return ResultRelay(self.config, history=UpdateHistory(self.config.update_history_file),
                           report=self.reports.append, **({"now": now} if now else {}))

    @unittest.skipIf(NOT_ROOT_POSIX, "on POSIX the relay only trusts root-owned result files")
    def test_progress_and_the_final_outcome_are_relayed_once_each(self):
        self.write_result("installing")
        self.relay().poll()
        self.relay().poll()  # a new process (agent restart) does not re-report
        self.write_result("rolled_back", error="health_check_failed: /health answered 500")
        self.relay().poll()
        self.assertEqual([r.state.value for r in self.reports], ["installing", "rolled_back"])
        self.assertEqual(self.reports[-1].rollback_from, "1.2.0")
        row = UpdateHistory(self.config.update_history_file).get(self.update_id)
        self.assertEqual(row["state"], "rolled_back")  # survives the process: durable history
        self.assertIn(UpdateState.ROLLED_BACK, TERMINAL_STATES)

    @unittest.skipIf(NOT_ROOT_POSIX, "needs root-owned result files on POSIX")
    def test_a_staging_request_the_root_side_never_picks_up_times_out(self):
        later = lambda: 10 ** 12  # noqa: E731
        self.relay(now=later).poll()
        self.assertEqual(self.reports[-1].state, UpdateState.INSTALL_FAILED)
        self.assertTrue(self.reports[-1].error.startswith("activation_not_started"))
        self.assertGreater(10 ** 12, ACTIVATION_PICKUP_TIMEOUT_SECONDS)

    def test_a_result_file_not_written_by_root_is_ignored(self):
        self.write_result("healthy")
        fake = SimpleNamespace(st_uid=1000, st_mode=0o100644)
        with mock.patch("anyaicam_agent.updater.apply_results.os.lstat", return_value=fake), \
                mock.patch("anyaicam_agent.updater.apply_results.os.geteuid", create=True, return_value=1000):
            self.relay().poll()
        self.assertEqual(self.reports, [])
        self.assertEqual(UpdateHistory(self.config.update_history_file).get(self.update_id)["state"], "activation_requested")


class PeriodicCheckNeverInstallsTests(StagingTestCase):
    """The periodic poll may discover a release; it can never stage it, queue
    the root activation, or reach the legacy pointer-flip pipeline."""

    def test_the_periodic_poll_never_stages_or_requests_activation(self):
        owner = self.owner_update()
        owner.stage = mock.Mock(side_effect=AssertionError("periodic poll must not stage"))
        machine = SimpleNamespace(
            has_unresolved_activation=lambda: False,
            check_available=lambda current_version=None: {"version": "1.2.0", "update_id": "u", "current_version": current_version},
            check_and_install=mock.Mock(side_effect=AssertionError("never")),
            process_install_update=mock.Mock(side_effect=AssertionError("never")),
        )
        agent = SimpleNamespace(state_machine=machine, owner_update=owner, update_resume_failed=False, _next_source_check_at=0.0,
                                available_update=None, config=self.config, log=logging.getLogger("test"))
        ApplianceAgent.check_for_source_update(agent)
        self.assertEqual(agent.available_update["version"], "1.2.0")
        self.assertEqual(agent.available_update["current_version"], "1.1.0")  # from the release marker
        self.assertEqual(self.queued, [])
        self.assertFalse(self.config.pending_actions_dir.exists() and any(self.config.pending_actions_dir.iterdir()))

    def test_no_periodic_or_startup_code_path_writes_the_activation_marker(self):
        source = (Path(__file__).resolve().parents[1] / "anyaicam_agent" / "service.py").read_text(encoding="utf-8")
        # Only the owner-update path (OwnerUpdate.stage via the install_update
        # command) is wired to queue apply_release.
        self.assertEqual(source.count("queue_privileged_action=self._queue_privileged_update"), 1)
        # No actual CALL (comments aside) to either legacy install entry point.
        import ast
        called = {node.func.attr for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn("check_and_install", called)
        self.assertNotIn("process_install_update", called)
        commands = (Path(__file__).resolve().parents[1] / "anyaicam_agent" / "commands.py").read_text(encoding="utf-8")
        called = {node.func.attr for node in ast.walk(ast.parse(commands))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn("process_install_update", called)
        self.assertNotIn("check_and_install", called)


if __name__ == "__main__":
    unittest.main()
