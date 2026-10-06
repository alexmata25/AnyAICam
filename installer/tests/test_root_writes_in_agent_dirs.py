"""Root writes inside folders the anyaicam user owns (2026-10-06).

The installer, the agent installer and rollback.sh run as root but write
files in /etc/anyaicam, which the anyaicam user owns. Shell redirection, cp,
chown and sed -i there follow a symlink that user planted, so it could make
root overwrite -- or hand it ownership of -- any file, or copy a secret file
into a file it can read. These writes now go through the root applier's
symlink-safe routines (appliance-agent/system/apply_release.py
agent_file_main). Also: /opt/anyaicam-agent, which holds the root-only
applier, is never created owned by the anyaicam user.

Shell steps are run against temporary folders (never the real system).
Symlink attacks need real symlinks: they run on Linux and are skipped where
`ln -s` cannot make one (Git Bash on Windows).
"""
import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
SKIP = 77  # a scenario that needs real symlinks, on a host without them

PRELUDE = r'''
set -euo pipefail
source ./03-product-mode.sh
source ./06-deploy-vms.sh
source ./09-identity.sh
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
CONFIG_DIR="$tmp/config"
VMS_ENV_FILE="$CONFIG_DIR/vms.env"
IDENTITY_FILE="$CONFIG_DIR/appliance_identity.json"
VERSION_MARKER="$CONFIG_DIR/installed_version"
VMS_RELEASE_MARKER="$CONFIG_DIR/vms_release.json"
UPDATE_STATE_DIR="$tmp/update-state"
VMS_HLS_DIR="$tmp/vms/hls"
VMS_RECORDINGS_DIR="$tmp/vms/recordings"
VMS_DATA_CONFIG_DIR="$tmp/vms/data-config"
QUARANTINE_DIR="$tmp/vms/recordings/quarantine"
VMS_INSTALL_ROOT="$tmp/old"
PAYLOAD_DIR="$tmp/payload"
VMS_RELEASE_COMMIT=0123456789012345678901234567890123456789
INSTALLER_VERSION=1.2.0
RELEASE_VERSION=1.2.0
INSTALLER_SOURCE_COMMIT=abcdefabcdefabcdefabcdefabcdefabcdefabcd
mkdir -p "$CONFIG_DIR" "$VMS_INSTALL_ROOT" "$UPDATE_STATE_DIR"
INSTALL_STATE=existing
ANYAICAM_PRODUCT_MODE=local
log() { :; }
victim="$tmp/victim"
printf 'root-only-secret\n' > "$victim"
plant() { ln -s "$1" "$2" 2>/dev/null || true; [[ -L "$2" ]] || exit 77; }
'''


@unittest.skipUnless(BASH, "bash is required")
class RootWritesTests(unittest.TestCase):
    def run_steps(self, body, *, expect_ok=True):
        result = subprocess.run([BASH, "-c", PRELUDE + textwrap.dedent(body)], cwd=ROOT, text=True, capture_output=True)
        if result.returncode == SKIP:
            self.skipTest("needs Linux (real symlinks, /proc)")
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, "the installer step should have refused")
        return result

    # ------------------------------------------------------------ normal behaviour is unchanged
    def test_vms_env_is_created_and_repaired_as_before(self):
        self.run_steps('''
            ensure_vms_env
            grep -qx ANYAICAM_RUNTIME_ROLE=edge "$VMS_ENV_FILE"
            grep -qx "ANYAICAM_VMS_COMMIT=$VMS_RELEASE_COMMIT" "$VMS_ENV_FILE"
            grep -qx "ANYAICAM_VERSION=$RELEASE_VERSION" "$VMS_ENV_FILE"
            grep -q '^ANYAICAM_APP_SECRETS=[0-9a-f]\\{64\\}$' "$VMS_ENV_FILE"
            secret="$(grep '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE")"
            ensure_vms_env  # a rerun keeps every existing value
            test "$(grep '^ANYAICAM_CAMERA_CREDENTIAL_KEY=' "$VMS_ENV_FILE")" = "$secret"
            test "$(grep -c '^ANYAICAM_RUNTIME_ROLE=' "$VMS_ENV_FILE")" = 1
            test "$(grep -c '^ANYAICAM_VMS_COMMIT=' "$VMS_ENV_FILE")" = 1
            if [[ "$(uname -s)" == Linux ]]; then test "$(stat -c %a "$VMS_ENV_FILE")" = 640; fi
        ''')

    def test_identity_and_markers_are_written_and_identity_preserved(self):
        self.run_steps('''
            [[ -r /proc/sys/kernel/random/uuid ]] || exit 77   # Linux only
            identity_provision existing
            id1="$(cat "$IDENTITY_FILE")"
            identity_provision existing
            test "$(cat "$IDENTITY_FILE")" = "$id1"  # never regenerated
            grep -qx 1.2.0 "$VERSION_MARKER"
            stamp_release
            grep -q "\\"vms_release_commit\\": \\"$VMS_RELEASE_COMMIT\\"" "$VMS_RELEASE_MARKER"
            cmp -s "$VMS_RELEASE_MARKER" "$UPDATE_STATE_DIR/installed_release.json"
            if [[ "$(uname -s)" == Linux ]]; then test "$(stat -c %a "$IDENTITY_FILE")" = 600; fi
        ''')

    def test_secrets_are_never_passed_on_a_command_line(self):
        source = (ROOT / "06-deploy-vms.sh").read_text(encoding="utf-8")
        body = source.split("ensure_vms_env() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('env_request+=("default ANYAICAM_APP_SECRETS=', body)
        self.assertIn('printf \'%s\\n\' "${env_request[@]}" | agent_file', body)  # on stdin, not argv

    # ------------------------------------------------------------ planted symlinks
    def test_a_symlinked_vms_env_is_never_followed(self):
        self.run_steps('''
            plant "$victim" "$VMS_ENV_FILE"
            ensure_vms_env
        ''', expect_ok=False)

    def test_a_symlinked_vms_env_never_receives_the_targets_contents(self):
        result = self.run_steps('''
            plant "$victim" "$VMS_ENV_FILE"
            upsert_env_key "$VMS_ENV_FILE" ANYAICAM_VMS_COMMIT x || true
            grep -qx root-only-secret "$victim"            # the target is untouched...
            test "$(wc -l < "$victim")" = 1
            if [[ -f "$VMS_ENV_FILE" && ! -L "$VMS_ENV_FILE" ]]; then ! grep -q root-only-secret "$VMS_ENV_FILE"; fi
            echo done
        ''')
        self.assertIn("done", result.stdout)

    def test_a_dangling_symlink_cannot_make_root_create_a_file_elsewhere(self):
        self.run_steps('''
            elsewhere="$tmp/elsewhere/profile.sh"; mkdir -p "$tmp/elsewhere"
            plant "$elsewhere" "$VMS_ENV_FILE"
            ensure_vms_env || true
            test ! -e "$elsewhere"
            plant "$tmp/elsewhere/marker" "$VERSION_MARKER"
            identity_provision existing || true
            test ! -e "$tmp/elsewhere/marker"
        ''')

    def test_a_symlinked_identity_is_refused(self):
        self.run_steps('''
            plant "$victim" "$IDENTITY_FILE"
            identity_provision existing
        ''', expect_ok=False)

    def test_symlinked_markers_are_replaced_not_followed(self):
        self.run_steps('''
            plant "$victim" "$VERSION_MARKER"
            plant "$victim" "$VMS_RELEASE_MARKER"
            identity_provision existing
            stamp_release
            grep -qx root-only-secret "$victim"
            test "$(wc -l < "$victim")" = 1
            test ! -L "$VERSION_MARKER" && test ! -L "$VMS_RELEASE_MARKER"
            ! grep -q root-only-secret "$UPDATE_STATE_DIR/installed_release.json"   # root's copy is never read back
        ''')

    def test_the_legacy_env_migration_never_follows_a_symlink(self):
        self.run_steps('''
            printf 'LEGACY=1\\n' > "$VMS_INSTALL_ROOT/.env"
            plant "$tmp/elsewhere-env" "$VMS_ENV_FILE"
            migrate_legacy_persistent_file "$VMS_INSTALL_ROOT/.env" "$VMS_ENV_FILE" "VMS environment config" || true
            test ! -e "$tmp/elsewhere-env"
        ''')

    # ------------------------------------------------------------ the agent install root
    def test_the_agent_install_root_is_never_created_owned_by_anyaicam(self):
        step = (ROOT / "05-provision-users-dirs.sh").read_text(encoding="utf-8")
        code = "\n".join(line for line in step.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("/opt/anyaicam-agent", code)
        agent = (REPO / "appliance-agent" / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("install -d -m 0755 -o root -g root /opt/anyaicam-agent", agent)

    def test_users_dirs_step_creates_only_agent_owned_state_folders(self):
        result = self.run_steps('''
            mkdir -p "$tmp/bin"
            printf '#!/usr/bin/env bash\\necho "$*" >> "%s"\\n' "$tmp/install.log" > "$tmp/bin/install"
            printf '#!/usr/bin/env bash\\nexit 0\\n' > "$tmp/bin/id"
            chmod +x "$tmp/bin/install" "$tmp/bin/id"
            PATH="$tmp/bin:$PATH"
            source ./05-provision-users-dirs.sh
            provision_users_dirs existing
            cat "$tmp/install.log"
        ''')
        self.assertNotIn("/opt/anyaicam-agent", result.stdout)
        self.assertIn("-o anyaicam -g anyaicam", result.stdout)

    # ------------------------------------------------------------ other root writers
    def test_the_agent_installer_and_rollback_use_the_safe_helper(self):
        agent = (REPO / "appliance-agent" / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("> /etc/anyaicam/agent.env", agent)
        self.assertIn("apply_release.agent_file_main()", agent)
        rollback = (ROOT / "rollback.sh").read_text(encoding="utf-8")
        code = "\n".join(line for line in rollback.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("sed -i", code)
        self.assertNotIn('>> "$VMS_ENV_FILE"', code)
        self.assertNotIn(".rollback-tmp", code)
        self.assertIn("agent_file_main", code)


if __name__ == "__main__":
    unittest.main()
