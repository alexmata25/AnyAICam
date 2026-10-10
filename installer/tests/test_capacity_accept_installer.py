"""Release wiring tests for the protected Linux camera-capacity acceptance helper."""
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "installer"))
from build_release_installer import AGENT_RELEASE_PATHS, INSTALLER_RUNTIME_FILES  # noqa: E402

BASH = os.environ.get("TEST_BASH") or shutil.which("bash")


def bash_path(path):
    value = Path(path).as_posix()
    if os.name == "nt" and len(value) > 1 and value[1] == ":":
        value = "/" + value[0].lower() + value[2:]
    return value


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
        self.assertIn("Pending signed camera-capacity snapshot was not accepted", library)
        self.assertNotIn("refusing to report bootstrap success", library)
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

    def test_agent_repair_rejection_is_nonfatal_and_vms_restart_follows_agent_install(self):
        installer = (ROOT / "installer/install.sh").read_text(encoding="utf-8")
        agent_installer = (ROOT / "appliance-agent/scripts/install.sh").read_text(encoding="utf-8")
        self.assertLess(installer.index('deploy_vms "$INSTALL_STATE"'), installer.index('install_agent "$INSTALL_STATE"'))
        self.assertLess(installer.index('install_agent "$INSTALL_STATE" --defer-capacity-bootstrap'),
                        installer.index('    systemd_setup\n'))
        self.assertIn('    install_agent "$INSTALL_STATE" --defer-capacity-bootstrap\n', installer)
        self.assertIn('"$1" == "--defer-capacity-bootstrap"', agent_installer)
        self.assertIn('"$DEFER_CAPACITY_BOOTSTRAP" == 0', agent_installer)
        self.assertLess(installer.index('    systemd_setup\n'), installer.index("bootstrap_capacity_identity migration"))
        library = (ROOT / "appliance-agent/scripts/lib-capacity-accept.sh").read_text(encoding="utf-8")
        self.assertIn("Pending signed camera-capacity snapshot was not accepted", library)

    def test_full_installer_passes_deferral_to_agent_subinstaller(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "agent-argv"
            wheelhouse = Path(tmp) / "wheels"
            wheelhouse.mkdir(mode=0o700)
            result = subprocess.run([BASH, "-c", r'''set -euo pipefail
source ./install.sh
AGENT_PAYLOAD_DIR="$TEST_PAYLOAD"
AGENT_SOURCE_ROOT="$TEST_SOURCE"
AGENT_WHEELHOUSE_DIR="$TEST_WHEELHOUSE"
apt-get() { :; }
deploy_agent_source() { :; }
bash() { printf '%s\n' "$@" > "$AGENT_ARGV_TRACE"; }
install_agent repair --defer-capacity-bootstrap
'''], cwd=ROOT / "installer", text=True, capture_output=True,
                                env={**os.environ, "TEST_PAYLOAD": str(ROOT / "appliance-agent"),
                                     "TEST_SOURCE": str(ROOT / "appliance-agent"),
                                     "TEST_WHEELHOUSE": bash_path(wheelhouse),
                                     "AGENT_ARGV_TRACE": str(trace)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            arguments = trace.read_text(encoding="utf-8").splitlines()
            self.assertEqual(Path(arguments[0]).resolve(),
                             (ROOT / "appliance-agent/scripts/install.sh").resolve())
            self.assertEqual(arguments[1:], ["--defer-capacity-bootstrap"])


@unittest.skipUnless(BASH, "bash is required for installer repair integration")
class CapacityRepairContinuationTests(unittest.TestCase):
    """Exercise the real installer orchestrator and bootstrap shell function.

    The host commands are isolated stubs. A rejected stage fails the watcher
    service as it does for an expired/invalid snapshot; the real signed-snapshot
    rejection and state preservation are covered by test_capacity_accept.py.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.agent_privileged = self.tmp / "opt/anyaicam-agent/privileged"
        self.agent_privileged.mkdir(parents=True)
        self.stage = self.tmp / "var/lib/anyaicam/vms/data-config/vms_capacity_signed.json"
        self.stage.parent.mkdir(parents=True)
        self.stage.write_text("expired signed snapshot fixture\n", encoding="utf-8")
        self.files = {
            "accepted capacity": self.tmp / "etc/anyaicam-update/vms_capacity_accepted.json",
            "appliance identity": self.tmp / "etc/anyaicam/appliance_identity.json",
            "camera credentials": self.tmp / "etc/anyaicam/camera-credentials.json",
            "recording": self.tmp / "var/lib/anyaicam/recordings/camera-1.mkv",
        }
        for label, path in self.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((label + " preserved\n").encode())
        self.expected_files = {label: path.read_bytes() for label, path in self.files.items()}
        self.trace = self.tmp / "repair-trace.log"
        self.systemctl_trace = self.tmp / "systemctl-trace.log"

        fake_python = self.bin / "python3"
        fake_python.write_text("""#!/usr/bin/env bash
case " $* " in
  *" --bootstrap-identity "*) echo unchanged; exit 0 ;;
  *" --accept-migrated-snapshot "*) echo 'capacity snapshot rejected: expired' >&2; exit 1 ;;
  *) exit 2 ;;
esac
""", encoding="utf-8", newline="\n")
        fake_python.chmod(0o755)
        systemctl = self.bin / "systemctl"
        systemctl.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {shlex.quote(bash_path(self.systemctl_trace))}
if [[ "$*" == "start anyaicam-capacity-accept.service" ]]; then exit 1; fi
exit 0
""", encoding="utf-8", newline="\n")
        systemctl.chmod(0o755)

        # Use the production function body with only fixed system paths
        # redirected into this disposable fixture.
        library = (ROOT / "appliance-agent/scripts/lib-capacity-accept.sh").read_text(encoding="utf-8")
        library = library.replace("/usr/bin/python3", shlex.quote(bash_path(fake_python)))
        library = library.replace("/opt/anyaicam-agent/privileged", shlex.quote(bash_path(self.agent_privileged)))
        library = library.replace("/var/lib/anyaicam/vms/data-config/vms_capacity_signed.json",
                                  shlex.quote(bash_path(self.stage)))
        self.library = self.tmp / "lib-capacity-accept.sh"
        self.library.write_text(library, encoding="utf-8", newline="\n")

    def test_expired_stage_and_repeated_repair_complete_and_restore_vms(self):
        env = dict(os.environ, PATH=f"{bash_path(self.bin)}:{os.environ.get('PATH', '')}",
                   REPAIR_TRACE=bash_path(self.trace), SYSTEMCTL_TRACE=bash_path(self.systemctl_trace),
                   CAPACITY_LIBRARY=bash_path(self.library))
        script = f'''\
set -euo pipefail
source {shlex.quote(bash_path(ROOT / "installer/install.sh"))}
source "$CAPACITY_LIBRARY"
log() {{ :; }}
load_release_metadata() {{ INSTALLER_VERSION=1.2.5; VMS_RELEASE_COMMIT={"a" * 40}; }}
verify_installer_payload() {{ :; }}; preflight_checks() {{ :; }}
webrtc_port_preflight() {{ :; }}; vms_http_port_preflight() {{ :; }}
detect_install_state() {{ INSTALL_STATE=existing; }}; release_downgrade_guard() {{ :; }}
select_product_mode() {{ :; }}; storage_preflight() {{ :; }}
secure_agent_install_root() {{ :; }}; provision_entitlement_signing_keys() {{ :; }}
install_capacity_accept_step() {{ :; }}; migrate_capacity_fallback_step() {{ :; }}
docker_setup() {{ :; }}; provision_users_dirs() {{ :; }}
prepare_agent_install_prerequisites() {{ :; }}; prepare_vms_image() {{ :; }}
deploy_vms() {{ printf 'VMS_REMOVED\\n' >> "$REPAIR_TRACE"; }}
install_rollback_tool() {{ :; }}
install_agent() {{ bootstrap_capacity_identity; printf 'AGENT_RETURNED\\n' >> "$REPAIR_TRACE"; }}
install_mediamtx() {{ :; }}; install_webrtc_firewall() {{ :; }}
systemd_setup() {{ printf 'VMS_RESTORED\\n' >> "$REPAIR_TRACE"; }}
disable_system_suspend() {{ :; }}; identity_provision() {{ :; }}
claim_label_provision() {{ :; }}; cloud_portal_provision() {{ :; }}
desktop_setup_provision() {{ :; }}; provision_update_signing_key() {{ :; }}
stamp_release() {{ :; }}; agent_restart_after_provisioning() {{ :; }}
run_install --repair
run_install --repair
'''
        result = subprocess.run([BASH, "-c", script], cwd=ROOT / "installer", env=env,
                                 text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.count("Pending signed camera-capacity snapshot was not accepted"), 2,
                         result.stderr)
        trace = self.trace.read_text(encoding="utf-8").splitlines()
        for start in range(0, len(trace), 3):
            group = trace[start:start + 3]
            self.assertIn("VMS_REMOVED", group)
            self.assertIn("AGENT_RETURNED", group)
            self.assertIn("VMS_RESTORED", group)
            self.assertLess(group.index("VMS_REMOVED"), group.index("AGENT_RETURNED"))
            self.assertLess(group.index("AGENT_RETURNED"), group.index("VMS_RESTORED"))
        self.assertTrue(self.stage.exists(), "rejected staging is preserved for a later cloud retry")
        self.assertEqual(self.systemctl_trace.read_text(encoding="utf-8").splitlines().count(
            "start anyaicam-capacity-accept.service"), 2)
        for label, path in self.files.items():
            self.assertEqual(path.read_bytes(), self.expected_files[label], label)

    def test_full_repair_defers_agent_bootstrap_until_after_vms_restart(self):
        agent_source = self.tmp / "agent-source/scripts"
        agent_source.mkdir(parents=True)
        child_installer = agent_source / "install.sh"
        child_installer.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" != "--defer-capacity-bootstrap" ]]; then
  printf 'CHILD_BOOTSTRAP_FAILED\n' >> {shlex.quote(bash_path(self.trace))}
  exit 1
fi
printf 'CHILD_BOOTSTRAP_DEFERRED\n' >> {shlex.quote(bash_path(self.trace))}
''', encoding="utf-8", newline="\n")
        env = dict(os.environ, REPAIR_TRACE=bash_path(self.trace),
                   TEST_AGENT_SOURCE=bash_path(self.tmp / "agent-source"),
                   TEST_AGENT_PAYLOAD=bash_path(self.tmp / "agent-payload"),
                   TEST_AGENT_WHEELHOUSE=bash_path(self.tmp / "agent-wheelhouse"))
        script = f'''\
set -euo pipefail
source {shlex.quote(bash_path(ROOT / "installer/install.sh"))}
AGENT_SOURCE_ROOT="$TEST_AGENT_SOURCE"
AGENT_PAYLOAD_DIR="$TEST_AGENT_PAYLOAD"
mkdir -p "$AGENT_PAYLOAD_DIR/scripts"
cp {shlex.quote(bash_path(child_installer))} "$AGENT_PAYLOAD_DIR/scripts/install.sh"
apt-get() {{ :; }}
deploy_agent_source() {{ :; }}
log() {{ :; }}
load_release_metadata() {{ INSTALLER_VERSION=1.2.5; VMS_RELEASE_COMMIT={"a" * 40}; }}
verify_installer_payload() {{ :; }}; preflight_checks() {{ :; }}
webrtc_port_preflight() {{ :; }}; vms_http_port_preflight() {{ :; }}
detect_install_state() {{ INSTALL_STATE=existing; }}; release_downgrade_guard() {{ :; }}
select_product_mode() {{ :; }}; storage_preflight() {{ :; }}
secure_agent_install_root() {{ :; }}; provision_entitlement_signing_keys() {{ :; }}
install_capacity_accept_step() {{ printf 'CAPACITY_VALIDATED_BEFORE_DOWNTIME\\n' >> "$REPAIR_TRACE"; }}
migrate_capacity_fallback_step() {{ :; }}; docker_setup() {{ :; }}
prepare_agent_install_prerequisites() {{ AGENT_WHEELHOUSE_DIR="$TEST_AGENT_WHEELHOUSE"; mkdir -p "$AGENT_WHEELHOUSE_DIR"; }}
prepare_vms_image() {{ :; }}; provision_users_dirs() {{ :; }}
deploy_vms() {{ printf 'VMS_STOPPED\\n' >> "$REPAIR_TRACE"; }}
install_rollback_tool() {{ :; }}; install_mediamtx() {{ :; }}
install_webrtc_firewall() {{ :; }}
systemd_setup() {{ printf 'VMS_RESTARTED\\n' >> "$REPAIR_TRACE"; }}
bootstrap_capacity_identity() {{
  if [[ "${{1:-}}" == migration ]]; then
    printf 'POST_RESTART_CLOCK_VALIDATION_REJECTED\\n' >> "$REPAIR_TRACE"
    return 1
  fi
  return 0
}}
disable_system_suspend() {{ :; }}; identity_provision() {{ :; }}
claim_label_provision() {{ :; }}; cloud_portal_provision() {{ :; }}
desktop_setup_provision() {{ :; }}; provision_update_signing_key() {{ :; }}
stamp_release() {{ :; }}; agent_restart_after_provisioning() {{ :; }}
rc=0
run_install --repair || rc=$?
printf 'INSTALL_RC=%s\\n' "$rc" >> "$REPAIR_TRACE"
'''
        result = subprocess.run([BASH, "-c", script], cwd=ROOT / "installer", env=env,
                                 text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.trace.read_text(encoding="utf-8").splitlines()
        self.assertEqual(events, ["CAPACITY_VALIDATED_BEFORE_DOWNTIME", "VMS_STOPPED",
                                  "CHILD_BOOTSTRAP_DEFERRED", "VMS_RESTARTED",
                                  "POST_RESTART_CLOCK_VALIDATION_REJECTED", "INSTALL_RC=1"])
        self.assertNotIn("CHILD_BOOTSTRAP_FAILED", events)


if __name__ == "__main__":
    unittest.main()
