"""Linux 1.2.5: manual-install downgrade protection (03-release-guard.sh).

Runs the real guard in bash against temporary release records:
  * newer and same-build installs proceed; clean machines proceed;
  * an older release, another build of the same release, or an unreadable
    or untrusted record is refused before anything changes;
  * --allow-downgrade is the explicit override, and the refusal names the
    controlled rollback command;
  * install.sh calls the guard before any step that changes the machine;
  * the builder requires --release-version and stamps it in the manifest.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
OLD = "a" * 40
NEW = "b" * 40


@unittest.skipUnless(BASH, "bash is required")
class ReleaseDowngradeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root_record = self.tmp / "update-state" / "installed_release.json"
        self.marker = self.tmp / "etc" / "vms_release.json"
        self.root_record.parent.mkdir()
        self.marker.parent.mkdir()
        # python3 for the guard, whatever this host calls it.
        shim = self.tmp / "bin"
        shim.mkdir()
        (shim / "python3").write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n', newline="\n")
        (shim / "python3").chmod(0o755)
        self.shim = shim

    def _bash_path(self, path):
        return subprocess.run([BASH, "-c", 'cygpath -u "$1" 2>/dev/null || printf %s "$1"', "_", str(path)],
                              capture_output=True, text=True).stdout.strip()

    def record(self, path, version, commit):
        path.write_text(json.dumps({"release_version": version, "vms_release_commit": commit}), encoding="utf-8")

    def guard(self, version, commit, allow=0):
        script = f'''
set -euo pipefail
export PATH="{self._bash_path(self.shim)}:$PATH"
log() {{ printf 'LOG %s\\n' "$*"; }}
VMS_RELEASE_MARKER="{self._bash_path(self.marker)}"
RELEASE_GUARD_ROOT_RECORD="{self._bash_path(self.root_record)}"
RELEASE_GUARD_ROOT_UID="$(id -u)"
RELEASE_VERSION="{version}"
VMS_RELEASE_COMMIT="{commit}"
source ./03-release-guard.sh
release_downgrade_guard {allow}
'''
        return subprocess.run([BASH, "-c", script], cwd=ROOT, capture_output=True, text=True)

    def test_a_clean_machine_installs(self):
        result = self.guard("1.2.5", NEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no recorded release version", result.stdout)

    def test_an_upgrade_installs(self):
        self.record(self.root_record, "1.2.4", OLD)
        result = self.guard("1.2.5", NEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upgrading 1.2.4", result.stdout)

    def test_the_same_build_reinstalls(self):
        self.record(self.root_record, "1.2.5", NEW)
        self.assertEqual(self.guard("1.2.5", NEW).returncode, 0)

    def test_an_older_release_is_refused_and_names_the_rollback(self):
        self.record(self.root_record, "1.2.5", NEW)
        result = self.guard("1.2.4", OLD)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OLDER release (1.2.4)", result.stderr)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertIn("sudo anyaicam-rollback", result.stderr)

    def test_version_comparison_is_numeric(self):
        self.record(self.root_record, "1.2.10", NEW)
        self.assertNotEqual(self.guard("1.2.9", OLD).returncode, 0)
        self.assertEqual(self.guard("1.3", OLD).returncode, 0)
        self.assertNotEqual(self.guard("1.2.10.0", OLD).returncode, 0)   # same version, other build

    def test_another_build_of_the_same_release_is_refused(self):
        self.record(self.root_record, "1.2.5", NEW)
        result = self.guard("1.2.5", OLD)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different build of 1.2.5", result.stderr)

    def test_allow_downgrade_is_the_explicit_override(self):
        self.record(self.root_record, "1.2.5", NEW)
        result = self.guard("1.2.4", OLD, allow=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: --allow-downgrade", result.stdout)

    def test_an_unversioned_installer_cannot_replace_a_versioned_release(self):
        self.record(self.root_record, "1.2.4", OLD)
        self.assertNotEqual(self.guard("", NEW).returncode, 0)

    def test_roots_record_wins_over_the_agent_writable_marker(self):
        self.record(self.root_record, "1.2.5", NEW)
        self.record(self.marker, "1.0.0", OLD)        # forged lower by the agent user
        self.assertNotEqual(self.guard("1.2.4", OLD).returncode, 0)

    def test_the_marker_is_used_before_root_has_a_record(self):
        self.record(self.marker, "1.2.5", NEW)
        self.assertNotEqual(self.guard("1.2.4", OLD).returncode, 0)
        self.assertEqual(self.guard("1.2.6", OLD).returncode, 0)

    def test_an_unversioned_install_from_before_release_versions_installs(self):
        self.marker.write_text(json.dumps({"vms_release_commit": OLD, "installer_version": "1.1.0"}), encoding="utf-8")
        self.assertEqual(self.guard("1.2.5", NEW).returncode, 0)

    def test_an_unreadable_record_is_refused_not_guessed(self):
        for content in ("not json", json.dumps({"release_version": "v1.2", "vms_release_commit": OLD}),
                        json.dumps(["1.2.5"]), json.dumps({"release_version": "1.2.5", "vms_release_commit": "zz"})):
            with self.subTest(content=content):
                self.root_record.write_text(content, encoding="utf-8")
                result = self.guard("1.2.6", NEW)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not be verified", result.stderr)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_symlinked_root_record_is_refused(self):
        target = self.tmp / "elsewhere.json"
        self.record(target, "1.0.0", OLD)
        self.root_record.symlink_to(target)
        self.assertNotEqual(self.guard("1.2.4", OLD).returncode, 0)

    def test_a_record_not_owned_by_root_is_refused(self):
        self.record(self.root_record, "1.2.4", OLD)
        script = f'''
export PATH="{self._bash_path(self.shim)}:$PATH"
log() {{ :; }}
VMS_RELEASE_MARKER=/nonexistent RELEASE_GUARD_ROOT_RECORD="{self._bash_path(self.root_record)}"
RELEASE_GUARD_ROOT_UID=4242424 RELEASE_VERSION=1.2.5 VMS_RELEASE_COMMIT={NEW}
source ./03-release-guard.sh
release_downgrade_guard 0
'''
        result = subprocess.run([BASH, "-c", script], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a regular root-owned file", result.stderr)


class InstallerWiringTests(unittest.TestCase):
    def test_install_sh_runs_the_guard_before_changing_anything(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        body = source.split("run_install() {", 1)[1]
        self.assertIn('source "$INSTALLER_DIR/03-release-guard.sh"', source)
        self.assertIn("--allow-downgrade) allow_downgrade=1 ;;", body)
        guard = body.index('release_downgrade_guard "$allow_downgrade" || return 1')
        self.assertLess(body.index("load_release_metadata"), guard)
        for step in ("storage_preflight", "secure_agent_install_root", "docker_setup", "provision_users_dirs", "deploy_vms"):
            self.assertLess(guard, body.index(step), step)

    def test_the_guard_is_packaged(self):
        sys.path.insert(0, str(ROOT))
        from build_release_installer import INSTALLER_RUNTIME_FILES
        self.assertIn("03-release-guard.sh", INSTALLER_RUNTIME_FILES)

    def test_the_installer_reports_the_release_it_carries(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('INSTALLER_VERSION="$RELEASE_VERSION"', source)
        builder = (ROOT / "build_release_installer.py").read_text(encoding="utf-8")
        self.assertIn('"installer_version": release_version,', builder)
        self.assertNotIn('default="1.1.0"', builder)


class BuilderRequiresReleaseVersionTests(unittest.TestCase):
    def test_a_build_without_release_version_is_refused(self):
        cmd = [sys.executable, str(ROOT / "build_release_installer.py"), "--vms-commit", "0" * 40,
               "--vms-repo", "/nonexistent-repo-path-never-reached", "--no-mediamtx", "--no-update-signing-key",
               "--no-entitlement-signing-keys"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--release-version is required", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
