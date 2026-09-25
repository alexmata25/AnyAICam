"""Execute installer policy in temporary directories; never run the installer.

Reconciled 2026-09-21 against app/product_mode.py's runtime resolver (see
03-product-mode.sh's own header comment and docs/product-mode-local-
hybrid-2026-09-21.md for the full trace): this file's original version
(Codex, commit 59bed3ddf04b0cb9936238d05a89779774d409ba) asserted that
persist_product_mode() materializes four individual ANYAICAM_*_ENABLED
flags here, duplicating app/product_mode.py's own resolve_cloud_flag()/
FLAG_REGISTRY mapping in a second place -- and one of those four
(ANYAICAM_CLOUD_UPLOAD_ENABLED) gates a retired no-op worker on this
branch, while three flags resolve_cloud_flag() actually governs were
never written here at all. persist_product_mode() no longer writes any
individual flag at all -- these tests assert that instead.
"""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")


class ProductModeTests(unittest.TestCase):
    def run_policy(self, setup, success=True):
        script = r'''
set -euo pipefail
export PATH=/usr/bin:/bin:$PATH
source ./03-product-mode.sh
source ./06-deploy-vms.sh
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
CONFIG_DIR="$tmp/config"
VMS_ENV_FILE="$CONFIG_DIR/vms.env"
VMS_INSTALL_ROOT="$tmp/old"
PAYLOAD_DIR="$tmp/payload"
VMS_RELEASE_COMMIT=0123456789012345678901234567890123456789
mkdir -p "$CONFIG_DIR" "$VMS_INSTALL_ROOT"
INSTALL_STATE=clean
unset ANYAICAM_PRODUCT_MODE
log() { :; }
''' + setup
        result = subprocess.run([BASH, "-c", script], cwd=ROOT,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr)
        return result

    def test_fresh_modes_persist_the_mode_value_and_repeat_safely(self):
        for mode in ("local", "hybrid"):
            with self.subTest(mode=mode):
                self.run_policy(f'''
ANYAICAM_PRODUCT_MODE={mode}
select_product_mode
ensure_vms_env
grep -qx 'ANYAICAM_PRODUCT_MODE={mode}' "$VMS_ENV_FILE"
cp "$VMS_ENV_FILE" "$tmp/before"
INSTALL_STATE=existing
select_product_mode
ensure_vms_env
diff <(sort "$tmp/before") <(sort "$VMS_ENV_FILE")
''')

    def test_persist_product_mode_never_writes_any_individual_enabled_flag(self):
        """The core reconciliation fix: regardless of which mode is
        chosen, persist_product_mode() itself must never materialize
        ANY ANYAICAM_*_ENABLED value -- app/product_mode.py's
        resolve_cloud_flag() is the sole authority for what a mode
        implies for a given flag. Any _ENABLED line present in the file
        after ensure_vms_env() runs must be identical regardless of
        mode, proving it came from ensure_vms_env()'s own unrelated,
        mode-independent defaults (e.g. ANYAICAM_LIVE_RELAY_ENABLED),
        never from this mode selection."""
        enabled_lines_by_mode = {}
        for mode in ("local", "hybrid"):
            result = self.run_policy(f'''
ANYAICAM_PRODUCT_MODE={mode}
select_product_mode
ensure_vms_env
grep '_ENABLED=' "$VMS_ENV_FILE" | sort
''')
            enabled_lines_by_mode[mode] = result.stdout
        self.assertEqual(enabled_lines_by_mode["local"], enabled_lines_by_mode["hybrid"])

    def test_explicit_template_overrides_are_never_touched(self):
        self.run_policy('''
mkdir -p "$PAYLOAD_DIR/config"
printf '%s\\n' ANYAICAM_RECORDING_UPLOAD_ENABLED=true ANYAICAM_ANALYTICS_SYNC_ENABLED=false > "$PAYLOAD_DIR/config/vms.env.template"
ANYAICAM_PRODUCT_MODE=local
select_product_mode
ensure_vms_env
grep -qx ANYAICAM_RECORDING_UPLOAD_ENABLED=true "$VMS_ENV_FILE"
grep -qx ANYAICAM_ANALYTICS_SYNC_ENABLED=false "$VMS_ENV_FILE"
''')

    def test_fresh_install_enables_the_entitlement_gated_analytics_workers(self):
        """Advanced Analytics grants People Counting and LPR per camera,
        but a fresh appliance never ran them: their appliance switches
        defaulted to off and nothing set them. Facial recognition stays
        an explicit opt-in."""
        self.run_policy('''
ANYAICAM_PRODUCT_MODE=hybrid
select_product_mode
ensure_vms_env
grep -qx ANYAICAM_LPR_ENABLED=true "$VMS_ENV_FILE"
grep -qx PEOPLE_COUNTING_ENABLED=true "$VMS_ENV_FILE"
grep -qx CUSTOMER_ANALYTICS_RULES_ENABLED=true "$VMS_ENV_FILE"
! grep -q '^ANYAICAM_FACIAL_RECOGNITION_ENABLED=' "$VMS_ENV_FILE"
''')

    def test_an_explicit_analytics_flag_is_never_changed_or_duplicated(self):
        self.run_policy('''
printf '%s\\n' PEOPLE_COUNTING_ENABLED=false ANYAICAM_LPR_ENABLED=true > "$VMS_ENV_FILE"
ANYAICAM_PRODUCT_MODE=local
select_product_mode
ensure_vms_env
ensure_vms_env
grep -qx PEOPLE_COUNTING_ENABLED=false "$VMS_ENV_FILE"
test "$(grep -c '^PEOPLE_COUNTING_ENABLED=' "$VMS_ENV_FILE")" = 1
test "$(grep -c '^ANYAICAM_LPR_ENABLED=' "$VMS_ENV_FILE")" = 1
test "$(grep -c '^CUSTOMER_ANALYTICS_RULES_ENABLED=true' "$VMS_ENV_FILE")" = 1
''')

    def test_invalid_and_blank_requests_fail(self):
        for value in ("", "LOCAL", "cloud", " hybrid "):
            with self.subTest(value=value):
                self.run_policy(f"ANYAICAM_PRODUCT_MODE='{value}'\nselect_product_mode", False)

    def test_unattended_missing_mode_fails(self):
        self.run_policy("select_product_mode", False)

    def test_legacy_existing_partial_and_legacy_path_leaves_mode_unset(self):
        """The other core reconciliation fix: a legacy install with no
        recorded mode must NOT get a guessed "hybrid" compatibility
        label written at all -- app/product_mode.py's own legacy_
        default=False already reproduces this appliance's exact prior
        behavior for every governed flag, and leaving the env var
        itself unset is what keeps it eligible for a future cloud-
        driven entitlement to set a real mode (see 03-product-mode.sh's
        header comment)."""
        for state in ("existing", "partial", "clean"):
            with self.subTest(state=state):
                self.run_policy(f'''
INSTALL_STATE={state}
printf '%s\\n' ANYAICAM_LIVE_RELAY_ENABLED=true > "$VMS_INSTALL_ROOT/.env"
select_product_mode
test -z "${{ANYAICAM_PRODUCT_MODE:-}}"
cp "$VMS_INSTALL_ROOT/.env" "$VMS_ENV_FILE"
ensure_vms_env
! grep -q '^ANYAICAM_PRODUCT_MODE=' "$VMS_ENV_FILE"
grep -qx ANYAICAM_LIVE_RELAY_ENABLED=true "$VMS_ENV_FILE"
''')

    def test_saved_mode_reused(self):
        self.run_policy('''
printf '%s\\n' ANYAICAM_PRODUCT_MODE=local > "$VMS_ENV_FILE"
select_product_mode
test "$ANYAICAM_PRODUCT_MODE" = local
''')

    def test_corrupt_saved_modes_and_switch_rejected(self):
        for contents in ("ANYAICAM_PRODUCT_MODE=", "ANYAICAM_PRODUCT_MODE=bad",
                         "ANYAICAM_PRODUCT_MODE=local\\nANYAICAM_PRODUCT_MODE=local",
                         "ANYAICAM_PRODUCT_MODE=local"):
            with self.subTest(contents=contents):
                self.run_policy(f'''
printf '{contents}\\n' > "$VMS_ENV_FILE"
ANYAICAM_PRODUCT_MODE=hybrid
select_product_mode
''', False)

    def test_module_is_packaged(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from build_release_installer import INSTALLER_RUNTIME_FILES
        self.assertIn("03-product-mode.sh", INSTALLER_RUNTIME_FILES)


if __name__ == "__main__":
    unittest.main()
