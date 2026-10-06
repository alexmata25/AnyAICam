"""Green full-lifecycle blocker B-1 (2026-10-06) and the uninstall cleanup it
found alongside.

B-1: the customer extracts the release archive as their own login account
and runs `sudo install.sh`. 07-install-agent.sh copied the agent payload with
plain `rsync -a`, so all of /opt/anyaicam-agent/source stayed owned by that
account while root executes and installs code from it -- and every later
repair then quarantined the freshly installed tree as untrusted.

These run the real installer functions against temporary folders, as root
(real ownership), with systemctl/iptables/docker replaced by logging stubs.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
LINUX = os.name == "posix" and Path("/proc").is_dir()
AS_ROOT = LINUX and os.geteuid() == 0
CUSTOMER_UID = 1000  # the login account that extracted the archive


def bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", script], cwd=ROOT, text=True, capture_output=True,
                          env={**os.environ, **(env or {})})


@unittest.skipUnless(BASH and AS_ROOT and shutil.which("rsync"), "needs Linux as root with rsync")
class AgentSourceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o755)
        self.payload = self.tmp / "extracted" / "payload" / "agent"
        self.agent_root = self.tmp / "opt-anyaicam-agent"
        self.source = self.agent_root / "source"
        self.make_customer_payload()

    def make_customer_payload(self):
        """As `tar -xf` leaves it for a login user with umask 002."""
        (self.payload / "scripts").mkdir(parents=True)
        (self.payload / "system").mkdir()
        (self.payload / "anyaicam_agent").mkdir()
        (self.payload / "scripts" / "install.sh").write_text("#!/bin/sh\necho install\n")
        (self.payload / "system" / "apply_release.py").write_text("print('applier')\n")
        (self.payload / "anyaicam_agent" / "__init__.py").write_text("")
        (self.payload / "pyproject.toml").write_text("[project]\nname='x'\n")
        os.chmod(self.payload / "scripts" / "install.sh", 0o775)
        for path in [self.payload, *self.payload.rglob("*")]:
            if path.is_dir():
                os.chmod(path, 0o775)
            elif path.name != "install.sh":
                os.chmod(path, 0o664)
        for path in [self.tmp / "extracted", *(self.tmp / "extracted").rglob("*")]:
            os.chown(path, CUSTOMER_UID, CUSTOMER_UID)

    def run_step(self, call: str) -> subprocess.CompletedProcess:
        script = textwrap.dedent(f'''\
            set -euo pipefail
            log() {{ echo "LOG: $*"; }}
            source ./07-install-agent.sh
            AGENT_PAYLOAD_DIR="{self.payload}"
            AGENT_SOURCE_ROOT="{self.source}"
            AGENT_INSTALL_ROOT="{self.agent_root}"
            ''') + call + "\n"
        return bash(script, {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}"})

    def deploy(self, expect_ok=True) -> subprocess.CompletedProcess:
        self.agent_root.mkdir(exist_ok=True)
        os.chmod(self.agent_root, 0o755)
        result = self.run_step("deploy_agent_source")
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def untrusted_entries(self, root: Path) -> list:
        bad = []
        for path in [root, *root.rglob("*")]:
            info = os.lstat(path)
            if info.st_uid != 0 or info.st_gid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                bad.append((str(path.relative_to(root.parent)), info.st_uid, oct(stat.S_IMODE(info.st_mode))))
        return bad

    def quarantines(self) -> list:
        return sorted(self.tmp.glob("opt-anyaicam-agent.untrusted-*"))

    def secure(self) -> subprocess.CompletedProcess:
        stub = self.tmp / "bin" / "systemctl"
        stub.parent.mkdir(exist_ok=True)
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        return self.run_step('AGENT_INSTALL_ROOT="%s"; secure_agent_install_root' % self.agent_root)

    # 1. Green's exact case
    def test_a_customer_extracted_payload_installs_root_owned_and_not_writable_by_others(self):
        self.deploy()
        self.assertEqual(self.untrusted_entries(self.source), [])
        installed = self.source / "scripts" / "install.sh"
        self.assertTrue(os.stat(installed).st_mode & stat.S_IXUSR)  # execute bits survive
        self.assertEqual(installed.read_text(), "#!/bin/sh\necho install\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.source / "system" / "apply_release.py").st_mode), 0o644)

    def test_the_old_plain_rsync_kept_the_customers_ownership(self):
        """The defect itself, as Green saw it: what `rsync -a` produced."""
        self.source.mkdir(parents=True)
        subprocess.run(["rsync", "-a", "--delete", f"{self.payload}/", f"{self.source}/"], check=True)
        owners = {os.lstat(p).st_uid for p in self.source.rglob("*")}
        self.assertEqual(owners, {CUSTOMER_UID})

    # 2. rerun and --repair keep a legitimate tree, and never quarantine it
    def test_rerun_and_repair_leave_a_freshly_installed_tree_alone(self):
        self.deploy()
        for _ in range(2):  # a rerun, then a --repair with the same package
            result = self.secure()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.quarantines(), [])
            self.deploy()
        self.assertEqual(self.untrusted_entries(self.agent_root), [])

    def test_files_an_earlier_install_left_customer_owned_are_reowned(self):
        """rsync skips unchanged files, so --no-owner alone would leave these."""
        self.source.mkdir(parents=True)
        subprocess.run(["rsync", "-a", f"{self.payload}/", f"{self.source}/"], check=True)  # the old install
        self.deploy()
        self.assertEqual(self.untrusted_entries(self.source), [])

    # 3. no weaker quarantine, no new escape
    def test_a_genuinely_unsafe_tree_is_still_quarantined(self):
        self.deploy()
        planted = self.agent_root / "privileged"
        planted.mkdir()
        (planted / "watcher.py").write_text("print('planted')\n")
        os.chown(planted / "watcher.py", CUSTOMER_UID, CUSTOMER_UID)
        result = self.secure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.quarantines()), 1)

    def test_a_symlink_in_the_payload_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, self.payload / "system" / "link")
        result = self.deploy(expect_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertFalse(self.source.exists())

    def test_a_symlinked_source_root_is_refused(self):
        self.agent_root.mkdir()
        target = self.tmp / "elsewhere"
        target.mkdir()
        os.symlink(target, self.source)
        result = self.run_step("deploy_agent_source")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(list(target.iterdir()), [])

    # 4. installer validation detects an unsafe tree
    def test_validate_detects_unsafe_agent_tree_ownership(self):
        self.deploy()
        check = textwrap.dedent(f'''\
            source ./validate.sh
            AGENT_INSTALL_ROOT="{self.agent_root}"
            agent_tree_trusted
            ''')
        self.assertEqual(bash(check).returncode, 0)
        os.chown(self.source / "pyproject.toml", CUSTOMER_UID, CUSTOMER_UID)
        self.assertNotEqual(bash(check).returncode, 0)
        os.chown(self.source / "pyproject.toml", 0, 0)
        os.chmod(self.source / "scripts", 0o775)
        self.assertNotEqual(bash(check).returncode, 0)

    def test_install_agent_uses_the_trusted_deploy(self):
        text = (ROOT / "07-install-agent.sh").read_text(encoding="utf-8")
        body = text.split("install_agent() {", 1)[1]
        self.assertIn("deploy_agent_source || return 1", body)
        self.assertNotIn('rsync -a --delete "$AGENT_PAYLOAD_DIR/"', text)
        self.assertIn("umask 022", (ROOT.parent / "appliance-agent" / "scripts" / "install.sh").read_text(encoding="utf-8"))


@unittest.skipUnless(BASH and LINUX, "needs Linux with bash")
class UninstallCleanupTests(unittest.TestCase):
    """What `uninstall.sh --purge-all` left behind in Green's run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"

    def stub(self, name: str, body: str):
        path = self.bin / name
        path.write_text(f"#!/usr/bin/env bash\necho \"{name} $*\" >> \"{self.log}\"\n{body}\n")
        path.chmod(0o755)

    def run_uninstall_fn(self, call: str, env: dict | None = None) -> subprocess.CompletedProcess:
        script = f"set -euo pipefail\nsource ./uninstall.sh\n{call}\n"
        return bash(script, {"PATH": f"{self.bin}:{os.environ['PATH']}", **(env or {})})

    def calls(self) -> list:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_the_webrtc_firewall_unit_script_jump_and_chain_are_removed(self):
        unit, script = self.tmp / "anyaicam-webrtc-firewall.service", self.tmp / "anyaicam-webrtc-firewall"
        unit.write_text("[Unit]\n")
        script.write_text("#!/bin/sh\n")
        (self.tmp / "anyaicam-webrtc-firewall.service.d").mkdir()
        counter = self.tmp / "jumps"
        counter.write_text("2")  # the jump was inserted twice by an old bug
        self.stub("systemctl", "exit 0")
        self.stub("iptables", textwrap.dedent(f'''\
            n=$(cat "{counter}")
            if [[ "$2" == "-C" && "$3" == "DOCKER-USER" ]]; then (( n > 0 )) && exit 0 || exit 1; fi
            if [[ "$2" == "-D" ]]; then echo $((n - 1)) > "{counter}"; fi
            exit 0'''))
        result = self.run_uninstall_fn("remove_webrtc_firewall",
                                       {"WEBRTC_FIREWALL_UNIT": str(unit), "WEBRTC_FIREWALL_SCRIPT": str(script)})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("systemctl disable --now anyaicam-webrtc-firewall.service", calls)
        self.assertEqual(sum(1 for c in calls if c.startswith("iptables -w -D DOCKER-USER")), 2)
        self.assertIn("iptables -w -F ANYAICAM-WEBRTC", calls)
        self.assertIn("iptables -w -X ANYAICAM-WEBRTC", calls)
        self.assertFalse(any("-X DOCKER-USER" in c or "-F DOCKER-USER" in c for c in calls))  # Docker's chain stays
        self.assertFalse(unit.exists() or script.exists() or (self.tmp / "anyaicam-webrtc-firewall.service.d").exists())

    def test_every_vms_image_tag_and_only_those_are_removed(self):
        self.stub("docker", textwrap.dedent('''\
            if [[ "$1 $2" == "image ls" ]]; then
              printf '%s\\n' anyaicam-vms:latest anyaicam-vms:rollback-0123456789ab anyaicam-vms:release-ba9876543210 \\
                anyaicam-vms:before-rollback 'anyaicam-vms:<none>' other-app:latest anyaicam-vms-extra:latest
            fi
            exit 0'''))
        result = self.run_uninstall_fn("remove_vms_images")
        self.assertEqual(result.returncode, 0, result.stderr)
        removed = sorted(c.split()[-1] for c in self.calls() if c.startswith("docker image rm"))
        self.assertEqual(removed, ["anyaicam-vms:before-rollback", "anyaicam-vms:latest",
                                   "anyaicam-vms:release-ba9876543210", "anyaicam-vms:rollback-0123456789ab"])

    def test_only_real_quarantine_directories_are_removed(self):
        root = self.tmp / "opt-anyaicam-agent"
        (self.tmp / "opt-anyaicam-agent.untrusted-20261006T000000Z" / "privileged").mkdir(parents=True)
        outside = self.tmp / "not-ours"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        os.symlink(outside, self.tmp / "opt-anyaicam-agent.untrusted-link")
        result = self.run_uninstall_fn(f'AGENT_INSTALL_ROOT="{root}"; remove_agent_quarantine')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tmp / "opt-anyaicam-agent.untrusted-20261006T000000Z").exists())
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        result = self.run_uninstall_fn(f'AGENT_INSTALL_ROOT="{self.tmp / "nothing-here"}"; remove_agent_quarantine')
        self.assertEqual(result.returncode, 0, result.stderr)  # no matches is fine

    def test_purge_restores_suspend_and_a_plain_uninstall_keeps_the_masks(self):
        self.stub("systemctl", "exit 0")
        result = self.run_uninstall_fn("restore_system_suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target", self.calls())
        text = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
        body = text.split("run_uninstall() {", 1)[1]
        purge = body.split('if [[ "$purge" -eq 1 ]]; then', 1)[1].split("else", 1)[0]
        for step in ("remove_vms_images", "remove_agent_quarantine", "restore_system_suspend"):
            self.assertIn(step, purge)
            self.assertEqual(body.count(step), 1, step)  # purge only
        self.assertLess(body.index("docker compose down"), body.index("remove_webrtc_firewall"))
        self.assertNotIn("remove_webrtc_firewall", purge)  # every uninstall removes the firewall


if __name__ == "__main__":
    unittest.main()
