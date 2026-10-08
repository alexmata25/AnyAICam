"""Windows cloud linking provisioning (installer/windows/cloud-link.ps1,
2026-10-07): the Windows counterpart of installer/09-identity.sh. These run the
real script against a temporary data folder (no install, no service, no
network) and check its files against the cloud's label formula and the agent's
own headless-claim eligibility rules."""
import hashlib
import json
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

    def env(self):
        return dict(l.split("=", 1) for l in self.read("config", "agent.env").splitlines() if "=" in l and not l.startswith("#"))

    def test_provisions_identity_portal_and_release_and_no_claim_code(self):
        # 2026-10-08: the permanent claim code is retired; the agent links on
        # demand (headless_claim on_demand=True), which needs no label.
        out = self.run_script()
        identity = json.loads(self.read("config", "appliance_identity.json"))
        self.assertRegex(identity["appliance_id"], headless_claim.DEVICE_ID_PATTERN)
        self.assertEqual(self.env(), {"ANYAICAM_PORTAL_URL": "https://app.anyaicam.com", "ANYAICAM_AGENT_MODE": "production"})
        self.assertFalse((self.root / "label").exists())
        self.assertFalse((self.root / "config" / "label_claim.json").exists())
        config = AgentConfig(config_dir=str(self.root / "config"), state_dir=str(self.root / "agent"),
                             portal_url=self.env()["ANYAICAM_PORTAL_URL"])
        self.assertIsNone(headless_claim.ineligibility(config, on_demand=True))
        self.assertIsNotNone(headless_claim.ineligibility(config))  # the label claim is not possible any more
        self.assertNotIn("claim code", (out.stdout + out.stderr).lower())

    def test_upgrade_from_1_3_0_retires_the_permanent_claim_code(self):
        code = "7K3M9QX2H4TB"
        (self.root / "label").mkdir(parents=True)
        (self.root / "config").mkdir(parents=True)
        (self.root / "label" / "claim-label.txt").write_text(f"Claim code: {code[:4]}-{code[4:8]}-{code[8:]}\n", encoding="utf-8")
        (self.root / "config" / "label_claim.json").write_text(json.dumps({"version": 1, "verifier": cloud_label_verifier(code)}), encoding="utf-8")
        self.run_script()
        self.assertFalse((self.root / "label").exists())
        self.assertFalse((self.root / "config" / "label_claim.json").exists())

    def test_a_label_folder_with_other_files_is_kept(self):
        (self.root / "label").mkdir(parents=True)
        (self.root / "label" / "claim-label.txt").write_text("Claim code: X\n", encoding="utf-8")
        (self.root / "label" / "notes.txt").write_text("kept", encoding="utf-8")
        self.run_script()
        self.assertEqual(sorted(p.name for p in (self.root / "label").iterdir()), ["notes.txt"])

    def test_installed_release_is_what_the_agent_reports(self):
        self.run_script()
        release = json.loads(self.read("config", "vms_release.json"))
        self.assertEqual((release["release_version"], release["vms_release_commit"], release["platform"]), ("1.3.0", COMMIT, "windows"))

    def test_reinstall_keeps_identity_and_portal(self):
        self.run_script(portal="https://cloud.example.test")
        identity = self.read("config", "appliance_identity.json")
        self.run_script()
        self.assertEqual(self.read("config", "appliance_identity.json"), identity)
        self.assertEqual(self.env()["ANYAICAM_PORTAL_URL"], "https://cloud.example.test")

    def test_portal_must_be_an_https_cloud_address(self):
        for bad in ("http://app.anyaicam.com", "https://localhost", "https://127.0.0.1:8000", "https://0.0.0.0", "not a url"):
            result = self.run_script(portal=bad, expect_ok=False)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertFalse((self.root / "config").exists(), bad)  # refused before anything is written

    def test_an_unusable_identity_file_is_never_replaced(self):
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "appliance_identity.json").write_text('{"appliance_id": "not-a-uuid"}', encoding="utf-8")
        result = self.run_script(expect_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not-a-uuid", self.read("config", "appliance_identity.json"))


if __name__ == "__main__":
    unittest.main()
