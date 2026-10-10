"""Failure injection for installer steps that must preserve Bash errexit."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "bash"


COMMON = r"""
source ./install.sh
TMP_DIR="$1"
load_release_metadata() { :; }
verify_installer_payload() { :; }
preflight_checks() { :; }
webrtc_port_preflight() { :; }
vms_http_port_preflight() { :; }
detect_install_state() { INSTALL_STATE=existing; }
release_downgrade_guard() { :; }
select_product_mode() { :; }
storage_preflight() { :; }
secure_agent_install_root() { :; }
provision_entitlement_signing_keys() { :; }
install_capacity_accept_step() { :; }
migrate_capacity_fallback_step() { :; }
docker_setup() { :; }
prepare_agent_install_prerequisites() {
    AGENT_WHEELHOUSE_PARENT_DIR="$TMP_DIR/wheelhouse"
    AGENT_WHEELHOUSE_DIR="$AGENT_WHEELHOUSE_PARENT_DIR/wheels"
    mkdir -p "$AGENT_WHEELHOUSE_DIR"
}
prepare_vms_image() { :; }
cleanup_agent_install_prerequisites() {
    printf 'cleanup_agent_install_prerequisites\n'
    [[ -z "${AGENT_WHEELHOUSE_PARENT_DIR:-}" ]] || rm -rf -- "$AGENT_WHEELHOUSE_PARENT_DIR"
    AGENT_WHEELHOUSE_DIR=""
    AGENT_WHEELHOUSE_PARENT_DIR=""
    AGENT_PREPARATION_VENV_DIR=""
}
install_mediamtx() { printf 'install_mediamtx\n'; }
install_webrtc_firewall() { :; }
systemd_setup() { printf 'systemd_setup\n'; }
disable_system_suspend() { :; }
identity_provision() { :; }
bootstrap_capacity_identity() { :; }
claim_label_provision() { :; }
cloud_portal_provision() { :; }
desktop_setup_provision() { :; }
provision_update_signing_key() { :; }
stamp_release() { :; }
agent_restart_after_provisioning() { :; }
"""


@unittest.skipUnless(os.name != "nt", "requires a working Bash interpreter")
class InstallerFailurePropagationTests(unittest.TestCase):
    def run_installer(self, extra: str, *, setup: str = "") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="anyaicam-installer-failure-") as temp:
            script = (
                COMMON
                + "\n"
                + textwrap.dedent(setup)
                + "\n"
                + textwrap.dedent(extra)
                + "\nrun_install --repair\n"
            )
            env = dict(os.environ)
            env["TMPDIR"] = temp
            return subprocess.run(
                [BASH, "-c", script, "failure-test", temp],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def assert_stopped_at_failure(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cleanup_agent_install_prerequisites", result.stdout)
        self.assertFalse(
            "install_mediamtx" in result.stdout,
            "installer continued after the injected failure: " + result.stdout + result.stderr,
        )

    def test_failed_user_provisioning_aborts_full_install(self) -> None:
        result = self.run_installer(
            r"""
            id() { return 1; }
            useradd() { return 41; }
            install() { return 0; }
            deploy_vms() { printf 'deploy_vms\n'; }
            """
        )
        self.assert_stopped_at_failure(result)
        self.assertNotIn("deploy_vms", result.stdout)

    def test_failed_rollback_tool_install_aborts_full_install(self) -> None:
        result = self.run_installer(
            r"""
            ROLLBACK_TOOL="$TMP_DIR/bin/anyaicam-rollback"
            provision_users_dirs() { :; }
            install() {
                if [[ "$1" == "-m" ]]; then return 42; fi
                return 0
            }
            deploy_vms() { :; }
            install_agent() { printf 'install_agent\n'; }
            """
        )
        self.assert_stopped_at_failure(result)
        self.assertNotIn("install_agent", result.stdout)

    def test_failed_agent_rsync_aborts_full_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="anyaicam-agent-payload-") as payload:
            (Path(payload) / "scripts").mkdir()
            (Path(payload) / "scripts" / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            result = self.run_installer(
                r"""
                AGENT_PAYLOAD_DIR="$1"
                AGENT_INSTALL_ROOT="$TMP_DIR/agent-root"
                AGENT_SOURCE_ROOT="$AGENT_INSTALL_ROOT/source"
                provision_users_dirs() { :; }
                install_rollback_tool() { :; }
                install() {
                    if [[ "$1" == "-d" ]]; then mkdir -p "${@: -1}"; return $?; fi
                    return 0
                }
                rsync() { return 43; }
                deploy_vms() { :; }
                install_mediamtx() { printf 'install_mediamtx\n'; }
                """.replace('AGENT_PAYLOAD_DIR="$1"', f'AGENT_PAYLOAD_DIR="{payload}"')
            )
        self.assert_stopped_at_failure(result)
        self.assertIn("Could not copy the verified appliance-agent source", result.stderr)

    def test_failed_first_vms_environment_write_aborts_deployment(self) -> None:
        result = self.run_installer(
            r"""
            CONFIG_DIR="$TMP_DIR/config"
            provision_users_dirs() { :; }
            VMS_ENV_FILE="$CONFIG_DIR/vms.env"
            VMS_PAYLOAD_DIR="$TMP_DIR/vms-payload"
            VMS_INSTALL_ROOT="$TMP_DIR/vms-install"
            mkdir -p "$VMS_PAYLOAD_DIR/app"
            VMS_RELEASE_COMMIT=abcdef1234567890abcdef1234567890abcdef12
            VMS_IMAGE=anyaicam-vms
            PREPARED_VMS_IMAGE_TAG="$VMS_IMAGE:release-${VMS_RELEASE_COMMIT:0:12}"
            migrate_legacy_persistent_data() { :; }
            migrate_legacy_persistent_file() { :; }
            create_rollback_point() { :; }
            relocate_stale_install_dirs() { :; }
            systemctl() { return 1; }
            install() {
                if [[ "$1" == "-d" ]]; then mkdir -p "${@: -1}"; return $?; fi
                return 0
            }
            rsync() { return 0; }
            normalize_vms_install_ownership() { :; }
            agent_file() { return 44; }
            persist_product_mode() { printf 'persist_product_mode\n'; }
            docker() { printf 'docker\n'; }
            install_rollback_tool() { printf 'install_rollback_tool\n'; }
            """
        )
        self.assert_stopped_at_failure(result)
        self.assertNotIn("persist_product_mode", result.stdout)
        self.assertNotIn("docker", result.stdout)
        self.assertNotIn("install_rollback_tool", result.stdout)


if __name__ == "__main__":
    unittest.main()
