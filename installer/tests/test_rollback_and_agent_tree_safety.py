"""Codex Linux release blockers R-01 and R-02 (2026-10-06).

R-01: rollback.sh stopped the VMS and replaced code and image before the
hardened helper checked vms.env, so an unsafe vms.env left the VMS stopped
with a half-switched release. Every file it writes is now checked first, the
replaced state is snapshotted, and a failure after the stop puts the previous
release back and restarts the VMS if it was running.

R-02: an older installer left /opt/anyaicam-agent owned by the unprivileged
service user, and root runs code from it (the privileged watcher, the
Software Update applier, the venv's pip) before step 07 re-owns it. The
installer now secures it first: a tree that is not entirely root-controlled
is moved aside, inert, and a fresh root-owned one takes its place.

docker/systemctl are stubs; everything runs in temporary folders. These need
real Linux (rsync, symlinks, file ownership) and skip elsewhere.
"""
import os
import shutil
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path

import test_rollback_point as rp
from test_rollback_point import BASH, NEW, OLD, ROOT

LINUX = os.name == "posix" and Path("/proc").is_dir()
AS_ROOT = LINUX and os.geteuid() == 0

DOCKER_STUB = textwrap.dedent('''\
    #!/usr/bin/env bash
    echo "docker $*" >> "$STUB_LOG"
    case "$1 $2" in
      "image inspect") exit 0 ;;
      "tag "*) [[ -n "${FAKE_TAG_FAIL_MATCH:-}" && "$2" == "$FAKE_TAG_FAIL_MATCH" ]] && exit 1 || exit 0 ;;
    esac
    if [[ "$1" == "inspect" ]]; then echo "${FAKE_RUNNING:-true}"; exit 0; fi
    if [[ "$1" == "exec" ]]; then
      name="${@: -1}"; printf 'online-backup' > "$FAKE_RECORDINGS/$name"; exit 0
    fi
    exit 0
    ''')
SYSTEMCTL_STUB = textwrap.dedent('''\
    #!/usr/bin/env bash
    echo "systemctl $*" >> "$STUB_LOG"
    if [[ "$1" == "is-active" ]]; then [[ "${FAKE_ACTIVE:-1}" == "1" ]] && exit 0 || exit 3; fi
    exit 0
    ''')


@unittest.skipUnless(BASH and shutil.which("rsync") and LINUX, "needs Linux with bash and rsync")
class RollbackSafetyTests(unittest.TestCase):
    """R-01. Reuses the rollback-point harness (setup, stubs, helpers)
    without re-running its own tests."""
    env = rp.RollbackPointTests.env
    run_bash = rp.RollbackPointTests.run_bash
    rollback_paths = rp.RollbackPointTests.rollback_paths

    def setUp(self):
        rp.RollbackPointTests.setUp(self)
        for name, body in (("docker", DOCKER_STUB), ("systemctl", SYSTEMCTL_STUB)):
            (self.bin / name).write_text(body, newline="\n")
        # A rollback point for OLD, then "upgraded" to NEW.
        self.run_bash("create_rollback_point")
        self.log.write_text("")
        (self.install_root / "app" / "main.py").write_text("NEW RELEASE CODE\n")
        self.env_text = f"ANYAICAM_VMS_COMMIT={NEW}\nANYAICAM_BUILD_ID={NEW}\nOTHER=kept\n"
        self.env_file.write_text(self.env_text)
        self.env_file.chmod(0o640)
        self.marker_text = '{"vms_release_commit": "%s", "release_version": "1.2.2"}' % NEW
        self.marker.write_text(self.marker_text)

    def rollback(self, *args, **extra):
        env = self.env(**self.rollback_paths(), **extra)
        return subprocess.run([BASH, "./rollback.sh", "--yes", *args], cwd=ROOT, text=True, capture_output=True, env=env)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_new_release_untouched(self):
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "NEW RELEASE CODE\n")
        self.assertNotIn(f"docker tag anyaicam-vms:rollback-{OLD[:12]} anyaicam-vms:latest", self.calls())

    # 1. an unsafe vms.env fails before the VMS is stopped
    def test_unsafe_vms_env_fails_before_the_vms_is_stopped(self):
        victim = self.tmp / "victim"
        victim.write_text("root-only-secret\n")
        cases = {
            "symlink": lambda: (self.env_file.unlink(), os.symlink(victim, self.env_file)),
            "hard link": lambda: os.link(self.env_file, self.tmp / "second-name"),
            "group-writable": lambda: self.env_file.chmod(0o664),
            "a directory": lambda: (self.env_file.unlink(), self.env_file.mkdir()),
        }
        for label, make_unsafe in cases.items():
            with self.subTest(label):
                self.setUp()
                make_unsafe()
                result = self.rollback()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("nothing was changed", result.stderr)
                self.assertFalse(any(c.startswith("systemctl stop") for c in self.calls()), self.calls())
                self.assert_new_release_untouched()
                self.assertEqual(victim.read_text(), "root-only-secret\n")
                self.assertEqual(self.marker.read_text(), self.marker_text)

    # 2. an unwritable marker location also stops before any change
    def test_an_unwritable_marker_fails_before_the_vms_is_stopped(self):
        self.marker.unlink()
        self.marker.mkdir()
        result = self.rollback()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c.startswith("systemctl stop") for c in self.calls()))
        self.assert_new_release_untouched()

    # 3. a failure after the stop restores the previous release and service state
    def test_a_failure_after_the_stop_restores_the_previous_release_and_restarts_the_vms(self):
        result = self.rollback(FAKE_TAG_FAIL_MATCH=f"anyaicam-vms:rollback-{OLD[:12]}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous release was put back", result.stderr)
        calls = self.calls()
        stop = calls.index("systemctl stop anyaicam-vms.service")
        self.assertIn("docker tag anyaicam-vms:before-rollback anyaicam-vms:latest", calls[stop:])
        self.assertEqual(calls[-1], "systemctl start anyaicam-vms.service")  # it was running: running again
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "NEW RELEASE CODE\n")
        self.assertEqual(self.env_file.read_text(), self.env_text)
        self.assertEqual(self.marker.read_text(), self.marker_text)
        self.assertEqual((self.install_root / "recordings" / "legacy.mkv").read_text(), "footage")  # data untouched
        self.assertEqual((self.install_root / ".env").read_text(), "SECRET=1\n")

    def test_a_vms_that_was_stopped_stays_stopped_after_a_restored_failure(self):
        result = self.rollback(FAKE_TAG_FAIL_MATCH=f"anyaicam-vms:rollback-{OLD[:12]}", FAKE_ACTIVE="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("systemctl start anyaicam-vms.service", self.calls())
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "NEW RELEASE CODE\n")

    def test_a_database_swap_is_undone_when_the_start_fails(self):
        (self.recordings / "partner_portal.db").write_text("database after upgrade")
        start_fails = self.bin / "systemctl"
        start_fails.write_text(SYSTEMCTL_STUB.replace(
            'exit 0\n', '[[ "$1" == "start" && ! -f "$STUB_LOG.started" ]] && { touch "$STUB_LOG.started"; exit 1; }\nexit 0\n'),
            newline="\n")
        result = self.rollback("--restore-database")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.recordings / "partner_portal.db").read_text(), "database after upgrade")
        self.assertFalse([p for p in self.recordings.iterdir() if p.name.startswith("partner_portal-before-rollback-")])
        self.assertEqual(self.env_file.read_text(), self.env_text)

    # 4. a successful rollback still works
    def test_a_successful_rollback_still_works(self):
        result = self.rollback()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "OLD RELEASE CODE\n")
        self.assertIn(f"ANYAICAM_BUILD_ID={OLD}", self.env_file.read_text())
        self.assertIn("OTHER=kept", self.env_file.read_text())
        self.assertIn(f'"vms_release_commit": "{OLD}"', self.marker.read_text())
        calls = self.calls()
        self.assertLess(calls.index("systemctl is-active --quiet anyaicam-vms.service"), calls.index("systemctl stop anyaicam-vms.service"))
        self.assertLess(calls.index("docker tag anyaicam-vms:latest anyaicam-vms:before-rollback"),
                        calls.index("systemctl stop anyaicam-vms.service"))
        self.assertEqual(calls[-1], "systemctl start anyaicam-vms.service")


@unittest.skipUnless(BASH and AS_ROOT, "needs Linux as root (real file ownership)")
class LegacyAgentTreeTests(unittest.TestCase):
    """R-02."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o755)
        self.root = self.tmp / "opt-anyaicam-agent"
        self.log = self.tmp / "calls.log"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text('#!/usr/bin/env bash\necho "systemctl $*" >> "%s"\n' % self.log, newline="\n")
        (self.bin / "systemctl").chmod(0o755)

    def legacy_tree(self, uid=65534):
        """As an older installer left it: owned by the service user, with code
        root runs from it, plus whatever that user planted."""
        (self.root / "privileged").mkdir(parents=True)
        (self.root / "venv" / "bin").mkdir(parents=True)
        (self.root / "privileged" / "watcher.py").write_text("print('legit')\n")
        (self.root / "privileged" / "json.py").write_text("raise SystemExit('planted')\n")
        (self.root / "venv" / "bin" / "pip").write_text("#!/bin/sh\necho planted\n")
        for path in [self.root, *self.root.rglob("*")]:
            os.chown(path, uid, uid, follow_symlinks=False)

    def secure(self):
        script = (f'set -euo pipefail; log() {{ echo "LOG: $*"; }}; source ./07-install-agent.sh; '
                  f'AGENT_INSTALL_ROOT="{self.root}"; secure_agent_install_root')
        env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run([BASH, "-c", script], cwd=ROOT, text=True, capture_output=True, env=env)

    def quarantines(self):
        return sorted(self.tmp.glob("opt-anyaicam-agent.untrusted-*"))

    def test_a_legacy_service_user_tree_is_replaced_by_a_root_owned_one(self):
        self.legacy_tree()
        result = self.secure()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = os.lstat(self.root)
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 0, 0o755))
        self.assertEqual(list(self.root.iterdir()), [])  # nothing the service user wrote is left to run
        [quarantine] = self.quarantines()
        info = os.lstat(quarantine)
        self.assertEqual((info.st_uid, stat.S_IMODE(info.st_mode)), (0, 0o700))
        self.assertTrue((quarantine / "privileged" / "json.py").exists())  # kept for inspection, inert
        self.assertIn("systemctl stop anyaicam-privileged-watcher.path anyaicam-privileged-watcher.service",
                      self.log.read_text())

    def test_a_partly_legacy_tree_is_replaced_too(self):
        self.legacy_tree(uid=0)
        os.chown(self.root / "venv" / "bin" / "pip", 65534, 65534)  # one file the service user still owns
        self.assertEqual(self.secure().returncode, 0)
        self.assertEqual(len(self.quarantines()), 1)
        self.assertFalse((self.root / "venv").exists())

    def test_a_group_writable_tree_is_replaced(self):
        self.legacy_tree(uid=0)
        os.chmod(self.root / "privileged", 0o775)
        self.assertEqual(self.secure().returncode, 0)
        self.assertEqual(len(self.quarantines()), 1)

    def test_securing_is_idempotent_and_leaves_a_root_tree_alone(self):
        self.legacy_tree()
        self.assertEqual(self.secure().returncode, 0)
        (self.root / "privileged").mkdir(mode=0o700)
        (self.root / "privileged" / "watcher.py").write_text("print('reinstalled')\n")
        self.assertEqual(self.secure().returncode, 0)
        self.assertEqual(len(self.quarantines()), 1)  # no second move
        self.assertEqual((self.root / "privileged" / "watcher.py").read_text(), "print('reinstalled')\n")

    def test_a_clean_machine_is_untouched(self):
        result = self.secure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.quarantines(), [])

    def test_a_symlinked_agent_root_stops_the_install(self):
        real = self.tmp / "elsewhere"
        real.mkdir()
        os.symlink(real, self.root)
        result = self.secure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertTrue(self.root.is_symlink())


class OrderTests(unittest.TestCase):
    def test_the_agent_tree_is_secured_before_any_other_step(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        body = install.split("run_install() {", 1)[1]
        steps = [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith(("#", "log ", "if ", "fi", "local ", "for ", "case ", "esac", "done", "mode=", "--", "*)", '[[ "$arg"'))]
        secure = steps.index("secure_agent_install_root")
        self.assertEqual(steps[secure - 1], "preflight_checks")  # right after the root/OS checks
        for later in ("provision_users_dirs \"$INSTALL_STATE\"", "deploy_vms \"$INSTALL_STATE\"", "install_agent \"$INSTALL_STATE\"",
                      "systemd_setup", "provision_update_signing_key", "provision_entitlement_signing_keys"):
            self.assertGreater(steps.index(later), secure, later)

    def test_users_dirs_step_never_hands_the_agent_root_to_the_service_user(self):
        step = (ROOT / "05-provision-users-dirs.sh").read_text(encoding="utf-8")
        code = "\n".join(line for line in step.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("/opt/anyaicam-agent", code)


if __name__ == "__main__":
    unittest.main()
