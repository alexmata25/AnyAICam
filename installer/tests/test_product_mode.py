"""Execute installer policy in temporary directories; never run the installer."""
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

    def test_fresh_modes_persist_defaults_and_repeat_safely(self):
        for mode, value in (("local", "false"), ("hybrid", "true")):
            with self.subTest(mode=mode):
                self.run_policy(f'''
ANYAICAM_PRODUCT_MODE={mode}
select_product_mode
ensure_vms_env
grep -qx 'ANYAICAM_PRODUCT_MODE={mode}' "$VMS_ENV_FILE"
test "$(grep -c '_ENABLED={value}' "$VMS_ENV_FILE")" = 4
cp "$VMS_ENV_FILE" "$tmp/before"
INSTALL_STATE=existing
select_product_mode
ensure_vms_env
diff <(sort "$tmp/before") <(sort "$VMS_ENV_FILE")
''')

    def test_explicit_template_overrides_survive(self):
        self.run_policy('''
mkdir -p "$PAYLOAD_DIR/config"
printf '%s\\n' ANYAICAM_RECORDING_UPLOAD_ENABLED=true ANYAICAM_ANALYTICS_SYNC_ENABLED=false > "$PAYLOAD_DIR/config/vms.env.template"
ANYAICAM_PRODUCT_MODE=local
select_product_mode
ensure_vms_env
grep -qx ANYAICAM_RECORDING_UPLOAD_ENABLED=true "$VMS_ENV_FILE"
grep -qx ANYAICAM_ANALYTICS_SYNC_ENABLED=false "$VMS_ENV_FILE"
''')

    def test_invalid_and_blank_requests_fail(self):
        for value in ("", "LOCAL", "cloud", " hybrid "):
            with self.subTest(value=value):
                self.run_policy(f"ANYAICAM_PRODUCT_MODE='{value}'\nselect_product_mode", False)

    def test_unattended_missing_mode_fails(self):
        self.run_policy("select_product_mode", False)

    def test_legacy_existing_partial_and_legacy_path(self):
        for state in ("existing", "partial", "clean"):
            with self.subTest(state=state):
                self.run_policy(f'''
INSTALL_STATE={state}
printf '%s\\n' ANYAICAM_LIVE_RELAY_ENABLED=true > "$VMS_INSTALL_ROOT/.env"
select_product_mode
cp "$VMS_INSTALL_ROOT/.env" "$VMS_ENV_FILE"
ensure_vms_env
grep -qx ANYAICAM_PRODUCT_MODE=hybrid "$VMS_ENV_FILE"
grep -qx ANYAICAM_LIVE_RELAY_ENABLED=true "$VMS_ENV_FILE"
test "$(grep -c '_ENABLED=false' "$VMS_ENV_FILE")" = 3
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
