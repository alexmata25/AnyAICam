"""Release wiring tests for the protected Linux camera-capacity acceptance helper."""
from pathlib import Path
import sys
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
        self.assertIn("--accept-migrated-snapshot", library)
        self.assertIn('"--accept-migrated-snapshot"', applier)
        # The steady-state systemd invocation has no migration exception.
        unit = (ROOT / "appliance-agent/system/anyaicam-capacity-accept.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/python3 -I /opt/anyaicam-agent/privileged/capacity_accept.py\n", unit)
        self.assertNotIn("--accept-migrated-snapshot", unit)

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
