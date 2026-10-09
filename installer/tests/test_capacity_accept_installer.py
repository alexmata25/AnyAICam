"""Release wiring tests for the protected Linux camera-capacity acceptance helper."""
from pathlib import Path
import os
import subprocess
import sys
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "installer"))
from build_release_installer import AGENT_RELEASE_PATHS, INSTALLER_RUNTIME_FILES  # noqa: E402


class CapacityAcceptanceReleaseWiringTests(unittest.TestCase):
    def test_builder_includes_installer_step_and_agent_system_payload(self):
        self.assertIn("16-capacity-accept.sh", INSTALLER_RUNTIME_FILES)
        self.assertIn("appliance-agent/system", AGENT_RELEASE_PATHS)
        for name in ("capacity_accept.py", "anyaicam-capacity-accept.path", "anyaicam-capacity-accept.service"):
            self.assertTrue((ROOT / "appliance-agent" / "system" / name).is_file())

    def test_clean_installer_bootstraps_capacity_identity_after_creation_before_agent_restart(self):
        installer = (ROOT / "installer/install.sh").read_text(encoding="utf-8")
        self.assertLess(installer.index("install_capacity_accept_step"), installer.index('deploy_vms "$INSTALL_STATE"'))
        self.assertLess(installer.index('identity_provision "$INSTALL_STATE"'), installer.index("bootstrap_capacity_identity ||"))
        self.assertLess(installer.index("bootstrap_capacity_identity ||"), installer.index("agent_restart_after_provisioning"))
        helper = (ROOT / "appliance-agent/system/capacity_accept.py").read_text(encoding="utf-8")
        self.assertIn("_write_pending_claim_identity", helper)
        self.assertIn("_write_identity_anchor(root_dir_fd, next_state[\"identity\"][\"appliance_id\"])", helper)
        self.assertIn("initial_claim_transition_required", helper)
        self.assertIn("pin_existing_identity=False", helper)
        self.assertIn("if pin_existing_identity:", helper)

    def test_installer_bootstraps_preexisting_state_before_downtime(self):
        step = (ROOT / "installer/16-capacity-accept.sh").read_text(encoding="utf-8")
        library = (ROOT / "appliance-agent/scripts/lib-capacity-accept.sh").read_text(encoding="utf-8")
        applier = (ROOT / "appliance-agent/system/apply_release.py").read_text(encoding="utf-8")
        self.assertIn('install_capacity_accept "$AGENT_PAYLOAD_DIR/system"', step)
        self.assertIn('bootstrap_capacity_identity || return 1', step)
        self.assertLess(step.index("install_capacity_accept"), step.index("systemctl start anyaicam-capacity-accept.service"))
        self.assertIn('"${INSTALL_STATE:-existing}" == "clean"', step)
        self.assertIn("bootstrap_capacity_identity migration", step)
        self.assertIn('"${1:-}" == "migration"', library)
        self.assertIn("legacy_upgrade=(--legacy-upgrade)", library)
        self.assertIn('"${legacy_upgrade[@]}"', library)
        self.assertIn("--accept-migrated-snapshot", library)
        self.assertIn("pending signed camera-capacity snapshot exists but identity/signature acceptance failed", library)
        self.assertIn('"--accept-migrated-snapshot"', applier)
        # The steady-state systemd invocation has no migration exception.
        unit = (ROOT / "appliance-agent/system/anyaicam-capacity-accept.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/python3 -I /opt/anyaicam-agent/privileged/capacity_accept.py\n", unit)
        self.assertNotIn("--accept-migrated-snapshot", unit)

    def test_full_repair_defers_second_capacity_start_until_vms_is_restarted(self):
        installer_step = (ROOT / "installer/07-install-agent.sh").read_text(encoding="utf-8")
        agent_script = (ROOT / "appliance-agent/scripts/install.sh").read_text(encoding="utf-8")
        self.assertIn('bash "$AGENT_SOURCE_ROOT/scripts/install.sh" --defer-capacity-bootstrap', installer_step)
        self.assertIn("--defer-capacity-bootstrap) DEFER_CAPACITY_BOOTSTRAP=1", agent_script)
        self.assertIn('&& "$DEFER_CAPACITY_BOOTSTRAP" -eq 0 ]]; then', agent_script)
        self.assertIn('bootstrap_capacity_identity\nfi', agent_script)
        self.assertIn("systemd_setup", (ROOT / "installer/install.sh").read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix" and __import__("shutil").which("bash"), "requires Bash")
    def test_full_installer_agent_step_passes_defer_and_preserves_child_status(self):
        harness = textwrap.dedent(f"""
            source {str(ROOT / 'installer/07-install-agent.sh')!r}
            AGENT_PAYLOAD_DIR="$TMPDIR/payload"
            AGENT_SOURCE_ROOT="$TMPDIR/source"
            mkdir -p "$AGENT_PAYLOAD_DIR/scripts" "$AGENT_SOURCE_ROOT/scripts"
            touch "$AGENT_PAYLOAD_DIR/scripts/install.sh"
            apt-get() {{ :; }}
            deploy_agent_source() {{ :; }}
            log() {{ :; }}
            bash() {{ printf 'CHILD_ARGS=%s\\n' "$*"; return 42; }}
            if install_agent repair; then result=0; else result=$?; fi
            printf 'AGENT_INSTALL_RESULT=%s\\n' "$result"
        """)
        result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--defer-capacity-bootstrap", result.stdout)
        self.assertIn("AGENT_INSTALL_RESULT=42", result.stdout)
        self.assertNotIn("Appliance agent verified/updated", result.stdout)

    @unittest.skipUnless(os.name == "posix" and __import__("shutil").which("bash"), "requires Bash")
    def test_run_install_propagates_agent_installer_failure_in_conditional_context(self):
        # `set -e` is suppressed throughout a function when the caller tests
        # that function in an if-condition. The real installer must therefore
        # explicitly propagate an agent install failure rather than continuing
        # to report success without reaching systemd_setup.
        harness = textwrap.dedent(f"""
            source {str(ROOT / 'installer/install.sh')!r}
            load_release_metadata() {{ :; }}
            verify_installer_payload() {{ :; }}
            preflight_checks() {{ :; }}
            webrtc_port_preflight() {{ :; }}
            vms_http_port_preflight() {{ :; }}
            detect_install_state() {{ INSTALL_STATE=existing; }}
            release_downgrade_guard() {{ :; }}
            select_product_mode() {{ :; }}
            storage_preflight() {{ :; }}
            secure_agent_install_root() {{ :; }}
            provision_entitlement_signing_keys() {{ :; }}
            install_capacity_accept_step() {{ :; }}
            migrate_capacity_fallback_step() {{ :; }}
            docker_setup() {{ :; }}
            provision_users_dirs() {{ :; }}
            deploy_vms() {{ :; }}
            install_rollback_tool() {{ :; }}
            install_agent() {{ return 73; }}
            systemd_setup() {{ echo SYSTEMD_SETUP_RAN; }}
            if run_install --repair; then result=0; else result=$?; fi
            printf 'INSTALL_RESULT=%s\\n' "$result"
        """)
        result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INSTALL_RESULT=73", result.stdout)
        self.assertNotIn("SYSTEMD_SETUP_RAN", result.stdout)

    def test_helper_unit_is_fixed_root_isolated_and_rate_limited(self):
        unit = (ROOT / "appliance-agent/system/anyaicam-capacity-accept.service").read_text(encoding="utf-8")
        path_unit = (ROOT / "appliance-agent/system/anyaicam-capacity-accept.path").read_text(encoding="utf-8")
        self.assertIn("User=root", unit)
        self.assertIn("python3 -I /opt/anyaicam-agent/privileged/capacity_accept.py", unit)
        self.assertIn("StartLimitBurst=5", unit)
        self.assertIn("PrivateNetwork=true", unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", unit)
        self.assertIn("CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH", unit)
        self.assertNotIn("CAP_SYS_ADMIN", unit)
        self.assertIn("PathChanged=/var/lib/anyaicam/vms/data-config/vms_capacity_signed.json", path_unit)
        self.assertNotIn("Environment=", unit)

    def test_normal_uninstall_preserves_accepted_capacity_and_trust_state(self):
        library = (ROOT / "appliance-agent/scripts/lib-capacity-accept.sh").read_text(encoding="utf-8")
        function = library.split("uninstall_capacity_accept()", 1)[1]
        self.assertNotRegex(function, r"rm\s+-[rf]+[^\n]*/etc/anyaicam-update")


if __name__ == "__main__":
    unittest.main()
