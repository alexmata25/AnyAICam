"""Codex finding 1 on fc50f44 (2026-10-08): install_agent (07-install-agent.sh)
restarts the agent BEFORE the installer writes the appliance identity, the
claim-label verifier and the cloud portal, so on a fresh install the running
agent had no identity, no label and the 127.0.0.1 placeholder portal: its
headless claim was ineligible and "AnyAiCam Setup" could not link. The
installer now restarts the agent once everything it needs exists
(agent_restart_after_provisioning, 09-identity.sh). These run the real shell
function against a fake systemctl; nothing touches the system."""
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")


@unittest.skipUnless(BASH, "bash is required")
class AgentRestartAfterProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-restart-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.calls = self.tmp / "calls.txt"
        fake = self.tmp / "systemctl"
        fake.write_text(textwrap.dedent(f'''\
            #!/bin/sh
            echo "$*" >> '{self.bash_path(self.calls)}'
            case "$1" in
                is-enabled) exit "${{FAKE_ENABLED:-0}}" ;;
                restart) exit "${{FAKE_RESTART:-0}}" ;;
            esac
            exit 0
        '''), newline="\n")
        fake.chmod(0o755)

    def bash_path(self, path):
        return subprocess.run([BASH, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", str(path)],
                              capture_output=True, text=True).stdout.strip()

    def run_restart(self, **env):
        script = textwrap.dedent(f'''
            set -euo pipefail
            log() {{ printf 'LOG %s\\n' "$*"; }}
            PATH="{self.bash_path(self.tmp)}:$PATH"
            source ./09-identity.sh
            agent_restart_after_provisioning
        ''')
        result = subprocess.run([BASH, "-c", script], cwd=ROOT, text=True, capture_output=True,
                                env={**os.environ, **{k: str(v) for k, v in env.items()}})
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return result, calls

    def test_restarts_the_enabled_agent(self):
        result, calls = self.run_restart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["is-enabled --quiet anyaicam-agent.service", "restart anyaicam-agent.service"])
        self.assertIn("Agent restarted with its identity, claim label and cloud portal.", result.stdout)

    def test_skips_an_agent_that_is_not_enabled(self):
        result, calls = self.run_restart(FAKE_ENABLED=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("restart anyaicam-agent.service", calls)

    def test_a_failed_restart_fails_the_install(self):
        result, _ = self.run_restart(FAKE_RESTART=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not restart anyaicam-agent.service", result.stderr)

    def test_install_restarts_the_agent_after_everything_it_needs_is_written(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        steps = install[install.index("run_install() {"):]
        restart = steps.index("    agent_restart_after_provisioning\n")
        for earlier in ("    install_agent ", '    identity_provision "$INSTALL_STATE"\n', "    claim_label_provision\n",
                        "    cloud_portal_provision\n", "    desktop_setup_provision\n", "    provision_update_signing_key\n",
                        "    provision_entitlement_signing_keys\n", "    stamp_release\n"):
            self.assertLess(steps.index(earlier), restart, earlier.strip())
        self.assertLess(restart, steps.index('    log "Install complete'))


if __name__ == "__main__":
    unittest.main()
