"""Software Update tracing on the appliance (2026-10-03).

The first real owner-requested install (Ryzen 1.2.0 -> 1.2.1) could only be
followed from the cloud: the agent logged nothing about download,
verification or staging, and the root applier printed one final line. Every
step now leaves one line in the appliance's own journal -- identifiers,
versions, sizes and hashes only, never the presigned download URL."""

import contextlib
import importlib.util
import io
import json
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from anyaicam_agent.portal import PortalError
from anyaicam_agent.service import ApplianceAgent
from anyaicam_agent.updater.apply_results import ResultRelay
from anyaicam_agent.updater.history import UpdateHistory
from anyaicam_agent.updater.models import UpdateState

from test_owner_update_staging import NOT_ROOT_POSIX, FakeSource, StagingTestCase

SYSTEM = Path(__file__).resolve().parents[1] / "system"
LOGGER = "anyaicam.agent.update"


def _load_applier():
    spec = importlib.util.spec_from_file_location("apply_release_tracing_under_test", SYSTEM / "apply_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class OwnerUpdateTracingTests(StagingTestCase):
    def test_every_step_of_a_successful_staging_is_logged_in_order(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            result = self.owner_update().stage(self.request())
        self.assertEqual(result.state, UpdateState.ACTIVATION_REQUESTED, result.error)
        text = "\n".join(logs.output)
        steps = ["owner request received", "manifest signature verified", "release checks passed",
                 "downloading the package", "downloaded", "package verified", "staged in",
                 "activation requested from the privileged updater"]
        positions = [text.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions), text)
        self.assertIn(self.manifest["update_id"], text)
        self.assertIn(self.manifest["sha256"][:12], text)
        self.assertIn("owner@example.test", text)
        self.assertNotIn("://", text)  # no URL, presigned or otherwise

    def test_a_failed_download_is_logged_as_a_warning(self):
        source = FakeSource(self.manifest, self.signature, self.package.read_bytes(), fail_download=True)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.owner_update(source).stage(self.request())
        self.assertTrue(any(line.startswith("WARNING") and "download_failed" in line for line in logs.output), logs.output)

    def test_a_package_that_fails_verification_is_logged_as_a_warning(self):
        source = FakeSource(self.manifest, self.signature, b"x" * self.manifest["package_size_bytes"])
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.owner_update(source).stage(self.request())
        self.assertTrue(any(line.startswith("WARNING") and "verify_failed" in line for line in logs.output), logs.output)
        self.assertFalse(any("package verified" in line for line in logs.output))

    def test_a_refused_request_is_logged_with_its_reason(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.owner_update().stage(self.request(sha256="0" * 64))
        self.assertTrue(any("rejected" in line and "release_changed" in line for line in logs.output), logs.output)


class RelayTracingTests(StagingTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.owner_update().stage(self.request()).state, UpdateState.ACTIVATION_REQUESTED)
        self.update_id = self.manifest["update_id"]
        self.reports = []

    def write_result(self, state, **extra):
        self.config.update_results_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(update_id=self.update_id, state=state, from_version="1.1.0", to_version="1.2.0", **extra)
        (self.config.update_results_dir / f"{self.update_id}.json").write_text(json.dumps(payload))

    @unittest.skipIf(NOT_ROOT_POSIX, "on POSIX the relay only trusts root-owned result files")
    def test_root_progress_and_the_final_outcome_are_logged(self):
        relay = ResultRelay(self.config, history=UpdateHistory(self.config.update_history_file), report=self.reports.append)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.write_result("installing")
            relay.poll()
            self.write_result("healthy", duration_seconds=601.1)
            relay.poll()
        self.assertTrue(any("privileged updater reports installing" in line for line in logs.output), logs.output)
        self.assertTrue(any(line.startswith("INFO") and "finished as healthy (1.1.0 -> 1.2.0, 601.1 s)" in line
                            for line in logs.output), logs.output)

    @unittest.skipIf(NOT_ROOT_POSIX, "on POSIX the relay only trusts root-owned result files")
    def test_a_rollback_is_logged_as_a_warning_with_its_reason(self):
        relay = ResultRelay(self.config, history=UpdateHistory(self.config.update_history_file), report=self.reports.append)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.write_result("rolled_back", error="health_check_failed: /health answered 500")
            relay.poll()
        self.assertTrue(any(line.startswith("WARNING") and "finished as rolled_back" in line and "health_check_failed" in line
                            for line in logs.output), logs.output)


class _Queue:
    def __init__(self, items):
        self.items, self.succeeded, self.failed = items, [], []

    def ready(self):
        return list(self.items)

    def success(self, item_id):
        self.succeeded.append(item_id)

    def fail(self, item_id):
        self.failed.append(item_id)


class _Client:
    def __init__(self, answers):
        self.answers = answers

    def request(self, method, path, payload):
        status = self.answers.get(path)
        if status:
            raise PortalError(f"HTTP Error {status}", status_code=status)
        return {}


class QueuedUpdateResultTests(unittest.TestCase):
    """Regression (2026-10-03): reports queued while the cloud answered 500
    were re-sent forever once it answered 409 (a final outcome already
    recorded) -- the generic flush treated every PortalError as retryable."""

    def _flush(self, answers):
        result_path = "/api/appliance/updates/1.2.1-abc/result"
        items = [{"id": "r409", "method": "POST", "path": result_path, "payload_json": "{}"},
                 {"id": "r404", "method": "POST", "path": "/api/appliance/updates/1.2.1-def/result", "payload_json": "{}"},
                 {"id": "hb", "method": "POST", "path": "/api/appliance/heartbeat", "payload_json": "{}"}]
        agent = SimpleNamespace(queue=_Queue(items), client=_Client(answers), log=logging.getLogger("anyaicam.agent"))
        ApplianceAgent.flush(agent)
        return agent.queue

    def test_a_queued_update_result_the_cloud_will_never_accept_is_dropped(self):
        queue = self._flush({"/api/appliance/updates/1.2.1-abc/result": 409,
                             "/api/appliance/updates/1.2.1-def/result": 404,
                             "/api/appliance/heartbeat": 409})
        self.assertEqual(sorted(queue.succeeded), ["r404", "r409"])
        self.assertEqual(queue.failed, ["hb"])  # other endpoints keep their existing retry behaviour

    def test_a_queued_update_result_is_still_retried_after_a_server_error(self):
        queue = self._flush({"/api/appliance/updates/1.2.1-abc/result": 500, "/api/appliance/updates/1.2.1-def/result": 503})
        self.assertEqual(sorted(queue.failed), ["r404", "r409"])
        self.assertEqual(queue.succeeded, ["hb"])


class RootApplierJournalTests(unittest.TestCase):
    def test_each_state_change_is_one_journal_line_without_secrets(self):
        applier = _load_applier()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            applier._journal({"update_id": "1.2.1-x", "state": "installing", "from_version": "1.2.0",
                              "from_build_id": "a" * 40, "to_version": "1.2.1", "to_build_id": "b" * 40,
                              "requested_by": "owner@example.test"})
            applier._journal({"update_id": "1.2.1-x", "state": "rolled_back", "final": True, "duration_seconds": 42.0,
                              "error": "health_check_failed: /health answered 500"})
        lines = stderr.getvalue().splitlines()
        self.assertEqual(lines[0], "apply_release: update_id=1.2.1-x state=installing 1.2.0+aaaaaaaaaaaa -> 1.2.1+bbbbbbbbbbbb")
        self.assertEqual(lines[1], "apply_release: update_id=1.2.1-x state=rolled_back final duration=42.0s "
                                   "error=health_check_failed: /health answered 500")
        self.assertNotIn("owner@example.test", stderr.getvalue())

    def test_writing_a_result_also_writes_the_journal_line(self):
        applier = _load_applier()
        source = (SYSTEM / "apply_release.py").read_text(encoding="utf-8")
        method = source[source.index("    def _write_result("):source.index("    def _audit(")]
        self.assertIn("_journal(payload)", method)
        self.assertTrue(callable(applier._journal))


if __name__ == "__main__":
    unittest.main()
