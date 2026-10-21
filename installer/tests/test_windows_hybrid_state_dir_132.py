"""Windows 1.3.2 (Blue): the VMS service finds the cloud agent's state folder.

Before: service-launcher.ps1 never set ANYAICAM_STATE_DIR, so the VMS's Hybrid workers (cloud backup, event media,
analytics/facial sync, live relay, P2P, talk-down) read credential.json from C:\\var\\lib\\anyaicam while the agent
wrote it to %ProgramData%\\AnyAiCam\\agent: every Hybrid feature stayed off. The launchers are run here for real,
up to the point where they would start Python, against a temporary ProgramData.
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
PYTHON_LINE = re.compile(r"^& \(Join-Path \$installRoot 'runtime\\python\\python\.exe'\).*$", re.M)


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


class StaticTests(unittest.TestCase):
    def test_vms_launcher_sets_the_agent_state_folder_after_reading_vms_env(self):
        vms, agent = read("service-launcher.ps1"), read("agent-launcher.ps1")
        line = "$env:ANYAICAM_STATE_DIR = Join-Path $dataRoot 'agent'"
        self.assertIn(line, vms)
        self.assertIn(line, agent)                       # the very same folder the agent writes
        self.assertLess(vms.index("foreach ($line in Get-Content -LiteralPath $environmentFile)"), vms.index(line))

    def test_installer_defaults_the_linux_analytics_switches_and_never_lpr(self):
        runtime = read("install-runtime.ps1")
        self.assertIn("foreach ($analyticsFlag in @('PEOPLE_COUNTING_ENABLED', 'CUSTOMER_ANALYTICS_RULES_ENABLED'))", runtime)
        self.assertIn("if (-not $values.Contains($analyticsFlag)) { $values[$analyticsFlag] = 'true' }", runtime)
        for never in ("ANYAICAM_LPR_ENABLED", "ANYAICAM_FACIAL_RECOGNITION_ENABLED", "ANYAICAM_PPE_ENABLED"):
            self.assertNotIn(f"$values['{never}']", runtime)
        self.assertLess(runtime.index("$analyticsFlag"), runtime.index("[IO.File]::WriteAllLines($environmentFile"))


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell")
class LauncherRunTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-launcher-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = self.tmp / "Program Files" / "AnyAiCam"
        (self.install / "service").mkdir(parents=True)
        (self.install / "app").mkdir()
        self.program_data = self.tmp / "ProgramData"
        (self.program_data / "AnyAiCam" / "config").mkdir(parents=True)
        # vms.env is Administrators-only, but even it never moves the state folder.
        (self.program_data / "AnyAiCam" / "config" / "vms.env").write_text(
            "ANYAICAM_STATE_DIR=C:\\elsewhere\nANYAICAM_HTTP_PORT=8000\n", encoding="utf-8")

    def run_launcher(self, name):
        text = read(name)
        self.assertEqual(len(PYTHON_LINE.findall(text)), 1)
        text = PYTHON_LINE.sub("Get-ChildItem env:ANYAICAM_* | ForEach-Object { \"$($_.Name)=$($_.Value)\" }", text)
        script = self.install / "service" / name
        script.write_text(text, encoding="utf-8")
        env = {**os.environ, "ProgramData": str(self.program_data)}
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(script)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def test_vms_and_agent_use_the_same_protected_state_folder(self):
        vms = self.run_launcher("service-launcher.ps1")
        agent = self.run_launcher("agent-launcher.ps1")
        expected = str(self.program_data / "AnyAiCam" / "agent")
        self.assertEqual(vms["ANYAICAM_STATE_DIR"], expected)
        self.assertEqual(agent["ANYAICAM_STATE_DIR"], expected)


if __name__ == "__main__":
    unittest.main()
