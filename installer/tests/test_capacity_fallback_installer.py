"""Installer step 15: the one-time camera-capacity migration (2026-10-09).

The migration rules themselves are tested against apply_release
(appliance-agent/tests/test_capacity_fallback_migration.py); here: the bash
step hands the applier the release being REPLACED (read like the downgrade
guard: root's record first), skips clean installs, stops the install on an
unsafe fallback, and runs after the old VMS is stopped and before the new
one starts.
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


@unittest.skipUnless(BASH, "bash is required")
class CapacityStepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root_record = self.tmp / "update-state/installed_release.json"
        self.marker = self.tmp / "etc/vms_release.json"
        self.root_record.parent.mkdir()
        self.marker.parent.mkdir()
        # A stand-in applier: records what it was given, answers OUTCOME.
        system = self.tmp / "payload/agent/system"
        system.mkdir(parents=True)
        (system / "apply_release.py").write_text(
            "import os, sys\n"
            "def migrate_capacity_fallback_main(argv):\n"
            "    open(os.environ['SEEN'], 'w').write(repr(argv))\n"
            "    outcome = os.environ.get('OUTCOME', 'migrated')\n"
            "    if outcome == 'crash':\n"
            "        raise SystemExit(1)\n"
            "    print(outcome); return 0\n", encoding="utf-8")
        shim = self.tmp / "bin"
        shim.mkdir()
        (shim / "python3").write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n', newline="\n")
        (shim / "python3").chmod(0o755)
        self.seen = self.tmp / "seen.txt"

    def _p(self, path):
        return subprocess.run([BASH, "-c", 'cygpath -u "$1" 2>/dev/null || printf %s "$1"', "_", str(path)],
                              capture_output=True, text=True).stdout.strip()

    def step(self, state="existing", outcome="migrated"):
        script = f'''
set -euo pipefail
export PATH="{self._p(self.tmp / "bin")}:$PATH" SEEN="{self._p(self.seen)}" OUTCOME="{outcome}"
log() {{ printf 'LOG %s\\n' "$*"; }}
PAYLOAD_DIR="{self._p(self.tmp / "payload")}"
VMS_RELEASE_MARKER="{self._p(self.marker)}"
RELEASE_GUARD_ROOT_RECORD="{self._p(self.root_record)}"
RELEASE_GUARD_ROOT_UID="$(id -u)"
source ./03-release-guard.sh
source ./15-capacity-fallback.sh
migrate_capacity_fallback_step {state}
'''
        return subprocess.run([BASH, "-c", script], cwd=ROOT, capture_output=True, text=True)

    def given(self):
        return eval(self.seen.read_text()) if self.seen.exists() else None  # noqa: S307 -- our own repr

    def record(self, path, version):
        path.write_text(json.dumps({"release_version": version, "vms_release_commit": "a" * 40}), encoding="utf-8")

    def test_a_clean_install_never_migrates(self):
        result = self.step(state="clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.given())
        self.assertIn("clean install, nothing to migrate", result.stdout)

    def test_the_replaced_release_comes_from_roots_record_first(self):
        self.record(self.root_record, "1.2.4")
        self.record(self.marker, "1.2.9")          # agent-writable: ignored while root's record exists
        result = self.step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.given(), ["1.2.4"])
        self.assertIn("carried the running capacity", result.stdout)

    def test_the_marker_is_used_only_without_roots_record(self):
        self.record(self.marker, "1.2.4")
        self.assertEqual(self.step().returncode, 0)
        self.assertEqual(self.given(), ["1.2.4"])

    def test_no_recorded_release_is_passed_as_empty(self):
        self.assertEqual(self.step(state="partial").returncode, 0)
        self.assertEqual(self.given(), [""])

    def test_an_unreadable_record_migrates_nothing(self):
        self.root_record.write_text("not json", encoding="utf-8")
        result = self.step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.given())
        self.assertIn("could not be read; nothing migrated", result.stdout)

    def test_every_outcome_is_reported_and_none_stops_the_install(self):
        # Orange 2026-10-09: the step runs with the old VMS already stopped,
        # so even an unsafe fallback or a crashed migration must not abort the
        # install half-way; the VMS fails closed on its own.
        self.record(self.root_record, "1.2.4")
        for outcome, text in (("exists", "already exists"), ("not_applicable", "already used signed capacity"),
                              ("no_capacity", "no capacity recorded yet"), ("no_identity", "not activated"),
                              ("unsafe", "existing fallback is unsafe"), ("error", "did not complete ('error')"),
                              ("crash", "did not complete ('error')")):
            with self.subTest(outcome=outcome):
                result = self.step(outcome=outcome)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(text, result.stdout)


class WiringTests(unittest.TestCase):
    def test_the_step_runs_before_vms_downtime(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        body = source.split("run_install() {", 1)[1]
        self.assertIn('source "$INSTALLER_DIR/15-capacity-fallback.sh"', source)
        step = body.index('migrate_capacity_fallback_step "$INSTALL_STATE"')
        self.assertLess(body.index('release_downgrade_guard "$allow_downgrade"'), step)
        self.assertLess(step, body.index('deploy_vms "$INSTALL_STATE"'))
        self.assertLess(step, body.index("systemd_setup"))
        self.assertLess(step, body.index("stamp_release"))   # the replaced release is still the recorded one

    def test_the_step_is_packaged(self):
        sys.path.insert(0, str(ROOT))
        from build_release_installer import INSTALLER_RUNTIME_FILES
        self.assertIn("15-capacity-fallback.sh", INSTALLER_RUNTIME_FILES)
        self.assertIn("16-capacity-accept.sh", INSTALLER_RUNTIME_FILES)

    def test_the_step_uses_the_shipped_applier_and_no_path_from_the_environment(self):
        step = (ROOT / "15-capacity-fallback.sh").read_text(encoding="utf-8")
        self.assertIn('"$PAYLOAD_DIR/agent/system"', step)
        self.assertIn("migrate_capacity_fallback_main", step)
        self.assertNotIn("vms_capacity_fallback.json\"", step.split("migrate_capacity_fallback_step()", 1)[1])


if __name__ == "__main__":
    unittest.main()
