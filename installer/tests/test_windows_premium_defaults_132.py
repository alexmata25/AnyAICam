"""Windows 1.3.2 (Blue): the Windows installer's side of the owner's analytics defaults (2026-10-19).

Policy (premium analytics OFF until entitled AND turned on; basic motion ON) lives in the shared app/ code (Red,
claude/red-127-analytics-default-off). The installer must not undercut it:
  * it never turns a premium analytic on (no per-feature ON switch, no camera or database state);
  * the only analytics keys it writes are the appliance CAPABILITY switches the Linux installer also defaults
    (06-deploy-vms.sh): People Counting and the rule analytics, add-if-missing, so an explicit false survives upgrade;
    1.3.4 (LPR parity): ANYAICAM_LPR_ENABLED joins them, as on Linux, now that the Tesseract engine is bundled. It is an
    appliance capability switch there too; the per-camera entitlement and enable still decide whether LPR runs;
  * it never turns basic (pixel-change) motion detection off.
"""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
PREMIUM_SWITCHES = ("ANYAICAM_SMART_MOTION_ENABLED", "ANYAICAM_PPE_ENABLED", "ANYAICAM_FACIAL_RECOGNITION_ENABLED")
CAPABILITY_SWITCHES = ("ANYAICAM_LPR_ENABLED", "PEOPLE_COUNTING_ENABLED", "CUSTOMER_ANALYTICS_RULES_ENABLED")


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.scripts = {path.name: path.read_text(encoding="utf-8") for path in WIN.glob("*.ps1")}
        self.scripts["AnyAiCam-VMS.iss"] = read("AnyAiCam-VMS.iss")

    def test_no_installer_script_turns_a_premium_analytic_on_or_basic_motion_off(self):
        for name, text in self.scripts.items():
            for switch in PREMIUM_SWITCHES:
                self.assertNotRegex(text, re.escape(switch) + r"[^\n]*=\s*'?true", f"{name} turns {switch} on")
            self.assertNotIn("MOTION_DETECTION_ENABLED", text, f"{name} touches basic motion detection")

    def test_the_installer_never_writes_camera_or_analytics_state(self):
        for name, text in self.scripts.items():
            for marker in ("camera_analytics_operations", "camera_analytics_entitlements", "partner_portal.db'", "sqlite"):
                if marker == "partner_portal.db'" and name == "service-launcher.ps1":
                    continue  # the launcher only tells the VMS where its database is
                self.assertNotIn(marker, text, f"{name} writes analytics/camera state ({marker})")

    def test_only_the_linux_capability_switches_are_defaulted(self):
        runtime = read("install-runtime.ps1")
        written = set(re.findall(r"\$values\['([A-Z_]*ENABLED)'\]", runtime))
        self.assertEqual(written, set())                                    # nothing written by literal key
        self.assertIn("@('ANYAICAM_LPR_ENABLED', 'PEOPLE_COUNTING_ENABLED', 'CUSTOMER_ANALYTICS_RULES_ENABLED')", runtime)


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell")
class UpgradeMergeTests(unittest.TestCase):
    """Runs install-runtime.ps1's own analytics block against a saved vms.env, as an upgrade does."""

    def merge(self, saved):
        runtime = read("install-runtime.ps1")
        block = re.search(r"foreach \(\$analyticsFlag in .*?\n\}\n", runtime, re.S)
        self.assertIsNotNone(block)
        pairs = "".join(f"$values['{key}'] = '{value}'\n" for key, value in saved.items())
        script = ("$values = [ordered]@{}\n" + pairs + block.group(0) +
                  "$values.GetEnumerator() | ForEach-Object { \"$($_.Key)=$($_.Value)\" }\n")
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def test_new_install_gets_the_capability_switches(self):
        self.assertEqual(self.merge({}), {key: "true" for key in CAPABILITY_SWITCHES})

    def test_an_explicit_false_survives_an_upgrade(self):
        merged = self.merge({"ANYAICAM_LPR_ENABLED": "false", "PEOPLE_COUNTING_ENABLED": "false",
                             "CUSTOMER_ANALYTICS_RULES_ENABLED": "false", "ANYAICAM_PPE_ENABLED": "false"})
        self.assertEqual(merged, {"ANYAICAM_LPR_ENABLED": "false", "PEOPLE_COUNTING_ENABLED": "false",
                                  "CUSTOMER_ANALYTICS_RULES_ENABLED": "false", "ANYAICAM_PPE_ENABLED": "false"})


if __name__ == "__main__":
    unittest.main()
