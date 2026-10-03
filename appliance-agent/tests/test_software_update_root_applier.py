"""Software Update (2026-10-03): the root applier (system/apply_release.py) and
the watcher's single fixed entry point to it.

A simulated host: a fake command runner records every argv, and its
'systemctl start' boots a fake VMS that serves /version from vms.env (like
the real one, which reads ANYAICAM_VERSION/ANYAICAM_BUILD_ID from env) and
/static/release-identity.json from whatever tree is ACTUALLY at
/opt/anyaicam -- so the tests see what a real running VMS would serve.
"""
import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from software_update_helpers import (BUILD_A, BUILD_B, BUILD_C, MIGRATIONS_DESTRUCTIVE, MIGRATIONS_V1, generate_keypair,
                                     manifest_for, release_files, sign, stage_release, verify_with, write_tarball)

SYSTEM = Path(__file__).resolve().parents[1] / "system"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SYSTEM / filename)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


ar = _load("apply_release_under_test", "apply_release.py")
watcher = _load("privileged_watcher_under_test", "privileged_watcher.py")


class FakeHost:
    """Commands + the VMS they control."""

    def __init__(self, paths):
        self.paths = paths
        self.calls = []
        self.fail = {}          # argv prefix tuple -> exit code
        self.running = None     # what the VMS serves while up
        self.on_http = None

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        for prefix, code in self.fail.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return code, "simulated failure"
        if argv[:2] == ["systemctl", "stop"]:
            self.running = None
        elif argv[:2] == ["systemctl", "start"]:
            env = dict(line.split("=", 1) for line in self.paths.vms_env.read_text().splitlines() if "=" in line)
            identity_file = self.paths.live / "app" / "static" / "release-identity.json"
            tree = json.loads(identity_file.read_text()) if identity_file.is_file() else {}
            self.running = {"version": env.get("ANYAICAM_VERSION", "0.9.0"), "build_id": env.get("ANYAICAM_BUILD_ID", ""),
                            "tree": tree, "broken": (self.paths.live / "app" / "BROKEN").exists()}
        return 0, ""

    def http_get(self, url, timeout):
        if self.on_http:
            self.on_http(url)
        if not self.running:
            return 0, "connection refused"
        path = url.split("127.0.0.1:8000", 1)[1]
        if path == "/version":
            return 200, json.dumps({"product": "AnyAiCam VMS", "version": self.running["version"],
                                    "build_id": self.running["build_id"]})
        if path == "/static/release-identity.json":
            return (200, json.dumps(self.running["tree"])) if self.running["tree"] else (404, "")
        if path == "/health":
            return (500, "") if self.running["broken"] else (200, '{"status":"ok"}')
        if path == "/ready":
            return 200, '{"self_test":{"ok":true}}'
        return 404, ""


class ApplierTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = ar.Paths(staged=self.tmp / "var/lib/anyaicam/updates/staged", root_state=self.tmp / "var/lib/anyaicam-update",
                              live=self.tmp / "opt/anyaicam", trusted_key=self.tmp / "etc/anyaicam-update/trusted_signing_key.pem",
                              vms_env=self.tmp / "etc/anyaicam/vms.env", release_marker=self.tmp / "etc/anyaicam/vms_release.json",
                              lock=self.tmp / "run/anyaicam-software-update.lock", os_release=self.tmp / "os-release")
        self.private_key, public_pem = generate_keypair()
        self.paths.trusted_key.parent.mkdir(parents=True)
        self.paths.trusted_key.write_bytes(public_pem)
        self.paths.vms_env.parent.mkdir(parents=True)
        self.paths.vms_env.write_text(f"ANYAICAM_ENV=production\nANYAICAM_APP_SECRETS=keep-me\nANYAICAM_VERSION=1.1.0\n"
                                      f"ANYAICAM_BUILD_ID={BUILD_A}\nANYAICAM_VMS_COMMIT={BUILD_A}\nCUSTOMER_SETTING=kept\n")
        self.paths.release_marker.write_text(json.dumps({"release_version": "1.1.0", "vms_release_commit": BUILD_A}))
        self._install_live("1.1.0", BUILD_A)
        (self.paths.live / "mediamtx").mkdir()
        (self.paths.live / "mediamtx" / "mediamtx").write_text("binary")
        self.host = FakeHost(self.paths)
        self.host.run(["systemctl", "start", "anyaicam-vms.service"], 1)  # release 1.1.0 is running
        self.host.calls.clear()

    def _install_live(self, version, build, migrations=MIGRATIONS_V1):
        files = release_files(version, build, migrations=migrations)
        for relative, data in files.items():
            if relative.startswith("payload/vms/"):
                target = self.paths.live / relative[len("payload/vms/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

    def applier(self, **overrides):
        options = dict(paths=self.paths, run=self.host.run, http_get=self.host.http_get,
                       verify_signature=verify_with(self.paths.trusted_key.read_bytes()), platform="ubuntu",
                       architecture="x86_64", disk_usage=lambda _p: SimpleNamespace(free=10 ** 13), sleep=lambda _s: None,
                       validation_timeout_seconds=0, agent_uid=None)
        options.update(overrides)
        return ar.Applier(**options)

    def stage(self, version="1.2.0", build=BUILD_B, *, app_files=None, migrations=MIGRATIONS_V1, extra_members=(),
              manifest_overrides=None, signer=None, requested_by="owner@example.test"):
        package = write_tarball(self.tmp / f"pkg-{version}.tar.gz", release_files(version, build, app_files=app_files,
                                                                                  migrations=migrations),
                                extra_members=extra_members)
        manifest = manifest_for(package, version=version, build_id=build)
        manifest.update(manifest_overrides or {})
        signature = sign(signer or self.private_key, manifest)
        stage_release(self.paths.staged, manifest, signature, package, requested_by=requested_by)
        return manifest

    def serving(self):
        status, body = self.host.http_get("http://127.0.0.1:8000/version", 1)
        tree = self.host.http_get("http://127.0.0.1:8000/static/release-identity.json", 1)[1]
        return status, json.loads(body) if status == 200 else {}, json.loads(tree) if tree else {}

    def marker(self):
        return json.loads(self.paths.release_marker.read_text())


class SuccessTests(ApplierTestCase):
    def test_the_new_release_is_activated_and_actually_serving(self):
        manifest = self.stage()
        result = self.applier().apply_staged()

        self.assertEqual(result["state"], "healthy", result.get("error"))
        status, version, tree = self.serving()
        self.assertEqual((status, version["version"], version["build_id"], tree["build_id"]), (200, "1.2.0", BUILD_B, BUILD_B))
        # The old tree is kept, whole, as the rollback copy; the installer's binary was carried over.
        self.assertEqual(json.loads((self.paths.previous / "app/static/release-identity.json").read_text())["build_id"], BUILD_A)
        self.assertEqual((self.paths.live / "mediamtx" / "mediamtx").read_text(), "binary")
        # Release identity recorded only now, root's record and the marker.
        self.assertEqual(self.marker()["release_version"], "1.2.0")
        self.assertEqual(self.marker()["vms_release_commit"], BUILD_B)
        self.assertEqual(json.loads(self.paths.installed_record.read_text())["vms_release_commit"], BUILD_B)
        # vms.env: only identity keys changed; customer configuration and secrets kept.
        env = self.paths.vms_env.read_text()
        self.assertIn("ANYAICAM_APP_SECRETS=keep-me", env)
        self.assertIn("CUSTOMER_SETTING=kept", env)
        self.assertIn(f"ANYAICAM_BUILD_ID={BUILD_B}", env)
        # The image was built and the database backed up BEFORE the VMS was stopped.
        verbs = [" ".join(call[:2]) for call in self.host.calls]
        self.assertLess(verbs.index("docker build"), verbs.index("systemctl stop"))
        self.assertLess(verbs.index("docker exec"), verbs.index("systemctl stop"))
        # The agent's staged copy is consumed; the outcome is in root's directory.
        self.assertFalse((self.paths.staged / manifest["update_id"]).exists())
        stored = json.loads((self.paths.results / f"{manifest['update_id']}.json").read_text())
        self.assertEqual((stored["state"], stored["final"]), ("healthy", True))
        audit = [json.loads(line) for line in self.paths.audit_log.read_text().splitlines()]
        self.assertEqual(audit[-1]["requested_by"], "owner@example.test")
        self.assertEqual((audit[-1]["from_version"], audit[-1]["to_version"]), ("1.1.0", "1.2.0"))

    def test_the_marker_never_names_the_new_release_before_it_is_validated(self):
        self.stage()
        seen = []
        self.host.on_http = lambda url: seen.append(self.marker()["release_version"])
        self.applier().apply_staged()
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"1.1.0"}, "the marker changed before validation finished")
        self.assertEqual(self.marker()["release_version"], "1.2.0")

    def test_every_command_is_a_fixed_argv_with_only_validated_values(self):
        self.stage(requested_by="x$(reboot);`id` ../../etc/passwd\n")
        self.applier().apply_staged()
        allowed = re.compile(r"^(docker|systemctl|build|exec|tag|stop|start|python|-c|-t|anyaicam-vms(\.service)?|"
                             r"anyaicam-vms:(latest|release-[0-9a-f]{12}|rollback-[0-9a-f]{12})|"
                             r"partner_portal-pre-[0-9a-f]{12}-\d{8}T\d{6}Z\.db)$")
        for call in self.host.calls:
            for token in call:
                if token == str(self.paths.next) or token.startswith("import sqlite3"):
                    continue
                self.assertRegex(token, allowed, f"unexpected argv token {token!r} in {call}")
        audit = json.loads(self.paths.audit_log.read_text().splitlines()[-1])
        self.assertNotIn("\n", audit["requested_by"])


class RollbackTests(ApplierTestCase):
    def test_a_release_that_fails_validation_is_rolled_back_and_the_old_build_serves_again(self):
        self.stage("1.3.0", BUILD_C, app_files={"BROKEN": "fails /health"})
        result = self.applier().apply_staged()

        self.assertEqual(result["state"], "rolled_back")
        self.assertIn("health_check_failed", result["error"])
        status, version, tree = self.serving()
        self.assertEqual((status, version["version"], version["build_id"], tree["build_id"]), (200, "1.1.0", BUILD_A, BUILD_A))
        self.assertEqual(json.loads((self.paths.live / "app/static/release-identity.json").read_text())["build_id"], BUILD_A)
        self.assertEqual(json.loads((self.paths.failed / "app/static/release-identity.json").read_text())["build_id"], BUILD_C)
        self.assertEqual(self.marker()["release_version"], "1.1.0")
        self.assertFalse(self.paths.installed_record.exists())
        self.assertIn(f"ANYAICAM_BUILD_ID={BUILD_A}", self.paths.vms_env.read_text())
        self.assertIn(["docker", "tag", f"anyaicam-vms:rollback-{BUILD_A[:12]}", "anyaicam-vms:latest"], self.host.calls)
        self.assertIn("previous release validated", result["rollback_steps"])

    def test_a_release_whose_vms_never_starts_is_rolled_back(self):
        self.stage()
        self.host.fail = {("systemctl", "start"): 1}
        applier = self.applier()
        original = self.host.run

        def run(argv, timeout):
            if argv[:2] == ["systemctl", "start"] and (self.paths.live / "app/static/release-identity.json").read_text().find(BUILD_A) >= 0:
                self.host.fail = {}
            return original(argv, timeout)
        applier.run = run
        result = applier.apply_staged()
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(self.serving()[1]["build_id"], BUILD_A)

    def test_a_failed_rollback_is_a_hard_failure_with_recovery_information(self):
        self.stage("1.3.0", BUILD_C, app_files={"BROKEN": "x"})
        applier = self.applier()
        original = self.host.run
        starts = []

        def run(argv, timeout):
            if argv[:2] == ["systemctl", "start"]:
                starts.append(1)
                if len(starts) >= 2:
                    return 1, "unit failed"
            return original(argv, timeout)
        applier.run = run
        result = applier.apply_staged()
        self.assertEqual(result["state"], "rollback_failed")
        self.assertIn("Rollback did not complete", result["recovery"])
        self.assertIn("rollback-", result["recovery"])
        self.assertEqual(self.marker()["release_version"], "1.1.0")  # never claims the new release


class NothingChangesTests(ApplierTestCase):
    """Failures before the downtime boundary leave the running VMS alone."""

    def assert_untouched(self, result, state, code):
        self.assertEqual(result["state"], state, result.get("error"))
        self.assertTrue(result["error"].startswith(code), result["error"])
        self.assertFalse(any(call[:2] == ["systemctl", "stop"] for call in self.host.calls))
        self.assertEqual(self.serving()[1]["build_id"], BUILD_A)
        self.assertEqual(self.marker()["release_version"], "1.1.0")
        self.assertFalse(self.paths.next.exists())
        self.assertFalse(self.paths.previous.exists())

    def test_a_bad_signature(self):
        other_key, _ = generate_keypair()
        self.stage(signer=other_key)
        self.assert_untouched(self.applier().apply_staged(), "rejected", "bad_signature")
        self.assertEqual(self.host.calls, [])

    def test_a_package_changed_after_staging(self):
        manifest = self.stage()
        package = self.paths.staged / manifest["update_id"] / "package.tar.gz"
        package.write_bytes(package.read_bytes()[:-10] + b"0123456789")
        self.assert_untouched(self.applier().apply_staged(), "rejected", "bad_hash")

    def test_the_wrong_architecture(self):
        self.stage()
        self.assert_untouched(self.applier(architecture="aarch64").apply_staged(), "rejected", "wrong_architecture")

    def test_insufficient_disk(self):
        self.stage()
        self.assert_untouched(self.applier(disk_usage=lambda _p: SimpleNamespace(free=10)).apply_staged(),
                              "rejected", "insufficient_disk")

    def test_unsafe_migrations_even_when_the_manifest_says_additive(self):
        self.stage(migrations=MIGRATIONS_DESTRUCTIVE)
        self.assert_untouched(self.applier().apply_staged(), "rejected", "unsafe_migrations")

    def test_a_malicious_archive(self):
        import tarfile
        evil = tarfile.TarInfo("../../escaped.txt")
        evil.size = 4
        self.stage(extra_members=[(evil, b"evil")])
        self.assert_untouched(self.applier().apply_staged(), "rejected", "unsafe_archive")
        self.assertEqual(list(self.tmp.rglob("escaped.txt")), [])

    def test_customer_data_inside_the_application_folder(self):
        (self.paths.live / "recordings").mkdir()
        (self.paths.live / "recordings" / "camera1.mkv").write_text("footage")
        self.stage()
        self.assert_untouched(self.applier().apply_staged(), "rejected", "unexpected_live_files")
        self.assertEqual((self.paths.live / "recordings" / "camera1.mkv").read_text(), "footage")

    def test_a_downgrade_cannot_be_unlocked_by_forging_the_agent_writable_marker(self):
        ar.ensure_root_dir(self.paths.root_state, 0o755)
        self.paths.installed_record.write_text(json.dumps({"release_version": "1.3.0", "vms_release_commit": BUILD_C}))
        self.paths.release_marker.write_text(json.dumps({"release_version": "1.0.0", "vms_release_commit": BUILD_A}))
        self.stage("1.2.0", BUILD_B)
        self.assert_untouched_downgrade(self.applier().apply_staged())

    def assert_untouched_downgrade(self, result):
        self.assertEqual(result["state"], "rejected")
        self.assertTrue(result["error"].startswith("not_newer"), result["error"])

    def test_an_image_build_failure(self):
        self.stage()
        self.host.fail = {("docker", "build"): 1}
        self.assert_untouched(self.applier().apply_staged(), "install_failed", "image_build_failed")

    def test_a_staged_folder_that_does_not_match_the_signed_update_id(self):
        manifest = self.stage()
        (self.paths.staged / manifest["update_id"]).rename(self.paths.staged / "someone-else")
        self.assert_untouched(self.applier().apply_staged(), "rejected", "bad_staging")

    def test_a_missing_trust_anchor(self):
        self.stage()
        applier = self.applier()
        self.paths.trusted_key.unlink()
        self.assert_untouched(applier.apply_staged(), "rejected", "untrusted_key")


class ConcurrencyTests(ApplierTestCase):
    def test_a_second_applier_waits_out_while_one_holds_the_lock(self):
        manifest = self.stage()
        with ar._Lock(self.paths.lock):
            self.assertIsNone(self.applier().apply_staged())
        self.assertTrue((self.paths.staged / manifest["update_id"]).exists())  # untouched, still queued
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.applier().apply_staged()["state"], "healthy")

    def test_two_staged_releases_are_both_refused(self):
        self.stage("1.2.0", BUILD_B)
        self.stage("1.3.0", BUILD_C)
        self.assertIsNone(self.applier().apply_staged())
        states = {json.loads(p.read_text())["state"] for p in self.paths.results.glob("*.json")}
        self.assertEqual(states, {"rejected"})
        self.assertEqual(self.host.calls, [])


class UntrustedFileTests(ApplierTestCase):
    """Everything the agent wrote is attacker-controlled."""

    def test_a_hard_link_in_the_staging_folder_is_refused(self):
        manifest = self.stage()
        request = self.paths.staged / manifest["update_id"] / "request.json"
        victim = self.tmp / "root-only-secret.json"
        victim.write_text('{"requested_by":"secret"}')
        request.unlink()
        try:
            os.link(victim, request)
        except (OSError, NotImplementedError):
            self.skipTest("hard links unavailable here")
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "rejected")
        self.assertIn("hard link", result["error"])

    def test_a_symlink_in_the_staging_folder_is_refused(self):
        manifest = self.stage()
        request = self.paths.staged / manifest["update_id"] / "request.json"
        request.unlink()
        try:
            os.symlink(self.paths.trusted_key, request)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "rejected")
        self.assertIn("bad_staging", result["error"])

    def test_a_planted_symlink_never_redirects_a_root_write(self):
        victim = self.tmp / "etc-shadow"
        victim.write_text("root:secret")
        self.paths.release_marker.unlink()
        try:
            os.symlink(victim, self.paths.release_marker)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        self.stage()
        self.assertEqual(self.applier().apply_staged()["state"], "healthy")
        self.assertEqual(victim.read_text(), "root:secret")
        self.assertFalse(self.paths.release_marker.is_symlink())

    def test_ownership_rules_for_untrusted_files(self):
        regular = 0o100644
        cases = [
            (SimpleNamespace(st_mode=regular, st_uid=1000, st_nlink=1), 1000, ""),
            (SimpleNamespace(st_mode=regular, st_uid=0, st_nlink=1), 1000, "owned by uid 0"),
            (SimpleNamespace(st_mode=regular, st_uid=1000, st_nlink=2), 1000, "hard link"),
            (SimpleNamespace(st_mode=0o100666, st_uid=1000, st_nlink=1), 1000, "writable by group or others"),
            (SimpleNamespace(st_mode=0o020644, st_uid=1000, st_nlink=1), 1000, "not a regular file"),
        ]
        with mock.patch.object(ar, "_posix", return_value=True):
            for info, uid, expected in cases:
                with self.subTest(expected=expected):
                    problem = ar.ownership_problem(info, expected_uid=uid)
                    self.assertIn(expected, problem) if expected else self.assertEqual(problem, "")

    def test_a_trust_anchor_not_owned_by_root_is_refused(self):
        fake = SimpleNamespace(st_mode=0o100644, st_uid=1000, st_nlink=1)
        with mock.patch.object(ar, "_posix", return_value=True), mock.patch.object(ar.os, "lstat", return_value=fake):
            self.assertIn("owned by uid 1000", ar.trusted_file_problem(self.paths.trusted_key))


class WatcherTests(unittest.TestCase):
    def test_apply_release_is_one_fixed_argv_and_no_input_reaches_it(self):
        argv = watcher.DISPATCH["apply_release"]
        self.assertEqual(argv[-1], "/opt/anyaicam-agent/privileged/apply_release.py")
        self.assertIn("-I", argv)
        self.assertEqual(argv[0], "systemd-run")

    def _lstat(self, overrides):
        def lstat(path):
            return overrides.get(path, SimpleNamespace(st_mode=0o040755 if not path.endswith(".py") else 0o100700, st_uid=0))
        return lstat

    def test_the_applier_is_never_started_unless_root_owns_every_file_on_its_path(self):
        self.assertEqual(watcher.untrusted_code_reason("apply_release", self._lstat({})), "")
        for path in watcher.ROOT_OWNED_BEFORE_RUN["apply_release"]:
            for bad, words in ((SimpleNamespace(st_mode=0o100700, st_uid=1000), "not owned by root"),
                               (SimpleNamespace(st_mode=0o100722, st_uid=0), "writable"),
                               (SimpleNamespace(st_mode=0o120777, st_uid=0), "symbolic link")):
                with self.subTest(path=path, words=words):
                    self.assertIn(words, watcher.untrusted_code_reason("apply_release", self._lstat({path: bad})))

    def test_a_tampered_applier_path_means_the_marker_is_refused_and_nothing_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "apply_release.json"
            marker.write_text(json.dumps({"type": "apply_release", "command_id": "abc"}))
            lstat = self._lstat({"/opt/anyaicam-agent/privileged": SimpleNamespace(st_mode=0o040755, st_uid=1000)})
            with mock.patch.object(watcher.subprocess, "run") as run:
                self.assertIsNone(watcher.process_marker(marker, False, grace_seconds=0, sleep=lambda _s: None, lstat=lstat))
            run.assert_not_called()
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
