"""Windows cloud linking provisioning (installer/windows/cloud-link.ps1,
2026-10-07): the Windows counterpart of installer/09-identity.sh. These run the
real script against a temporary data folder (no install, no service, no
network) and check its files against the cloud's label formula and the agent's
own headless-claim eligibility rules."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer" / "windows" / "cloud-link.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
COMMIT = "18dff1428805c3e05ebb4bf6a4feb9ff99a69194"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

sys.path.insert(0, str(ROOT / "appliance-agent"))
from anyaicam_agent import headless_claim  # noqa: E402
from anyaicam_agent.config import AgentConfig  # noqa: E402


def cloud_label_verifier(code):
    # app/appliance_claims.py label_verifier(), restated so this test needs
    # no web framework: sha256(LABEL_VERIFIER_PREFIX + code), ASCII.
    return hashlib.sha256(("anyaicam-label-claim-v1:" + code).encode("ascii")).hexdigest()


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class CloudLinkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="anyaicam-cloud-link-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_script(self, portal="", expect_ok=True):
        args = [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                "-DataRoot", str(self.root), "-AppVersion", "1.3.0", "-SourceCommit", COMMIT]
        if portal:
            args += ["-PortalUrl", portal]
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read(self, *parts):
        return (self.root.joinpath(*parts)).read_text(encoding="utf-8-sig")

    def code(self):
        line = next(l for l in self.read("label", "claim-label.txt").splitlines() if l.startswith("Claim code:"))
        return line.split(":", 1)[1].strip()

    def env(self):
        return dict(l.split("=", 1) for l in self.read("config", "agent.env").splitlines() if "=" in l and not l.startswith("#"))

    def test_mints_a_label_code_the_cloud_and_the_agent_accept(self):
        out = self.run_script()
        pretty = self.code()
        self.assertRegex(pretty, r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
        code = pretty.replace("-", "")
        label = json.loads(self.read("config", "label_claim.json"))
        self.assertEqual(label, {"version": 1, "verifier": cloud_label_verifier(code)})
        identity = json.loads(self.read("config", "appliance_identity.json"))
        self.assertRegex(identity["appliance_id"], headless_claim.DEVICE_ID_PATTERN)
        self.assertEqual(self.env(), {"ANYAICAM_PORTAL_URL": "https://app.anyaicam.com", "ANYAICAM_AGENT_MODE": "production"})
        self.assertIn(f"QR code: https://app.anyaicam.com/claim#label={pretty}", self.read("label", "claim-label.txt"))
        self.assertIn(f"Appliance ID: {identity['appliance_id']}", self.read("label", "claim-label.txt"))
        # The agent's own eligibility check passes on exactly these files.
        config = AgentConfig(config_dir=str(self.root / "config"), state_dir=str(self.root / "agent"),
                             portal_url=self.env()["ANYAICAM_PORTAL_URL"])
        self.assertIsNone(headless_claim.ineligibility(config))
        # Never printed: the code or its verifier.
        self.assertNotIn(code, out.stdout + out.stderr)
        self.assertNotIn(pretty, out.stdout + out.stderr)
        self.assertNotIn(label["verifier"], out.stdout + out.stderr)

    def test_installed_release_is_what_the_agent_reports(self):
        self.run_script()
        release = json.loads(self.read("config", "vms_release.json"))
        self.assertEqual((release["release_version"], release["vms_release_commit"], release["platform"]), ("1.3.0", COMMIT, "windows"))

    def test_reinstall_keeps_identity_code_and_portal(self):
        self.run_script(portal="https://cloud.example.test")
        identity, code = self.read("config", "appliance_identity.json"), self.code()
        self.run_script()
        self.assertEqual(self.read("config", "appliance_identity.json"), identity)
        self.assertEqual(self.code(), code)
        self.assertEqual(self.env()["ANYAICAM_PORTAL_URL"], "https://cloud.example.test")
        # The verifier is rewritten from the saved code every run.
        (self.root / "config" / "label_claim.json").write_text('{"version": 1, "verifier": "0"}', encoding="utf-8")
        self.run_script()
        self.assertEqual(json.loads(self.read("config", "label_claim.json"))["verifier"], cloud_label_verifier(code.replace("-", "")))

    def test_codes_are_random_and_use_the_whole_alphabet(self):
        seen = set()
        for _ in range(3):
            shutil.rmtree(self.root / "label", ignore_errors=True)
            self.run_script()
            seen.add(self.code())
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(ch in ALPHABET for code in seen for ch in code.replace("-", "")))

    def test_portal_must_be_an_https_cloud_address(self):
        for bad in ("http://app.anyaicam.com", "https://localhost", "https://127.0.0.1:8000", "https://0.0.0.0", "not a url"):
            result = self.run_script(portal=bad, expect_ok=False)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertFalse((self.root / "label").exists(), bad)  # refused before anything is written

    def test_an_unusable_identity_file_is_never_replaced(self):
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "appliance_identity.json").write_text('{"appliance_id": "not-a-uuid"}', encoding="utf-8")
        result = self.run_script(expect_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not-a-uuid", self.read("config", "appliance_identity.json"))


if __name__ == "__main__":
    unittest.main()
