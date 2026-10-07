"""Headless label claim (2026-10-07): installer/09-identity.sh mints the claim
code printed on the unit's label and gives the agent only its verifier;
install.sh --portal-url points the agent at the AnyAiCam cloud. These run the
real shell functions in temporary directories; they never run the installer."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")

PRELUDE = r'''
set -euo pipefail
source ./06-deploy-vms.sh
source ./09-identity.sh
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
CONFIG_DIR="$tmp/config"
IDENTITY_FILE="$CONFIG_DIR/appliance_identity.json"
CLAIM_LABEL_DIR="$tmp/label"
CLAIM_LABEL_FILE="$CLAIM_LABEL_DIR/claim-label.txt"
mkdir -p "$CONFIG_DIR"
printf '{\n  "appliance_id": "11111111-1111-4111-8111-111111111111"\n}\n' > "$IDENTITY_FILE"
log() { printf 'LOG %s\n' "$*"; }
'''

CODE = re.compile(r"^Claim code: ([0-9A-HJKMNP-TV-Z]{4})-([0-9A-HJKMNP-TV-Z]{4})-([0-9A-HJKMNP-TV-Z]{4})$", re.M)


def verifier(code):
    return hashlib.sha256(("anyaicam-label-claim-v1:" + code).encode()).hexdigest()


@unittest.skipUnless(BASH, "bash is required")
class ClaimLabelTests(unittest.TestCase):
    def run_steps(self, body, *, expect_ok=True):
        result = subprocess.run([BASH, "-c", PRELUDE + textwrap.dedent(body)], cwd=ROOT, text=True, capture_output=True)
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def label_and_json(self, body):
        out = self.run_steps(body + '\necho "---LABEL---"; cat "$CLAIM_LABEL_FILE"; echo "---JSON---"; cat "$CONFIG_DIR/label_claim.json"\n').stdout
        label = out.split("---LABEL---\n", 1)[1].split("---JSON---\n", 1)[0]
        data = json.loads(out.split("---JSON---\n", 1)[1])
        return out, label, data

    def test_generates_a_crockford_code_and_gives_the_agent_only_its_verifier(self):
        out, label, data = self.label_and_json("claim_label_provision")
        match = CODE.search(label)
        self.assertIsNotNone(match, label)
        code = "".join(match.groups())
        self.assertEqual(data, {"version": 1, "verifier": verifier(code)})
        self.assertIn(f"QR code: https://app.anyaicam.com/claim#label={code[:4]}-{code[4:8]}-{code[8:]}", label)
        self.assertIn("Appliance ID: 11111111-1111-4111-8111-111111111111", label)
        # Neither the code nor its verifier is ever logged.
        logs = "\n".join(line for line in out.splitlines() if line.startswith("LOG "))
        for secret in (code, f"{code[:4]}-{code[4:8]}-{code[8:]}", verifier(code)):
            self.assertNotIn(secret, logs)

    def test_codes_are_random_and_uniform_over_the_alphabet(self):
        out = self.run_steps('for i in $(seq 1 300); do label_code_generate; echo; done').stdout.split()
        self.assertEqual(len(out), 300)
        self.assertEqual(len(set(out)), 300)
        self.assertTrue(all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{12}", c) for c in out))
        seen = set("".join(out))
        self.assertEqual(seen, set("0123456789ABCDEFGHJKMNPQRSTVWXYZ"))  # all 32 symbols appear in 3600 draws

    def test_shell_verifier_matches_the_cloud_formula(self):
        out = self.run_steps('label_verifier 7K3M9QX2H4TB').stdout.strip()
        self.assertEqual(out, verifier("7K3M9QX2H4TB"))

    def test_a_repair_keeps_the_printed_label_and_rewrites_the_matching_verifier(self):
        out = self.run_steps('''
            claim_label_provision > /dev/null
            first="$(cat "$CLAIM_LABEL_FILE")"
            printf '{"version": 1, "verifier": "stale"}\\n' > "$CONFIG_DIR/label_claim.json"
            claim_label_provision
            test "$(cat "$CLAIM_LABEL_FILE")" = "$first"
            echo "---JSON---"; cat "$CONFIG_DIR/label_claim.json"; echo; echo "---LABEL---"; cat "$CLAIM_LABEL_FILE"
        ''').stdout
        self.assertIn("Existing claim label kept", out)
        code = "".join(CODE.search(out).groups())
        self.assertIn(verifier(code), out.split("---JSON---", 1)[1])

    def test_the_label_uses_the_install_portal_for_the_qr_link(self):
        _, label, _ = self.label_and_json('ANYAICAM_INSTALL_PORTAL_URL=https://portal.example.test/\nclaim_label_provision')
        self.assertRegex(label, r"QR code: https://portal\.example\.test/claim#label=")

    def test_label_file_is_private(self):
        self.run_steps('''
            claim_label_provision > /dev/null
            if [[ "$(uname -s)" == Linux ]]; then
                test "$(stat -c %a "$CLAIM_LABEL_DIR")" = 700
                test "$(stat -c %a "$CLAIM_LABEL_FILE")" = 600
                test "$(stat -c %a "$CONFIG_DIR/label_claim.json")" = 600
            fi
        ''')

    def test_refuses_a_symlinked_label_location(self):
        result = self.run_steps('''
            ln -s "$tmp" "$tmp/link" 2>/dev/null && [[ -L "$tmp/link" ]] || exit 77
            CLAIM_LABEL_DIR="$tmp/link"; CLAIM_LABEL_FILE="$CLAIM_LABEL_DIR/claim-label.txt"
            claim_label_provision
        ''', expect_ok=False)
        if result.returncode == 77:
            self.skipTest("needs real symlinks")
        self.assertIn("symbolic link", result.stderr)

    # ---------------------------------------------------------------- --portal-url
    def test_portal_url_validation(self):
        for good in ("https://app.anyaicam.com", "https://portal.example.test/", "https://portal.example.test:8443"):
            self.run_steps(f'validate_install_portal_url "{good}"')
        for bad in ("http://app.anyaicam.com", "https://localhost", "https://127.0.0.1:8000", "https://", "app.anyaicam.com", "https://a b.example"):
            result = self.run_steps(f'validate_install_portal_url "{bad}"', expect_ok=False)
            self.assertEqual(result.returncode, 2, bad)

    def test_portal_is_written_to_agent_env_keeping_other_settings(self):
        out = self.run_steps('''
            printf 'ANYAICAM_AGENT_MODE=development\\nANYAICAM_PORTAL_URL=http://127.0.0.1:8000\\nKEEP=1\\n' > "$CONFIG_DIR/agent.env"
            ANYAICAM_INSTALL_PORTAL_URL=https://app.anyaicam.com/
            cloud_portal_provision
            cat "$CONFIG_DIR/agent.env"
        ''').stdout
        self.assertIn("ANYAICAM_PORTAL_URL=https://app.anyaicam.com\n", out)
        self.assertIn("ANYAICAM_AGENT_MODE=production\n", out)
        self.assertIn("KEEP=1\n", out)
        self.assertNotIn("127.0.0.1", out)

    def test_no_portal_url_leaves_agent_env_alone(self):
        out = self.run_steps('''
            printf 'ANYAICAM_PORTAL_URL=http://127.0.0.1:8000\\n' > "$CONFIG_DIR/agent.env"
            unset ANYAICAM_INSTALL_PORTAL_URL
            cloud_portal_provision
            cat "$CONFIG_DIR/agent.env"
        ''').stdout
        self.assertIn("ANYAICAM_PORTAL_URL=http://127.0.0.1:8000", out)

    def test_install_refuses_a_bad_portal_url_before_changing_anything(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('--portal-url=*) ANYAICAM_INSTALL_PORTAL_URL="${arg#*=}" ;;', install)
        validate = install.index('validate_install_portal_url "$ANYAICAM_INSTALL_PORTAL_URL" || return 2')
        self.assertLess(validate, install.index("load_release_metadata\n    log"))
        self.assertLess(install.index('identity_provision "$INSTALL_STATE"'), install.index("claim_label_provision"))
        self.assertLess(install.index("claim_label_provision"), install.index("cloud_portal_provision"))


if __name__ == "__main__":
    unittest.main()
